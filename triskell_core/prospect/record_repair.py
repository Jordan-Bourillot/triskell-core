"""Réparation des fiches prospects abîmées — la « 3e IA » de la chaîne.

Pourquoi ce module (12/06/2026, demande Jordan) : la 2e IA de relecture
attrape les mails ridicules causés par une FICHE sale (nom pollué par la
description — « GF ARMOR ELEC . électricien. Dépannage… Devis sous 48h. » —
ou secteur mal classé — un ostréiculteur étiqueté « boulangerie » qui
reçoit une démo pâtisserie). Avant : le brouillon fautif restait en
attente et le prospect re-générait le même mail cassé à chaque passage.
Maintenant : quand la relecture met un mail en brouillon, on tente de
réparer la FICHE, et si elle change, le mail est régénéré proprement au
passage suivant.

RÈGLES ABSOLUES (alignées sur la politique de Jordan) :
- AUCUN enrichissement : on ne va JAMAIS chercher d'information à
  l'extérieur. La réparation ne travaille qu'avec ce que la fiche
  contient déjà (nom, secteur, description, ville).
- AUCUNE invention de nom : chaque mot du nom proposé doit exister dans
  le nom d'origine (garde-fou codé en dur, pas confié à l'IA).
- Champs réparables : name, industry, description. RIEN d'autre
  (emails/téléphones/urls intouchables).
- Une seule tentative par fiche (tag `fiche_reparee`) : pas de boucle.
- En cas de doute ou de réponse IA malformée : on ne touche à rien
  (l'humain garde le dernier mot via le brouillon, comme avant).
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Any

logger = logging.getLogger(__name__)

REPAIR_TAG = "fiche_reparee"

# Au-delà de cette longueur, un « nom » d'entreprise est suspect.
_NAME_MAX_REASONABLE = 60


# ---------------------------------------------------------------------------
# Heuristiques sans IA (gratuites, déterministes)
# ---------------------------------------------------------------------------

def name_looks_polluted(name: str) -> bool:
    """Vrai si le nom ressemble à « raison sociale + description collée ».

    Signaux : longueur déraisonnable, ou plusieurs segments de phrase
    (séparés par des points/points-virgules) dont la suite est du
    descriptif (≥ 15 caractères après le premier point).
    """
    n = (name or "").strip()
    if not n:
        return False
    if len(n) > _NAME_MAX_REASONABLE:
        return True
    # « : » inclus : c'est LE séparateur des fiches Google Maps polluées
    # (« FABBI PATRICK PEINTURE: Artisan peintre en bâtiment… »).
    m = re.match(r"^(.{2,60}?)\s*[.;|·:—–]\s+(.{15,})$", n)
    return bool(m)


def split_polluted_name(name: str) -> tuple[str, str] | None:
    """Sépare « raison sociale » et « descriptif » d'un nom pollué.

    Renvoie (nom_propre, reste_descriptif) ou None si le découpage n'est
    pas net (on préfère ne rien faire que de charcuter).
    """
    n = (name or "").strip()
    if not n:
        return None
    m = re.match(r"^(.{2,60}?)\s*[.;|·:—–]\s+(.{15,})$", n)
    if not m:
        return None
    clean = m.group(1).strip(" .,-—·|")
    rest = m.group(2).strip()
    # Le nom propre doit rester substantiel et ne pas être lui-même une phrase.
    if len(clean) < 2 or len(clean) > _NAME_MAX_REASONABLE:
        return None
    return clean, rest


# ---------------------------------------------------------------------------
# Garde-fous sur ce que l'IA propose
# ---------------------------------------------------------------------------

def _norm_words(text: str) -> set[str]:
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return set(re.findall(r"[a-z0-9]+", t.lower()))


def _name_is_subset(proposed: str, original: str) -> bool:
    """Chaque mot du nom proposé doit exister dans le nom d'origine."""
    pw = _norm_words(proposed)
    return bool(pw) and pw.issubset(_norm_words(original))


