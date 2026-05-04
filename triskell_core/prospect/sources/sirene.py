"""
Source Sirene — API publique des entreprises françaises (data.gouv.fr).

Utilise recherche-entreprises.api.gouv.fr (gratuit, sans clé, 7 req/sec) :
https://recherche-entreprises.api.gouv.fr/

Permet de cibler par :
- code NAF (ex : 4321A = travaux d'installation électrique)
- localisation (département / région / code postal)
- date de création (entreprises < N mois = early adopters chauds)
- effectif (TPE, PME)
- état (actif uniquement)

Renvoie nom, SIREN, adresse, téléphone si dispo, mais PAS d'email
(la base SIRENE n'en contient pas). L'email vient ensuite via le Web Enricher
sur le site dérivé du nom.
"""

from __future__ import annotations

import logging
import time
from typing import Iterator

import requests

from ..core.prospect import Prospect, Source

log = logging.getLogger(__name__)


BASE_URL = "https://recherche-entreprises.api.gouv.fr/search"
USER_AGENT = "TriskellProspect/0.1 (+https://triskell.studio/bot)"
MIN_INTERVAL = 0.15  # 7 req/s plafond officiel ; on reste très en dessous
MAX_PAGES = 25       # API plafonne à 25 pages × 25 = 625 résultats


def search(
    *,
    activite_principale: str = "",  # code NAF, ex: "43.21A"
    departement: str = "",          # ex: "35" pour Ille-et-Vilaine
    code_postal: str = "",
    region: str = "",
    section_activite: str = "",     # lettre A-U (NAF section)
    nom_entreprise: str = "",
    min_date_creation: str = "",    # YYYY-MM-DD inclus
    max_date_creation: str = "",
    tranche_effectif: str = "",     # ex: "00" (0 salarié), "01" (1-2)…
    etat: str = "A",                # A = actif uniquement
    per_page: int = 25,
    max_results: int = 200,
) -> Iterator[Prospect]:
    """
    Itère les entreprises matchant les critères, converties au format unifié.

    Exemple — électriciens TPE créés < 6 mois en Ille-et-Vilaine :
        search(
            activite_principale="43.21A",
            departement="35",
            min_date_creation="2025-11-04",
            tranche_effectif="00",
            max_results=100,
        )
    """
    params_base: dict[str, str | int] = {
        "page": 1,
        "per_page": min(per_page, 25),
        "etat_administratif": etat,
    }
    if activite_principale:
        params_base["activite_principale"] = activite_principale
    if departement:
        params_base["departement"] = departement
    if code_postal:
        params_base["code_postal"] = code_postal
    if region:
        params_base["region"] = region
    if section_activite:
        params_base["section_activite_principale"] = section_activite
    if nom_entreprise:
        params_base["q"] = nom_entreprise
    if min_date_creation:
        params_base["min_date_creation"] = min_date_creation
    if max_date_creation:
        params_base["max_date_creation"] = max_date_creation
    if tranche_effectif:
        params_base["tranche_effectif_salarie"] = tranche_effectif

    fetched = 0
    last = 0.0

    for page in range(1, MAX_PAGES + 1):
        if fetched >= max_results:
            return
        delta = time.time() - last
        if delta < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - delta)
        params = dict(params_base, page=page)
        try:
            r = requests.get(
                BASE_URL,
                params=params,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=15,
            )
            last = time.time()
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("Sirene page %d a échoué : %s", page, e)
            return

        results = data.get("results", []) or []
        if not results:
            return

        for item in results:
            if fetched >= max_results:
                return
            try:
                prospect = _convert(item)
            except Exception as e:
                log.debug("conversion sirene a échoué : %s", e)
                continue
            if prospect is None:
                continue
            fetched += 1
            yield prospect

        # Pagination par défaut de l'API
        total_pages = data.get("total_pages", 1) or 1
        if page >= total_pages:
            return


def _convert(item: dict) -> Prospect | None:
    """Convertit un résultat SIRENE en Prospect."""
    siren = item.get("siren") or ""
    name = item.get("nom_complet") or item.get("nom_raison_sociale") or ""
    if not siren or not name:
        return None

    siege = item.get("siege") or {}
    address_parts = []
    for k in ("numero_voie", "type_voie", "libelle_voie"):
        v = siege.get(k)
        if v:
            address_parts.append(str(v))
    address_line = " ".join(address_parts)
    postal_code = siege.get("code_postal") or ""
    city = siege.get("libelle_commune") or ""

    # NAF
    activite = item.get("activite_principale") or ""
    libelle_activite = item.get("libelle_activite_principale") or ""

    return Prospect(
        name=name,
        legal_name=name,
        siren=siren,
        emails=[],   # Sirene n'expose pas d'email — sera comblé par WebEnricher
        phones=[],   # idem (rare dans Sirene v3)
        website="",  # idem
        address=address_line,
        postal_code=postal_code,
        city=city,
        country="FR",
        industry=libelle_activite,
        naf_code=activite,
        description=f"{libelle_activite} · SIREN {siren} · {city}".strip(" ·"),
        language="fr",
        sources=[Source(name="sirene", source_id=siren)],
    )
