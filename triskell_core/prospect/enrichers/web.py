"""
Web Enricher — visite le site externe d'un prospect et extrait les coordonnées.

Stratégie :
1. Fetch homepage (HEAD pour vérifier, puis GET).
2. Suit jusqu'à 2 pages internes prioritaires (mentions légales, contact, à propos).
3. Extrait emails (standard + obfusqués), téléphones FR/intl, adresse postale.
4. Détecte la présence de mentions légales (signal RGPD-friendly = entreprise sérieuse).
5. Cache HTML 7 jours dans ~/.triskell-prospect/enrich_cache/ pour ne pas re-fetcher.

Politesse :
- User-Agent honnête (mention Triskell Prospect + URL contact à venir)
- Délai 1 req/sec/domaine
- Respect des robots.txt sur les chemins explorés
- Timeout 10s, taille max 1.5 MB par fetch
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import urllib.robotparser
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

try:
    from bs4 import BeautifulSoup  # type: ignore
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

from ..core.crm import ENRICH_CACHE_DIR, ensure_dirs

log = logging.getLogger(__name__)


USER_AGENT = "TriskellProspect/0.1 (+https://triskell.studio/bot)"
TIMEOUT_S = 10
MAX_BYTES = 1_500_000  # 1.5 MB
CACHE_TTL = timedelta(days=7)
MIN_INTERVAL_PER_DOMAIN = 1.0  # s

# Pages prioritaires à explorer si trouvées
PRIORITY_PATHS = [
    # FR
    "/mentions-legales", "/mentions-legales/", "/legal", "/legal/",
    "/contact", "/contact/", "/contactez-nous", "/nous-contacter",
    "/about", "/about/", "/a-propos", "/a-propos/", "/qui-sommes-nous",
    # EN
    "/imprint", "/legal-notice",
]
PRIORITY_KEYWORDS_IN_LINK = [
    "mentions légales", "mentions legales", "mentions",
    "contact", "contactez",
    "à propos", "a propos", "about",
    "imprint", "legal",
]


# ---------------------------------------------------------------------------
# Patterns d'extraction
# ---------------------------------------------------------------------------
_EMAIL_STANDARD = re.compile(
    r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"
)
_EMAIL_OBFUSCATED = re.compile(
    r"\b([a-zA-Z0-9._%+\-]+)\s*[\(\[]?\s*(?:at|chez|arobase)\s*[\)\]]?\s*"
    r"([a-zA-Z0-9.\-]+)\s*[\(\[]?\s*(?:dot|point)\s*[\)\]]?\s*"
    r"([a-zA-Z]{2,})\b",
    re.IGNORECASE,
)
_PHONE_FR = re.compile(
    r"(?:(?<!\d)(?:\+33|0033)[\s.\-]?[1-9](?:[\s.\-]?\d{2}){4}|"
    r"(?<!\d)0[1-9](?:[\s.\-]?\d{2}){4}(?!\d))"
)
_PHONE_INTL = re.compile(
    r"(?<!\d)\+\d{1,3}[\s.\-]?\d{1,4}(?:[\s.\-]?\d{2,4}){2,4}(?!\d)"
)
# Adresse FR : 12 rue Untel, 75000 Ville  /  12 avenue Untel, 35000 Rennes
_ADDRESS_FR = re.compile(
    r"\d{1,4}(?:bis|ter)?\s*[,\s]?\s*(?:rue|avenue|av\.?|boulevard|bd\.?|"
    r"impasse|allée|allee|place|chemin|route|rte\.?|cours|quai|esplanade|sentier)\s+"
    r"[A-Za-zÀ-ÿ0-9'\-\s.]{3,80}?[,\s]+\d{5}\s+[A-Za-zÀ-ÿ\-\s']{2,40}",
    re.IGNORECASE,
)
_LEGAL_MENTIONS_HINT = re.compile(
    r"\b(?:siret|siren|tva\s*intra|n°\s*tva|num[eé]ro\s*tva|"
    r"directeur\s+de\s+publication|publication\s+directeur|"
    r"mentions?\s+l[eé]gales?|legal\s+notice|imprint)\b",
    re.IGNORECASE,
)
# Numéros SIREN/SIRET trouvés dans le HTML (utile pour cross-référencer
# avec le SIREN Sirene d'origine et confirmer qu'on est bien sur le bon site).
_SIREN_RE = re.compile(r"\b(?:SIREN|R\.?C\.?S\.?)\s*[:\-]?\s*(\d{3}[\s.]?\d{3}[\s.]?\d{3})\b", re.IGNORECASE)
_SIRET_RE = re.compile(r"\b(?:SIRET)\s*[:\-]?\s*(\d{3}[\s.]?\d{3}[\s.]?\d{3}[\s.]?\d{5})\b", re.IGNORECASE)
_POSTAL_FR_RE = re.compile(r"\b(\d{5})\b")


# ---------------------------------------------------------------------------
# HTTP throttler par domaine
# ---------------------------------------------------------------------------
class DomainThrottler:
    def __init__(self, min_interval: float = MIN_INTERVAL_PER_DOMAIN):
        self._last: dict[str, float] = {}
        self._min = min_interval

    def wait(self, domain: str) -> None:
        now = time.time()
        last = self._last.get(domain, 0)
        delta = now - last
        if delta < self._min:
            time.sleep(self._min - delta)
        self._last[domain] = time.time()


# ---------------------------------------------------------------------------
# robots.txt cache
# ---------------------------------------------------------------------------
class RobotsCache:
    def __init__(self):
        self._cache: dict[str, urllib.robotparser.RobotFileParser] = {}

    def can_fetch(self, url: str, user_agent: str = USER_AGENT) -> bool:
        try:
            parsed = urlparse(url)
            base = f"{parsed.scheme}://{parsed.netloc}"
            rp = self._cache.get(base)
            if rp is None:
                rp = urllib.robotparser.RobotFileParser()
                rp.set_url(f"{base}/robots.txt")
                try:
                    rp.read()
                except Exception:
                    return True  # robots indisponible → on autorise
                self._cache[base] = rp
            return rp.can_fetch(user_agent, url)
        except Exception:
            return True


# ---------------------------------------------------------------------------
# Cache HTML 7j
# ---------------------------------------------------------------------------
def _cache_path(url: str) -> Path:
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    return ENRICH_CACHE_DIR / f"{h}.html"


def _read_cache(url: str) -> str | None:
    p = _cache_path(url)
    if not p.exists():
        return None
    age = datetime.now() - datetime.fromtimestamp(p.stat().st_mtime)
    if age > CACHE_TTL:
        return None
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None


def _write_cache(url: str, html: str) -> None:
    ensure_dirs()
    try:
        _cache_path(url).write_text(html, encoding="utf-8", errors="ignore")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------
def _fetch(url: str, throttler: DomainThrottler, robots: RobotsCache,
           session: requests.Session) -> str | None:
    if not robots.can_fetch(url):
        log.debug("robots.txt interdit : %s", url)
        return None

    cached = _read_cache(url)
    if cached is not None:
        return cached

    domain = urlparse(url).netloc
    throttler.wait(domain)
    try:
        r = session.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "fr,en;q=0.9"},
            timeout=TIMEOUT_S,
            stream=True,
            allow_redirects=True,
        )
        r.raise_for_status()
        ct = r.headers.get("Content-Type", "").lower()
        if "html" not in ct and "text" not in ct:
            return None
        chunks = []
        total = 0
        for chunk in r.iter_content(chunk_size=8192, decode_unicode=False):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_BYTES:
                break
        raw = b"".join(chunks)
        # Décodage en respectant l'encoding annoncé
        encoding = r.encoding or r.apparent_encoding or "utf-8"
        html = raw.decode(encoding, errors="ignore")
        _write_cache(url, html)
        return html
    except Exception as e:
        log.debug("fetch %s a échoué : %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _text_from_html(html: str) -> str:
    if HAS_BS4:
        soup = BeautifulSoup(html, "lxml" if _has_lxml() else "html.parser")
        # Vire les <script> et <style> qui polluent
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return soup.get_text(" ", strip=True)
    # Fallback regex bas niveau
    no_script = re.sub(
        r"<script[^>]*>.*?</script>|<style[^>]*>.*?</style>",
        " ",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    no_tags = re.sub(r"<[^>]+>", " ", no_script)
    return re.sub(r"\s+", " ", no_tags).strip()


def _has_lxml() -> bool:
    try:
        import lxml  # noqa: F401
        return True
    except ImportError:
        return False


def _internal_priority_links(html: str, base_url: str) -> list[str]:
    """Trouve les liens vers /mentions-legales, /contact, /a-propos sur le même domaine."""
    if not HAS_BS4:
        return []
    soup = BeautifulSoup(html, "lxml" if _has_lxml() else "html.parser")
    base_host = urlparse(base_url).netloc
    found = []
    seen_paths = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = (a.get_text(" ", strip=True) or "").lower()
        if not href or href.startswith(("javascript:", "mailto:", "tel:")):
            continue
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        if parsed.netloc and parsed.netloc != base_host:
            continue  # liens externes ignorés
        path = parsed.path.lower().rstrip("/") or "/"
        if path in seen_paths:
            continue
        match = (
            any(path == p.rstrip("/") or path.startswith(p.rstrip("/") + "/")
                for p in PRIORITY_PATHS)
            or any(kw in text for kw in PRIORITY_KEYWORDS_IN_LINK)
        )
        if match:
            found.append(full)
            seen_paths.add(path)
        if len(found) >= 3:
            break
    return found


def _extract_contacts_from_text(text: str) -> dict:
    from ..core.prospect import norm_phone

    emails = set()
    for m in _EMAIL_STANDARD.findall(text):
        emails.add(m.lower())
    for local, domain, tld in _EMAIL_OBFUSCATED.findall(text):
        emails.add(f"{local}@{domain}.{tld}".lower())

    # Dédoublonnage par forme normalisée (+33 X = 0X) tout en gardant la 1re
    # forme rencontrée pour l'affichage humain.
    phones_canonical: dict[str, str] = {}
    for m in _PHONE_FR.findall(text):
        raw = re.sub(r"[\s.\-]", "", m)
        canon = norm_phone(raw) or raw
        phones_canonical.setdefault(canon, raw)
    for m in _PHONE_INTL.findall(text):
        raw = re.sub(r"[\s.\-]", "", m)
        if len(re.sub(r"\D", "", raw)) < 8:
            continue
        canon = norm_phone(raw) or raw
        phones_canonical.setdefault(canon, raw)
    phones = set(phones_canonical.values())

    addr_match = _ADDRESS_FR.search(text)
    address = addr_match.group(0).strip() if addr_match else ""

    has_legal = bool(_LEGAL_MENTIONS_HINT.search(text))

    # Identifiants légaux trouvés sur le site (SIREN/SIRET) — source de vérité
    # pour confirmer qu'on est bien sur le bon site lors d'un cross-ref Sirene.
    sirens = {re.sub(r"[\s.]", "", s) for s in _SIREN_RE.findall(text)}
    sirets = {re.sub(r"[\s.]", "", s) for s in _SIRET_RE.findall(text)}
    # Codes postaux trouvés (pour cross-ref si pas de SIREN explicite)
    postal_codes = {m for m in _POSTAL_FR_RE.findall(text)
                    if 1000 <= int(m) <= 99999}

    return {
        "emails": sorted(emails),
        "phones": sorted(phones),
        "address": address,
        "has_legal_mentions": has_legal,
        "sirens": sorted(sirens),
        "sirets": sorted(sirets),
        "postal_codes": sorted(postal_codes),
    }


def _extract_emails_from_mailto(html: str) -> list[str]:
    """Extra : `<a href="mailto:...">` que le texte brut perdrait."""
    if not HAS_BS4:
        return re.findall(r'mailto:([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})', html, flags=re.IGNORECASE)
    soup = BeautifulSoup(html, "lxml" if _has_lxml() else "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().startswith("mailto:"):
            email = href[7:].split("?", 1)[0].strip().lower()
            if "@" in email:
                out.append(email)
    return out


# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------
class WebEnricher:
    """Enrichit un prospect en visitant son site web."""

    def __init__(self):
        self._throttler = DomainThrottler()
        self._robots = RobotsCache()
        self._session = requests.Session()

    def enrich_url(self, url: str, max_pages: int = 3) -> dict:
        """Visite `url` + jusqu'à (max_pages-1) pages prio. Renvoie agrégat."""
        if not url:
            return _empty_result()

        # Normalise scheme
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        agg_emails: set[str] = set()
        agg_phones: set[str] = set()
        agg_sirens: set[str] = set()
        agg_sirets: set[str] = set()
        agg_postal: set[str] = set()
        agg_address = ""
        has_legal = False
        pages_visited = []

        # 1) Homepage
        html = _fetch(url, self._throttler, self._robots, self._session)
        if not html:
            return _empty_result()
        pages_visited.append(url)
        text = _text_from_html(html)
        ext = _extract_contacts_from_text(text)
        agg_emails.update(ext["emails"])
        agg_emails.update(_extract_emails_from_mailto(html))
        agg_phones.update(ext["phones"])
        agg_address = agg_address or ext["address"]
        has_legal = has_legal or ext["has_legal_mentions"]
        agg_sirens.update(ext["sirens"])
        agg_sirets.update(ext["sirets"])
        agg_postal.update(ext["postal_codes"])

        # 2) Pages prioritaires
        priority_links = _internal_priority_links(html, url)
        for link in priority_links[: max_pages - 1]:
            sub_html = _fetch(link, self._throttler, self._robots, self._session)
            if not sub_html:
                continue
            pages_visited.append(link)
            sub_text = _text_from_html(sub_html)
            sub_ext = _extract_contacts_from_text(sub_text)
            agg_emails.update(sub_ext["emails"])
            agg_emails.update(_extract_emails_from_mailto(sub_html))
            agg_phones.update(sub_ext["phones"])
            if not agg_address and sub_ext["address"]:
                agg_address = sub_ext["address"]
            has_legal = has_legal or sub_ext["has_legal_mentions"]
            agg_sirens.update(sub_ext["sirens"])
            agg_sirets.update(sub_ext["sirets"])
            agg_postal.update(sub_ext["postal_codes"])

        from ..core.prospect import norm_phone
        # Dédup canonique cross-pages : on garde 1 forme par numéro normalisé
        canonical_phones: dict[str, str] = {}
        for raw in agg_phones:
            canon = norm_phone(raw) or raw
            canonical_phones.setdefault(canon, raw)

        # Priorise les emails du domaine du site visité (vrais emails de
        # l'entreprise) avant les domaines tiers (sous-traitants, mentions).
        site_domain = urlparse(url).netloc.lower().lstrip("www.")
        ranked_emails = sorted(
            _filter_emails(agg_emails),
            key=lambda e: (0 if e.endswith("@" + site_domain) else 1, e),
        )
        return {
            "emails": ranked_emails[:5],
            "phones": sorted(canonical_phones.values())[:3],
            "address": agg_address,
            "has_legal_mentions": has_legal,
            "pages_visited": pages_visited,
            "sirens": sorted(agg_sirens),
            "sirets": sorted(agg_sirets),
            "postal_codes": sorted(agg_postal),
        }


def _filter_emails(emails) -> list[str]:
    """Vire les faux positifs courants : sentry, wixpress, exemples…"""
    blacklist = (
        "sentry.io", "sentry-next.wixpress.com", "wixpress.com",
        "example.com", "example.fr", "domain.com", "email.com",
        "yourcompany.com", "votre-email.com",
    )
    return [
        e for e in emails
        if not any(e.endswith("@" + b) or ("@" in e and e.split("@", 1)[1] == b)
                   for b in blacklist)
    ]


def _empty_result() -> dict:
    return {
        "emails": [],
        "phones": [],
        "address": "",
        "has_legal_mentions": False,
        "pages_visited": [],
        "sirens": [],
        "sirets": [],
        "postal_codes": [],
    }
