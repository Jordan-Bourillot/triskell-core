"""Source Bluesky — recherche d'acteurs (créateurs/utilisateurs) via AT Protocol.

API publique gratuite : https://docs.bsky.app/docs/api/app-bsky-actor-search-actors
- Endpoint : https://public.api.bsky.app/xrpc/app.bsky.actor.searchActors
- Pas d'authentification requise pour la recherche publique.
- Cap par page : 100 (q + limit). On fait jusqu'à 2 pages via cursor.

Bluesky est jeune mais en forte croissance (créateurs early-stage qui n'ont
pas encore monétisé). Profil idéal pour Le Dénicheur.

Champs Bluesky pertinents :
- handle (alice.bsky.social), displayName, description, avatar
- followersCount (parfois absent, demande l'endpoint getProfile)
- did (identifiant pérenne)
"""

from __future__ import annotations

import logging

from ._http import SourceHttpError, get_json

log = logging.getLogger(__name__)


BASE = "https://public.api.bsky.app/xrpc"
SEARCH_ENDPOINT = f"{BASE}/app.bsky.actor.searchActors"
PROFILE_ENDPOINT = f"{BASE}/app.bsky.actor.getProfiles"


class BlueskyAPI:
    """Client Bluesky non authentifié (lecture publique uniquement)."""

    def __init__(self) -> None:
        pass

    def available(self) -> bool:
        return True

    def search_actors(self, query: str, max_results: int = 50) -> list[dict]:
        """Recherche d'acteurs (créateurs) par mot-clé."""
        if not query:
            return []
        results: list[dict] = []
        cursor: str | None = None
        # Bluesky cap à 100 par page, on fait jusqu'à 2 pages
        for _ in range(2):
            params = {
                "q": query,
                "limit": min(100, max(1, max_results - len(results))),
            }
            if cursor:
                params["cursor"] = cursor
            try:
                data = get_json(SEARCH_ENDPOINT, params=params, max_retries=2)
            except SourceHttpError as e:
                raise RuntimeError(f"Bluesky a échoué : {e}") from e
            if not isinstance(data, dict):
                break
            actors = data.get("actors", []) or []
            if not actors:
                break

            # Pour les profils enrichis, on récupère followersCount via getProfiles
            dids = [a.get("did") for a in actors if a.get("did")]
            stats = self._fetch_profiles(dids) if dids else {}

            for a in actors:
                did = a.get("did", "")
                full = stats.get(did, {})
                handle = a.get("handle", "") or ""
                results.append({
                    "platform":     "bluesky",
                    "id":           did or handle,
                    "name":         a.get("displayName") or handle,
                    "handle":       handle,
                    "subscribers":  full.get("followersCount") or a.get("followersCount") or 0,
                    "subs_hidden":  False,
                    "description":  (a.get("description") or full.get("description") or "")[:4000],
                    "language":     "",  # non disponible via search
                    "thumbnail":    a.get("avatar", "") or full.get("avatar", "") or "",
                    "url":          f"https://bsky.app/profile/{handle}" if handle else "",
                    "follows_count": full.get("followsCount") or 0,
                    "posts_count":  full.get("postsCount") or 0,
                    "created_at":   full.get("createdAt", ""),
                    "indexed_at":   a.get("indexedAt", ""),
                })

            cursor = data.get("cursor")
            if not cursor or len(results) >= max_results:
                break
        return results[:max_results]

    def _fetch_profiles(self, dids: list[str]) -> dict[str, dict]:
        """Récupère les infos enrichies (followers, posts) pour une liste de DIDs."""
        if not dids:
            return {}
        out: dict[str, dict] = {}
        # getProfiles cap à 25 actors par appel
        for i in range(0, len(dids), 25):
            chunk = dids[i:i + 25]
            params = [("actors", d) for d in chunk]
            try:
                data = get_json(PROFILE_ENDPOINT, params=params,
                                max_retries=1, accept_404=True)
            except SourceHttpError:
                continue
            if not isinstance(data, dict):
                continue
            for prof in data.get("profiles", []) or []:
                did = prof.get("did", "")
                if did:
                    out[did] = prof
        return out
