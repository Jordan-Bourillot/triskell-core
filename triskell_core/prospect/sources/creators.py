"""Convertit les résultats bruts YouTube/Twitch/Reddit vers Prospect unifié.

Wrapper de haut niveau qui combine :
- Recherche brute (youtube.YouTubeAPI, twitch.TwitchAPI, reddit.RedditAPI)
- Détection de monétisation (enrichers/monetization.py)
- Extraction emails/phones depuis la bio
- Conversion vers le schéma core.prospect.Prospect

Permet à Le Dénicheur ET Triskell Command de réutiliser exactement les mêmes
sources, tout en peuplant le même CRM unifié (~/.triskell-prospect/prospects.json).
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from ..core.prospect import Prospect, Source
from ..enrichers.monetization import detect_monetization, extract_contacts


def search_youtube(
    api,
    query: str,
    *,
    max_results: int = 50,
    include_monetized: bool = True,
) -> Iterable[Prospect]:
    """Recherche YouTube + conversion vers Prospect.

    Args:
        api: instance youtube.YouTubeAPI déjà configurée
        query: mot-clé de niche
        max_results: cap par run (cap dur YouTube = 100)
        include_monetized: si False, exclut les créateurs déjà monétisés
    """
    if not api.available():
        return
    ids = api.search_channels(query, max_results=max_results)
    if not ids:
        return
    for raw in api.get_channels_details(ids):
        prospect = _from_raw(raw)
        if not prospect:
            continue
        if not include_monetized and prospect.monetized:
            continue
        yield prospect


def search_twitch(
    api,
    query: str,
    *,
    max_results: int = 50,
    include_monetized: bool = True,
) -> Iterable[Prospect]:
    if not api.available():
        return
    raw_channels = api.search_channels(query, max_results=max_results)
    if not raw_channels:
        return
    raw_channels = api.enrich_with_user_info(raw_channels)
    for raw in raw_channels:
        prospect = _from_raw(raw)
        if not prospect:
            continue
        if not include_monetized and prospect.monetized:
            continue
        yield prospect


def search_reddit(
    api,
    query: str,
    *,
    max_results: int = 25,
    kind: str = "both",  # "subreddit" | "user" | "both"
    include_monetized: bool = True,
) -> Iterable[Prospect]:
    raw_list: list[dict] = []
    if kind in ("both", "subreddit"):
        limit = max_results // 2 if kind == "both" else max_results
        raw_list.extend(api.search_subreddits(query, max_results=limit))
    if kind in ("both", "user"):
        limit = max_results // 2 if kind == "both" else max_results
        raw_list.extend(api.search_users(query, max_results=limit))
    for raw in raw_list:
        prospect = _from_raw(raw)
        if not prospect:
            continue
        if not include_monetized and prospect.monetized:
            continue
        yield prospect


# ---------------------------------------------------------------------------
# Conversion brut → Prospect unifié
# ---------------------------------------------------------------------------
def _from_raw(raw: dict) -> Prospect | None:
    """Convertit un dict brut (issu de YouTubeAPI/TwitchAPI/RedditAPI) en Prospect."""
    if not raw or not raw.get("id"):
        return None

    desc = raw.get("description", "") or ""
    monetization = detect_monetization(desc)
    contacts = extract_contacts(desc)
    platform = raw.get("platform", "")
    return Prospect(
        name=raw.get("name", "") or "",
        handle=raw.get("handle", "") or "",
        emails=list(contacts["emails"]),
        phones=list(contacts["phones"]),
        other_urls=list(monetization.get("urls", []))[:8],
        country=raw.get("country", "") or "",
        language=raw.get("language", "") or "",
        industry=platform,                 # plateforme = "secteur" pour les créateurs
        description=desc[:2000],
        monetized=bool(monetization.get("monetized")),
        monetization_reasons=list(monetization.get("reasons", [])),
        subscribers=raw.get("subscribers"),
        platform_url=raw.get("url", "") or "",
        sources=[Source(
            name=platform,
            source_id=str(raw.get("id", "")),
            url=raw.get("url", "") or "",
            found_at=datetime.now().isoformat(timespec="seconds"),
        )],
    )
