"""
Templates de prospection — chargement depuis Triskell Sales Tunnel
ou depuis ~/.triskell-prospect/templates.json (override perso).

Format unifié : {key, channel, subject, body}
- channel : "email", "linkedin", "instagram", "whatsapp", "messenger", "twitter"
- placeholders au format `{prenom}`, `{nom_entreprise}`, etc.

Le moteur ne fait PAS la rédaction (c'est le rôle du Sales Tunnel).
Il consomme les templates et les rend pour un prospect donné.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..core.crm import APP_DIR


USER_TEMPLATES = APP_DIR / "templates.json"


# Templates par défaut (mêmes valeurs que le Sales Tunnel)
DEFAULT_TEMPLATES = {
    "tpe_intro": {
        "channel": "email",
        "subject": "Un site qui ressemble enfin à {nom_entreprise} ?",
        "body": (
            "Bonjour {prenom},\n\n"
            "Je suis {mon_prenom} de Triskell Studio, agence digitale basée en "
            "Bretagne. En jetant un œil au site de {nom_entreprise}, j'ai "
            "remarqué quelques pistes simples qui pourraient vous faire gagner "
            "en crédibilité et en clients.\n\n"
            "On accompagne plusieurs entreprises {region_secteur} avec des "
            "sites premium taillés sur mesure, pas de templates copiés-collés, "
            "juste du design qui vous ressemble.\n\n"
            "Si le sujet vous intéresse, je peux vous envoyer par mail 2 ou 3 "
            "pistes concrètes pour {nom_entreprise}, sans engagement.\n\n"
            "Bien cordialement,\n"
            "{mon_prenom}, Triskell Studio"
        ),
    },
    "tpe_relance_j5": {
        "channel": "email",
        "subject": "Re: Un site qui ressemble enfin à {nom_entreprise} ?",
        "body": (
            "Bonjour {prenom},\n\n"
            "Je me permets une petite relance suite à mon précédent message. "
            "Je sais que vous recevez sûrement beaucoup de propositions.\n\n"
            "Si le moment n'est pas le bon, aucun souci, on pourra reprendre "
            "contact plus tard. Et si vous avez la moindre question, j'y "
            "réponds volontiers par mail.\n\n"
            "Bonne journée,\n"
            "{mon_prenom}"
        ),
    },
}


def load_all() -> dict:
    """Renvoie {key: template} en mergeant defaults + override utilisateur."""
    out = dict(DEFAULT_TEMPLATES)
    if USER_TEMPLATES.exists():
        try:
            data = json.loads(USER_TEMPLATES.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out.update(data)
        except Exception:
            pass
    return out


def render(template_key: str, prospect, sender_vars: dict) -> tuple[str, str]:
    """Rend (subject, body) pour un prospect donné. Lève si template introuvable."""
    tpls = load_all()
    if template_key not in tpls:
        raise KeyError(f"Template inconnu : {template_key}. "
                       f"Disponibles : {sorted(tpls.keys())}")
    tpl = tpls[template_key]

    # Variables auto-déduites du prospect + sender_vars utilisateur
    raw_name = prospect.name or prospect.legal_name or ""
    # Nom propre : on coupe avant la 1re parenthèse (les Sirene incluent
    # souvent des variantes commerciales entre parenthèses) et on titre-case
    # si le nom est tout en majuscules (typique Sirene).
    main_name = raw_name.split("(", 1)[0].strip(" .-")
    if main_name and main_name == main_name.upper():
        # ECO.PROTECH → Eco.Protech ; SARL MOUGIN → Sarl Mougin
        main_name = " ".join(w.capitalize() for w in main_name.split())
    # "Prénom" approximatif si pas fourni explicitement
    prenom = (
        sender_vars.get("contact_prenom")
        or _guess_first_name(main_name)
        or "bonjour"
    )
    vars_ = {
        "prenom": prenom,
        "nom_entreprise": main_name,
        "region_secteur": prospect.city or prospect.industry or "votre région",
        "mon_prenom": sender_vars.get("mon_prenom", ""),
        "ma_signature": sender_vars.get("signature", ""),
    }
    # Override par sender_vars (priorité haute)
    vars_.update({k: v for k, v in sender_vars.items() if v})

    try:
        subject = tpl.get("subject", "").format(**_safe_dict(vars_))
        body = tpl.get("body", "").format(**_safe_dict(vars_))
    except KeyError as e:
        raise ValueError(f"Variable manquante dans le template {template_key}: {e}")
    return subject, body


def _guess_first_name(company_name: str) -> str:
    """Heuristique faible : le 1er mot du nom (mais sans 'SARL', 'SAS', etc.)."""
    if not company_name:
        return ""
    SKIPS = {"sarl", "sas", "eurl", "sa", "sci", "snc", "the", "le", "la", "les"}
    for tok in company_name.split():
        clean = tok.lower().strip(".,()[]")
        if clean and clean not in SKIPS and any(c.isalpha() for c in clean):
            return tok.capitalize()
    return ""


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _safe_dict(d: dict) -> _SafeDict:
    return _SafeDict(d)
