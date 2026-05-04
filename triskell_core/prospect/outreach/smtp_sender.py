"""
SMTP Sender — envoie les emails de prospection via Gmail/OVH/IONOS.

Configuration : voir `triskell_core.prospect.cli config --smtp-host ...`.

Politesse :
- Délai aléatoire 30–120 s entre 2 envois (paraît humain, anti-spam-throttle).
- Plafond quotidien configurable (default 40/jour pour Gmail gratuit).
- Headers `Reply-To` corrects + Message-ID stable.
- Mode `--dry-run` qui affiche sans envoyer.

Stockage : log d'envoi dans `prospect.history` + statut → "contacted",
+ champ `last_contact_at` mis à jour.
"""

from __future__ import annotations

import json
import logging
import random
import smtplib
import time
import uuid
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from ..core.crm import APP_DIR, CONFIG_FILE, CRM
from ..core.prospect import Prospect
from . import templates

log = logging.getLogger(__name__)


SEND_LOG = APP_DIR / "send_log.json"


class SmtpConfigError(Exception):
    pass


def _load_smtp_config() -> dict:
    if not CONFIG_FILE.exists():
        raise SmtpConfigError(f"Config absente : {CONFIG_FILE}")
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        raise SmtpConfigError(f"Config illisible : {e}")
    required = ("smtp_host", "smtp_port", "smtp_user", "smtp_password", "from_email")
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        raise SmtpConfigError(
            f"Config SMTP incomplète. Manque : {', '.join(missing)}. "
            f"Voir : python -m triskell_core.prospect.cli config --help"
        )
    return cfg


