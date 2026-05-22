"""Source OpenStreetMap — commerces locaux, artisans, lieux.

Idée : OSM est une cartographie collaborative où les contributeurs ajoutent
les commerces, restos, cabinets, asso, etc. La policy OSM **interdit** aux
contributeurs d'ajouter des emails non publics → tout ce qui est dans la
base est par construction RGPD-safe.

Pipeline :
1. **Recherche** via l'API Overpass (gratuite, sans clé, fair use ~10k req/j) :
   - On filtre par catégorie (`amenity`, `shop`, `craft`, `office`...) et zone
     géographique (commune, département, pays).
   - On peut imposer la présence du tag `contact:email` ou `email` pour
     ne sortir que les POI avec email.
2. **Output** : chaque POI devient un prospect avec name, ville, email,
   téléphone, site web — directement utilisable.

Légalité : licence ODbL (redistribution autorisée avec attribution).
"""
from __future__ import annotations

import logging
from typing import Iterable

from ._http import SourceHttpError, get_text

log = logging.getLogger(__name__)


OVERPASS_URL = "https://overpass-api.de/api/interpreter"


# Catégories OSM les plus utiles pour la prospection B2B locale.
# Chaque entrée = (libellé humain, tag OSM, valeur).
CATEGORIES: dict[str, list[tuple[str, str]]] = {
    "restaurants":     [("amenity", "restaurant"), ("amenity", "cafe"),
                        ("amenity", "bar"), ("amenity", "fast_food")],
    "commerces":       [("shop", ".*")],
    "artisans":        [("craft", ".*")],
    "bureaux":         [("office", ".*")],
    "sante":           [("amenity", "doctors"), ("amenity", "dentist"),
                        ("amenity", "pharmacy"), ("amenity", "veterinary"),
                        ("healthcare", ".*")],
    "education":       [("amenity", "school"), ("amenity", "kindergarten"),
                        ("amenity", "language_school"), ("amenity", "music_school")],
    "tourisme":        [("tourism", "hotel"), ("tourism", "guest_house"),
                        ("tourism", "apartment"), ("tourism", "chalet")],
    "services":        [("amenity", "post_office"), ("amenity", "bank"),
                        ("office", "lawyer"), ("office", "accountant"),
                        ("office", "estate_agent"), ("office", "insurance")],
    "associations":    [("office", "association"), ("amenity", "community_centre")],
}


