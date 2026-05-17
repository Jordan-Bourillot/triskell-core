"""Source YouTube — recherche de chaînes via Data API v3.

Origine : extrait du monolithe Le Dénicheur.
- Recherche par mot-clé (max 100 résultats par run)
- Récupération détails (snippet + statistiques + brandingSettings)
- Récupération vidéos récentes (pour analyser le momentum)
- Rotation automatique de clés API en cas de quota dépassé

Coût : gratuit. Quota officiel = 10 000 unités/jour par clé.
- search.list : 100 unités
- channels.list : 1 unité par batch de 50 IDs
- videos.list : 1 unité par batch de 50 IDs
Une recherche complète (search + détails + vidéos) ≈ 130 unités → ~75 recherches/jour.
"""

from __future__ import annotations

import json
import re

import requests


# Pattern email pour scraper la page /about
_EMAIL_RE_PAGE = re.compile(
    r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"
)
# Liens externes dans ytInitialData (format JSON inline dans le HTML)
_REDIRECT_RE = re.compile(
    r'https?://(?:www\.)?youtube\.com/redirect\?[^"\'<>\s]*[?&]q=([^"&\'<> ]+)'
)
# Faux positifs : on vire les emails YouTube/Google
_EMAIL_BLACKLIST = (
    "youtube.com", "googlemail.com", "noreply.com", "noreply",
    "no-reply", "example.com", "sentry.io",
)


