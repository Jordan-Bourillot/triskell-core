"""Pipeline de prospection autonome — orchestration end-to-end.

Enchaîne sans intervention humaine :
    1. search (Sirene/Maps) → prospects bruts
    2. enrich (web + footprint + cross-ref) → prospects qualifiés (site_verified)
    3. AI personalize → mail unique pour chaque prospect (template-cadre + contexte)
    4. mode AUTO    : envoi SMTP direct
       mode SAS     : draft posé dans pending_drafts (validation manuelle ensuite)
    5. relances J+5 sur les non-répondants
    6. poll IMAP → bascule status=replied → stoppe relances

Appelé par :
    - la nightly (~03:00) avec PipelineConfig persistée
    - manuellement via Triskell Command (vue Auto-pilote → "Lancer maintenant")

Toutes les étapes sont opt-in : tu peux désactiver search (si déjà des prospects),
ou désactiver send (si tu veux juste enrichir + générer drafts).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .core.crm import APP_DIR, CONFIG_FILE, CRM, ensure_dirs
from .core.prospect import Prospect, Source, norm_website

logger = logging.getLogger(__name__)


# Fichier de config du pipeline (persistée)
PIPELINE_CONFIG_FILE = APP_DIR / "pipeline.json"


# Modes d'envoi
MODE_AUTO = "auto"               # IA génère + envoi direct
MODE_VALIDATION = "validation"   # IA génère + dépose un draft, user valide à la main
# Compat : ancienne valeur "sas" est interprétée comme "validation"
MODE_SAS = MODE_VALIDATION


@dataclass
class PipelineConfig:
    """Configuration persistée du pipeline auto-pilote."""

    enabled: bool = False
    mode: str = MODE_VALIDATION  # "auto" | "validation"  (ancien "sas" = validation)

    # Source de prospects
    source: str = "sirene"  # sirene | maps | none (pas de search auto)
    sirene_naf: str = ""
    sirene_departement: str = ""
    sirene_code_postal: str = ""
    sirene_query: str = ""
    sirene_effectif: str = "00"
    sirene_min_date_creation: str = ""
    maps_query: str = ""
    maps_lat: float | None = None
    maps_lng: float | None = None
    maps_radius_m: int = 50000
    search_max_results: int = 50

    # Obelisk (créateurs/vendeurs réseaux, déjà déposés dans la base partagée)
    obelisk_platform: str = ""             # "" = toutes ; ex "youtube", "tiktok"…
    obelisk_min_subscribers: int = 0       # 0 = pas de plancher
    obelisk_max_subscribers: int = 0       # 0 = pas de plafond
    obelisk_country: str = ""              # "" = tout, ex "FR"
    obelisk_language: str = ""             # "" = tout, ex "fr"
    obelisk_only_with_email: bool = False  # exclut ceux sans email connu
    obelisk_only_uncontacted: bool = True  # exclut déjà contactés
    obelisk_monetized_only: bool = False   # uniquement profils monétisés

    # Enrichissement
    enrich_with_footprint: bool = True
    enrich_no_emails_only: bool = True
    enrich_max: int = 100

    # IA
    ai_provider: str = "google"
    ai_model: str = "gemini-2.5-flash"
    ai_mega_prompts: list[str] = field(default_factory=lambda: ["01"])  # honnêteté brutale par défaut
    ai_template_brief: str = (
        "Génère un mail de prospection court (≤ 12 lignes), tutoiement chaleureux mais "
        "professionnel. L'objet doit être personnalisé avec le nom de l'entreprise. "
        "Pas de bullshit, pas de jargon, pas d'emojis. Format strict :\n"
        "OBJET : <objet>\n\n"
        "<corps du mail>\n\n"
        "Cordialement,\n{mon_prenom}"
    )

    # Sender
    sender_mon_prenom: str = ""
    sender_signature: str = ""

    # Envoi
    daily_cap: int = 40
    follow_up_days: int = 5

    # Auto-pilote v2 : combien de prospects vises par run nocturne. Utilise
    # par le UI tableau de commande pour afficher / piloter le volume cible.
    nightly_target: int = 50

    # Auto-pilote v2 : quel produit on pousse cette nuit. Si rempli, le
    # pipeline pioche dans les templates de prospection de ce produit
    # (table triskell_email_templates, category='prospection') au lieu
    # de generer le mail from scratch. Vide -> ancien comportement (IA libre).
    autopilot_product: str = ""

    # Auto-pilote v2 : audience visee pour le matching de template.
    # "" = peu importe, "creator" = createurs/influenceurs, "pro" = B2B local.
    autopilot_audience: str = ""

    # Auto-pilote v2 : seuil minimal de note de la 2e IA pour autoriser
    # l'envoi direct (sinon brouillon). 0 = pas de relecture.
    autopilot_review_min_score: int = 7

    @classmethod
    def load(cls) -> "PipelineConfig":
        if not PIPELINE_CONFIG_FILE.exists():
            return cls()
        try:
            data = json.loads(PIPELINE_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return cls()
        # Tolérance aux clés en trop ou manquantes
        valid = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in valid}
        # Migration : ancien mode="sas" → "validation"
        if clean.get("mode") == "sas":
            clean["mode"] = MODE_VALIDATION
        return cls(**clean)

    def save(self) -> None:
        ensure_dirs()
        PIPELINE_CONFIG_FILE.write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Stats accumulées par run
# ---------------------------------------------------------------------------
@dataclass
class PipelineStats:
    started_at: str = ""
    finished_at: str = ""
    searched: int = 0
    enriched: int = 0
    enrich_emails_found: int = 0
    drafts_generated: int = 0
    drafts_sent: int = 0
    drafts_pending: int = 0
    follow_ups_sent: int = 0
    replies_detected: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------
def run_full_pipeline(
    cfg: PipelineConfig,
    *,
    progress: Callable[[str], None] | None = None,
    poll_imap: bool = True,
    do_follow_up: bool = True,
    do_search: bool = True,
    do_enrich: bool = True,
    do_send: bool = True,
) -> PipelineStats:
    """Exécute le pipeline complet en une passe.

    Args:
        cfg: configuration courante (critères, mode, etc.)
        progress: callback(str) appelé à chaque étape pour reporter la progression
        poll_imap: poll IMAP en début pour stopper les relances inutiles
        do_search: recherche de nouveaux prospects
        do_enrich: enrichissement web
        do_send: génération IA + envoi (ou drafts si mode=sas)
        do_follow_up: relances J+5 sur les non-répondants
    """
    stats = PipelineStats(started_at=datetime.now().isoformat(timespec="seconds"))
    log = progress or (lambda _msg: None)

    # ------------------------------------------------------------------
    # 0) Poll IMAP en premier — pour ne pas relancer ceux qui ont répondu
    # ------------------------------------------------------------------
    if poll_imap:
        log("Étape 0/4 — Vérification des réponses IMAP…")
        try:
            from .outreach import imap_listener
            r = imap_listener.poll_replies(verbose=False)
            stats.replies_detected = r.get("matched", 0)
            log(f"  → {stats.replies_detected} réponse(s) détectée(s)")
        except imap_listener.ImapConfigError as e:
            log(f"  ⚠ IMAP non configuré ({e}) — étape skippée")
        except Exception as e:
            stats.errors.append(f"imap: {e}")
            log(f"  ⚠ {e}")

    # ------------------------------------------------------------------
    # 1) Search
    # ------------------------------------------------------------------
    if do_search and cfg.source != "none":
        log(f"Étape 1/4 — Recherche {cfg.source}…")
        try:
            crm = CRM()
            iterator, finalize = _search_iterator(cfg, log=log)
            before = len(crm)
            r = crm.upsert_many(iterator)
            crm.save()
            stats.searched = r.get("created", 0)
            log(f"  → {stats.searched} nouveau(x) prospect(s) ({r.get('merged', 0)} fusionnés)")
            try:
                finalize()
            except Exception as e:
                logger.debug("search cursor finalize: %s", e)
        except Exception as e:
            stats.errors.append(f"search: {e}")
            log(f"  ⚠ {e}")

    # ------------------------------------------------------------------
    # 2) Enrich
    # ------------------------------------------------------------------
    if do_enrich:
        log(f"Étape 2/4 — Enrichissement web (max {cfg.enrich_max})…")
        try:
            stats.enriched, stats.enrich_emails_found = _run_enrichment(cfg, log)
        except Exception as e:
            stats.errors.append(f"enrich: {e}")
            log(f"  ⚠ {e}")

    # ------------------------------------------------------------------
    # 3) Génération IA + envoi (ou drafts)
    # ------------------------------------------------------------------
    if do_send:
        log(f"Étape 3/4 — Génération IA + envoi (mode {cfg.mode})…")
        try:
            sent, pending = _run_ai_outreach(cfg, log)
            stats.drafts_generated = sent + pending
            stats.drafts_sent = sent
            stats.drafts_pending = pending
        except Exception as e:
            stats.errors.append(f"send: {e}")
            log(f"  ⚠ {e}")

    # ------------------------------------------------------------------
    # 4) Relances J+5 (uniquement mode auto, en sas l'user gère)
    # ------------------------------------------------------------------
    if do_follow_up and cfg.mode == MODE_AUTO:  # follow-up auto-only
        log(f"Étape 4/4 — Relances J+{cfg.follow_up_days}…")
        try:
            from .outreach import smtp_sender
            r = smtp_sender.run_campaign(
                template_key="tpe_relance_j5",
                sender_vars={
                    "mon_prenom": cfg.sender_mon_prenom,
                    "signature": cfg.sender_signature,
                },
                daily_cap=cfg.daily_cap,
                follow_up=True,
                follow_up_days=cfg.follow_up_days,
                dry_run=False,
            )
            stats.follow_ups_sent = r.get("sent", 0)
            log(f"  → {stats.follow_ups_sent} relance(s) envoyée(s)")
        except Exception as e:
            stats.errors.append(f"follow_up: {e}")
            log(f"  ⚠ {e}")

    stats.finished_at = datetime.now().isoformat(timespec="seconds")
    return stats


# ---------------------------------------------------------------------------
# Helpers internes
# ---------------------------------------------------------------------------
def _search_iterator(cfg: PipelineConfig, *, log: Callable[[str], None] | None = None):
    """Renvoie (iterator, finalize_callback).

    Le finalize_callback doit être appelé APRÈS la consommation totale de
    l'iterator : il persiste l'avancée du curseur pour le prochain run.

    Cette indirection permet à chaque source d'avancer entre runs au lieu
    de retomber sur les mêmes pages / mêmes coordonnées / mêmes prospects.
    """
    from . import search_cursor
    _log = log or (lambda _msg: None)

    if cfg.source == "sirene":
        from .sources import sirene
        naf_codes = search_cursor.split_list(cfg.sirene_naf) or [""]
        depts = search_cursor.split_list(cfg.sirene_departement) or [""]
        criteria = {
            "source": "sirene",
            "naf_codes": naf_codes,
            "depts": depts,
            "code_postal": cfg.sirene_code_postal,
            "query": cfg.sirene_query,
            "effectif": cfg.sirene_effectif,
            "min_date_creation": cfg.sirene_min_date_creation,
        }
        state = search_cursor.load("sirene", criteria)
        naf_idx = int(state.get("naf_index") or 0) % len(naf_codes)
        dept_idx = int(state.get("dept_index") or 0) % len(depts)
        start_page = int(state.get("next_page") or 1)
        cur_naf = naf_codes[naf_idx]
        cur_dept = depts[dept_idx]
        _log(f"  curseur sirène : NAF={cur_naf or '*'} dept={cur_dept or '*'} page={start_page}")
        cursor_out: dict = {}

        iterator = sirene.search(
            activite_principale=cur_naf,
            departement=cur_dept,
            code_postal=cfg.sirene_code_postal,
            nom_entreprise=cfg.sirene_query,
            min_date_creation=cfg.sirene_min_date_creation,
            tranche_effectif=cfg.sirene_effectif,
            max_results=cfg.search_max_results,
            start_page=start_page,
            cursor_out=cursor_out,
        )

        def finalize_sirene():
            new_state = dict(state)
            if cursor_out.get("exhausted"):
                # On a fini ce (NAF, dept) → on avance dans la rotation
                next_dept_idx = (dept_idx + 1) % len(depts)
                if next_dept_idx == 0:
                    next_naf_idx = (naf_idx + 1) % len(naf_codes)
                else:
                    next_naf_idx = naf_idx
                new_state["naf_index"] = next_naf_idx
                new_state["dept_index"] = next_dept_idx
                new_state["next_page"] = 1
            else:
                new_state["naf_index"] = naf_idx
                new_state["dept_index"] = dept_idx
                new_state["next_page"] = cursor_out.get("next_page", start_page + 1)
            search_cursor.save("sirene", criteria, new_state)

        return iterator, finalize_sirene

    if cfg.source == "maps":
        from .sources import maps
        if not maps.is_configured():
            raise RuntimeError("Clé Google Places non configurée")
        criteria = {
            "source": "maps",
            "query": cfg.maps_query,
            "lat": cfg.maps_lat,
            "lng": cfg.maps_lng,
            "radius_m": cfg.maps_radius_m,
        }
        state = search_cursor.load("maps", criteria)
        # Pas de coord → pas de rotation possible
        if cfg.maps_lat is None or cfg.maps_lng is None:
            iterator = maps.search(
                text_query=cfg.maps_query,
                radius_m=cfg.maps_radius_m,
                max_results=cfg.search_max_results,
            )
            return iterator, (lambda: None)

        # Grille : cellules de taille = radius (zones tangentes)
        step_m = max(1000, int(cfg.maps_radius_m))
        cells = search_cursor.spiral_offsets(step_m=step_m, max_cells=49)
        cell_idx = int(state.get("cell_index") or 0) % len(cells)
        dx, dy = cells[cell_idx]
        _log(f"  curseur maps : cellule {cell_idx}/{len(cells) - 1} (offset {dx}m, {dy}m)")
        cursor_out = {}

        iterator = maps.search(
            text_query=cfg.maps_query,
            location_bias_lat=cfg.maps_lat,
            location_bias_lng=cfg.maps_lng,
            radius_m=cfg.maps_radius_m,
            max_results=cfg.search_max_results,
            lat_offset_m=float(dy),
            lng_offset_m=float(dx),
            cursor_out=cursor_out,
        )

        def finalize_maps():
            new_state = dict(state)
            # On avance toujours d'une cellule, qu'elle soit épuisée ou
            # pleine — pour explorer la grille. La même cellule sera
            # re-visitée plus tard au prochain tour.
            new_state["cell_index"] = (cell_idx + 1) % len(cells)
            search_cursor.save("maps", criteria, new_state)

        return iterator, finalize_maps

    if cfg.source == "obelisk":
        from .sources import obelisk
        if not obelisk.is_available():
            raise RuntimeError(
                "Base partagée Triskell non joignable (connexion requise)"
            )
        criteria = {
            "source": "obelisk",
            "platform": cfg.obelisk_platform,
            "min_subs": cfg.obelisk_min_subscribers,
            "max_subs": cfg.obelisk_max_subscribers,
            "country": cfg.obelisk_country,
            "language": cfg.obelisk_language,
            "with_email": cfg.obelisk_only_with_email,
            "uncontacted": cfg.obelisk_only_uncontacted,
            "monetized": cfg.obelisk_monetized_only,
        }
        state = search_cursor.load("obelisk", criteria)
        offset = int(state.get("next_offset") or 0)
        seed = int(state.get("shuffle_seed") or 0) or None
        if seed is None:
            # Première fois : on génère une seed stable pour ce jeu de critères
            import hashlib as _h
            seed = int(_h.sha1(str(criteria).encode()).hexdigest()[:8], 16)
        _log(f"  curseur obelisk : offset={offset} (seed={seed})")
        cursor_out = {}

        iterator = obelisk.search(
            platform=cfg.obelisk_platform,
            min_subscribers=cfg.obelisk_min_subscribers or None,
            max_subscribers=cfg.obelisk_max_subscribers or None,
            country=cfg.obelisk_country,
            language=cfg.obelisk_language,
            only_with_email=cfg.obelisk_only_with_email,
            only_uncontacted=cfg.obelisk_only_uncontacted,
            monetized_only=cfg.obelisk_monetized_only,
            max_results=cfg.search_max_results,
            offset=offset,
            shuffle_seed=seed,
            cursor_out=cursor_out,
        )

        def finalize_obelisk():
            new_state = dict(state)
            if cursor_out.get("exhausted"):
                # Tour complet : on garde la seed (ordre stable) mais on
                # repart au début. L'anti-doublon CRM saute les déjà-contactés.
                new_state["next_offset"] = 0
            else:
                new_state["next_offset"] = cursor_out.get("next_offset", offset)
            new_state["shuffle_seed"] = seed
            search_cursor.save("obelisk", criteria, new_state)

        return iterator, finalize_obelisk

    return iter([]), (lambda: None)


def _run_enrichment(cfg: PipelineConfig, log: Callable[[str], None]) -> tuple[int, int]:
    """Exécute l'enrichissement web. Renvoie (n_enriched, n_emails_found)."""
    from .enrichers.footprint import FootprintFinder
    from .enrichers.linktree import LinktreeFollower, is_hub
    from .enrichers.web import WebEnricher

    crm = CRM()
    web = WebEnricher()
    linktree = LinktreeFollower(web_enricher=web)
    footprint = FootprintFinder() if cfg.enrich_with_footprint else None

    # Cibles : sans email + qui ont un nom/ville (footprint) ou un site déjà connu
    candidates = []
    for p in crm.all():
        if cfg.enrich_no_emails_only and p.emails:
            continue
        if p.website or p.other_urls or (footprint and p.name and p.city):
            candidates.append(p)
        if len(candidates) >= cfg.enrich_max:
            break

    if not candidates:
        log(f"  → 0 prospect(s) à enrichir")
        return (0, 0)

    n_enriched = 0
    n_emails = 0
    for i, prospect in enumerate(candidates, 1):
        url = prospect.website or _first_useful_url(prospect)
        url_was_guessed = False
        if not url and footprint and prospect.name and (prospect.city or prospect.legal_name):
            url = footprint.find_official_site(
                prospect.name or prospect.legal_name,
                city=prospect.city,
                country=prospect.country or "FR",
            )
            if url:
                url_was_guessed = True
        if not url:
            continue
        try:
            if is_hub(url):
                data = linktree.enrich_hub(url)
                if data.get("primary_url") and not prospect.website:
                    prospect.website = data["primary_url"]
            else:
                data = web.enrich_url(url)
                verdict = _cross_ref(prospect, data, url=url)
                if verdict == "reject":
                    prospect.history.append({
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "kind": "site_rejected", "url": url,
                    })
                    continue
                if not prospect.website:
                    if not url_was_guessed or verdict in ("high", "ok"):
                        prospect.website = url
                if verdict == "high":
                    prospect.tags = [t for t in prospect.tags if t != "site_unverified"]
                    if "site_verified" not in prospect.tags:
                        prospect.tags.append("site_verified")
                elif verdict == "low" and "site_unverified" not in prospect.tags:
                    prospect.tags.append("site_unverified")

            gained_emails = [e for e in data["emails"]
                             if e.lower() not in {x.lower() for x in prospect.emails}]
            gained_phones = [p_ for p_ in data["phones"]
                             if p_ not in prospect.phones]
            if gained_emails:
                prospect.emails = (prospect.emails + gained_emails)[:8]
                n_emails += len(gained_emails)
            if gained_phones:
                prospect.phones = (prospect.phones + gained_phones)[:5]
            if data["address"] and not prospect.address:
                prospect.address = data["address"]
            if data["has_legal_mentions"]:
                prospect.has_legal_mentions = True
            prospect.history.append({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "kind": "web_enrich", "url": url,
            })
            prospect.sources.append(Source(
                name="linktree" if is_hub(url) else "web",
                source_id=norm_website(url), url=url,
            ))
            prospect.updated_at = datetime.now().isoformat(timespec="seconds")
            crm._dirty = True  # noqa: SLF001
            n_enriched += 1
        except Exception as e:
            logger.debug("enrich %s : %s", prospect.name, e)
        if i % 10 == 0:
            log(f"  → {i}/{len(candidates)} traité(s)…")

    crm._rebuild_index()  # noqa: SLF001
    crm.save()
    log(f"  → {n_enriched} enrichi(s), {n_emails} nouveau(x) email(s)")
    return n_enriched, n_emails


