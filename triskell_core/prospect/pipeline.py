"""Pipeline de prospection autonome — orchestration end-to-end.

Enchaîne sans intervention humaine :
    1. search (Sirene/Maps) → prospects bruts
    2. enrich (web + footprint + cross-ref) → prospects enrichis
    3. AI personalize → mail unique pour chaque prospect (template-cadre + contexte)
    4. mode AUTO    : envoi SMTP direct
       mode SAS     : draft posé dans pending_drafts (validation manuelle ensuite)
    5. relances J+5 sur les non-répondants
    6. poll IMAP → bascule status=replied → stoppe relances

Appelé par :
    - le déclencheur automatique à l'heure réglée dans le tableau de commande
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
    ai_provider: str = "anthropic"
    ai_model: str = "claude-sonnet-4-5"
    # Méga-prompts : NON utilisés par la rédaction des mails de prospection
    # (ils sont conçus pour le chat avec Claude et entrent en conflit avec
    # les consignes mail). Champ gardé pour compat avec d'éventuelles vues
    # historiques mais le pipeline les ignore explicitement. Cf. selected_megas
    # dans _run_ai_outreach.
    ai_mega_prompts: list[str] = field(default_factory=list)
    ai_template_brief: str = (
        "Génère un mail de prospection court (≤ 12 lignes). "
        "L'objet doit être personnalisé avec le nom de l'entreprise. "
        "Pas de bullshit, pas de jargon, pas d'emojis, pas d'expressions familières "
        "type 'A tout' / 'Bisous' / 'A+'. Format strict :\n"
        "OBJET : <objet>\n\n"
        "<corps du mail>\n\n"
        "<formule de fin adaptée au ton>,\n{mon_prenom}"
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

    # Auto-pilote v2 : plage horaire d'envoi (heure Europe/Paris).
    # Hors plage -> les mails generes sont mis en brouillons au lieu d'etre
    # envoyes (l'envoi reprendra naturellement quand on est de nouveau
    # dans la fenetre). 0..23, inclusif sur le debut, exclusif sur la fin.
    send_hour_start: int = 8
    send_hour_end:   int = 19

    # Auto-pilote v2 : delai en secondes a observer entre 2 envois successifs.
    # Sert a etaler la cadence (anti-detection spam, reputation IONOS, etc.).
    # 0 = pas de delai (comportement historique). S'applique aussi a l'envoi
    # groupe manuel depuis l'onglet Brouillons.
    send_delay_seconds: int = 0

    # Délivrabilité : plafond d'envois AUTO par run vers des adresses
    # DEVINÉES (génériques type contact@ / info@, jamais confirmées noir
    # sur blanc). Les adresses confirmées partent d'abord ; au-delà du
    # quota, les devinées passent en brouillon au lieu d'envoi direct.
    # Règle de Jordan : protéger la réputation IONOS partagée.
    guessed_daily_cap: int = 8

    # Auto-pilote v2 : heure de declenchement du run nocturne (Europe/Paris).
    # Le runner verifie toutes les 5 min ; quand on est dans la fenetre
    # [nightly_hour, nightly_hour+1[ ET qu'on n'a pas deja run aujourd'hui,
    # il declenche la chaine. Defaut 3h. Borne [0, 23].
    nightly_hour: int = 3

    # Auto-pilote v2 : pool d'adresses expeditrices avec cap individuel 24h
    # glissantes. Liste de dicts {"account_id": str, "daily_cap": int}.
    # - Pool vide -> envoi mono-adresse (compte principal), cap global = daily_cap.
    # - Pool rempli -> a chaque mail, tirage aleatoire d'une adresse dont le cap
    #   n'est pas encore atteint sur 24h glissantes. Si toutes saturees ->
    #   bascule en brouillon pour le reste du run.
    autopilot_sender_pool: list = field(default_factory=list)

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
# CRM du pipeline — base PARTAGÉE (Supabase) d'abord, locale en secours
# ---------------------------------------------------------------------------
# C'est LE branchement central de toute la chaîne : historiquement le
# pipeline lisait UNIQUEMENT le fichier local (~/.triskell-prospect/
# prospects.json). Sur le serveur web, les outils de recherche poussent
# pourtant leurs prospects dans la base partagée Supabase → l'Auto-pilote
# ne les voyait JAMAIS. Le moteur travaille maintenant sur la même base
# que les outils, avec retour automatique au fichier local quand Supabase
# n'est pas disponible (desktop hors-ligne, dev) — zéro régression.

def _crm_is_remote(crm) -> bool:
    return crm.__class__.__name__ == "RemoteCRM"


def _pipeline_crm(log: Callable | None = None):
    """Renvoie le CRM de travail du pipeline (partagé si possible)."""
    try:
        from .core.crm import get_crm
        crm = get_crm()
    except Exception as exc:
        logger.debug("get_crm indisponible (%s) — CRM local", exc)
        crm = CRM()
    if log:
        backend = ("base partagée (Supabase)" if _crm_is_remote(crm)
                   else "base locale (fichier)")
        try:
            log(f"  -> source prospects : {backend}")
        except Exception:
            pass
    return crm


def _record_event(crm, prospect, event: dict) -> None:
    """Trace un événement d'historique — en base partagée si possible.

    Sur le CRM partagé, l'événement part dans `email_history` (visible
    dans la fiche prospect web, compté par les pulses/compteurs). Sur le
    CRM local, comportement historique (append en mémoire + save()).
    """
    if _crm_is_remote(crm) and hasattr(crm, "add_history_event"):
        try:
            crm.add_history_event(prospect, event)
            return
        except Exception as exc:
            logger.debug("add_history_event KO (%s) — append local", exc)
    prospect.history.append(event)


def _persist_prospect(crm, prospect) -> None:
    """Persiste les champs modifiés d'un prospect (status, contact, tags…)."""
    if _crm_is_remote(crm):
        try:
            crm.upsert(prospect)
        except Exception as exc:
            logger.warning("persistance prospect KO : %s", exc)
    else:
        crm._dirty = True  # noqa: SLF001 — le crm.save() final écrit le fichier


def _prospect_row_id(crm, prospect) -> str:
    """Identifiant réel du prospect dans le CRM.

    Sur la base partagée (Supabase), l'objet Prospect ne porte PAS son id
    (`prospect.id` est vide) : le vrai id vit dans `crm.get_row_id(prospect)`.
    Sans ça, tout ce qui hashe `prospect.id` (répartition des boîtes du pool,
    lien de désinscription) retombe sur une valeur vide -> même boîte pour
    tout le monde / lien de désinscription incomplet. On lit donc le row id
    en priorité, avec repli sur l'attribut objet (CRM fichier local).
    """
    try:
        rid = crm.get_row_id(prospect)
        if rid:
            return str(rid)
    except Exception:
        pass
    return str(getattr(prospect, "id", "") or "")


def _store_validation_draft(crm, prospect, payload: dict) -> bool:
    """Dépose un brouillon de validation dans la base partagée.

    Renvoie True si le brouillon est en base (table prospect_drafts →
    visible dans « Brouillons à valider » du site web). False → l'appelant
    retombe sur le stockage local historique (pending_drafts).
    Tolérant au schéma : si les colonnes bonus (body_html, notes de la
    2e IA — migration 45) n'existent pas encore, on insère sans elles.
    """
    if not _crm_is_remote(crm):
        return False
    try:
        rid = crm.get_row_id(prospect)
        if not rid:
            return False
        client = crm._client  # noqa: SLF001
        row = {
            "prospect_id": rid,
            "subject": (payload.get("subject") or "")[:200],
            "body": payload.get("body") or "",
            "template_key": payload.get("template_key") or "",
            "provider": payload.get("provider") or "",
            "model": payload.get("model") or "",
            "kind": payload.get("kind") or "first_contact",
            "status": "pending",
            "created_by": client.user_id,
        }
        try:
            ws = client._current_workspace_id()  # noqa: SLF001
        except Exception:
            ws = None
        if ws:
            row["workspace_id"] = ws
        extended = dict(row)
        if payload.get("body_html"):
            extended["body_html"] = payload["body_html"]
        for k in ("review_score", "review_verdict", "review_comment"):
            if k in payload:
                extended[k] = payload[k]
        # Câblage modèle→adresse (migration 46) : l'adresse d'expéditeur
        # exigée voyage avec le brouillon jusqu'à la validation.
        if payload.get("sender_address"):
            extended["sender_address"] = payload["sender_address"]
        # Retouche unique de la 2e IA (migration 53) : avant/après note + type.
        extended_full = dict(extended)
        for k in ("review_score_before", "review_score_after",
                  "review_modif_type", "review_modif_applied"):
            if k in payload:
                extended_full[k] = payload[k]
        sb = client.raw
        try:
            sb.table("prospect_drafts").insert(extended_full).execute()
        except Exception:
            try:
                # Migration 53 pas encore appliquée : on insère sans les
                # champs de retouche (avant/après note + type).
                sb.table("prospect_drafts").insert(extended).execute()
            except Exception:
                # Colonnes bonus absentes (migration 45 pas encore appliquée).
                sb.table("prospect_drafts").insert(row).execute()
        return True
    except Exception as exc:
        logger.warning("brouillon -> base partagée KO (%s), fallback local", exc)
        return False


# Délivrabilité : adresses génériques = « devinées » au sens de Jordan
# (jamais confirmées noir sur blanc comme la boîte d'UNE personne).
GENERIC_EMAIL_LOCALPARTS = frozenset({
    "contact", "info", "hello", "bonjour", "accueil", "commercial",
    "secretariat", "administration", "admin", "direction", "office",
    "mail", "courrier", "reception", "boutique", "magasin",
})


def _email_is_guessed(prospect) -> bool:
    """True si l'adresse principale du prospect est « devinée ».

    Deux signaux : la source de l'email (guess/footprint = fabriquée),
    ou un local-part générique (contact@, info@…). Les confirmées
    partent d'abord, les devinées au compte-gouttes (cf guessed_daily_cap).
    """
    email = (prospect.emails[0] if getattr(prospect, "emails", None) else "") or ""
    email = email.lower().strip()
    if not email or "@" not in email:
        return True
    local = email.split("@", 1)[0]
    for m in (getattr(prospect, "emails_meta", None) or []):
        if (m.get("email") or "").lower() == email:
            if (m.get("source") or "").lower() in ("guess", "footprint"):
                return True
            break
    return local in GENERIC_EMAIL_LOCALPARTS


def _route_for_template_address(template_from: str,
                                 pool_addr_to_account: dict,
                                 pool_remaining: dict) -> tuple:
    """Câblage modèle→adresse : routage de l'expéditeur exigé par le modèle.

    Un modèle de prospection peut exiger SON adresse d'envoi (champ
    « Expéditeur (adresse) » du modèle). Règle absolue : un mail ne part
    JAMAIS sous l'étiquette d'une AUTRE marque — au pire il devient un
    brouillon.

    Rotation même marque (multi-adresses) : la marque, c'est le DOMAINE.
    Si plusieurs adresses du pool partagent le domaine de l'adresse exigée
    (ex. contact@ + hello@ + bonjour@ chez pixel-pros.fr), le mail peut
    partir par n'importe laquelle — toutes affichent « @pixel-pros.fr »,
    donc l'esprit de la règle (jamais une autre marque) est respecté, et
    on répartit le volume sur plusieurs boîtes (meilleure délivrabilité).
    On tire au hasard parmi celles qui ont encore de la marge sur 24 h.

    Args:
        template_from        : adresse exigée par le modèle ("" = aucune).
        pool_addr_to_account : {adresse (lower) → account_id} des comptes du
                               pool d'envoi dont la config SMTP est résolue.
        pool_remaining       : {account_id → envois restants sur 24 h}.

    Renvoie (decision, account_id) :
      - ("none", "")        : pas d'adresse exigée → tirage pool habituel.
      - ("ok", account_id)  : compte trouvé (même marque), marge 24 h OK.
      - ("cap", account_id) : marque trouvée mais toutes ses boîtes au
                              plafond 24 h.
      - ("missing", "")     : aucune adresse de cette marque dans le pool
                              (compte non déclaré ou SMTP incomplet).
    """
    import random
    addr = (template_from or "").strip().lower()
    if not addr:
        return ("none", "")
    domain = addr.split("@", 1)[1] if "@" in addr else ""
    # Candidats = adresses du pool de la MÊME marque (même domaine).
    # L'adresse exacte en fait partie ; on ne la privilégie pas, pour
    # répartir réellement le volume.
    candidates: list[str] = []
    for pool_addr, aid in (pool_addr_to_account or {}).items():
        a = (pool_addr or "").strip().lower()
        if not aid:
            continue
        if a == addr or (domain and a.endswith("@" + domain)):
            candidates.append(aid)
    if not candidates:
        return ("missing", "")
    available = []
    for aid in candidates:
        try:
            if int((pool_remaining or {}).get(aid, 0)) > 0:
                available.append(aid)
        except Exception:
            pass
    if not available:
        return ("cap", candidates[0])
    return ("ok", random.choice(available))


