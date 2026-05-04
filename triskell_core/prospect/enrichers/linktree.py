"""
Linktree Follower — quand la bio d'un prospect contient un hub de liens
(linktr.ee, beacons.ai, bio.link, stan.store, komi.io...), suit ce hub
pour récupérer les VRAIS destinations (site perso, email...).

Réutilise le WebEnricher : on télécharge la page hub, on extrait tous les liens
sortants, et on visite (limité) ceux qui ne sont pas des réseaux sociaux purs.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup  # type: ignore

from .web import (
    DomainThrottler,
    RobotsCache,
    USER_AGENT,
    WebEnricher,
    _empty_result,
    _extract_contacts_from_text,
    _extract_emails_from_mailto,
    _fetch,
    _filter_emails,
    _has_lxml,
    _text_from_html,
)

log = logging.getLogger(__name__)


HUB_DOMAINS = {
    "linktr.ee", "beacons.ai", "stan.store", "komi.io",
    "bio.link", "snipfeed.co", "campsite.bio", "lnk.bio",
    "carrd.co", "milkshake.app", "tap.bio", "later.com",
    "msha.ke", "shorby.com", "withkoji.com",
}

# Réseaux sociaux à NE PAS suivre (déjà connus, peu de valeur ajoutée pour
# trouver un email pro).
SOCIAL_DOMAINS = {
    "instagram.com", "twitter.com", "x.com", "facebook.com",
    "tiktok.com", "youtube.com", "youtu.be", "twitch.tv",
    "reddit.com", "linkedin.com", "snapchat.com", "pinterest.com",
    "vimeo.com", "soundcloud.com", "spotify.com", "discord.gg",
    "discord.com", "telegram.me", "t.me", "wa.me", "whatsapp.com",
    "threads.net", "bsky.app",
}


def is_hub(url: str) -> bool:
    if not url:
        return False
    try:
        host = urlparse(
            url if url.startswith(("http://", "https://")) else "https://" + url
        ).netloc.lower()
    except Exception:
        return False
    return any(host == d or host.endswith("." + d) for d in HUB_DOMAINS)


def _is_social(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    return any(host == d or host.endswith("." + d) for d in SOCIAL_DOMAINS)


def _outbound_links(html: str, base_url: str) -> list[str]:
    """Tous les liens sortants non-sociaux du hub."""
    soup = BeautifulSoup(html, "lxml" if _has_lxml() else "html.parser")
    base_host = urlparse(base_url).netloc.lower()
    seen = set()
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        full = urljoin(base_url, href)
        try:
            host = urlparse(full).netloc.lower()
        except Exception:
            continue
        if not host or host == base_host:
            continue
        if _is_social(full):
            continue
        if full in seen:
            continue
        seen.add(full)
        out.append(full)
    return out


class LinktreeFollower:
    """Visite un hub de liens et fusionne les contacts trouvés sur les destinations."""

    def __init__(self, web_enricher: WebEnricher | None = None):
        self._web = web_enricher or WebEnricher()
        # On réutilise le throttler et robots du web enricher
        self._throttler = self._web._throttler  # noqa: SLF001
        self._robots = self._web._robots        # noqa: SLF001
        self._session = self._web._session      # noqa: SLF001

    def enrich_hub(self, hub_url: str, max_outbound: int = 4) -> dict:
        """Suit un linktree-like et agrège emails/phones de la 1re page de chaque destination."""
        if not hub_url:
            return _empty_result()
        if not hub_url.startswith(("http://", "https://")):
            hub_url = "https://" + hub_url

        html = _fetch(hub_url, self._throttler, self._robots, self._session)
        if not html:
            return _empty_result()

        # Extrait déjà les emails/phones du hub lui-même (au cas où)
        text = _text_from_html(html)
        agg = _extract_contacts_from_text(text)
        emails = set(agg["emails"])
        emails.update(_extract_emails_from_mailto(html))
        phones = set(agg["phones"])
        address = agg["address"]
        has_legal = agg["has_legal_mentions"]
        pages_visited = [hub_url]

        # Suit jusqu'à max_outbound liens sortants non-sociaux
        outbound = _outbound_links(html, hub_url)
        primary_url = ""
        for link in outbound[:max_outbound]:
            sub_data = self._web.enrich_url(link, max_pages=2)
            if sub_data["pages_visited"]:
                pages_visited.extend(sub_data["pages_visited"])
                if not primary_url:
                    primary_url = link  # 1re destination réelle = candidate site perso
            emails.update(sub_data["emails"])
            phones.update(sub_data["phones"])
            if not address and sub_data["address"]:
                address = sub_data["address"]
            has_legal = has_legal or sub_data["has_legal_mentions"]

        return {
            "emails": sorted(_filter_emails(emails))[:5],
            "phones": sorted(phones)[:3],
            "address": address,
            "has_legal_mentions": has_legal,
            "pages_visited": pages_visited,
            "primary_url": primary_url,  # site qui semble être la "vraie" destination
        }