def _run_ai_outreach(cfg: PipelineConfig, log: Callable[[str], None]) -> tuple[int, int]:
    """Génère mail IA personnalisé pour chaque prospect éligible, puis envoie OU draft.

    Renvoie (n_sent, n_pending).
    """
    from ..ai import providers as ai_providers
    from ..ai.builder import build_ultimate_prompt
    from ..ai.library import load_packaged_library

    # Charge config IA depuis config.json (clés API)
    api_keys = _load_ai_keys()
    if not api_keys.get(cfg.ai_provider):
        log(f"  ⚠ Clé API '{cfg.ai_provider}' manquante — étape AI skippée")
        return (0, 0)

    # === Mode "pioche dans templates" (Auto-pilote v2, etape 6) ===
    # Si cfg.autopilot_product est rempli, on tente d'utiliser les templates
    # de prospection deja prets (table triskell_email_templates) au lieu de
    # la generation libre. Si l'import echoue ou aucun template n'est dispo,
    # fallback automatique sur la generation libre.
    templates_for_picking: list[dict] = []
    use_templates = bool((cfg.autopilot_product or "").strip())
    if use_templates:
        try:
            from triskell_command.integrations.prospection_templates import (
                list_prospection_templates,
            )
            templates_for_picking = list_prospection_templates(
                product=cfg.autopilot_product.strip(),
                audience=(cfg.autopilot_audience or "").strip() or None,
            ) or []
            log(f"  -> mode templates : {len(templates_for_picking)} template(s) "
                f"trouve(s) pour produit '{cfg.autopilot_product}'")
            if not templates_for_picking:
                use_templates = False
                log("  [WARN] aucun template pour ce produit -> fallback IA libre")
        except ImportError as exc:
            use_templates = False
            log(f"  [WARN] mode templates indisponible ({exc}) -> fallback IA libre")
        except Exception as exc:
            use_templates = False
            log(f"  [WARN] chargement templates a plante ({exc}) -> fallback IA libre")

    # Charge méga-prompts seulement si mode libre
    if not use_templates:
        library = load_packaged_library()
        selected_megas = [mp for mp in library if mp.get("id") in cfg.ai_mega_prompts]
    else:
        selected_megas = []  # pas utilise en mode templates

    crm = CRM()
    eligible = [
        p for p in crm.all()
        if p.emails
        and "site_verified" in (p.tags or [])
        and p.status in ("new", "qualified")
        and not any(h.get("kind") == "email_sent" for h in (p.history or []))
        and not p.pending_drafts  # pas déjà un draft en attente
    ]
    if not eligible:
        log(f"  → 0 prospect(s) éligible(s) (besoin email + site_verified + jamais contacté)")
        return (0, 0)

    cap = min(cfg.daily_cap, len(eligible))
    log(f"  → {cap} prospect(s) éligibles, génération IA…")

    n_sent = 0
    n_pending = 0
    for i, prospect in enumerate(eligible[:cap], 1):
        try:
            if use_templates:
                # === Mode templates : pioche le bon modele puis adapte ===
                from triskell_command.integrations.convoy_ai import (
                    generate_message_from_templates,
                )
                prospect_dict = {
                    "raison_sociale": prospect.name or prospect.legal_name or "",
                    "prenom":         "",
                    "nom":            "",
                    "email":          prospect.emails[0] if prospect.emails else "",
                    "ville":          prospect.city or "",
                    "code_postal":    prospect.postal_code or "",
                    "secteur":        prospect.industry or "",
                    "notes":          (prospect.description or "")[:300],
                }
                gen = generate_message_from_templates(
                    prospect_dict,
                    templates=templates_for_picking,
                    template_product=cfg.autopilot_product.strip(),
                    sender_name=cfg.sender_mon_prenom or "",
                    user_brief=cfg.ai_template_brief or "",
                    provider=cfg.ai_provider,
                    model=cfg.ai_model,
                    api_keys=api_keys,
                )
                subject = gen.get("subject") or ""
                body    = gen.get("body") or ""
                _tpl_key = gen.get("template_key") or "auto"
            else:
                # === Mode classique : generation libre ===
                user_prompt = _build_personalized_prompt(prospect, cfg)
                full = build_ultimate_prompt(user_prompt, selected_megas)
                response = ai_providers.send_to_provider(
                    cfg.ai_provider, cfg.ai_model, full, api_keys,
                )
                subject, body = _parse_ai_response(response, prospect, cfg)
                _tpl_key = "ai_pipeline"

            # === Etape 7 : 2e IA de relecture ===
            # Si autopilot_review_min_score > 0, on relit le mail et on
            # decide envoi vs draft selon la note. Sinon : comportement
            # actuel (cfg.mode decide tout).
            effective_mode = cfg.mode
            if int(getattr(cfg, "autopilot_review_min_score", 0) or 0) > 0:
                try:
                    from .quality_reviewer import review_email
                    ctx = (
                        f"Nom: {prospect.name or prospect.legal_name or '?'}\n"
                        f"Ville: {prospect.city or '?'}\n"
                        f"Secteur: {prospect.industry or '?'}\n"
                        f"Description: {(prospect.description or '')[:200]}"
                    )
                    review = review_email(
                        subject=subject, body=body,
                        prospect_context=ctx,
                        provider=cfg.ai_provider,
                        model=cfg.ai_model,
                        api_keys=api_keys,
                    )
                    log(f"  [review] {prospect.name[:30]} : "
                        f"score={review['score']}/10 verdict={review['verdict']} "
                        f"-- {review['comment'][:80]}")
                    if (review["verdict"] == "draft"
                        or review["score"] < cfg.autopilot_review_min_score):
                        effective_mode = MODE_VALIDATION
                        log(f"    -> force en brouillon (score insuffisant)")
                    # Trace la review dans l'historique du prospect (compteur Relit)
                    prospect.history.append({
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "kind": "email_reviewed",
                        "score": review["score"],
                        "verdict": review["verdict"],
                        "comment": review["comment"],
                    })
                except Exception as exc:
                    log(f"  [WARN] reviewer plante ({exc}) -> on envoie sans relecture")

            if effective_mode == MODE_AUTO:
                # Envoi direct via SMTP
                from .outreach.smtp_sender import _load_smtp_config, send_email
                smtp_cfg = _load_smtp_config()
                msg_id = send_email(
                    smtp_cfg, to=prospect.emails[0],
                    subject=subject, body=body,
                )
                prospect.history.append({
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "kind": "email_sent",
                    "to": prospect.emails[0],
                    "subject": subject,
                    "template_key": _tpl_key,
                    "message_id": msg_id,
                    "generated_by": f"{cfg.ai_provider}/{cfg.ai_model}",
                })
                prospect.status = "contacted"
                prospect.last_contact_at = datetime.now().isoformat(timespec="seconds")
                n_sent += 1
            else:
                # Mode SAS : on dépose le draft pour validation manuelle
                prospect.pending_drafts.append({
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "kind": "first_contact",
                    "subject": subject,
                    "body": body,
                    "template_key": _tpl_key,
                    "provider": cfg.ai_provider,
                    "model": cfg.ai_model,
                })
                prospect.status = "qualified"  # garde "qualified" tant que pas validé
                n_pending += 1
            crm._dirty = True  # noqa: SLF001
        except Exception as e:
            log(f"  ⚠ {prospect.name[:30]} : {e}")

        if i % 5 == 0:
            log(f"    {i}/{cap}…")

    crm.save()
    log(f"  → {n_sent} envoyé(s), {n_pending} en attente de validation")
    return n_sent, n_pending


