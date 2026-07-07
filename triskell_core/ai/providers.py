"""AI provider integrations: OpenAI, Anthropic, Google Gemini, Mistral, xAI Grok.

Origine : extrait depuis Prompts/ultimate_prompt_app/ai_providers.py.

Tous les providers exposent la même interface : send(prompt, model, api_key) -> str
On utilise HTTP brut via requests pour éviter la course aux versions des SDKs.
"""
from __future__ import annotations

import json
import logging
from typing import Callable

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120  # seconds


class ProviderError(Exception):
    """Levée quand un appel à un provider IA échoue."""


def _validate(prompt: str, api_key: str, provider: str) -> None:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ProviderError("Prompt vide.")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ProviderError(f"Clé API manquante pour {provider}.")


def _post(url: str, headers: dict, payload: dict, provider: str) -> dict:
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as exc:
        raise ProviderError(f"{provider}: erreur réseau ({exc})") from exc
    if r.status_code >= 400:
        try:
            body = r.json()
        except ValueError:
            body = r.text
        raise ProviderError(
            f"{provider} HTTP {r.status_code}: {json.dumps(body)[:500]}"
        )
    try:
        return r.json()
    except ValueError as exc:
        raise ProviderError(f"{provider}: réponse JSON invalide") from exc


def call_openai(prompt: str, model: str, api_key: str) -> str:
    _validate(prompt, api_key, "OpenAI")
    data = _post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        payload={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        },
        provider="OpenAI",
    )
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError(f"OpenAI: format de réponse inattendu ({exc})") from exc


def call_anthropic(prompt: str, model: str, api_key: str) -> str:
    _validate(prompt, api_key, "Anthropic")
    data = _post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        payload={
            "model": model,
            # 8192 (et non 4096) : les réponses JSON longues (audit GEO :
            # plusieurs blocs HTML de 200+ mots) étaient TRONQUÉES en plein
            # JSON → « réponse non exploitable » (constaté le 13/06/2026).
            # Anthropic facture à l'usage réel, pas au plafond.
            "max_tokens": 8192,
            "messages": [{"role": "user", "content": prompt}],
        },
        provider="Anthropic",
    )
    try:
        parts = data["content"]
        return "".join(p.get("text", "") for p in parts if p.get("type") == "text")
    except (KeyError, TypeError) as exc:
        raise ProviderError(f"Anthropic: format de réponse inattendu ({exc})") from exc


