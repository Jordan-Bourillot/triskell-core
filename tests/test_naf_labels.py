"""Tests du traducteur de codes NAF → libellé lisible.

Contexte : l'API recherche-entreprises ne renvoie que le code (« 47.76Z »)
et la lettre de section (« G »). Avant correction, les fiches du Chasseur
affichaient « G » comme métier (audit prospection du 13/06/2026).

Exécution :
    python -m pytest tests/test_naf_labels.py -v
ou (sans pytest) :
    python tests/test_naf_labels.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triskell_core.prospect.naf_labels import _canon, naf_label


def test_code_connu_fleuriste():
    """47.76Z doit donner un vrai libellé, pas la lettre G."""
    label = naf_label("47.76Z", "G")
    assert "fleurs" in label.lower()
    assert label != "G"


def test_tolere_la_forme_sans_point():
    """4776Z == 47.76Z (l'API peut renvoyer l'un ou l'autre)."""
    assert naf_label("4776Z", "G") == naf_label("47.76Z", "G")


def test_repli_sur_le_libelle_de_section():
    """Code inconnu mais section connue → libellé de section (jamais la lettre)."""
    label = naf_label("99.99Z", "G")
    assert label != "G"
    assert "ommerce" in label  # « Commerce ; réparation… »


def test_repli_sur_le_code_si_rien_de_connu():
    """Ni code ni section connus → on renvoie le code brut (mieux que vide)."""
    assert naf_label("99.99Z", "") == "99.99Z"


def test_jamais_une_lettre_seule_si_section_connue():
    """Le bug d'origine : ne JAMAIS afficher juste « G », « F », « I »…"""
    for letter in ("A", "F", "G", "I", "Q", "S"):
        assert naf_label("00.00Z", letter) != letter


def test_quelques_metiers_courants():
    """Les cibles fréquentes de prospection sont bien traduites."""
    assert "plombier" in naf_label("43.22A").lower()
    assert "électricien" in naf_label("43.21A").lower()
    assert "boulangerie" in naf_label("10.71C").lower()
    assert "coiffure" in naf_label("96.02A").lower()
    assert "restauration" in naf_label("56.10A").lower()


def test_canon():
    assert _canon("47.76z") == "47.76Z"
    assert _canon(" 4321A ") == "43.21A"
    assert _canon("") == ""


if __name__ == "__main__":
    tests = [v for k, v in globals().items()
             if k.startswith("test_") and callable(v)]
    fails = 0
    for t in tests:
        try:
            t()
            print(f"OK  — {t.__name__}")
        except AssertionError as e:
            print(f"FAIL — {t.__name__} : {e}")
            fails += 1
        except Exception as e:
            print(f"ERR  — {t.__name__} : {e}")
            fails += 1
    print()
    print(f"{len(tests) - fails}/{len(tests)} tests OK")
    sys.exit(0 if fails == 0 else 1)
