"""
Footprint — pour les prospects qui n'ont pas de site connu (Sirene),
on tente de trouver leur site officiel **sans aucun service externe**.

Stratégie principale (rapide, gratuite, robuste) :
- Devine 5–8 domaines candidats à partir du nom + ville :
  `<slug>.fr`, `<slug>.com`, `<slug-ville>.fr`, `<initiales>.fr` …
- HEAD/GET court sur chacun (timeout 5s) ; on garde ceux qui répondent 200/301/302.
- Si plusieurs répondent : on prend le plus court (heuristique : `nom.fr`
  > `nom-detail.fr`). Le Web Enricher valide ensuite via la présence de
  mentions légales + email du domaine.

Stratégie de secours : DuckDuckGo HTML (souvent bloqué en 2026, conservé en cas).

Volontairement aucune dépendance à SerpAPI/Brave/Google Search payant.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from urllib.parse import parse_qs, unquote, urlparse

import requests

try:
    from bs4 import BeautifulSoup  # type: ignore
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

log = logging.getLogger(__name__)


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)
DDG_URL = "https://html.duckduckgo.com/html/"
MIN_INTERVAL_S = 2.0

DIRECTORY_DOMAINS = {
    "pagesjaunes.fr", "societe.com", "infogreffe.fr", "verif.com",
    "manageo.fr", "pappers.fr", "score3.fr", "infonet.fr",
    "annuaire-entreprises.data.gouv.fr", "data.gouv.fr",
    "kompass.com", "europages.fr", "yelp.fr", "yelp.com",
    "tripadvisor.fr", "tripadvisor.com", "mappy.com",
    "bottin.fr", "118000.fr", "118712.fr",
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com",
    "x.com", "youtube.com", "tiktok.com", "pinterest.fr",
    "wikipedia.org", "wikidata.org",
    "amazon.fr", "leboncoin.fr",
    "datalegal.fr", "bornehero.fr", "lefigaro.fr", "lesechos.fr",
    "bfmtv.com", "francebleu.fr", "ouest-france.fr",
}


class FootprintFinder:
    """Trouve le site officiel d'une entreprise depuis son nom + ville."""

    def __init__(self):
        self._last_call = 0.0
        self._session = requests.Session()

    def find_official_site(
        self,
        company_name: str,
        city: str = "",
        country: str = "FR",
    ) -> str:
        """
        Renvoie l'URL la plus probable du site officiel, ou '' si rien trouvé.
        Tente d'abord la stratégie "guess + HEAD" (rapide, fiable),
        retombe sur DuckDuckGo si aucun candidat ne répond.
        """
        if not company_name:
            return ""

        # 1) Stratégie principale : devine + vérifie
        guessed = self._guess_and_verify(company_name, country)
        if guessed:
            return guessed

        # 2) Fallback : DDG (peut ne rien renvoyer si bloqué)
        query_parts = [f'"{company_name}"']
        if city:
            query_parts.append(f'"{city}"')
        for d in ("pagesjaunes.fr", "societe.com", "pappers.fr"):
            query_parts.append(f"-site:{d}")
        query = " ".join(query_parts)
        candidates = self._duckduckgo_search(query)
        return self._pick_best(candidates, company_name, country)

    # ------------------------------------------------------------------
    def _guess_and_verify(self, company_name: str, country: str) -> str:
        """Génère des domaines candidats et vérifie qu'ils répondent."""
        candidates = self._guess_domains(company_name, country)
        alive: list[str] = []
        for domain in candidates:
            url = f"https://{domain}"
            if self._is_alive(url):
                alive.append(url)
                if len(alive) >= 3:
                    break
        if not alive:
            return ""
        # On préfère le domaine le plus court (= le plus "officiel"
        # sur l'heuristique "marque.fr" > "marque-tagline.fr")
        alive.sort(key=lambda u: (len(urlparse(u).netloc), u))
        return alive[0]

    @staticmethod
    def _guess_domains(company_name: str, country: str) -> list[str]:
        """Liste de domaines plausibles, ordre de priorité décroissant."""
        slug = _slugify(company_name)
        if len(slug) < 3:
            return []
        # Variantes
        words = re.split(r"\s+", company_name.strip().lower())
        first_word = _slugify(words[0]) if words else slug
        # Initiales (utile pour "A.A.D.E.L" → "aadel")
        initials = "".join(
            w[0] for w in words
            if w and w[0].isalpha()
        ).lower()

        bases: list[str] = []
        for b in (slug, first_word, initials):
            if b and len(b) >= 3 and b not in bases:
                bases.append(b)

        out: list[str] = []
        tlds = (".fr", ".com") if country.upper() == "FR" else (".com", ".fr")
        for base in bases:
            for tld in tlds:
                d = f"{base}{tld}"
                if d not in out:
                    out.append(d)
        return out

    def _is_alive(self, url: str) -> bool:
        """HEAD rapide pour savoir si le domaine répond. 5s max, suit redirects."""
        try:
            r = self._session.head(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=5,
                allow_redirects=True,
            )
            if r.status_code in (200, 301, 302, 303, 307, 308):
                # Évite les parkings (Content-Length=0 est un signal mais pas
                # fiable ; on accepte par défaut et on laisse le Web Enricher
                # rejeter ensuite si aucun email/contact n'y est).
                return True
            # Certains sites refusent HEAD ; on essaie un GET court
            if r.status_code in (403, 405, 501):
                return self._is_alive_get(url)
            return False
        except requests.exceptions.SSLError:
            return False
        except Exception:
            return self._is_alive_get(url)

    def _is_alive_get(self, url: str) -> bool:
        try:
            r = self._session.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=5,
                stream=True,
                allow_redirects=True,
            )
            if r.status_code in (200, 301, 302, 303, 307, 308):
                # On lit max 1 KB pour confirmer que c'est de l'HTML vivant
                next(r.iter_content(chunk_size=1024), b"")
                return True
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------
    def _duckduckgo_search(self, query: str) -> list[str]:
        delta = time.time() - self._last_call
        if delta < MIN_INTERVAL_S:
            time.sleep(MIN_INTERVAL_S - delta)
        try:
            r = self._session.post(
                DDG_URL,
                data={"q": query, "kl": "fr-fr"},
                headers={"User-Agent": USER_AGENT},
                timeout=15,
            )
            self._last_call = time.time()
            r.raise_for_status()
            html = r.text
        except Exception as e:
            log.debug("DDG a échoué : %s", e)
            return []

        return self._parse_ddg_results(html)

    def _parse_ddg_results(self, html: str) -> list[str]:
        urls: list[str] = []
        if HAS_BS4:
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.select("a.result__a"):
                href = a.get("href") or ""
                u = self._unwrap_ddg(href)
                if u and u not in urls:
                    urls.append(u)
                if len(urls) >= 10:
                    break
        else:
            # Fallback regex sur la classe result__a
            for m in re.findall(
                r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"',
                html,
            ):
                u = self._unwrap_ddg(m)
                if u and u not in urls:
                    urls.append(u)
                if len(urls) >= 10:
                    break
        return urls

    @staticmethod
    def _unwrap_ddg(href: str) -> str:
        """DDG enveloppe les liens : /l/?uddg=ENCODED_URL → décode."""
        if href.startswith("//duckduckgo.com/l/") or href.startswith("/l/"):
            try:
                qs = parse_qs(urlparse(href).query)
                target = qs.get("uddg", [""])[0]
                return unquote(target)
            except Exception:
                return ""
        return href

    # ------------------------------------------------------------------
    def _pick_best(
        self, urls: list[str], company_name: str, country: str
    ) -> str:
        """
        On accepte un domaine UNIQUEMENT s'il a une ressemblance lexicale
        avec le nom de l'entreprise (slug ou trigramme commun). Cela élimine
        les annuaires obscurs qu'on ne peut pas tous blacklister.
        """
        slug = _slugify(company_name)
        if not slug:
            return ""
        # Trigrammes du nom (au moins 3 lettres consécutives doivent matcher)
        trigrams = {slug[i:i + 3] for i in range(len(slug) - 2)} if len(slug) >= 3 else set()

        scored: list[tuple[int, str]] = []
        for url in urls:
            try:
                host = urlparse(url).netloc.lower().lstrip("www.")
            except Exception:
                continue
            if not host:
                continue
            if any(host == d or host.endswith("." + d) for d in DIRECTORY_DOMAINS):
                continue

            host_slug = _slugify(host.split(".")[0])
            if not host_slug:
                continue

            # Ressemblance OBLIGATOIRE : sous peine de quoi on rejette.
            similar = (
                slug == host_slug
                or slug in host_slug
                or host_slug in slug
                or any(tri in host_slug for tri in trigrams if len(tri) >= 3)
            )
            if not similar:
                continue

            score = 0
            if slug == host_slug:
                score += 5
            elif slug in host_slug or host_slug in slug:
                score += 3
            else:
                score += 1  # match trigramme seulement
            if country.upper() == "FR" and host.endswith(".fr"):
                score += 2
            elif host.endswith((".com", ".net", ".eu")):
                score += 1
            if host.count(".") > 2:
                score -= 1
            scored.append((score, url))

        if not scored:
            return ""
        scored.sort(reverse=True)
        # Seuil minimum : on n'accepte rien sous score 3 (= match faible
        # qui pourrait toujours être un annuaire avec un trigramme commun).
        if scored[0][0] < 3:
            return ""
        return scored[0][1]


def _slugify(text: str) -> str:
    """Normalise un nom pour matching : minuscule, sans accent, alphanum only."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    no_accent = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", no_accent.lower())
