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

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
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
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"Twitch OAuth a échoué : {e}")
        self._token = data.get("access_token", "")
        self._token_expires = time.time() + int(data.get("expires_in", 3600))
        return self._token

    def _headers(self) -> dict:
        return {
            "Client-ID":     self.client_id,
            "Authorization": f"Bearer {self._get_token()}",
        }

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
                r = requests.get(
                    f"{self.BASE}/search/channels",
                    headers=self._headers(),
                    params=params, timeout=15,
                )
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                raise RuntimeError(f"Twitch search a échoué : {e}")

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
                r = requests.get(
                    f"{self.BASE}/users",
                    headers=self._headers(),
                    params=params, timeout=15,
                )
                r.raise_for_status()
                data = r.json()
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