class YouTubeAPI:
    """Client YouTube Data API v3 avec rotation de clés."""

    BASE = "https://www.googleapis.com/youtube/v3"

    def __init__(self, api_key: str, extra_keys: list | None = None):
        keys: list[str] = []
        if api_key and api_key.strip():
            keys.append(api_key.strip())
        for k in (extra_keys or []):
            if k and k.strip() and k.strip() not in keys:
                keys.append(k.strip())
        self.keys = keys
        self.api_key = keys[0] if keys else ""
        self._failed_keys: set[str] = set()

    def available(self) -> bool:
        return bool(self.keys)

    def _rotate_to_next_key(self) -> bool:
        """Marque la clé courante comme épuisée, passe à la suivante."""
        self._failed_keys.add(self.api_key)
        for k in self.keys:
            if k not in self._failed_keys:
                self.api_key = k
                return True
        return False

    def _get_with_rotation(self, url: str, params: dict, what: str = ""):
        """GET avec rotation auto si quota/forbidden."""
        attempts = 0
        last_err: Exception | None = None
        while attempts < len(self.keys) + 1:
            params["key"] = self.api_key
            try:
                r = requests.get(url, params=params, timeout=15)
                if r.status_code in (403, 429):
                    if self._rotate_to_next_key():
                        attempts += 1
                        continue
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last_err = e
                msg = str(e)
                if (("403" in msg or "429" in msg or "quotaExceeded" in msg)
                        and self._rotate_to_next_key()):
                    attempts += 1
                    continue
                break
        raise RuntimeError(f"YouTube {what} a échoué : {last_err}")

    # ------------------------------------------------------------------
    # Recherche
    # ------------------------------------------------------------------
    def search_channels(self, query: str, max_results: int = 50) -> list[str]:
        """Cherche des chaînes par mot-clé. Renvoie les channel IDs."""
        if not self.available():
            return []
        ids: list[str] = []
        page_token = None
        # YouTube cap à 50 par page, on fait jusqu'à 2 pages
        for _ in range(2):
            params = {
                "part": "snippet",
                "q": query,
                "type": "channel",
                "maxResults": min(50, max_results - len(ids)),
                "key": self.api_key,
            }
            if page_token:
                params["pageToken"] = page_token
            data = self._get_with_rotation(f"{self.BASE}/search", params, "search")
            for item in data.get("items", []):
                cid = (item.get("snippet", {}).get("channelId")
                       or item.get("id", {}).get("channelId"))
                if cid:
                    ids.append(cid)
            page_token = data.get("nextPageToken")
            if not page_token or len(ids) >= max_results:
                break
        return ids[:max_results]

    def get_recent_videos(
        self, channel_id: str, max_results: int = 20
    ) -> list[dict]:
        """N dernières vidéos d'une chaîne avec stats vue/like/commentaire."""
        if not self.available() or not channel_id:
            return []
        search_data = self._get_with_rotation(
            f"{self.BASE}/search",
            {
                "part": "id,snippet",
                "channelId": channel_id,
                "type": "video",
                "order": "date",
                "maxResults": max_results,
            },
            "search videos",
        )
        video_ids = [
            it["id"]["videoId"]
            for it in search_data.get("items", [])
            if it.get("id", {}).get("videoId")
        ]
        if not video_ids:
            return []
        data = self._get_with_rotation(
            f"{self.BASE}/videos",
            {"part": "snippet,statistics", "id": ",".join(video_ids)},
            "videos",
        )
        videos = []
        for it in data.get("items", []):
            sn = it.get("snippet", {})
            st = it.get("statistics", {})
            videos.append({
                "id":        it.get("id"),
                "title":     sn.get("title", ""),
                "published": sn.get("publishedAt", ""),
                "views":     int(st.get("viewCount", 0))
                             if st.get("viewCount", "").isdigit() else 0,
                "likes":     int(st.get("likeCount", 0))
                             if st.get("likeCount", "").isdigit() else 0,
                "comments":  int(st.get("commentCount", 0))
                             if st.get("commentCount", "").isdigit() else 0,
            })
        videos.sort(key=lambda v: v.get("published", ""), reverse=True)
        return videos

    @staticmethod
    def scrape_about_page(channel_id: str = "",
                          handle: str = "") -> dict:
        """Scrape la page /about d'une chaîne YouTube pour récupérer les
        liens externes (Instagram, TikTok, site perso) et l'email contact
        si l'utilisateur l'a rendu public.

        L'API Data v3 ne renvoie PAS ces infos — elles ne sont dispos qu'en
        scrapant le HTML de la page.

        Renvoie {emails: [...], external_links: [...]}.
        """
        if not channel_id and not handle:
            return {"emails": [], "external_links": []}
        # Privilégie l'URL @handle si dispo (plus stable), sinon /channel/id
        if handle:
            handle_clean = handle.lstrip("@")
            url = f"https://www.youtube.com/@{handle_clean}/about"
        else:
            url = f"https://www.youtube.com/channel/{channel_id}/about"
        try:
            r = requests.get(
                url,
                headers={
                    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/120.0 Safari/537.36"),
                    "Accept-Language": "fr,en;q=0.9",
                },
                timeout=15,
            )
            r.raise_for_status()
            html = r.text
        except Exception:
            return {"emails": [], "external_links": []}

        # 1) Liens externes : YouTube enrobe tous les liens externes dans
        #    https://www.youtube.com/redirect?...&q=<URL>
        ext_links_raw = _REDIRECT_RE.findall(html)
        from urllib.parse import unquote
        ext_links: list[str] = []
        seen = set()
        for raw_url in ext_links_raw:
            try:
                decoded = unquote(raw_url)
            except Exception:
                decoded = raw_url
            # Vire les anchors / fragments
            decoded = decoded.split("#", 1)[0].rstrip("/")
            if not decoded or decoded in seen:
                continue
            # Filtre les liens vers YouTube lui-même (uploads, channels, etc.)
            if "youtube.com" in decoded or "youtu.be" in decoded:
                continue
            seen.add(decoded)
            ext_links.append(decoded)

        # 2) Emails : YouTube met parfois l'email contact dans le HTML
        #    (uniquement si l'utilisateur l'a rendu public).
        emails: list[str] = []
        for m in _EMAIL_RE_PAGE.findall(html):
            e = m.lower()
            if any(b in e for b in _EMAIL_BLACKLIST):
                continue
            if e not in emails:
                emails.append(e)

        return {"emails": emails[:5], "external_links": ext_links[:10]}


    def get_channels_details(self, channel_ids: list[str]) -> list[dict]:
        """Récupère snippet + statistiques + brandingSettings pour une liste de chaînes."""
        if not self.available() or not channel_ids:
            return []
        results = []
        # API limite à 50 IDs par appel
        for i in range(0, len(channel_ids), 50):
            chunk = channel_ids[i:i + 50]
            data = self._get_with_rotation(
                f"{self.BASE}/channels",
                {"part": "snippet,statistics,brandingSettings",
                 "id": ",".join(chunk)},
                "channels",
            )
            for item in data.get("items", []):
                sn = item.get("snippet", {})
                st = item.get("statistics", {})
                br = item.get("brandingSettings", {}).get("channel", {})
                desc = br.get("description") or sn.get("description") or ""
                subs = (int(st.get("subscriberCount", 0))
                        if st.get("subscriberCount", "").isdigit() else 0)
                lang = (br.get("defaultLanguage")
                        or sn.get("defaultLanguage", "")
                        or "")[:2].lower()
                if not lang and sn.get("country", "").upper() == "FR":
                    lang = "fr"
                results.append({
                    "platform":     "youtube",
                    "id":           item.get("id"),
                    "name":         sn.get("title", ""),
                    "handle":       sn.get("customUrl", ""),
                    "subscribers":  subs,
                    "subs_hidden":  st.get("hiddenSubscriberCount", False),
                    "description":  desc,
                    "country":      sn.get("country", ""),
                    "language":     lang,
                    "thumbnail":    sn.get("thumbnails", {})
                                       .get("default", {}).get("url", ""),
                    "url":          f"https://www.youtube.com/channel/{item.get('id')}",
                    "video_count":  int(st.get("videoCount", 0))
                                    if st.get("videoCount", "").isdigit() else 0,
                    "view_count":   int(st.get("viewCount", 0))
                                    if st.get("viewCount", "").isdigit() else 0,
                    "published_at": sn.get("publishedAt", ""),
                })
        return results
