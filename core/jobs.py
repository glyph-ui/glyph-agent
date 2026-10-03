# -*- coding: utf-8 -*-
"""
HSEQ-Jobs für °_Agent (glyph-agent) — geplante und manuelle Läufe.

OpenClaw-Cron ersetzt: Eingang-Prüfung, Handover, Aus-Fertig-lernen.
Schreiben nur mit Auto-Confirm in erlaubte HSEQ-Sync-Pfade (kein MessageSend).
"""
from __future__ import annotations

import json
import os
import re
import time
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo

from . import config, log, tool_loop

# Thread-sicherer Override der Cloud-Kontext-Grenze während Jobs
_external_max_override: ContextVar[Optional[int]] = ContextVar(
    "glyph_job_external_max", default=None
)

TZ = ZoneInfo("Europe/Berlin")
HSEQ_ROOT = "/Users/lxndrhbrt/ObsidianVaults/HSEQ Sync"

# Vault-Schreiben nur unter diesen relativen Präfixen (HSEQ Sync)
_WRITE_PREFIXES = (
    "00 Arbeitsfluss/",
    "HSEQ Sync/00 Arbeitsfluss/",
    "Vorlagen/",
    "HSEQ Sync/Vorlagen/",
    "Themen/",
    "HSEQ Sync/Themen/",
)

_WRITE_TOOLS = frozenset({"CreateNote", "ApplyEdit"})

JOBS: Dict[str, Dict[str, Any]] = {
    "hseq-eingang": {
        "name": "hseq-eingang",
        "label": "HSEQ Eingang → Fertig",
        "prompt_path": os.path.join(
            HSEQ_ROOT,
            "Vorlagen/Audit/Prompt - Auditnotizen auswerten und Auditbericht.md",
        ),
        "message": (
            "HSEQ-Eingang: genau EINE Rohnotiz. Nach der Notiz sofort CreateNote — nicht suchen.\n"
            "Nur: ListVaultDir Eingang einmal, Log, FileHash, ReadNote Rohnotiz, "
            "ReadNote Blaupause Kopf/Überschrift/Kapitel, dann SOFORT CreateNote "
            "Auditbericht + Maßnahmenplan in 00 Arbeitsfluss/Fertig/ (nie bestehende Berichte ApplyEdit), "
            "dann ApplyEdit nur Verarbeitungslog.\n"
            "Kein Archiv, kein VaultFind, kein zweites ListVaultDir, nicht den MASTER-PROMPT lesen.\n"
            "Logo ![[MBVD-TUeV-Sued-Pluspunkt.jpg]] stehen lassen. 4 Spalten. "
            "Keine Personennamen. Rohnotizen nicht ändern. "
            f"Pfade unter {HSEQ_ROOT}/. "
            "Nichts: 'HSEQ: keine Änderungen'. Selbstständig, keine Rückfragen."
        ),
        "external_max_chars": 48000,
        "timeout_hint_s": 1800,
    },
    "hseq-handover": {
        "name": "hseq-handover",
        "label": "HSEQ Handover Daily",
        "prompt_path": os.path.join(HSEQ_ROOT, "Vorlagen/Jobs/PROMPT-Handover.md"),
        "message": (
            "HSEQ-Handover (glyph-agent Job). Öffne und führe vollständig aus: "
            f"{HSEQ_ROOT}/Vorlagen/Jobs/PROMPT-Handover.md\n\n"
            "Schreibe/überschreibe nur: "
            f"{HSEQ_ROOT}/00 Arbeitsfluss/Daily/YYYY-MM-DD.md (heute Europe/Berlin).\n"
            "Quellen lesen: Eingang/, Fertig/, Verarbeitungslog, letztes Daily.\n"
            "Keine Personennamen. Kein Privat-Vault. Keine Rohnotizen ändern.\n"
            "Ruhig: 'Handover: ruhig (YYYY-MM-DD)'. Sonst 3–6 Zeilen + Pfad.\n"
            "Selbstständig, keine Rückfragen."
        ),
        "external_max_chars": 24000,
        "timeout_hint_s": 900,
    },
    "hseq-aus-fertig-lernen": {
        "name": "hseq-aus-fertig-lernen",
        "label": "HSEQ Aus Fertig lernen",
        "prompt_path": os.path.join(
            HSEQ_ROOT, "Vorlagen/Jobs/PROMPT-Aus-Fertig-lernen.md"
        ),
        "message": (
            "HSEQ Aus-Fertig-lernen (glyph-agent Job). Öffne und führe vollständig aus: "
            f"{HSEQ_ROOT}/Vorlagen/Jobs/PROMPT-Aus-Fertig-lernen.md\n\n"
            "Maximal EINE kleine Änderung an Vorlage oder Themen-Notiz — oder nichts.\n"
            "Keine neuen Berichte. Eingang/Fertig nur lesen. Keine Personennamen.\n"
            "Nichts: 'Aus Fertig lernen: nichts Neues'. Selbstständig, keine Rückfragen."
        ),
        "external_max_chars": 32000,
        "timeout_hint_s": 900,
    },
}


def get_external_max_chars() -> int:
    """Job-Override, recurring-Override oder Config-Default."""
    o = _external_max_override.get()
    if o is not None:
        return int(o)
    try:
        from . import recurring as _rec

        return _rec.get_external_max_chars()
    except Exception:
        pass
    return int(getattr(config, "EXTERNAL_MAX_CHARS", 4000) or 4000)


