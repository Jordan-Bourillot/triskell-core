"""Tests de la normalisation de casse d'un nom d'affichage.

Contexte : ~1 % des fiches prospects ont un nom tout en minuscules
(« à la mesure du bois »), qui apparaissait tel quel dans les mails de
prospection. On remet une casse « enseigne » AU RENDU, uniquement sur les
noms clairement fautifs, sans toucher à la casse voulue (mixte / ALL CAPS)
ni aggraver les noms « poubelle » scrapés.

Exécution :
    python -m pytest tests/test_name_case.py -v
ou (sans pytest) :
    python tests/test_name_case.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Permet de lancer le test sans installer le paquet
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triskell_core.prospect.core.name_case import (
    fr_title_case,
    normalize_display_name,
)


# --- Noms clairement fautifs (tout minuscule) → casse « enseigne » ---------
CASES_FIXED = [
    ("à la mesure du bois", "À la Mesure du Bois"),
    ("plomberie albert nicolas", "Plomberie Albert Nicolas"),
    ("pacific carrelage", "Pacific Carrelage"),
    ("l'instant immobilier", "L'Instant Immobilier"),
    ("chemins d'aencrage", "Chemins d'Aencrage"),
    # apostrophe possessive anglaise : PAS une élision → pas de majuscule après
    ("dilya's cake", "Dilya's Cake"),
    ("le fournil de paul", "Le Fournil de Paul"),
    ("au bon pain", "Au Bon Pain"),
    ("dupont et fils", "Dupont et Fils"),
    ("briblue", "Briblue"),
    ("gms33 gironde moto services", "Gms33 Gironde Moto Services"),
    # accent initial (à → À, é → É)
    ("école du web", "École du Web"),
    ("éco protech", "Éco Protech"),
    # traits d'union traités comme un mini-titre
    ("marie-claire dupont", "Marie-Claire Dupont"),
    ("saint-jean-de-luz", "Saint-Jean-de-Luz"),
    # petit mot / article élidé en tête → capitalisé
    ("les jardins de sophie", "Les Jardins de Sophie"),
    ("l'atelier de maud", "L'Atelier de Maud"),
]

# --- Noms à NE PAS toucher (casse voulue ou poubelle) ----------------------
CASES_UNCHANGED = [
    # casse mixte voulue
    "l'Atelier de Maud",
    "iD Verde",
    "Pacific Carrelage",
    "eBay",
    # ALL CAPS légitimes
    "SARL MOUGIN",
    "ID VERDE",
    "SNCF",
    # poubelle scrapée : trop de mots
    "yan daubisse electricité electricien toulon var 83 depot",
    # poubelle scrapée : trop long
    "artisan peintre en batiment renovation facade et decoration interieure",
    # rien à normaliser
    "",
    "   ",
    "1234",
]


def test_noms_fautifs_normalises():
    for src, expected in CASES_FIXED:
        got = normalize_display_name(src)
        assert got == expected, f"{src!r} -> {got!r} (attendu {expected!r})"


def test_noms_intacts():
    for src in CASES_UNCHANGED:
        got = normalize_display_name(src)
        assert got == src, f"{src!r} devait rester intact, obtenu {got!r}"


def test_none_et_vide():
    assert normalize_display_name(None) == ""
    assert normalize_display_name("") == ""


def test_idempotent():
    # Re-normaliser un nom déjà corrigé ne le change plus (il porte des
    # majuscules → considéré comme « déjà propre »).
    for _src, expected in CASES_FIXED:
        assert normalize_display_name(expected) == expected


def test_alias():
    assert fr_title_case("à la mesure du bois") == "À la Mesure du Bois"


def _run_all():
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            n += 1
    print(f"\n{n} test(s) OK — {len(CASES_FIXED)} corrigés, "
          f"{len(CASES_UNCHANGED)} intacts.")


if __name__ == "__main__":
    _run_all()
