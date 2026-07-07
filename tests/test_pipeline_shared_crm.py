"""Tests du branchement du pipeline sur la base PARTAGÉE.

Le bug d'origine : le moteur (Auto-pilote, relances) lisait uniquement le
fichier local — il ne voyait jamais les prospects poussés dans Supabase
par les outils web. Ces tests verrouillent la mécanique de persistance
sans réseau (CRM factices).
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triskell_core.prospect import pipeline as pl  # noqa: E402
from triskell_core.prospect.core.prospect import Prospect  # noqa: E402
from triskell_core.prospect.outreach.smtp_sender import (  # noqa: E402
    prospection_headers,
)


class FakeRemoteCRM:
    """Imite RemoteCRM : nom de classe + API minimale."""

    def __init__(self, row_id="row-123", fail_draft=False):
        self.events = []
        self.upserts = []
        self.row_id = row_id
        self.fail_draft = fail_draft
        self._client = mock.MagicMock()
        self._client.user_id = "uuid-jordan"
        self._client._current_workspace_id.return_value = "ws-1"
        self.inserted_rows = []

        def _insert(row):
            m = mock.MagicMock()

            def _exec():
                if self.fail_draft and ("body_html" in row
                                        or "review_score" in row):
                    raise RuntimeError("colonne inconnue")
                self.inserted_rows.append(row)
                return mock.MagicMock(data=[{"id": "d1"}])

            m.execute = _exec
            return m

        table = mock.MagicMock()
        table.insert.side_effect = _insert
        self._client.raw.table.return_value = table

    def add_history_event(self, prospect, event):
        self.events.append(event)
        prospect.history.append(event)

    def upsert(self, prospect):
        self.upserts.append(prospect)
        return prospect, False

    def get_row_id(self, prospect):
        return self.row_id


FakeRemoteCRM.__name__ = "RemoteCRM"


class FakeLocalCRM:
    def __init__(self):
        self._dirty = False


class RecordEventTests(unittest.TestCase):
    def test_remote_ecrit_en_base(self):
        crm = FakeRemoteCRM()
        p = Prospect(name="Test", emails=["a@b.fr"])
        pl._record_event(crm, p, {"kind": "email_sent", "ts": "t"})
        self.assertEqual(len(crm.events), 1)
        self.assertEqual(p.history[-1]["kind"], "email_sent")

    def test_local_append_memoire(self):
        crm = FakeLocalCRM()
        p = Prospect(name="Test", emails=["a@b.fr"])
        pl._record_event(crm, p, {"kind": "email_sent", "ts": "t"})
        self.assertEqual(p.history[-1]["kind"], "email_sent")


class PersistProspectTests(unittest.TestCase):
    def test_remote_upsert(self):
        crm = FakeRemoteCRM()
        p = Prospect(name="Test", emails=["a@b.fr"])
        pl._persist_prospect(crm, p)
        self.assertEqual(crm.upserts, [p])

    def test_local_marque_dirty(self):
        crm = FakeLocalCRM()
        p = Prospect(name="Test", emails=["a@b.fr"])
        pl._persist_prospect(crm, p)
        self.assertTrue(crm._dirty)


class StoreDraftTests(unittest.TestCase):
    def _payload(self):
        return {"subject": "Objet", "body": "Corps", "body_html": "<p>x</p>",
                "kind": "first_contact", "template_key": "tpl",
                "provider": "anthropic", "model": "m",
                "review_score": 9, "review_verdict": "ok",
                "review_comment": "ras"}

    def test_remote_insere_dans_prospect_drafts(self):
        crm = FakeRemoteCRM()
        p = Prospect(name="Test", emails=["a@b.fr"])
        ok = pl._store_validation_draft(crm, p, self._payload())
        self.assertTrue(ok)
        row = crm.inserted_rows[0]
        self.assertEqual(row["prospect_id"], "row-123")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["workspace_id"], "ws-1")
        self.assertEqual(row["body_html"], "<p>x</p>")
        self.assertEqual(row["review_score"], 9)

    def test_retry_sans_colonnes_bonus_si_migration_absente(self):
        crm = FakeRemoteCRM(fail_draft=True)
        p = Prospect(name="Test", emails=["a@b.fr"])
        ok = pl._store_validation_draft(crm, p, self._payload())
        self.assertTrue(ok)
        row = crm.inserted_rows[0]
        self.assertNotIn("body_html", row)
        self.assertNotIn("review_score", row)
        self.assertEqual(row["subject"], "Objet")

    def test_local_renvoie_false(self):
        crm = FakeLocalCRM()
        p = Prospect(name="Test", emails=["a@b.fr"])
        self.assertFalse(pl._store_validation_draft(crm, p, self._payload()))

    def test_sans_row_id_renvoie_false(self):
        crm = FakeRemoteCRM(row_id=None)
        p = Prospect(name="Test", emails=["a@b.fr"])
        self.assertFalse(pl._store_validation_draft(crm, p, self._payload()))


class GuessedEmailTests(unittest.TestCase):
    def test_generique_est_devine(self):
        p = Prospect(name="X", emails=["contact@boite.fr"])
        self.assertTrue(pl._email_is_guessed(p))

    def test_nominal_est_confirme(self):
        p = Prospect(name="X", emails=["jordan@boite.fr"])
        self.assertFalse(pl._email_is_guessed(p))

    def test_source_guess_est_devine(self):
        p = Prospect(name="X", emails=["jordan@boite.fr"],
                     emails_meta=[{"email": "jordan@boite.fr",
                                   "source": "guess"}])
        self.assertTrue(pl._email_is_guessed(p))

    def test_sans_email_est_devine(self):
        p = Prospect(name="X", emails=[])
        self.assertTrue(pl._email_is_guessed(p))

    def test_tri_confirmees_d_abord(self):
        a = Prospect(name="A", emails=["contact@a.fr"])
        b = Prospect(name="B", emails=["marie@b.fr"])
        c = Prospect(name="C", emails=["info@c.fr"])
        lst = [a, b, c]
        lst.sort(key=pl._email_is_guessed)
        self.assertEqual([p.name for p in lst], ["B", "A", "C"])


class MergeContactTrackingTests(unittest.TestCase):
    """Verrou du bug 06/07/2026 : merge() doit reporter last_contact_at.

    Reproduit la fusion faite par l'upsert du CRM partagé après expiration
    du cache : la fiche FRAÎCHE de la base (sans date de contact) fusionne
    l'objet d'envoi qui vient d'être marqué « contacted » avec sa date.
    Avant le fix, le status remontait mais last_contact_at était perdu.
    """

    def test_reporte_last_contact_at_sur_fiche_fraiche(self):
        fraiche = Prospect(name="X", emails=["a@b.fr"], status="qualified")
        envoi = Prospect(name="X", emails=["a@b.fr"], status="contacted",
                         last_contact_at="2026-07-06T12:30:00")
        fraiche.merge(envoi)
        self.assertEqual(fraiche.status, "contacted")
        self.assertEqual(fraiche.last_contact_at, "2026-07-06T12:30:00")

    def test_dedup_recherche_n_efface_jamais_une_vraie_date(self):
        # Fiche en base déjà contactée ; un prospect fraîchement scrappé
        # (sans date) fusionne dedans → la vraie date doit survivre.
        en_base = Prospect(name="X", emails=["a@b.fr"], status="contacted",
                           last_contact_at="2026-07-01T09:00:00")
        scrappe = Prospect(name="X", emails=["a@b.fr"], status="new")
        en_base.merge(scrappe)
        self.assertEqual(en_base.last_contact_at, "2026-07-01T09:00:00")

    def test_garde_la_date_la_plus_recente(self):
        a = Prospect(name="X", emails=["a@b.fr"],
                     last_contact_at="2026-07-06T12:00:00")
        b = Prospect(name="X", emails=["a@b.fr"],
                     last_contact_at="2026-07-01T08:00:00")
        a.merge(b)
        self.assertEqual(a.last_contact_at, "2026-07-06T12:00:00")

    def test_reporte_next_follow_up_et_canal(self):
        fraiche = Prospect(name="X", emails=["a@b.fr"])
        envoi = Prospect(name="X", emails=["a@b.fr"],
                         next_follow_up_at="2026-07-13T09:00:00",
                         contact_channel="email")
        fraiche.merge(envoi)
        self.assertEqual(fraiche.next_follow_up_at, "2026-07-13T09:00:00")
        self.assertEqual(fraiche.contact_channel, "email")


class ProspectionHeadersTests(unittest.TestCase):
    def test_pose_list_unsubscribe(self):
        h = prospection_headers("pro@triskell-studio.fr")
        self.assertEqual(h["List-Unsubscribe"],
                         "<mailto:pro@triskell-studio.fr?subject=unsubscribe>")

    def test_conserve_les_entetes_existants(self):
        h = prospection_headers("pro@t.fr", extra={"In-Reply-To": "<x>"})
        self.assertEqual(h["In-Reply-To"], "<x>")
        self.assertIn("List-Unsubscribe", h)

    def test_sans_adresse_pas_d_entete(self):
        self.assertEqual(prospection_headers(""), {})

    def test_n_ecrase_pas_un_list_unsubscribe_fourni(self):
        h = prospection_headers("a@b.fr",
                                extra={"List-Unsubscribe": "<mailto:x@y.z>"})
        self.assertEqual(h["List-Unsubscribe"], "<mailto:x@y.z>")


class PipelineCrmFallbackTests(unittest.TestCase):
    def test_fallback_local_si_get_crm_explose(self):
        with mock.patch("triskell_core.prospect.core.crm.get_crm",
                        side_effect=RuntimeError("boom")):
            crm = pl._pipeline_crm()
        self.assertEqual(crm.__class__.__name__, "CRM")

    def test_remote_si_get_crm_le_donne(self):
        fake = FakeRemoteCRM()
        with mock.patch("triskell_core.prospect.core.crm.get_crm",
                        return_value=fake):
            logs = []
            crm = pl._pipeline_crm(logs.append)
        self.assertIs(crm, fake)
        self.assertIn("partagée", logs[0])


if __name__ == "__main__":
    unittest.main()
