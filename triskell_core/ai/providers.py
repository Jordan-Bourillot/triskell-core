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


PROVIDERS: dict[str, dict] = {
    "anthropic": {
        "label": "Anthropic (Claude)",
        "key_field": "anthropic",
        "caller": call_anthropic,
        "models": [
            "claude-opus-4-5",
            "claude-sonnet-4-5",
            "claude-haiku-4-5",
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
