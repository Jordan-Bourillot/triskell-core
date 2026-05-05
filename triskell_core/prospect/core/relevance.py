"""Scoring de pertinence et dédoublonnage cross-plateforme.

Le moteur de recherche brut renvoie souvent des dizaines de profils dont
beaucoup sont du bruit (homonymes, comptes inactifs, faux positifs). Ce
module post-traite la liste pour :

1. **Scorer la pertinence** par rapport à la query (nom, description, tags).
2. **Filtrer les profils morts** (pas d'activité, pas de description, etc.).
3. **Dédoublonner** ce qui matche entre plateformes (même handle, même URL
   externe, même email).
4. **Trier** par pertinence puis par taille d'audience.

Le calcul est purement local — pas de réseau, pas d'IA — pour rester rapide
et déterministe.
"""

from __future__ import annotations

import math
import re
import unicodedata


# ---------------------------------------------------------------------------
# Tokenisation et normalisation
# ---------------------------------------------------------------------------
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _strip_accents(s: str) -> str:
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _tokenize(text: str) -> list[str]:
    """Liste de tokens minuscules, sans accents, sans ponctuation."""
    if not text:
        return []
    norm = _strip_accents(text).lower()
    return [t for t in _WORD_RE.findall(norm) if len(t) > 1]


def _query_terms(query: str) -> list[str]:
    """Sépare la query en termes utiles (vire les stopwords courts)."""
    return [t for t in _tokenize(query) if len(t) >= 2]


# ---------------------------------------------------------------------------
# Détection de langue ultra-simple (FR vs EN)
# ---------------------------------------------------------------------------
_FR_HINTS = frozenset({
    "le", "la", "les", "un", "une", "des", "et", "ou", "mais", "donc", "car",
    "pour", "avec", "sans", "dans", "sur", "sous", "vers", "chez", "depuis",
    "que", "qui", "quoi", "comment", "pourquoi",
    "boutique", "création", "creation", "vidéo", "video", "chaîne", "chaine",
    "français", "francais", "francaise", "francaises", "français", "fr",
    "abonnés", "abonnes", "abonné", "abonne",
})
_EN_HINTS = frozenset({
    "the", "and", "or", "but", "for", "with", "without", "from", "to",
    "channel", "video", "videos", "subscribe", "subscriber", "subscribers",
    "english", "uk", "us", "usa",
})


def detect_query_lang(query: str) -> str:
    """Renvoie 'fr', 'en' ou ''. Heuristique : compte les hints."""
    tokens = _tokenize(query)
    fr = sum(1 for t in tokens if t in _FR_HINTS)
    en = sum(1 for t in tokens if t in _EN_HINTS)
    if fr > en and fr > 0:
        return "fr"
    if en > fr and en > 0:
        return "en"
    # Heuristique de repli : caractères accentués → FR
    if any(c in query for c in "éèêëàâäîïôöùûüç"):
        return "fr"
    return ""


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def relevance_score(profile: dict, query: str, *,
                    boost_lang: str = "") -> float:
    """Score 0-100 d'un profil par rapport à la query.

    Composants (poids) :
    - Mot-clé exact dans le nom : +35
    - Mot-clé exact dans le handle : +20
    - Coverage des termes de la query dans la description : +25
    - Plateforme bonus : +5 (YT/Twitch ont par défaut des profils plus actifs)
    - Audience log10 : +0..15
    - Langue match : +5
    - Activité (description non vide, urls dans bio) : +0..5
    - Pénalités : description vide (-5), profil très récent (-2)
    """
    if not profile:
        return 0.0

    name_norm = _strip_accents(profile.get("name", "") or "").lower()
    handle_norm = _strip_accents(profile.get("handle", "") or "").lower()
    desc_norm = _strip_accents(profile.get("description", "") or "").lower()
    desc_tokens = set(_tokenize(profile.get("description", "") or ""))
    name_tokens = set(_tokenize(profile.get("name", "") or ""))
    handle_tokens = set(_tokenize(profile.get("handle", "") or ""))

    terms = _query_terms(query)
    if not terms:
        return 0.0
    terms_set = set(terms)

    score = 0.0

    # Match nom
    name_overlap = len(terms_set & name_tokens)
    if name_overlap == len(terms_set):
        score += 35
    elif name_overlap > 0:
        score += 20 * (name_overlap / len(terms_set))
    elif any(t in name_norm for t in terms):
        score += 12

    # Match handle
    handle_overlap = len(terms_set & handle_tokens)
    if handle_overlap > 0:
        score += 15 * (handle_overlap / len(terms_set))
    elif any(t in handle_norm for t in terms):
        score += 6

    # Match description
    desc_overlap = len(terms_set & desc_tokens)
    if desc_overlap > 0:
        score += 25 * (desc_overlap / len(terms_set))
    elif any(t in desc_norm for t in terms):
        score += 5

    # Plateforme bonus
    platform = (profile.get("platform") or "").lower()
    if platform in ("youtube", "twitch", "apple_podcasts"):
        score += 5
    elif platform in ("bluesky", "mastodon", "kick", "dailymotion", "github"):
        score += 3

    # Audience (log10) — borné à 15 points
    subs = profile.get("subscribers")
    if isinstance(subs, (int, float)) and subs > 0:
        # 100 → 4.6 ; 1k → 6 ; 10k → 8 ; 100k → 10 ; 1M → 12 ; 10M → 14
        score += min(15.0, 2.0 * math.log10(max(1, float(subs))))

    # Langue
    if boost_lang:
        plang = (profile.get("language") or "").lower()[:2]
        if plang and plang == boost_lang:
            score += 5
        elif boost_lang == "fr" and (profile.get("country") or "").upper() == "FR":
            score += 5

    # Signal d'activité (description nourrie, urls détectées)
    desc_len = len(profile.get("description", "") or "")
    if desc_len > 200:
        score += 5
    elif desc_len > 50:
        score += 2
    if profile.get("urls_in_bio"):
        score += 3
    if profile.get("emails"):
        score += 5  # email déjà extrait → contact direct possible

    # Pénalités
    if desc_len == 0:
        score -= 5

    return max(0.0, min(100.0, score))


