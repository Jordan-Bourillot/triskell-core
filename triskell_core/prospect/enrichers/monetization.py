"""Détection de monétisation + extraction emails/téléphones.

Origine : extrait du monolithe Le Dénicheur (LeDenicheur.py:67-305).
Réutilisable par Le Dénicheur ET par Triskell Command pour qualifier les prospects créateurs.

Logique :
- Un profil "non monétisé" est quasi-vierge : pas de lien commercial connu,
  pas de mot-clé commercial, pas plus d'1 lien externe non-social.
- L'extraction email gère le standard ET les variantes anti-scraping
  ((at), [at], chez, point, dot...).
"""

from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# Patterns d'URL commerciales (affiliation, boutiques, hubs payants…)
# ---------------------------------------------------------------------------
MONETIZATION_URL_PATTERNS = [
    # Affiliation Amazon
    r"amzn\.to/", r"amazon\.[a-z.]{2,6}/(?:dp|gp/product|exec/obidos)/",
    # Réseaux d'affiliation
    r"awin1\.com", r"shareasale\.com", r"impact\.com",
    r"clickbank\.net", r"effiliation\.com",
    # Hubs de liens (souvent monétisés)
    r"linktr\.ee/", r"beacons\.ai/", r"stan\.store/",
    r"snipfeed\.co/", r"komi\.io/", r"bio\.link/",
    # Boutiques / merch
    r"shopify\.com", r"shop\.[\w-]+\.", r"\.myshopify\.com",
    r"etsy\.com/shop/", r"teespring\.com/", r"redbubble\.com/",
    r"spreadshirt\.", r"teepublic\.com", r"fanjoy\.co",
    # Plateformes créateurs payantes
    r"gumroad\.com/", r"ko-fi\.com/", r"patreon\.com/",
    r"onlyfans\.com/", r"buymeacoffee\.com", r"tipeee\.com",
    r"fansly\.com", r"substack\.com",
    # Cours / formations
    r"teachable\.com", r"podia\.com", r"thinkific\.com",
    r"systeme\.io", r"learnybox\.com",
    # Raccourcisseurs d'affiliation
    r"\bbit\.ly/", r"\btinyurl\.com/", r"\bcutt\.ly/",
]

# Mots-clés qui indiquent une monétisation (français + anglais)
MONETIZATION_KEYWORDS = [
    # === Français ===
    "boutique", "ma boutique", "notre boutique",
    "ma marque", "marque déposée",
    "code promo", "code reduc", "code réduction",
    "partenariat", "partenariats", "partenaire",
    "collab payée", "collab rémunérée", "collaboration payée",
    "affilié", "affiliation", "lien affilié", "liens affiliés",
    "sponsorisé", "sponsorisée", "sponsor", "sponsors", "sponsoring",
    "lien en bio", "lien dans la bio", "liens en bio",
    "mes produits", "ma collection", "ma sélection", "mes recommandations",
    "merch", "merchandising",
    "achetez", "commander", "commande", "commandez",
    "réduction", "promo", "promo en cours", "promotion",
    "ebook", "e-book", "livre numérique",
    "formation payante", "ma formation", "mes formations", "mon cours", "mes cours",
    "consulting", "consultant", "coaching individuel", "coaching personnalisé",
    "abonnement payant", "abonnement premium",
    "soutien", "soutenir", "soutenez",
    "ma masterclass", "masterclass payante",
    "produit phare", "best seller",
    "vente", "à vendre", "en vente",
    "site web", "mon site", "mon site web",
    "newsletter", "ma newsletter",
    "contact pro", "contact professionnel",
    # === Anglais ===
    "shop", "my shop", "store", "my store", "online store",
    "use code", "discount code", "promo code", "coupon",
    "sponsored by", "sponsored content", "this video is sponsored",
    "affiliate", "affiliate link", "affiliate links",
    "ad partner", "brand partner", "brand partnership",
    "link in bio", "my merch", "merch store",
    "buy now", "shop now", "order now", "pre-order", "preorder",
    "my course", "my book", "my ebook", "my masterclass", "my workshop",
    "support me", "tip jar", "donate",
    "join my patreon", "subscribe to my", "exclusive content",
    "members only", "subscribers only", "patreon supporters",
    "for business", "for collaborations", "for partnerships",
    "press kit", "media kit",
    "newsletter", "join the newsletter",
    "discord premium",
]


_URL_REGEX = re.compile(
    r"https?://[^\s<>\"\']+|www\.[^\s<>\"\']+",
    re.IGNORECASE,
)
_MONETIZATION_URL_RE = re.compile(
    "|".join(MONETIZATION_URL_PATTERNS),
    re.IGNORECASE,
)

# Domaines purement sociaux : ne flag pas comme commercial
_SOCIAL_DOMAINS = {
    "instagram.com", "instagr.am",
    "twitter.com", "x.com", "t.co",
    "facebook.com", "fb.com", "fb.me", "m.facebook.com",
    "tiktok.com", "vm.tiktok.com",
    "youtube.com", "youtu.be", "m.youtube.com",
    "twitch.tv", "m.twitch.tv",
    "reddit.com", "redd.it",
    "discord.gg", "discord.com",
    "github.com",
    "linkedin.com",
    "snapchat.com", "snap.com",
    "pinterest.com", "pinterest.fr",
    "vimeo.com",
    "soundcloud.com",
    "spotify.com", "open.spotify.com",
    "deezer.com", "music.apple.com",
    "threads.net",
    "bluesky.app", "bsky.app",
    "mastodon.social",
    "telegram.me", "t.me",
    "whatsapp.com", "wa.me",
    "kick.com",
}


