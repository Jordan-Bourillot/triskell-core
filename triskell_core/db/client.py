"""Client Supabase pour Triskell — singleton + auth + helpers.

Conception :
- Un seul `SupabaseClient` actif à la fois (singleton via `get_client()`).
- Config résolue dans cet ordre :
    1. variables d'environnement SUPABASE_URL / SUPABASE_ANON_KEY
    2. fichier ~/.triskell-command/settings.json → "supabase" section
    3. fichier ~/.triskell-prospect/config.json → "supabase" section
- Auth : login/password via Supabase Auth, le token est gardé en mémoire
  (et persisté dans ~/.triskell-command/auth.json) pour les sessions
  suivantes.
- Si aucune config Supabase n'est trouvée, on lève `SupabaseNotConfigured`
  pour que le code appelant puisse retomber sur le mode JSON local
  (transition douce).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Localisation des fichiers de config (cohérent avec les autres modules)
# ---------------------------------------------------------------------------
TRISKELL_COMMAND_DIR = Path.home() / ".triskell-command"
TRISKELL_PROSPECT_DIR = Path.home() / ".triskell-prospect"
LEDENICHEUR_DIR = Path.home() / ".ledenicheur"
AUTH_FILE = TRISKELL_COMMAND_DIR / "auth.json"


class SupabaseAuthError(Exception):
    """Erreur d'authentification Supabase."""


class SupabaseNotConfigured(Exception):
    """L'URL ou la clé Supabase n'a pas été fournie."""


# ---------------------------------------------------------------------------
@dataclass
class SupabaseConfig:
    url: str
    anon_key: str
    # Clé service_role (serveur uniquement). Quand elle est présente, le
    # client fonctionne en "mode service" : accès permanent à la base, sans
    # session utilisateur ni JWT qui expire. C'est le mode nominal pour le
    # serveur HTTP (workers 24/7) — le mode user/anon reste celui du desktop.
    service_role_key: str = ""

    @classmethod
    def _settings_service_key(cls) -> str:
        """Cherche une clé service_role dans les fichiers de settings."""
        for cfg_path in (
            TRISKELL_COMMAND_DIR / "settings.json",
            TRISKELL_PROSPECT_DIR / "config.json",
            LEDENICHEUR_DIR / "config.json",
        ):
            if not cfg_path.exists():
                continue
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            sb = data.get("supabase") or {}
            key = sb.get("service_role_key") or sb.get("service_key") or ""
            if key:
                return key
        return ""

    @classmethod
    def resolve(cls) -> "SupabaseConfig":
        """Résout la config depuis env vars ou settings.json."""
        env_url = os.environ.get("SUPABASE_URL")
        env_key = os.environ.get("SUPABASE_ANON_KEY")
        env_service = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
                       or os.environ.get("SUPABASE_SERVICE_KEY") or "")
        if env_url and (env_key or env_service):
            return cls(url=env_url, anon_key=env_key or "",
                       service_role_key=env_service
                       or cls._settings_service_key())

        for cfg_path in (
            TRISKELL_COMMAND_DIR / "settings.json",
            TRISKELL_PROSPECT_DIR / "config.json",
            LEDENICHEUR_DIR / "config.json",
        ):
            if not cfg_path.exists():
                continue
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            sb = data.get("supabase") or {}
            url = sb.get("url") or ""
            key = sb.get("anon_key") or ""
            service = (sb.get("service_role_key") or sb.get("service_key")
                       or env_service or "")
            if url and (key or service):
                return cls(url=url, anon_key=key, service_role_key=service)

        raise SupabaseNotConfigured(
            "Supabase non configuré. Définis SUPABASE_URL + SUPABASE_ANON_KEY "
            "dans l'environnement, OU ajoute une section 'supabase' avec 'url' "
            "et 'anon_key' dans ~/.triskell-command/settings.json, "
            "~/.triskell-prospect/config.json ou ~/.ledenicheur/config.json."
        )


