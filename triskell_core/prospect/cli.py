"""
CLI Triskell Prospection Engine.

Sous-commandes :
- import-denicheur     : importe ~/.ledenicheur/prospects.json vers le CRM unifié
- search-sirene        : cherche des entreprises FR (NAF + département + …)
- enrich               : visite les sites web des prospects et complète emails/tél
- list                 : affiche le contenu du CRM unifié
- export               : exporte le CRM en CSV

Usage :
    python -m triskell_core.prospect.cli import-denicheur
    python -m triskell_core.prospect.cli search-sirene --naf 43.21A --departement 35 --max 100
    python -m triskell_core.prospect.cli enrich --no-emails-only --max 50
    python -m triskell_core.prospect.cli list --status qualified
    python -m triskell_core.prospect.cli export --out prospects.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

from .core.crm import CONFIG_FILE, CRM, ENRICH_CACHE_DIR, ensure_dirs
from .core.prospect import Prospect, Source, norm_website
from .enrichers.footprint import FootprintFinder
from .enrichers.linktree import LinktreeFollower, is_hub
from .enrichers.web import WebEnricher
from .outreach import imap_listener, smtp_sender
from .sources import creators, denicheur, maps, sirene
from .sources.reddit import RedditAPI
from .sources.twitch import TwitchAPI
from .sources.youtube import YouTubeAPI


def cmd_import_denicheur(args) -> int:
    if not denicheur.is_available():
        print("✗ Aucun fichier ~/.ledenicheur/prospects.json trouvé.", file=sys.stderr)
        print("  Lance Le Dénicheur au moins une fois et sauvegarde des prospects.", file=sys.stderr)
        return 2
    crm = CRM()
    stats = crm.upsert_many(denicheur.import_all())
    crm.save()
    print(f"✓ Le Dénicheur : {stats['created']} créés, {stats['merged']} fusionnés. "
          f"Total CRM : {stats['total']}.")
    return 0


def cmd_search_maps(args) -> int:
    if not maps.is_configured():
        print("✗ Clé Maps Places API non configurée.", file=sys.stderr)
        print(f"  Ajoute-la avec : python -m triskell_core.prospect.cli config "
              f"--google-places-api-key TA_CLÉ", file=sys.stderr)
        return 2
    crm = CRM()
    print(f"Recherche Maps Places (q='{args.query}', max={args.max})…")
    iterator = maps.search(
        text_query=args.query,
        location_bias_lat=args.lat,
        location_bias_lng=args.lng,
        radius_m=args.radius,
        max_results=args.max,
    )
    stats = crm.upsert_many(iterator)
    crm.save()
    print(f"✓ Maps : {stats['created']} créés, {stats['merged']} fusionnés. "
          f"Total CRM : {stats['total']}.")
    return 0


def cmd_config(args) -> int:
    ensure_dirs()
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    changed = False
    if args.google_places_api_key is not None:
        cfg["google_places_api_key"] = args.google_places_api_key
        changed = True
    if args.smtp_host is not None:
        cfg["smtp_host"] = args.smtp_host
        changed = True
    if args.smtp_port is not None:
        cfg["smtp_port"] = args.smtp_port
        changed = True
    if args.smtp_user is not None:
        cfg["smtp_user"] = args.smtp_user
        changed = True
    if args.smtp_password is not None:
        cfg["smtp_password"] = args.smtp_password
        changed = True
    if args.from_email is not None:
        cfg["from_email"] = args.from_email
        changed = True
    if args.from_name is not None:
        cfg["from_name"] = args.from_name
        changed = True
    if args.imap_host is not None:
        cfg["imap_host"] = args.imap_host
        changed = True
    if args.imap_user is not None:
        cfg["imap_user"] = args.imap_user
        changed = True
    if args.imap_password is not None:
        cfg["imap_password"] = args.imap_password
        changed = True
    if args.youtube_api_key is not None:
        cfg["youtube_api_key"] = args.youtube_api_key
        changed = True
    if args.twitch_client_id is not None:
        cfg["twitch_client_id"] = args.twitch_client_id
        changed = True
    if args.twitch_client_secret is not None:
        cfg["twitch_client_secret"] = args.twitch_client_secret
        changed = True

    if changed:
        CONFIG_FILE.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"✓ Config écrite : {CONFIG_FILE}")
    if args.show:
        # Masque les secrets à l'affichage
        masked = {
            k: ("***" if any(s in k.lower() for s in ("password", "key", "secret"))
                else v)
            for k, v in cfg.items()
        }
        print(json.dumps(masked, indent=2, ensure_ascii=False))
    if not changed and not args.show:
        print("Rien à faire. Voir --help.")
    return 0


def cmd_search_creators(args) -> int:
    """Recherche créateurs YouTube / Twitch / Reddit (multi-plateformes)."""
    crm = CRM()
    cfg = _load_creator_keys()

    found_iters = []
    if "youtube" in args.platforms:
        yt_key = cfg.get("youtube_api_key", "")
        if not yt_key:
            print("⚠ YouTube API key manquante, plateforme skippée")
        else:
            api = YouTubeAPI(yt_key, cfg.get("youtube_api_keys") or [])
            found_iters.append((
                "YouTube",
                creators.search_youtube(
                    api, args.query, max_results=args.max,
                    include_monetized=args.include_monetized,
                ),
            ))
    if "twitch" in args.platforms:
        tw_id = cfg.get("twitch_client_id", "")
        tw_secret = cfg.get("twitch_client_secret", "")
        if not (tw_id and tw_secret):
            print("⚠ Twitch credentials manquants, plateforme skippée")
        else:
            api = TwitchAPI(tw_id, tw_secret)
            found_iters.append((
                "Twitch",
                creators.search_twitch(
                    api, args.query, max_results=args.max,
                    include_monetized=args.include_monetized,
                ),
            ))
    if "reddit" in args.platforms:
        api = RedditAPI()
        found_iters.append((
            "Reddit",
            creators.search_reddit(
                api, args.query, max_results=args.max,
                kind=args.reddit_kind,
                include_monetized=args.include_monetized,
            ),
        ))

    print(f"Recherche créateurs (q='{args.query}', plateformes={args.platforms})…")
    total_created = 0
    total_merged = 0
    for label, it in found_iters:
        prospects = list(it)
        stats = crm.upsert_many(prospects)
        total_created += stats["created"]
        total_merged += stats["merged"]
        print(f"  {label}: {stats['created']} créés, {stats['merged']} fusionnés "
              f"({len(prospects)} renvoyés)")
    crm.save()
    print(f"✓ Total : {total_created} créés, {total_merged} fusionnés. "
          f"CRM = {len(crm)} prospects.")
    return 0


def _load_creator_keys() -> dict:
    """Charge les clés YouTube/Twitch depuis CONFIG_FILE."""
    cfg = {}
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return cfg


def cmd_search_sirene(args) -> int:
    crm = CRM()
    print(f"Recherche Sirene (NAF={args.naf or '-'}, dpt={args.departement or '-'}, "
          f"q={args.query or '-'}, max={args.max})…")
    iterator = sirene.search(
        activite_principale=args.naf,
        departement=args.departement,
        code_postal=args.code_postal,
        nom_entreprise=args.query,
        min_date_creation=args.created_since,
        tranche_effectif=args.effectif,
        max_results=args.max,
    )
    stats = crm.upsert_many(iterator)
    crm.save()
    print(f"✓ Sirene : {stats['created']} créés, {stats['merged']} fusionnés. "
          f"Total CRM : {stats['total']}.")
    return 0


def cmd_enrich(args) -> int:
    crm = CRM()
    web = WebEnricher()
    linktree = LinktreeFollower(web_enricher=web)
    footprint = FootprintFinder() if args.footprint else None
    targets = _select_for_enrichment(crm, args)

    if not targets:
        print("Aucun prospect à enrichir avec ces filtres.")
        return 0

    print(f"Enrichissement de {len(targets)} prospect(s)…")
    counters = {"site_visited": 0, "hub_visited": 0, "footprint_found": 0,
                "new_emails": 0, "new_phones": 0, "new_addr": 0}

    for i, prospect in enumerate(targets, 1):
        url = prospect.website or _first_useful_url(prospect)
        url_was_guessed = False  # True si URL devinée par footprint et donc à valider
        # Si pas d'URL et qu'on a un nom + ville (typiquement les Sirene),
        # tente de trouver le site via DuckDuckGo / guess+HEAD.
        if not url and footprint and prospect.name and (prospect.city or prospect.legal_name):
            url = footprint.find_official_site(
                prospect.name or prospect.legal_name,
                city=prospect.city,
                country=prospect.country or "FR",
            )
            if url:
                counters["footprint_found"] += 1
                url_was_guessed = True
                # On NE persiste PAS encore prospect.website : c'est le cross-ref
                # qui décidera si on garde ou pas.
        if not url:
            continue

        if is_hub(url):
            data = linktree.enrich_hub(url)
            counters["hub_visited"] += 1
            # Si le hub a révélé une vraie destination, on la stocke comme website
            if data.get("primary_url") and not prospect.website:
                prospect.website = data["primary_url"]
        else:
            data = web.enrich_url(url)
            counters["site_visited"] += 1

            # CROSS-REF Sirene ↔ site (uniquement pertinent pour les
            # prospects avec SIREN/code postal, càd majoritairement Sirene).
            verdict = _cross_ref(prospect, data, url=url)
            if verdict == "reject":
                counters["site_rejected"] = counters.get("site_rejected", 0) + 1
                prospect.history.append({
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "kind": "site_rejected",
                    "url": url,
                    "reason": "cross_ref_mismatch",
                })
                continue

            # Si l'URL venait du footprint (devinée), on n'ose la persister
            # comme website QUE si le cross-ref est concluant (high/ok).
            # Pour "low" (site sans SIREN ni CP), on stocke avec un tag d'incertitude.
            if not prospect.website:
                if not url_was_guessed or verdict in ("high", "ok"):
                    prospect.website = url
                else:
                    # verdict "low" + URL devinée : on garde l'info mais pas comme website
                    if url not in prospect.other_urls:
                        prospect.other_urls.append(url)
            if verdict == "high":
                prospect.tags = [t for t in prospect.tags if t != "site_unverified"]
                if "site_verified" not in prospect.tags:
                    prospect.tags.append("site_verified")
            elif verdict == "ok":
                if "site_postal_match" not in prospect.tags:
                    prospect.tags.append("site_postal_match")
            elif verdict == "low" and "site_unverified" not in prospect.tags:
                prospect.tags.append("site_unverified")

        gained_emails = [e for e in data["emails"] if e.lower() not in
                         {x.lower() for x in prospect.emails}]
        gained_phones = [p for p in data["phones"] if p not in prospect.phones]
        if gained_emails:
            prospect.emails = (prospect.emails + gained_emails)[:8]
            counters["new_emails"] += len(gained_emails)
        if gained_phones:
            prospect.phones = (prospect.phones + gained_phones)[:5]
            counters["new_phones"] += len(gained_phones)
        if data["address"] and not prospect.address:
            prospect.address = data["address"]
            counters["new_addr"] += 1
        if data["has_legal_mentions"]:
            prospect.has_legal_mentions = True

        prospect.history.append({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "kind": "web_enrich",
            "url": url,
            "pages": data.get("pages_visited", []),
            "found_emails": gained_emails,
            "found_phones": gained_phones,
        })
        prospect.sources.append(Source(
            name="linktree" if is_hub(url) else "web",
            source_id=norm_website(url),
            url=url,
        ))
        prospect.updated_at = datetime.now().isoformat(timespec="seconds")

        # Re-indexe (les nouveaux emails/phones changent les match_keys)
        crm._dirty = True  # noqa: SLF001
        if i % 10 == 0:
            print(f"  …{i}/{len(targets)} traité(s)")

    # Une dernière sauvegarde + reindexation des match_keys ajoutés
    crm._rebuild_index()  # noqa: SLF001
    crm.save()
    rej = counters.get("site_rejected", 0)
    rej_str = f", {rej} rejeté(s) (cross-ref Sirene KO)" if rej else ""
    print(
        f"✓ Enrichissement terminé : "
        f"{counters['site_visited']} site(s) + {counters['hub_visited']} hub(s) "
        f"+ {counters['footprint_found']} site(s) trouvé(s) par footprint{rej_str}. "
        f"+{counters['new_emails']} email(s), +{counters['new_phones']} tél(s), "
        f"+{counters['new_addr']} adresse(s)."
    )
    return 0


def cmd_list(args) -> int:
    crm = CRM()
    rows = crm.all()
    if args.status:
        rows = [p for p in rows if p.status == args.status]
    if args.has_email:
        rows = [p for p in rows if p.emails]
    if args.source:
        rows = [p for p in rows
                if any(s.name == args.source for s in p.sources)]
    print(f"{len(rows)} prospect(s) :\n")
    for p in rows[: args.limit]:
        srcs = ",".join(sorted({s.name for s in p.sources}))
        emails = p.emails[0] if p.emails else "-"
        print(f"  [{p.status:9s}] {p.name[:50]:50s} | {emails:35s} | "
              f"src={srcs} | siren={p.siren or '-'}")
    if len(rows) > args.limit:
        print(f"  … et {len(rows) - args.limit} de plus")
    return 0


def cmd_send(args) -> int:
    sender_vars = {
        "mon_prenom": args.mon_prenom or "",
        "signature": args.signature or "",
    }
    try:
        stats = smtp_sender.run_campaign(
            template_key=args.template,
            sender_vars=sender_vars,
            daily_cap=args.daily_cap,
            pause_min_s=args.pause_min,
            pause_max_s=args.pause_max,
            only_verified=not args.allow_unverified,
            follow_up=args.follow_up,
            follow_up_days=args.follow_up_days,
            dry_run=args.dry_run,
            limit=args.max,
        )
    except smtp_sender.SmtpConfigError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    label = "(dry-run)" if args.dry_run else ""
    print(f"✓ Envoi terminé {label} : {stats}")
    return 0


def cmd_poll_replies(args) -> int:
    try:
        stats = imap_listener.poll_replies(verbose=args.verbose)
    except imap_listener.ImapConfigError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    print(f"✓ IMAP : {stats['scanned']} mail(s) scanné(s), "
          f"{stats['matched']} réponse(s) détectée(s), {stats['errors']} erreur(s).")
    return 0


def cmd_export(args) -> int:
    crm = CRM()
    rows = crm.all()
    if args.status:
        rows = [p for p in rows if p.status == args.status]

    out = Path(args.out).resolve()
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow([
            "name", "legal_name", "siren", "email", "phone", "website",
            "address", "city", "postal_code", "country", "industry",
            "naf", "subscribers", "status", "tags", "sources",
        ])
        for p in rows:
            w.writerow([
                p.name, p.legal_name, p.siren,
                p.emails[0] if p.emails else "",
                p.phones[0] if p.phones else "",
                p.website, p.address, p.city, p.postal_code, p.country,
                p.industry, p.naf_code, p.subscribers or "",
                p.status, ",".join(p.tags),
                ",".join(sorted({s.name for s in p.sources})),
            ])
    print(f"✓ Export → {out} ({len(rows)} ligne(s))")
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _first_useful_url(prospect: Prospect) -> str:
    """Trouve la 1re URL pertinente : platform_url évité, on préfère other_urls."""
    for u in prospect.other_urls:
        if u and not _is_pure_social(u):
            return u
    # Sinon on retombe sur platform_url (qui est juste la page YouTube/Twitch — moins riche)
    return ""


def _cross_ref(prospect: Prospect, web_data: dict, url: str = "") -> str:
    """
    Vérifie qu'un site fetché correspond bien au prospect Sirene.

    Politique : on ne rejette que sur PREUVE POSITIVE de mismatch.
    L'absence de signal → "low" (accept avec tag d'incertitude).

    Renvoie :
      - "high"   : SIREN trouvé sur le site = SIREN du prospect, OU
                   email du même domaine que le site + slug nom-domaine match
      - "ok"     : code postal Sirene matche un code postal trouvé sur le site
      - "low"    : aucun signal pour valider (mais aucune preuve contraire) → accept
      - "reject" : preuve positive de mismatch
    """
    from urllib.parse import urlparse
    import unicodedata

    site_sirens = set(web_data.get("sirens") or [])
    site_sirets = set(web_data.get("sirets") or [])
    site_postal = set(web_data.get("postal_codes") or [])
    site_emails = web_data.get("emails") or []

    # Pas de prospect Sirene (créateur, etc.) → pas de cross-ref possible
    if not prospect.siren and not prospect.postal_code:
        return "low"

    # 1) SIREN match — preuve forte
    if prospect.siren:
        if prospect.siren in site_sirens:
            return "high"
        if any(s.startswith(prospect.siren) for s in site_sirets):
            return "high"
        # Preuve positive de mismatch : le site affiche UN SIREN, mais pas le bon
        if site_sirens and prospect.siren not in site_sirens:
            return "reject"

    # 2) Email pro du domaine + slug-nom match → preuve forte aussi.
    #    Ex : prospect "ECO.PROTECH" + site ecoprotech.fr + email @ecoprotech.fr
    #    → c'est très probablement le bon. Aucun usurpateur n'aurait email + nom + domaine alignés.
    if url and site_emails:
        try:
            host = urlparse(
                url if url.startswith(("http://", "https://")) else "https://" + url
            ).netloc.lower().lstrip("www.")
        except Exception:
            host = ""
        if host:
            domain_emails = [e for e in site_emails if e.endswith("@" + host)]
            if domain_emails:
                # Slug "principal" du nom : avant 1re parenthèse, nettoyé.
                def _main_slug(s: str) -> str:
                    if not s:
                        return ""
                    base = s.split("(", 1)[0]
                    nfkd = unicodedata.normalize("NFKD", base)
                    no_accent = "".join(c for c in nfkd if not unicodedata.combining(c))
                    return re.sub(r"[^a-z0-9]", "", no_accent.lower())

                name_slug = _main_slug(prospect.name) or _main_slug(prospect.legal_name)
                host_slug = _main_slug(host.split(".")[0])
                if name_slug and host_slug:
                    if name_slug == host_slug:
                        return "high"
                    # Ratio des longueurs (le plus court / le plus long).
                    # Doit être ≥ 0.7 pour qu'un "in" soit jugé fiable :
                    # ça élimine "protec" matchant "protecelectronic".
                    a, b = sorted([len(name_slug), len(host_slug)])
                    ratio = a / b if b else 0
                    if ratio >= 0.7 and (name_slug in host_slug or host_slug in name_slug):
                        return "high"

    # 3) Code postal
    if prospect.postal_code and site_postal:
        if prospect.postal_code in site_postal:
            return "ok"
        dept_prospect = prospect.postal_code[:2]
        if any(cp.startswith(dept_prospect) for cp in site_postal):
            return "low"
        return "reject"

    return "low"


def _is_pure_social(url: str) -> bool:
    from urllib.parse import urlparse
    try:
        host = urlparse(
            url if url.startswith(("http://", "https://")) else "https://" + url
        ).netloc.lower()
    except Exception:
        return False
    pure = ("youtube.com", "youtu.be", "twitch.tv", "reddit.com",
            "x.com", "twitter.com", "facebook.com", "instagram.com",
            "tiktok.com", "linkedin.com")
    return any(host == d or host.endswith("." + d) for d in pure)


def _select_for_enrichment(crm: CRM, args) -> list[Prospect]:
    candidates = crm.all()
    if args.no_emails_only:
        candidates = [p for p in candidates if not p.emails]
    if args.source:
        candidates = [p for p in candidates
                      if any(s.name == args.source for s in p.sources)]
    if args.status:
        candidates = [p for p in candidates if p.status == args.status]
    # Sans footprint : on n'enrichit que ceux qui ont une URL hors social.
    # Avec footprint : on accepte aussi ceux qui ont juste un nom + ville
    # (typiquement les Sirene).
    if args.footprint:
        candidates = [p for p in candidates
                      if p.website
                      or any(not _is_pure_social(u) for u in p.other_urls)
                      or (p.name and p.city)]
    else:
        candidates = [p for p in candidates
                      if p.website or any(not _is_pure_social(u) for u in p.other_urls)]
    return candidates[: args.max]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="triskell_core.prospect.cli",
        description="Triskell Prospection Engine — moteur multi-sources.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("import-denicheur",
                   help="Importer ~/.ledenicheur/prospects.json")

    s_creators = sub.add_parser(
        "search-creators",
        help="Chercher des créateurs YouTube / Twitch / Reddit",
    )
    s_creators.add_argument("--query", required=True,
                            help="Niche / mot-clé (ex: 'café spécialité')")
    s_creators.add_argument("--platforms", nargs="+",
                            default=["youtube", "twitch", "reddit"],
                            choices=["youtube", "twitch", "reddit"])
    s_creators.add_argument("--max", type=int, default=50,
                            help="Max résultats par plateforme")
    s_creators.add_argument("--include-monetized", action="store_true",
                            help="Inclure les créateurs déjà monétisés "
                                 "(par défaut on les exclut)")
    s_creators.add_argument("--reddit-kind", default="both",
                            choices=["both", "subreddit", "user"])

    s_sirene = sub.add_parser("search-sirene",
                              help="Chercher des entreprises FR via Sirene")
    s_sirene.add_argument("--naf", default="",
                          help="Code NAF (ex: 43.21A pour électricien)")
    s_sirene.add_argument("--departement", default="",
                          help="Numéro de département (ex: 35)")
    s_sirene.add_argument("--code-postal", default="")
    s_sirene.add_argument("--query", default="",
                          help="Mot-clé libre dans le nom de l'entreprise")
    s_sirene.add_argument("--created-since", default="",
                          help="Date YYYY-MM-DD : ne garde que les entreprises "
                               "créées après cette date")
    s_sirene.add_argument("--effectif", default="",
                          help="Tranche effectif salarié (00=0, 01=1-2…)")
    s_sirene.add_argument("--max", type=int, default=200)

    s_maps = sub.add_parser("search-maps",
                            help="Chercher des commerces/TPE via Google Maps Places")
    s_maps.add_argument("--query", required=True,
                        help="Requête textuelle (ex: 'boulangerie Rennes')")
    s_maps.add_argument("--lat", type=float, default=None,
                        help="Biais géographique : latitude (centre du cercle)")
    s_maps.add_argument("--lng", type=float, default=None,
                        help="Biais géographique : longitude")
    s_maps.add_argument("--radius", type=int, default=50_000,
                        help="Rayon en mètres pour le biais géographique")
    s_maps.add_argument("--max", type=int, default=60,
                        help="Maps cap à 60 par requête")

    s_cfg = sub.add_parser("config", help="Configurer les clés et secrets")
    s_cfg.add_argument("--show", action="store_true", help="Afficher la config (secrets masqués)")
    s_cfg.add_argument("--google-places-api-key", default=None)
    s_cfg.add_argument("--smtp-host", default=None)
    s_cfg.add_argument("--smtp-port", type=int, default=None)
    s_cfg.add_argument("--smtp-user", default=None)
    s_cfg.add_argument("--smtp-password", default=None,
                       help="Mot de passe d'application Gmail (16 chars sans espace)")
    s_cfg.add_argument("--from-email", default=None)
    s_cfg.add_argument("--from-name", default=None)
    s_cfg.add_argument("--imap-host", default=None)
    s_cfg.add_argument("--imap-user", default=None)
    s_cfg.add_argument("--imap-password", default=None)
    # Clés sources créateurs
    s_cfg.add_argument("--youtube-api-key", default=None)
    s_cfg.add_argument("--twitch-client-id", default=None)
    s_cfg.add_argument("--twitch-client-secret", default=None)

    s_enrich = sub.add_parser("enrich",
                              help="Visiter les sites web des prospects et compléter")
    s_enrich.add_argument("--no-emails-only", action="store_true",
                          help="Ne traiter que les prospects sans email")
    s_enrich.add_argument("--source", default="",
                          help="Restreindre à une source (denicheur, sirene…)")
    s_enrich.add_argument("--status", default="",
                          help="Restreindre à un statut")
    s_enrich.add_argument("--max", type=int, default=50)
    s_enrich.add_argument("--footprint", action="store_true",
                          help="Pour les prospects sans URL (typiquement Sirene), "
                               "tente de trouver leur site via DuckDuckGo "
                               "(nom + ville)")

    s_list = sub.add_parser("list", help="Lister le CRM")
    s_list.add_argument("--status", default="")
    s_list.add_argument("--source", default="")
    s_list.add_argument("--has-email", action="store_true")
    s_list.add_argument("--limit", type=int, default=50)

    s_exp = sub.add_parser("export", help="Exporter le CRM en CSV")
    s_exp.add_argument("--out", required=True, help="Chemin du fichier CSV")
    s_exp.add_argument("--status", default="")

    s_send = sub.add_parser("send",
                            help="Envoyer une vague d'emails (1er contact ou relance)")
    s_send.add_argument("--template", required=True,
                        help="Clé du template (ex: tpe_intro, tpe_relance_j5)")
    s_send.add_argument("--mon-prenom", default="")
    s_send.add_argument("--signature", default="")
    s_send.add_argument("--daily-cap", type=int, default=40,
                        help="Plafond d'envois par jour (default: 40 = safe Gmail)")
    s_send.add_argument("--pause-min", type=float, default=30.0)
    s_send.add_argument("--pause-max", type=float, default=120.0)
    s_send.add_argument("--allow-unverified", action="store_true",
                        help="Inclut aussi les prospects sans tag 'site_verified'")
    s_send.add_argument("--follow-up", action="store_true",
                        help="Cible les prospects déjà contactés sans réponse "
                             "(envoie une relance au lieu d'un 1er contact)")
    s_send.add_argument("--follow-up-days", type=int, default=5)
    s_send.add_argument("--dry-run", action="store_true",
                        help="Affiche sans envoyer")
    s_send.add_argument("--max", type=int, default=0,
                        help="Plafond cette session (0 = jusqu'au daily-cap)")

    s_poll = sub.add_parser("poll-replies",
                            help="Scanne IMAP et détecte les réponses aux mails envoyés")
    s_poll.add_argument("--verbose", action="store_true")

    return p


def main(argv: list[str] | None = None) -> int:
    ensure_dirs()
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "import-denicheur": cmd_import_denicheur,
        "search-creators": cmd_search_creators,
        "search-sirene": cmd_search_sirene,
        "search-maps": cmd_search_maps,
        "enrich": cmd_enrich,
        "list": cmd_list,
        "export": cmd_export,
        "send": cmd_send,
        "poll-replies": cmd_poll_replies,
        "config": cmd_config,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
