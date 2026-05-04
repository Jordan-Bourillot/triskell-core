"""Accès à la bibliothèque de méga-prompts livrée avec Triskell Core.

Source : data/mega_prompts.json (copié depuis AlphaBeast).
L'utilisateur peut l'override en plaçant un fichier mega_prompts.json
dans son dossier de config (~/.triskell-prospect/mega_prompts.json).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _resolve_packaged_library() -> Path:
    """Trouve mega_prompts.json en mode dev OU en mode PyInstaller frozen.

    En PyInstaller --onedir, les datas sont dans `sys._MEIPASS/triskell_core/data/`.
    En mode dev, c'est `<package>/data/mega_prompts.json`.
    """
    rel = Path("triskell_core") / "data" / "mega_prompts.json"
    # Mode frozen
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / rel
        if candidate.exists():
            return candidate
    # Mode dev
    return Path(__file__).parent.parent / "data" / "mega_prompts.json"


PACKAGED_LIBRARY = _resolve_packaged_library()


def load_packaged_library() -> list[dict[str, Any]]:
    """Charge la bibliothèque livrée dans le package."""
    if not PACKAGED_LIBRARY.exists():
        return []
    try:
        data = json.loads(PACKAGED_LIBRARY.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [
        item for item in data
        if isinstance(item, dict) and "id" in item and "name" in item and "content" in item
    ]


def load_library(override_path: Path | None = None) -> list[dict[str, Any]]:
    """Charge la bibliothèque, en privilégiant un override utilisateur si fourni."""
    if override_path and override_path.exists():
        try:
            data = json.loads(override_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [
                    item for item in data
                    if isinstance(item, dict) and "id" in item and "name" in item and "content" in item
                ]
        except Exception:
            pass
    return load_packaged_library()


def find_by_id(prompt_id: str, library: list[dict] | None = None) -> dict | None:
    lib = library if library is not None else load_packaged_library()
    for item in lib:
        if item.get("id") == prompt_id:
            return item
    return None