def _build_personalized_prompt(prospect: Prospect, cfg: PipelineConfig) -> str:
    """Construit la consigne user pour l'IA, contextualisée sur le prospect."""
    name = prospect.name or prospect.legal_name or "(sans nom)"
    main_name = name.split("(", 1)[0].strip(" .-")
    city = prospect.city or "—"
    industry = prospect.industry or "—"
    description = prospect.description or "—"

    return (
        f"Tu vas rédiger un mail de prospection commercial pour ce prospect précis :\n\n"
        f"PROSPECT :\n"
        f"- Nom / entreprise : {main_name}\n"
        f"- Ville : {city}\n"
        f"- Secteur : {industry}\n"
        f"- Description : {description[:300]}\n"
        f"- Site web : {prospect.website or '—'}\n\n"
        f"MON PRÉNOM : {cfg.sender_mon_prenom or '(non renseigné)'}\n\n"
        f"CONSIGNES :\n{cfg.ai_template_brief}\n"
    )


def _parse_ai_response(response: str, prospect: Prospect, cfg: PipelineConfig) -> tuple[str, str]:
    """Extrait (subject, body) d'une réponse IA. Tolérant aux variations."""
    response = (response or "").strip()
    subject = ""
    body = response

    # Pattern strict : "OBJET : ...\n\n<corps>"
    import re
    m = re.search(r"^\s*(?:OBJET|SUBJECT|Objet)\s*[:：]\s*(.+?)\s*$", response,
                  re.MULTILINE | re.IGNORECASE)
    if m:
        subject = m.group(1).strip()
        # Corps = tout ce qui suit la ligne objet
        body = response[m.end():].lstrip()

    if not subject:
        # Fallback : objet générique
        name = prospect.name or prospect.legal_name or "vous"
        subject = f"Une idée pour {name.split('(', 1)[0].strip(' .-')}"

    return (subject, body)