def is_social_url(url: str) -> bool:
    """True si l'URL pointe vers un réseau social pur (pas commercial)."""
    try:
        m = re.match(r"https?://(?:www\.)?([^/\s]+)", url, re.IGNORECASE)
        if not m:
            m = re.match(r"www\.([^/\s]+)", url, re.IGNORECASE)
        if not m:
            return False
        domain = m.group(1).lower()
        for sd in _SOCIAL_DOMAINS:
            if domain == sd or domain.endswith("." + sd):
                return True
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Extraction emails / téléphones
# ---------------------------------------------------------------------------
_EMAIL_REGEX_STANDARD = re.compile(
    r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"
)
_EMAIL_REGEX_OBFUSCATED = re.compile(
    r"\b([a-zA-Z0-9._%+\-]+)\s*[\(\[]?\s*(?:at|chez|arobase)\s*[\)\]]?\s*"
    r"([a-zA-Z0-9.\-]+)\s*[\(\[]?\s*(?:dot|point)\s*[\)\]]?\s*"
    r"([a-zA-Z]{2,})\b",
    re.IGNORECASE,
)
_PHONE_REGEX_FR = re.compile(
    r"(?:(?<!\d)(?:\+33|0033)[\s.\-]?[1-9](?:[\s.\-]?\d{2}){4}|"
    r"(?<!\d)0[1-9](?:[\s.\-]?\d{2}){4}(?!\d))"
)
_PHONE_REGEX_INTL = re.compile(
    r"(?<!\d)\+\d{1,3}[\s.\-]?\d{1,4}(?:[\s.\-]?\d{2,4}){2,4}(?!\d)"
)


def extract_contacts(text: str) -> dict:
    """Extrait emails et numéros de téléphone d'un texte."""
    if not text:
        return {"emails": [], "phones": []}

    from .email_filter import clean_email

    raw_emails = set()
    for m in _EMAIL_REGEX_STANDARD.findall(text):
        raw_emails.add(m.lower())
    for m in _EMAIL_REGEX_OBFUSCATED.findall(text):
        local, domain, tld = m
        raw_emails.add(f"{local}@{domain}.{tld}".lower())

    # Filtre central : rejette domaines plateforme/factices, www.*,
    # local-parts suspects (only/online/more/info).
    emails = set()
    for e in raw_emails:
        ce = clean_email(e)
        if ce:
            emails.add(ce)

    phones = set()
    for m in _PHONE_REGEX_FR.findall(text):
        normalized = re.sub(r"[\s.\-]", "", m)
        phones.add(normalized)
    for m in _PHONE_REGEX_INTL.findall(text):
        normalized = re.sub(r"[\s.\-]", "", m)
        if len(re.sub(r"\D", "", normalized)) >= 8:
            phones.add(normalized)

    return {
        "emails": sorted(emails)[:5],
        "phones": sorted(phones)[:3],
    }


def detect_monetization(text: str) -> dict:
    """Analyse stricte. Profil "non monétisé" = quasi-vierge.

    Renvoie :
        {
          "monetized": bool,
          "reasons": list[str],           # max 6 raisons les plus parlantes
          "urls": list[str],              # toutes URLs trouvées
          "commercial_urls": list[str],   # URLs matchant un pattern commercial
          "other_urls": list[str],        # URLs sociales (informatif)
        }
    """
    if not text:
        return {"monetized": False, "reasons": [], "urls": [],
                "commercial_urls": [], "other_urls": []}

    reasons = []
    found_urls = _URL_REGEX.findall(text)

    # 1. URL qui matche un pattern de monétisation EXPLICITE
    matched_urls = []
    for url in found_urls:
        if _MONETIZATION_URL_RE.search(url):
            matched_urls.append(url)
            reasons.append(f"Lien commercial : {url[:60]}")

    # 2. URL externe non-sociale = suspect
    other_urls = []
    suspicious_urls = []
    for url in found_urls:
        if url in matched_urls:
            continue
        if is_social_url(url):
            other_urls.append(url)
        else:
            suspicious_urls.append(url)
            reasons.append(f"Lien externe non-social : {url[:60]}")

    # 3. Mots-clés français/anglais (mot entier)
    text_lower = text.lower()
    for kw in MONETIZATION_KEYWORDS:
        pattern = r"\b" + re.escape(kw) + r"\b"
        if re.search(pattern, text_lower):
            reasons.append(f"Mot-clé : « {kw} »")

    # 4. Email pro de contact (signal de business actif)
    pro_email_pattern = (
        r"\b(?:contact|pro|business|partenariat|partnership|sponsor|"
        r"booking|management|biz|hello)@"
    )
    if re.search(pro_email_pattern, text_lower):
        reasons.append("Email pro de contact détecté")

    return {
        "monetized": bool(reasons),
        "reasons": reasons[:6],
        "urls": list(set(found_urls))[:8],
        "commercial_urls": matched_urls,
        "other_urls": other_urls[:5],
    }
