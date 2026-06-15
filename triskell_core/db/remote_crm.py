"""RemoteCRM — implémente l'API du CRM local mais lit/écrit dans Supabase.

Compat : expose la même surface publique que `CRM` (`all()`, `find()`,
`upsert()`, `save()`, `__len__`, `_dirty`) pour que tout le code existant
(LeDenicheur.py, Triskell Command, scripts CLI) puisse être basculé en
changeant juste 1 import.

Stratégie d'écriture :
- `upsert(prospect)` : on cherche par `match_keys` (stockés en JSONB),
  si trouvé → UPDATE, sinon → INSERT. Renvoie le Prospect avec son
  champ `id` interne.
- `save()` : no-op (Supabase écrit en direct, pas de buffering local).
  Le flag `_dirty` est gardé pour compat avec le code qui le toggle.
- `all()` : SELECT *, transforme en list[Prospect]. Cache 5 secondes
  pour éviter de spammer l'API si plusieurs vues lisent à la suite.

History et pending_drafts ne sont PAS dans la table prospects ; ils ont
leurs tables propres (email_history, prospect_drafts) qu'on hydrate en
parallèle au moment du load.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from ..prospect.core.prospect import Prospect
from .client import SupabaseClient, get_client, SupabaseNotConfigured
from .repos import (
    history_event_to_row,
    prospect_to_row,
    row_to_draft_dict,
    row_to_history_event,
    row_to_prospect,
)

logger = logging.getLogger(__name__)


class RemoteCRM:
    """CRM dont la persistance est Supabase. API compatible CRM local."""

    CACHE_TTL_SEC = 5

    def __init__(self, client: SupabaseClient | None = None,
                 path: Path | None = None):
        self._client = client or get_client()
        # `path` ignoré : présent pour signature-compat avec le CRM local.
        self._cache: list[Prospect] = []
        self._id_by_match: dict[str, str] = {}   # match_key → prospect.id
        self._row_id_by_prospect: dict[int, str] = {}  # id(Prospect) → row.id
        self._cache_at: float = 0.0
        self._dirty = False  # compat
        # Charge tout de suite la 1re fois pour avoir un état utilisable
        try:
            self._load()
        except SupabaseNotConfigured:
            raise
        except Exception as exc:
            logger.warning("RemoteCRM: load initial échoué : %s", exc)

    # ------------------------------------------------------------------
    # Chargement
    # ------------------------------------------------------------------
    def _load(self, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self._cache_at) < self.CACHE_TTL_SEC and self._cache:
            return
        sb = self._client.raw
        # ⚠️ Supabase/PostgREST plafonne un SELECT à 1000 lignes par défaut.
        # Sans pagination, on ne chargeait que les 1000 premiers prospects —
        # l'Auto-Pilote et les vues ignoraient le reste de la base (au 15/06
        # /2026 : 1000 vus sur 2864). On pagine donc par lots de 1000.
        rows = []
        _PAGE = 1000
        _page = 0
        while True:
            res = (sb.table("prospects").select("*")
                   .order("id")
                   .range(_page * _PAGE, _page * _PAGE + _PAGE - 1)
                   .execute())
            batch = res.data or []
            rows.extend(batch)
            if len(batch) < _PAGE:
                break
            _page += 1
            if _page > 200:   # garde-fou dur (200k fiches max)
                break
        self._cache = []
        self._id_by_match.clear()
        self._row_id_by_prospect.clear()

        # Pré-charge tous les drafts pending pour les remettre dans
        # Prospect.pending_drafts (compat ancien code)
        drafts_res = (sb.table("prospect_drafts").select("*")
                      .eq("status", "pending").execute())
        drafts_rows = drafts_res.data or []
        drafts_by_prospect: dict[str, list[dict[str, Any]]] = {}
        for dr in drafts_rows:
            pid = dr.get("prospect_id")
            if pid:
                drafts_by_prospect.setdefault(pid, []).append(
                    row_to_draft_dict(dr)
                )

        # Pré-charge l'history (limité aux 200 derniers événements globaux
        # pour éviter d'exploser la mémoire). L'API publique le filtre
        # ensuite par prospect_id si besoin.
        hist_res = (sb.table("email_history").select("*")
                    .order("ts", desc=True).limit(2000).execute())
        hist_rows = hist_res.data or []
        history_by_prospect: dict[str, list[dict[str, Any]]] = {}
        for h in hist_rows:
            pid = h.get("prospect_id")
            if pid:
                history_by_prospect.setdefault(pid, []).append(
                    row_to_history_event(h)
                )
        # Trie chaque history par ts ascendant (cohérent avec le local)
        for k in history_by_prospect:
            history_by_prospect[k].sort(key=lambda e: e.get("ts", ""))

        for row in rows:
            p = row_to_prospect(row)
            row_id = row.get("id")
            if row_id:
                self._row_id_by_prospect[id(p)] = row_id
                p.pending_drafts = drafts_by_prospect.get(row_id, [])
                p.history = history_by_prospect.get(row_id, [])
            self._cache.append(p)
            for k in p.match_keys:
                self._id_by_match.setdefault(k, row_id or "")

        self._cache_at = now

    # ------------------------------------------------------------------
    # API publique (mimique l'ancien CRM)
    # ------------------------------------------------------------------
    def all(self) -> list[Prospect]:
        self._load()
        return list(self._cache)

    def find(self, prospect: Prospect) -> Prospect | None:
        self._load()
        for k in prospect.match_keys:
            row_id = self._id_by_match.get(k)
            if row_id:
                # cherche le prospect en cache par row_id
                for p in self._cache:
                    if self._row_id_by_prospect.get(id(p)) == row_id:
                        return p
        return None

    def __len__(self) -> int:
        self._load()
        return len(self._cache)

    def __iter__(self):
        return iter(self.all())

    def _ws_id(self) -> Optional[str]:
        """Recupere workspace_id pour injection sur les inserts (migration 20)."""
        try:
            return self._client._current_workspace_id()
        except Exception:
            return None

    def upsert(self, prospect: Prospect) -> tuple[Prospect, bool]:
        """Insère ou fusionne. Renvoie (prospect_final, was_new_bool).

        Signature alignée avec le CRM local pour compat.
        """
        existing = self.find(prospect)
        sb = self._client.raw
        ws_id = self._ws_id()
        if existing is not None:
            existing.merge(prospect)
            row = prospect_to_row(existing)
            row["updated_by"] = self._client.user_id
            row_id = self._row_id_by_prospect.get(id(existing))
            if not row_id:
                # Sécurité : recherche par 1re match_key
                for k in existing.match_keys:
                    row_id = self._id_by_match.get(k)
                    if row_id:
                        break
            if row_id:
                sb.table("prospects").update(row).eq("id", row_id).execute()
            else:
                # Cas dégradé : insère
                row["created_by"] = self._client.user_id
                if ws_id:
                    row["workspace_id"] = ws_id
                res = sb.table("prospects").insert(row).execute()
                if res.data:
                    new_id = res.data[0].get("id")
                    if new_id:
                        self._row_id_by_prospect[id(existing)] = new_id
                        for k in existing.match_keys:
                            self._id_by_match.setdefault(k, new_id)
            self._dirty = True
            self._cache_at = 0.0
            return existing, False
        # Nouveau prospect
        row = prospect_to_row(prospect)
        row["created_by"] = self._client.user_id
        row["updated_by"] = self._client.user_id
        if ws_id:
            row["workspace_id"] = ws_id
        res = sb.table("prospects").insert(row).execute()
        if res.data:
            new_id = res.data[0].get("id")
            if new_id:
                self._row_id_by_prospect[id(prospect)] = new_id
                for k in prospect.match_keys:
                    self._id_by_match.setdefault(k, new_id)
            self._cache.append(prospect)
        self._dirty = True
        self._cache_at = 0.0
        return prospect, True

    def upsert_many(self, prospects: Iterable[Prospect]) -> dict:
        """Bulk upsert. Renvoie {created, merged, total} comme le CRM local."""
        created = 0
        merged = 0
        for p in prospects:
            _, is_new = self.upsert(p)
            if is_new:
                created += 1
            else:
                merged += 1
        return {
            "created": created,
            "merged": merged,
            "total": len(self._cache),
        }

    def save(self) -> None:
        """No-op : Supabase écrit immédiatement à chaque upsert.

        Présent pour compat API.
        """
        self._dirty = False

    # ------------------------------------------------------------------
    # Helpers spécifiques Supabase (utiles pour le code qui veut bypasser
    # le mode compat et causer directement à la base).
    # ------------------------------------------------------------------
    def get_row_id(self, prospect: Prospect) -> Optional[str]:
        """Renvoie le UUID de la row pour ce prospect (None si pas en base)."""
        rid = self._row_id_by_prospect.get(id(prospect))
        if rid:
            return rid
        for k in prospect.match_keys:
            rid = self._id_by_match.get(k)
            if rid:
                return rid
        return None

    def add_history_event(self, prospect: Prospect,
                           event: dict[str, Any]) -> None:
        rid = self.get_row_id(prospect)
        if not rid:
            logger.warning("add_history_event sans prospect_id : %s",
                           event.get("kind"))
            return
        row = history_event_to_row(rid, event,
                                    created_by=self._client.user_id)
        # Depuis la migration 20 (multi-tenant), email_history.workspace_id
        # est NOT NULL : sans lui, l'insert échouait silencieusement (warning)
        # et l'historique ne montait jamais en base.
        ws_id = self._ws_id()
        if ws_id:
            row["workspace_id"] = ws_id
        try:
            self._client.raw.table("email_history").insert(row).execute()
            prospect.history.append(event)
        except Exception as exc:
            logger.warning("add_history_event a échoué : %s", exc)

    def refresh(self) -> None:
        """Force un reload depuis Supabase."""
        self._load(force=True)
