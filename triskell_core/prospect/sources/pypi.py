"""Source PyPI — recherche de packages Python.

Idée : chaque package PyPI a un mainteneur qui a déclaré son email pro
dans son `setup.py`/`pyproject.toml`. Pour les niches IA / automatisation /
outils dev, taux email observé : 70-90% sur les packages avec mainteneur
non-anonyme.

Pipeline en 2 étapes :
1. **Recherche** : on s'appuie sur la liste publique des 15 000 packages
   PyPI les plus téléchargés
   (https://hugovk.github.io/top-pypi-packages/top-pypi-packages.min.json,
   ~800 ko, mise à jour mensuellement). On filtre cette liste par le mot-clé.
   Raison : PyPI a verrouillé le scrape HTML de sa page de search fin 2024,
   son XML-RPC est désactivé, et il n'expose pas d'API JSON de search.
2. **Enrichissement** : pour chaque match, fetch direct sur
   `pypi.org/pypi/<pkg>/json` (qui reste ouvert sans clé) — on récupère
   `info.author_email` + `info.maintainer_email` + summary + home_page.

La liste de top packages est cachée en mémoire process (chargée une fois).
"""
from __future__ import annotations

import logging
import re
from urllib.parse import quote_plus

from ._http import SourceHttpError, get_json

log = logging.getLogger(__name__)


TOP_PACKAGES_URL = (
    "https://hugovk.github.io/top-pypi-packages/top-pypi-packages.min.json"
)
PACKAGE_JSON_URL = "https://pypi.org/pypi/{name}/json"

_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
)


# Cache process — partagé entre tous les appels.
_TOP_CACHE: list[dict] | None = None


def _load_top_packages() -> list[dict]:
    """Charge (et cache) la liste des top packages PyPI."""
    global _TOP_CACHE
    if _TOP_CACHE is not None:
        return _TOP_CACHE
    try:
        data = get_json(
            TOP_PACKAGES_URL,
            headers={"User-Agent": "Obelisk-Triskell/1.0"},
            max_retries=2,
        )
    except SourceHttpError as e:
        raise RuntimeError(f"PyPI top-packages a échoué : {e}") from e
    rows = []
    if isinstance(data, dict):
        rows = data.get("rows") or data.get("packages") or []
    _TOP_CACHE = rows if isinstance(rows, list) else []
    return _TOP_CACHE


class PyPIAPI:
    """Client PyPI — search par filtrage top-15000, enrich via JSON officiel."""

    def __init__(self) -> None:
        pass

    def available(self) -> bool:
        return True  # endpoints publics

    def search_packages(
        self,
        query: str,
        max_results: int = 30,
        *,
        enrich_limit: int = 30,
    ) -> list[dict]:
        """Cherche dans les 15 000 packages PyPI les plus téléchargés."""
        if not query:
            return []
        top = _load_top_packages()
        tokens = [t.lower() for t in re.split(r"\W+", query) if len(t) >= 2]
        if not tokens:
            return []

        # Filtre : nom de package matche au moins un token entier OU contient
        # un token comme préfixe/suffixe (langchain → langchain-community, etc.)
        matches: list[str] = []
        for row in top:
            name = (row.get("project") or row.get("name") or "").strip()
            if not name:
                continue
            n = name.lower()
            for tok in tokens:
                # Match strict : token bordé par séparateurs OU préfixe/suffixe
                if (re.search(rf"(^|[\W_]){re.escape(tok)}([\W_]|$)", n)
                    or n.startswith(tok + "-")
                    or n.endswith("-" + tok)
                    or n == tok):
                    matches.append(name)
                    break
            if len(matches) >= max_results * 3:
                # Cap pour ne pas trop fetcher
                break

        # Trim à max_results et enrich
        results: list[dict] = []
        for name in matches[:max_results]:
            p = self._build_prospect(name,
                                     enrich=len(results) < enrich_limit)
            if p:
                results.append(p)
        return results

    def _build_prospect(self, package_name: str, *, enrich: bool) -> dict | None:
        base = {
            "platform":    "pypi",
            "id":          f"pypi:{package_name}",
            "name":        package_name,
            "handle":      package_name,
            "subscribers": None,
            "subs_hidden": True,
            "description": "",
            "thumbnail":   "",
            "url":         f"https://pypi.org/project/{quote_plus(package_name)}/",
            "emails":      [],
            "urls_in_bio": [],
        }
        if not enrich:
            return base
        try:
            data = get_json(
                PACKAGE_JSON_URL.format(name=quote_plus(package_name)),
                headers={"User-Agent": "Obelisk-Triskell/1.0",
                         "Accept": "application/json"},
                max_retries=1,
                accept_404=True,
            )
        except SourceHttpError:
            return base
        if not isinstance(data, dict):
            return base
        info = data.get("info") or {}

        emails: list[str] = []
        for raw in (info.get("author_email") or "",
                    info.get("maintainer_email") or ""):
            for m in _EMAIL_RE.findall(raw):
                e = m.lower()
                if e not in emails:
                    emails.append(e)

        author = (info.get("author") or info.get("maintainer") or "").strip()
        # Format souvent "Prénom Nom <email>", on garde la partie nom
        author_display = author.split("<")[0].strip() or package_name
        summary = info.get("summary") or ""
        desc_md = info.get("description") or ""
        full_desc = "\n\n".join(filter(None,
                                       [summary, desc_md[:1800]]))[:4000]

        urls: list[str] = []
        home = info.get("home_page") or ""
        if home and home.startswith("http"):
            urls.append(home)
        project_urls = info.get("project_urls") or {}
        if isinstance(project_urls, dict):
            for v in project_urls.values():
                if isinstance(v, str) and v.startswith("http") and v not in urls:
                    urls.append(v)

        base.update({
            "name":        author_display,
            "description": full_desc,
            "emails":      emails,
            "urls_in_bio": urls[:5],
            "website":     urls[0] if urls else "",
            "language":    "en",
        })
        return base


__all__ = ["PyPIAPI"]
