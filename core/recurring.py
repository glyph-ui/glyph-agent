# -*- coding: utf-8 -*-
"""
Wiederkehrende To-dos (glyph-agent) — eine Wahrheit für Plan-Tab + Scheduler.

Persistenz: jobs/recurring.json
Schedule: daily | weekly (Europe/Berlin), Pause-Flag, allow_write (HSEQ-Sandbox).
Ausführung: Freitext-Prompt → tool_loop mit optional Auto-Confirm.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from . import config, llm, log, tool_loop

TZ = ZoneInfo("Europe/Berlin")
HSEQ_ROOT = "/Users/lxndrhbrt/ObsidianVaults/HSEQ Sync"

_WRITE_PREFIXES = (
    "00 Arbeitsfluss/",
    "HSEQ Sync/00 Arbeitsfluss/",
    "Vorlagen/",
    "HSEQ Sync/Vorlagen/",
    "Themen/",
    "HSEQ Sync/Themen/",
)
_WRITE_TOOLS = frozenset({"CreateNote", "ApplyEdit"})

_external_max_override: ContextVar[Optional[int]] = ContextVar(
    "glyph_recurring_external_max", default=None
)

_lock = threading.Lock()  # store + global run lock
_running_id: Optional[str] = None

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE_PATH = os.path.join(_ROOT, "jobs", "recurring.json")
EVENTS_PATH = os.path.join(_ROOT, "logs", "recurring-events.jsonl")


def get_external_max_chars() -> int:
    o = _external_max_override.get()
    if o is not None:
        return int(o)
    return int(getattr(config, "EXTERNAL_MAX_CHARS", 4000) or 4000)


def max_rounds_for(item: Optional[dict]) -> int:
    """Tool-Runden pro Lauf. Default 24; Item darf 1–24 setzen."""
    try:
        n = int((item or {}).get("max_rounds") or 24)
    except (TypeError, ValueError):
        n = 24
    if n < 1 or n > 24:
        return 24
    return n


def _now() -> datetime:
    return datetime.now(TZ)


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or _now()).isoformat(timespec="seconds")


def _ensure_store_dir() -> None:
    os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(EVENTS_PATH), exist_ok=True)


def _default_store() -> dict:
    return {"version": 1, "migrated_hseq_v1": False, "items": []}


def load_store() -> dict:
    _ensure_store_dir()
    if not os.path.isfile(STORE_PATH):
        return _default_store()
    try:
        with open(STORE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _default_store()
        data.setdefault("version", 1)
        data.setdefault("migrated_hseq_v1", False)
        if not isinstance(data.get("items"), list):
            data["items"] = []
        return data
    except (OSError, json.JSONDecodeError):
        return _default_store()


def save_store(data: dict) -> None:
    _ensure_store_dir()
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, STORE_PATH)


def _append_event(event: dict) -> None:
    _ensure_store_dir()
    row = {"ts": _iso(), **event}
    try:
        with open(EVENTS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def list_events(after_ts: str = "", limit: int = 50) -> List[dict]:
    """Events nach optionalem ISO-Timestamp (exklusiv)."""
    if not os.path.isfile(EVENTS_PATH):
        return []
    out: List[dict] = []
    try:
        with open(EVENTS_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = str(row.get("ts") or "")
                if after_ts and ts <= after_ts:
                    continue
                out.append(row)
    except OSError:
        return []
    return out[-max(1, min(limit, 200)) :]


def _normalize_schedule(raw: Any) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or raw.get("type") or "").lower().strip()
    if kind not in ("daily", "weekly"):
        return None
    time_s = str(raw.get("time") or "09:00").strip()
    if not re.match(r"^\d{1,2}:\d{2}$", time_s):
        return None
    hh, mm = time_s.split(":")
    hh_i, mm_i = int(hh), int(mm)
    if not (0 <= hh_i <= 23 and 0 <= mm_i <= 59):
        return None
    time_norm = f"{hh_i:02d}:{mm_i:02d}"
    out: dict = {"kind": kind, "time": time_norm}
    if kind == "weekly":
        # 0=Mo … 6=So (datetime.weekday)
        wd = raw.get("weekday", raw.get("dow", 0))
        try:
            wd_i = int(wd)
        except (TypeError, ValueError):
            return None
        if not (0 <= wd_i <= 6):
            return None
        out["weekday"] = wd_i
    return out


# Erlaubte Script-Jobs (nur unter glyph-agent/scripts/, Whitelist)
_SCRIPT_ALLOW = frozenset(
    {
        "memory_hygiene.py",
        "session_cleanup_legacy.py",
        "wiki_hygiene.py",
    }
)


def _resolve_job_script(script: str) -> Optional[str]:
    """Nur relative Namen aus _SCRIPT_ALLOW unter scripts/."""
    name = os.path.basename((script or "").strip())
    if name not in _SCRIPT_ALLOW:
        return None
    path = os.path.join(_ROOT, "scripts", name)
    if not os.path.isfile(path):
        return None
    return path


def _run_script_job(script_path: str, timeout_s: int = 600) -> dict:
    r = subprocess.run(
        [sys.executable or "python3", script_path],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        cwd=_ROOT,
    )
    answer = (r.stdout or "").strip() or (r.stderr or "").strip() or f"exit {r.returncode}"
    return {
        "ok": r.returncode == 0,
        "answer": answer[:4000],
        "rounds": 0,
        "exit_code": r.returncode,
    }


def _normalize_item(raw: dict) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    prompt = str(raw.get("prompt") or "").strip()
    script = str(raw.get("script") or "").strip()
    # Script-Jobs brauchen keinen LLM-Prompt (Placeholder ok)
    if script:
        if not _resolve_job_script(script):
            return None
        if not prompt:
            prompt = f"(script) {os.path.basename(script)}"
    if not title or not prompt:
        return None
    sched = _normalize_schedule(raw.get("schedule") or {})
    if not sched:
        return None
    iid = str(raw.get("id") or "").strip() or uuid.uuid4().hex[:12]
    out = {
        "id": iid,
        "title": title[:200],
        "prompt": prompt[:50000],
        "schedule": sched,
        "paused": bool(raw.get("paused")),
        "allow_write": bool(raw.get("allow_write")),
        "created_at": str(raw.get("created_at") or _iso()),
        "last_run_at": raw.get("last_run_at"),
        "last_status": raw.get("last_status"),  # ok | error | skipped | empty
        "last_answer_preview": raw.get("last_answer_preview"),
        "last_stamp": raw.get("last_stamp"),  # YYYY-MM-DD or YYYY-MM-DD-HH:MM due key
    }
    finish = str(raw.get("pass") or "").strip()[:400]
    if finish:
        out["pass"] = finish
    if script:
        out["script"] = os.path.basename(script)
    try:
        mr = int(raw.get("max_rounds") or 0)
    except (TypeError, ValueError):
        mr = 0
    if 1 <= mr <= 24:
        out["max_rounds"] = mr
    return out


def list_items() -> List[dict]:
    store = load_store()
    return [x for x in (_normalize_item(i) for i in store.get("items") or []) if x]


def get_item(item_id: str) -> Optional[dict]:
    for it in list_items():
        if it["id"] == item_id:
            return it
    return None


def create_item(payload: dict) -> dict:
    item = _normalize_item(
        {
            **(payload or {}),
            "id": (payload or {}).get("id") or uuid.uuid4().hex[:12],
            "created_at": _iso(),
            "last_run_at": None,
            "last_status": None,
            "last_answer_preview": None,
            "last_stamp": None,
        }
    )
    if not item:
        raise ValueError("Ungültige To-do (title, prompt, schedule nötig)")
    with _lock:
        store = load_store()
        items = store.get("items") or []
        if any(str(i.get("id")) == item["id"] for i in items):
            raise ValueError(f"id existiert: {item['id']}")
        items.append(item)
        store["items"] = items
        save_store(store)
    log.log("recurring_create", id=item["id"], title=item["title"][:80])
    return item


def update_item(item_id: str, patch: dict) -> dict:
    with _lock:
        store = load_store()
        items = store.get("items") or []
        found = None
        idx = -1
        for i, raw in enumerate(items):
            if str(raw.get("id")) == item_id:
                found = raw
                idx = i
                break
        if found is None:
            raise ValueError(f"To-do nicht gefunden: {item_id}")
        merged = {**found, **(patch or {}), "id": item_id}
        # schedule nested merge
        if isinstance(patch.get("schedule"), dict) and isinstance(found.get("schedule"), dict):
            merged["schedule"] = {**found["schedule"], **patch["schedule"]}
        item = _normalize_item(merged)
        if not item:
            raise ValueError("Patch ergibt ungültige To-do")
        # preserve run metadata unless patched
        for k in ("last_run_at", "last_status", "last_answer_preview", "last_stamp", "created_at"):
            if k not in (patch or {}) and found.get(k) is not None:
                item[k] = found.get(k)
        items[idx] = item
        store["items"] = items
        save_store(store)
    log.log("recurring_update", id=item_id)
    return item


def delete_item(item_id: str) -> bool:
    with _lock:
        store = load_store()
        items = store.get("items") or []
        new_items = [i for i in items if str(i.get("id")) != item_id]
        if len(new_items) == len(items):
            return False
        store["items"] = new_items
        save_store(store)
    log.log("recurring_delete", id=item_id)
    return True


def set_paused(item_id: str, paused: bool) -> dict:
    return update_item(item_id, {"paused": bool(paused)})


def _path_allowed(path: str) -> bool:
    p = (path or "").strip().replace("\\", "/")
    if not p:
        return False
    if p.startswith(HSEQ_ROOT):
        p = p[len(HSEQ_ROOT) :].lstrip("/")
    for pref in _WRITE_PREFIXES:
        if p.startswith(pref) or p == pref.rstrip("/"):
            return True
    return False


def make_confirm(allow_write: bool) -> Optional[Callable[[str, dict], bool]]:
    if not allow_write:
        return None  # write tools rejected

    def confirm(tool_name: str, args: dict) -> bool:
        if tool_name not in _WRITE_TOOLS:
            return False
        path = str((args or {}).get("path") or "")
        ok = _path_allowed(path)
        log.log("recurring_confirm", tool=tool_name, path=path[:200], allowed=ok)
        return ok

    return confirm


def _due_stamp(item: dict, now: Optional[datetime] = None) -> Optional[str]:
    """
    Liefert den Stamp-Key für den aktuellen Fälligkeits-Slot, oder None wenn
    heute/jetzt nicht fällig (noch vor Uhrzeit / falscher Wochentag).
    Stamp = YYYY-MM-DD für daily, YYYY-MM-DD für weekly am richtigen Tag
    (ein Lauf pro Kalendertag sobald Uhrzeit erreicht).
    """
    now = now or _now()
    sched = item.get("schedule") or {}
    kind = sched.get("kind")
    time_s = sched.get("time") or "09:00"
    try:
        hh, mm = map(int, time_s.split(":"))
    except ValueError:
        return None
    if kind == "weekly":
        wd = int(sched.get("weekday", 0))
        if now.weekday() != wd:
            return None
    # daily oder weekly am richtigen Tag
    slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now < slot:
        return None
    return now.strftime("%Y-%m-%d")


def is_due(item: dict, now: Optional[datetime] = None) -> bool:
    if item.get("paused"):
        return False
    stamp = _due_stamp(item, now)
    if not stamp:
        return False
    return item.get("last_stamp") != stamp


def _first_line_leer(answer: str) -> bool:
    lines = (answer or "").strip().splitlines()
    if not lines:
        return False
    head = lines[0].strip().upper()
    return head == "LEER" or head.startswith("LEER ") or head.startswith("LEER:")


def answer_is_bare_tool(answer: str) -> bool:
    """True, wenn die Ausgabe nur ein Werkzeug-Aufruf ist und kein Satz."""
    text = (answer or "").strip()
    if not text:
        return False
    from . import tool_registry
    if tool_registry.try_parse_tool_call(text) is None:
        return False
    if tool_registry.looks_like_dsml(text):
        return not (tool_registry.prose_before_dsml(text) or "").strip()
    body = text
    if body.startswith("```"):
        body = body.split("\n", 1)[-1]
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3].strip()
    return body.startswith("{")


def eingang_reads_done(tool_calls) -> bool:
    """True, wenn dieser Lauf den Eingang gelistet und das Verarbeitungslog gelesen hat."""
    listed = False
    logged = False
    for call in tool_calls or []:
        if not call.get("ok") or call.get("canceled"):
            continue
        path = str((call.get("args") or {}).get("path") or "").replace("\\", "/").lower()
        if call.get("tool") == "ListVaultDir" and "arbeitsfluss/eingang" in path:
            listed = True
        if call.get("tool") == "ReadNote" and "verarbeitungslog" in path:
            logged = True
    return listed and logged


def classify_run_status(ok: bool, answer: str, has_pass: bool) -> str:
    """ok | empty | error. LEER nur wenn ein Fertig-Kriterium gesetzt ist."""
    if not ok or answer_is_bare_tool(answer):
        return "error"
    if has_pass:
        if _first_line_leer(answer):
            return "empty"
        if not (answer or "").strip():
            return "error"
    return "ok"


def message_for_run(item: dict, now: Optional[datetime] = None) -> str:
    day = (now or _now()).strftime("%Y-%m-%d")
    message = (item.get("prompt") or "").replace("YYYY-MM-DD", day)
    finish = str(item.get("pass") or "").strip()
    if not finish:
        return message
    return (
        f"{message}\n\nFertig nur wenn: {finish}\n"
        "Keine neue Arbeit: erste Zeile genau LEER."
    )


def _set_run_result(item_id: str, status: str, answer: str, stamp: Optional[str]) -> None:
    preview = (answer or "")[:400]
    with _lock:
        store = load_store()
        for i, raw in enumerate(store.get("items") or []):
            if str(raw.get("id")) == item_id:
                raw["last_run_at"] = _iso()
                raw["last_status"] = status
                raw["last_answer_preview"] = preview
                if stamp:
                    raw["last_stamp"] = stamp
                store["items"][i] = raw
                break
        save_store(store)


def run_item(item_id: str, force: bool = False) -> dict:
    """
    Führt eine To-do aus. force=True: Stamp ignorieren (Einmal jetzt).
    Globaler Lock: nur ein Lauf gleichzeitig.
    """
    global _running_id
    item = get_item(item_id)
    if not item:
        return {"ok": False, "id": item_id, "error": "nicht gefunden", "answer": ""}

    if item.get("paused") and not force:
        return {
            "ok": True,
            "id": item_id,
            "skipped": True,
            "answer": "pausiert",
            "title": item["title"],
        }

    stamp = _due_stamp(item) if not force else _now().strftime("%Y-%m-%d")
    if not force:
        if not is_due(item):
            return {
                "ok": True,
                "id": item_id,
                "skipped": True,
                "answer": "nicht fällig oder heute schon gelaufen",
                "title": item["title"],
            }

    with _lock:
        if _running_id is not None:
            return {
                "ok": True,
                "id": item_id,
                "skipped": True,
                "answer": f"übersprungen — läuft bereits: {_running_id}",
                "title": item["title"],
            }
        _running_id = item_id

    t0 = time.time()
    script_name = (item.get("script") or "").strip()
    script_path = _resolve_job_script(script_name) if script_name else None
    token = _external_max_override.set(32000)
    log.log(
        "recurring_run_start",
        id=item_id,
        force=force,
        allow_write=item.get("allow_write"),
        script=script_name or None,
    )
    try:
        if script_path:
            result = _run_script_job(script_path, timeout_s=600)
        else:
            # HSEQ-Modelljobs auf dem Cloud-Hop. Der Chat-Singleton (local) bleibt.
            # Zwei-Runden-Grenze gilt nur, solange der Kopf lokal ist.
            eingang = item_id == "td-eingang"
            with llm.cloud_job():
                result = tool_loop.run(
                    message_for_run(item),
                    confirm=make_confirm(bool(item.get("allow_write"))),
                    max_rounds=max_rounds_for(item),
                    precheck=not eingang,
                    step_guard=eingang,
                )
        tool_ok = bool(result.get("ok", True))
        answer = result.get("answer") or ""
        status = classify_run_status(tool_ok, answer, bool(item.get("pass")))
        # „keine Änderungen“ nur, wenn Ordner und Log in diesem Lauf wirklich gelesen wurden.
        if (
            item_id == "td-eingang"
            and status != "error"
            and "HSEQ: keine Änderungen" in answer
            and not eingang_reads_done(result.get("tool_calls"))
        ):
            status = "error"
        ok = status != "error"
        # Scheduled: Stamp bei ok/empty (Retry am selben Tag nur bei Fehler). force: Stamp immer.
        use_stamp = stamp if (ok or force) else None
        _set_run_result(item_id, status, answer, use_stamp)

        duration_ms = int((time.time() - t0) * 1000)
        out = {
            "ok": ok,
            "id": item_id,
            "title": item["title"],
            "force": force,
            "duration_ms": duration_ms,
            "answer": answer,
            "rounds": result.get("rounds", 0),
            "last_status": status,
            "script": script_name or None,
        }
        _append_event(
            {
                "type": "run",
                "id": item_id,
                "title": item["title"],
                "ok": ok,
                "preview": (answer or "")[:280],
                "force": force,
                "script": script_name or None,
            }
        )
        log.log("recurring_run_done", id=item_id, ok=ok, duration_ms=duration_ms)
        return out
    except Exception as e:
        _set_run_result(item_id, "error", str(e), stamp if force else None)
        _append_event(
            {
                "type": "run",
                "id": item_id,
                "title": item["title"],
                "ok": False,
                "preview": str(e)[:280],
                "force": force,
            }
        )
        log.log("recurring_run_error", id=item_id, error=str(e)[:300])
        return {
            "ok": False,
            "id": item_id,
            "title": item["title"],
            "error": str(e),
            "answer": f"Lauf fehlgeschlagen: {e}",
        }
    finally:
        _external_max_override.reset(token)
        with _lock:
            _running_id = None


def run_due() -> dict:
    """Scheduler: alle fälligen To-dos nacheinander (Lock serialisiert)."""
    results = []
    for it in list_items():
        if is_due(it):
            results.append(run_item(it["id"], force=False))
    return {"ok": True, "ran": len([r for r in results if not r.get("skipped")]), "results": results}


# --- Migration: 3 HSEQ-Jobs als To-dos ---

_SEED_PROMPTS = {
    "hseq-eingang": {
        "title": "HSEQ Eingang → Fertig",
        "time": "18:00",
        "kind": "daily",
        "prompt": (
            "HSEQ-Eingang: genau EINE Rohnotiz. Nach der Notiz sofort ApplyEdit — nicht suchen.\n"
            "ReadNote nur mit EXAKTEN Dateinamen: "
            "'Qualitätsmanagement/Audits/2. Audit - MBVD 9001 - Kopf.md' usw. "
            "Kein Archiv. SOFORT ApplyEdit Bericht+Maßnahmenplan in Fertig/, dann Log. "
            "Keine Personennamen. Pfade unter "
            f"{HSEQ_ROOT}/."
        ),
    },
    "hseq-handover": {
        "title": "HSEQ Handover Daily",
        "time": "18:30",
        "kind": "daily",
        "prompt": (
            "HSEQ-Handover. Öffne und führe aus: "
            f"{HSEQ_ROOT}/Vorlagen/Jobs/PROMPT-Handover.md\n"
            f"Schreibe nur: {HSEQ_ROOT}/00 Arbeitsfluss/Daily/YYYY-MM-DD.md "
            "(heute Europe/Berlin). Quellen: Eingang/, Fertig/, Verarbeitungslog. "
            "Daily beginnt mit Briefing 3 Zeilen: Neu / Offen / Konflikt-Stale. "
            "Antwort an Nutzer = dieselben 3 Zeilen. Eingang-mit-Log = Beleg, nicht Offen. "
            "Keine Personennamen. Alles leer: 'Handover: ruhig (YYYY-MM-DD)' + Briefing mit —. "
            "Selbstständig, keine Rückfragen."
        ),
    },
    "hseq-aus-fertig-lernen": {
        "title": "HSEQ Aus Fertig lernen",
        "time": "19:00",
        "kind": "weekly",
        "weekday": 4,  # Freitag
        "prompt": (
            "HSEQ Aus-Fertig-lernen. Öffne und führe aus: "
            f"{HSEQ_ROOT}/Vorlagen/Jobs/PROMPT-Aus-Fertig-lernen.md\n"
            "Maximal EINE kleine Änderung an Vorlage oder Themen-Notiz — oder nichts. "
            "Keine neuen Berichte. Nichts: 'Aus Fertig lernen: nichts Neues'. "
            "Selbstständig, keine Rückfragen."
        ),
    },
}


def ensure_migrated() -> dict:
    """Einmalig: 3 HSEQ-To-dos + Nightly-Scripts (Memory/Cleanup) anlegen."""
    with _lock:
        store = load_store()
        items = list(store.get("items") or [])
        existing_ids = {str(i.get("id")) for i in items}
        existing_titles = {str(i.get("title")) for i in items}
        added = []

        if not store.get("migrated_hseq_v1"):
            for sid, spec in _SEED_PROMPTS.items():
                if spec["title"] in existing_titles:
                    continue
                sched: dict = {"kind": spec["kind"], "time": spec["time"]}
                if spec["kind"] == "weekly":
                    sched["weekday"] = spec["weekday"]
                item = _normalize_item(
                    {
                        "id": sid.replace("hseq-", "td-")[:12],
                        "title": spec["title"],
                        "prompt": spec["prompt"],
                        "schedule": sched,
                        "paused": False,
                        "allow_write": True,
                        "created_at": _iso(),
                    }
                )
                if item:
                    item["id"] = {
                        "hseq-eingang": "td-eingang",
                        "hseq-handover": "td-handover",
                        "hseq-aus-fertig-lernen": "td-lernen",
                    }.get(sid, item["id"])
                    items.append(item)
                    added.append(item["id"])
            store["migrated_hseq_v1"] = True

        # Nightly: ex OpenClaw 03:00 / 06:00 → Glyph scripts
        if not store.get("migrated_nightly_v1"):
            nightly = [
                {
                    "id": "td-memory",
                    "title": "Memory-Hygiene (Glyph)",
                    "prompt": "(script) memory_hygiene.py — Daily anlegen, MEMORY.md PII-Scan",
                    "script": "memory_hygiene.py",
                    "schedule": {"kind": "daily", "time": "03:00"},
                    "paused": False,
                    "allow_write": False,
                    "created_at": _iso(),
                },
                {
                    "id": "td-cleanup",
                    "title": "Session-Cleanup (Legacy OpenClaw)",
                    "prompt": "(script) session_cleanup_legacy.py — nur wenn openclaw CLI da",
                    "script": "session_cleanup_legacy.py",
                    "schedule": {"kind": "daily", "time": "06:00"},
                    "paused": False,
                    "allow_write": False,
                    "created_at": _iso(),
                },
            ]
            for raw in nightly:
                if raw["id"] in existing_ids or raw["title"] in existing_titles:
                    continue
                item = _normalize_item(raw)
                if item:
                    items.append(item)
                    added.append(item["id"])
            store["migrated_nightly_v1"] = True

        store["items"] = items
        save_store(store)
    log.log("recurring_migrated", added=added)
    return {
        "ok": True,
        "migrated": bool(added),
        "added": added,
        "items": len(items),
    }
