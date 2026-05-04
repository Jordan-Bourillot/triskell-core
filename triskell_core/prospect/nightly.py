"""Boucle nocturne automatisée — orchestration end-to-end.

Lance le pipeline complet (search → enrich → AI → send/sas → follow-up → poll IMAP).

Cible : Windows Task Scheduler à 03:00.
Sortie : log dans ~/.triskell-prospect/nightly.log
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from .core.crm import APP_DIR, ensure_dirs
from .pipeline import PipelineConfig, run_full_pipeline


LOG_FILE = APP_DIR / "nightly.log"


def run() -> dict:
    ensure_dirs()
    log_lines: list[str] = []

    def emit(msg: str) -> None:
        line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
        log_lines.append(line)
        try:
            print(line)
        except UnicodeEncodeError:
            pass

    emit("=== Triskell Prospection nightly run ===")

    cfg = PipelineConfig.load()
    if not cfg.enabled:
        emit("Pipeline désactivé (cfg.enabled = False). Run avorté.")
        _flush_log(log_lines)
        return {"skipped": True, "reason": "disabled"}

    emit(f"Mode : {cfg.mode}  | source : {cfg.source}  | daily_cap : {cfg.daily_cap}")

    stats = run_full_pipeline(cfg, progress=emit)

    emit(
        f"=== Fin : "
        f"+{stats.searched} prospects, "
        f"{stats.enriched} enrichis ({stats.enrich_emails_found} emails), "
        f"{stats.drafts_sent} envoyés / {stats.drafts_pending} en attente, "
        f"{stats.follow_ups_sent} relances, "
        f"{stats.replies_detected} réponses détectées, "
        f"{len(stats.errors)} erreurs"
    )
    if stats.errors:
        for e in stats.errors:
            emit(f"  ✗ {e}")

    _flush_log(log_lines)
    return {
        "searched": stats.searched,
        "enriched": stats.enriched,
        "sent": stats.drafts_sent,
        "pending": stats.drafts_pending,
        "follow_ups": stats.follow_ups_sent,
        "replies": stats.replies_detected,
    }


def _flush_log(lines: list[str]) -> None:
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Triskell Prospection — boucle nocturne")
    # Ce flag est conservé pour compat ; en mode pipeline il est lu depuis pipeline.json.
    p.add_argument("--mon-prenom", default="")
    p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
