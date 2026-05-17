"""
Source Obelisk — pioche les créateurs/vendeurs déposés par Obelisk
(ex-Le Dénicheur) dans la base partagée Triskell (Supabase).

Différence avec sirene.py / maps.py :
  - Sirene/Maps font une recherche EXTERNE et créent de nouveaux Prospects.
  - Obelisk a déjà fait ce boulot en amont : il a scrapé les réseaux
    (YouTube, TikTok, Insta, Twitch…) et déposé les créateurs dans la
    table `prospects` partagée. Cette source se contente donc de
    sélectionner ceux qui sont déjà là, en filtrant sur des critères
    métier (plateforme, taille d'audience, langue, géo, etc.).

Marqueur d'appartenance Obelisk : Prospect.sources[*].name == "denicheur"
(historique : la base interne d'Obelisk s'appelait "denicheur" avant le
rebrand).
"""

from __future__ import annotations

from typing import Iterator

from ..core.prospect import Prospect


# Le nom de source historiquement écrit par Obelisk dans la base (pas
# changé au rebrand pour ne pas casser les données existantes).
OBELISK_SOURCE_NAME = "denicheur"


def is_available() -> bool:
    """Vrai si la base partagée est joignable + authentifiée."""
    try:
        from triskell_core.db import get_client, SupabaseNotConfigured
        try:
            client = get_client()
        except SupabaseNotConfigured:
            return False
        return bool(client.is_authenticated)
    except Exception:
        return False


def search(
    *,
    platform: str = "",
    min_subscribers: int | None = None,
    max_subscribers: int | None = None,
    country: str = "",
    language: str = "",
    only_with_email: bool = False,
    only_uncontacted: bool = True,
    monetized_only: bool = False,
    max_results: int = 50,
    offset: int = 0,
    shuffle_seed: int | None = None,
    cursor_out: dict | None = None,
) -> Iterator[Prospect]:
    """Itère les prospects Obelisk déposés en base, filtrés.

    Args:
        platform        : "youtube" / "tiktok" / "instagram" / … ou "" pour tout
        min_subscribers : ne garde que ceux avec ≥ N abonnés
        max_subscribers : ne garde que ceux avec ≤ N abonnés
        country         : code ISO 2 (ex "FR") ou nom plein, "" pour tout
        language        : code ISO 2 (ex "fr") ou "" pour tout
        only_with_email : exclut ceux sans email connu
        only_uncontacted: exclut ceux déjà contactés (status != 'new'/'qualified')
        monetized_only  : ne garde que les profils détectés monétisés
        max_results     : plafond d'éléments yieldés

    Note : on ne fait PAS de pagination Supabase ici — on charge tout
    via crm.all() (qui a déjà la logique cache/sync) puis on filtre en
    mémoire. Acceptable jusqu'à ~10k prospects en base.
    """
    try:
        from ..core.crm import get_crm
    except ImportError:
        from ..core.crm import CRM
        get_crm = None  # type: ignore

    crm = get_crm() if get_crm else CRM()
    all_prospects = list(crm.all())

    pf = (platform or "").strip().lower()
    co = (country or "").strip().lower()
    la = (language or "").strip().lower()

    # États considérés comme "encore contactables"
    UNCONTACTED_STATUSES = {"new", "qualified", ""}

    # Filtre d'abord, puis applique offset + shuffle pour ne pas retomber
    # sur les mêmes prospects en tête de liste à chaque run.
    candidates = []
    for p in all_prospects:
        if not _is_obelisk(p):
            continue
        if pf and (p.industry or "").lower() != pf:
            continue
        subs = p.subscribers
        if min_subscribers is not None:
            if subs is None or subs < min_subscribers:
                continue
        if max_subscribers is not None:
            if subs is None or subs > max_subscribers:
                continue
        if co and (p.country or "").lower() != co:
            continue
        if la and (p.language or "").lower() != la:
            continue
        if only_with_email and not (p.emails or []):
            continue
        if only_uncontacted:
            if (p.status or "").lower() not in UNCONTACTED_STATUSES:
                continue
        if monetized_only and not p.monetized:
            continue
        candidates.append(p)

    total = len(candidates)

    if shuffle_seed is not None:
        import random
        random.Random(shuffle_seed).shuffle(candidates)

    start = max(0, int(offset or 0))
    if start >= total:
        # On a déjà tout balayé : on recommence depuis le début pour ne
        # pas renvoyer 0 résultat (l'anti-doublon CRM fera le tri ensuite).
        start = 0

    yielded = 0
    consumed = 0
    for p in candidates[start:]:
        if yielded >= max_results:
            break
        yielded += 1
        consumed += 1
        yield p

    if cursor_out is not None:
        cursor_out["next_offset"] = start + consumed
        cursor_out["total"] = total
        cursor_out["exhausted"] = (start + consumed) >= total


def _is_obelisk(prospect: Prospect) -> bool:
    """Vrai si l'un des Source de ce prospect est Obelisk (denicheur)."""
    for s in (prospect.sources or []):
        # Source peut être un dataclass Source ou un dict (selon backend)
        name = getattr(s, "name", None) or (s.get("name") if isinstance(s, dict) else "")
        if (name or "").lower() == OBELISK_SOURCE_NAME:
            return True
    return False
