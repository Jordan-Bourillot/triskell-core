"""Curseur de recherche persisté entre runs.

Sans ce module, chaque exécution de l'auto pilot repart des mêmes pages /
mêmes coordonnées géographiques. Le système anti-doublons (match_keys
côté CRM) protège contre le re-contact, mais le robot perd du temps à
re-trouver les mêmes prospects à chaque run et n'explore jamais en
profondeur.

Ici on retient « où on s'est arrêté » pour chaque source, par signature
de critères. Si l'utilisateur change ses critères, on repart à zéro.

Backend :
    1. Supabase `shared_settings` clé `search_cursors` (synchro Jordan/Thomas)
    2. Fallback local : `~/.triskell-prospect/search_cursors.json`

Structure stockée :
    {
        "<source_key>": {
            # Sirène
            "sirene_naf_index": 2,      # index courant dans la rotation NAF
            "sirene_dept_index": 1,
            "sirene_page": 5,           # prochaine page à attaquer
            "sirene_exhausted": false,  # true = tous les codes NAF × pages épuisés
            # Maps
            "maps_cell": 7,             # cellule courante de la grille
            # Obelisk
            "obelisk_offset": 50,
            "obelisk_seed": 1234,       # seed du shuffle, change chaque run
            # Méta
            "last_run_at": "2026-05-17T08:30:00",
            "criteria_hash": "abc123",  # détecte un changement de critères
        }
    }
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from .core.crm import APP_DIR, ensure_dirs

logger = logging.getLogger(__name__)


SHARED_SETTING_KEY = "search_cursors"
LOCAL_FILE = APP_DIR / "search_cursors.json"


# ---------------------------------------------------------------------------
# Signature des critères — détecte si l'utilisateur a changé ses filtres
# ---------------------------------------------------------------------------
def criteria_hash(criteria: dict[str, Any]) -> str:
    """Hash stable des critères de recherche. Si l'utilisateur les change,
    le curseur se reset automatiquement (nouvelle exploration)."""
    clean = {k: v for k, v in sorted(criteria.items()) if v not in (None, "", [], {})}
    blob = json.dumps(clean, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Backend Supabase (avec fallback local)
# ---------------------------------------------------------------------------
def _get_client():
    try:
        from triskell_core.db import get_client, SupabaseNotConfigured
        try:
            c = get_client()
            if c.is_authenticated:
                return c
        except SupabaseNotConfigured:
            return None
    except Exception:
        return None
    return None


def _load_all() -> dict[str, dict[str, Any]]:
    client = _get_client()
    if client is not None:
        try:
            raw = client.get_shared_setting(SHARED_SETTING_KEY, {}) or {}
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except Exception:
                    raw = {}
            if isinstance(raw, dict):
                return raw
        except Exception as exc:
            logger.debug("search_cursor load supabase: %s", exc)
    if LOCAL_FILE.exists():
        try:
            data = json.loads(LOCAL_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception as exc:
            logger.debug("search_cursor load local: %s", exc)
    return {}


def _save_all(cursors: dict[str, dict[str, Any]]) -> None:
    client = _get_client()
    if client is not None:
        try:
            client.set_shared_setting(SHARED_SETTING_KEY, cursors)
        except Exception as exc:
            logger.warning("search_cursor save supabase: %s", exc)
    ensure_dirs()
    try:
        LOCAL_FILE.write_text(
            json.dumps(cursors, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("search_cursor save local: %s", exc)


# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------
def load(source_key: str, criteria: dict[str, Any]) -> dict[str, Any]:
    """Charge le curseur pour cette source + ces critères.

    Si les critères ont changé depuis le dernier run, on renvoie un
    curseur vide (= repart de zéro avec les nouveaux filtres).
    """
    all_cursors = _load_all()
    state = all_cursors.get(source_key) or {}
    if not isinstance(state, dict):
        return {}
    expected = criteria_hash(criteria)
    if state.get("criteria_hash") != expected:
        return {"criteria_hash": expected}
    return state


def save(source_key: str, criteria: dict[str, Any], state: dict[str, Any]) -> None:
    """Sauve le curseur. Stamp automatique du hash de critères et de la date."""
    all_cursors = _load_all()
    state = dict(state or {})
    state["criteria_hash"] = criteria_hash(criteria)
    state["last_run_at"] = datetime.now().isoformat(timespec="seconds")
    all_cursors[source_key] = state
    _save_all(all_cursors)


def reset(source_key: str) -> None:
    """Supprime le curseur d'une source — la prochaine recherche repart
    à zéro (page 1, premier NAF, première cellule…)."""
    all_cursors = _load_all()
    if source_key in all_cursors:
        del all_cursors[source_key]
        _save_all(all_cursors)


# ---------------------------------------------------------------------------
# Helpers de rotation
# ---------------------------------------------------------------------------
def split_list(value: str | list[str]) -> list[str]:
    """Accepte 'A,B,C' ou ['A','B','C'] et renvoie une liste propre."""
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if not value:
        return []
    return [p.strip() for p in str(value).split(",") if p.strip()]


def spiral_offsets(step_m: int, max_cells: int = 49) -> list[tuple[int, int]]:
    """Génère des offsets (dx, dy) en mètres autour de (0,0) en spirale.

    Utile pour la grille géographique Maps : la cellule 0 = centre,
    1..8 = anneau adjacent, etc. step_m = côté d'une cellule en mètres.

    Renvoie une liste de tuples (dx_metres, dy_metres).
    """
    cells: list[tuple[int, int]] = [(0, 0)]
    ring = 1
    while len(cells) < max_cells:
        for x in range(-ring, ring + 1):
            cells.append((x * step_m, ring * step_m))
            cells.append((x * step_m, -ring * step_m))
        for y in range(-ring + 1, ring):
            cells.append((ring * step_m, y * step_m))
            cells.append((-ring * step_m, y * step_m))
        ring += 1
    return cells[:max_cells]
