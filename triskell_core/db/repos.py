"""Repositories — convertissent les rows Supabase en dataclasses Triskell
et inversement. Une fonction par table.

Pourquoi un fichier séparé du client : permet de tester chaque conversion
sans toucher au réseau (on passe une row dict en entrée, on vérifie l'objet
en sortie).

Toutes les fonctions `to_*` partent d'une row dict et renvoient le bon
objet ; toutes les fonctions `from_*` partent de l'objet et renvoient une
row dict prête pour `.upsert()` ou `.insert()`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime
from typing import Any

from ..prospect.core.prospect import Prospect, Source

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Conversions Prospect ↔ row Supabase
# ---------------------------------------------------------------------------
def prospect_to_row(p: Prospect) -> dict[str, Any]:
    """Sérialise un Prospect en row pour la table `prospects`.

    Les listes complexes (sources, history, pending_drafts) sont en JSONB.
    history et pending_drafts NE sont PAS dans la table `prospects` —
    ils ont leurs propres tables (`email_history` et `prospect_drafts`).
    """
    return {
        "name": p.name or "",
        "handle": p.handle or "",
        "legal_name": p.legal_name or "",
        "siren": p.siren or "",
        "emails": list(p.emails or []),
        "emails_meta": list(p.emails_meta or []),
        "phones": list(p.phones or []),
        "website": p.website or "",
        "other_urls": list(p.other_urls or []),
        "address": p.address or "",
        "city": p.city or "",
        "postal_code": p.postal_code or "",
        "country": p.country or "",
        "industry": p.industry or "",
        "naf_code": p.naf_code or "",
        "description": p.description or "",
        "language": p.language or "",
        "monetized": bool(p.monetized),
        "monetization_reasons": list(p.monetization_reasons or []),
        "has_legal_mentions": bool(p.has_legal_mentions),
        "score": int(p.score or 0),
        "score_label": p.score_label or "",
        "subscribers": p.subscribers,
        "platform_url": p.platform_url or "",
        "status": p.status or "new",
        "tags": list(p.tags or []),
        "notes": p.notes or "",
        "last_contact_at": _to_iso(p.last_contact_at),
        "sources": [_source_to_dict(s) for s in (p.sources or [])],
        "match_keys": list(p.match_keys),
    }


def row_to_prospect(row: dict[str, Any]) -> Prospect:
    p = Prospect()
    p.name = row.get("name") or ""
    p.handle = row.get("handle") or ""
    p.legal_name = row.get("legal_name") or ""
    p.siren = row.get("siren") or ""
    p.emails = list(row.get("emails") or [])
    # emails_meta peut être absent (anciens prospects) — on tolère.
    raw_meta = row.get("emails_meta") or []
    if isinstance(raw_meta, list):
        p.emails_meta = [m for m in raw_meta
                          if isinstance(m, dict) and m.get("email")]
    p.phones = list(row.get("phones") or [])
    p.website = row.get("website") or ""
    p.other_urls = list(row.get("other_urls") or [])
    p.address = row.get("address") or ""
    p.city = row.get("city") or ""
    p.postal_code = row.get("postal_code") or ""
    p.country = row.get("country") or ""
    p.industry = row.get("industry") or ""
    p.naf_code = row.get("naf_code") or ""
    p.description = row.get("description") or ""
    p.language = row.get("language") or ""
    p.monetized = bool(row.get("monetized"))
    p.monetization_reasons = list(row.get("monetization_reasons") or [])
    p.has_legal_mentions = bool(row.get("has_legal_mentions"))
    p.score = int(row.get("score") or 0)
    p.score_label = row.get("score_label") or ""
    p.subscribers = row.get("subscribers")
    p.platform_url = row.get("platform_url") or ""
    p.status = row.get("status") or "new"
    p.tags = list(row.get("tags") or [])
    p.notes = row.get("notes") or ""
    p.last_contact_at = _from_iso(row.get("last_contact_at"))
    p.sources = [_dict_to_source(s) for s in (row.get("sources") or [])]
    p.created_at = _from_iso(row.get("created_at")) or p.created_at
    p.updated_at = _from_iso(row.get("updated_at")) or p.updated_at
    return p


def _source_to_dict(s: Source) -> dict[str, Any]:
    return {
        "name": s.name or "",
        "source_id": s.source_id or "",
        "url": s.url or "",
        "found_at": s.found_at or "",
    }


def _dict_to_source(d: dict[str, Any]) -> Source:
    return Source(
        name=d.get("name") or "",
        source_id=d.get("source_id") or "",
        url=d.get("url") or "",
        found_at=d.get("found_at") or datetime.now().isoformat(timespec="seconds"),
    )


# ---------------------------------------------------------------------------
# Conversions email_history ↔ row
# ---------------------------------------------------------------------------
def history_event_to_row(prospect_id: str, event: dict[str, Any],
                          *, created_by: str | None = None) -> dict[str, Any]:
    """L'ancien Prospect.history[i] → row dans email_history."""
    return {
        "prospect_id": prospect_id,
        "kind": event.get("kind", "email_sent"),
        "ts": event.get("ts") or datetime.now().isoformat(timespec="seconds"),
        "subject": event.get("subject", ""),
        "body": event.get("body", ""),
        "template_key": event.get("template_key", ""),
        "provider": event.get("provider", ""),
        "model": event.get("model", ""),
        "message_id": event.get("message_id", ""),
        "extra": {k: v for k, v in event.items()
                  if k not in {"kind", "ts", "subject", "body",
                                "template_key", "provider", "model",
                                "message_id"}},
        "created_by": created_by,
    }


def row_to_history_event(row: dict[str, Any]) -> dict[str, Any]:
    out = {
        "kind": row.get("kind", ""),
        "ts": row.get("ts", ""),
        "subject": row.get("subject", ""),
        "body": row.get("body", ""),
        "template_key": row.get("template_key", ""),
        "provider": row.get("provider", ""),
        "model": row.get("model", ""),
        "message_id": row.get("message_id", ""),
    }
    extra = row.get("extra") or {}
    if isinstance(extra, dict):
        for k, v in extra.items():
            if k not in out:
                out[k] = v
    return out


# ---------------------------------------------------------------------------
# Conversions prospect_drafts ↔ row (les drafts auto-pilote du Dénicheur)
# ---------------------------------------------------------------------------
def draft_dict_to_row(prospect_id: str, draft: dict[str, Any],
                       *, created_by: str | None = None) -> dict[str, Any]:
    return {
        "prospect_id": prospect_id,
        "subject": draft.get("subject", ""),
        "body": draft.get("body", ""),
        "template_key": draft.get("template_key", ""),
        "provider": draft.get("provider", ""),
        "model": draft.get("model", ""),
        "kind": draft.get("kind", "first_contact"),
        "status": draft.get("status", "pending"),
        "created_by": created_by,
    }


def row_to_draft_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "ts": row.get("created_at", ""),
        "subject": row.get("subject", ""),
        "body": row.get("body", ""),
        "template_key": row.get("template_key", ""),
        "provider": row.get("provider", ""),
        "model": row.get("model", ""),
        "kind": row.get("kind", "first_contact"),
        "status": row.get("status", "pending"),
    }


# ---------------------------------------------------------------------------
# Helpers ISO datetime
# ---------------------------------------------------------------------------
def _to_iso(v: Any) -> str | None:
    if not v:
        return None
    if isinstance(v, str):
        return v
    try:
        return v.isoformat()
    except Exception:
        return str(v)


def _from_iso(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    try:
        return v.isoformat()
    except Exception:
        return str(v)