def annotate_relevance(profiles: list[dict], query: str,
                       boost_lang: str = "") -> list[dict]:
    """Ajoute le champ `relevance_score` et `relevance_label` à chaque profil."""
    if not boost_lang:
        boost_lang = detect_query_lang(query)
    for p in profiles:
        s = relevance_score(p, query, boost_lang=boost_lang)
        p["relevance_score"] = round(s, 1)
        if s >= 65:
            p["relevance_label"] = "très pertinent"
        elif s >= 45:
            p["relevance_label"] = "pertinent"
        elif s >= 25:
            p["relevance_label"] = "moyen"
        else:
            p["relevance_label"] = "faible"
    return profiles


def sort_by_relevance(profiles: list[dict]) -> list[dict]:
    """Tri stable : score décroissant, puis subs décroissant, puis nom asc."""
    def key(p: dict) -> tuple:
        return (
            -float(p.get("relevance_score") or 0),
            -int(p.get("subscribers") or 0),
            (p.get("name") or "").lower(),
        )
    return sorted(profiles, key=key)


# ---------------------------------------------------------------------------
# Dédoublonnage cross-plateforme
# ---------------------------------------------------------------------------
def _norm_handle(h: str) -> str:
    if not h:
        return ""
    return _strip_accents(h).lower().lstrip("@").strip()


def _ext_keys(profile: dict) -> set[str]:
    """Clés de matching cross-plateforme : email, handle homonyme, urls externes."""
    keys: set[str] = set()
    for e in (profile.get("emails") or []):
        if e:
            keys.add("email:" + e.lower())
    h = _norm_handle(profile.get("handle") or "")
    if h and len(h) >= 4:
        keys.add("h:" + h)
    # URLs externes (non-plateforme) — typiquement le site personnel
    for u in (profile.get("urls_in_bio") or []):
        if not u:
            continue
        u_low = u.lower()
        if any(host in u_low for host in (
            "youtube.com", "twitch.tv", "reddit.com", "bsky.app", "mastodon",
            "kick.com", "dailymotion.com", "github.com", "apple.com",
            "podcasts.apple.com",
        )):
            continue
        # On dédup sur l'host pour éviter le bruit (un même profil partage
        # souvent son site personnel sur toutes ses plateformes)
        m = re.match(r"https?://(?:www\.)?([^/\s]+)", u_low)
        if m:
            keys.add("host:" + m.group(1))
    # Site web direct
    site = (profile.get("website") or "").lower()
    m = re.match(r"https?://(?:www\.)?([^/\s]+)", site)
    if m:
        keys.add("host:" + m.group(1))
    return keys


def deduplicate_cross_platform(profiles: list[dict]) -> list[dict]:
    """Fusionne les profils détectés sur plusieurs plateformes.

    Stratégie : on parcourt les profils dans l'ordre. Pour chaque profil, on
    teste si l'une de ses `_ext_keys` matche déjà un profil précédent. Si oui,
    on agrège (on garde le profil avec le meilleur score, on ajoute la 2nde
    plateforme dans `also_on`).
    """
    out: list[dict] = []
    seen: dict[str, dict] = {}  # key → profil principal

    for p in profiles:
        keys = _ext_keys(p)
        # Trouve le 1er profil principal qui partage une clé
        principal = None
        for k in keys:
            if k in seen:
                principal = seen[k]
                break
        if principal is None:
            # Nouveau prospect : on l'enregistre
            for k in keys:
                seen[k] = p
            out.append(p)
            continue

        # Fusion : on garde le profil avec le meilleur score, on note l'autre
        better, worse = (
            (principal, p)
            if (principal.get("relevance_score") or 0) >= (p.get("relevance_score") or 0)
            else (p, principal)
        )
        # Si on switch le principal, il faut maintenir la liste de sortie
        if better is p and worse is principal:
            try:
                idx = out.index(principal)
                out[idx] = p
            except ValueError:
                out.append(p)
            for k in keys:
                seen[k] = p
            principal = p

        # Note l'autre plateforme
        also = better.setdefault("also_on", [])
        entry = {
            "platform": worse.get("platform"),
            "name":     worse.get("name"),
            "url":      worse.get("url"),
            "handle":   worse.get("handle"),
            "subs":     worse.get("subscribers"),
        }
        if entry not in also:
            also.append(entry)
        # Fusionne emails et phones (les deux profils peuvent en avoir des
        # complémentaires)
        for src_key in ("emails", "phones"):
            for v in (worse.get(src_key) or []):
                better.setdefault(src_key, [])
                if v not in better[src_key]:
                    better[src_key].append(v)
        # Si l'autre profil a des urls dans la bio que le principal n'a pas
        for u in (worse.get("urls_in_bio") or []):
            better.setdefault("urls_in_bio", [])
            if u not in better["urls_in_bio"]:
                better["urls_in_bio"].append(u)
        # Garde la nouvelle clé pour bloquer les futurs doublons
        for k in keys:
            seen.setdefault(k, better)
    return out
