"""Quality reviewer — 2e IA qui relit chaque mail avant envoi.

Etape 7 du chantier Auto-pilote v2. Quand l'autopilote vient de generer un
mail (par IA libre ou pioche template), il l'envoie ici pour relecture.
La 2e IA renvoie une note sur 10 + un verdict + un commentaire.

Strategie :
- Meme provider + meme model que la generation (pour ne pas multiplier
  les cles API). On peut differencier plus tard si besoin.
- Prompt strict : on demande JSON {score: int 1-10, verdict: 'ok'|'draft',
  comment: str}. Si la reponse est malformee, on est conservatif (verdict
  'draft' pour que l'humain relise).
- Pas de retry : si la 2e IA timeout / plante, on retourne un verdict
  'draft' avec score=0 -- l'humain aura le dernier mot.

Aucune dependance a triskell-command : reutilise les providers IA de
triskell_core (ai/providers.py).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


_REVIEW_PROMPT = """\
Tu es relecteur senior de mails de prospection. Le mail ci-dessous a ete \
genere a partir d'un template ecrit a la main par l'auteur. Ton role :

1) Noter le mail sur 10 selon ces criteres :
   - Personnalisation (le mail parle vraiment de ce prospect) : 3 pts
   - Clarte de l'offre (le destinataire comprend ce qu'on propose) : 3 pts
   - Ton naturel, pas de jargon, pas de bullshit : 2 pts
   - Pas de variables non remplies type {{nom}} ou {{ville}} : 2 pts

2) Eventuellement, proposer UNE micro-amelioration du corps si tu vois un \
truc vraiment derangeant (phrase bancale, formulation lourde, repetition \
evidente, mot qui sonne faux pour ce prospect precis). Sinon, laisse-le tel \
quel. Tu ne reecris JAMAIS le mail en entier : juste 1 a 2 phrases au max.

REGLES :
- score >= 7 -> verdict 'ok' (envoi autorise)
- score < 7  -> verdict 'draft' (brouillon pour validation manuelle)
- Si tu ne proposes aucune amelioration -> body_revised = "" (chaine vide).
- Si tu en proposes une, body_revised = le corps complet du mail avec ta \
modif integree (et SEULEMENT ta modif). Le reste du mail reste intact.
- Ne touche pas a la signature, ni aux URLs, ni au sujet.
{tone_rule}

Reponds UNIQUEMENT en JSON, sans markdown, sans commentaire avant ou apres :
{{
  "score": <int 1-10>,
  "verdict": "ok" ou "draft",
  "comment": "<une phrase qui explique la note et eventuellement ce que tu as ajuste>",
  "body_revised": "<corps modifie OU chaine vide si rien a ajuster>"
}}

CONTEXTE DU PROSPECT :
{prospect_context}

OBJET DU MAIL :
{subject}

CORPS DU MAIL :
{body}
"""


def review_email(
    *,
    subject: str,
    body: str,
    prospect_context: str,
    provider: str,
    model: str,
    api_keys: dict[str, str],
    audience: str = "",
) -> dict[str, Any]:
    """Demande a la 2e IA de relire et noter le mail.

    Renvoie {score: int, verdict: 'ok'|'draft', comment: str, raw: str}.

    audience : 'creator' -> les modeles createurs sont VOLONTAIREMENT en
    tutoiement (ecrits a la main), la relectrice ne doit pas penaliser ca.
    Autre valeur / vide -> regle pro classique (vouvoiement obligatoire).

    En cas d'erreur de l'IA ou de reponse malformee, renvoie un verdict
    conservatif ('draft' avec score=0) pour que l'humain ait le dernier mot.
    """
    try:
        from ..ai import providers as ai_providers
    except ImportError as exc:
        logger.warning("quality_reviewer: providers IA indisponibles (%s)", exc)
        return {"score": 0, "verdict": "draft",
                "comment": "providers IA indisponibles",
                "engine_down": True,
                "body_revised": "", "raw": ""}

    if (audience or "").strip().lower() == "creator":
        tone_rule = (
            "- Ce prospect est un CREATEUR (YouTube, Twitch...) : le "
            "tutoiement est VOULU par l'auteur du modele. Ne baisse pas la "
            "note pour ca et ne le transforme pas en vouvoiement."
        )
    else:
        tone_rule = "- VOUVOIEMENT obligatoire (jamais de tutoiement)."

    prompt = _REVIEW_PROMPT.format(
        prospect_context=(prospect_context or "(aucun contexte)").strip()[:600],
        subject=(subject or "(sans objet)").strip()[:200],
        body=(body or "").strip()[:3000],
        tone_rule=tone_rule,
    )

    raw = ""
    used_provider = provider
    try:
        # Bascule automatique : si l'IA préférée est en panne (plus de crédit,
        # coupure…), on essaie les autres IA enregistrées par ordre de priorité.
        raw, used_provider, _used_model = ai_providers.send_with_fallback(
            provider, model, prompt, api_keys)
        raw = raw or ""
    except ai_providers.AllProvidersFailed as exc:
        # AUCUNE IA n'a pu relire. On NE met PAS un faux « 0/10 » : on signale
        # une panne (engine_down) pour que l'écran l'affiche comme telle. Le
        # mail reste en brouillon par sécurité (l'humain a le dernier mot).
        logger.warning("quality_reviewer: aucune IA disponible (%s)", exc)
        return {"score": 0, "verdict": "draft",
                "comment": ("Le correcteur (2e IA) n'a pas pu relire : aucune IA "
                            "disponible (plus de crédit ou coupure). Mail gardé en "
                            "brouillon par sécurité — recharge tes crédits ou "
                            "ajoute une autre IA dans Réglages."),
                "engine_down": True,
                "body_revised": "", "raw": ""}
    except Exception as exc:  # garde-fou : ne jamais laisser remonter
        logger.warning("quality_reviewer: appel IA a echoue (%s)", exc)
        return {"score": 0, "verdict": "draft",
                "comment": f"reviewer plante : {exc}",
                "engine_down": True,
                "body_revised": "", "raw": raw}

    out = _parse_review(raw)
    out["reviewed_by"] = used_provider
    return out


def _parse_review(raw: str) -> dict[str, Any]:
    """Extrait {score, verdict, comment} d'une reponse IA tolerante."""
    raw = (raw or "").strip()
    if not raw:
        return {"score": 0, "verdict": "draft",
                "comment": "reviewer vide",
                "body_revised": "", "raw": ""}
    # Vire d'eventuels backticks markdown autour du JSON
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw,
                     flags=re.IGNORECASE | re.MULTILINE).strip()
    # Cherche le 1er objet JSON dans la reponse (au cas ou l'IA radote avant)
    m = re.search(r"\{[\s\S]*?\}", cleaned)
    if not m:
        return {"score": 0, "verdict": "draft",
                "comment": "reviewer reponse non-JSON",
                "body_revised": "", "raw": raw}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        return {"score": 0, "verdict": "draft",
                "comment": f"reviewer JSON invalide : {exc}",
                "body_revised": "", "raw": raw}
    try:
        score = int(data.get("score", 0))
    except Exception:
        score = 0
    score = max(0, min(10, score))
    verdict = str(data.get("verdict") or "").lower().strip()
    if verdict not in ("ok", "draft"):
        # Conservatif : si verdict pas explicite, on se base sur le score
        verdict = "ok" if score >= 7 else "draft"
    comment = str(data.get("comment") or "").strip()[:300]
    body_revised = str(data.get("body_revised") or "").strip()
    return {"score": score, "verdict": verdict, "comment": comment,
            "body_revised": body_revised, "raw": raw}
