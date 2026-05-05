"""Source Dailymotion — recherche d'utilisateurs (créateurs) via l'API publique.

API publique gratuite, sans clé : https://developers.dailymotion.com/
- Endpoint : https://api.dailymotion.com/users
- Paramètres : search, fields, limit (max 100), page

Fields utiles :
- id, screenname, username, description, avatar_240_url
- url, followers_total, videos_total
- language

Coût : gratuit en lecture publique. Throttle naturel par notre HTTP helper
(0.3s entre requêtes sur api.dailymotion.com).
"""

from __future__ import annotations

import logging

from ._http import SourceHttpError, get_json

log = logging.getLogger(__name__)


SEARCH_URL = "https://api.dailymotion.com/users"

FIELDS = ",".join([
    "id",
    "screenname",
    "username",
    "description",
    "url",
    "avatar_240_url",
    "followers_total",
    "videos_total",
    "language",
    "country",
    "created_time",
])


class DailymotionAPI:
    """Client Dailymotion API publique (lecture seule, pas de clé)."""

    def __init__(self) -> None:
        pass

    def available(self) -> bool:
        return True

    def search_users(self, query: str, max_results: int = 50) -> list[dict]:
        """Cherche des utilisateurs Dailymotion par mot-clé."""
        if not query:
            return []
        results: list[dict] = []
        page = 1
        # Cap dur à 200 résultats (2 pages × 100) — Dailymotion ne donne pas
        # toujours toute la liste pour les queries vagues.
        while len(results) < max_results and page <= 2:
            params = {
                "search": query,
                "fields": FIELDS,
                "limit": min(100, max_results - len(results)),
                "page": page,
                "sort": "relevance",
            }
            try:
                data = get_json(SEARCH_URL, params=params, max_retries=2)
            except SourceHttpError as e:
                raise RuntimeError(f"Dailymotion a échoué : {e}") from e
            if not isinstance(data, dict):
                break
            items = data.get("list", []) or []
            if not items:
                break
            for u in items:
                username = u.get("username") or ""
                screenname = u.get("screenname") or ""
                results.append({
                    "platform":    "dailymotion",
                    "id":          u.get("id") or username,
                    "name":        screenname or username,
                    "handle":      username,
                    "subscribers": u.get("followers_total") or 0,
                    "subs_hidden": False,
                    "description": (u.get("description") or "")[:4000],
                    "language":    (u.get("language") or "")[:2].lower(),
                    "country":     (u.get("country") or "").upper(),
                    "thumbnail":   u.get("avatar_240_url") or "",
                    "url":         u.get("url") or
                                   (f"https://www.dailymotion.com/{username}"
                                    if username else ""),
                    "video_count": u.get("videos_total") or 0,
                    "created_at":  u.get("created_time") or "",
                })
            if not data.get("has_more"):
                break
            page += 1
        return results[:max_results]
