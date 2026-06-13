"""Pipeline nocturne pour Le Dénicheur — créateurs YT/Twitch/Reddit.

Enchaîne sans intervention :
    1. Search (YouTube + Twitch + Reddit + Bluesky + Mastodon + Apple Podcasts
       + Dailymotion + Kick + GitHub selon ce qui est activé)
    2. Enrichissement web (suit le linktree/site externe → email pro)
    3. Génération IA d'un mail personnalisé pour chaque prospect avec email
    4. Mode AUTO       → envoi SMTP direct
       Mode VALIDATION → draft en attente

Lit la config depuis ~/.ledenicheur/autopilot.json.
Persiste les prospects dans ~/.ledenicheur/prospects.json (CRM Le Dénicheur).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .core.crm import CONFIG_FILE
from .enrichers.email_filter import (
    filter_emails, guess_email_from_url, is_platform_domain, normalize_domain)
from .enrichers.linktree import LinktreeFollower, is_hub
from .enrichers.monetization import is_social_url
from .enrichers.web import WebEnricher
from .sources import creators
from .sources.bluesky import BlueskyAPI
from .sources.dailymotion import DailymotionAPI
from .sources.github import GitHubAPI
from .sources.kick import KickAPI
from .sources.mastodon import MastodonAPI
from .sources.openstreetmap import OSMAPI
from .sources.podcasts import ApplePodcastsAPI
from .sources.pubmed import PubMedAPI
from .sources.pypi import PyPIAPI
from .sources.reddit import RedditAPI
from .sources.twitch import TwitchAPI
from .sources.youtube import YouTubeAPI

logger = logging.getLogger(__name__)


DENICHEUR_DIR = Path.home() / ".ledenicheur"
AUTOPILOT_CONFIG = DENICHEUR_DIR / "autopilot.json"
PROSPECTS_FILE = DENICHEUR_DIR / "prospects.json"
NIGHTLY_LOG = DENICHEUR_DIR / "nightly.log"

MODE_AUTO = "auto"
MODE_VALIDATION = "validation"


@dataclass
class AutopilotConfig:
    enabled: bool = False
    mode: str = MODE_VALIDATION  # auto | validation
    niche: str = ""               # mot-clé de recherche
    platforms: list[str] = field(default_factory=lambda: ["youtube", "reddit"])
    max_per_platform: int = 30
    only_unmonetized: bool = True
    enrich_web: bool = True
    daily_cap: int = 20

    # Si True, l'étape 3 (génération IA des mails) est sautée. Triskell Command
    # s'en sert pour ne faire que collecte + enrichissement + upload dans la
    # liste prospects, sans écrire de brouillons. L'envoi/draft se gère à la
    # main depuis l'UI Triskell ensuite.
    skip_ai: bool = False

    # Biais géographique / linguistique sur la recherche YouTube
    # (et autres sources qui supportent ces params). Vide = mondial.
    # Ex : search_lang='fr', search_region='FR' pour cibler la France.
    search_lang: str = ""
    search_region: str = ""

    # OpenStreetMap : zone géographique (commune, département, pays) + catégorie.
    # Ex : osm_area="Rennes", osm_category="restaurants"
    # Catégories possibles : voir openstreetmap.CATEGORIES.
    osm_area: str = "France"
    osm_category: str = "restaurants"

    # IA
    ai_provider: str = "google"
    ai_model: str = "gemini-2.5-flash"
    # Défauts adaptés au pipeline auto-pilote :
    #   16 - Exécution stricte (override les comportements de challenge des autres prompts)
    #   06 - Anti-slop (style humain, pas de vocabulaire LLM)
    #   07 - Densité d'information (chaque phrase apporte une info nouvelle)
    # /!\ NE PAS mettre 01 (Honnêteté brutale) ni 13 (Mode produit/business) par défaut :
    #     ils provoquent des refus d'exécution dans un pipeline non-supervisé.
    ai_mega_prompts: list[str] = field(default_factory=lambda: ["16", "06", "07"])

    # Catalogue d'offres (texte libre multi-lignes décrivant les produits/services
    # de l'utilisateur). Injecté dans le contexte IA pour que les mails ne soient
    # plus génériques. L'IA choisit dans ce catalogue le produit le plus pertinent
    # pour chaque prospect — sauf si product_override est rempli (ci-dessous).
    catalog: str = ""

    # Force un produit précis pour TOUS les drafts du run en cours. Si vide,
    # l'IA choisit librement dans le catalogue. Format libre (ex : "Alpha Beast").
    product_override: str = ""
    ai_template_brief: str = (
        "TÂCHE : produire UN email de prospection pour proposer une collaboration à ce créateur.\n"
        "\n"
        "RÈGLES STRICTES :\n"
        "- Tu écris l'email directement. Aucun méta-commentaire, aucun verdict, aucune question.\n"
        "- Si une info te manque (ce que vend exactement l'expéditeur, etc.), tu inventes plausible et continues.\n"
        "- Tu NE refuses PAS au motif que la demande serait prématurée ou mal cadrée.\n"
        "- Tu NE produis PAS de liste de questions à l'utilisateur.\n"
        "- Court (≤ 12 lignes), tutoiement chaleureux mais pro. Objet personnalisé.\n"
        "- Pas d'emoji, pas de jargon, pas de structure 'Not just X but Y'.\n"
        "- Les produits du catalogue sont DÉJÀ EN VENTE. Ne dis JAMAIS 'nous développons' "
        "ou 'en cours de développement'. Présente-les comme disponibles dès maintenant.\n"
        "\n"
        "FORMAT DE SORTIE STRICT (rien d'autre, surtout pas de 'Voici l'email :' en préambule) :\n"
        "OBJET : <objet>\n"
        "\n"
        "<corps>\n"
        "\n"
        "Cordialement,\n"
        "{mon_prenom}"
    )

    @classmethod
    def load(cls) -> "AutopilotConfig":
        if not AUTOPILOT_CONFIG.exists():
            return cls()
        try:
            data = json.loads(AUTOPILOT_CONFIG.read_text(encoding="utf-8"))
        except Exception:
            return cls()
        valid = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in valid}
        return cls(**clean)

    def save(self) -> None:
        DENICHEUR_DIR.mkdir(parents=True, exist_ok=True)
        AUTOPILOT_CONFIG.write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _load_denicheur_config() -> dict:
    """Charge le config.json de Le Dénicheur (clés API)."""
    cfg_file = DENICHEUR_DIR / "config.json"
    if not cfg_file.exists():
        return {}
    try:
        return json.loads(cfg_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_denicheur_prospects() -> list[dict]:
    if not PROSPECTS_FILE.exists():
        return []
    try:
        data = json.loads(PROSPECTS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_denicheur_prospects(prospects: list[dict]) -> None:
    DENICHEUR_DIR.mkdir(parents=True, exist_ok=True)
    PROSPECTS_FILE.write_text(
        json.dumps(prospects, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _prospect_key(p: dict) -> str:
    return f"{p.get('platform', '')}|{p.get('id', '')}"


def _infer_standard_emails(url: str) -> list[str]:
    """Devine UN email pro à partir de l'URL du site d'un prospect.

    Délègue au filtre central (`enrichers.email_filter`) qui :
    - bloque les domaines de plateformes (skool, tiktok, base44, bit.ly...),
    - aplatit les sous-domaines exotiques (links.foo.com → foo.com),
    - vérifie le MX du domaine avant d'inventer un mail dessus.

    Retourne une liste 0 ou 1 élément (jamais 3) pour éviter les bounces.
    """
    guess = guess_email_from_url(url)
    return [guess] if guess else []


def _url_host_is_platform(url: str) -> bool:
    """Vrai si l'URL pointe vers une plateforme / raccourcisseur / hébergeur
    (donc PAS le vrai site du créateur) — ex. un lien d'affiliation amzn.to
    laissé dans la bio. Évite de deviner contact@amzn.to ou d'enregistrer un
    lien affilié comme « site ». (Observé en vrai le 13/06/2026.)"""
    if not url:
        return True
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url if url.startswith("http") else "https://" + url)
        host = parsed.netloc or parsed.path
    except Exception:
        return False
    return is_platform_domain(normalize_domain(host))


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------
def run_creators_pipeline(
    cfg: AutopilotConfig | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Exécute le pipeline complet pour Le Dénicheur."""
    cfg = cfg or AutopilotConfig.load()
    log = progress or (lambda _msg: None)
    stats = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "found": 0, "enriched": 0, "drafts": 0, "sent": 0,
        "errors": [],
    }
    if not cfg.niche:
        log("⚠ Pas de niche définie — abandonné.")
        return stats

    denicheur_cfg = _load_denicheur_config()
    existing = _load_denicheur_prospects()
    existing_keys = {_prospect_key(p) for p in existing}

    # ---- Étape 1 : Search ----
    log(f"Étape 1/4 — Recherche '{cfg.niche}' sur {cfg.platforms}…")
    new_prospects: list[dict] = []
    if "youtube" in cfg.platforms:
        try:
            yt_key = denicheur_cfg.get("youtube_api_key", "")
            if yt_key:
                api = YouTubeAPI(yt_key, denicheur_cfg.get("youtube_api_keys") or [])
                ids = api.search_channels(
                    cfg.niche,
                    max_results=cfg.max_per_platform,
                    relevance_language=cfg.search_lang or "",
                    region_code=cfg.search_region or "",
                )
                raw_list = api.get_channels_details(ids)
                for raw in raw_list:
                    if cfg.only_unmonetized:
                        from .enrichers.monetization import detect_monetization
                        if detect_monetization(raw.get("description", "") or "").get("monetized"):
                            continue
                    new_prospects.append(raw)
                log(f"  YouTube : +{len(raw_list)} bruts")
            else:
                log("  YouTube : clé API manquante, skippé")
        except Exception as e:
            stats["errors"].append(f"youtube: {e}")
    if "twitch" in cfg.platforms:
        try:
            tid = denicheur_cfg.get("twitch_client_id", "")
            tsec = denicheur_cfg.get("twitch_client_secret", "")
            if tid and tsec:
                api = TwitchAPI(tid, tsec)
                raw_list = api.search_channels(cfg.niche, max_results=cfg.max_per_platform)
                raw_list = api.enrich_with_user_info(raw_list)
                new_prospects.extend(raw_list)
                log(f"  Twitch : +{len(raw_list)} bruts")
        except Exception as e:
            stats["errors"].append(f"twitch: {e}")
    if "reddit" in cfg.platforms:
        try:
            api = RedditAPI()
            sr = api.search_subreddits(cfg.niche, max_results=cfg.max_per_platform // 2)
            us = api.search_users(cfg.niche, max_results=cfg.max_per_platform // 2)
            new_prospects.extend(sr + us)
            log(f"  Reddit : +{len(sr) + len(us)} bruts")
        except Exception as e:
            stats["errors"].append(f"reddit: {e}")

    if "bluesky" in cfg.platforms:
        try:
            api = BlueskyAPI()
            actors = api.search_actors(cfg.niche, max_results=cfg.max_per_platform)
            new_prospects.extend(actors)
            log(f"  Bluesky : +{len(actors)} bruts")
        except Exception as e:
            stats["errors"].append(f"bluesky: {e}")

    if "mastodon" in cfg.platforms:
        try:
            api = MastodonAPI(
                extra_instances=denicheur_cfg.get("mastodon_instances") or [])
            accounts = api.search_accounts(cfg.niche, max_results=cfg.max_per_platform)
            new_prospects.extend(accounts)
            log(f"  Mastodon : +{len(accounts)} bruts")
        except Exception as e:
            stats["errors"].append(f"mastodon: {e}")

    if "apple_podcasts" in cfg.platforms:
        try:
            api = ApplePodcastsAPI(
                country=denicheur_cfg.get("apple_podcasts_country") or "FR",
                lang=denicheur_cfg.get("apple_podcasts_lang") or "fr_fr",
            )
            podcasts = api.search_podcasts(
                cfg.niche, max_results=cfg.max_per_platform,
                enrich_with_feed=True,
                feed_enrich_limit=min(20, cfg.max_per_platform),
            )
            new_prospects.extend(podcasts)
            log(f"  Apple Podcasts : +{len(podcasts)} bruts")
        except Exception as e:
            stats["errors"].append(f"apple_podcasts: {e}")

    if "dailymotion" in cfg.platforms:
        try:
            api = DailymotionAPI()
            users = api.search_users(cfg.niche, max_results=cfg.max_per_platform)
            new_prospects.extend(users)
            log(f"  Dailymotion : +{len(users)} bruts")
        except Exception as e:
            stats["errors"].append(f"dailymotion: {e}")

    if "kick" in cfg.platforms:
        try:
            api = KickAPI()
            channels = api.search_channels(
                cfg.niche, max_results=cfg.max_per_platform,
                enrich_details=True,
                enrich_limit=min(15, cfg.max_per_platform),
            )
            new_prospects.extend(channels)
            log(f"  Kick : +{len(channels)} bruts")
        except Exception as e:
            stats["errors"].append(f"kick: {e}")

    if "github" in cfg.platforms:
        try:
            api = GitHubAPI(denicheur_cfg.get("github_token", ""))
            users = api.search_users(
                cfg.niche, max_results=cfg.max_per_platform,
                in_bio=True, enrich_details=True,
                enrich_limit=min(30, cfg.max_per_platform),
            )
            new_prospects.extend(users)
            log(f"  GitHub : +{len(users)} bruts")
        except Exception as e:
            stats["errors"].append(f"github: {e}")

    if "pypi" in cfg.platforms:
        try:
            api = PyPIAPI()
            packages = api.search_packages(
                cfg.niche, max_results=cfg.max_per_platform,
                enrich_limit=min(30, cfg.max_per_platform),
            )
            new_prospects.extend(packages)
            log(f"  PyPI : +{len(packages)} bruts")
        except Exception as e:
            stats["errors"].append(f"pypi: {e}")

    if "osm" in cfg.platforms:
        try:
            api = OSMAPI()
            pois = api.search_local(
                category=cfg.osm_category,
                area=cfg.osm_area,
                max_results=cfg.max_per_platform,
                require_email=True,
            )
            new_prospects.extend(pois)
            log(f"  OpenStreetMap ({cfg.osm_category}@{cfg.osm_area}) : +{len(pois)} bruts")
        except Exception as e:
            stats["errors"].append(f"osm: {e}")

    if "pubmed" in cfg.platforms:
        try:
            api = PubMedAPI(denicheur_cfg.get("pubmed_api_key", ""))
            papers = api.search_papers(
                cfg.niche, max_results=cfg.max_per_platform,
            )
            new_prospects.extend(papers)
            log(f"  PubMed : +{len(papers)} bruts")
        except Exception as e:
            stats["errors"].append(f"pubmed: {e}")

    # Filtre les déjà-connus
    fresh = [p for p in new_prospects if _prospect_key(p) not in existing_keys]
    log(f"  → {len(fresh)} nouveaux après dédup")

    # ⚡ Scrape la page /about de chaque chaîne YouTube nouvelle pour
    # récupérer les liens externes (site perso, Instagram, Linktree…) et
    # l'email contact (rare mais possible) — infos absentes de l'API Data v3.
    # Indispensable pour que l'étape 2 (enrich web) ait un site à crawler
    # et trouve un email, sinon le filtre `only_with_email` rejette tout.
    # Fait APRÈS le dédup pour ne pas gaspiller du temps sur des chaînes
    # déjà connues. Parallélisé (15 chaînes en même temps).
    yt_fresh = [p for p in fresh if (p.get("platform") or "").lower() == "youtube"]
    if yt_fresh:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        log(f"  YouTube : scrape /about de {len(yt_fresh)} nouvelle(s) chaîne(s)…")

        def _scrape_one(raw):
            try:
                return raw, YouTubeAPI.scrape_about_page(
                    channel_id=raw.get("id"),
                    handle=raw.get("handle") or "",
                )
            except Exception:
                return raw, {"emails": [], "external_links": []}

        scraped_with_email = 0
        scraped_with_links = 0
        with ThreadPoolExecutor(max_workers=15) as pool:
            futures = [pool.submit(_scrape_one, p) for p in yt_fresh]
            for fut in as_completed(futures):
                try:
                    raw, about = fut.result()
                except Exception:
                    continue
                if about.get("emails"):
                    raw["emails"] = about["emails"]
                    scraped_with_email += 1
                if about.get("external_links"):
                    raw["urls_in_bio"] = about["external_links"]
                    scraped_with_links += 1
        log(f"  YouTube : scrape /about → "
            f"{scraped_with_email} email(s), {scraped_with_links} avec liens externes")

    # Enrichit déjà avec monétisation et contacts (l'app Le Dénicheur le fait
    # normalement dans run_search, mais ici on saute, alors on le fait à la main)
    from .enrichers.monetization import detect_monetization, extract_contacts
    for p in fresh:
        desc = p.get("description", "") or ""
        det = detect_monetization(desc)
        p["monetized"] = det["monetized"]
        p["monetization_reasons"] = det["reasons"]
        # Ne PAS écraser urls_in_bio/emails/phones si déjà remplis par le
        # scrape /about YouTube juste au-dessus — sinon on perd les liens
        # externes et l'email contact, ce qui fait que l'étape 2 d'enrich
        # web ne trouve rien et que le filtre `only_with_email` rejette tout.
        contacts = extract_contacts(desc)
        if not p.get("urls_in_bio"):
            p["urls_in_bio"] = det["urls"]
        if not p.get("emails"):
            p["emails"] = contacts["emails"]
        if not p.get("phones"):
            p["phones"] = contacts["phones"]
        p["found_at"] = datetime.now().isoformat(timespec="seconds")
        p["status"] = "new"
        p["saved_at"] = datetime.now().isoformat(timespec="seconds")

    existing.extend(fresh)
    _save_denicheur_prospects(existing)
    stats["found"] = len(fresh)

    # ---- Étape 2 : Enrich web ----
    if cfg.enrich_web:
        log(f"Étape 2/4 — Enrichissement web…")
        web = WebEnricher()
        linktree = LinktreeFollower(web_enricher=web)
        n_enriched = 0
        n_inferred = 0
        for p in existing:
            if p.get("emails"):
                continue
            urls = p.get("urls_in_bio") or []
            url = next((u for u in urls
                        if u and not is_social_url(u)
                        and not _url_host_is_platform(u)), "")
            if not url:
                continue
            try:
                if is_hub(url):
                    data = linktree.enrich_hub(url)
                else:
                    data = web.enrich_url(url)
                scraped = filter_emails(list(data.get("emails") or []))
                if scraped:
                    p["emails"] = scraped[:3]
                    # Tag la provenance : ces emails viennent du site web
                    # lié dans la bio (pas du profil créateur lui-même).
                    src_label = "linktree" if is_hub(url) else "web"
                    ctx_label = ("hub Linktree / Beacons / etc."
                                  if src_label == "linktree"
                                  else "page contact ou mentions légales du site officiel")
                    p["emails_meta"] = [
                        {"email": e, "source": src_label, "source_id": "",
                         "url": url, "context": ctx_label, "found_at": ""}
                        for e in scraped[:3]
                    ]
                    n_enriched += 1
                else:
                    inferred = _infer_standard_emails(url)
                    if inferred:
                        p["emails"] = inferred
                        p["emails_inferred"] = True
                        # Email "inféré" = deviné depuis le domaine (contact@<site>).
                        # Source = "web_inferred" pour que l'IA sache que
                        # l'adresse n'a pas été VUE sur la page, juste devinée.
                        p["emails_meta"] = [
                            {"email": e, "source": "web_inferred", "source_id": "",
                             "url": url,
                             "context": "adresse devinée à partir du domaine du site",
                             "found_at": ""}
                            for e in inferred
                        ]
                        n_inferred += 1
                if data.get("phones"):
                    p["phones"] = list(data["phones"])[:3]
                p["web_enriched_at"] = datetime.now().isoformat(timespec="seconds")
                p["web_enriched_url"] = url
            except Exception as e:
                logger.debug("enrich %s : %s", p.get("name", ""), e)
        _save_denicheur_prospects(existing)
        stats["enriched"] = n_enriched
        stats["inferred_emails"] = n_inferred
        log(f"  → {n_enriched} prospect(s) avec email récupéré "
            f"(+ {n_inferred} avec email inféré standard)")

    # ---- Étape 3 : Génération IA + envoi/draft ----
    if cfg.skip_ai:
        log("Étape 3/4 — IA sautée (mode collecte/enrichissement seul).")
        stats["finished_at"] = datetime.now().isoformat(timespec="seconds")
        return stats
    log(f"Étape 3/4 — IA + envoi (mode {cfg.mode})…")
    api_keys = (denicheur_cfg.get("ai_api_keys") or {})
    if not api_keys.get(cfg.ai_provider):
        log(f"  ⚠ Clé {cfg.ai_provider} manquante — étape skippée.")
        return stats

    from ..ai.builder import build_ultimate_prompt
    from ..ai.library import load_packaged_library
    from ..ai.providers import send_to_provider, ProviderError

    library = load_packaged_library()
    selected_megas = [mp for mp in library if mp.get("id") in cfg.ai_mega_prompts]
    # Prénom : la modale Réglages le sauve sous "user_name", l'ancienne config
    # outreach utilisait "outreach.mon_prenom". On accepte les deux pour
    # compatibilité ascendante, en priorisant la valeur la plus récente.
    mon_prenom = (
        (denicheur_cfg.get("outreach", {}) or {}).get("mon_prenom", "")
        or denicheur_cfg.get("user_name", "")
        or ""
    )

    # Exclut les "comptes groupe" (subreddits, communautés) : on ne s'adresse pas
    # à un groupe entier comme on s'adresserait à un créateur individuel.
    def _is_group_account(p: dict) -> bool:
        name = (p.get("name") or "").strip().lower()
        handle = (p.get("handle") or "").strip().lower()
        url = (p.get("platform_url") or "").lower()
        if name.startswith("r/") or handle.startswith("r/") or handle.startswith("/r/"):
            return True
        if "reddit.com/r/" in url and "reddit.com/u/" not in url and "reddit.com/user/" not in url:
            return True
        return False

    eligible = [
        p for p in existing
        if p.get("emails")
        and p.get("status") in ("new", "qualified")
        and not p.get("pending_draft")
        and not any(h.get("kind") == "email_sent" for h in (p.get("history") or []))
        and not _is_group_account(p)
    ][:cfg.daily_cap]

    n_skipped_groups = sum(1 for p in existing if _is_group_account(p) and p.get("emails"))
    if n_skipped_groups:
        log(f"  → {n_skipped_groups} compte(s) groupe (subreddits) ignoré(s)")
    log(f"  → {len(eligible)} candidat(s) éligible(s)")

    # Bloc catalogue / offre à pitcher : on l'assemble une fois
    catalog_block_lines: list[str] = []
    if cfg.product_override.strip():
        # Mode override : on impose un produit précis
        catalog_block_lines = [
            "OFFRE À PITCHER OBLIGATOIREMENT :",
            cfg.product_override.strip(),
            "",
            "Tu DOIS pitcher exclusivement cette offre. Tu n'inventes pas d'autre produit.",
            "Si tu manques d'info sur l'offre, tu restes générique sur ce produit précis "
            "plutôt que d'inventer un autre nom.",
        ]
    elif cfg.catalog.strip():
        # Mode catalogue : on laisse l'IA choisir, avec une stratégie de
        # match prioritaire sur les templates métier (« Site Template
        # Électricien », etc.) si applicable.
        catalog_block_lines = [
            "MON CATALOGUE D'OFFRES (l'expéditeur Jordan vend ces produits/services) :",
            cfg.catalog.strip(),
            "",
            "INSTRUCTIONS DE CHOIX D'OFFRE :",
            "1. Regarde si le catalogue contient un TEMPLATE MÉTIER qui correspond "
            "exactement au métier/secteur du prospect (ex: prospect = électricien → "
            "« Site Template Électricien »). Si oui, pitche-le précisément.",
            "2. Sinon, cherche un produit du catalogue qui peut RAISONNABLEMENT "
            "convenir à ce prospect (par audience, secteur, taille). Adapte ton "
            "pitch pour expliquer pourquoi ce produit est pertinent pour LUI.",
            "3. Si VRAIMENT rien dans le catalogue ne colle, propose un produit "
            "générique du catalogue (le plus large) en restant subtil — pas de "
            "vente forcée.",
            "",
            "Tu pitches TOUJOURS un produit avec son nom EXACT tel qu'il apparaît "
            "dans le catalogue. N'invente JAMAIS un produit absent du catalogue.",
        ]
    # else : pas de catalogue → comportement historique (l'IA peut inventer)

    for p in eligible:
        ctx_parts = [
            "PROSPECT À CONTACTER :",
            f"- Créateur : {p.get('name', '')}",
            f"- Plateforme : {p.get('platform', '')}",
            f"- Abonnés : {p.get('subscribers', '?')}",
            f"- Description : {(p.get('description', '') or '')[:400]}",
            "",
            f"MON PRÉNOM : {mon_prenom or '(non renseigné)'}",
        ]
        if catalog_block_lines:
            ctx_parts.extend(["", *catalog_block_lines])
        ctx_parts.extend(["", "CONSIGNES :", cfg.ai_template_brief])
        ctx = "\n".join(ctx_parts)
        full = build_ultimate_prompt(ctx, selected_megas)
        try:
            response = send_to_provider(cfg.ai_provider, cfg.ai_model, full, api_keys)
        except (ProviderError, Exception) as e:
            stats["errors"].append(f"ai {p.get('name', '')[:20]}: {e}")
            continue

        # Parse OBJET : ...
        import re as _re
        m = _re.search(r"^\s*(?:OBJET|SUBJECT|Objet)\s*[:：]\s*(.+?)\s*$",
                       response, _re.MULTILINE | _re.IGNORECASE)
        if m:
            subject = m.group(1).strip()
            body = response[m.end():].lstrip()
        else:
            subject = f"Une idée pour {p.get('name', 'vous')}"
            body = response

        if cfg.mode == MODE_AUTO:
            # Envoi direct
            try:
                from .outreach.smtp_sender import (
                    _load_smtp_config, prospection_headers, send_email,
                )
                smtp_cfg = _load_smtp_config()
                msg_id = send_email(
                    smtp_cfg, to=p["emails"][0],
                    subject=subject, body=body,
                    custom_headers=prospection_headers(
                        smtp_cfg.get("from_email", "")),
                )
                p.setdefault("history", []).append({
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "kind": "email_sent",
                    "to": p["emails"][0],
                    "subject": subject,
                    "message_id": msg_id,
                    "from_autopilot": True,
                })
                p["status"] = "contacted"
                p["last_contact_at"] = datetime.now().isoformat(timespec="seconds")
                stats["sent"] += 1
            except Exception as e:
                stats["errors"].append(f"send {p.get('name', '')[:20]}: {e}")
        else:
            # Mode validation : draft en attente
            p["pending_draft"] = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "subject": subject,
                "body": body,
                "to": p["emails"][0],
                "provider": cfg.ai_provider,
                "model": cfg.ai_model,
            }
            stats["drafts"] += 1

    _save_denicheur_prospects(existing)
    log(f"  → {stats['sent']} envoyé(s), {stats['drafts']} en attente")

    # ---- Stats finales ----
    stats["finished_at"] = datetime.now().isoformat(timespec="seconds")
    return stats


def run_nightly() -> dict:
    """Entrypoint pour Windows Task Scheduler."""
    DENICHEUR_DIR.mkdir(parents=True, exist_ok=True)
    log_lines: list[str] = []

    def emit(msg: str) -> None:
        line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
        log_lines.append(line)
        try:
            print(line)
        except UnicodeEncodeError:
            pass

    emit("=== Le Dénicheur — nightly run ===")
    cfg = AutopilotConfig.load()
    if not cfg.enabled:
        emit("Auto-pilote désactivé. Run avorté.")
        _flush(log_lines)
        return {"skipped": True}

    stats = run_creators_pipeline(cfg, progress=emit)
    emit(f"=== Fin : {stats['found']} trouvés, {stats['enriched']} enrichis, "
         f"{stats['sent']} envoyés, {stats['drafts']} en attente, "
         f"{len(stats['errors'])} erreurs ===")
    _flush(log_lines)
    return stats


def _flush(lines: list[str]) -> None:
    try:
        with NIGHTLY_LOG.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    import sys
    sys.exit(0 if run_nightly() else 1)
