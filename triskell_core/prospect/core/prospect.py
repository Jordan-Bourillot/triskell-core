"""
Schéma unifié de prospect — partagé par toutes les sources et tous les enrichers.

Conception :
- Aucune dépendance externe, dataclass standard.
- Identité = (norm_email) > (norm_phone) > (norm_website) > (source|source_id).
  La 1re clé non-vide gagne, ce qui permet le dédoublonnage cross-source :
  un même prospect trouvé sur YouTube ET sur Sirene fusionne s'il partage email/site.
- Les listes (emails, phones, urls, tags, sources, history) sont mergées par union.
- Les champs scalaires (name, address, monetized…) sont écrasés par la version
  la plus récente non-vide.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


# ---------------------------------------------------------------------------
# Normalisation pour le matching
# ---------------------------------------------------------------------------
_EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$")
_TRAILING_SLASH_RE = re.compile(r"/+$")
_WWW_RE = re.compile(r"^https?://(?:www\.)?", re.IGNORECASE)


def norm_email(email: str | None) -> str:
    if not email:
        return ""
    e = email.strip().lower()
    return e if _EMAIL_RE.match(e) else ""


def norm_phone(phone: str | None) -> str:
    """Normalise FR : 0612345678 → +33612345678."""
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return ""
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 10 and digits.startswith("0"):
        digits = "33" + digits[1:]
    if len(digits) < 8:
        return ""
    return "+" + digits


def norm_website(url: str | None) -> str:
    """Normalise une URL : minuscule, sans www., sans / final, sans query/fragment."""
    if not url:
        return ""
    u = url.strip().lower()
    u = _WWW_RE.sub("", u)
    # coupe query et fragment
    u = u.split("?", 1)[0].split("#", 1)[0]
    u = _TRAILING_SLASH_RE.sub("", u)
    return u


# Adresse FR « 12 Rue X, 35000 Rennes[, France] » → (code_postal, ville).
_FR_POSTAL_CITY_RE = re.compile(
    r"\b(\d{5})\s+([^,]+?)(?:\s*,\s*France)?\s*$", re.IGNORECASE)


def split_fr_address(address: str | None) -> tuple[str, str]:
    """Déduit (code_postal, ville) d'une adresse à la française.

    Ex : '65 Rue de Paris, 35000 Rennes' → ('35000', 'Rennes').
    La ville Google Maps arrive collée au code postal en fin d'adresse ;
    on la récupère pour ne plus laisser le champ ville vide (sinon les mails
    de prospection sortent avec « … à , … » faute de ville). ('', '') si
    rien d'exploitable.
    """
    if not address:
        return "", ""
    s = address.strip()
    m = _FR_POSTAL_CITY_RE.search(s)
    if not m:
        # Ville pas en toute fin (suivie d'autre chose) : on prend ce qui suit
        # le code postal jusqu'à la prochaine virgule.
        m = re.search(r"\b(\d{5})\s+([^,]+)", s)
    if not m:
        return "", ""
    cp = m.group(1)
    city = re.sub(r"\s+CEDEX\b.*$", "", m.group(2).strip(),
                  flags=re.IGNORECASE).strip()
    # Communes listées avec l'article inversé : « Chapelle des Fougeretz (La) »
    # (Google tronque parfois en « (La ») ou « Mans (Le) » → on remet l'article
    # devant : « La Chapelle des Fougeretz », « Le Mans », « L'Île-Rousse ».
    ma = re.match(r"^(.+?)\s*\(\s*(le|la|les|l['’])\s*\)?\s*$", city,
                  flags=re.IGNORECASE)
    if ma:
        nom = ma.group(1).strip()
        art = ma.group(2).strip().capitalize()
        city = (art + nom) if art.endswith(("'", "’")) else (art + " " + nom)
    return cp, city


# ---------------------------------------------------------------------------
# Source — métadonnée d'origine d'une donnée
# ---------------------------------------------------------------------------
@dataclass
class Source:
    """D'où vient cette donnée."""
    name: str            # "denicheur", "sirene", "maps", "web", "linktree", "footprint"
    source_id: str = ""  # ID natif de la source (channel_id YouTube, SIREN, place_id…)
    url: str = ""        # URL d'origine si pertinent
    found_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


