"""Source Mastodon — recherche de comptes via plusieurs instances publiques.

API publique gratuite (lecture seule, pas d'auth) :
- /api/v2/search?type=accounts&q=<query>&limit=40&resolve=false

Mastodon est fédéré : pas de "moteur global". On interroge plusieurs instances
populaires en parallèle puis on agrège/dédoublonne par `acct` (nom@instance).

Instances par défaut (les plus peuplées, FR + EN) :
- mastodon.social  (instance phare, multi-langue)
- mastodon.world   (international)
- mas.to           (généraliste)
- piaille.fr       (FR généraliste)
- mamot.fr         (FR La Quadrature)

Pour ajouter d'autres instances : `MastodonAPI(extra_instances=["pixelfed.social"])`.
"""

from __future__ import annotations

import logging

from ._http import SourceHttpError, get_json

log = logging.getLogger(__name__)


DEFAULT_INSTANCES = (
    "mastodon.social",
    "mastodon.world",
    "mas.to",
    "piaille.fr",
    "mamot.fr",
)


class MastodonAPI:
    """Client Mastodon multi-instances (lecture publique)."""

    def __init__(self, instances: list[str] | None = None,
                 extra_instances: list[str] | None = None) -> None:
        seeds = list(instances) if instances is not None else list(DEFAULT_INSTANCES)
        for ex in (extra_instances or []):
            ex = ex.strip().lower()
            if ex and ex not in seeds:
                seeds.append(ex)
        self.instances = seeds

    def available(self) -> bool:
        return bool(self.instances)

    def search_accounts(self, query: str, max_results: int = 50) -> list[dict]:
        """Cherche des comptes par mot-clé sur toutes les instances configurées."""
        if not query:
            return []
        # Répartit le quota uniformément entre les instances (au moins 5 par instance)
        per_instance = max(5, min(40, (max_results // max(1, len(self.instances))) + 5))

        seen_acct: set[str] = set()
        results: list[dict] = []
        for instance in self.instances:
            url = f"https://{instance}/api/v2/search"
            params = {
                "q": query,
                "type": "accounts",
                "limit": per_instance,
                "resolve": "false",  # ne tente pas de résoudre les acteurs distants
            }
            try:
                data = get_json(url, params=params, max_retries=1, accept_404=True)
            except SourceHttpError as e:
                log.debug("Mastodon %s search a échoué : %s", instance, e)
                continue
            if not isinstance(data, dict):
                continue
            accounts = data.get("accounts", []) or []
            for acc in accounts:
                acct = acc.get("acct", "") or ""
                if not acct:
                    continue
                # Si pas de @, c'est un compte local de l'instance qu'on interroge
                full_acct = acct if "@" in acct else f"{acct}@{instance}"
                if full_acct in seen_acct:
                    continue
                seen_acct.add(full_acct)
                username = acc.get("username", "") or ""
                acct_url = acc.get("url", "") or ""
                # Note bien : Mastodon expose la bio en HTML brut (note). On
                # nettoiera côté pipeline (creators._from_raw lit le texte)
                bio = acc.get("note", "") or ""
                bio_text = _strip_html(bio)
                fields_text = _join_fields(acc.get("fields", []) or [])
                results.append({
                    "platform":    "mastodon",
                    "id":          str(acc.get("id", "") or full_acct),
                    "name":        acc.get("display_name") or username,
                    "handle":      full_acct,
                    "subscribers": acc.get("followers_count") or 0,
                    "subs_hidden": False,
                    "description": (bio_text + ("\n" + fields_text if fields_text else ""))[:4000],
                    "language":    (acc.get("language") or "")[:2].lower(),
                    "thumbnail":   acc.get("avatar") or "",
                    "url":         acct_url,
                    "posts_count": acc.get("statuses_count") or 0,
                    "follows_count": acc.get("following_count") or 0,
                    "created_at":  acc.get("created_at", ""),
                    "instance":    instance,
                })
                if len(results) >= max_results:
                    return results
        return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _strip_html(html: str) -> str:
    """Vire les balises HTML d'une bio Mastodon (sans dépendre de BS4)."""
    if not html:
        return ""
    import re as _re
    text = _re.sub(r"<br\s*/?>", "\n", html, flags=_re.IGNORECASE)
    text = _re.sub(r"</p>", "\n\n", text, flags=_re.IGNORECASE)
    text = _re.sub(r"<[^>]+>", " ", text)
    text = _re.sub(r"\s+\n", "\n", text)
    text = _re.sub(r"[ \t]+", " ", text)
    # Décode quelques entités HTML courantes
    text = (text
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&#39;", "'")
            .replace("&nbsp;", " "))
    return text.strip()


def _join_fields(fields: list[dict]) -> str:
    """Concatène les fields Mastodon (souvent : "Email", "Site", "Pronoms"…)."""
    parts: list[str] = []
    for f in fields:
        name = f.get("name", "") or ""
        value = f.get("value", "") or ""
        value_text = _strip_html(value)
        if name and value_text:
            parts.append(f"{name}: {value_text}")
        elif value_text:
            parts.append(value_text)
    return "\n".join(parts)