# ---------------------------------------------------------------------------
# Stats accumulées par run
# ---------------------------------------------------------------------------
@dataclass
class PipelineStats:
    started_at: str = ""
    finished_at: str = ""
    searched: int = 0
    # Fiches trouvées par la recherche mais écartées AVANT insertion :
    # sans adresse mail (politique du 11/06/2026 — une fiche muette ne
    # sert à rien, et le verrou base la refuserait de toute façon).
    skipped_no_email: int = 0
    # Fiches refusées par la base à l'insertion (verrou, contrainte…) :
    # sautées une par une, l'étape search continue (bug du 08/07/2026).
    search_rejected: int = 0
    enriched: int = 0
    enrich_emails_found: int = 0
    drafts_generated: int = 0
    drafts_sent: int = 0
    drafts_pending: int = 0
    follow_ups_sent: int = 0
    replies_detected: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers globaux pour l'auto-pilote v2 (etape 8)
# ---------------------------------------------------------------------------
def _is_within_send_window(cfg: "PipelineConfig") -> bool:
    """True si l'heure actuelle (Paris) est dans la plage d'envoi configuree.

    Gere le cas d'une plage qui passe minuit (ex: 22h-2h).
    """
    try:
        from zoneinfo import ZoneInfo
        h = datetime.now(ZoneInfo("Europe/Paris")).hour
    except Exception:
        h = datetime.now().hour
    start = max(0, min(23, int(getattr(cfg, "send_hour_start", 8))))
    end   = max(0, min(24, int(getattr(cfg, "send_hour_end",   19))))
    if start == end:
        return False
    if start < end:
        return start <= h < end
    return h >= start or h < end


def _resolve_review_min_score(raw) -> int:
    """Seuil effectif de la 2e IA de relecture.

    Convention a trois etats :
      - n > 0  : seuil choisi (note minimale pour autoriser l'envoi direct)
      - -1     : relecture VOLONTAIREMENT coupee (interrupteur Relit=Manuel
                 du tableau de commande) -> 0, pas de relecture
      - 0 / absent / invalide : vieille config sans le champ -> on restaure
                 le defaut 7 (filet anti-config-corrompue). Avant ce filet,
                 l'etape "Relit" restait silencieusement desactivee chez
                 Jordan a cause d'une config historique.
    """
    try:
        n = int(raw)
    except Exception:
        n = 0
    if n < 0:
        return 0
    if n == 0:
        return 7
    return min(10, n)


def _effective_template_brief(cfg: "PipelineConfig") -> str:
    """Consignes de redaction effectives : celles de la config si remplies,
    sinon le brief par defaut de PipelineConfig.

    La page Reglages du site sauvegarde la config entiere, champ vide
    compris : un brief efface ne doit pas priver l'IA libre du format
    strict "OBJET : ..." (sans lui, le sujet retombe sur un generique).
    """
    brief = (getattr(cfg, "ai_template_brief", "") or "").strip()
    if brief:
        return brief
    return PipelineConfig.__dataclass_fields__["ai_template_brief"].default


# Placeholders d'identite : si un modele les utilise, le prospect doit avoir
# un nom, sinon le mail part avec un trou ("pour une entreprise comme , ...").
_IDENTITY_PLACEHOLDERS = (
    "{prenom}", "{nom}", "{raison_sociale}",
    "{{first_name}}", "{{last_name}}", "{{name}}",
    "{{company_name}}", "{{company}}",
)


def _template_requires_identity(template: dict) -> bool:
    """True si le sujet ou le corps du modele cite le nom du prospect."""
    blob = ((template.get("subject") or "")
            + "\n" + (template.get("body_text") or "")
            + "\n" + (template.get("body_html") or ""))
    return any(ph in blob for ph in _IDENTITY_PLACEHOLDERS)


def _count_mails_sent_last_24h(sb_client) -> int:
    """Compte les envois (kind='email_sent') sur les 24 dernieres heures
    glissantes via Supabase. Renvoie 0 en cas d'erreur (fallback prudent :
    on n'empeche pas l'envoi si la base est down)."""
    if sb_client is None:
        return 0
    try:
        from datetime import timedelta
        since = (datetime.now(tz=None) - timedelta(hours=24)).isoformat()
        r = (sb_client.raw.table("email_history")
             .select("id", count="exact")
             .eq("kind", "email_sent")
             .gte("ts", since)
             .execute())
        return int(r.count or 0)
    except Exception as exc:
        logger.debug("count_mails_sent_last_24h: %s", exc)
        return 0


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
    sender_pool_smtp: dict | None = None,
    templates_override: list[dict] | None = None,
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
        log({"type": "activity", "message": "Je regarde les réponses entrantes dans la boîte mail..."})
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
        log({"type": "stage", "id": "search", "state": "running",
             "message": f"Je cherche de nouveaux prospects ({cfg.source})..."})
        log({"type": "activity", "message": f"Je vais piocher des prospects dans la source : {cfg.source}"})
        log(f"Étape 1/4 — Recherche {cfg.source}…")
        try:
            crm = _pipeline_crm(log)
            iterator, finalize = _search_iterator(cfg, log=log)
            # Versement UN PAR UN, sous blindage (cf _upsert_search_results) :
            # filtre les fiches sans mail en amont, et une fiche refusée par
            # la base ne tue plus toute l'étape (bug Éclaireur du 08/07/2026).
            r = _upsert_search_results(crm, iterator, log)
            crm.save()
            stats.searched = r.get("created", 0)
            stats.skipped_no_email = r.get("skipped_no_email", 0)
            stats.search_rejected = r.get("rejected", 0)
            log(f"  → {stats.searched} nouveau(x) prospect(s) ({r.get('merged', 0)} fusionnés)")
            _no_mail = (f" — {stats.skipped_no_email} ignoré(s) (sans adresse mail)"
                        if stats.skipped_no_email else "")
            log({"type": "stage_done", "id": "search", "count": stats.searched,
                 "message": f"{stats.searched} nouveau(x) prospect(s) ajouté(s) à la base{_no_mail}"})
            try:
                finalize()
            except Exception as e:
                logger.debug("search cursor finalize: %s", e)
        except Exception as e:
            stats.errors.append(f"search: {e}")
            log(f"  ⚠ {e}")
            log({"type": "stage_error", "id": "search", "message": str(e)})
    elif not do_search:
        log({"type": "stage_done", "id": "search", "count": 0,
             "message": "Cible : prospects existants dans le CRM (aucune nouvelle recherche)."})

    # ------------------------------------------------------------------
    # 2) Enrich (rattache au maillon "Cherche" cote UI)
    # ------------------------------------------------------------------
    if do_enrich:
        log({"type": "activity",
             "message": f"J'enrichis les fiches des prospects (max {cfg.enrich_max} sites visités)..."})
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
            sent, pending = _run_ai_outreach(
                cfg, log,
                sender_pool_smtp=sender_pool_smtp,
                templates_override=templates_override,
            )
            stats.drafts_generated = sent + pending
            stats.drafts_sent = sent
            stats.drafts_pending = pending
        except Exception as e:
            stats.errors.append(f"send: {e}")
            log(f"  ⚠ {e}")
            log({"type": "stage_error", "id": "write", "message": str(e)})
    else:
        log({"type": "stage_done", "id": "sort",   "count": 0,
             "message": "Tri en mode manuel — non lancé."})
        log({"type": "stage_done", "id": "write",  "count": 0,
             "message": "Rédaction en mode manuel — non lancée."})
        log({"type": "stage_done", "id": "review", "count": 0,
             "message": "Relecture non lancée (rédaction en manuel)."})
        log({"type": "stage_done", "id": "send",   "count": 0,
             "message": "Envoi non lancé (rédaction en manuel)."})

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


def _has_any_email(prospect) -> bool:
    """True si la fiche porte au moins UNE adresse mail non vide."""
    try:
        return any((e or "").strip() for e in (prospect.emails or []))
    except Exception:
        return False


def _upsert_search_results(crm, prospects, log: Callable[[str], None]) -> dict:
    """Verse les prospects trouvés par l'étape search dans le CRM, un par un.

    Remplace le `crm.upsert_many(iterator)` d'un bloc : depuis le verrou
    base `prospect_sans_email` (migration 47, 11/06/2026), la PREMIÈRE
    fiche sans mail rejetée faisait remonter l'exception et tuait TOUTE
    l'étape search (bug Éclaireur du 08/07/2026 : searched=0 en 11 s).
    Deux garde-fous, dans l'ordre :

    1. Filtre en amont : une fiche trouvée SANS adresse mail n'est pas
       envoyée à l'insertion. Politique maison (« une fiche muette ne sert
       à rien, tous les outils filtrent en amont ») — on l'écarte et on la
       compte au lieu de laisser la base la refuser.
    2. Blindage individuel : chaque insertion a son propre try/except.
       Si la base rejette UNE fiche (verrou prospect_sans_email, autre
       contrainte, hoquet réseau), on la saute et on continue — on ne tue
       JAMAIS toute l'étape pour une seule fiche.

    Renvoie {created, merged, skipped_no_email, rejected} — mêmes clés
    created/merged que crm.upsert_many(), plus les deux compteurs d'écart.
    """
    created = 0
    merged = 0
    skipped_no_email = 0
    rejected = 0
    for p in prospects:
        if not _has_any_email(p):
            skipped_no_email += 1
            continue
        try:
            _, is_new = crm.upsert(p)
        except Exception as exc:
            rejected += 1
            _msg = str(exc)
            # Verrou base « prospect_sans_email » (code SQL 23514) : message
            # dédié ; toute autre erreur d'insert est résumée telle quelle.
            _why = ("refusée par le verrou base (fiche sans adresse mail)"
                    if ("prospect_sans_email" in _msg or "23514" in _msg)
                    else _msg[:120])
            _name = (getattr(p, "name", "") or getattr(p, "legal_name", "")
                     or "(sans nom)")[:40]
            log(f"  [skip] fiche « {_name} » non insérée : {_why}")
            logger.debug("insert prospect rejeté (%s) : %s", _name, exc)
            continue
        if is_new:
            created += 1
        else:
            merged += 1
    if skipped_no_email:
        log(f"  → {skipped_no_email} ignoré(s) (sans adresse mail)")
    if rejected:
        log(f"  ⚠ {rejected} fiche(s) refusée(s) par la base — sautée(s), "
            f"l'étape continue")
    return {
        "created": created,
        "merged": merged,
        "skipped_no_email": skipped_no_email,
        "rejected": rejected,
    }


