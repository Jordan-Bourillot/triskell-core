"""
Source Google Maps Places — pour les TPE/commerces qui n'ont pas forcément
de site web mais SONT sur Google Maps avec téléphone + adresse + horaires.

API : Google Places API (New) — https://developers.google.com/maps/documentation/places/web-service/text-search
- Endpoint Text Search :
  POST https://places.googleapis.com/v1/places:searchText
- Coût : ~32 $/1000 requêtes Text Search ; 200 $/mois offerts par Google
  → ~6000 recherches gratuites par mois, largement suffisant.
- Aucun renvoi vers Place Details (qui serait extra-billed) : on prend tout
  ce qui est utile via le `fieldMask` du searchText.

Configuration : nécessite une clé API Google Cloud avec "Places API (New)" activée.
Stockée dans ~/.triskell-prospect/config.json sous la clé "google_places_api_key".

Si la clé n'est pas définie, la source renvoie un message clair et 0 résultat.
"""

from __future__ import annotations

import json
import logging
from typing import Iterator

import requests

from ..core.crm import CONFIG_FILE
from ..core.prospect import Prospect, Source

log = logging.getLogger(__name__)


PLACES_TEXT_SEARCH = "https://places.googleapis.com/v1/places:searchText"
USER_AGENT = "TriskellProspect/0.1"

# Champs renvoyés par Places API. Plus on demande de champs, plus le tier de
# facturation grimpe — ici on reste sur le tier "Text Search Pro" qui inclut
# tout ce dont on a besoin.
FIELD_MASK = ",".join([
    "places.displayName",
    "places.formattedAddress",
    "places.shortFormattedAddress",
    "places.nationalPhoneNumber",
    "places.internationalPhoneNumber",
    "places.websiteUri",
    "places.types",
    "places.primaryType",
    "places.id",
    "places.businessStatus",
    "places.rating",
    "places.userRatingCount",
    "places.location",
    "places.googleMapsUri",
    "nextPageToken",
])


def is_configured() -> bool:
    return bool(_load_api_key())


def _load_api_key() -> str:
    if not CONFIG_FILE.exists():
        return ""
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return ""
    return (cfg.get("google_places_api_key") or "").strip()


def search(
    *,
    text_query: str,
    location_bias_lat: float | None = None,
    location_bias_lng: float | None = None,
    radius_m: int = 50_000,
    language: str = "fr",
    region: str = "FR",
    max_results: int = 60,
    lat_offset_m: float = 0.0,
    lng_offset_m: float = 0.0,
    cursor_out: dict | None = None,
) -> Iterator[Prospect]:
    """
    Recherche Places par requête textuelle.

    Exemple :
        search(text_query="boulangerie Rennes", max_results=40)
        search(text_query="restaurant", location_bias_lat=48.117, location_bias_lng=-1.677, radius_m=10000)

    Note : Places API New limite à 60 résultats max par requête (paginés en 3 pages).

    lat_offset_m / lng_offset_m : décalent le centre de la zone de recherche
    pour permettre au pipeline de balayer une grille géographique entre les
    runs successifs (sinon on retombe toujours sur le même top de zone).
    """
    api_key = _load_api_key()
    if not api_key:
        log.warning(
            "Maps Places API non configurée. Ajoute ta clé dans %s sous "
            '"google_places_api_key" pour activer cette source.',
            CONFIG_FILE,
        )
        return

    # Application des offsets (conversion mètres → degrés)
    lat = location_bias_lat
    lng = location_bias_lng
    if lat is not None and lng is not None and (lat_offset_m or lng_offset_m):
        import math
        # 1 degré latitude ≈ 111 320 m
        dlat = lat_offset_m / 111_320.0
        # 1 degré longitude ≈ 111 320 m × cos(lat)
        cos_lat = max(0.01, math.cos(math.radians(lat)))
        dlng = lng_offset_m / (111_320.0 * cos_lat)
        lat = lat + dlat
        lng = lng + dlng

    payload: dict = {
        "textQuery": text_query,
        "languageCode": language,
        "regionCode": region,
        "pageSize": min(20, max_results),
    }
    if lat is not None and lng is not None:
        payload["locationBias"] = {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": float(radius_m),
            }
        }

    fetched = 0
    next_token = None

    while fetched < max_results:
        if next_token:
            payload["pageToken"] = next_token
        try:
            r = requests.post(
                PLACES_TEXT_SEARCH,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": api_key,
                    "X-Goog-FieldMask": FIELD_MASK,
                    "User-Agent": USER_AGENT,
                },
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("Maps Places search a échoué : %s", e)
            return

        places = data.get("places", []) or []
        if not places:
            if cursor_out is not None:
                cursor_out["cell_exhausted"] = True
                cursor_out["yielded"] = fetched
            return

        for item in places:
            if fetched >= max_results:
                if cursor_out is not None:
                    cursor_out["cell_exhausted"] = False
                    cursor_out["yielded"] = fetched
                return
            try:
                prospect = _convert(item)
            except Exception as e:
                log.debug("conversion maps a échoué : %s", e)
                continue
            if prospect is None:
                continue
            fetched += 1
            yield prospect

        next_token = data.get("nextPageToken")
        if not next_token:
            if cursor_out is not None:
                cursor_out["cell_exhausted"] = True
                cursor_out["yielded"] = fetched
            return
        # Le pageToken doit être propagé tel quel ; on retire le textQuery
        # (l'API exige uniquement pageToken pour la suite).
        payload = {"pageToken": next_token}

    if cursor_out is not None:
        cursor_out["cell_exhausted"] = False
        cursor_out["yielded"] = fetched


