"""Source PubMed — chercheurs biomédicaux.

PubMed indexe ~38 millions de publications biomédicales. Depuis 1996, le champ
`Affiliation` contient l'email du first/corresponding author quasi-systématique.
Taux observé : 60-80% d'emails extractibles sur les papiers post-2015.

Pipeline :
1. **esearch** : recherche de PMIDs par mot-clé (`db=pubmed&term=<query>`).
2. **efetch** : récupère le XML complet des papiers (titre, abstract,
   affiliations avec emails).
3. **parsing** : extraction des emails depuis les `<Affiliation>` ou
   `<AffiliationInfo>` via regex.

API : NCBI E-utilities, gratuite, sans clé jusqu'à 3 req/s. Avec clé API
gratuite (signup NCBI) : 10 req/s.

Légalité : données publiques, ToS NCBI autorisent l'extraction pour usage
de recherche/prospection légitime. RGPD : email rendu public par l'auteur
dans son affiliation publiée.
"""
from __future__ import annotations

import logging
import re
from xml.etree import ElementTree as ET

from ._http import SourceHttpError, get_json, get_text

log = logging.getLogger(__name__)


ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
)


class PubMedAPI:
    """Client NCBI E-utilities — search + parsing affiliations."""

    def __init__(self, api_key: str = "") -> None:
        self.api_key = (api_key or "").strip()

    def available(self) -> bool:
        return True

    def search_papers(
        self,
        query: str,
        max_results: int = 30,
        *,
        sort: str = "pub_date",
    ) -> list[dict]:
        """Cherche des publications PubMed et renvoie un prospect par
        author/email trouvé.

        Args:
            query : mot-clé (ex: "large language models",
                    "machine learning diagnosis")
            max_results : nombre de PAPERS examinés (un paper peut générer
                          plusieurs prospects si plusieurs co-auteurs ont
                          leur email).
            sort : "pub_date" (récent d'abord) ou "relevance"
        """
        if not query:
            return []

        params = {
            "db":      "pubmed",
            "term":    query,
            "retmax":  min(100, max_results),
            "retmode": "json",
            "sort":    sort,
        }
        if self.api_key:
            params["api_key"] = self.api_key

        try:
            esearch = get_json(
                ESEARCH_URL,
                params=params,
                headers={"User-Agent": "Obelisk-Triskell/1.0"},
                max_retries=2,
            )
        except SourceHttpError as e:
            raise RuntimeError(f"PubMed esearch a échoué : {e}") from e
        ids = []
        if isinstance(esearch, dict):
            ids = ((esearch.get("esearchresult") or {}).get("idlist") or [])
        if not ids:
            return []

        # efetch en XML pour avoir affiliations + emails
        fetch_params = {
            "db":      "pubmed",
            "id":      ",".join(ids),
            "retmode": "xml",
            "rettype": "xml",
        }
        if self.api_key:
            fetch_params["api_key"] = self.api_key

        try:
            xml = get_text(
                EFETCH_URL,
                params=fetch_params,
                headers={"User-Agent": "Obelisk-Triskell/1.0"},
                max_retries=2,
                timeout=45,
            )
        except SourceHttpError as e:
            raise RuntimeError(f"PubMed efetch a échoué : {e}") from e

        return list(self._parse_articles(xml))

    def _parse_articles(self, xml: str):
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as e:
            log.warning("PubMed XML parse error: %s", e)
            return

        seen_emails: set[str] = set()
        for article in root.iter("PubmedArticle"):
            medline = article.find("MedlineCitation")
            if medline is None:
                continue
            pmid_el = medline.find("PMID")
            pmid = pmid_el.text if pmid_el is not None else ""

            art = medline.find("Article")
            if art is None:
                continue

            title_el = art.find("ArticleTitle")
            title = self._text_of(title_el) if title_el is not None else ""

            abstract_parts = []
            for ab in art.iter("AbstractText"):
                txt = self._text_of(ab)
                if txt:
                    abstract_parts.append(txt)
            abstract = "\n".join(abstract_parts)

            journal_el = art.find("Journal/Title")
            journal = self._text_of(journal_el) if journal_el is not None else ""

            year_el = art.find("Journal/JournalIssue/PubDate/Year")
            year = self._text_of(year_el) if year_el is not None else ""

            authors = art.find("AuthorList")
            if authors is None:
                continue

            for author in authors.iter("Author"):
                last = self._text_of(author.find("LastName")) or ""
                fore = self._text_of(author.find("ForeName")) or ""
                full_name = f"{fore} {last}".strip()
                if not full_name:
                    continue

                # Trouve l'email dans une AffiliationInfo de cet auteur
                for aff in author.iter("AffiliationInfo"):
                    aff_text = self._text_of(aff.find("Affiliation")) or ""
                    if not aff_text:
                        continue
                    emails = _EMAIL_RE.findall(aff_text)
                    if not emails:
                        continue
                    for email in emails:
                        email_lower = email.lower()
                        if email_lower in seen_emails:
                            continue
                        seen_emails.add(email_lower)

                        affiliation_clean = re.sub(
                            r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\.?$",
                            "", aff_text
                        ).strip(" .,;:")

                        desc = "\n".join(filter(None, [
                            f"Affiliation : {affiliation_clean}" if affiliation_clean else "",
                            f"Article : {title}" if title else "",
                            f"Journal : {journal} ({year})" if journal else "",
                            f"Résumé : {abstract[:600]}" if abstract else "",
                        ]))[:4000]

                        country = self._infer_country(affiliation_clean)

                        yield {
                            "platform":    "pubmed",
                            "id":          f"pubmed:{pmid}:{email_lower}",
                            "name":        full_name,
                            "handle":      "",
                            "subscribers": None,
                            "subs_hidden": True,
                            "description": desc,
                            "thumbnail":   "",
                            "url":         f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                            "emails":      [email_lower],
                            "website":     "",
                            "country":     country,
                            "language":    "fr" if country == "FR" else "en",
                            "urls_in_bio": [],
                        }
                        # 1 email max par auteur (le premier trouvé)
                        break
                    # 1 author traité, on sort de la boucle d'affiliations
                    break

    @staticmethod
    def _text_of(el) -> str:
        if el is None:
            return ""
        # Concatène tout le texte (l'élément peut contenir des balises filles)
        return "".join(el.itertext()).strip()

    @staticmethod
    def _infer_country(aff: str) -> str:
        if not aff:
            return ""
        a = aff.lower()
        if "france" in a or "paris" in a or "lyon" in a or "marseille" in a:
            return "FR"
        if "u.s.a" in a or "usa" in a or "united states" in a:
            return "US"
        if "united kingdom" in a or "u.k." in a:
            return "GB"
        if "deutschland" in a or "germany" in a:
            return "DE"
        return ""


__all__ = ["PubMedAPI"]
