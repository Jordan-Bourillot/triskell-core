# -*- coding: utf-8 -*-
"""Tests anti-faux-site du Chasseur PME (bug réel du 2026-06-10).

En test grandeur nature, le Chasseur attribuait à des fleuristes de Rennes
des sites de PRESSE (actu.fr, 20minutes.fr) et le site d'un BAR
(vieuxsinge.com) — uniquement parce que ces pages mentionnent « Rennes ».
Règle posée : un site n'est fiable que si un mot DISTINCTIF du nom du
commerçant y apparaît ; la ville seule ne valide jamais.

Ce test vit dans triskell-core/tests pour tourner dans la CI, mais il
importe le module côté triskell-command (présent en sibling au build).
"""
import sys
import unittest
from pathlib import Path

# triskell-command est le repo voisin (cloné à côté en prod / dev)
CMD = Path(__file__).resolve().parents[2] / "triskell-command"
if CMD.exists():
    sys.path.insert(0, str(CMD))

try:
    from triskell_command.integrations.chasseur import _score_site_relevance
    _HAS_CMD = True
except Exception:
    _HAS_CMD = False


@unittest.skipUnless(_HAS_CMD, "triskell-command non disponible")
class SiteMatchTests(unittest.TestCase):
    def _media(self):
        return ("<html><body>"
                + "Actualités Rennes : la ville de Rennes en France. " * 20
                + "</body></html>")

    def test_media_local_rejete(self):
        """Un journal qui parle de Rennes n'est pas le site du commerçant."""
        score, _ = _score_site_relevance(
            self._media(), "LE JARDIN DE GRAND-MERE", "RENNES")
        self.assertLess(score, 50)

    def test_mauvaise_entreprise_rejetee(self):
        """Le site d'un bar attribué à un fleuriste : aucun mot du nom."""
        html = ("<html><body>Le Vieux Singe, bar à Rennes. "
                "Cocktails et bières.</body></html>")
        score, _ = _score_site_relevance(html, "SARL DES LICES", "RENNES")
        self.assertLess(score, 50)

    def test_vrai_site_garde(self):
        """Le vrai site (nom + ville présents) reste validé."""
        html = ("<html><body>Le Jardin de Grand-Mère, fleuriste à Rennes. "
                "Notre boutique de fleurs.</body></html>")
        score, _ = _score_site_relevance(
            html, "LE JARDIN DE GRAND-MERE", "RENNES")
        self.assertGreaterEqual(score, 50)

    def test_forme_juridique_non_distinctive(self):
        """« SARL »/« France » ne comptent pas : seul « martin » distingue,
        et il est absent de la page média → non fiable."""
        html = ("<html><body>SARL France, actualités de Rennes. "
                "Toute l'info de la ville.</body></html>")
        score, _ = _score_site_relevance(html, "SARL MARTIN FRANCE", "RENNES")
        self.assertLess(score, 50)


if __name__ == "__main__":
    unittest.main()