def _load_ai_keys() -> dict:
    """Charge les clés API IA depuis ~/.triskell-prospect/config.json + AppState."""
    keys: dict[str, str] = {}
    # 1) Tente CONFIG_FILE de Core
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            for k in ("anthropic", "openai", "google", "mistral", "xai"):
                v = data.get(f"ai_api_key_{k}") or data.get(k) or ""
                if v:
                    keys[k] = v
        except Exception:
            pass
    # 2) Tente le state Triskell Command (~/.triskell-command/settings.json)
    cmd_settings = Path.home() / ".triskell-command" / "settings.json"
    if cmd_settings.exists():
        try:
            data = json.loads(cmd_settings.read_text(encoding="utf-8"))
            ai_keys = (data.get("ai") or {}).get("api_keys") or {}
            for k, v in ai_keys.items():
                if v:
                    keys[k] = v
        except Exception:
            pass
    return keys


# ---------------------------------------------------------------------------
# Cross-ref Sirene/site (réplique du CLI pour réutilisation)
# ---------------------------------------------------------------------------
def _cross_ref(prospect: Prospect, web_data: dict, url: str = "") -> str:
    from urllib.parse import urlparse
    import re as _re
    import unicodedata
    site_sirens = set(web_data.get("sirens") or [])
    site_sirets = set(web_data.get("sirets") or [])
    site_postal = set(web_data.get("postal_codes") or [])
    site_emails = web_data.get("emails") or []
    if not prospect.siren and not prospect.postal_code:
        return "low"
    if prospect.siren:
        if prospect.siren in site_sirens:
            return "high"
        if any(s.startswith(prospect.siren) for s in site_sirets):
            return "high"
        if site_sirens and prospect.siren not in site_sirens:
            return "reject"
    if url and site_emails:
        try:
            host = urlparse(url if url.startswith(("http://", "https://"))
                            else "https://" + url).netloc.lower().lstrip("www.")
        except Exception:
            host = ""
        if host:
            domain_emails = [e for e in site_emails if e.endswith("@" + host)]
            if domain_emails:
                def _slug(s: str) -> str:
                    if not s:
                        return ""
                    base = s.split("(", 1)[0]
                    nfkd = unicodedata.normalize("NFKD", base)
                    no_acc = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
                    return _re.sub(r"[^a-z0-9]", "", no_acc.lower())
                ns = _slug(prospect.name) or _slug(prospect.legal_name)
                hs = _slug(host.split(".")[0])
                if ns and hs:
                    if ns == hs:
                        return "high"
                    a, b = sorted([len(ns), len(hs)])
                    ratio = a / b if b else 0
                    if ratio >= 0.7 and (ns in hs or hs in ns):
                        return "high"
    if prospect.postal_code and site_postal:
        if prospect.postal_code in site_postal:
            return "ok"
        dept = prospect.postal_code[:2]
        if any(cp.startswith(dept) for cp in site_postal):
            return "low"
        return "reject"
    return "low"