def _convert(item: dict) -> Prospect | None:
    name = (item.get("displayName") or {}).get("text") or ""
    if not name:
        return None
    place_id = item.get("id") or ""
    formatted_addr = item.get("formattedAddress") or ""
    short_addr = item.get("shortFormattedAddress") or ""
    phone_intl = item.get("internationalPhoneNumber") or ""
    phone_nat = item.get("nationalPhoneNumber") or ""
    website = item.get("websiteUri") or ""
    primary = item.get("primaryType") or ""
    types = item.get("types") or []
    status = item.get("businessStatus") or ""
    rating = item.get("rating")
    rating_count = item.get("userRatingCount", 0)
    gmaps_uri = item.get("googleMapsUri") or ""

    # Skip les commerces fermés
    if status in ("CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"):
        return None

    # Décompose l'adresse pour extraire ville + code postal si possible
    city, postal = _split_address_fr(formatted_addr or short_addr)

    phones = []
    if phone_intl:
        phones.append(phone_intl)
    elif phone_nat:
        phones.append(phone_nat)

    description_bits = [primary or " / ".join(types[:2])]
    if rating:
        description_bits.append(f"⭐ {rating} ({rating_count} avis)")
    description = " · ".join(b for b in description_bits if b)

    return Prospect(
        name=name,
        emails=[],
        phones=phones,
        website=website,
        address=formatted_addr,
        city=city,
        postal_code=postal,
        country="FR",
        industry=primary or (types[0] if types else ""),
        description=description,
        language="fr",
        platform_url=gmaps_uri,
        sources=[Source(name="maps", source_id=place_id, url=gmaps_uri)],
    )


def _split_address_fr(addr: str) -> tuple[str, str]:
    """Extrait (ville, code_postal) d'une adresse formatée FR.

    Format Google Maps FR typique : "12 rue X, 35000 Rennes, France"
    """
    if not addr:
        return ("", "")
    parts = [p.strip() for p in addr.split(",")]
    # Cherche une partie qui commence par 5 chiffres (code postal FR)
    import re as _re
    for part in parts:
        m = _re.match(r"^(\d{5})\s+(.+)$", part)
        if m:
            return (m.group(2).strip(), m.group(1))
    return ("", "")