# ---------------------------------------------------------------------------
# Prospect — entité unifiée
# ---------------------------------------------------------------------------
@dataclass
class Prospect:
    # Identité
    name: str = ""
    handle: str = ""               # @pseudo / customUrl / etc.
    legal_name: str = ""           # raison sociale (Sirene)
    siren: str = ""

    # Contact
    emails: list[str] = field(default_factory=list)
    # Métadonnées par email : d'où vient CHAQUE adresse (page contact du site,
    # mentions légales, profil YouTube, fiche Google Maps, etc.). Une entrée
    # par email connu. Schéma de chaque dict :
    #   {
    #     "email": str,        # l'adresse telle quelle (clé de jointure)
    #     "source": str,       # nom de la source qui l'a trouvée
    #                          # ("web", "obelisk", "maps", "sirene", "file"…)
    #     "source_id": str,    # optionnel : ID natif (URL page, channel_id…)
    #     "url": str,          # optionnel : URL d'origine si pertinent
    #     "context": str,      # libellé humain ("page mentions légales"…)
    #     "found_at": str,     # ISO timestamp
    #   }
    # Peut être plus court que `emails` (entrées legacy sans meta) — dans ce
    # cas, le code applicatif retombe sur la source globale du prospect.
    emails_meta: list[dict[str, str]] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    website: str = ""
    other_urls: list[str] = field(default_factory=list)
    address: str = ""
    city: str = ""
    postal_code: str = ""
    country: str = ""

    # Activité / catégorie
    industry: str = ""            # libellé NAF, niche YouTube…
    naf_code: str = ""
    description: str = ""
    language: str = ""

    # Signal commercial
    monetized: bool = False
    monetization_reasons: list[str] = field(default_factory=list)
    has_legal_mentions: bool = False  # si on a fetch /mentions-legales sur le site
    score: int = 0
    score_label: str = ""

    # Stats plateforme (créateurs)
    subscribers: int | None = None
    platform_url: str = ""

    # CRM
    status: str = "new"           # new / qualified / contacted / replied / refused / won / lost
    tags: list[str] = field(default_factory=list)
    notes: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)
    last_contact_at: str = ""
    # Suivi prospection « créateurs » (démarchage par réseaux sociaux) —
    # ajoutés en 2026-06 (migration SQL 51). Optionnels, défauts vides :
    # les prospects existants restent intacts.
    contact_channel: str = ""     # instagram / tiktok / youtube / facebook / email / autre
    next_follow_up_at: str = ""   # date de prochaine relance réglée à la main (ISO), "" = aucune
    demo_url: str = ""            # lien de la démo construite pour ce créateur
    # Drafts IA en attente de validation utilisateur (mode SAS)
    # Chaque draft : {ts, subject, body, template_key, provider, model, kind}
    pending_drafts: list[dict[str, Any]] = field(default_factory=list)

    # Provenance
    sources: list[Source] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def __post_init__(self) -> None:
        # Filet anti « ville manquante » : si on a une adresse à la française
        # mais pas la ville (cas des fiches Google Maps, qui arrivent avec
        # l'adresse complète mais sans ville isolée), on déduit ville + code
        # postal depuis l'adresse. Évite qu'un mail parte avec « … à , … ».
        if self.address and (not self.city or not self.postal_code):
            cp, city = split_fr_address(self.address)
            if city and not self.city:
                self.city = city
            if cp and not self.postal_code:
                self.postal_code = cp

    # ---------- Identité pour dédoublonnage ----------
    @property
    def match_keys(self) -> list[str]:
        """Toutes les clés exploitables pour dédoublonnage, par priorité."""
        keys = []
        for e in self.emails:
            ne = norm_email(e)
            if ne:
                keys.append("email:" + ne)
        for ph in self.phones:
            np = norm_phone(ph)
            if np:
                keys.append("phone:" + np)
        if self.siren:
            keys.append("siren:" + self.siren.strip())
        nw = norm_website(self.website)
        if nw:
            keys.append("web:" + nw)
        for s in self.sources:
            if s.source_id:
                keys.append(f"src:{s.name}:{s.source_id}")
        return keys

    # ---------- Emails : ajout avec source ----------
    def add_email(self, email: str, *, source: str = "",
                  source_id: str = "", url: str = "",
                  context: str = "", found_at: str = "") -> bool:
        """Ajoute un email + déclare sa provenance.

        Idempotent : si l'email est déjà connu, on enrichit la meta existante
        (on ne crée pas de doublon). Renvoie True si l'email était nouveau.
        """
        ne = norm_email(email)
        if not ne:
            return False
        ts = found_at or datetime.now().isoformat(timespec="seconds")
        meta_entry = {
            "email": email.strip(),
            "source": (source or "").strip().lower(),
            "source_id": (source_id or "").strip(),
            "url": (url or "").strip(),
            "context": (context or "").strip(),
            "found_at": ts,
        }
        # Email déjà connu ? On enrichit la meta si elle n'a pas déjà de source.
        existing_idx = -1
        for i, e in enumerate(self.emails):
            if norm_email(e) == ne:
                existing_idx = i
                break
        if existing_idx >= 0:
            # Cherche une meta existante pour cet email
            for m in self.emails_meta:
                if norm_email(m.get("email", "")) == ne:
                    # On ne remplace pas une source déjà renseignée — la
                    # première source qui a trouvé l'email garde le crédit.
                    if not (m.get("source") or "").strip():
                        m.update(meta_entry)
                    return False
            # Meta absente pour cet email connu : on l'ajoute
            self.emails_meta.append(meta_entry)
            return False
        # Nouvel email : on l'ajoute aux 2 listes
        self.emails.append(email.strip())
        self.emails_meta.append(meta_entry)
        return True

    def source_of_email(self, email: str) -> dict | None:
        """Retourne la meta de provenance d'un email, ou None si inconnue."""
        ne = norm_email(email)
        if not ne:
            return None
        for m in self.emails_meta:
            if norm_email(m.get("email", "")) == ne:
                return dict(m)
        return None

    # ---------- Merge ----------
    def merge(self, other: "Prospect") -> "Prospect":
        """Fusionne `other` dans self. Modifie self en place et le renvoie."""
        # Listes : union ordonnée
        self.emails = _merge_list(self.emails, other.emails, key=norm_email)
        # emails_meta : union par email normalisé ; conserve la 1ère source
        seen_meta = {norm_email(m.get("email", ""))
                     for m in self.emails_meta
                     if norm_email(m.get("email", ""))}
        for m in (other.emails_meta or []):
            ne = norm_email(m.get("email", ""))
            if ne and ne not in seen_meta:
                self.emails_meta.append(dict(m))
                seen_meta.add(ne)
        self.phones = _merge_list(self.phones, other.phones, key=norm_phone)
        self.other_urls = _merge_list(self.other_urls, other.other_urls, key=norm_website)
        self.tags = _merge_list(self.tags, other.tags)
        self.monetization_reasons = _merge_list(
            self.monetization_reasons, other.monetization_reasons
        )
        self.history.extend(other.history)
        self.pending_drafts.extend(other.pending_drafts)
        # Sources : union par (name, source_id)
        seen_src = {(s.name, s.source_id) for s in self.sources}
        for s in other.sources:
            if (s.name, s.source_id) not in seen_src:
                self.sources.append(s)
                seen_src.add((s.name, s.source_id))
        # Scalaires : si vide chez self ET non vide chez other, on prend other
        for f in (
            "name", "handle", "legal_name", "siren", "website", "address",
            "city", "postal_code", "country", "industry", "naf_code",
            "description", "language", "platform_url", "score_label",
        ):
            if not getattr(self, f) and getattr(other, f):
                setattr(self, f, getattr(other, f))
        # Booléens : OR (si l'un dit oui, on garde oui)
        self.monetized = self.monetized or other.monetized
        self.has_legal_mentions = self.has_legal_mentions or other.has_legal_mentions
        # Score : on garde le plus élevé (signal le plus fort observé)
        if other.score > self.score:
            self.score = other.score
            self.score_label = other.score_label or self.score_label
        # Subs : on garde le plus élevé
        if other.subscribers is not None:
            if self.subscribers is None or other.subscribers > self.subscribers:
                self.subscribers = other.subscribers
        # Status : ne jamais régresser un statut « avancé » par un import
        if _status_rank(other.status) > _status_rank(self.status):
            self.status = other.status
        # Suivi par fiche que la fusion oubliait — bug du 06/07/2026 : au 1er
        # envoi auto, 25 fiches sur 35 sont restées last_contact_at NULL alors
        # que status passait bien à « contacted ». Cause : le cache du CRM
        # partagé expire (600 s) ; l'upsert fusionne alors la fiche FRAÎCHE de
        # la base (last_contact_at vide) avec l'objet d'envoi, et sérialise la
        # fiche fraîche. Le status remontait via le rang ci-dessus, mais
        # last_contact_at (absent de merge) était écrasé à NULL. On reporte
        # donc explicitement l'horodatage de contact et l'échéance de relance.
        # Garde-fou : uniquement si l'entrant est renseigné → la déduplication
        # de recherche (prospect fraîchement scrappé, sans date) ne peut
        # JAMAIS effacer une vraie date déjà en base.
        _other_lca = getattr(other, "last_contact_at", "") or ""
        if _other_lca and _other_lca > (self.last_contact_at or ""):
            self.last_contact_at = _other_lca
        _other_nfu = getattr(other, "next_follow_up_at", "") or ""
        if _other_nfu:
            self.next_follow_up_at = _other_nfu
        for _f in ("contact_channel", "demo_url"):
            if not getattr(self, _f, "") and getattr(other, _f, ""):
                setattr(self, _f, getattr(other, _f))
        self.updated_at = datetime.now().isoformat(timespec="seconds")
        return self

    # ---------- Sérialisation ----------
    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Prospect":
        sources = [Source(**s) for s in d.get("sources", [])]
        d2 = dict(d)
        d2["sources"] = sources
        # emails_meta : sanitize en list[dict] (tolère les formats anciens)
        raw_meta = d.get("emails_meta") or []
        clean_meta: list[dict] = []
        if isinstance(raw_meta, list):
            for m in raw_meta:
                if isinstance(m, dict) and m.get("email"):
                    clean_meta.append({
                        "email":     str(m.get("email") or ""),
                        "source":    str(m.get("source") or "").lower(),
                        "source_id": str(m.get("source_id") or ""),
                        "url":       str(m.get("url") or ""),
                        "context":   str(m.get("context") or ""),
                        "found_at":  str(m.get("found_at") or ""),
                    })
        d2["emails_meta"] = clean_meta
        # tolérance aux champs absents
        valid = {f for f in cls.__dataclass_fields__}
        d2 = {k: v for k, v in d2.items() if k in valid}
        return cls(**d2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_STATUS_ORDER = ["new", "qualified", "contacted", "replied", "won", "lost", "refused"]


def _status_rank(status: str) -> int:
    try:
        return _STATUS_ORDER.index(status)
    except ValueError:
        return -1


def _merge_list(a: list[str], b: list[str], key=None) -> list[str]:
    """Union ordonnée, dédoublonnée par `key` (ou par valeur si key=None)."""
    out = list(a)
    seen = {(key(x) if key else x) for x in a if (key(x) if key else x)}
    for x in b:
        k = key(x) if key else x
        if k and k not in seen:
            out.append(x)
            seen.add(k)
    return out
