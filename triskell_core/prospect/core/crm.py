"""
CRM unifié — stockage local des prospects toutes sources confondues.

- Chargement depuis ~/.triskell-prospect/prospects.json
- upsert() : ajoute OU fusionne par match_keys (dédoublonnage cross-source)
- Index secondaire en mémoire pour O(1) sur le lookup

Aucune dépendance externe.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from .prospect import Prospect


APP_DIR = Path.home() / ".triskell-prospect"
PROSPECTS_FILE = APP_DIR / "prospects.json"
CONFIG_FILE = APP_DIR / "config.json"
ENRICH_CACHE_DIR = APP_DIR / "enrich_cache"


def ensure_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    ENRICH_CACHE_DIR.mkdir(parents=True, exist_ok=True)


class CRM:
    def __init__(self, path: Path = PROSPECTS_FILE):
        self.path = path
        self._prospects: list[Prospect] = []
        self._index: dict[str, int] = {}  # match_key -> idx dans _prospects
        self._dirty = False
        self._load()

    # ---------------------------------------------------------------------
    # Persistence
    # ---------------------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, list):
            return
        for item in data:
            try:
                p = Prospect.from_dict(item)
            except Exception:
                continue
            self._prospects.append(p)
        self._rebuild_index()

    def _rebuild_index(self) -> None:
        self._index.clear()
        for i, p in enumerate(self._prospects):
            for k in p.match_keys:
                # 1re occurrence gagne ; on n'écrase pas
                self._index.setdefault(k, i)

    def save(self) -> None:
        if not self._dirty:
            return
        ensure_dirs()
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                [p.to_dict() for p in self._prospects],
                indent=2,
                ensure_ascii=False,
                default=str,
            ),
            encoding="utf-8",
        )
        tmp.replace(self.path)
        self._dirty = False

    # ---------------------------------------------------------------------
    # Lecture
    # ---------------------------------------------------------------------
    def all(self) -> list[Prospect]:
        return list(self._prospects)

    def find(self, prospect: Prospect) -> Prospect | None:
        """Cherche un prospect existant qui matche `prospect` sur n'importe quelle clé."""
        for k in prospect.match_keys:
            idx = self._index.get(k)
            if idx is not None:
                return self._prospects[idx]
        return None

    def __len__(self) -> int:
        return len(self._prospects)

    # ---------------------------------------------------------------------
    # Écriture
    # ---------------------------------------------------------------------
    def upsert(self, prospect: Prospect) -> tuple[Prospect, bool]:
        """Ajoute ou fusionne. Renvoie (prospect_final, was_new_bool)."""
        existing = self.find(prospect)
        if existing is not None:
            existing.merge(prospect)
            # Met à jour l'index avec d'éventuelles nouvelles clés
            idx = self._prospects.index(existing)
            for k in existing.match_keys:
                self._index.setdefault(k, idx)
            self._dirty = True
            return existing, False
        # Nouveau
        idx = len(self._prospects)
        self._prospects.append(prospect)
        for k in prospect.match_keys:
            self._index.setdefault(k, idx)
        self._dirty = True
        return prospect, True

    def upsert_many(self, prospects: Iterable[Prospect]) -> dict:
        """Bulk upsert. Renvoie {created, merged, total}."""
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
            "total": len(self._prospects),
        }


# ---------------------------------------------------------------------------
# Factory : choisit le backend (Supabase si dispo, sinon JSON local)
# ---------------------------------------------------------------------------
def get_crm(*, force_local: bool = False, force_remote: bool = False):
    """Renvoie un CRM utilisable, peu importe que Supabase soit configuré.

    - Si `force_local=True` : toujours JSON local.
    - Si `force_remote=True` : toujours Supabase (lève si pas configuré).
    - Sinon : tente Supabase en priorité, fallback JSON local si :
        * supabase non configuré (URL/clé absentes)
        * supabase non authentifié (token absent / expiré)
        * SDK supabase-py non installé

    Le code consommateur peut traiter le résultat comme un CRM uniforme
    (les deux exposent : all(), find(), upsert(), upsert_many(), save(),
    __len__).
    """
    if force_local and force_remote:
        raise ValueError("force_local et force_remote sont exclusifs.")

    if not force_local:
        try:
            # Imports lazy : pas de pénalité si Supabase pas installé
            from ...db.client import get_client, SupabaseNotConfigured
            from ...db.remote_crm import RemoteCRM

            try:
                client = get_client()
            except SupabaseNotConfigured:
                if force_remote:
                    raise
                return CRM()

            if not client.is_authenticated and force_remote:
                raise RuntimeError(
                    "Supabase configuré mais pas authentifié. "
                    "Login requis avant get_crm(force_remote=True)."
                )
            if client.is_authenticated:
                return RemoteCRM(client=client)
            if force_remote:
                raise RuntimeError("Login Supabase requis.")
        except ImportError:
            if force_remote:
                raise
            # Pas de SDK → fallback local
            pass
        except Exception:
            if force_remote:
                raise
            # Toute autre erreur réseau → fallback local
            pass

    return CRM()
