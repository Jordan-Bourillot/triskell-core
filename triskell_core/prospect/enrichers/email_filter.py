"""Filtre et devine les emails pros à partir d'un domaine de site.

Objectifs :
- Ne JAMAIS générer `contact@<plateforme>` (skool, tiktok, base44, bit.ly...).
- Aplatir les sous-domaines exotiques (links.foo.com → foo.com).
- Vérifier que le domaine accepte du mail (MX ou A record).
- Retourner UN SEUL email par prospect (au lieu de 3) pour éviter les bounces.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Domaines qui appartiennent à des plateformes — pas au prospect.
# Si le « site web » d'un créateur pointe vers un de ces domaines, c'est qu'il
# n'a pas de site propre. Inventer contact@<ce_domaine> = écrire au support
# de la plateforme, jamais au créateur.
PLATFORM_DOMAINS: frozenset[str] = frozenset({
    # Réseaux sociaux / vidéo
    "tiktok.com", "instagram.com", "facebook.com", "fb.com",
    "x.com", "twitter.com", "t.me", "telegram.me",
    "youtube.com", "youtu.be", "twitch.tv", "kick.com", "dailymotion.com",
    "bsky.app", "mastodon.social", "threads.net",
    # Cours / contenu payant
    "skool.com", "medium.com", "substack.com", "patreon.com",
    "podia.com", "kajabi.com", "teachable.com", "thinkific.com",
    "gumroad.com", "ko-fi.com", "buymeacoffee.com", "tipeee.com",
    # No-code / bio links
    "base44.app", "linktr.ee", "bio.link", "carrd.co", "beacons.ai",
    "linkin.bio", "later.com", "lnk.bio", "milkshake.app",
    # Raccourcisseurs de liens : un « site » qui pointe vers un raccourci
    # n'est pas le domaine du prospect. Inventer contact@<raccourci> écrit au
    # service du raccourcisseur. (contact@amzn.to aspiré en vrai sur un
    # youtubeur le 13/06/2026 : le lien affilié Amazon pris pour son site.)
    "bit.ly", "tinyurl.com", "rb.gy", "is.gd",
    "cutt.ly", "shorturl.at", "rebrand.ly", "ow.ly", "buff.ly",
    "t.co", "goo.gl", "dlvr.it", "ift.tt", "snip.ly", "lnkd.in",
    "amzn.to", "amzn.eu", "amzn.asia", "a.co",
    "wa.me", "discord.gg", "fb.me", "fb.watch",
    # Marketplaces / liens d'affiliation
    "envato.market", "amazon.com", "amazon.fr", "amazon.de", "amazon.co.uk",
    "etsy.com",
    # Google ecosystem
    "plus.google.com", "google.com", "forms.gle",
    "docs.google.com", "sites.google.com", "blogspot.com",
    # Outils RDV / forms
    "cal.com", "calendly.com", "tidycal.com", "savvycal.com",
    "typeform.com",
    # Hébergeurs / builders sans email pro
    "github.com", "github.io", "notion.site", "notion.so",
    "wordpress.com", "wix.com", "wixsite.com", "weebly.com",
    "squarespace.com", "shopify.com", "webflow.io", "vercel.app",
    "netlify.app", "fly.dev",
    # Hébergeurs : leur mail (support@ovhcloud.com…) traîne sur les pages
    # d'erreur/parking des sites clients — jamais le mail du prospect.
    # (Observé en vrai le 10/06/2026 sur un plombier de Saint-Erblon.)
    "ovhcloud.com", "ovh.com", "ovh.net", "gandi.net",
    "ionos.fr", "ionos.com", "1and1.com", "1und1.de",
    "o2switch.fr", "infomaniak.com", "infomaniak.ch",
    "hostinger.com", "hostinger.fr",
    # Hébergeurs / registrars supplémentaires : leur mail commercial
    # (sales@scalahosting.com, support@godaddy.com…) traîne dans le pied de
    # page ou les pages de parking des sites clients — jamais le mail du
    # prospect. (sales@scalahosting.com aspiré sur une boulangerie, 13/06/2026.)
    "scalahosting.com", "godaddy.com", "hostgator.com", "bluehost.com",
    "siteground.com", "dreamhost.com", "namecheap.com", "name.com",
    "a2hosting.com", "hostpapa.com", "kinsta.com", "wpengine.com",
    "cloudways.com", "planethoster.net", "planethoster.com",
    "lws.fr", "amen.fr", "nuxit.com", "hosteur.com", "ex2.com",
    "hetzner.com", "contabo.com", "scaleway.com", "digitalocean.com",
    # Marketplaces / réseaux qui hébergent des mini-sites de commerçants :
    # le mail contact@<marketplace> est celui du support, PAS du commerçant.
    "sessile.fr", "florajet.com", "interflora.fr", "aquarelle.com",
    # Vente / parking de domaines : quand le site d'un prospect est expiré,
    # la page de parking affiche le mail de la régie (service@atom.com,
    # info@sedo.com…) — jamais celui du prospect. Observé en vrai le
    # 10/06/2026 : deux brouillons d'autopilote visaient ces adresses.
    "atom.com", "squadhelp.com", "sedo.com", "dan.com", "afternic.com",
    "hugedomains.com", "buydomains.com", "domainmarket.com",
    "brandbucket.com", "parkingcrew.net", "bodis.com", "above.com",
    # Pollution observée dans la donnée
    "aaa.com", "savagex.com", "gobble.com",
    "nom-de-domaine.com", "votresite.com", "monsite.com",
})

# Domaines factices/placeholders qui ne peuvent jamais recevoir de mail réel.
# Souvent issus de fragments d'URL parsés à tort comme email, ou de
# placeholders laissés dans des templates de site.
FAKE_DOMAINS: frozenset[str] = frozenset({
    "aaa.com", "bbb.com", "ccc.com", "xxx.com",
    "example.com", "example.fr", "example.org", "example.net",
    "exemple.com", "exemple.fr",
    "domain.com", "test.com", "email.com", "mail.com",
    "gobble.com", "savagex.com",
    "yourcompany.com", "yoursite.com", "yourbusiness.com",
    "votre-email.com", "votresite.com", "monsite.com",
    "nom-de-domaine.com",
    # Placeholders de templates de sites (observé en vrai le 10/06/2026 :
    # « votre@adressemail.com » sur le site d'un coiffeur de Vannes).
    "adressemail.com", "adresse-email.com", "adresseemail.com",
    "votremail.com", "monemail.com", "emailaddress.com",
    # Placeholder universel des pages de parking de domaines
    # (« inquire@webname.com » affiché sur tout domaine en vente).
    "webname.com", "yourdomain.com", "your-domain.com",
    "yourwebsite.com", "mywebsite.com", "mysite.com", "placeholder.com",
    "votredomaine.com", "votre-domaine.com", "votredomaine.fr",
    "mondomaine.com", "mon-domaine.com", "mondomaine.fr",
    # Placeholder archi-classique « abc@xyz.com » (audit du 13/06/2026).
    "xyz.com",
    # Médiateurs de la consommation : leurs adresses traînent dans les
    # mentions légales de tous les sites marchands — JAMAIS des prospects
    # (« litiges@cm2c.net » aspiré en vrai le 13/06/2026).
    "cm2c.net", "medicys.fr", "mediation-conso.fr",
    "cnpm-mediation-consommation.eu",
})

# Local-parts qui sont JAMAIS des emails légitimes : presque toujours
# des fragments de texte/URL ("only" / "online" / "more"...) parsés à tort.
SUSPICIOUS_LOCAL_PARTS: frozenset[str] = frozenset({
    "only", "online", "more",
    # Placeholders : « votre@… » / « your@… » ne sont jamais de vrais comptes.
    "votre", "your",
    # Adresses techniques : on ne prospecte jamais ces boîtes (audit 13/06).
    "no-reply", "noreply", "postmaster", "mailer-daemon",
})

# Local-parts AMBIGUS : légitimes en soi (info@vraie-boite.com existe), mais
# louches quand combinés avec un domaine factice ou un préfixe www.
# Ex : "more info on www.xxx.com" → regex naïve qui sort "info@www.xxx.com".
AMBIGUOUS_LOCAL_PARTS: frozenset[str] = frozenset({
    "info",
})

# Préfixes de sous-domaines à aplatir vers le domaine principal.
# Ex: links.isao.io → isao.io (les mails sont sur le domaine racine,
# rarement sur un sous-domaine type linkinbio).
FLATTEN_PREFIXES: frozenset[str] = frozenset({
    "links", "link", "linkin", "lnk", "bio",
    "social", "socials",
    "offre", "offres", "promo", "promos",
    "formation", "formations", "cours",
    "shop", "store", "boutique",
    "blog", "news",
    "page", "pages",
    "www",
})

# Préfixes locaux préférés (plus petit = mieux).
# Un mail nominal (claude.bailly@...) est toujours meilleur qu'un générique.
_PRIORITY: dict[str, int] = {
    "contact": 20,
    "sales": 25,
    "info": 30,
    "accueil": 35,
    "press": 70,
    "hello": 80,
    "support": 90,
    "help": 90,
    "support-technique": 95,
    "booking": 100,
    "noreply": 999,
    "no-reply": 999,
    "donotreply": 999,
}


def _email_priority(email: str) -> int:
    local = email.split("@", 1)[0].lower()
    if local in _PRIORITY:
        return _PRIORITY[local]
    return 10  # mail nominal — toujours préféré


def normalize_domain(domain: str) -> str:
    """Met le domaine en minuscule, retire www., aplatit 1 niveau de sous-domaine
    si le préfixe est dans FLATTEN_PREFIXES."""
    if not domain:
        return ""
    d = domain.lower().strip().lstrip(".")
    if d.startswith("www."):
        d = d[4:]
    parts = d.split(".")
    if len(parts) >= 3 and parts[0] in FLATTEN_PREFIXES:
        d = ".".join(parts[1:])
    return d


def is_platform_domain(domain: str) -> bool:
    """Vrai si le domaine appartient à une plateforme connue (et donc pas
    au prospect lui-même). Match exact ou suffixe (sub.skool.com → True)."""
    if not domain:
        return True
    d = domain.lower().lstrip(".")
    if d.startswith("www."):
        d = d[4:]
    if d in PLATFORM_DOMAINS:
        return True
    return any(d.endswith("." + p) for p in PLATFORM_DOMAINS)


# Cache MX en mémoire process pour éviter de re-questionner le DNS.
_mx_cache: dict[str, bool] = {}


def has_mail_record(domain: str, timeout: float = 3.0) -> bool:
    """Vérifie que le domaine peut recevoir du mail.

    Tente MX d'abord, fallback sur A record (certains domaines servent du mail
    sans MX dédié). Retourne True si quoi que ce soit répond.

    Si dnspython n'est pas installé, on accepte le domaine par défaut (mieux
    vaut un faux positif qu'un blocage total).
    """
    if not domain:
        return False
    if domain in _mx_cache:
        return _mx_cache[domain]
    try:
        import dns.resolver
    except ImportError:
        logger.debug("dnspython non installé — MX check skippé pour %s", domain)
        return True

    try:
        resolver = dns.resolver.Resolver()
        resolver.timeout = timeout
        resolver.lifetime = timeout + 1.0
        try:
            answers = resolver.resolve(domain, "MX")
            ok = len(list(answers)) > 0
        except Exception:
            # Fallback A record
            try:
                resolver.resolve(domain, "A")
                ok = True
            except Exception:
                ok = False
    except Exception as e:
        logger.debug("MX check failed for %s : %s", domain, e)
        ok = False

    _mx_cache[domain] = ok
    return ok


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


# Terminaisons (.com, .fr…) plausibles. Sans cette vérification, la regex
# accepte n'importe quelle longueur de terminaison → des emails collés à du
# texte passaient (ex réel observé sur un fleuriste de Rennes :
# "lebazarapetales@gmail.comwebmaster", le mot "webmaster" soudé au .com).
# Règle : 2-3 lettres = toujours OK (couvre TOUS les codes pays + com/net/
# org/biz/pro/app/dev/xyz…) ; 4 lettres et + = uniquement si dans la liste
# blanche ci-dessous. "comwebmaster" n'y est pas → rejeté.
KNOWN_LONG_TLDS: frozenset[str] = frozenset({
    # gTLDs génériques courants
    "info", "name", "mobi", "aero", "jobs", "coop", "asia", "post",
    "shop", "site", "club", "blog", "wine", "live", "life", "love",
    "care", "team", "city", "zone", "town", "fund", "work", "world",
    "store", "email", "earth", "group", "media", "house", "salon",
    "photo", "video", "money", "today", "click", "cloud", "space",
    "studio", "agency", "online", "center", "design", "travel",
    "photos", "coffee", "garden", "events", "social", "global",
    "digital", "marketing", "solutions", "services", "company",
    "business", "boutique", "immobilier", "restaurant", "technology",
    "photography", "enterprises", "consulting", "construction",
    # ccTLDs / géographiques longs (réels)
    "paris", "alsace", "corsica", "bretagne", "brussels", "museum",
})


def _tld_is_plausible(domain: str) -> bool:
    """Vrai si la terminaison du domaine ressemble à une vraie terminaison.

    2-3 lettres → toujours plausible (codes pays + com/net/org/biz/pro…).
    4 lettres et + → uniquement si dans KNOWN_LONG_TLDS (sinon c'est
    presque toujours du texte soudé à un vrai TLD, type "comwebmaster").
    """
    if not domain or "." not in domain:
        return False
    tld = domain.rsplit(".", 1)[-1].lower()
    if not tld.isalpha():
        return False
    if len(tld) <= 3:
        return True
    return tld in KNOWN_LONG_TLDS


def is_fake_domain(domain: str) -> bool:
    """Vrai si le domaine est un placeholder factice (jamais valide en réel)."""
    if not domain:
        return True
    d = domain.lower().lstrip(".")
    if d.startswith("www."):
        d = d[4:]
    return d in FAKE_DOMAINS


# Suffixes de domaines d'INFRASTRUCTURE : des identifiants techniques que
# les extracteurs prennent pour des emails (vu en vrai le 13/06/2026 :
# « 14d5…@o45….ingest.us.sentry.io » dans la base). Jamais des prospects.
TECHNICAL_DOMAIN_SUFFIXES: tuple[str, ...] = (
    ".sentry.io", ".amazonses.com", ".sendgrid.net", ".mailgun.org",
    ".cloudfront.net", ".herokuapp.com", ".execute-api.amazonaws.com",
)


def clean_email(email: str) -> str | None:
    """Nettoie un email :
    - normalise le domaine (lowercase, retire www., aplatit sous-domaine connu),
    - rejette si plateforme ou domaine factice,
    - rejette si local-part toujours suspect (only/online/more — fragments d'URL),
    - rejette si local-part ambigu (info) COMBINÉ avec un domaine louche,
    - rejette si domaine commence par "www." (jamais valide en vrai email),
    - rejette si format invalide.

    Retourne l'email canonique ou None.
    """
    if not email or not isinstance(email, str):
        return None
    email = email.strip().lower()
    m = _EMAIL_RE.fullmatch(email)
    if not m:
        # Tente d'extraire un email d'une chaîne polluée
        found = _EMAIL_RE.findall(email)
        if not found:
            return None
        email = found[0].lower()

    try:
        local, domain = email.split("@", 1)
    except ValueError:
        return None

    # 1. Local-part toujours bidon (fragments type "more X on …")
    if local in SUSPICIOUS_LOCAL_PARTS:
        return None

    # 2. Domaine qui commence par "www." → jamais un vrai email
    #    (un vrai MX n'est jamais sur www.exemple.com)
    if domain.startswith("www."):
        return None

    # 3. Plateformes et domaines factices
    if is_platform_domain(domain) or is_fake_domain(domain):
        return None
    norm = normalize_domain(domain)
    if is_platform_domain(norm) or is_fake_domain(norm):
        return None

    # 3bis. Terminaison invalide (texte soudé au .com, faute de frappe…) →
    # rejet. Sans ça, "x@gmail.comwebmaster" passait.
    if not _tld_is_plausible(norm):
        return None

    # 3ter. Domaines d'infrastructure (sentry, passerelles mail…) :
    # des identifiants techniques, jamais des boîtes de prospects.
    if any(norm.endswith(s) or domain.endswith(s)
           for s in TECHNICAL_DOMAIN_SUFFIXES):
        return None

    # 4. Local-part ambigu (ex: "info") légitime sauf si on a déjà un autre
    #    indice louche. Comme on est arrivé ici, le domaine est "propre" :
    #    on garde l'email. (Les cas www./fake sont déjà filtrés au-dessus.)
    #    Ce bloc est conservé pour documenter l'intention — si on étend
    #    AMBIGUOUS_LOCAL_PARTS, on saura quoi vérifier ici.

    return f"{local}@{norm}"


def filter_emails(emails: list[str], *, verify_mx: bool = True) -> list[str]:
    """Filtre une liste d'emails (scrapés ou inférés) :
    - retire les plateformes,
    - normalise les domaines,
    - vérifie le MX si demandé,
    - dédoublonne,
    - trie par priorité (nominal d'abord).
    """
    if not emails:
        return []
    cleaned = []
    seen = set()
    for e in emails:
        ce = clean_email(e) if isinstance(e, str) else None
        if ce and ce not in seen:
            seen.add(ce)
            cleaned.append(ce)
    cleaned.sort(key=_email_priority)
    if not verify_mx:
        return cleaned
    out = []
    for c in cleaned:
        domain = c.split("@", 1)[1]
        if has_mail_record(domain):
            out.append(c)
    return out


def guess_email_from_url(url: str, *, verify_mx: bool = True) -> str | None:
    """Tente de deviner UN email pro à partir de l'URL du site d'un prospect.

    Pipeline :
    1. Extrait le domaine de l'URL.
    2. Aplatit les sous-domaines exotiques (links., social., offre., ...).
    3. Refuse si plateforme connue.
    4. Vérifie MX pour confirmer que le domaine accepte du mail.
    5. Retourne `contact@<domaine>` (un seul, pas 3, pour éviter les bounces).

    Retourne None si rien d'exploitable.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url if url.startswith("http") else "https://" + url)
        host = parsed.netloc or parsed.path
    except Exception:
        return None
    if not host or "." not in host:
        return None

    norm = normalize_domain(host)
    if is_platform_domain(norm):
        return None
    if verify_mx and not has_mail_record(norm):
        return None

    return f"contact@{norm}"


__all__ = [
    "PLATFORM_DOMAINS",
    "FAKE_DOMAINS",
    "SUSPICIOUS_LOCAL_PARTS",
    "AMBIGUOUS_LOCAL_PARTS",
    "FLATTEN_PREFIXES",
    "normalize_domain",
    "is_platform_domain",
    "is_fake_domain",
    "has_mail_record",
    "clean_email",
    "filter_emails",
    "guess_email_from_url",
]
