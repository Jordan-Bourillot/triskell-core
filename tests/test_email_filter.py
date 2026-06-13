"""Tests du filtre email — couvre les faux positifs détectés en prod.

Contexte : le 2026-05-22, 2 chaînes YouTube (Adamantium Coach et
Ia-Automatisation) se sont retrouvées en base avec des "emails" comme
"online@www.aaa.com", "only@savagex.com", "only@www.gobble.com" —
en réalité des fragments de texte type "more info on www.xxx.com"
mal parsés par la regex d'extraction.

Exécution :
    python -m pytest tests/test_email_filter.py -v
ou (sans pytest) :
    python tests/test_email_filter.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Permet de lancer le test sans installer le paquet
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triskell_core.prospect.enrichers.email_filter import (
    AMBIGUOUS_LOCAL_PARTS,
    FAKE_DOMAINS,
    SUSPICIOUS_LOCAL_PARTS,
    clean_email,
    guess_email_from_url,
    is_fake_domain,
)


# ---------------------------------------------------------------------------
# Les 3 faux positifs observés en prod le 2026-05-22 — DOIVENT être rejetés
# ---------------------------------------------------------------------------

def test_rejette_online_www_aaa():
    """Cas 1 : fragment 'online www.aaa.com' parsé en email."""
    assert clean_email("online@www.aaa.com") is None


def test_rejette_only_savagex():
    """Cas 2 : 'only' + domaine factice savagex.com."""
    assert clean_email("only@savagex.com") is None


def test_rejette_only_www_gobble():
    """Cas 3 : 'only' + www.gobble.com (placeholder de template)."""
    assert clean_email("only@www.gobble.com") is None


# ---------------------------------------------------------------------------
# Règles individuelles
# ---------------------------------------------------------------------------

def test_rejette_tous_les_local_parts_suspects():
    """Chaque local-part de SUSPICIOUS_LOCAL_PARTS doit être rejeté."""
    for lp in SUSPICIOUS_LOCAL_PARTS:
        email = f"{lp}@vraieboite.fr"
        assert clean_email(email) is None, f"{email} aurait dû être rejeté"


def test_accepte_local_parts_ambigus_sur_domaine_propre():
    """info@vraie-boite.com est légitime — ne doit PAS être rejeté.
    Régression du cas Bruxelles Formation observé sur la base prod
    le 2026-05-22."""
    for lp in AMBIGUOUS_LOCAL_PARTS:
        email = f"{lp}@bruxellesformation.brussels"
        assert clean_email(email) == email, \
            f"{email} est légitime, ne devrait pas être rejeté"


def test_rejette_local_parts_ambigus_combinés_à_un_domaine_louche():
    """info@www.xxx.com et info@example.com restent rejetés (domaine louche)."""
    for lp in AMBIGUOUS_LOCAL_PARTS:
        assert clean_email(f"{lp}@www.aaa.com") is None
        assert clean_email(f"{lp}@example.com") is None


def test_rejette_domaine_avec_prefix_www():
    """Aucun vrai email n'a son MX sur www.qqch.com."""
    assert clean_email("contact@www.mareboite.com") is None
    assert clean_email("hello@www.exemple.fr") is None


def test_rejette_domaines_factices():
    """Les placeholders typiques (example.com, aaa.com, etc.) sont rejetés."""
    for d in ("example.com", "aaa.com", "gobble.com", "savagex.com",
              "domain.com", "yourcompany.com", "votresite.com"):
        email = f"contact@{d}"
        assert clean_email(email) is None, f"{email} aurait dû être rejeté"


def test_is_fake_domain_strip_www():
    """is_fake_domain doit reconnaître les fakes même préfixés www."""
    assert is_fake_domain("aaa.com") is True
    assert is_fake_domain("www.aaa.com") is True
    assert is_fake_domain("www.gobble.com") is True


# ---------------------------------------------------------------------------
# Régression : les vrais emails légitimes passent toujours
# ---------------------------------------------------------------------------