# ---------------------------------------------------------------------------
# Quota quotidien
# ---------------------------------------------------------------------------
def _load_today_count() -> int:
    today = datetime.now().date().isoformat()
    if not SEND_LOG.exists():
        return 0
    try:
        data = json.loads(SEND_LOG.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if data.get("date") == today:
        return int(data.get("count", 0))
    return 0


def _bump_today_count(by: int = 1) -> int:
    today = datetime.now().date().isoformat()
    if SEND_LOG.exists():
        try:
            data = json.loads(SEND_LOG.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    else:
        data = {}
    if data.get("date") != today:
        data = {"date": today, "count": 0}
    data["count"] = int(data.get("count", 0)) + by
    SEND_LOG.write_text(json.dumps(data), encoding="utf-8")
    return data["count"]


# ---------------------------------------------------------------------------
# Envoi
# ---------------------------------------------------------------------------
def send_email(
    smtp_cfg: dict,
    *,
    to: str,
    subject: str,
    body: str,
    reply_to: str = "",
    custom_headers: dict | None = None,
) -> str:
    """Envoie 1 mail. Renvoie le Message-ID. Lève en cas d'échec."""
    msg = EmailMessage()
    from_name = smtp_cfg.get("from_name", "")
    from_email = smtp_cfg["from_email"]
    msg["From"] = f"{from_name} <{from_email}>" if from_name else from_email
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    domain = from_email.split("@", 1)[1]
    message_id = make_msgid(domain=domain)
    msg["Message-ID"] = message_id
    if reply_to:
        msg["Reply-To"] = reply_to
    elif from_email:
        msg["Reply-To"] = from_email
    if custom_headers:
        for k, v in custom_headers.items():
            msg[k] = v
    msg.set_content(body)

    host = smtp_cfg["smtp_host"]
    port = int(smtp_cfg["smtp_port"])
    user = smtp_cfg["smtp_user"]
    password = smtp_cfg["smtp_password"]

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        # 587 STARTTLS (Gmail, OVH par défaut)
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(user, password)
            s.send_message(msg)
    return message_id


# ---------------------------------------------------------------------------
# Boucle de campagne
# ---------------------------------------------------------------------------
def select_due_for_first_contact(
    crm: CRM, *, limit: int, only_verified: bool, with_email_required: bool
) -> list[Prospect]:
    """Sélectionne les prospects éligibles à un 1er envoi."""
    out = []
    for p in crm.all():
        if p.status != "qualified" and p.status != "new":
            continue
        if with_email_required and not p.emails:
            continue
        if only_verified and "site_verified" not in p.tags:
            continue
        # On ne re-contacte jamais
        if any(h.get("kind") == "email_sent" for h in p.history):
            continue
        out.append(p)
        if len(out) >= limit:
            break
    return out


def select_due_for_followup(
    crm: CRM, *, limit: int, follow_up_days: int = 5
) -> list[Prospect]:
    """Sélectionne les prospects qui ont reçu un mail il y a >= follow_up_days
    et qui n'ont PAS répondu."""
    cutoff = datetime.now() - timedelta(days=follow_up_days)
    out = []
    for p in crm.all():
        if p.status != "contacted":
            continue
        last_sent = None
        had_followup = False
        for h in p.history:
            if h.get("kind") == "email_sent":
                ts = h.get("ts")
                if ts:
                    try:
                        dt = datetime.fromisoformat(ts)
                        if last_sent is None or dt > last_sent:
                            last_sent = dt
                    except Exception:
                        pass
                if h.get("template_key", "").endswith("_relance_j5"):
                    had_followup = True
        if had_followup or last_sent is None:
            continue
        if last_sent < cutoff:
            out.append(p)
        if len(out) >= limit:
            break
    return out


def run_campaign(
    *,
    template_key: str,
    sender_vars: dict,
    daily_cap: int = 40,
    pause_min_s: float = 30,
    pause_max_s: float = 120,
    only_verified: bool = True,
    follow_up: bool = False,
    follow_up_days: int = 5,
    dry_run: bool = False,
    limit: int = 0,
) -> dict:
    """Lance une vague d'envois. Renvoie des stats.

    En mode dry_run, la config SMTP n'est PAS exigée : on ne contacte
    aucun serveur, on simule juste la sélection et le rendu des templates.
    """
    if dry_run:
        try:
            smtp_cfg = _load_smtp_config()
        except SmtpConfigError:
            smtp_cfg = {}
    else:
        smtp_cfg = _load_smtp_config()
    crm = CRM()

    sent_today = _load_today_count()
    remaining = max(0, daily_cap - sent_today)
    if remaining == 0:
        return {"sent": 0, "skipped_quota": 0, "reason": "daily_cap_reached", "today": sent_today}

    target_n = min(limit or remaining, remaining)
    if follow_up:
        targets = select_due_for_followup(crm, limit=target_n, follow_up_days=follow_up_days)
    else:
        targets = select_due_for_first_contact(
            crm, limit=target_n, only_verified=only_verified, with_email_required=True
        )

    if not targets:
        return {"sent": 0, "candidates": 0, "today": sent_today}

    counters = {"sent": 0, "errors": 0, "candidates": len(targets), "today": sent_today,
                "dry_run": dry_run}

    for p in targets:
        if not p.emails:
            continue
        to_addr = p.emails[0]
        try:
            subject, body = templates.render(template_key, p, sender_vars)
        except Exception as e:
            log.warning("Render échoué pour %s : %s", p.name, e)
            counters["errors"] += 1
            continue

        if dry_run:
            # ASCII safe — éviter '→' qui plante sur stdout cp1252 en bundle Windows
            try:
                print(f"[dry-run] -> {to_addr} | {subject!r}")
            except UnicodeEncodeError:
                pass
            counters["sent"] += 1
            continue

        try:
            msg_id = send_email(smtp_cfg, to=to_addr, subject=subject, body=body)
            p.history.append({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "kind": "email_sent",
                "to": to_addr,
                "subject": subject,
                "template_key": template_key,
                "message_id": msg_id,
            })
            p.status = "contacted"
            p.last_contact_at = datetime.now().isoformat(timespec="seconds")
            crm._dirty = True  # noqa: SLF001
            counters["sent"] += 1
            _bump_today_count()
            crm.save()
            print(f"  ✉ {to_addr} ({p.name[:30]})")
            # Pause humaine entre 2 envois (sauf le dernier)
            if p is not targets[-1]:
                time.sleep(random.uniform(pause_min_s, pause_max_s))
        except Exception as e:
            log.warning("Envoi à %s a échoué : %s", to_addr, e)
            counters["errors"] += 1
            p.history.append({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "kind": "email_failed",
                "to": to_addr,
                "error": str(e)[:200],
            })
            crm._dirty = True  # noqa: SLF001

    crm.save()
    return counters
