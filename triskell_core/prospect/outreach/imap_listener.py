"""
IMAP Listener — détecte les réponses aux emails de prospection
et bascule automatiquement le statut → "replied" pour stopper les relances.

Stratégie :
- À chaque run, fetch les mails INBOX depuis le dernier check
- Pour chaque mail, on regarde :
  1. Le header `In-Reply-To` ou `References` qui contient le Message-ID
     d'un envoi précédent — match exact dans `prospect.history`
  2. Sinon, le header `From` matche un email de prospect — match flou
- On marque le prospect `replied`, on log l'événement, on stoppe la séquence.

Idempotent : on stocke le dernier UID traité dans
~/.triskell-prospect/imap_state.json. Re-run = reprend où on en était.
"""

from __future__ import annotations

import email
import imaplib
import json
import logging
from datetime import datetime
from pathlib import Path

from ..core.crm import APP_DIR, CONFIG_FILE, CRM
from ..core.prospect import norm_email

log = logging.getLogger(__name__)


IMAP_STATE = APP_DIR / "imap_state.json"


class ImapConfigError(Exception):
    pass


def _load_imap_config() -> dict:
    if not CONFIG_FILE.exists():
        raise ImapConfigError(f"Config absente : {CONFIG_FILE}")
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        raise ImapConfigError(f"Config illisible : {e}")
    required = ("imap_host", "imap_user", "imap_password")
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        raise ImapConfigError(
            f"Config IMAP incomplète. Manque : {', '.join(missing)}. "
            f"Voir : python -m triskell_core.prospect.cli config --help"
        )
    return cfg


def _load_state() -> dict:
    if not IMAP_STATE.exists():
        return {"last_uid": 0}
    try:
        return json.loads(IMAP_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"last_uid": 0}


def _save_state(state: dict) -> None:
    IMAP_STATE.write_text(json.dumps(state), encoding="utf-8")


def _extract_msg_id(raw: str) -> str:
    """Extrait un Message-ID depuis un header (qui peut contenir des < > ou des espaces)."""
    if not raw:
        return ""
    # Cherche `<...>`, sinon prend tout
    import re
    m = re.search(r"<([^>]+)>", raw)
    return m.group(1).strip() if m else raw.strip()


def _from_address(raw: str) -> str:
    """Extrait l'email depuis un header From: 'Marie <marie@x.fr>'."""
    if not raw:
        return ""
    addr = email.utils.parseaddr(raw)
    return (addr[1] or "").lower().strip()


def poll_replies(verbose: bool = False) -> dict:
    """Scanne la boîte IMAP, détecte les réponses, met à jour le CRM."""
    cfg = _load_imap_config()
    crm = CRM()
    state = _load_state()

    host = cfg["imap_host"]
    user = cfg["imap_user"]
    password = cfg["imap_password"]
    port = int(cfg.get("imap_port", 993))

    counters = {"scanned": 0, "matched": 0, "errors": 0}

    M = imaplib.IMAP4_SSL(host, port)
    try:
        M.login(user, password)
        M.select("INBOX", readonly=True)

        last_uid = int(state.get("last_uid", 0))
        # UID search : tous les messages strictement supérieurs au dernier vu
        criteria = f"UID {last_uid + 1}:*" if last_uid else "ALL"
        typ, data = M.uid("search", None, criteria)
        if typ != "OK":
            log.warning("IMAP search a échoué : %s", typ)
            return counters

        uids = data[0].split() if data and data[0] else []
        if not uids:
            return counters

        # Index des Message-IDs envoyés (depuis history) → prospect
        msgid_to_prospect = {}
        from_to_prospect = {}
        for p in crm.all():
            for h in p.history:
                if h.get("kind") == "email_sent":
                    mid = h.get("message_id", "")
                    mid_clean = _extract_msg_id(mid)
                    if mid_clean:
                        msgid_to_prospect[mid_clean] = p
            for e in p.emails:
                from_to_prospect[norm_email(e)] = p

        max_uid_seen = last_uid

        for uid in uids:
            uid_int = int(uid)
            max_uid_seen = max(max_uid_seen, uid_int)
            counters["scanned"] += 1
            try:
                # On ne fetch que les headers pour rester léger
                typ, msg_data = M.uid(
                    "fetch", uid,
                    "(BODY.PEEK[HEADER.FIELDS (FROM IN-REPLY-TO REFERENCES SUBJECT)])",
                )
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                hdr_raw = msg_data[0][1]
                if isinstance(hdr_raw, bytes):
                    hdr_raw = hdr_raw.decode("utf-8", errors="ignore")
                hdr_msg = email.message_from_string(hdr_raw)

                in_reply_to = _extract_msg_id(hdr_msg.get("In-Reply-To", ""))
                references = hdr_msg.get("References", "") or ""
                from_addr = _from_address(hdr_msg.get("From", ""))
                subject = (hdr_msg.get("Subject") or "").strip()

                # Match précis : Message-ID dans le thread
                match = None
                for candidate_mid in [in_reply_to] + [
                    _extract_msg_id(r) for r in references.split()
                ]:
                    if candidate_mid and candidate_mid in msgid_to_prospect:
                        match = msgid_to_prospect[candidate_mid]
                        break

                # Match flou : l'expéditeur est un prospect connu
                if not match and from_addr:
                    match = from_to_prospect.get(from_addr)

                if not match:
                    continue

                if match.status not in ("replied", "won", "lost", "refused"):
                    match.status = "replied"
                match.history.append({
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "kind": "reply_detected",
                    "from": from_addr,
                    "subject": subject[:120],
                    "in_reply_to": in_reply_to,
                })
                crm._dirty = True  # noqa: SLF001
                counters["matched"] += 1
                if verbose:
                    print(f"  ↩ Réponse détectée de {from_addr} → {match.name[:40]}")
            except Exception as e:
                log.warning("UID %s : %s", uid_int, e)
                counters["errors"] += 1

        # On persiste le dernier UID vu
        state["last_uid"] = max_uid_seen
        _save_state(state)
        crm.save()
    finally:
        try:
            M.logout()
        except Exception:
            pass

    return counters
