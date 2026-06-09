"""Tests du mode service (clé service_role) de SupabaseClient.

Le mode service est le mode nominal du serveur HTTP : accès base permanent,
sans JWT utilisateur qui expire. Ces tests vérifient la mécanique SANS
réseau (pas de vrai Supabase) :
  - résolution de la config (env vars, priorité service_role)
  - is_authenticated / refresh_session / restore_session en mode service
  - le client de données n'est jamais pollué par un set_session utilisateur
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from triskell_core.db.client import (  # noqa: E402
    SupabaseClient,
    SupabaseConfig,
)


class ResolveConfigTests(unittest.TestCase):
    def test_env_service_role_active_le_mode_service(self):
        env = {
            "SUPABASE_URL": "https://x.supabase.co",
            "SUPABASE_ANON_KEY": "anon123",
            "SUPABASE_SERVICE_ROLE_KEY": "service456",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = SupabaseConfig.resolve()
        self.assertEqual(cfg.url, "https://x.supabase.co")
        self.assertEqual(cfg.anon_key, "anon123")
        self.assertEqual(cfg.service_role_key, "service456")

    def test_env_sans_service_role_reste_en_mode_user(self):
        env = {
            "SUPABASE_URL": "https://x.supabase.co",
            "SUPABASE_ANON_KEY": "anon123",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            for var in ("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERVICE_KEY"):
                os.environ.pop(var, None)
            with mock.patch.object(SupabaseConfig, "_settings_service_key",
                                   return_value=""):
                cfg = SupabaseConfig.resolve()
        self.assertEqual(cfg.service_role_key, "")

    def test_service_key_seule_suffit(self):
        env = {
            "SUPABASE_URL": "https://x.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "service456",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("SUPABASE_ANON_KEY", None)
            cfg = SupabaseConfig.resolve()
        self.assertEqual(cfg.anon_key, "")
        self.assertEqual(cfg.service_role_key, "service456")

    def test_service_key_depuis_settings_quand_env_incomplet(self):
        env = {
            "SUPABASE_URL": "https://x.supabase.co",
            "SUPABASE_ANON_KEY": "anon123",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            for var in ("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SERVICE_KEY"):
                os.environ.pop(var, None)
            with mock.patch.object(SupabaseConfig, "_settings_service_key",
                                   return_value="service-from-file"):
                cfg = SupabaseConfig.resolve()
        self.assertEqual(cfg.service_role_key, "service-from-file")


class ServiceModeClientTests(unittest.TestCase):
    def _client(self, **kw) -> SupabaseClient:
        cfg = SupabaseConfig(url="https://x.supabase.co",
                             anon_key=kw.get("anon_key", "anon123"),
                             service_role_key=kw.get("service_role_key",
                                                      "service456"))
        return SupabaseClient(cfg)

    def test_authentifie_sans_session_utilisateur(self):
        c = self._client()
        self.assertTrue(c.service_mode)
        self.assertTrue(c.is_authenticated)
        self.assertIsNone(c.user_id)

    def test_refresh_est_un_no_op_qui_reussit(self):
        c = self._client()
        self.assertTrue(c.refresh_session())

    def test_mode_user_sans_session_pas_authentifie(self):
        c = self._client(service_role_key="")
        self.assertFalse(c.service_mode)
        self.assertFalse(c.is_authenticated)
        self.assertFalse(c.refresh_session())

    def test_sdk_cree_avec_la_cle_service(self):
        c = self._client()
        created = {}

        def fake_create_client(url, key):
            created["url"] = url
            created["key"] = key
            return mock.MagicMock()

        fake_supabase = mock.MagicMock()
        fake_supabase.create_client = fake_create_client
        with mock.patch.dict(sys.modules, {"supabase": fake_supabase}):
            c.raw
        self.assertEqual(created["key"], "service456")

    def test_restore_session_lit_identite_sans_toucher_au_sdk(self):
        c = self._client()
        fake_auth = {"user_id": "uuid-jordan", "display_name": "Jordan",
                     "access_token": "expired", "refresh_token": "expired"}
        with mock.patch("triskell_core.db.client.AUTH_FILE") as af:
            af.exists.return_value = True
            af.read_text.return_value = json.dumps(fake_auth)
            ok = c.restore_session()
        self.assertTrue(ok)
        self.assertEqual(c.user_id, "uuid-jordan")
        self.assertEqual(c.user_display_name, "Jordan")
        # Le SDK n'a jamais été instancié : aucun set_session utilisateur
        # n'a pu remplacer l'autorisation service_role.
        self.assertIsNone(c._client)

    def test_restore_session_sans_auth_json_reste_operationnel(self):
        c = self._client()
        with mock.patch("triskell_core.db.client.AUTH_FILE") as af:
            af.exists.return_value = False
            ok = c.restore_session()
        self.assertTrue(ok)
        self.assertTrue(c.is_authenticated)

    def test_sign_out_ne_detruit_pas_le_client_de_donnees(self):
        c = self._client()
        c._user_id = "uuid-jordan"
        sdk = mock.MagicMock()
        c._client = sdk
        with mock.patch("triskell_core.db.client.AUTH_FILE") as af:
            af.exists.return_value = False
            c.sign_out()
        sdk.auth.sign_out.assert_not_called()
        self.assertIsNone(c.user_id)
        self.assertTrue(c.is_authenticated)  # mode service toujours actif


class WorkspaceFallbackTests(unittest.TestCase):
    def _client_with_tables(self, rpc_data=None, members_rows=None,
                            workspaces_rows=None) -> SupabaseClient:
        cfg = SupabaseConfig(url="https://x.supabase.co", anon_key="a",
                             service_role_key="s")
        c = SupabaseClient(cfg)
        sdk = mock.MagicMock()
        sdk.rpc.return_value.execute.return_value = mock.MagicMock(
            data=rpc_data)

        def table(name):
            t = mock.MagicMock()
            rows = members_rows if name == "workspace_members" else workspaces_rows
            (t.select.return_value.eq.return_value.order.return_value
              .limit.return_value.execute.return_value) = mock.MagicMock(
                data=rows or [])
            (t.select.return_value.order.return_value.limit.return_value
              .execute.return_value) = mock.MagicMock(data=rows or [])
            return t

        sdk.table.side_effect = table
        c._client = sdk
        return c

    def test_rpc_prioritaire_quand_elle_repond(self):
        c = self._client_with_tables(rpc_data="ws-rpc")
        self.assertEqual(c._current_workspace_id(), "ws-rpc")

    def test_fallback_membres_par_user(self):
        c = self._client_with_tables(
            rpc_data=None,
            members_rows=[{"workspace_id": "ws-jordan"}])
        c._user_id = "uuid-jordan"
        self.assertEqual(c._current_workspace_id(), "ws-jordan")

    def test_fallback_premier_workspace_sans_user(self):
        c = self._client_with_tables(
            rpc_data=None,
            workspaces_rows=[{"id": "ws-default"}])
        self.assertEqual(c._current_workspace_id(), "ws-default")

    def test_cache_apres_premier_appel(self):
        c = self._client_with_tables(rpc_data="ws-rpc")
        c._current_workspace_id()
        c._client.rpc.reset_mock()
        self.assertEqual(c._current_workspace_id(), "ws-rpc")
        c._client.rpc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
