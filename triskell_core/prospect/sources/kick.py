"""Source Kick.com — recherche de channels via l'endpoint public JSON.

Kick n'a pas d'API documentée mais expose un endpoint JSON public utilisé par
leur propre frontend :
- https://kick.com/api/search?searched_word=<query>

Ça renvoie users + channels + categories. On extrait `channels` (créateurs).

Note : Kick n'expose pas publiquement le nombre de followers via cet endpoint.
Pour récupérer plus de détails (followersCount, bio complète), on peut suivre
avec /api/v2/channels/<slug> mais ça multiplie les requêtes par N. On le fait
optionnellement (`enrich_details=True`).

Throttle : 0.6s entre requêtes (config dans _http.py).
"""

from __future__ import annotations

import logging

from ._http import SourceHttpError, get_json

log = logging.getLogger(__name__)


SEARCH_URL = "https://kick.com/api/search"
CHANNEL_URL = "https://kick.com/api/v2/channels/{slug}"


class KickAPI:
    """Client Kick.com public (lecture seule, pas d'auth)."""

    def __init__(self) -> None:
        pass

    def available(self) -> bool:
        return True

    def search_channels(
        self,
        query: str,
        max_results: int = 25,
        *,
        enrich_details: bool = True,
        enrich_limit: int = 15,
    ) -> list[dict]:
        """Cherche des channels Kick par mot-clé."""
        if not query:
            return []
        params = {"searched_word": query}
        try:
            data = get_json(SEARCH_URL, params=params, max_retries=2,
                            accept_404=True)
        except SourceHttpError as e:
            raise RuntimeError(f"Kick a échoué : {e}") from e
        if not isinstance(data, dict):
            return []

        channels = data.get("channels", []) or []
        users = data.get("users", []) or []

        seen_slugs: set[str] = set()
        out: list[dict] = []

        for ch in channels:
            slug = ch.get("slug") or ch.get("user", {}).get("username") or ""
            if not slug or slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            user = ch.get("user", {}) or {}
            out.append({
                "platform":    "kick",
                "id":          str(ch.get("id") or slug),
                "name":        user.get("username") or slug,
                "handle":      slug,
                "subscribers": ch.get("followers_count") or ch.get("followersCount"),
                "subs_hidden": ch.get("followers_count") is None,
                "description": (ch.get("user", {}).get("bio") or "")[:4000],
                "language":    "",
                "thumbnail":   user.get("profile_pic") or "",
                "url":         f"https://kick.com/{slug}",
                "verified":    bool(ch.get("verified")),
            })
            if len(out) >= max_results:
                break

        # Fallback sur "users" si peu de channels (Kick mélange parfois)
        for u in users:
            if len(out) >= max_results:
                break
            slug = u.get("username", "") or ""
            if not slug or slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            out.append({
                "platform":    "kick",
                "id":          str(u.get("id") or slug),
                "name":        u.get("username") or slug,
                "handle":      slug,
                "subscribers": None,
                "subs_hidden": True,
                "description": (u.get("bio") or "")[:4000],
                "language":    "",
                "thumbnail":   u.get("profile_pic") or "",
                "url":         f"https://kick.com/{slug}",
                "verified":    False,
            })

        # Enrichissement (followers + bio complète) via /channels/<slug>
        if enrich_details:
            for p in out[:enrich_limit]:
                slug = p.get("handle") or ""
                if not slug:
                    continue
                try:
                    detail = get_json(
                        CHANNEL_URL.format(slug=slug),
                        max_retries=1,
                        accept_404=True,
                    )
                except SourceHttpError:
                    continue
                if not isinstance(detail, dict):
                    continue
                user = detail.get("user", {}) or {}
                bio = (user.get("bio") or "") if user else ""
                p["subscribers"] = (
                    detail.get("followers_count")
                    or detail.get("followersCount")
                    or p.get("subscribers")
                )
                if bio and not p.get("description"):
                    p["description"] = bio[:4000]
                if user.get("country"):
                    p["country"] = user["country"]
        return out[:max_results]