def _first_useful_url(prospect: Prospect) -> str:
    pure_social = ("youtube.com", "youtu.be", "twitch.tv", "reddit.com",
                   "x.com", "twitter.com", "facebook.com", "instagram.com",
                   "tiktok.com", "linkedin.com")
    from urllib.parse import urlparse
    for u in prospect.other_urls:
        if not u:
            continue
        try:
            host = urlparse(u if u.startswith(("http://", "https://"))
                            else "https://" + u).netloc.lower()
        except Exception:
            continue
        if not any(host == d or host.endswith("." + d) for d in pure_social):
            return u
    return ""


# ---------------------------------------------------------------------------
# API : approbation / rejet d'un draft (mode SAS)
# ---------------------------------------------------------------------------
def approve_draft(prospect_match_key: str, draft_index: int = 0) -> dict:
    """Envoie le draft #idx d'un prospect (mode SAS → réel envoi)."""
    from .outreach.smtp_sender import _load_smtp_config, send_email
    crm = CRM()
    target = None
    for p in crm.all():
        if prospect_match_key in p.match_keys:
            target = p
            break
    if not target:
        return {"ok": False, "reason": "prospect introuvable"}
    if not target.pending_drafts or draft_index >= len(target.pending_drafts):
        return {"ok": False, "reason": "aucun draft à valider"}
    draft = target.pending_drafts.pop(draft_index)

    try:
        smtp_cfg = _load_smtp_config()
        msg_id = send_email(
            smtp_cfg,
            to=target.emails[0] if target.emails else "",
            subject=draft["subject"],
            body=draft["body"],
        )
        target.history.append({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "kind": "email_sent",
            "to": target.emails[0] if target.emails else "",
            "subject": draft["subject"],
            "template_key": draft.get("template_key", "ai_pipeline"),
            "message_id": msg_id,
            "generated_by": f"{draft.get('provider', '?')}/{draft.get('model', '?')}",
            "approved_from_sas": True,
        })
        target.status = "contacted"
        target.last_contact_at = datetime.now().isoformat(timespec="seconds")
        crm._dirty = True  # noqa: SLF001
        crm.save()
        return {"ok": True, "message_id": msg_id}
    except Exception as e:
        # Restaure le draft si échec
        target.pending_drafts.insert(draft_index, draft)
        crm._dirty = True  # noqa: SLF001
        crm.save()
        return {"ok": False, "reason": str(e)}


def reject_draft(prospect_match_key: str, draft_index: int = 0) -> dict:
    """Supprime le draft #idx (sans envoyer)."""
    crm = CRM()
    target = None
    for p in crm.all():
        if prospect_match_key in p.match_keys:
            target = p
            break
    if not target or not target.pending_drafts:
        return {"ok": False, "reason": "aucun draft"}
    if draft_index >= len(target.pending_drafts):
        return {"ok": False, "reason": "draft index invalide"}
    target.pending_drafts.pop(draft_index)
    target.history.append({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "kind": "draft_rejected",
    })
    crm._dirty = True  # noqa: SLF001
    crm.save()
    return {"ok": True}


def list_pending_drafts() -> list[tuple[Prospect, dict]]:
    """Renvoie [(prospect, draft), ...] de tous les drafts en attente."""
    crm = CRM()
    out = []
    for p in crm.all():
        for draft in (p.pending_drafts or []):
            out.append((p, draft))
    return out