def _run_enrichment(cfg: PipelineConfig, log: Callable[[str], None]) -> tuple[int, int]:
    """Exécute l'enrichissement web. Renvoie (n_enriched, n_emails_found)."""
    from .enrichers.footprint import FootprintFinder
    from .enrichers.linktree import LinktreeFollower, is_hub
    from .enrichers.web import WebEnricher

    crm = _pipeline_crm(log)
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
                    _record_event(crm, prospect, {
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
                # On tag la provenance : "web" (ou "linktree" si hub agrégateur),
                # avec l'URL du site visité comme contexte. Ça permet à l'IA
                # de comprendre que cet email vient de la page contact /
                # mentions légales du site officiel.
                src_name = "linktree" if is_hub(url) else "web"
                ctx = ("hub Linktree / Beacons / etc." if src_name == "linktree"
                        else "page contact ou mentions légales du site officiel")
                for e in gained_emails:
                    prospect.add_email(e, source=src_name, url=url, context=ctx)
                # Garde la limite à 8 emails (sécurité historique)
                if len(prospect.emails) > 8:
                    keep = set(prospect.emails[:8])
                    prospect.emails = prospect.emails[:8]
                    prospect.emails_meta = [m for m in prospect.emails_meta
                                             if m.get("email") in keep]
                n_emails += len(gained_emails)
            if gained_phones:
                prospect.phones = (prospect.phones + gained_phones)[:5]
            if data["address"] and not prospect.address:
                prospect.address = data["address"]
            if data["has_legal_mentions"]:
                prospect.has_legal_mentions = True
            _record_event(crm, prospect, {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "kind": "web_enrich", "url": url,
            })
            prospect.sources.append(Source(
                name="linktree" if is_hub(url) else "web",
                source_id=norm_website(url), url=url,
            ))
            prospect.updated_at = datetime.now().isoformat(timespec="seconds")
            _persist_prospect(crm, prospect)
            n_enriched += 1
        except Exception as e:
            logger.debug("enrich %s : %s", prospect.name, e)
        if i % 10 == 0:
            log(f"  → {i}/{len(candidates)} traité(s)…")

    if hasattr(crm, "_rebuild_index"):
        crm._rebuild_index()  # noqa: SLF001
    crm.save()
    log(f"  → {n_enriched} enrichi(s), {n_emails} nouveau(x) email(s)")
    return n_enriched, n_emails


def _run_ai_outreach(
    cfg: PipelineConfig,
    log: Callable[[str], None],
    *,
    sender_pool_smtp: dict | None = None,
    templates_override: list[dict] | None = None,
) -> tuple[int, int]:
    """Génère mail IA personnalisé pour chaque prospect éligible, puis envoie OU draft.

    Renvoie (n_sent, n_pending).

    sender_pool_smtp : dict {account_id: smtp_cfg} optionnel. Si fourni ET
    que `cfg.autopilot_sender_pool` est non vide, l'envoi utilise le pool
    multi-adresses (tirage aléatoire respectant les caps 24h glissantes).
    Sinon : envoi mono-adresse via _load_smtp_config() (legacy).
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
    # Deux sources possibles de templates :
    #   1) templates_override (kwarg) : preferre. Construit par
    #      autopilot_runner a partir des produits ACTIFS du catalogue. Permet
    #      de combiner les templates de plusieurs produits actifs en une
    #      seule pool, l'IA picker choisira le bon par prospect.
    #   2) cfg.autopilot_product (legacy) : fallback si pas d'override. Pour
    #      compat avec d'anciens chemins (Eclaireur, runs manuels).
    # Si AUCUN des deux ne donne de template, on retombe sur la generation
    # libre IA (qui ecrira un mail propre sans s'appuyer sur un modele).
    templates_for_picking: list[dict] = []
    use_templates = False
    if templates_override:
        templates_for_picking = list(templates_override)
        use_templates = True
        # Diagnostic round-robin : si Jordan voit toujours le meme template
        # alors qu'il en a plusieurs, c'est souvent que ses templates ne
        # sont pas tous tagues a la bonne audience. On affiche le compte
        # par audience pour qu'il sache d'un coup d'oeil ce que l'autopilote
        # va trouver pour ses prospects (creator vs pro).
        _by_aud: dict[str, int] = {}
        for _t in templates_for_picking:
            _a = (_t.get("audience") or "creator").lower()
            _by_aud[_a] = _by_aud.get(_a, 0) + 1
        _aud_summary = ", ".join(f"{k}={v}" for k, v in sorted(_by_aud.items()))
        log(f"  -> mode templates (override catalogue) : "
            f"{len(templates_for_picking)} template(s) charge(s) "
            f"[{_aud_summary or 'audience non taguee'}].")
    elif (cfg.autopilot_product or "").strip():
        try:
            from triskell_command.integrations.prospection_templates import (
                list_prospection_templates,
            )
            # On charge TOUS les modèles du produit (créateurs ET pros) :
            # le filtre par catégorie se fait ensuite, prospect par prospect,
            # selon ce qu'est ce prospect spécifique (un YouTubeur ne reçoit
            # pas le même mail qu'un commerce SIRENE).
            templates_for_picking = list_prospection_templates(
                product=cfg.autopilot_product.strip(),
                audience="",   # vide = pas de filtre global
            ) or []
            use_templates = bool(templates_for_picking)
            log(f"  -> mode templates : {len(templates_for_picking)} template(s) "
                f"trouve(s) pour produit '{cfg.autopilot_product}' "
                f"(filtrage par categorie de prospect au moment du pick)")
            if not templates_for_picking:
                log(f"  [WARN] aucun template prospection pour '{cfg.autopilot_product}' "
                    f"-> fallback generation libre par l'IA")
        except ImportError as exc:
            log(f"  [WARN] mode templates indispo ({exc}) -> fallback generation libre")
        except Exception as exc:
            log(f"  [WARN] chargement templates plante ({exc}) -> fallback generation libre")
    else:
        log("  -> pas de produit selectionne et catalogue vide -> generation libre IA.")

    # Méga-prompts : on ne les charge PAS pour la rédaction de mails de
    # prospection. Ces prompts (Honnêteté brutale, Anti-hallucination, ...)
    # sont conçus pour le chat avec Claude — ils tutoient l'IA et lui
    # ordonnent de refuser quand elle manque d'info. Résultat : conflit
    # avec la consigne "VOUVOIEMENT obligatoire" du prompt prospection, ET
    # l'IA refuse d'écrire si le prospect n'a que nom+ville+SIREN. Dans le
    # doute, on n'en charge AUCUN ici, peu importe le contenu de
    # cfg.ai_mega_prompts (qui peut rester rempli pour d'autres usages).
    selected_megas = []

    log({"type": "stage", "id": "sort", "state": "running",
         "message": "Je trie les prospects pour ne garder que ceux à contacter..."})
    log({"type": "activity",
         "message": "Je trie : on garde ceux qui ont un mail et qu'on n'a jamais contactés..."})
    crm = _pipeline_crm(log)
    eligible = [
        p for p in crm.all()
        if p.emails
        and p.status in ("new", "qualified")
        and not any(h.get("kind") == "email_sent" for h in (p.history or []))
        and not p.pending_drafts  # pas déjà un draft en attente
    ]
    if not eligible:
        log(f"  → 0 prospect(s) éligible(s) (besoin email + jamais contacté)")
        log({"type": "stage_done", "id": "sort", "count": 0,
             "message": "Aucun prospect à contacter (besoin d'un email + jamais contacté)."})
        log({"type": "stage_done", "id": "write",  "count": 0, "message": "Rien à rédiger."})
        log({"type": "stage_done", "id": "review", "count": 0, "message": "Rien à relire."})
        log({"type": "stage_done", "id": "send",   "count": 0, "message": "Rien à envoyer."})
        return (0, 0)

    # Cap effectif = min entre :
    # - cfg.nightly_target : ce que Jordan a regle dans "Cherche-moi N / run"
    #   (0 ou non renseigne = pas de cap, on retombe sur daily_cap)
    # - cfg.daily_cap : plafond de securite global d'envoi sur 24h
    # - len(eligible) : on n'a pas plus de prospects que ca
    nightly_target = int(getattr(cfg, "nightly_target", 0) or 0)
    target_cap = nightly_target if nightly_target > 0 else cfg.daily_cap
    cap = min(target_cap, cfg.daily_cap, len(eligible))
    if nightly_target > 0:
        log(f"  → cap utilisateur (Cherche-moi {nightly_target}/run) "
            f"appliqué — on traitera {cap} prospect(s) max")
    log(f"  → {cap} prospect(s) éligibles, génération IA…")
    log({"type": "stage_done", "id": "sort", "count": cap,
         "message": f"{cap} prospect(s) prêts à recevoir un mail."})

    # Etape 8.A : filet anti-doublon Supabase (en plus du check local).
    # On essaie de recuperer le client Supabase pour pouvoir appeler
    # has_recent_send(forever=True, check_clients=True). Si pas dispo,
    # on tombe sur le seul filtre eligible[] (check local p.history).
    _sb_client = None
    _PS = None
    try:
        from triskell_command.integrations import prospect_status as _PS
        from ..db import get_client as _get_client
        _sb_client = _get_client()
        if _sb_client is None or not _sb_client.is_authenticated:
            _sb_client = None
    except Exception as _exc:
        logger.debug("anti-doublon supabase non dispo: %s", _exc)
        _sb_client = None

    # Etape 8.C : plafond global 24h glissantes (compte les vrais envois
    # deja partis depuis 24h, tous canaux confondus). On laisse `cap` (par
    # run) en plus comme garde-fou local. Si daily_cap est deja atteint,
    # on sort tout de suite -- inutile de generer des mails qu'on ne pourra
    # pas envoyer.
    _already_sent_24h = _count_mails_sent_last_24h(_sb_client)
    _remaining_quota = max(0, int(cfg.daily_cap) - _already_sent_24h)
    if _sb_client is not None and _remaining_quota <= 0:
        log(f"  [stop] plafond {cfg.daily_cap} mails/24h deja atteint "
            f"({_already_sent_24h} envoyes) -- aucun envoi cette nuit.")
        log({"type": "stage_done", "id": "write",  "count": 0,
             "message": f"Plafond {cfg.daily_cap} mails/24h déjà atteint."})
        log({"type": "stage_done", "id": "review", "count": 0, "message": "Pas de relecture."})
        log({"type": "stage_done", "id": "send",   "count": 0,
             "message": f"Quota journalier {cfg.daily_cap} atteint — rien envoyé."})
        return (0, 0)
    if _sb_client is not None:
        log(f"  -> quota restant 24h : {_remaining_quota} mails "
            f"(deja {_already_sent_24h}/{cfg.daily_cap})")

    n_sent = 0
    n_pending = 0
    n_reviewed = 0
    n_repaired = 0  # fiches réparées par la 3e IA (record_repair)
    # Round-robin sur les templates de prospection : sans ca, l'IA picker
    # de generate_message_from_templates pioche pour CHAQUE prospect le
    # template qu'elle juge le plus pertinent -> elle finit par toujours
    # choisir le meme. Jordan veut que les modeles tournent (3 prospects
    # avec 3 templates dispo = 3 templates differents utilises).
    # Compteur partage entre prospects, filtre par audience plus loin.
    _template_rr_idx = 0
    # Seuil de relecture a trois etats (cf _resolve_review_min_score) :
    # -1 = coupe par l'interrupteur Relit=Manuel ; 0/absent = vieille config
    # -> defaut 7 ; n > 0 = seuil choisi.
    _review_score_raw = getattr(cfg, "autopilot_review_min_score", None)
    _review_score = _resolve_review_min_score(_review_score_raw)
    try:
        if int(_review_score_raw or 0) == 0 and _review_score == 7:
            log("  -> [info] autopilot_review_min_score etait 0 ou absent -> "
                "force a 7 (defaut). La 2e IA va relire chaque mail.")
    except Exception:
        pass
    cfg.autopilot_review_min_score = _review_score
    review_enabled = _review_score > 0
    log(f"  -> relecture 2e IA : {'ACTIVE' if review_enabled else 'desactivee'} "
        f"(seuil={_review_score}/10)")

    # === Sender pool (Auto-pilote v2) ===
    # Si la config a un pool d'adresses (autopilot_sender_pool) ET que les
    # smtp configs ont ete pre-resolues, on active le mode multi-adresses :
    # a chaque mail, tirage aleatoire d'une adresse dont le cap 24h
    # glissantes n'est pas atteint. Sinon : envoi mono-adresse.
    raw_pool = list(getattr(cfg, "autopilot_sender_pool", None) or [])
    pool = []
    # ATTENTION : ne JAMAIS reutiliser le nom `cap` ici -- c'est le plafond
    # global du run (Cherche-moi N prospects), defini juste au-dessus du
    # for loop. L'ecraser ici reduisait le respect du "Cherche-moi" a peau
    # de chagrin (bug constate par Jordan : 2 demande, 7 ecrits).
    for entry in raw_pool:
        if not isinstance(entry, dict):
            continue
        aid = str(entry.get("account_id") or "").strip()
        acc_cap = int(entry.get("daily_cap") or 0)
        if aid and acc_cap > 0:
            pool.append({"account_id": aid, "daily_cap": acc_cap})
    use_pool = bool(pool and sender_pool_smtp)
    pool_tracker = None
    pool_counts_24h: dict[str, int] = {}
    if use_pool:
        try:
            from triskell_command.integrations import sender_pool_tracker as _spt
            pool_tracker = _spt
            pool_ids = [p["account_id"] for p in pool]
            pool_counts_24h = pool_tracker.count_sent_24h_by_account(pool_ids)
            log("  -> sender pool actif : "
                + ", ".join(f"{p['account_id']} (cap {p['daily_cap']}/24h, "
                            f"deja {pool_counts_24h.get(p['account_id'], 0)})"
                            for p in pool))
        except Exception as exc:
            log(f"  [WARN] sender pool indisponible ({exc}) -> mono-adresse")
            use_pool = False
            pool_tracker = None

    # Index adresse → compte du pool (câblage modèle→adresse). Une adresse
    # n'y figure que si son compte est dans le pool ET sa config SMTP est
    # résolue (donc réellement envoyable).
    pool_addr_to_account: dict[str, str] = {}
    if use_pool:
        _pool_ids = {p["account_id"] for p in pool}
        for _aid, _scfg in (sender_pool_smtp or {}).items():
            _addr = ((_scfg or {}).get("from_email") or "").strip().lower()
            if _addr and _aid in _pool_ids:
                pool_addr_to_account[_addr] = _aid

    log({"type": "stage", "id": "write", "state": "running", "count": 0,
         "message": "Je rédige les mails un par un..."})
    if review_enabled:
        log({"type": "stage", "id": "review", "state": "running", "count": 0,
             "message": "La 2è IA relit chaque mail avant validation..."})
    log({"type": "stage", "id": "send", "state": "running", "count": 0,
         "message": "J'attends qu'un mail soit prêt à partir..."})

    # Délivrabilité : adresses CONFIRMÉES d'abord (tri stable), les
    # devinées (contact@/info@…) en queue + quota d'envoi direct dédié.
    # ET, à qualité de mail égale, on contacte EN PRIORITÉ les « sites à
    # refaire » (tag site_a_refaire) : ce sont les prospects qui convertissent
    # le mieux — on a un vrai service à leur vendre (demande Jordan 15/06/2026).
    def _send_priority(p):
        is_redo = "site_a_refaire" in (getattr(p, "tags", None) or [])
        return (_email_is_guessed(p), 0 if is_redo else 1)
    eligible.sort(key=_send_priority)
    _guessed_quota = max(0, int(getattr(cfg, "guessed_daily_cap", 8) or 0))
    _guessed_sent = 0
    _n_guessed_selected = sum(1 for p in eligible[:cap] if _email_is_guessed(p))
    if _n_guessed_selected:
        log(f"  -> {_n_guessed_selected} adresse(s) devinée(s) dans le lot — "
            f"max {_guessed_quota} partiront en direct, le reste en brouillon")

    for i, prospect in enumerate(eligible[:cap], 1):
        _prospect_label = (prospect.name or prospect.legal_name or "(sans nom)")[:60]
        log({"type": "activity",
             "message": f"Je rédige le mail pour {_prospect_label}..."})
        try:
            # Anti-doublon Supabase : skip si deja contacte (forever) ou client
            _to_email = prospect.emails[0] if prospect.emails else ""
            if _sb_client is not None and _to_email and _PS is not None:
                _rec = _PS.has_recent_send(
                    _sb_client, email=_to_email,
                    forever=True, check_clients=True,
                )
                if _rec.get("recent"):
                    _why = ("deja client" if _rec.get("last_kind") == "client"
                            else "deja contacte")
                    log(f"  [skip] {prospect.name[:30]} : {_why} ({_to_email})")
                    log({"type": "prospect_touched", "id": getattr(prospect, "id", ""),
                         "name": _prospect_label, "action": "skipped",
                         "reason": _why})
                    # La fiche doit refleter la realite, sinon elle reste
                    # "new"/"qualified", re-rentre dans la selection a CHAQUE
                    # run et bouche le cap (famine constatee le 12/06/2026 :
                    # 5 fiches "deja contacte" en tete de file -> les
                    # prospects jamais traites derriere n'avaient JAMAIS
                    # leur tour). Adresse deja cliente -> "won" (plus jamais
                    # re-maile) ; adresse deja contactee -> "contacted" (le
                    # recyclage des dormants pourra la reprendre plus tard).
                    try:
                        prospect.status = ("won"
                                           if _rec.get("last_kind") == "client"
                                           else "contacted")
                        if _rec.get("last_ts"):
                            prospect.last_contact_at = _rec["last_ts"]
                        _persist_prospect(crm, prospect)
                        log(f"  -> fiche marquee '{prospect.status}' "
                            f"(ne bouchera plus la file)")
                    except Exception as _mark_exc:
                        log(f"  [WARN] marquage '{_why}' impossible "
                            f"({_mark_exc}) — la fiche restera dans la file")
                    continue

            # Filtrage des modèles par catégorie de CE prospect : un
            # YouTubeur n'a pas droit aux mails écrits pour des PME.
            # Convention : les modèles sans audience définie tombent dans
            # 'creator' (les 5 historiques Pixel Pros).
            prospect_audience = _detect_audience(prospect, cfg)
            templates_for_this_prospect = [
                t for t in templates_for_picking
                if (t.get("audience") or "creator").lower() == prospect_audience
            ] if use_templates else []
            # Affinage 'pro' : choisir la catégorie (commerce / artisan /
            # cabinet) selon le métier, pour ne pas envoyer un mail pensé
            # pour un commerce à un plombier. Repli sur toute l'audience si
            # le métier n'est pas classable (jamais de trou).
            if (use_templates and prospect_audience == "pro"
                    and templates_for_this_prospect):
                _cat = _pro_category(prospect.industry or "")
                if _cat:
                    _by_cat = [t for t in templates_for_this_prospect
                               if _cat in (t.get("key") or "").lower()]
                    if _by_cat:
                        templates_for_this_prospect = _by_cat

            _html_is_custom = False
            if use_templates and templates_for_this_prospect:
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
                # Round-robin : on pioche le template suivant dans la
                # liste filtree par audience (et on incremente l'index pour
                # le prochain prospect). Sans ca, le picker IA en aval
                # piocherait toujours le meme.
                _rr_pick = templates_for_this_prospect[
                    _template_rr_idx % len(templates_for_this_prospect)
                ]
                _template_rr_idx += 1
                # Garde-fou trou d'identite : si le modele cite le nom du
                # prospect ({{name}}, {raison_sociale}...) et que la fiche
                # n'a aucun nom, la substitution laisserait un trou ("pour
                # une entreprise comme , ..."). On saute ce prospect.
                if (_template_requires_identity(_rr_pick)
                        and not prospect_dict["raison_sociale"].strip()):
                    log(f"  [skip] (fiche sans nom) : le modele "
                        f"'{_rr_pick.get('key') or '?'}' cite le nom du "
                        f"prospect -> mail troue, prospect saute ce run")
                    log({"type": "prospect_touched",
                         "id": getattr(prospect, "id", ""),
                         "name": _prospect_label, "action": "skipped",
                         "reason": "fiche sans nom pour un modele nominatif"})
                    continue
                _rr_key = _rr_pick.get("key") or "(sans-cle)"
                log(f"  -> template a tour de role : '{_rr_key}' "
                    f"({_template_rr_idx}/{len(templates_for_this_prospect)} "
                    f"de l'audience '{prospect_audience}')")
                # On force ce template en ne passant QUE celui-la a
                # generate_message_from_templates : court-circuit du picker
                # IA (un seul choix possible). Cela fait aussi gagner un
                # appel IA par prospect.
                tp_label = (
                    cfg.autopilot_product.strip()
                    or (_rr_pick.get("product_label") or "")
                    or "(catalogue actif)"
                )
                gen = generate_message_from_templates(
                    prospect_dict,
                    templates=[_rr_pick],
                    template_product=tp_label,
                    sender_name=cfg.sender_mon_prenom or "",
                    user_brief=_effective_template_brief(cfg),
                    provider=cfg.ai_provider,
                    model=cfg.ai_model,
                    api_keys=api_keys,
                )
                subject  = gen.get("subject") or ""
                body     = gen.get("body") or ""
                body_html = gen.get("body_html") or ""
                _html_is_custom = bool(gen.get("html_is_custom"))
                _tpl_key = gen.get("template_key") or "auto"
                # Câblage modèle→adresse : le modèle peut exiger SON
                # adresse d'expéditeur. Mémorisée ici, appliquée au
                # moment du choix d'adresse (et stockée dans le brouillon).
                _tpl_from = (_rr_pick.get("from_address") or "").strip()
            else:
                # === Mode classique : generation libre par l'IA ===
                # On y arrive soit en l'absence totale de modèles, soit
                # quand AUCUN modèle ne correspond à la catégorie de ce
                # prospect en particulier (ex : tu n'as encore aucun modèle
                # 'pro' alors que ce prospect est une PME).
                if use_templates and not templates_for_this_prospect:
                    log(f"  [info] aucun modele '{prospect_audience}' pour "
                        f"'{prospect.name[:30]}' -> redaction libre par l'IA")
                user_prompt = _build_personalized_prompt(prospect, cfg)
                full = build_ultimate_prompt(user_prompt, selected_megas)
                # Bascule auto entre IA : si l'IA préférée est à sec (plus de
                # crédit / coupure), on rédige avec une autre IA enregistrée.
                response, _gen_prov, _gen_model = ai_providers.send_with_fallback(
                    cfg.ai_provider, cfg.ai_model, full, api_keys,
                )
                subject, body = _parse_ai_response(response, prospect, cfg)
                body_html = ""  # genere apres signature (cf. plus bas)
                _tpl_key = "ai_pipeline"
                _tpl_from = ""  # generation libre : pas d'adresse exigee

            # === Garde-fou anti-placeholder oublie (regle absolue Jordan) ===
            # Si un {variable} ou {{variable}} traine dans le sujet ou le
            # corps (substitution incomplete, template casse, IA qui a
            # ajoute son propre placeholder), le mail ne doit PAS partir.
            # On jette le mail et on saute le prospect ce run.
            _orphan = _has_unfilled_placeholder(subject, body)
            if _orphan:
                log(f"  [skip] {prospect.name[:30]} : placeholder oublie "
                    f"'{_orphan}' dans le mail -> jete, prospect non "
                    f"contacte ce run (regle anti-placeholder)")
                log({"type": "prospect_touched", "id": getattr(prospect, "id", ""),
                     "name": _prospect_label, "action": "skipped",
                     "reason": f"placeholder oublie : {_orphan}"})
                continue

            # === Detection refus IA ===
            # Si l'IA a refuse d'ecrire et a dump sa meta-analyse a la place
            # ("PROBLEME MAJEUR", "Je ne peux pas rediger", "impossible de
            # rediger", "Aucune info exploitable", ...), on skip le prospect
            # plutot que de stocker un draft inutilisable.
            if _looks_like_ai_refusal(body):
                log(f"  [skip] {prospect.name[:30]} : l'IA a refusé d'écrire "
                    f"(probablement trop peu d'info sur le prospect)")
                log({"type": "prospect_touched", "id": getattr(prospect, "id", ""),
                     "name": _prospect_label, "action": "skipped",
                     "reason": "IA a refuse d'ecrire (trop peu d'info)"})
                continue

            # Brouillon redige -> on incremente le compteur "write"
            log({"type": "stage", "id": "write", "state": "running",
                 "count": n_sent + n_pending + 1,
                 "message": f"Dernier mail rédigé : {_prospect_label}"})

            # === Etape 7 : 2e IA de relecture ===
            # Si autopilot_review_min_score > 0, on relit le mail et on
            # decide envoi vs draft selon la note. Sinon : comportement
            # actuel (cfg.mode decide tout).
            effective_mode = cfg.mode
            # Capture la review pour la stocker dans le brouillon : Jordan
            # veut voir la note + le commentaire dans l'onglet Brouillons
            # pour trier vite (les douteux passent en revue, les bons sont
            # valides sans relire).
            review_for_draft: dict | None = None
            _applied = False            # une retouche de la 2e IA a-t-elle ete appliquee ?
            _orig_body_for_html = None  # texte AVANT retouche (pour la reporter dans le HTML)
            if review_enabled:
                # La 2e IA ne fait que NOTER/relire le mail : un petit modèle
                # suffit largement. On force le modèle économique du provider
                # (Haiku pour Anthropic) au lieu du modèle de rédaction ->
                # même qualité de tri, coût par mail nettement réduit.
                _review_model = (ai_providers.cheap_model_for(cfg.ai_provider)
                                 or cfg.ai_model)
                log({"type": "activity",
                     "message": f"La 2è IA relit le mail pour {_prospect_label}..."})
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
                        model=_review_model,
                        api_keys=api_keys,
                        audience=prospect_audience,
                    )
                    _engine_down = bool(review.get("engine_down"))
                    review_for_draft = {
                        "score":   int(review.get("score") or 0),
                        "verdict": str(review.get("verdict") or ""),
                        "comment": str(review.get("comment") or "")[:300],
                        "engine_down": _engine_down,
                    }
                    if _engine_down:
                        # Panne du correcteur : aucune IA n'a pu noter. On NE
                        # compte pas ça comme une relecture et on signale clair
                        # (pas de faux « note X/10 »).
                        log(f"  [review] {prospect.name[:30]} : ⚙️ correcteur en "
                            f"panne -- {review['comment'][:80]}")
                        log({"type": "stage", "id": "review", "state": "running",
                             "count": n_reviewed,
                             "message": "⚙️ 2è IA en panne (aucune IA disponible) — "
                                        f"{_prospect_label} gardé en brouillon"})
                    else:
                        log(f"  [review] {prospect.name[:30]} : "
                            f"score={review['score']}/10 verdict={review['verdict']} "
                            f"-- {review['comment'][:80]}")
                        n_reviewed += 1
                        log({"type": "stage", "id": "review", "state": "running",
                             "count": n_reviewed,
                             "message": f"Dernier mail relu : {_prospect_label} (note {review['score']}/10)"})

                    # === Micro-retouche par la 2e IA (autorisee UNE seule fois) ===
                    # Demande de Jordan (17/06/2026) : la 2e IA a le droit de
                    # proposer UNE petite retouche (1-2 phrases), on l'applique,
                    # puis on RELIT et on RENOTE -- une seule fois (pas de boucle).
                    # On ne garde la retouche que si elle AMELIORE (ou egale) la
                    # note ; sinon on revient au mail d'origine. Vaut AUSSI pour
                    # les mails issus d'un template (l'ancienne regle « ne touche
                    # pas aux modeles » est levee pour cette retouche unique).
                    _score_before = int(review.get("score") or 0)
                    _score_after = None
                    _modif_type = str(review.get("modif_type") or "").strip()[:80]
                    _applied = False
                    _revised = (review.get("body_revised") or "").strip()
                    if _revised and not _engine_down and _revised != (body or "").strip():
                        # Decision de Jordan (17/06/2026) : si la 2e IA repere une
                        # retouche, on l'APPLIQUE -- meme si la note ne monte pas
                        # (une amelioration de style ne se voit pas toujours dans
                        # la note sur 10). On relit quand meme la version retouchee
                        # pour afficher la nouvelle note (avant -> apres), en toute
                        # transparence : si elle baisse, Jordan le voit et tranche
                        # a la validation manuelle.
                        _orig_body_for_html = body
                        body = _revised
                        _applied = True
                        log(f"    -> retouche appliquee ({_modif_type or 'retouche'}), "
                            "relecture pour la nouvelle note...")
                        try:
                            review2 = review_email(
                                subject=subject, body=_revised,
                                prospect_context=ctx,
                                provider=cfg.ai_provider, model=_review_model,
                                api_keys=api_keys, audience=prospect_audience,
                            )
                        except Exception:
                            review2 = {"engine_down": True}
                        if not review2.get("engine_down"):
                            _score_after = int(review2.get("score") or 0)
                            review_for_draft["score"]   = _score_after
                            review_for_draft["verdict"] = str(
                                review2.get("verdict") or review_for_draft["verdict"])
                            review_for_draft["comment"] = str(
                                review2.get("comment") or review_for_draft["comment"])[:300]
                            log(f"    -> note {_score_before} -> {_score_after}")
                        else:
                            log("    -> nouvelle note indisponible (IA indispo), "
                                "retouche gardee, ancienne note conservee")
                    # Avant/apres + type de retouche, pour l'afficher sur le brouillon.
                    review_for_draft["score_before"]  = _score_before
                    review_for_draft["score_after"]   = _score_after
                    review_for_draft["modif_type"]    = _modif_type
                    review_for_draft["modif_applied"] = _applied

                    # === Decision envoi vs brouillon (sur la note FINALE) ===
                    if (review_for_draft["verdict"] == "draft"
                            or int(review_for_draft["score"] or 0)
                               < cfg.autopilot_review_min_score):
                        effective_mode = MODE_VALIDATION
                        if not _engine_down:
                            log("    -> force en brouillon (score insuffisant)")

                    # Trace la review dans l'historique du prospect. Via le
                    # CRM partagé, elle part dans email_history → le
                    # compteur "Relit" du tableau de commande remonte ENFIN
                    # les vrais chiffres (il restait à 0 avant ce branchement).
                    _record_event(crm, prospect, {
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "kind": "email_reviewed",
                        "score": review_for_draft["score"],
                        "verdict": review_for_draft["verdict"],
                        "comment": review_for_draft["comment"],
                        "edited": _applied,
                    })
                except Exception as exc:
                    log(f"  [WARN] reviewer plante ({exc}) -> on envoie sans relecture")

            # === Étape 7.bis : la 3e IA répare la FICHE si c'est elle la cause ===
            # Un mail mis en brouillon par la relecture vient parfois d'une
            # fiche sale (nom pollué par la description collée, secteur mal
            # classé → mauvaise démo). Avant : le brouillon fautif restait et
            # le même mail cassé revenait à chaque passage. Maintenant : on
            # répare la fiche (données INTERNES uniquement — name/industry/
            # description —, JAMAIS d'enrichissement extérieur, règle Jordan),
            # on NE crée PAS le brouillon fautif, et le mail est régénéré
            # proprement au prochain passage. Garde-fous : une seule tentative
            # par fiche (tag), et tout échec retombe sur le brouillon normal.
            if (review_for_draft is not None
                    and review_for_draft.get("verdict") == "draft"
                    and not review_for_draft.get("engine_down")
                    and "fiche_reparee" not in (prospect.tags or [])):
                try:
                    from .record_repair import (
                        REPAIR_TAG, name_looks_polluted, split_polluted_name,
                        propose_repair)
                    patch: dict = {}
                    # 1) Heuristique gratuite : nom pollué -> découpage net.
                    if name_looks_polluted(prospect.name or ""):
                        parts = split_polluted_name(prospect.name or "")
                        if parts:
                            patch["name"] = parts[0]
                            if not (prospect.description or "").strip():
                                patch["description"] = parts[1]
                    # 2) Sinon, la 3e IA diagnostique la fiche elle-même.
                    if not patch:
                        rep = propose_repair(
                            name=prospect.name or "",
                            industry=prospect.industry or "",
                            description=prospect.description or "",
                            city=prospect.city or "",
                            provider=cfg.ai_provider,
                            model=cfg.ai_model,
                            api_keys=api_keys,
                        )
                        if rep:
                            if rep.get("name"):
                                patch["name"] = rep["name"]
                            if rep.get("industry"):
                                patch["industry"] = rep["industry"]
                    if patch:
                        _old_name = prospect.name
                        for _k, _v in patch.items():
                            setattr(prospect, _k, _v)
                        prospect.tags = list(prospect.tags or []) + [REPAIR_TAG]
                        _persist_prospect(crm, prospect)
                        _record_event(crm, prospect, {
                            "ts": datetime.now().isoformat(timespec="seconds"),
                            "kind": "record_repaired",
                            "fields": sorted(patch.keys()),
                            "before_name": (_old_name or "")[:120],
                        })
                        n_repaired += 1
                        log(f"  🔧 fiche réparée ({', '.join(sorted(patch))}) "
                            f"-> pas de brouillon fautif, le mail sera réécrit "
                            f"au prochain passage avec la fiche propre")
                        continue
                except Exception as exc:
                    log(f"  [WARN] réparation de fiche impossible ({exc}) "
                        f"-> brouillon classique")

            # Délivrabilité : adresse devinée au-delà du quota -> brouillon.
            # Les confirmées sont passées d'abord (tri en amont), donc on
            # n'ampute jamais le volume des bonnes adresses.
            if effective_mode == MODE_AUTO and _email_is_guessed(prospect):
                if _guessed_sent >= _guessed_quota:
                    effective_mode = MODE_VALIDATION
                    log(f"  -> adresse devinée (quota {_guessed_quota}/run "
                        f"atteint) -> brouillon")

            # Etape 8.B : plage horaire d'envoi (Paris). Hors plage -> draft.
            if effective_mode == MODE_AUTO and not _is_within_send_window(cfg):
                effective_mode = MODE_VALIDATION
                log(f"  -> hors plage horaire ({cfg.send_hour_start}h-"
                    f"{cfg.send_hour_end}h) -> brouillon au lieu d'envoi direct")

            # Etape 8.C : plafond 24h glissantes. Si on a deja atteint le
            # quota au cours de la boucle, on bascule en draft pour le reste.
            if effective_mode == MODE_AUTO and _sb_client is not None:
                if n_sent >= _remaining_quota:
                    effective_mode = MODE_VALIDATION
                    log(f"  -> plafond {cfg.daily_cap} mails/24h atteint "
                        f"-> brouillon (relance demain)")

            # === Choix de l'adresse expeditrice ===
            # 1) Câblage modèle→adresse : si le modèle exige une adresse
            #    (champ « Expéditeur (adresse) »), le mail part par CE
            #    compte du pool — ou devient un brouillon. JAMAIS par une
            #    autre adresse (fini les mails Pixel Pros signés Lagriffe).
            # 2) Sinon, mode pool : tirage aleatoire dans le pool, en
            #    respectant les caps 24h glissantes. Pool sature -> draft.
            #    Mode mono : "primary" (legacy).
            sender_account_id = "primary"
            sender_smtp_cfg = None
            _route, _route_aid = _route_for_template_address(
                _tpl_from, pool_addr_to_account,
                {p["account_id"]: p["daily_cap"]
                                  - pool_counts_24h.get(p["account_id"], 0)
                 for p in pool} if use_pool else {},
            )
            if _route in ("ok", "cap"):
                # Compte exigé identifié : c'est lui qui signe aussi le
                # brouillon éventuel (cohérence adresse ↔ signature).
                sender_account_id = _route_aid
                sender_smtp_cfg = dict(
                    (sender_pool_smtp or {}).get(_route_aid) or {}) or None
            if effective_mode == MODE_AUTO and _route == "missing":
                effective_mode = MODE_VALIDATION
                log(f"  -> le modele exige l'adresse '{_tpl_from}' mais elle "
                    f"n'est pas dans le pool d'envoi de l'Auto-pilote -> "
                    f"brouillon (jamais une autre adresse)")
            elif effective_mode == MODE_AUTO and _route == "cap":
                effective_mode = MODE_VALIDATION
                log(f"  -> l'adresse '{_tpl_from}' exigee par le modele est "
                    f"au plafond 24h -> brouillon (jamais une autre adresse)")
            elif effective_mode == MODE_AUTO and _route == "ok":
                if not sender_smtp_cfg:
                    # Defense : ne doit pas arriver (l'index est construit
                    # depuis les SMTP resolus), mais jamais d'envoi par une
                    # autre adresse que celle exigee.
                    effective_mode = MODE_VALIDATION
                    log(f"  -> config SMTP du compte '{sender_account_id}' "
                        f"absente -> brouillon")
                else:
                    log(f"  -> adresse exigee par le modele : envoi via "
                        f"'{sender_account_id}' ({_tpl_from})")
            elif effective_mode == MODE_AUTO and use_pool and pool_tracker is not None:
                pool_with_remaining = [
                    {"account_id": p["account_id"],
                     "daily_cap":  max(0, p["daily_cap"]
                                       - pool_counts_24h.get(p["account_id"], 0))}
                    for p in pool
                ]
                # pool_with_remaining porte DÉJÀ la marge restante (cap - envois
                # 24h init base + incréments in-run). On tire donc directement
                # parmi les boîtes à marge > 0. Repasser par
                # pick_random_available_account relirait la base et
                # re-soustrairait les envois -> DOUBLE COMPTAGE (boîtes vues
                # « pleines » à la moitié du cap : 5 au lieu de 10).
                import random as _rnd_pool
                _avail_boxes = [p for p in pool_with_remaining
                                if int(p.get("daily_cap") or 0) > 0]
                chosen = _rnd_pool.choice(_avail_boxes) if _avail_boxes else None
                if chosen is None:
                    # Toutes les adresses du pool sont saturees -> brouillon
                    effective_mode = MODE_VALIDATION
                    log("  -> toutes les adresses du pool sont au plafond 24h "
                        "-> brouillon (relance demain)")
                else:
                    sender_account_id = chosen["account_id"]
                    sender_smtp_cfg = (sender_pool_smtp or {}).get(sender_account_id)
                    if sender_smtp_cfg is None:
                        # Config SMTP manquante pour ce compte -> brouillon
                        effective_mode = MODE_VALIDATION
                        log(f"  -> config SMTP du compte '{sender_account_id}' "
                            f"absente -> brouillon")

            # Filet identité multi-boîtes : avec un pool configuré, 'primary' ne
            # doit JAMAIS porter le mail (sinon signature manquante, ou en local
            # desktop la signature PERSO de Jordan collée sur un mail « nous »).
            # - Un envoi AUTO qui retomberait sur 'primary' (pool en erreur au
            #   tick) passe en brouillon : jamais un envoi non signé / mal signé.
            # - Tout brouillon reçoit une vraie boîte du pool (adresse + signature
            #   de la BONNE personne), même hors plafond (on n'envoie pas ici) :
            #   la boîte est choisie de façon stable par prospect (réparti).
            if pool and sender_account_id == "primary":
                if effective_mode == MODE_AUTO:
                    effective_mode = MODE_VALIDATION
                    log("  -> pool configuré mais aucune boîte résolue -> "
                        "brouillon (jamais un envoi 'primary')")
                # Clé de répartition stable : le VRAI id du prospect (row id
                # de la base partagée), sinon son email — jamais vide, sinon
                # tout le monde tombe sur pool[0] (même boîte / même personne).
                _pid_pick = _prospect_row_id(crm, prospect)
                if not _pid_pick:
                    try:
                        _pid_pick = (prospect.emails[0] or "").strip().lower()
                    except Exception:
                        _pid_pick = ""
                try:
                    import hashlib as _hl
                    _pick_i = (int(_hl.md5(_pid_pick.encode("utf-8"))
                                   .hexdigest()[:8], 16)
                               if _pid_pick else 0) % len(pool)
                except Exception:
                    _pick_i = 0
                sender_account_id = pool[_pick_i]["account_id"]
                _sc = (sender_pool_smtp or {}).get(sender_account_id)
                if _sc:
                    sender_smtp_cfg = _sc

            # Marque par boîte : les modèles sont rédigés « Triskell Studio »
            # (marque mère). Une boîte d'une autre marque maison (WoW, RankUs,
            # La Griffe) envoie sous SON nom -> on échange la marque dans le
            # corps déjà rédigé. Le CTA, le lien et l'offre Pixel Pros ne
            # bougent pas (Pixel Pros reste le produit proposé). Fait APRÈS la
            # relecture 2e IA (qui a vu un texte propre) et juste avant la
            # signature (elle aussi propre à la boîte, via account_id).
            _BOX_BRAND = {
                "wow":      "Studio WoW",
                "rankus":   "RankUs Studio",
                "lagriffe": "Lagriffe Studio",
            }
            _brand = _BOX_BRAND.get(sender_account_id, "")
            if _brand:
                body = ((body or "").replace("TRISKELL STUDIO", _brand.upper())
                                    .replace("Triskell Studio", _brand))
                if body_html:
                    body_html = (body_html.replace("TRISKELL STUDIO", _brand.upper())
                                          .replace("Triskell Studio", _brand))

            # Signature auto : la boîte expéditrice ajoute sa signature avant
            # envoi (et avant stockage en draft, pour que la validation montre
            # bien le mail final). L'IA s'arrête à "Cordialement, {prénom}" ;
            # la signature complète est collée derrière. En mode pool, on prend
            # la signature liee au compte choisi.
            try:
                from triskell_command.integrations.signatures import (
                    append_signature_to_body,
                )
                body = append_signature_to_body(body, account_id=sender_account_id)
            except Exception as _sig_exc:
                log(f"  [WARN] signature non ajoutée ({_sig_exc})")
            # Même signature côté HTML de MODÈLE : append_signature_to_body ne
            # touche que le texte. Sans ça, un modèle HTML dont on a retiré la
            # signature (envoi multi-boîtes signé par personne) partirait sans
            # signature chez Gmail. Injecté ici, une fois la boîte choisie.
            if _html_is_custom and body_html:
                try:
                    from triskell_command.integrations.signatures import (
                        append_signature_to_html,
                    )
                    body_html = append_signature_to_html(
                        body_html, account_id=sender_account_id)
                except Exception as _sigh_exc:
                    log(f"  [WARN] signature HTML non ajoutée ({_sigh_exc})")

            # === Mise en forme HTML legere (Auto-pilote v2) ===
            # Le HTML est (re)genere a partir du texte SIGNE, sauf si le
            # modele apporte son propre HTML ecrit a la main (_html_is_custom).
            # Avant : en mode template sans HTML custom, le HTML etait fige
            # AVANT l'ajout de la signature -> Gmail (qui affiche le HTML)
            # montrait le mail sans signature alors que la version texte
            # l'avait.
            if not _html_is_custom:
                try:
                    from triskell_command.integrations.convoy_ai import (
                        _first_url_in, text_to_email_html,
                    )
                    body_html = text_to_email_html(
                        body, sender_name=cfg.sender_mon_prenom or "",
                        # Bouton CTA en bas uniquement en mode modele (meme
                        # rendu qu'avant), pas en generation libre.
                        primary_url=(_first_url_in(body) if use_templates else ""),
                        primary_label="En savoir plus",
                    )
                except Exception as _exc:
                    logger.debug("text_to_email_html indispo: %s", _exc)
                    body_html = ""

            # Retouche dans un HTML de MODELE (avec apercu du site) : on ne
            # REGENERE PAS le HTML (ca ferait sauter l'apercu) -> on remplace
            # juste la phrase retouchee SUR PLACE. Introuvable -> on ne touche
            # a rien (l'apercu reste). (Jordan, 17/06/2026.)
            if _applied and _html_is_custom and body_html and _orig_body_for_html:
                try:
                    from .quality_reviewer import apply_retouche_to_html
                    body_html, _ = apply_retouche_to_html(
                        _orig_body_for_html, body, body_html)
                except Exception as _exc:
                    logger.debug("retouche-in-place HTML KO: %s", _exc)

            if effective_mode == MODE_AUTO:
                log({"type": "activity",
                     "message": f"J'envoie le mail à {_prospect_label}..."})
                # Envoi direct via SMTP. En mode pool, on a deja resolu
                # sender_smtp_cfg via le pool ; sinon on tombe sur le compte
                # principal (legacy).
                from .outreach.smtp_sender import (
                    _load_smtp_config, prospection_headers, send_email,
                )
                smtp_cfg = sender_smtp_cfg if sender_smtp_cfg else _load_smtp_config()
                _to_addr = prospect.emails[0]
                # Vrai id (base partagée) pour un lien de désinscription complet.
                _pid = _prospect_row_id(crm, prospect)
                # Pied de désinscription cliquable (texte + HTML), lien signé
                # propre au destinataire. Sans casser si le module est absent.
                _send_body, _send_html = body, body_html
                try:
                    from triskell_command.integrations import unsubscribe as _unsub
                    _send_body, _send_html = _unsub.inject_footer(
                        body, body_html, _to_addr, _pid)
                except Exception as _u_exc:
                    logger.debug("inject_footer KO: %s", _u_exc)
                msg_id = send_email(
                    smtp_cfg, to=_to_addr,
                    subject=subject, body=_send_body, body_html=_send_html,
                    custom_headers=prospection_headers(
                        (smtp_cfg or {}).get("from_email", ""),
                        to_email=_to_addr, prospect_id=_pid),
                )
                if _email_is_guessed(prospect):
                    _guessed_sent += 1
                _record_event(crm, prospect, {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "kind": "email_sent",
                    "to": prospect.emails[0],
                    "subject": subject,
                    "template_key": _tpl_key,
                    "message_id": msg_id,
                    "generated_by": f"{cfg.ai_provider}/{cfg.ai_model}",
                    "account_id": sender_account_id,
                    "from": (smtp_cfg or {}).get("from_email", ""),
                })
                prospect.status = "contacted"
                prospect.last_contact_at = datetime.now().isoformat(timespec="seconds")
                _persist_prospect(crm, prospect)
                # Incremente le cache pool pour le prochain tirage (evite de
                # re-requeter Supabase a chaque mail).
                if use_pool:
                    pool_counts_24h[sender_account_id] = (
                        pool_counts_24h.get(sender_account_id, 0) + 1
                    )
                n_sent += 1
                log({"type": "stage", "id": "send", "state": "running",
                     "count": n_sent,
                     "message": f"Dernier mail envoyé : {_prospect_label}"
                                + (f" (via {sender_account_id})" if use_pool else "")})
                log({"type": "prospect_touched", "id": getattr(prospect, "id", ""),
                     "name": _prospect_label, "action": "sent",
                     "reason": f"envoyé à {prospect.emails[0]}"
                               + (f" depuis {sender_account_id}" if use_pool else "")})
                # Delai anti-cadence entre 2 envois reussis : etale la
                # cadence pour proteger la reputation des boites mail
                # (anti-flag spam). 0 = pas de delai (legacy).
                _delay = int(getattr(cfg, "send_delay_seconds", 0) or 0)
                if _delay > 0:
                    import time as _time
                    _time.sleep(_delay)
            else:
                # Mode SAS : on dépose le draft pour validation manuelle
                _draft_payload = {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "kind": "first_contact",
                    "subject": subject,
                    "body": body,
                    "body_html": body_html,
                    "template_key": _tpl_key,
                    "provider": cfg.ai_provider,
                    "model": cfg.ai_model,
                }
                # Adresse d'expéditeur du brouillon : l'adresse EXIGÉE par le
                # modèle si présente, sinon la boîte du pool choisie ci-dessus
                # (filet identité). À la validation, le mail part depuis CE
                # compte — donc la bonne personne, avec la signature déjà posée
                # dans le corps. Sans ça, un brouillon 'primary' repartait non
                # signé (câblage modèle→adresse : l'exigence suit le brouillon).
                _draft_from = _tpl_from
                if not _draft_from and sender_account_id != "primary":
                    _draft_from = ((sender_pool_smtp or {})
                                   .get(sender_account_id, {}) or {}).get("from_email") or ""
                    if not _draft_from:
                        # SMTP du pool pas résolu au tick (hoquet base, secrets
                        # pas encore synchro) : on retrouve QUAND MÊME l'adresse
                        # de la boîte (mail_accounts) pour que le brouillon parte
                        # de la BONNE personne à la validation. Sinon il porterait
                        # la signature de X mais partirait de la boîte principale.
                        try:
                            from triskell_command.integrations import (
                                shared_secrets as _ss,
                            )
                            _acc = _ss.get_account_by_id(sender_account_id)
                            _draft_from = ((_acc or {}).get("from_email") or "")
                        except Exception:
                            _draft_from = ""
                if _draft_from:
                    _draft_payload["sender_address"] = _draft_from
                # Embarque la note + le commentaire de la 2e IA dans le
                # brouillon pour que l'UI les affiche cote validation
                # manuelle (Jordan voit en un coup d'oeil les mails surs
                # vs ceux qui meritent une relecture humaine attentive).
                if review_for_draft:
                    if review_for_draft.get("engine_down"):
                        # Panne technique du correcteur : on garde le brouillon
                        # (sécurité) mais on n'enregistre PAS un faux 0/10. Le
                        # verdict spécial « engine_down » fait afficher « 2è IA
                        # en panne » côté écran (note volontairement vide).
                        _draft_payload["review_score"]   = None
                        _draft_payload["review_verdict"] = "engine_down"
                        _draft_payload["review_comment"] = review_for_draft["comment"]
                    else:
                        _draft_payload["review_score"]   = review_for_draft["score"]
                        _draft_payload["review_verdict"] = review_for_draft["verdict"]
                        _draft_payload["review_comment"] = review_for_draft["comment"]
                        # Retouche unique de la 2e IA (avant/apres + type) pour
                        # l'afficher dans « Brouillons a valider ».
                        _draft_payload["review_score_before"]  = review_for_draft.get("score_before")
                        _draft_payload["review_score_after"]   = review_for_draft.get("score_after")
                        _draft_payload["review_modif_type"]    = review_for_draft.get("modif_type") or ""
                        _draft_payload["review_modif_applied"] = bool(review_for_draft.get("modif_applied"))
                # Base partagée d'abord : le brouillon part dans la table
                # prospect_drafts → visible dans « Brouillons à valider »
                # du site. Sinon (hors-ligne) : stockage local historique.
                if not _store_validation_draft(crm, prospect, _draft_payload):
                    prospect.pending_drafts.append(_draft_payload)
                prospect.status = "qualified"  # garde "qualified" tant que pas validé
                _persist_prospect(crm, prospect)
                n_pending += 1
                log({"type": "stage", "id": "send", "state": "running",
                     "count": n_sent + n_pending,
                     "message": f"Dernier brouillon posé : {_prospect_label}"})
                log({"type": "prospect_touched", "id": getattr(prospect, "id", ""),
                     "name": _prospect_label, "action": "draft",
                     "reason": "mis en brouillon pour validation manuelle"})
        except Exception as e:
            log(f"  ⚠ {prospect.name[:30]} : {e}")

        if i % 5 == 0:
            log(f"    {i}/{cap}…")

    crm.save()
    log(f"  → {n_sent} envoyé(s), {n_pending} en attente de validation")
    log({"type": "stage_done", "id": "write", "count": n_sent + n_pending,
         "message": f"{n_sent + n_pending} mail(s) rédigé(s)."})
    if review_enabled:
        _msg_review = f"{n_reviewed} mail(s) relu(s) par la 2è IA."
        if n_repaired:
            _msg_review += (f" {n_repaired} fiche(s) réparée(s) par la 3e IA"
                            f" (mail réécrit au prochain passage).")
        log({"type": "stage_done", "id": "review", "count": n_reviewed,
             "message": _msg_review})
    else:
        log({"type": "stage_done", "id": "review", "count": 0,
             "message": "Relecture désactivée."})
    log({"type": "stage_done", "id": "send", "count": n_sent + n_pending,
         "message": f"{n_sent} envoyé(s), {n_pending} en brouillon."})
    return n_sent, n_pending


def _has_unfilled_placeholder(*texts: str) -> str:
    """Renvoie le 1er placeholder non rempli trouve dans les textes, sinon "".

    Regle absolue Jordan : DANS TOUS LES CAS, si un placeholder reste dans
    le sujet ou le corps du mail (variable oubliee, template casse,
    substitution incomplete), le mail ne doit pas partir -- il est jete et
    le prospect saute ce run.

    Detecte les deux syntaxes en usage dans les templates Triskell :
      - 1 accolade : {prenom}, {ville}, {raison_sociale}, ...
      - 2 accolades : {{first_name}}, {{city}}, {{business_type}}, ...

    Pour eviter les faux positifs, on n'attrape QUE des identifiants
    plausibles (lettres/chiffres/_), pas des accolades qui pourraient
    apparaitre dans une URL ou un bout de code accidentel.
    """
    import re as _re
    pat = _re.compile(r"\{\{?[A-Za-z_][A-Za-z0-9_]*\}?\}")
    for t in texts:
        if not t:
            continue
        m = pat.search(t)
        if m:
            return m.group(0)
    return ""


def _looks_like_ai_refusal(body: str) -> bool:
    """Detecte si l'IA a dump une meta-analyse au lieu d'ecrire le mail.

    Couvre les patterns vus en production (Claude qui refuse) : declarations
    explicites de probleme, listes de raisons numerotees, contradictions
    relevees. On verifie en debut de corps pour ne pas voir un faux positif
    si un vrai mail mentionne le mot 'probleme' en milieu de phrase.
    """
    if not body:
        return False
    head = body.strip().lower()[:400]
    markers = (
        "**probleme majeur**", "**problème majeur**",
        "problème majeur :", "probleme majeur :",
        "je ne peux pas rediger", "je ne peux pas rédiger",
        "impossible de rediger", "impossible de rédiger",
        "aucune info exploitable",
        "contradiction directe dans les consignes",
        "je n'ai pas assez d'info",
        "je manque d'info pour",
    )
    return any(m in head for m in markers)


def _detect_audience(prospect: Prospect, cfg: PipelineConfig) -> str:
    """Renvoie 'creator' ou 'pro' selon la source du prospect ou l'override
    explicite dans la config.

    Regle : si le prospect vient d'Obelisk (createurs reseaux sociaux), c'est
    un createur -> tutoiement chaleureux. Sinon (Chasseur, Sirene, Maps,
    Eclaireur), c'est une entreprise pro -> vouvoiement professionnel.
    L'override `cfg.autopilot_audience` ('creator'/'pro') prime si renseigne.
    """
    override = (getattr(cfg, "autopilot_audience", "") or "").strip().lower()
    if override in ("creator", "pro"):
        return override
    source_names = {(s.name or "").lower() for s in (prospect.sources or [])}
    creator_markers = {"obelisk", "obelisk_youtube", "obelisk_tiktok",
                       "obelisk_instagram", "obelisk_twitch", "obelisk_reddit",
                       "obelisk_bluesky", "obelisk_github"}
    for name in source_names:
        if name in creator_markers or name.startswith("obelisk"):
            return "creator"
    return "pro"


def _pro_category(secteur: str) -> str:
    """Classe un métier 'pro' en 'commerce' / 'artisan' / 'cabinet' pour choisir
    le bon modèle (un plombier ne doit pas recevoir le mail d'un fleuriste).

    Renvoie "" si le métier n'est pas reconnu → l'appelant garde alors TOUS les
    modèles 'pro' (jamais de trou, comportement historique).
    """
    import re
    import unicodedata
    base = (secteur or "").lower().replace("œ", "oe").replace("æ", "ae")
    s = "".join(c for c in unicodedata.normalize("NFD", base)
                if unicodedata.category(c) != "Mn")
    s = s.replace("’", "'")   # apostrophe typographique → droite (d'intérieur…)
    # Métiers à démo Pixel Pros dédiée (16/06/2026) : on les route en amont
    # vers le parcours « visuel » (commerce/artisan) pour que l'aperçu de leur
    # démo apparaisse bien dans le mail — même quand le mot-clé ressemblerait
    # à un cabinet (« ostéo », « architecte ») ou n'est dans aucune liste.
    if any(k in s for k in ("osteo", "kinesi", "kine", "reeduc")):
        return "commerce"   # ostéo & kiné (paramédical, mais a une démo)
    if "tapiss" in s:
        return "artisan"    # tapissier-décorateur (AVANT « decorateur »)
    if any(k in s for k in ("architecte d'interieur", "architecte interieur",
                            "architecte d interieur", "decorateur", "decoratrice",
                            "decoration d'interieur", "home staging")):
        return "commerce"   # architecte / décorateur d'intérieur
    if "piscin" in s:
        return "artisan"    # pisciniste
    if any(k in s for k in ("chambre", "gite", "maison d'hote", "hotes")):
        return "commerce"   # chambres d'hôtes / gîtes
    if any(k in s for k in ("auto-ecole", "auto ecole", "ecole de conduite",
                            "permis")):
        return "commerce"   # auto-école
    # — 15 nouveaux métiers à démo (16/06/2026) : on force commerce/artisan
    #   pour qu'ils reçoivent bien l'aperçu (jamais « cabinet », jamais vide). —
    if any(k in s for k in ("lavage auto", "lavage automobile", "detailing",
                            "car wash", "nettoyage auto", "lustrage")):
        return "artisan"    # lavage auto & detailing
    if any(k in s for k in ("food truck", "food-truck", "foodtruck", "food trailer",
                            "camion pizza", "camion a pizza", "camion restaurant",
                            "camion a burger", "street food")):
        return "commerce"   # food truck
    if any(k in s for k in ("dieteti", "nutrition")):
        return "commerce"   # diététicien / nutritionniste (paramédical, a une démo)
    if any(k in s for k in ("salle de sport", "salle de fitness", "fitness",
                            "crossfit", "cross-fit", "musculation", "club de sport")):
        return "commerce"   # salle de sport
    if any(k in s for k in ("salle de reception", "salle des fetes", "salle de fete",
                            "domaine de mariage", "domaine de reception",
                            "lieu de reception", "location de salle")):
        return "commerce"   # salle de réception
    if any(k in s for k in ("wedding", "organisateur de mariage",
                            "organisation de mariage", "organisateur d'evenement",
                            "organisation d'evenement", "organisateur d evenement",
                            "organisation d evenement")):
        return "commerce"   # wedding planner / organisateur d'événements
    if any(k in s for k in ("agent immobilier", "agence immobiliere",
                            "mandataire immo", "negociateur immo", "immobilier",
                            "immobiliere")):
        return "commerce"   # agent immobilier
    if any(k in s for k in ("poele", "granul", "pellet", "pompe a chaleur",
                            "aerotherm", "geotherm", "climatis", "photovolta",
                            "panneau solaire", "energies renouvelab")):
        return "artisan"    # chauffage nouvelle génération (poêle granulés, PAC)
    if re.search(r"\bdj\b", s) or any(k in s for k in (
            "disc jockey", "disc-jockey", "deejay", "sonorisation",
            "animation de soiree", "animation musicale")):
        return "commerce"   # DJ / animation de soirées
    # — 8 métiers de plus (16/06/2026) —
    if any(k in s for k in ("demenag", "garde-meuble")):
        return "artisan"    # déménageur
    if any(k in s for k in ("domoti", "alarme", "videosurveillance",
                            "video surveillance", "videoprotection",
                            "maison connectee")):
        return "artisan"    # domoticien / sécurité
    if any(k in s for k in ("diagnostiqueur", "diagnostic immobilier",
                            "diagnostics immobiliers", "diagnostic immo", "dpe")):
        return "commerce"   # diagnostiqueur immobilier
    if "veterin" in s:
        return "commerce"   # vétérinaire (était « cabinet » : a une démo)
    if any(k in s for k in ("constructeur de maison", "constructeur maison",
                            "maitre d'oeuvre", "maitre d oeuvre",
                            "maison individuelle", "constructeur")):
        return "artisan"    # constructeur de maisons
    if any(k in s for k in ("boucher", "boucherie", "charcuti")):
        return "commerce"   # boucher-charcutier
    if any(k in s for k in ("bijou", "joaill", "orfevr", "horloger")):
        return "commerce"   # bijoutier-horloger
    if any(k in s for k in ("opticien", "optique", "lunetier", "lunetterie")):
        return "commerce"   # opticien
    # — 6 métiers de plus (16/06/2026) —
    if any(k in s for k in ("fromag", "cremerie", "cremier")):
        return "commerce"   # fromager-affineur
    if any(k in s for k in ("poissonn", "maree", "fruits de mer", "ecailler")):
        return "commerce"   # poissonnier
    if any(k in s for k in ("torref", "brulerie", "cafe de specialite", "barista")):
        return "commerce"   # torréfacteur
    if any(k in s for k in ("microbrasserie", "micro-brasserie", "brasseur",
                            "biere artisanale")):
        return "commerce"   # microbrasserie
    if any(k in s for k in ("homme toutes mains", "toutes mains", "multiservice",
                            "petits travaux", "petit travaux", "factotum", "bricol")):
        return "artisan"    # homme toutes mains / petits travaux
    artisan = ("plomb", "chauffag", "electric", "peintr", "carrel", "faienc",
               "macon", "menuis", "ebenist", "charpent", "plaquist", "placo",
               "platr", "paysag", "jardin", "elagag", "espaces vert", "couvr",
               "serrur", "terrass", "renov", "batiment", "travaux", "artisan",
               "isolation", "garage", "mecanic", "carross", "vitrier", "metall",
               "ferronn", "facad", "ravalement", "cuisiniste", "store", "portail",
               "cloture", "etancheit", "demolition")
    cabinet = ("avocat", "notaire", "huissier", "comptab", "medecin", "dentist",
               "dentaire", "kine", "osteo", "infirmier", "podolog", "orthophon",
               "psycholog", "psychiatr", "veterin", "cabinet", "architect",
               "geometre", "assurance", "courtier", "therapeut", "expert")
    commerce = ("fleur", "patiss", "boulang", "chocolat", "confis", "cake",
                "restaur", "pizz", "brasser", "creper", "bistro", "traiteur",
                "snack", "kebab", "cafe", "coiff", "barbier", "esthet", "beaut",
                "ongle", "manucur", "maquill", "institut", "cils", "massag", "spa",
                "bien-etre", "bien etre", "sophro", "naturopath", "reflexo", "yoga",
                "hypno", "coach", "photograph", "videast", "tatou", "tattoo",
                "piercing", "toilettag", "canin", "pension", "dressage", "boutique",
                "magasin", "epicerie", "boucher", "primeur", "caviste", "opticien",
                "bijou", "pressing", "hotel", "gite", "fromager", "poissonn")
    for k in artisan:
        if k in s:
            return "artisan"
    for k in cabinet:
        if k in s:
            return "cabinet"
    for k in commerce:
        if k in s:
            return "commerce"
    return ""


def _humanize_email_source(source: str, context: str = "") -> str:
    """Traduit une source technique en libellé humain pour le brief IA."""
    s = (source or "").lower()
    if context:
        return context  # le contexte stocké est déjà rédigé pour un humain
    mapping = {
        "web":            "page contact ou mentions légales du site officiel",
        "web_inferred":   "adresse devinée à partir du domaine du site (non vérifiée)",
        "sirene":         "annuaire d'entreprises SIRENE",
        "maps":           "fiche Google Maps de l'établissement",
        "file":           "fichier importé",
        "linktree":       "hub de liens (Linktree, Beacons, etc.)",
        "obelisk":        "profil créateur récupéré via Obélisk",
        "phantombuster":  "profil social récupéré via PhantomBuster",
        "chasseur":       "trouvé via Le Chasseur (entreprises)",
        "bio":            "bio / description du profil",
    }
    if s in mapping:
        return mapping[s]
    # Cas obelisk_youtube, obelisk_twitch, phantombuster_instagram, etc.
    if s.startswith("obelisk_"):
        return f"profil {s.split('_', 1)[1]} (récupéré via Obélisk)"
    if s.startswith("phantombuster_"):
        return f"profil {s.split('_', 1)[1]} (récupéré via PhantomBuster)"
    return source or "source inconnue"


def _humanize_prospect_sources(prospect: Prospect) -> list[str]:
    """Liste des origines globales du prospect, en libellés humains."""
    out: list[str] = []
    seen: set[str] = set()
    for s in (prospect.sources or []):
        name = (getattr(s, "name", "") or "").lower()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(_humanize_email_source(name))
    return out


def _build_personalized_prompt(prospect: Prospect, cfg: PipelineConfig) -> str:
    """Construit la consigne user pour l'IA, contextualisée sur le prospect."""
    name = prospect.name or prospect.legal_name or "(sans nom)"
    main_name = name.split("(", 1)[0].strip(" .-")
    city = prospect.city or "—"
    industry = prospect.industry or "—"
    description = prospect.description or "—"

    # Branche tutoiement (createurs / influenceurs) retiree a la demande de
    # Jordan : il ne veut PLUS jamais d'instructions de tutoiement dans le
    # prompt, peu importe le type de prospect. Tout le monde est vouvoye.
    audience = _detect_audience(prospect, cfg)
    ton_instruction = (
        "TON ET FORMULATION : VOUVOIEMENT obligatoire et systematique "
        "('Bonjour,', 'vous', 'votre'). JAMAIS de tutoiement, jamais. "
        "Formule de fin : 'Cordialement' ou 'Bien cordialement'. "
        "STRICTEMENT INTERDIT toute familiarite (pas de surnoms, pas de "
        "'Salut', pas de 'A+', pas de 'Bisous', pas de 'A tout', pas de "
        "'Bien a toi')."
    )

    mise_en_forme = (
        "MISE EN FORME : tu peux utiliser un peu de gras et de soulignement "
        "pour faire ressortir les points importants (prix, délai, "
        "argument-clé). Syntaxe :\n"
        "  - **mot ou phrase courte** pour le GRAS\n"
        "  - __mot ou phrase courte__ pour le SOULIGNEMENT\n"
        "Maximum 3 passages en gras et 2 en souligné par mail. Reste sobre, "
        "ne décore pas, n'abuse pas. Aucun emoji, aucune liste à puces, "
        "aucun titre."
    )

    # === ORIGINE DU PROSPECT + DE SON EMAIL ===
    # On expose à l'IA d'où vient le prospect (Google Maps, YouTube, SIRENE…)
    # et d'où vient SPÉCIFIQUEMENT l'adresse mail choisie (page contact d'un
    # site officiel, bio YouTube, mentions légales…). Ces infos changent
    # l'angle commercial : un coiffeur trouvé sur Maps n'est pas abordé comme
    # un YouTubeur, et un email pris en mentions légales n'a pas la même
    # connotation qu'un email mis en avant sur une page contact.
    chosen_email = prospect.emails[0] if prospect.emails else ""
    email_origin_human = ""
    if chosen_email:
        meta = prospect.source_of_email(chosen_email)
        if meta:
            email_origin_human = _humanize_email_source(
                meta.get("source", ""), meta.get("context", "")
            )
    prospect_origins = _humanize_prospect_sources(prospect)
    audience_human = ("Pro / Entreprise" if audience == "pro"
                       else "Créateur / Influenceur" if audience == "creator"
                       else "Inconnue")
    subs_line = ""
    if prospect.subscribers and audience == "creator":
        subs_line = f"- Audience (abonnés / followers) : {prospect.subscribers:,}\n".replace(",", " ")

    contexte_origine = "CONTEXTE D'ACQUISITION DU PROSPECT :\n"
    contexte_origine += f"- Type de prospect : {audience_human}\n"
    if prospect_origins:
        contexte_origine += "- D'où le prospect a été trouvé : " + ", ".join(prospect_origins) + "\n"
    if chosen_email and email_origin_human:
        contexte_origine += (
            f"- D'où vient l'adresse mail que tu vas écrire ({chosen_email}) : "
            f"{email_origin_human}\n"
        )
    if subs_line:
        contexte_origine += subs_line
    contexte_origine += (
        "\nADAPTE ton angle commercial à ce contexte. Exemples :\n"
        "  • Si l'adresse vient des « mentions légales » d'un site pro, c'est "
        "  un contact officiel B2B : reste très pro, parle entreprise.\n"
        "  • Si l'adresse vient d'une « page contact » : c'est le bon canal "
        "  pour une approche commerciale directe.\n"
        "  • Si c'est un créateur (YouTube, Twitch…) avec adresse en bio : "
        "  reste pro mais reconnais leur statut de créateur ; pas de jargon "
        "  d'entreprise lourd.\n"
        "  • Si c'est un commerce local (Google Maps) : parle de leur métier "
        "  de proximité, pas de « scaling » ni de « ROI »."
    )

    return (
        f"Tu vas rédiger un mail de prospection commercial pour ce prospect précis :\n\n"
        f"PROSPECT :\n"
        f"- Nom / entreprise : {main_name}\n"
        f"- Ville : {city}\n"
        f"- Secteur : {industry}\n"
        f"- Description : {description[:300]}\n"
        f"- Site web : {prospect.website or '—'}\n\n"
        f"{contexte_origine}\n\n"
        f"MON PRÉNOM : {cfg.sender_mon_prenom or '(non renseigné)'}\n\n"
        f"{ton_instruction}\n\n"
        f"{mise_en_forme}\n\n"
        f"CONSIGNES :\n{_effective_template_brief(cfg)}\n"
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
            for k in ("anthropic", "openai", "google", "mistral", "xai", "deepseek"):
                # Trois formats de nommage possibles dans config.json selon
                # l'écriveur. Le format CANONIQUE est "{k}_api_key" (ex
                # "google_api_key"), écrit par shared_secrets.sync_ai_keys_to_core
                # ET lu par shared_secrets.get_ai_keys — c'est lui qui faisait
                # défaut ici : l'auto-pilote ne voyait qu'une IA sur plusieurs
                # enregistrées (toutes les clés de secours étaient invisibles).
                v = (data.get(f"{k}_api_key")
                     or data.get(f"ai_api_key_{k}")
                     or data.get(k) or "")
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
    from .outreach.smtp_sender import (
        _load_smtp_config, prospection_headers, send_email,
    )
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
    # La situation a pu changer depuis la creation du brouillon : un
    # desinscrit / rebond / refus / client ne doit plus jamais recevoir
    # de mail, meme valide a la main.
    _blocked = {
        "unsubscribed": "ce prospect s'est désinscrit — envoi interdit",
        "bounced":      "l'adresse de ce prospect est morte (rebond)",
        "refused":      "ce prospect a refusé — on ne le recontacte pas",
        "won":          "ce prospect est déjà client",
        "lost":         "cycle clos avec ce prospect",
    }
    if (target.status or "").lower() in _blocked:
        return {"ok": False, "reason": _blocked[(target.status or "").lower()]}
    draft = target.pending_drafts.pop(draft_index)

    # Regle absolue anti-placeholder : un brouillon avec une variable non
    # remplie ne part JAMAIS, meme valide a la main (le validateur humain
    # peut rater un {{x}} au milieu d'un long mail).
    _orphan = _has_unfilled_placeholder(
        draft.get("subject", ""), draft.get("body", ""))
    if _orphan:
        target.pending_drafts.insert(draft_index, draft)
        crm._dirty = True  # noqa: SLF001
        crm.save()
        return {"ok": False,
                "reason": f"variable non remplie dans le mail : {_orphan}"}

    try:
        smtp_cfg = _load_smtp_config()
        # Câblage modèle→adresse : un brouillon qui exige une adresse
        # d'expéditeur part par le compte correspondant ou ne part pas —
        # jamais par une autre adresse (même validé à la main).
        _wanted = (draft.get("sender_address") or "").strip().lower()
        if _wanted and ((smtp_cfg or {}).get("from_email")
                        or "").strip().lower() != _wanted:
            _acc = None
            try:
                from triskell_command.integrations.shared_secrets import (
                    get_account_by_address,
                )
                try:
                    from triskell_core.db import get_client as _gc
                    _cl = _gc()
                except Exception:
                    _cl = None
                _acc = get_account_by_address(_wanted, client=_cl)
            except Exception:
                _acc = None
            _required = ("smtp_host", "smtp_user", "smtp_password",
                         "from_email")
            if _acc and all(_acc.get(k) for k in _required):
                smtp_cfg = {
                    "smtp_host":     _acc.get("smtp_host"),
                    "smtp_port":     int(_acc.get("smtp_port") or 587),
                    "smtp_user":     _acc.get("smtp_user"),
                    "smtp_password": _acc.get("smtp_password"),
                    "from_email":    _acc.get("from_email"),
                    "from_name":     _acc.get("from_name", ""),
                }
            else:
                target.pending_drafts.insert(draft_index, draft)
                crm._dirty = True  # noqa: SLF001
                crm.save()
                return {"ok": False,
                        "reason": (f"ce brouillon doit partir de l'adresse "
                                   f"{_wanted}, introuvable dans les "
                                   f"adresses d'envoi — rien envoyé")}
        msg_id = send_email(
            smtp_cfg,
            to=target.emails[0] if target.emails else "",
            subject=draft["subject"],
            body=draft["body"],
            body_html=draft.get("body_html", ""),
            custom_headers=prospection_headers(
                smtp_cfg.get("from_email", "")),
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
