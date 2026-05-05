"""triskell_core.licensing — vérification de certificats de licence Ed25519.

Niveau 3 sécurité : l'app refuse de démarrer sans certificat de licence valide.
Le certificat est un JSON signé avec une clé privée Ed25519 (côté serveur Triskell).
La clé publique correspondante est embarquée dans ce package (`public_key.pem`).

Format du certificat (transmis par email post-achat, à coller dans l'app) :

    Format texte : <base64url_payload>.<base64url_signature>

Le payload décode en JSON :
    {
        "email":       "<acheteur@example.com>",
        "session_id":  "<cs_xxx>",
        "product":     "le-denicheur-v1",
        "issued_at":   <epoch_ms>,
        "expires_at":  <epoch_ms | null>,
        "version":     1
    }

La signature couvre exactement les bytes du payload encodé base64url.

Sécurité :
- Ed25519 = signature asymétrique. Personne ne peut forger un certificat sans
  la clé privée (qui vit côté serveur Netlify uniquement).
- La vérification est offline (la clé publique est embarquée dans le binaire)
- Si le binaire est patché pour bypass `verify_license()`, on peut tomber sur du
  reverse-engineering — mais c'est l'effort. Niveau 3 protège contre 99 % des
  copies pirates "naïves" (partage du Setup.exe par mail / Discord).
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Optional


# === Chargement de la clé publique ==========================================
def _load_public_key():
    """Lit public_key.pem fourni avec ce package."""
    from cryptography.hazmat.primitives import serialization

    pub_path = Path(__file__).resolve().parent / "public_key.pem"
    if not pub_path.exists():
        raise RuntimeError(
            f"Clé publique manquante : {pub_path}. "
            "Le bundle est cassé ou la clé n'a pas été embarquée."
        )
    pem = pub_path.read_bytes()
    return serialization.load_pem_public_key(pem)


_PUBLIC_KEY = None


def get_public_key():
    """Cache de la clé publique chargée une fois pour toutes."""
    global _PUBLIC_KEY
    if _PUBLIC_KEY is None:
        _PUBLIC_KEY = _load_public_key()
    return _PUBLIC_KEY


# === Vérification d'un certificat ==========================================
class LicenseError(Exception):
    """Levée si la licence est invalide. Le message est utilisateur-friendly."""


def verify_license_certificate(certificate: str) -> dict:
    """Vérifie la signature Ed25519 du certificat et retourne le payload.

    Lève LicenseError avec un message clair si :
      - Format invalide (pas de '.', base64 corrompu, JSON cassé)
      - Signature invalide (clé fausse, payload modifié, mauvaise privée serveur)
      - Certificat expiré
      - Mauvais produit

    Args:
        certificate: chaîne "<base64url_payload>.<base64url_signature>"

    Returns:
        Le payload décodé (dict avec email, session_id, expires_at, etc.)

    Sécurité : utiliser ce résultat pour décider d'autoriser le démarrage de
    l'app. Ne PAS bypasser cette vérification.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if not certificate or not isinstance(certificate, str):
        raise LicenseError("Aucun certificat de licence fourni.")

    cert = certificate.strip()
    if "." not in cert:
        raise LicenseError("Format invalide : le certificat doit contenir un point '.'.")

    encoded_payload, _, encoded_sig = cert.partition(".")
    if not encoded_payload or not encoded_sig:
        raise LicenseError("Format invalide : payload ou signature vide.")

    # Décode base64url (avec padding)
    def b64d(s: str) -> bytes:
        try:
            return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
        except Exception:
            raise LicenseError("Certificat corrompu (base64 invalide).")

    payload_bytes = b64d(encoded_payload)
    sig_bytes = b64d(encoded_sig)

    # Vérification de signature Ed25519
    try:
        pub = get_public_key()
        if not isinstance(pub, Ed25519PublicKey):
            raise LicenseError("Clé publique embarquée incorrecte (pas Ed25519).")
        # Le payload signé = les bytes encodés (pas le JSON décodé) — garantit
        # qu'on signe la même chose que ce qui est envoyé sur le réseau.
        pub.verify(sig_bytes, encoded_payload.encode("ascii"))
    except InvalidSignature:
        raise LicenseError(
            "Signature de licence invalide. Cette clé n'a pas été émise par "
            "Triskell Studio, ou elle a été modifiée."
        )

    # Décode le payload JSON
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        raise LicenseError("Payload de licence illisible (JSON cassé).")

    # Vérifications métier
    if not isinstance(payload, dict):
        raise LicenseError("Format de payload invalide (attendu : objet JSON).")

    if payload.get("product") != "le-denicheur-v1":
        raise LicenseError(
            "Cette licence ne concerne pas Le Dénicheur "
            f"(produit : {payload.get('product')!r})."
        )

    expires_at = payload.get("expires_at")
    if expires_at is not None:
        try:
            if int(expires_at) < int(time.time() * 1000):
                raise LicenseError("Cette licence a expiré.")
        except (TypeError, ValueError):
            raise LicenseError("Champ expires_at invalide dans la licence.")

    return payload


def license_status(certificate: Optional[str]) -> dict:
    """Retourne un dict {valid, payload, error} pour l'UI sans lever d'exception.

    Pratique pour afficher un statut "✓ active" / "✗ invalide" dans l'UI sans
    avoir à try/except partout.
    """
    if not certificate:
        return {"valid": False, "error": "Aucune licence configurée.", "payload": None}
    try:
        payload = verify_license_certificate(certificate)
        return {"valid": True, "error": "", "payload": payload}
    except LicenseError as e:
        return {"valid": False, "error": str(e), "payload": None}
    except Exception as e:
        # Erreur inattendue (lib manquante, fichier illisible, etc.)
        return {"valid": False, "error": f"Erreur de vérification : {e}", "payload": None}


__all__ = [
    "LicenseError",
    "verify_license_certificate",
    "license_status",
    "get_public_key",
]