class OSMAPI:
    """Client Overpass — recherche géolocalisée de POI avec email obligatoire."""

    def __init__(self) -> None:
        pass

    def available(self) -> bool:
        return True

    def search_local(
        self,
        *,
        category: str = "restaurants",
        area: str = "France",
        max_results: int = 50,
        require_email: bool = True,
    ) -> list[dict]:
        """Retourne les POI d'une catégorie dans une zone donnée.

        Args:
            category : clé de CATEGORIES (restaurants, commerces, artisans...).
                       Peut aussi être un tag direct au format "key=value"
                       (ex: "amenity=bakery").
            area     : nom de la zone OSM (commune, département, pays).
                       Doit matcher exactement un `name` taggé dans OSM.
                       Exemples : "Rennes", "Bretagne", "France",
                       "Île-de-France", "75001".
            require_email : si True, ne ramène que les POI avec un email.
                       Réduit drastiquement le volume mais 100% utiles.
            max_results : cap output (Overpass renvoie tout par défaut).
        """
        filters = self._build_filters(category)
        if not filters:
            raise ValueError(f"catégorie OSM inconnue : {category}")

        # Construit la requête Overpass QL.
        # Note : Overpass est strict sur la syntaxe. On utilise :
        # area["name"="X"]->.a; puis nwr[...](area.a); out body N;
        # Pour le filtre email : beaucoup d'établissements taggent juste
        # "email" (forme historique) et d'autres "contact:email" (forme
        # recommandée). On émet une ligne par tag email pour ne rater
        # aucun des deux — sinon on perd ~50% des résultats.
        if require_email:
            email_tags = ['["contact:email"]', '["email"]']
        else:
            email_tags = [""]
        filter_lines_list: list[str] = []
        for k, v in filters:
            tag_filter = self._tag_filter(k, v)
            for ef in email_tags:
                filter_lines_list.append(
                    f'  nwr[{tag_filter}]{ef}(area.a);'
                )
        filter_lines = "\n".join(filter_lines_list)

        # Cap dur sur le nombre demandé : Overpass est lent en France entière.
        # Timeout serveur côté Overpass (90s) > timeout réseau côté client (120s).
        # Match case-insensitive sur le nom de l'area : OSM stocke "Lyon" /
        # "Île-de-France" / "France" avec leur casse exacte ; l'utilisateur
        # tape souvent "lyon" en minuscule → sans le flag `,i` Overpass
        # retourne 0 area, donc 0 POI sans message d'erreur.
        # On émet aussi `.a out ids;` pour détecter quand l'area est vide
        # et lever un message explicite (avant : 0 bruts silencieux).
        area_pattern = self._escape_regex(area)
        ql = (
            f'[out:json][timeout:90];\n'
            f'area["name"~"^{area_pattern}$",i]->.a;\n'
            f'.a out ids;\n'
            f'(\n{filter_lines}\n);\n'
            f'out body {max_results};\n'
        )

        try:
            text = get_text(
                OVERPASS_URL,
                params={"data": ql},
                headers={
                    "User-Agent": "Obelisk-Triskell/1.0 (prospection commerce local)",
                    "Accept": "application/json",
                },
                max_retries=2,
                timeout=120,
            )
        except SourceHttpError as e:
            raise RuntimeError(f"OpenStreetMap a échoué : {e}") from e

        import json
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Overpass renvoie parfois du HTML d'erreur (timeout, rate limit,
            # area inconnue). On lève une vraie exception pour que l'utilisateur
            # comprenne dans le log (avant : silencieux → 0 bruts mystérieux).
            snippet = (text or "")[:200].strip().replace("\n", " ")
            raise RuntimeError(
                f"OpenStreetMap : Overpass a renvoyé une réponse non-JSON "
                f"(timeout, rate-limit, ou zone « {area} » introuvable). "
                f"Essaie une zone plus petite (ville plutôt que pays). "
                f"Début de la réponse : {snippet}"
            )
        elements = data.get("elements", []) or []

        # On a demandé `.a out ids;` en premier : si aucun élément de type
        # "area" n'est présent dans la réponse, c'est que la zone n'existe
        # pas dans OSM (faute de frappe, accents oubliés, etc.). Avant
        # cette détection : 0 POI silencieux et l'utilisateur ne savait pas
        # pourquoi.
        has_area = any(e.get("type") == "area" for e in elements)
        if not has_area:
            raise RuntimeError(
                f"OpenStreetMap : zone « {area} » introuvable. "
                f"Vérifie l'orthographe exacte (avec accents si besoin) "
                f"— ex : « Lyon », « Île-de-France », « Saint-Étienne »."
            )

        # Dédup par id (les multiples lignes nwr peuvent retourner le même POI
        # plusieurs fois s'il a à la fois contact:email et email).
        # On filtre aussi le marqueur d'area lui-même renvoyé par `.a out ids;`.
        seen: set[str] = set()
        unique: list[dict] = []
        for e in elements:
            if e.get("type") == "area":
                continue
            key = f"{e.get('type')}:{e.get('id')}"
            if key in seen:
                continue
            seen.add(key)
            unique.append(e)
        return [self._to_prospect(e, category) for e in unique
                if self._has_useful_data(e)]

    @staticmethod
    def _build_filters(category: str) -> list[tuple[str, str]]:
        if "=" in category:
            k, v = category.split("=", 1)
            return [(k.strip(), v.strip())]
        return CATEGORIES.get(category, [])

    @staticmethod
    def _tag_filter(key: str, value: str) -> str:
        if value == ".*":
            # Tag présent peu importe la valeur
            return f'"{key}"'
        return f'"{key}"="{value}"'

    @staticmethod
    def _escape(s: str) -> str:
        return s.replace('"', '').replace('\\', '')

    @staticmethod
    def _escape_regex(s: str) -> str:
        # On passe la zone dans une regex Overpass entre ^ et $.
        # Échappe les guillemets, antislashs, et les méta-caractères regex
        # qui apparaissent légitimement dans des noms de communes
        # (parenthèses, points dans les abréviations, traits d'union dans
        # "Saint-Étienne" — le `-` n'a pas besoin d'échappement hors
        # crochets, mais on traite tout par sécurité).
        import re
        cleaned = s.replace('"', '').replace('\\', '')
        return re.escape(cleaned)

    @staticmethod
    def _has_useful_data(e: dict) -> bool:
        tags = e.get("tags") or {}
        return bool(tags.get("name"))

    def _to_prospect(self, e: dict, category: str) -> dict:
        tags = e.get("tags") or {}
        emails = []
        for k in ("contact:email", "email"):
            v = tags.get(k)
            if v:
                for chunk in v.split(";"):
                    chunk = chunk.strip().lower()
                    if "@" in chunk and chunk not in emails:
                        emails.append(chunk)

        phones = []
        for k in ("contact:phone", "phone"):
            v = tags.get(k)
            if v and v not in phones:
                phones.append(v.strip())

        website = (tags.get("contact:website") or tags.get("website") or "").strip()
        if website and not website.startswith(("http://", "https://")):
            website = "https://" + website

        city = (tags.get("addr:city") or "").strip()
        postal = (tags.get("addr:postcode") or "").strip()
        country = (tags.get("addr:country") or "").strip().upper() or "FR"

        # Type humain (restaurant, bakery, dentist...) pour la description
        type_tag = ""
        for k in ("amenity", "shop", "craft", "office", "tourism",
                  "healthcare", "leisure"):
            if tags.get(k):
                type_tag = f"{k}={tags[k]}"
                break

        desc_parts = [type_tag] if type_tag else []
        if tags.get("description"):
            desc_parts.append(tags["description"])
        if tags.get("cuisine"):
            desc_parts.append(f"cuisine: {tags['cuisine']}")
        if tags.get("addr:street"):
            addr = " ".join(filter(None, [
                tags.get("addr:housenumber") or "",
                tags["addr:street"],
                postal, city,
            ]))
            desc_parts.append(addr)
        desc = "\n".join(desc_parts)[:2000]

        return {
            "platform":    "osm",
            "id":          f"osm:{e.get('type')}:{e.get('id')}",
            "name":        tags.get("name") or "",
            "handle":      "",
            "subscribers": None,
            "subs_hidden": True,
            "description": desc,
            "thumbnail":   "",
            "url":         (f"https://www.openstreetmap.org/"
                            f"{e.get('type')}/{e.get('id')}"),
            "emails":      emails,
            "phones":      phones,
            "website":     website,
            "city":        city,
            "postal_code": postal,
            "country":     country,
            "language":    "fr" if country == "FR" else "",
            "urls_in_bio": [website] if website else [],
        }


__all__ = ["OSMAPI", "CATEGORIES"]
