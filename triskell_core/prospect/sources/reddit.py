"""Source Reddit — recherche de subreddits et de redditors via API publique.

Origine : extrait du monolithe Le Dénicheur.

Note : Reddit users n'ont pas vraiment de "communauté à monétiser" au sens
classique. Reddit subreddits = communautés (membres), users = redditors actifs.
On cherche les deux mais le user de Triskell décide ce qui l'intéresse.

Coût : gratuit. Pas d'OAuth requis pour l'API publique en lecture.
Rate limit doux ~100 req/min — on throttle à 0.6s entre chaque appel.
"""

from __future__ import annotations

import time

import requests


DEFAULT_USER_AGENT = "TriskellProspect/1.0 (recherche de prospects)"


class RedditAPI:
    """Client Reddit API publique (lecture seule, pas d'OAuth)."""

    BASE = "https://www.reddit.com"

    def __init__(self, user_agent: str = DEFAULT_USER_AGENT):
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self._last_call = 0.0
        self._min_interval = 0.6  # ~100 req/min max

    def available(self) -> bool:
        return True  # API publique, toujours dispo

    def _throttle(self):
        delta = time.time() - self._last_call
        if delta < self._min_interval:
            time.sleep(self._min_interval - delta)
        self._last_call = time.time()

    def _get(self, path: str, params: dict | None = None,
             max_retries: int = 3) -> dict:
        last_err: Exception | None = None
        for attempt in range(max_retries + 1):
            self._throttle()
            try:
                r = requests.get(
                    f"{self.BASE}{path}",
                    headers={"User-Agent": self.user_agent},
                    params=params or {},
                    timeout=15,
                )
                # Retry sur 429 / 5xx
                if r.status_code in (429,) or 500 <= r.status_code < 600:
                    last_err = RuntimeError(f"HTTP {r.status_code}")
                    if attempt < max_retries:
                        retry_after = r.headers.get("Retry-After")
                        sleep_for = (
                            float(retry_after)
                            if retry_after
                            and retry_after.replace(".", "", 1).isdigit()
                            else min(8.0, 0.5 * (2 ** attempt))
                        )
                        time.sleep(sleep_for)
                        continue
                r.raise_for_status()
                return r.json()
            except requests.RequestException as e:
                last_err = e
                if attempt < max_retries:
                    time.sleep(min(8.0, 0.5 * (2 ** attempt)))
                    continue
                break
            except ValueError as e:  # JSON malformé
                last_err = e
                break
        raise RuntimeError(f"Reddit a échoué : {last_err}")

    # ------------------------------------------------------------------
    def search_subreddits(self, query: str, max_results: int = 25) -> list[dict]:
        """Cherche des subreddits par mot-clé."""
        try:
            data = self._get("/subreddits/search.json", {
                "q": query,
                "limit": min(100, max_results),
                "sort": "relevance",
            })
        except Exception:
            return []
        results: list[dict] = []
        for child in data.get("data", {}).get("children", []):
            d = child.get("data", {})
            results.append({
                "platform":    "reddit",
                "id":          d.get("name", ""),  # t5_xxxxx
                "name":        d.get("display_name_prefixed", "")
                              or f"r/{d.get('display_name', '')}",
                "handle":      d.get("display_name", ""),
                "subscribers": d.get("subscribers", 0) or 0,
                "subs_hidden": False,
                "description": (d.get("public_description", "") + "\n"
                                + d.get("description", ""))[:4000],
                "thumbnail":   (d.get("icon_img", "")
                                or d.get("community_icon", "").split("?")[0]),
                "url":         f"https://www.reddit.com/r/{d.get('display_name', '')}/",
                "kind":        "subreddit",
                "over_18":     d.get("over18", False),
            })
        return results

    def search_users(self, query: str, max_results: int = 25) -> list[dict]:
        """Cherche des redditors par mot-clé.

        Depuis 2023, Reddit a fermé l'endpoint public /users/search.json
        (403 sans OAuth, vérifié le 14/06/2026). On dégrade proprement →
        liste vide, sans faire planter la recherche multi-plateforme.
        """
        try:
            data = self._get("/users/search.json", {
                "q": query,
                "limit": min(100, max_results),
            })
        except Exception:
            return []
        results: list[dict] = []
        for child in data.get("data", {}).get("children", []):
            d = child.get("data", {})
            sub = d.get("subreddit") or {}
            results.append({
                "platform":      "reddit",
                "id":            d.get("name", ""),
                "name":          f"u/{d.get('name', '')}",
                "handle":        d.get("name", ""),
                "subscribers":   sub.get("subscribers", 0) or 0,
                "subs_hidden":   False,
                "description":   (sub.get("public_description", "") + "\n"
                                  + sub.get("title", ""))[:4000],
                "thumbnail":     d.get("icon_img", ""),
                "url":           f"https://www.reddit.com/user/{d.get('name', '')}/",
                "kind":          "user",
                "link_karma":    d.get("link_karma", 0),
                "comment_karma": d.get("comment_karma", 0),
            })
        return results

    def get_top_posts(self, subreddit: str, limit: int = 25,
                      timespan: str = "month") -> list[dict]:
        """Top N posts d'un subreddit sur une période donnée (utile pour mesurer engagement)."""
        try:
            data = self._get(f"/r/{subreddit}/top.json",
                             {"t": timespan, "limit": limit})
            return [c.get("data", {}) for c in data.get("data", {}).get("children", [])]
        except Exception:
            return []
