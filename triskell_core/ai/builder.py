"""Composeur de prompts ultime — combine prompt utilisateur + N méga-prompts.

Origine : extrait depuis Prompts/ultimate_prompt_app/prompt_builder.py.
"""
from __future__ import annotations

from datetime import datetime
from typing import Sequence


SEPARATOR = "=" * 70
SUB_SEPARATOR = "-" * 70


def build_ultimate_prompt(
    user_prompt: str, mega_prompts: Sequence[dict]
) -> str:
    """Combine le prompt utilisateur avec un ou plusieurs méga-prompts.

    Structure :
      1. En-tête (rôle système, méta)
      2. Chaque méga-prompt comme bloc numéroté
      3. La demande utilisateur, clairement marquée
    """
    if not isinstance(user_prompt, str):
        raise TypeError("user_prompt must be a string")
    user_prompt = user_prompt.strip()
    if not user_prompt:
        raise ValueError("Le prompt utilisateur est vide.")
    if not mega_prompts:
        return user_prompt

    parts: list[str] = []
    parts.append(SEPARATOR)
    parts.append("PROMPT ULTIME — généré par Triskell Core")
    parts.append(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    parts.append(f"Méga prompts actifs ({len(mega_prompts)}):")
    for i, mp in enumerate(mega_prompts, 1):
        parts.append(f"  {i}. [{mp.get('id', '??')}] {mp.get('name', 'Sans nom')}")
    parts.append(SEPARATOR)
    parts.append("")
    parts.append("INSTRUCTIONS COMPORTEMENTALES (à appliquer simultanément)")
    parts.append("")

    for i, mp in enumerate(mega_prompts, 1):
        parts.append(SUB_SEPARATOR)
        parts.append(
            f"--- MEGA PROMPT {i}/{len(mega_prompts)}: {mp.get('name', '?')} ---"
        )
        parts.append(SUB_SEPARATOR)
        parts.append(mp.get("content", "").strip())
        parts.append("")

    parts.append(SEPARATOR)
    parts.append("DEMANDE DE L'UTILISATEUR")
    parts.append(SEPARATOR)
    parts.append("")
    parts.append(user_prompt)
    parts.append("")
    parts.append(SEPARATOR)
    parts.append(
        "FIN DU PROMPT ULTIME. Réponds en appliquant TOUTES les règles ci-dessus."
    )
    parts.append(SEPARATOR)
    return "\n".join(parts)