def _industry_is_sane(industry: str) -> bool:
    s = (industry or "").strip()
    return 2 <= len(s) <= 40 and bool(re.fullmatch(r"[a-zA-ZÀ-ÿ' \-/&]+", s))


# ---------------------------------------------------------------------------
# La 3e IA : diagnostic + proposition de réparation (fiche seule)
# ---------------------------------------------------------------------------

_REPAIR_PROMPT = """Tu vérifies la FICHE d'un prospect dans un logiciel de prospection.
Ta seule mission : repérer si les champs `name` (raison sociale) et
`industry` (métier) sont manifestement faux PAR RAPPORT AU CONTENU MÊME
de la fiche, et proposer la correction.

Cas typiques :
- name contient la raison sociale PLUS une description collée
  (slogan, services, « devis sous 48h »…) → propose le nom seul.
- industry contredit ce que dit le nom ou la description (ex. une
  entreprise « L'île aux huîtres » classée « boulangerie » → propose
  « ostréiculture »).

RÈGLES STRICTES :
- N'utilise QUE les informations de la fiche ci-dessous. N'invente rien,
  ne devine pas à partir de connaissances extérieures.
- Le name proposé ne doit contenir QUE des mots déjà présents dans le
  name actuel (tu retires, tu ne réécris pas).
- industry : un métier court en français (« plombier », « ostréiculture »…).
- Si la fiche est correcte, réponds avec name=null et industry=null.

Réponds UNIQUEMENT ce JSON (aucun texte autour) :
{{"name": "..." ou null, "industry": "..." ou null, "reason": "explication en une phrase"}}

FICHE :
- name : {name}
- industry : {industry}
- description : {description}
- ville : {city}
"""


def propose_repair(
    *,
    name: str,
    industry: str,
    description: str,
    city: str,
    provider: str,
    model: str,
    api_keys: dict[str, str],
) -> dict[str, Any] | None:
    """Demande à la 3e IA une réparation de fiche. None = rien à changer.

    Sortie validée par les garde-fous codés : {"name": str|None,
    "industry": str|None, "reason": str} avec au moins un champ non-None.
    """
    try:
        from ..ai import providers as ai_providers
    except ImportError as exc:
        logger.warning("record_repair: providers IA indisponibles (%s)", exc)
        return None

    prompt = _REPAIR_PROMPT.format(
        name=(name or "(vide)").strip()[:300],
        industry=(industry or "(vide)").strip()[:80],
        description=(description or "(vide)").strip()[:400],
        city=(city or "(vide)").strip()[:80],
    )
    try:
        # Bascule auto entre IA : si l'IA préférée est en panne, on tente les
        # autres IA enregistrées avant d'abandonner la réparation.
        raw, _used_prov, _used_model = ai_providers.send_with_fallback(
            provider, model, prompt, api_keys)
        raw = raw or ""
    except Exception as exc:
        logger.warning("record_repair: appel IA a échoué (%s)", exc)
        return None

    return parse_repair(raw, original_name=name)


def parse_repair(raw: str, *, original_name: str) -> dict[str, Any] | None:
    """Parse tolérant + garde-fous. None si rien d'applicable."""
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    out: dict[str, Any] = {"name": None, "industry": None,
                           "reason": str(data.get("reason") or "")[:200]}

    proposed_name = data.get("name")
    if isinstance(proposed_name, str) and proposed_name.strip():
        cand = proposed_name.strip()[:_NAME_MAX_REASONABLE]
        # Jamais d'invention : sous-ensemble strict des mots d'origine,
        # et un vrai raccourcissement (sinon aucun intérêt).
        if (_name_is_subset(cand, original_name)
                and len(cand) < len((original_name or "").strip())):
            out["name"] = cand

    proposed_ind = data.get("industry")
    if isinstance(proposed_ind, str) and proposed_ind.strip():
        cand = proposed_ind.strip().lower()
        if _industry_is_sane(cand):
            out["industry"] = cand

    if out["name"] is None and out["industry"] is None:
        return None
    return out