def call_google(prompt: str, model: str, api_key: str) -> str:
    _validate(prompt, api_key, "Google")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    data = _post(
        url,
        headers={"Content-Type": "application/json"},
        payload={"contents": [{"parts": [{"text": prompt}]}]},
        provider="Google",
    )
    try:
        cands = data["candidates"][0]
        parts = cands["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError(f"Google: format de réponse inattendu ({exc})") from exc


def call_mistral(prompt: str, model: str, api_key: str) -> str:
    _validate(prompt, api_key, "Mistral")
    data = _post(
        "https://api.mistral.ai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        payload={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        },
        provider="Mistral",
    )
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError(f"Mistral: format de réponse inattendu ({exc})") from exc


def call_xai(prompt: str, model: str, api_key: str) -> str:
    _validate(prompt, api_key, "xAI")
    data = _post(
        "https://api.x.ai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        payload={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        },
        provider="xAI",
    )
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError(f"xAI: format de réponse inattendu ({exc})") from exc


def call_deepseek(prompt: str, model: str, api_key: str) -> str:
    # API DeepSeek : format compatible OpenAI (mêmes champs messages/choices).
    _validate(prompt, api_key, "DeepSeek")
    data = _post(
        "https://api.deepseek.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        payload={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        },
        provider="DeepSeek",
    )
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError(f"DeepSeek: format de réponse inattendu ({exc})") from exc


PROVIDERS: dict[str, dict] = {
    "anthropic": {
        "label": "Anthropic (Claude)",
        "key_field": "anthropic",
        "caller": call_anthropic,
        # ⚠️ L'ORDRE COMPTE : models[0] est le modèle par défaut renvoyé par
        # default_model_for(). Sonnet en tête, JAMAIS Opus : un appel qui
        # oublie de préciser le modèle ne doit pas partir en Opus (le plus
        # cher) par accident.
        "models": [
            "claude-sonnet-4-5",
            "claude-haiku-4-5",
            "claude-opus-4-5",
            "claude-3-5-sonnet-latest",
            "claude-3-5-haiku-latest",
        ],
    },
    "openai": {
        "label": "OpenAI (GPT)",
        "key_field": "openai",
        "caller": call_openai,
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"],
    },
    "google": {
        "label": "Google (Gemini)",
        "key_field": "google",
        "caller": call_google,
        "models": [
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-2.5-flash-lite",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
            "gemini-flash-latest",
            "gemini-pro-latest",
        ],
    },
    "mistral": {
        "label": "Mistral",
        "key_field": "mistral",
        "caller": call_mistral,
        "models": ["mistral-large-latest", "mistral-small-latest", "open-mistral-nemo"],
    },
    "xai": {
        "label": "xAI (Grok)",
        "key_field": "xai",
        "caller": call_xai,
        "models": ["grok-2-latest", "grok-beta"],
    },
    "deepseek": {
        "label": "DeepSeek",
        "key_field": "deepseek",
        "caller": call_deepseek,
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
}


def send_to_provider(
    provider_id: str, model: str, prompt: str, api_keys: dict[str, str]
) -> str:
    """Dispatch vers le bon provider. Renvoie la réponse texte de l'IA."""
    if provider_id not in PROVIDERS:
        raise ProviderError(f"Provider inconnu: {provider_id}")
    info = PROVIDERS[provider_id]
    caller: Callable[[str, str, str], str] = info["caller"]
    key = api_keys.get(info["key_field"], "")
    return caller(prompt, model, key)


# ---------------------------------------------------------------------------
# Bascule automatique entre IA (fallback)
# ---------------------------------------------------------------------------
# Quand l'IA configurée tombe en panne (plus de crédit, coupure réseau,
# surcharge…), on ne veut pas bloquer toute la chaîne : on essaie les autres
# IA dont une clé est enregistrée, par ordre de priorité, jusqu'à ce qu'une
# réponde. Sert surtout à la 2e IA de relecture (quality_reviewer), pour qu'une
# simple panne de crédit ne fige plus la prospection en silence.

# Ordre de priorité par défaut, de la « meilleure » à la moins bonne pour du
# jugement / de la rédaction de texte. L'IA explicitement choisie dans les
# réglages passe TOUJOURS en premier ; ce classement ne sert qu'aux secours.
DEFAULT_PRIORITY: list[str] = ["anthropic", "openai", "google", "deepseek", "mistral", "xai"]


class AllProvidersFailed(ProviderError):
    """Aucune IA disponible n'a pu répondre (toutes en panne ou sans clé)."""

    def __init__(self, errors: dict[str, str]):
        self.errors = dict(errors or {})
        if self.errors:
            detail = " | ".join(f"{p}: {e}" for p, e in self.errors.items())
        else:
            detail = "aucune clé IA enregistrée"
        super().__init__(f"Aucune IA disponible — {detail}")


def available_providers(api_keys: dict[str, str]) -> list[str]:
    """Providers pour lesquels une clé non vide est enregistrée, classés par
    priorité par défaut."""
    keys = api_keys or {}
    return [
        p for p in DEFAULT_PRIORITY
        if (keys.get(PROVIDERS[p]["key_field"], "") or "").strip()
    ]


def default_model_for(provider_id: str) -> str:
    """Meilleur modèle par défaut (1er de la liste) d'un provider."""
    info = PROVIDERS.get(provider_id)
    if not info or not info.get("models"):
        return ""
    return info["models"][0]


# Modèle le moins cher « assez bon » de chaque provider, pour les tâches
# SIMPLES (relire/noter un mail, trier, classer) où le gros modèle est du
# pur gaspillage. Le jugement d'un mail ne demande pas Sonnet/Opus.
CHEAP_MODEL: dict[str, str] = {
    "anthropic": "claude-haiku-4-5",
    "openai": "gpt-4o-mini",
    "google": "gemini-2.5-flash-lite",
    "mistral": "mistral-small-latest",
    "xai": "grok-2-latest",
    "deepseek": "deepseek-chat",
}


def cheap_model_for(provider_id: str) -> str:
    """Modèle le moins cher 'assez bon' d'un provider pour les tâches simples.
    Repli : le modèle par défaut du provider s'il n'est pas répertorié."""
    return CHEAP_MODEL.get(provider_id) or default_model_for(provider_id)


def send_with_fallback(
    preferred_provider: str,
    preferred_model: str,
    prompt: str,
    api_keys: dict[str, str],
    *,
    priority: list[str] | None = None,
) -> tuple[str, str, str]:
    """Essaie l'IA préférée, puis bascule sur les autres IA enregistrées par
    ordre de priorité jusqu'à ce qu'une réponde.

    Renvoie (texte_reponse, provider_utilise, modele_utilise).
    Lève AllProvidersFailed si AUCUNE IA n'a pu répondre (toutes en panne ou
    aucune clé enregistrée).

    - L'IA préférée garde le modèle demandé (preferred_model).
    - Les IA de secours utilisent leur meilleur modèle par défaut
      (preferred_model est un nom de modèle propre à l'IA préférée, il ne
      veut rien dire ailleurs).
    - Une IA sans clé enregistrée est sautée en silence (ce n'est pas une
      panne) ; seul un vrai échec d'appel est mémorisé dans le rapport.
    """
    keys = api_keys or {}
    order: list[str] = []
    if preferred_provider in PROVIDERS:
        order.append(preferred_provider)
    for p in (priority or DEFAULT_PRIORITY):
        if p in PROVIDERS and p not in order:
            order.append(p)

    errors: dict[str, str] = {}
    for prov in order:
        key = (keys.get(PROVIDERS[prov]["key_field"], "") or "").strip()
        if not key:
            continue  # pas de clé pour cette IA → on saute (ce n'est pas une panne)
        if prov == preferred_provider and (preferred_model or "").strip():
            model = preferred_model
        else:
            model = default_model_for(prov)
        try:
            text = send_to_provider(prov, model, prompt, keys)
        except ProviderError as exc:
            errors[prov] = str(exc)
            logger.warning("send_with_fallback: %s a échoué (%s)", prov, exc)
            continue
        except Exception as exc:  # robustesse maximale : un provider exotique
            errors[prov] = f"erreur inattendue ({exc})"
            logger.warning("send_with_fallback: %s erreur inattendue (%s)", prov, exc)
            continue
        if text and text.strip():
            if prov != preferred_provider:
                logger.info(
                    "send_with_fallback: bascule sur '%s' (préféré '%s' indisponible)",
                    prov, preferred_provider,
                )
            return text, prov, model
        errors[prov] = "réponse vide"
    raise AllProvidersFailed(errors)