def list_jobs() -> list:
    return [
        {
            "id": j["name"],
            "label": j["label"],
            "prompt_path": j["prompt_path"],
        }
        for j in JOBS.values()
    ]


def _path_allowed(path: str) -> bool:
    p = (path or "").strip().replace("\\", "/")
    if not p:
        return False
    # Absoluten HSEQ-Pfad → relativ
    if p.startswith(HSEQ_ROOT):
        p = p[len(HSEQ_ROOT) :].lstrip("/")
    for pref in _WRITE_PREFIXES:
        if p.startswith(pref) or p == pref.rstrip("/"):
            return True
    return False


def make_job_confirm() -> Callable[[str, dict], bool]:
    """Auto-Confirm nur CreateNote/ApplyEdit in HSEQ-Sync-Arbeitsbereichen."""

    def confirm(tool_name: str, args: dict) -> bool:
        if tool_name not in _WRITE_TOOLS:
            return False
        path = ""
        if isinstance(args, dict):
            path = str(args.get("path") or "")
        ok = _path_allowed(path)
        log.log(
            "job_confirm",
            tool=tool_name,
            path=path[:200],
            allowed=ok,
        )
        return ok

    return confirm


def _load_prompt_body(path: str, limit: int = 12000) -> str:
    """Liest Job-Prompt vom Disk (ohne Vault-Tools), gekürzt für den Job-Kontext."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        if len(text) > limit:
            return text[:limit] + "\n\n… [Prompt gekürzt]"
        return text
    except OSError as e:
        return f"(Prompt-Datei nicht lesbar: {path}: {e})"


def run_job(job_id: str, message_override: Optional[str] = None) -> dict:
    """
    Führt einen benannten Job im Agent-Tool-Loop aus (mit Auto-Confirm).
    """
    job = JOBS.get(job_id)
    if not job:
        return {
            "ok": False,
            "job": job_id,
            "answer": f"Unbekannter Job '{job_id}'. Bekannt: {', '.join(JOBS)}",
            "rounds": 0,
            "tool_calls": [],
        }

    message = (message_override or job["message"] or "").strip()
    if not message:
        return {
            "ok": False,
            "job": job_id,
            "answer": "Leere Job-Nachricht.",
            "rounds": 0,
            "tool_calls": [],
        }

    # Heutiges Datum in Message einsetzen, falls Platzhalter
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    message = message.replace("YYYY-MM-DD", today)

    # Prompt-Inhalt einbetten (zuverlässiger als ReadNote mit Vault-Präfix-Pfaden)
    prompt_path = job.get("prompt_path") or ""
    if prompt_path and not message_override:
        body = _load_prompt_body(prompt_path)
        message = (
            message
            + "\n\n--- BEGIN JOB PROMPT (verbindlich ausführen) ---\n"
            + body
            + "\n--- END JOB PROMPT ---\n"
            + "\nPfade relativ zum Vault HSEQ Sync ohne Präfix nutzen "
            + "(z.B. Vorlagen/Jobs/…, 00 Arbeitsfluss/Daily/…). "
            + "Oder absolute Pfade unter "
            + HSEQ_ROOT
            + "/. "
            + "Kein WebSearch. Zuerst ListVaultDir/ReadNote auf Eingang, Fertig, Log."
        )

    token = _external_max_override.set(int(job.get("external_max_chars") or 24000))
    t0 = time.time()
    log.log("job_start", job=job_id, chars=len(message))
    try:
        result = tool_loop.run(
            message,
            confirm=make_job_confirm(),
            max_rounds=24,
        )
    except Exception as e:
        log.log("job_error", job=job_id, error=str(e)[:300])
        return {
            "ok": False,
            "job": job_id,
            "answer": f"Job fehlgeschlagen: {e}",
            "rounds": 0,
            "tool_calls": [],
            "duration_ms": int((time.time() - t0) * 1000),
        }
    finally:
        _external_max_override.reset(token)

    duration_ms = int((time.time() - t0) * 1000)
    out = {
        "ok": bool(result.get("ok", True)),
        "job": job_id,
        "label": job["label"],
        "date": today,
        "duration_ms": duration_ms,
        "answer": result.get("answer", ""),
        "rounds": result.get("rounds", 0),
        "tool_calls": result.get("tool_calls") or [],
        "trace": result.get("trace"),
    }
    log.log(
        "job_done",
        job=job_id,
        ok=out["ok"],
        duration_ms=duration_ms,
        rounds=out["rounds"],
        answer_chars=len(out["answer"] or ""),
    )
    _append_job_log(out)
    return out


def _append_job_log(out: dict) -> None:
    """Kompaktes Job-Log unter logs/jobs.jsonl."""
    try:
        log_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"
        )
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, "jobs.jsonl")
        row = {
            "ts": datetime.now(TZ).isoformat(timespec="seconds"),
            "job": out.get("job"),
            "ok": out.get("ok"),
            "duration_ms": out.get("duration_ms"),
            "rounds": out.get("rounds"),
            "answer_preview": (out.get("answer") or "")[:400],
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def stamp_path(job_id: str) -> str:
    """Catch-up-Stamp: einmal pro Kalendertag."""
    stamp_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "logs",
        "job-stamps",
    )
    os.makedirs(stamp_dir, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", job_id)
    return os.path.join(stamp_dir, safe)


def already_ran_today(job_id: str) -> bool:
    path = stamp_path(job_id)
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() == today
    except OSError:
        return False


def mark_ran_today(job_id: str) -> None:
    path = stamp_path(job_id)
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    with open(path, "w", encoding="utf-8") as f:
        f.write(today)
