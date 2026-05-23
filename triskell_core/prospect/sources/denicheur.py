"""
Source Le Dénicheur — importe les prospects existants de l'app Le Dénicheur
(~/.ledenicheur/prospects.json) vers le format unifié Prospect.

Lecture seule : on ne touche jamais au fichier source.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from ..core.prospect import Prospect, Source


DENICHEUR_FILE = Path.home() / ".ledenicheur" / "prospects.json"


def is_available() -> bool:
    return DENICHEUR_FILE.exists()


def import_all() -> Iterator[Prospect]:
    """Itère les prospects Le Dénicheur convertis au format unifié."""
    if not DENICHEUR_FILE.exists():
        return
    try:
        data = json.loads(DENICHEUR_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(data, list):
        return
    for raw in data:
        try:
            yield _convert(raw)
        except Exception:
            continue


def _convert(raw: dict) -> Prospect:
    platform = raw.get("platform", "")
    desc = raw.get("description", "") or ""

    # URL externe = la 1re URL non-social parmi urls_in_bio
    other_urls = [u for u in (raw.get("urls_in_bio") or [])
                  if isinstance(u, str)]
    website = ""
    # On ne préempte pas le website ici : la détection commerciale du Dénicheur
    # mélange plein de patterns. On reposera la question lors de l'enrichissement.

    emails_list = list(raw.get("emails") or [])
    # Si le pipeline d'enrichissement a déjà tagué la source de chaque
    # email (web / linktree / etc.), on la respecte. Sinon : source par
    # défaut = la plateforme du créateur (bio profil).
    raw_meta = raw.get("emails_meta")
    if isinstance(raw_meta, list) and raw_meta:
        emails_meta_list = [
            m for m in raw_meta
            if isinstance(m, dict) and m.get("email")
        ]
    else:
        emails_meta_list = [
            {
                "email": e,
                "source": f"obelisk_{platform}" if platform else "obelisk",
                "source_id": str(raw.get("id", "") or ""),
                "url": raw.get("url", "") or "",
                "context": (f"bio / profil {platform}" if platform
                            else "profil créateur"),
                "found_at": raw.get("found_at") or "",
            }
            for e in emails_list if e
        ]
    p = Prospect(
        name=raw.get("name", "") or "",
        handle=raw.get("handle", "") or "",
        emails=emails_list,
        emails_meta=emails_meta_list,
        phones=list(raw.get("phones") or []),
        website=website,
        other_urls=other_urls,
        country=raw.get("country", "") or "",
        language=raw.get("language", "") or "",
        industry=platform,
        description=desc[:2000],
        monetized=bool(raw.get("monetized")),
        monetization_reasons=list(raw.get("monetization_reasons") or []),
        subscribers=raw.get("subscribers"),
        platform_url=raw.get("url", "") or "",
        status=_map_status(raw.get("status", "new")),
        tags=list(raw.get("tags") or []),
        notes=raw.get("notes", "") or "",
        sources=[
            Source(
                name="denicheur",
                source_id=f"{platform}|{raw.get('id', '')}",
                url=raw.get("url", "") or "",
                found_at=raw.get("found_at") or "",
            )
        ],
    )
    return p


def _map_status(s: str) -> str:
    """Map les statuts internes Le Dénicheur vers le vocabulaire unifié."""
    s = (s or "").lower().strip()
    return {
        "new": "new",
        "à contacter": "qualified",
        "a contacter": "qualified",
        "to_contact": "qualified",
        "contacté": "contacted",
        "contacte": "contacted",
        "contacted": "contacted",
        "a répondu": "replied",
        "a repondu": "replied",
        "replied": "replied",
        "refus": "refused",
        "refused": "refused",
    }.get(s, "new")
