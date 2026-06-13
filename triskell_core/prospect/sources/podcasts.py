"""Source Apple Podcasts — recherche de podcasts via iTunes Search API.

API publique gratuite, sans clé : https://performance-partners.apple.com/search-api
- Endpoint : https://itunes.apple.com/search
- Paramètres : term, media=podcast, country, limit (max 200), entity, lang.

Très utile pour les niches B2B (les hôtes de podcasts sont par définition
des créateurs avec une audience captive ET un email pro souvent listé sur
le site du show).

L'iTunes Search API retourne pour chaque podcast :
- collectionName (titre du show), artistName (host), feedUrl (RSS du podcast)
- artworkUrl (couverture), genres, country, primaryGenreName
- trackCount, releaseDate

Pour récupérer un email/site : on suit le `feedUrl` (RSS) où le tag
`<itunes:email>` ou `<link>` est presque toujours rempli. On le fait
optionnellement (cher en bande passante), via `enrich_with_feed=True`.
"""

from __future__ import annotations

import logging
import re
from xml.etree import ElementTree as ET

from ._http import SourceHttpError, get_json, get_session

log = logging.getLogger(__name__)


SEARCH_URL = "https://itunes.apple.com/search"

# RSS namespaces utilisés par iTunes Connect / Apple Podcasts
_NS = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "googleplay": "http://www.google.com/schemas/play-podcasts/1.0",
}


class ApplePodcastsAPI:
    """Client iTunes Search API pour podcasts."""

    def __init__(self, country: str = "FR", lang: str = "fr_fr") -> None:
        self.country = country
        self.lang = lang

    def available(self) -> bool:
        return True

    def search_podcasts(
        self,
        query: str,
        max_results: int = 50,
        *,
        enrich_with_feed: bool = True,
        feed_enrich_limit: int = 40,
    ) -> list[dict]:
        """Cherche des podcasts par mot-clé.

        Args:
            enrich_with_feed: si True, suit les RSS feed pour récupérer email
                              et site web officiel (limité à `feed_enrich_limit`
                              pour ne pas exploser la bande passante).
        """
        if not query:
            return []
        params = {
            "term": query,
            "media": "podcast",
            "entity": "podcast",
            "country": self.country,
            "lang": self.lang,
            "limit": min(200, max_results * 2),  # on cherche large, on filtre après
        }
        try:
            data = get_json(SEARCH_URL, params=params, max_retries=2)
        except SourceHttpError as e:
            raise RuntimeError(f"Apple Podcasts a échoué : {e}") from e
        if not isinstance(data, dict):
            return []

        items = data.get("results", []) or []
        results: list[dict] = []
        for item in items:
            collection = item.get("collectionName", "") or item.get("trackName", "")
            artist = item.get("artistName", "") or ""
            feed = item.get("feedUrl", "") or ""
            if not collection:
                continue
            description = (
                (item.get("description") or "")
                + (f"\n\nGenres : {', '.join(item.get('genres', []))}"
                   if item.get("genres") else "")
            )
            results.append({
                "platform":     "apple_podcasts",
                "id":           str(item.get("collectionId") or item.get("trackId") or feed),
                "name":         collection,
                "handle":       artist,
                "subscribers":  None,  # Apple ne publie pas les écoutes
                "subs_hidden":  True,
                "description":  description[:4000],
                "language":     (item.get("country", "") or "").lower()[:2],
                "country":      item.get("country", "") or "",
                "thumbnail":    item.get("artworkUrl600") or item.get("artworkUrl100", ""),
                "url":          item.get("collectionViewUrl") or item.get("trackViewUrl") or "",
                "feed_url":     feed,
                "track_count":  item.get("trackCount") or 0,
                "primary_genre": item.get("primaryGenreName", ""),
                "released_at":  item.get("releaseDate", ""),
            })
            if len(results) >= max_results:
                break

        # Enrichit (email + site web) via le RSS feed officiel — le gain
        # commercial est énorme, c'est souvent là qu'est le vrai contact pro.
        if enrich_with_feed:
            for p in results[:feed_enrich_limit]:
                feed = p.get("feed_url", "")
                if not feed:
                    continue
                meta = _fetch_feed_meta(feed)
                if not meta:
                    continue
                if meta.get("email"):
                    p["description"] = (
                        f"Email officiel : {meta['email']}\n"
                        + (p.get("description") or "")
                    )[:4000]
                if meta.get("link"):
                    # Concatène à la description pour que extract_contacts
                    # trouve l'URL plus tard. On l'expose aussi en clair :
                    p["website"] = meta["link"]
                    p["description"] = (
                        f"Site officiel : {meta['link']}\n"
                        + (p.get("description") or "")
                    )[:4000]
                if meta.get("language"):
                    p["language"] = meta["language"][:2].lower()
        return results


# ---------------------------------------------------------------------------
# Lecture du flux RSS pour extraire email + site
# ---------------------------------------------------------------------------
_EMAIL_RE = re.compile(
    r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"
)


def _fetch_feed_meta(feed_url: str) -> dict | None:
    """Récupère <itunes:email>, <link>, <language> d'un flux RSS podcast."""
    if not feed_url:
        return None
    sess = get_session()
    try:
        r = sess.get(feed_url, timeout=10, stream=True)
        r.raise_for_status()
        # Limite à 200 KB (les <channel> meta sont en début de flux)
        chunks = []
        total = 0
        for chunk in r.iter_content(chunk_size=8192, decode_unicode=False):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total > 200_000:
                break
        raw = b"".join(chunks)
        encoding = r.encoding or r.apparent_encoding or "utf-8"
        text = raw.decode(encoding, errors="ignore")
    except Exception as e:
        log.debug("feed fetch %s : %s", feed_url, e)
        return None

    # On parse uniquement le début (jusqu'au 1er <item>) pour gagner du temps
    head = text.split("<item", 1)[0] + "</channel></rss>"

    meta: dict = {}
    # 1) Parsing XML propre
    try:
        root = ET.fromstring(head)
        channel = root.find("channel") or root
        # Email itunes:owner > email
        owner = channel.find("itunes:owner", _NS)
        if owner is not None:
            email_el = owner.find("itunes:email", _NS)
            if email_el is not None and email_el.text:
                meta["email"] = email_el.text.strip().lower()
        if "email" not in meta:
            ie = channel.find("itunes:email", _NS)
            if ie is not None and ie.text:
                meta["email"] = ie.text.strip().lower()
        # Link
        link_el = channel.find("link")
        if link_el is not None and link_el.text:
            meta["link"] = link_el.text.strip()
        # Language
        lang_el = channel.find("language")
        if lang_el is not None and lang_el.text:
            meta["language"] = lang_el.text.strip()
    except ET.ParseError:
        pass

    # 2) Fallback regex pour les flux mal formés
    if "email" not in meta:
        m = _EMAIL_RE.search(head)
        if m:
            meta["email"] = m.group(0).lower()
    if "link" not in meta:
        m = re.search(r"<link>\s*(https?://[^<\s]+)\s*</link>", head, re.IGNORECASE)
        if m:
            meta["link"] = m.group(1).strip()
    if "language" not in meta:
        m = re.search(r"<language>\s*([a-z\-]+)\s*</language>", head, re.IGNORECASE)
        if m:
            meta["language"] = m.group(1).strip()

    return meta or None
