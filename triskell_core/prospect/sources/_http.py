"""HTTP helpers partagés par toutes les sources : retry exponentiel, throttle
par domaine, gestion uniforme des erreurs.

Toutes les sources publiques (Bluesky, Mastodon, Apple Podcasts, Dailymotion,
Kick, GitHub, Reddit, …) utilisent cet utilitaire. YouTube et Twitch ont leur
propre logique de rotation de clés mais peuvent en hériter pour les retries.

Conception :
- Un seul `requests.Session` par process (réutilisation des connexions TCP).
- Retry sur erreurs réseau et codes 5xx, 429. Pas de retry sur 4xx (sauf 429).
- Backoff exponentiel : 0.5s → 1s → 2s → 4s, plafonné à 8s.
- Throttle par domaine optionnel (utile pour les API gratuites sans budget).
- User-Agent uniforme : `TriskellProspect/<version> (...)`.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)


USER_AGENT = "TriskellProspect/1.0 (+https://triskell-studio.fr)"
DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE = 0.5
DEFAULT_BACKOFF_CAP = 8.0


# ---------------------------------------------------------------------------
# Singleton session
# ---------------------------------------------------------------------------
_session_lock = threading.Lock()
_SESSION: requests.Session | None = None


def get_session() -> requests.Session:
    """Renvoie le session partagé. Thread-safe."""
    global _SESSION
    if _SESSION is None:
        with _session_lock:
            if _SESSION is None:
                s = requests.Session()
                s.headers.update({"User-Agent": USER_AGENT})
                _SESSION = s
    return _SESSION


# ---------------------------------------------------------------------------
# Throttle par domaine
# ---------------------------------------------------------------------------
class DomainRateLimiter:
    """Throttle minimal par domaine pour les API publiques sans clé."""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}
        self._intervals: dict[str, float] = {}
        self._lock = threading.Lock()

    def configure(self, domain: str, min_interval: float) -> None:
        """Définit l'intervalle min entre 2 requêtes pour ce domaine (en s)."""
        with self._lock:
            self._intervals[domain] = float(min_interval)

    def wait(self, url: str) -> None:
        domain = urlparse(url).netloc.lower()
        if not domain:
            return
        min_interval = self._intervals.get(domain, 0.0)
        if min_interval <= 0:
            return
        with self._lock:
            last = self._last.get(domain, 0.0)
            now = time.time()
            delta = now - last
            if delta < min_interval:
                sleep_for = min_interval - delta
            else:
                sleep_for = 0.0
            self._last[domain] = now + sleep_for
        if sleep_for > 0:
            time.sleep(sleep_for)


_RATE_LIMITER = DomainRateLimiter()
# Quelques domaines qu'on cap d'office pour rester poli
_RATE_LIMITER.configure("api.bsky.app", 0.4)
_RATE_LIMITER.configure("public.api.bsky.app", 0.4)
_RATE_LIMITER.configure("api.dailymotion.com", 0.3)
_RATE_LIMITER.configure("itunes.apple.com", 0.3)
_RATE_LIMITER.configure("api.github.com", 1.0)
_RATE_LIMITER.configure("kick.com", 0.6)
_RATE_LIMITER.configure("www.reddit.com", 0.6)


def configure_rate_limit(domain: str, min_interval: float) -> None:
    _RATE_LIMITER.configure(domain, min_interval)


# ---------------------------------------------------------------------------
# Erreur typée
# ---------------------------------------------------------------------------
class SourceHttpError(RuntimeError):
    """Erreur HTTP non récupérable côté source. Attribut `status` si HTTP."""

    def __init__(self, message: str, *, status: int | None = None,
                 url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


# ---------------------------------------------------------------------------
# GET / POST avec retry
# ---------------------------------------------------------------------------
def _backoff_seconds(attempt: int, base: float, cap: float) -> float:
    """Backoff exponentiel avec jitter ±25 %."""
    raw = min(cap, base * (2 ** attempt))
    return raw * (0.75 + 0.5 * random.random())


def request_json(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    data: Any = None,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_base: float = DEFAULT_BACKOFF_BASE,
    backoff_cap: float = DEFAULT_BACKOFF_CAP,
    accept_404: bool = False,
) -> dict | list | None:
    """Effectue une requête HTTP et renvoie le JSON décodé.

    Retry sur 429, 500-599 et erreurs réseau. Pas de retry sur 4xx (sauf 429).

    Args:
        accept_404: si True, renvoie None sur 404 sans lever (utile pour
                    les endpoints "user not found").
    """
    sess = get_session()
    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        _RATE_LIMITER.wait(url)
        try:
            r = sess.request(
                method.upper(),
                url,
                params=params,
                data=data,
                json=json_body,
                headers=headers,
                timeout=timeout,
            )
        except requests.RequestException as e:
            last_err = e
            if attempt < max_retries:
                time.sleep(_backoff_seconds(attempt, backoff_base, backoff_cap))
                continue
            raise SourceHttpError(f"network error on {url} : {e}", url=url) from e

        if r.status_code == 200:
            try:
                return r.json()
            except ValueError as e:
                raise SourceHttpError(f"non-JSON body on {url}", url=url) from e

        if r.status_code == 204:
            return None

        if r.status_code == 404 and accept_404:
            return None

        # Retryables : 429 + 5xx
        if r.status_code in (429,) or 500 <= r.status_code < 600:
            if attempt < max_retries:
                # Honore Retry-After si présent
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    try:
                        time.sleep(min(float(retry_after), backoff_cap))
                    except ValueError:
                        time.sleep(_backoff_seconds(attempt, backoff_base, backoff_cap))
                else:
                    time.sleep(_backoff_seconds(attempt, backoff_base, backoff_cap))
                continue
            raise SourceHttpError(
                f"HTTP {r.status_code} on {url} after {max_retries} retries",
                status=r.status_code, url=url,
            )

        # 4xx non récupérables
        raise SourceHttpError(
            f"HTTP {r.status_code} on {url} : {(r.text or '')[:200]}",
            status=r.status_code, url=url,
        )

    raise SourceHttpError(f"unreachable on {url} : {last_err}", url=url)


def get_json(url: str, **kwargs: Any) -> dict | list | None:
    return request_json("GET", url, **kwargs)


def post_json(url: str, **kwargs: Any) -> dict | list | None:
    return request_json("POST", url, **kwargs)