def test_accepte_emails_legitimes():
    """Pas de régression sur les vrais emails pros."""
    assert clean_email("contact@triskell.studio") == "contact@triskell.studio"
    assert clean_email("jordan@mareelle.fr") == "jordan@mareelle.fr"
    assert clean_email("hello@super-boite.com") == "hello@super-boite.com"


def test_normalise_domaine_minuscule():
    """Les majuscules sont normalisées (côté domaine ET local-part)."""
    assert clean_email("Contact@Triskell.Studio") == "contact@triskell.studio"


def test_aplatit_sous_domaine_links():
    """links.isao.io → isao.io (préfixe à plat)."""
    assert clean_email("contact@links.isao.io") == "contact@isao.io"


# ---------------------------------------------------------------------------
# Terminaison invalide (bug réel observé en test grandeur nature le
# 2026-06-10 : un fleuriste de Rennes via le Prospecteur Google ramenait
# "lebazarapetales@gmail.comwebmaster" — le mot "webmaster" soudé au .com)
# ---------------------------------------------------------------------------

def test_rejette_terminaison_soudee_a_du_texte():
    """gmail.comwebmaster : terminaison invalide → rejet."""
    assert clean_email("lebazarapetales@gmail.comwebmaster") is None
    assert clean_email("x@domaine.commercedetail") is None


def test_garde_le_vrai_email_a_cote_du_casse():
    """Le bon email du même commerçant reste accepté."""
    assert (clean_email("lebazarapetales@gmail.com")
            == "lebazarapetales@gmail.com")


def test_accepte_terminaisons_longues_reelles():
    """Les vraies terminaisons de 4+ lettres passent (régression brussels,
    studio, online, bretagne…)."""
    for email in ("info@bruxellesformation.brussels",
                  "contact@triskell.studio",
                  "hello@studio.agency",
                  "contact@plomberie.online",
                  "jardin@boutique.bretagne"):
        assert clean_email(email) == email, f"{email} aurait dû passer"


def test_rejette_marketplace_fleuristes():
    """contact@sessile.fr (marketplace) n'est pas le mail du commerçant."""
    assert clean_email("contact@sessile.fr") is None
    assert clean_email("contact@interflora.fr") is None


# ---------------------------------------------------------------------------
# Hébergeurs & raccourcisseurs (audit prospection du 13/06/2026)
# ---------------------------------------------------------------------------

def test_rejette_hebergeurs_et_registrars():
    """Le mail commercial d'un hébergeur/registrar n'est JAMAIS celui du
    prospect (sales@scalahosting.com aspiré sur une boulangerie, 13/06/2026)."""
    for d in ("scalahosting.com", "godaddy.com", "hostgator.com",
              "ovh.com", "ionos.fr", "hostinger.com", "namecheap.com",
              "lws.fr", "planethoster.net"):
        assert clean_email(f"sales@{d}") is None, f"sales@{d} aurait dû sauter"
        assert clean_email(f"support@{d}") is None


def test_rejette_raccourcisseurs_de_liens():
    """Un lien raccourci pris pour un site → contact@<raccourci> rejeté
    (contact@amzn.to aspiré sur un youtubeur, 13/06/2026)."""
    for d in ("amzn.to", "a.co", "bit.ly", "cutt.ly", "t.co",
              "wa.me", "discord.gg", "lnkd.in"):
        assert clean_email(f"contact@{d}") is None, f"contact@{d} aurait dû sauter"


def test_guess_email_refuse_lien_affilie():
    """guess_email_from_url ne doit JAMAIS inventer un mail sur un raccourci."""
    assert guess_email_from_url("https://amzn.to/4kyQyQz", verify_mx=False) is None
    assert guess_email_from_url("https://bit.ly/abc", verify_mx=False) is None


def test_guess_email_accepte_vrai_site():
    """Sur un vrai domaine, guess_email_from_url renvoie contact@domaine."""
    assert (guess_email_from_url("https://promessedefleurs.com", verify_mx=False)
            == "contact@promessedefleurs.com")


# ---------------------------------------------------------------------------
# Exécution directe (sans pytest)
# ---------------------------------------------------------------------------

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
