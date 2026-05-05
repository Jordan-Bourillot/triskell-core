"""Source GitHub — recherche de profils utilisateurs (devs, créateurs tech).

API publique : https://docs.github.com/en/rest/search
- Endpoint : https://api.github.com/search/users
- Requête type : `q=python+in:bio+location:france`
- Sans token : 60 req/h. Avec token : 5000 req/h.

Le token est optionnel mais fortement recommandé. Stocké côté config :
`config["github_token"]`.

GitHub renvoie souvent l'email public ET le site personnel ET le bio,
ce qui en fait une source riche pour les créateurs/devs/freelances tech.
"""

from __future__ import annotations

import logging

from ._http import SourceHttpError, get_json

log = logging.getLogger(__name__)


SEARCH_URL = "https://api.github.com/search/users"
USER_URL = "https://api.github.com/users/{username}"


class GitHubAPI:
    """Client GitHub Search API (mode lecture, token optionnel)."""

    def __init__(self, token: str = "") -> None:
        self.token = (token or "").strip()

    def available(self) -> bool:
        return True  # marche sans token (rate limit plus bas)

    def _headers(self) -> dict:
        h = {"Accept": "application/vnd.github+json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def search_users(
        self,
        query: str,
        max_results: int = 30,
        *,
        in_bio: bool = True,
        enrich_details: bool = True,
        enrich_limit: int = 30,
    ) -> list[dict]:
        """Cherche des utilisateurs par mot-clé.

        Args:
            in_bio: si True, ajoute `in:bio` au qualifier (cherche le mot
                    dans la bio plutôt que dans le nom seul).
            enrich_details: si True, suit /users/<login> pour récupérer
                            email + blog + company.
        """
        if not query:
            return []
        # Construit la query avec qualifier in:bio (plus pertinent)
        q = f"{query} in:bio" if in_bio and "in:" not in query else query
        # Cap GitHub : 1000 résultats au total, 100 par page
        params = {
            "q": q,
            "per_page": min(100, max_results),
            "sort": "followers",
            "order": "desc",
        }
        try:
            data = get_json(SEARCH_URL, params=params, headers=self._headers(),
                            max_retries=2)
        except SourceHttpError as e:
            # Si rate limit sans token : explique clairement
            if e.status == 403:
                raise RuntimeError(
                    "GitHub a échoué (rate limit). Configure un token GitHub "
                    "dans Réglages pour passer à 5000 req/h."
                ) from e
            raise RuntimeError(f"GitHub a échoué : {e}") from e
        if not isinstance(data, dict):
            return []

        items = data.get("items", []) or []
        results: list[dict] = []
        for u in items[:max_results]:
            login = u.get("login") or ""
            results.append({
                "platform":    "github",
                "id":          str(u.get("id") or login),
                "name":        login,
                "handle":      login,
                "subscribers": None,  # peuplé par enrich
                "subs_hidden": True,
                "description": "",    # peuplé par enrich
                "thumbnail":   u.get("avatar_url") or "",
                "url":         u.get("html_url") or f"https://github.com/{login}",
            })

        if enrich_details:
            for p in results[:enrich_limit]:
                login = p.get("handle", "")
                if not login:
                    continue
                try:
                    detail = get_json(
                        USER_URL.format(username=login),
                        headers=self._headers(),
                        max_retries=1,
                        accept_404=True,
                    )
                except SourceHttpError:
                    continue
                if not isinstance(detail, dict):
                    continue
                bio = detail.get("bio") or ""
                blog = detail.get("blog") or ""
                email = detail.get("email") or ""
                company = detail.get("company") or ""
                location = detail.get("location") or ""
                p["name"] = detail.get("name") or login
                # On colle blog/email dans la description : extract_contacts
                # pourra ensuite récupérer l'email proprement.
                desc_parts = [b for b in [bio, company and f"@ {company}",
                                          location and f"📍 {location}",
                                          blog, email] if b]
                p["description"] = "\n".join(desc_parts)[:4000]
                p["subscribers"] = detail.get("followers") or 0
                p["subs_hidden"] = False
                if blog and not blog.startswith(("http://", "https://")):
                    blog = "https://" + blog
                p["website"] = blog
                p["country"] = ""
                if location and any(loc in location.lower()
                                    for loc in ("france", "paris", "lyon",
                                                "marseille", "toulouse",
                                                "lille", "bordeaux")):
                    p["country"] = "FR"
                    p["language"] = "fr"
                p["public_repos"] = detail.get("public_repos") or 0
                p["created_at"] = detail.get("created_at") or ""
        return results