# ---------------------------------------------------------------------------
class SupabaseClient:
    """Wrapper léger autour du SDK supabase-py.

    Pourquoi un wrapper et pas le SDK direct :
    - Centraliser la persistance du token JWT (auth.json).
    - Donner des helpers métier (`upsert_prospect`, `list_drafts`, etc.).
    - Fallback explicite vers JSON si Supabase est inaccessible.
    """

    def __init__(self, config: SupabaseConfig):
        self.config = config
        self._client = None
        self._user_id: Optional[str] = None
        self._user_display_name: Optional[str] = None
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        # Mode service : la clé service_role sert d'autorisation permanente.
        # Plus aucun JWT utilisateur n'est posé sur le client de données →
        # plus d'expiration possible (fini les pannes "JWT expired" des
        # workers qui tournent 24/7 sur le serveur).
        self._service_mode: bool = bool(config.service_role_key)

    # ------------------------------------------------------------------
    # Bas niveau — accès au SDK supabase-py
    # ------------------------------------------------------------------
    def _ensure_sdk(self):
        if self._client is None:
            try:
                from supabase import create_client  # type: ignore
            except ImportError as exc:
                raise SupabaseNotConfigured(
                    "Module 'supabase' non installé. "
                    "pip install supabase"
                ) from exc
            key = self.config.service_role_key or self.config.anon_key
            self._client = create_client(self.config.url, key)
        return self._client

    @property
    def raw(self):
        """Accès direct au client supabase-py pour les usages avancés."""
        return self._ensure_sdk()

    @property
    def service_mode(self) -> bool:
        """True si le client tourne avec la clé service_role (serveur)."""
        return self._service_mode

    @property
    def is_authenticated(self) -> bool:
        # En mode service, l'accès base est permanent : le client est
        # toujours opérationnel, même sans session utilisateur restaurée.
        return self._user_id is not None or self._service_mode

    @property
    def user_id(self) -> Optional[str]:
        return self._user_id

    @property
    def user_display_name(self) -> Optional[str]:
        return self._user_display_name

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    def sign_in(self, email: str, password: str) -> dict[str, Any]:
        """Login email/password. Persiste les tokens dans auth.json.

        En mode service, le mot de passe est vérifié sur un client jetable
        (clé anon) : le client de données garde son autorisation service_role
        et ne reçoit JAMAIS le JWT utilisateur — seule l'identité (user_id,
        display_name) est conservée pour l'attribution des écritures.
        """
        if self._service_mode:
            if not self.config.anon_key:
                raise SupabaseAuthError(
                    "Login impossible : anon_key absente de la config "
                    "(le mode service n'en a pas besoin pour les données, "
                    "mais la vérification du mot de passe oui)."
                )
            try:
                from supabase import create_client  # type: ignore
                tmp = create_client(self.config.url, self.config.anon_key)
                res = tmp.auth.sign_in_with_password({"email": email,
                                                       "password": password})
            except Exception as exc:
                raise SupabaseAuthError(f"Login refusé : {exc}") from exc
        else:
            sb = self._ensure_sdk()
            try:
                res = sb.auth.sign_in_with_password({"email": email,
                                                      "password": password})
            except Exception as exc:
                raise SupabaseAuthError(f"Login refusé : {exc}") from exc
        session = getattr(res, "session", None)
        user = getattr(res, "user", None)
        if session is None or user is None:
            raise SupabaseAuthError("Session vide après login.")
        self._access_token = session.access_token
        self._refresh_token = session.refresh_token
        self._user_id = user.id
        # Récupère le display_name dans la table users
        try:
            row = (self.table("users").select("display_name")
                   .eq("user_id", user.id).limit(1).execute())
            data = row.data or []
            if data:
                self._user_display_name = data[0].get("display_name", "")
        except Exception:
            pass
        self._save_auth()
        return {
            "user_id": self._user_id,
            "display_name": self._user_display_name,
            "email": user.email,
        }

    def sign_out(self) -> None:
        if not self._service_mode:
            sb = self._ensure_sdk()
            try:
                sb.auth.sign_out()
            except Exception:
                pass
        self._user_id = None
        self._user_display_name = None
        self._access_token = None
        self._refresh_token = None
        try:
            if AUTH_FILE.exists():
                AUTH_FILE.unlink()
        except Exception:
            pass

    def restore_session(self) -> bool:
        """Charge le token persisté et tente une reprise de session.

        Renvoie True si la session a été restaurée avec succès.

        En mode service : on ne pose AUCUN JWT utilisateur sur le client de
        données (sinon l'autorisation service_role serait remplacée par un
        token qui expire). On récupère juste l'identité depuis auth.json,
        pour l'attribution des écritures (created_by / updated_by).
        """
        if self._service_mode:
            try:
                if AUTH_FILE.exists():
                    data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
                    self._user_id = data.get("user_id") or self._user_id
                    self._user_display_name = (data.get("display_name")
                                               or self._user_display_name)
            except Exception as exc:
                logger.debug("Restore identité (mode service) : %s", exc)
            return True
        if not AUTH_FILE.exists():
            return False
        try:
            data = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        except Exception:
            return False
        access = data.get("access_token") or ""
        refresh = data.get("refresh_token") or ""
        if not access or not refresh:
            return False
        sb = self._ensure_sdk()
        try:
            sb.auth.set_session(access, refresh)
            user = sb.auth.get_user()
            uobj = getattr(user, "user", None) or user
            uid = getattr(uobj, "id", None)
            if uid is None:
                return False
            self._user_id = uid
            self._access_token = access
            self._refresh_token = refresh
            # Récupère display_name
            try:
                row = (sb.table("users").select("display_name")
                       .eq("user_id", uid).limit(1).execute())
                data2 = row.data or []
                if data2:
                    self._user_display_name = data2[0].get("display_name", "")
            except Exception:
                pass
            return True
        except Exception as exc:
            logger.debug("Restore session failed : %s", exc)
            return False

    def refresh_session(self) -> bool:
        """Rafraîchit l'access_token via le refresh_token avant expiration.

        À appeler périodiquement (ex. toutes les 30 min) par le serveur HTTP
        pour que l'utilisateur n'ait JAMAIS à se reconnecter manuellement.

        Renvoie True si le refresh a réussi, False sinon.
        Sauvegarde automatiquement les nouveaux tokens dans auth.json.

        En mode service : rien à rafraîchir (l'autorisation service_role
        n'expire pas) → True direct.
        """
        if self._service_mode:
            return True
        if not self._refresh_token:
            return False
        sb = self._ensure_sdk()
        try:
            res = sb.auth.refresh_session(self._refresh_token)
            session = getattr(res, "session", None)
            if session is None:
                return False
            new_access = getattr(session, "access_token", None)
            new_refresh = getattr(session, "refresh_token", None) or self._refresh_token
            if not new_access:
                return False
            self._access_token = new_access
            self._refresh_token = new_refresh
            try:
                sb.auth.set_session(new_access, new_refresh)
            except Exception:
                pass
            self._save_auth()
            return True
        except Exception as exc:
            logger.warning("refresh_session a échoué : %s", exc)
            return False

    def _save_auth(self) -> None:
        try:
            TRISKELL_COMMAND_DIR.mkdir(parents=True, exist_ok=True)
            AUTH_FILE.write_text(
                json.dumps({
                    "access_token": self._access_token or "",
                    "refresh_token": self._refresh_token or "",
                    "user_id": self._user_id or "",
                    "display_name": self._user_display_name or "",
                }, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.debug("Save auth failed : %s", exc)

    # ------------------------------------------------------------------
    # Helpers métier — couche fine, le code consommateur fait le reste
    # ------------------------------------------------------------------
    def table(self, name: str):
        """Raccourci `client.raw.table(name)`."""
        return self.raw.table(name)

    def list_users(self) -> list[dict[str, Any]]:
        try:
            res = self.table("users").select("*").execute()
            return list(res.data or [])
        except Exception as exc:
            logger.warning("list_users a échoué : %s", exc)
            return []

    def get_shared_setting(self, key: str, default: Any = None) -> Any:
        try:
            res = (self.table("shared_settings").select("value")
                   .eq("key", key).limit(1).execute())
            data = res.data or []
            if data:
                return data[0].get("value", default)
        except Exception as exc:
            logger.debug("get_shared_setting %s : %s", key, exc)
        return default

    def _current_workspace_id(self) -> str | None:
        """Renvoie l'uuid du workspace courant (multi-tenant depuis migration 20).

        Utilise la fonction SQL public.current_workspace_id() qui lit auth.uid().
        Cache le résultat pour éviter une RPC par appel.

        En mode service, auth.uid() est NULL → la RPC renvoie None. On
        retombe alors sur une résolution directe : le workspace du user
        connu (auth.json), sinon le tout premier workspace créé (cas
        mono-workspace "triskell-studio").
        """
        cached = getattr(self, "_ws_id_cache", None)
        if cached:
            return cached
        try:
            res = self.raw.rpc("current_workspace_id").execute()
            ws_id = res.data if isinstance(res.data, str) else None
            if ws_id:
                self._ws_id_cache = ws_id
                return ws_id
        except Exception as exc:
            logger.debug("current_workspace_id RPC: %s", exc)
        if not self._service_mode:
            return None
        # Fallback mode service — par user d'abord, sinon 1er workspace.
        try:
            if self._user_id:
                res = (self.table("workspace_members")
                       .select("workspace_id, joined_at")
                       .eq("user_id", self._user_id)
                       .order("joined_at").limit(1).execute())
                rows = res.data or []
                if rows and rows[0].get("workspace_id"):
                    self._ws_id_cache = rows[0]["workspace_id"]
                    return self._ws_id_cache
            res = (self.table("workspaces").select("id, created_at")
                   .order("created_at").limit(1).execute())
            rows = res.data or []
            if rows and rows[0].get("id"):
                self._ws_id_cache = rows[0]["id"]
                return self._ws_id_cache
        except Exception as exc:
            logger.debug("workspace fallback (mode service) : %s", exc)
        return None

    def set_shared_setting(self, key: str, value: Any) -> None:
        try:
            row = {
                "key": key,
                "value": value,
                "updated_by": self._user_id,
            }
            # Depuis la migration 20 (multi-tenant), shared_settings a une
            # colonne workspace_id NOT NULL et PK (workspace_id, key). Sans
            # ce champ, l'upsert plante silencieusement (warning seulement).
            ws_id = self._current_workspace_id()
            if ws_id:
                row["workspace_id"] = ws_id
            self.table("shared_settings").upsert(row).execute()
        except Exception as exc:
            logger.warning("set_shared_setting %s a échoué : %s", key, exc)


# ---------------------------------------------------------------------------
# Singleton process-wide (1 seul client à la fois)
# ---------------------------------------------------------------------------
_INSTANCE: SupabaseClient | None = None


def get_client(*, auto_restore: bool = True) -> SupabaseClient:
    """Renvoie le client global (le crée et restaure la session si nécessaire).

    Lève SupabaseNotConfigured si on n'a pas l'URL + clé.
    """
    global _INSTANCE
    if _INSTANCE is None:
        cfg = SupabaseConfig.resolve()
        _INSTANCE = SupabaseClient(cfg)
        if auto_restore:
            try:
                _INSTANCE.restore_session()
            except Exception:
                pass
    return _INSTANCE


def set_client(client: SupabaseClient) -> None:
    """Surcharge le client global (utile pour les tests)."""
    global _INSTANCE
    _INSTANCE = client


def reset_client() -> None:
    """Détruit le client global. Le prochain get_client() le recréera."""
    global _INSTANCE
    _INSTANCE = None
