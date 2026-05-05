"""Source Twitch — recherche de channels via API Helix.

Origine : extrait du monolithe Le Dénicheur.
- Recherche par mot-clé (sur title + tags + game)
- Enrichissement via /users (pour récupérer la bio complète)

Coût : gratuit. Authentification : OAuth client credentials.
Limite : pas d'accès public au nombre d'abonnés sans autorisation du créateur
(c'est une limite Twitch, pas de l'app).
"""

from __future__ import annotations

import time

import requests


class TwitchAPI:
    """Client Twitch Helix avec OAuth client credentials."""

    OAUTH = "https://id.twitch.tv/oauth2/token"
    BASE = "https://api.twitch.tv/helix"

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
        self._token_expires: float = 0

    def available(self) -> bool:
        return bool(self.client_id and self.client_secret
                    and self.client_id.strip() and self.client_secret.strip())

    def _get_token(self, force_refresh: bool = False) -> str:
        if (not force_refresh) and self._token \
                and time.time() < self._token_expires - 60:
            return self._token
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                r = requests.post(
                    self.OAUTH,
                    params={
                        "client_id":     self.client_id,
                        "client_secret": self.client_secret,
                        "grant_type":    "client_credentials",
                    },
                    timeout=15,
                )
                if r.status_code == 401:
                    raise RuntimeError(
                        "Twitch OAuth 401 — Client ID ou Secret invalide "
                        "(re-vérifie dans Réglages)."
                    )
                if r.status_code in (429,) or 500 <= r.status_code < 600:
                    last_err = RuntimeError(f"HTTP {r.status_code}")
                    time.sleep(min(8.0, 0.5 * (2 ** attempt)))
                    continue
                r.raise_for_status()
                data = r.json()
            except requests.RequestException as e:
                last_err = e
                if attempt < 2:
                    time.sleep(min(8.0, 0.5 * (2 ** attempt)))
                    continue
                raise RuntimeError(f"Twitch OAuth a échoué : {e}") from e
            else:
                self._token = data.get("access_token", "")
                self._token_expires = (
                    time.time() + int(data.get("expires_in", 3600))
                )
                return self._token
        raise RuntimeError(f"Twitch OAuth a échoué après 3 tentatives : {last_err}")

    def _headers(self) -> dict:
        return {
            "Client-ID":     self.client_id,
            "Authorization": f"Bearer {self._get_token()}",
        }

    def _helix_get(self, path: str, *, params=None,
                   max_retries: int = 3) -> dict:
        """GET sur l'API Helix avec retry exponentiel + refresh token sur 401.

        Retry sur 429 / 5xx / network errors. Sur 401, force un refresh OAuth
        et retente une fois (le token a peut-être expiré entre-temps).
        """
        last_err: Exception | None = None
        token_refreshed = False
        for attempt in range(max_retries + 1):
            try:
                r = requests.get(
                    f"{self.BASE}{path}",
                    headers=self._headers(),
                    params=params or {},
                    timeout=15,
                )
            except requests.RequestException as e:
                last_err = e
                if attempt < max_retries:
                    time.sleep(min(8.0, 0.5 * (2 ** attempt)))
                    continue
                raise RuntimeError(f"Twitch network: {e}") from e
            if r.status_code == 401 and not token_refreshed:
                # Force le refresh OAuth puis retente
                self._get_token(force_refresh=True)
                token_refreshed = True
                continue
            if r.status_code in (429,) or 500 <= r.status_code < 600:
                last_err = RuntimeError(f"HTTP {r.status_code}")
                if attempt < max_retries:
                    retry_after = r.headers.get("Retry-After")
                    if retry_after and retry_after.replace(".", "", 1).isdigit():
                        time.sleep(min(float(retry_after), 8.0))
                    else:
                        time.sleep(min(8.0, 0.5 * (2 ** attempt)))
                    continue
                raise RuntimeError(f"Twitch HTTP {r.status_code}")
            r.raise_for_status()
            try:
                return r.json()
            except ValueError as e:
                raise RuntimeError(f"Twitch JSON malformé : {e}") from e
        raise RuntimeError(f"Twitch a échoué : {last_err}")

    # ------------------------------------------------------------------
    # Recherche
    # ------------------------------------------------------------------
    def search_channels(self, query: str, max_results: int = 50) -> list[dict]:
        """Recherche de channels Twitch par mot-clé."""
        if not self.available():
            return []
        results: list[dict] = []
        cursor = None
        for _ in range(2):  # max ~200 résultats
            params = {
                "query": query,
                "first": min(100, max_results - len(results)),
                "live_only": "false",
            }
            if cursor:
                params["after"] = cursor
            try:
                data = self._helix_get("/search/channels", params=params)
            except Exception as e:
                raise RuntimeError(f"Twitch search a échoué : {e}") from e

            for item in data.get("data", []):
                results.append({
                    "platform":    "twitch",
                    "id":          item.get("id"),
                    "name":        item.get("display_name", ""),
                    "handle":      item.get("broadcaster_login", ""),
                    "subscribers": None,  # non disponible publiquement
                    "subs_hidden": True,
                    "description": (item.get("title", "") + "\n"
                                    + (item.get("tags", "") or "")),
                    "language":    (item.get("broadcaster_language", "") or "")[:2].lower(),
                    "thumbnail":   item.get("thumbnail_url", ""),
                    "url":         f"https://twitch.tv/{item.get('broadcaster_login', '')}",
                    "is_live":     item.get("is_live", False),
                    "game_name":   item.get("game_name", ""),
                })
            cursor = data.get("pagination", {}).get("cursor")
            if not cursor or len(results) >= max_results:
                break
        return results[:max_results]

    def enrich_with_user_info(self, channels: list[dict]) -> list[dict]:
        """Complète la description des channels via /users (bio complète)."""
        if not self.available() or not channels:
            return channels
        by_login = {c["handle"]: c for c in channels if c.get("handle")}
        logins = list(by_login.keys())
        for i in range(0, len(logins), 100):
            chunk = logins[i:i + 100]
            params = [("login", lg) for lg in chunk]
            try:
                data = self._helix_get("/users", params=params)
            except Exception:
                continue
            for u in data.get("data", []):
                login = u.get("login", "")
                if login in by_login:
                    desc = u.get("description", "")
                    if desc:
                        existing = by_login[login].get("description", "")
                        by_login[login]["description"] = (existing + "\n" + desc).strip()
                    by_login[login]["view_count"] = u.get("view_count", 0)
                    by_login[login]["created_at"] = u.get("created_at", "")
        return channels
