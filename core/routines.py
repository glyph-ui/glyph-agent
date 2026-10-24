# -*- coding: utf-8 -*-
"""^_tick — Routine-Kopf: Job-Ordner und Plan/Aktivität.

Der Job-Ordner (SoT: ``config.ROUTINES_ROOT``, Default
``~/ObsidianVaults/memory-wiki/Routinen``) ist der sichtbare, prüfbare Zustand
der täglichen Arbeit:

    Daily/    wiederkehrende Jobs (bleiben liegen)
    Inbox/    neue Aufträge (nach Erledigung nach Done/)
    Blocked/  Aufträge mit genau einer offenen Frage am Dateianfang
    Done/     erledigte Inbox-Aufträge, Dateiname mit Datum
    Log/      ein Protokoll pro Lauf: YYYY-MM-DD_HHMM.md

Harte Regeln (aus dem Auftrag): nie löschen, nie überschreiben — bei
Namenskollision bleibt die Quelle unverändert und der Grund geht in den
Rückgabewert; kein Zugriff auf Passwort-Ordner; alle Pfade bleiben unterhalb
des Job-Ordners.

Zusätzlich die Brücke nach Plan & Aktivität: ``plan_overview``,
``create_routine``, ``update_routine``, ``run_routine`` lesen/schreiben die
bestehende SoT ``jobs/recurring.json`` (core.recurring) und die Glyph-Aufgaben
(core.tasks) — keine zweite Wahrheit.

Copyright (c) 2026 Alexander Hubert · SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from . import config, log

TZ = ZoneInfo("Europe/Berlin")

BOXES = ("Daily", "Inbox", "Blocked", "Done", "Log")
# Reihenfolge der Abarbeitung: erst Daily, dann Inbox.
WORK_BOXES = ("Daily", "Inbox")
READ_BOXES = ("Daily", "Inbox", "Blocked")

_NAME_RE = re.compile(r"[^0-9A-Za-zÄÖÜäöüß._\- ]+")
_DENY_RE = re.compile(r"passwor|passwörter|password|secret|\.env", re.IGNORECASE)
_MAX_BODY = 200_000


def root() -> str:
    """Absoluter Job-Ordner (expandiert, ohne Trailing-Slash)."""
    return os.path.abspath(os.path.expanduser(config.ROUTINES_ROOT)).rstrip("/")


def ensure_layout() -> Dict[str, str]:
    """Legt fehlende Ordner an; gibt die Pfade zurück. Nie Dateien."""
    base = root()
    out = {"root": base}
    for box in BOXES:
        path = os.path.join(base, box)
        os.makedirs(path, exist_ok=True)
        out[box] = path
    return out


def _inside(path: str) -> bool:
    base = root() + os.sep
    real = os.path.abspath(path)
    return real == root() or real.startswith(base)


def _safe_name(name: str, *, stamp: bool = False) -> str:
    """Dateiname ohne Pfadanteile. ``stamp`` erzwingt das Done-Datum."""
    raw = os.path.basename(str(name or "").strip())
    if not raw:
        raise ValueError("Dateiname fehlt")
    if _DENY_RE.search(raw):
        raise ValueError("Passwort-/Secret-Dateien sind für ^_tick tabu")
    stem, ext = os.path.splitext(raw)
    if ext.lower() != ".md":
        stem, ext = raw, ".md"
    stem = _NAME_RE.sub("-", stem).strip(" .-") or "job"
    stem = stem[:120]
    if stamp and not re.match(r"^\d{4}-\d{2}-\d{2}[_ ]", stem):
        stem = f"{datetime.now(TZ).strftime('%Y-%m-%d')}_{stem}"
    return stem + ".md"


def _digest(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _head_line(body: str) -> str:
    for line in (body or "").splitlines():
        s = line.strip()
        if s:
            return s[:160]
    return ""


def _box_dir(box: str, *, create: bool = False) -> str:
    name = str(box or "").strip().capitalize()
    if name not in BOXES:
        raise ValueError(f"Unbekannter Ordner '{box}'. Erlaubt: {', '.join(BOXES)}")
    path = os.path.join(root(), name)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def _resolve_existing(name: str, box: Optional[str] = None) -> Optional[Dict[str, str]]:
    """Findet eine Job-Datei; ohne ``box`` in Daily → Inbox → Blocked."""
    boxes = [_box_dir(box)] if box else [_box_dir(b) for b in READ_BOXES]
    wanted = os.path.basename(str(name or "").strip())
    if not wanted:
        return None
    stem = os.path.splitext(wanted)[0]
    for path in boxes:
        for candidate in (wanted, stem + ".md"):
            full = os.path.join(path, os.path.basename(candidate))
            if os.path.isfile(full) and _inside(full):
                return {"box": os.path.basename(path), "name": os.path.basename(full), "path": full}
        # Toleranz: eindeutiger Präfix-Treffer (Modell darf kürzen)
        hits = [
            n
            for n in sorted(os.listdir(path))
            if n.lower().startswith(stem.lower()) and n.lower().endswith(".md")
        ]
        if len(hits) == 1:
            return {"box": os.path.basename(path), "name": hits[0], "path": os.path.join(path, hits[0])}
    return None


def list_jobs(box: Optional[str] = None, *, include_done: bool = False) -> List[dict]:
    """Job-Dateien mit Datum, Größe und Kopfzeile. Daily zuerst, dann Inbox."""
    boxes = [_box_dir(box)] if box else [
        _box_dir(b) for b in (READ_BOXES + (("Done",) if include_done else ()))
    ]
    out: List[dict] = []
    for path in boxes:
        if not os.path.isdir(path):
            continue
        for name in sorted(os.listdir(path)):
            if not name.lower().endswith(".md") or name.startswith("."):
                continue
            full = os.path.join(path, name)
            if not os.path.isfile(full):
                continue
            try:
                st = os.stat(full)
                with open(full, encoding="utf-8", errors="replace") as f:
                    body = f.read()
            except OSError:
                continue
            out.append({
                "box": os.path.basename(path),
                "name": name,
                "path": full,
                "date": datetime.fromtimestamp(st.st_mtime, TZ).strftime("%Y-%m-%d"),
                "mtime": datetime.fromtimestamp(st.st_mtime, TZ).isoformat(timespec="seconds"),
                "size": st.st_size,
                "head": _head_line(body),
            })
    return out


def read_job(name: str, box: Optional[str] = None) -> dict:
    """Liest einen Job vollständig (inkl. Kopfzeile und Pfad)."""
    found = _resolve_existing(name, box)
    if not found:
        return {"ok": False, "error": f"Job '{name}' nicht gefunden (Daily/Inbox/Blocked)."}
    try:
        with open(found["path"], encoding="utf-8", errors="replace") as f:
            body = f.read()
    except OSError as e:
        return {"ok": False, "error": f"nicht lesbar: {e}"}
    return {"ok": True, "box": found["box"], "name": found["name"], "path": found["path"], "body": body}


def write_job(box: str, name: str, body: str) -> dict:
    """Legt eine Job-Datei NEU an. Existiert sie, bleibt sie unverändert."""
    text = str(body or "").replace("\x00", "")
    if not text.strip():
        return {"ok": False, "error": "Leerer Job-Text."}
    if len(text) > _MAX_BODY:
        return {"ok": False, "error": f"Job-Text zu groß ({len(text)} > {_MAX_BODY})."}
    try:
        target_dir = _box_dir(box, create=True)
        fname = _safe_name(name)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if os.path.basename(target_dir) not in ("Daily", "Inbox", "Blocked"):
        return {"ok": False, "error": "Neue Jobs nur in Daily/, Inbox/ oder Blocked/."}
    full = os.path.join(target_dir, fname)
    if not _inside(full):
        return {"ok": False, "error": "Pfad außerhalb des Job-Ordners."}
    if os.path.exists(full):
        return {
            "ok": False,
            "error": f"Existiert bereits, unverändert gelassen: {fname}",
            "existing": full,
        }
    tmp = full + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text if text.endswith("\n") else text + "\n")
        os.replace(tmp, full)
    except OSError as e:
        return {"ok": False, "error": f"Schreiben fehlgeschlagen: {e}"}
    log.log("routine_job_write", box=os.path.basename(target_dir), name=fname)
    return {"ok": True, "box": os.path.basename(target_dir), "name": fname, "path": full, "bytes": len(text)}


def move_job(name: str, to: str, question: str = "") -> dict:
    """Verschiebt einen Job (z. B. Inbox → Done/Blocked).

    Kopie zuerst, Quelle erst danach entfernen — und nur, wenn die Kopie
    inhaltlich identisch ist. Namenskollision: nichts wird überschrieben,
    die Quelle bleibt liegen.
    """
    dest_box = str(to or "").strip().capitalize()
    if dest_box not in ("Daily", "Inbox", "Blocked", "Done"):
        return {"ok": False, "error": f"Zielordner '{to}' nicht erlaubt."}
    found = _resolve_existing(name)
    if not found:
        return {"ok": False, "error": f"Job '{name}' nicht gefunden."}
    if found["box"] == dest_box:
        return {"ok": False, "error": f"Job liegt schon in {dest_box}/."}
    try:
        with open(found["path"], encoding="utf-8", errors="replace") as f:
            body = f.read()
    except OSError as e:
        return {"ok": False, "error": f"nicht lesbar: {e}"}

    if dest_box == "Blocked":
        q = " ".join(str(question or "").split()).strip()
        if not q:
            return {"ok": False, "error": "Blocked braucht genau eine konkrete Frage."}
        if not q.endswith("?"):
            q += "?"
        body = f"> **Offene Frage:** {q}\n\n{body}"

    try:
        target_dir = _box_dir(dest_box, create=True)
        fname = _safe_name(found["name"], stamp=(dest_box == "Done"))
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    full = os.path.join(target_dir, fname)
    if not _inside(full):
        return {"ok": False, "error": "Pfad außerhalb des Job-Ordners."}
    if os.path.exists(full):
        return {
            "ok": False,
            "error": f"Ziel existiert bereits, unverändert gelassen: {dest_box}/{fname}",
            "source_kept": found["path"],
            "existing": full,
        }
    tmp = full + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, full)
    except OSError as e:
        return {"ok": False, "error": f"Schreiben fehlgeschlagen: {e}"}
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            copied = f.read()
    except OSError as e:
        return {"ok": False, "error": f"Kopie nicht prüfbar: {e}", "dest": full}
    if _digest(copied) != _digest(body):
        return {"ok": False, "error": "Kopie weicht ab — Quelle bleibt.", "dest": full}
    try:
        os.remove(found["path"])
    except OSError as e:
        return {"ok": False, "error": f"Kopie liegt, Quelle nicht entfernbar: {e}", "dest": full}
    log.log("routine_job_move", **{"from": found["box"], "to": dest_box, "name": fname})
    return {
        "ok": True,
        "from": found["box"],
        "to": dest_box,
        "name": fname,
        "path": full,
        "question": question if dest_box == "Blocked" else "",
    }


def append_log(lines, *, run_at: Optional[datetime] = None) -> dict:
    """Hängt Zeilen an das Laufprotokoll Log/YYYY-MM-DD_HHMM.md (append-only)."""
    if isinstance(lines, str):
        items = [lines]
    elif isinstance(lines, (list, tuple)):
        items = [str(x) for x in lines if str(x).strip()]
    else:
        return {"ok": False, "error": "Log braucht Text oder Liste von Zeilen."}
    if not items:
        return {"ok": False, "error": "Leeres Protokoll."}
    now = run_at or datetime.now(TZ)
    path = os.path.join(root(), "Log", now.strftime("%Y-%m-%d_%H%M") + ".md")
    if not _inside(path):
        return {"ok": False, "error": "Pfad außerhalb des Job-Ordners."}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fresh = not os.path.exists(path)
    block = []
    if fresh:
        block.append(f"# Lauf {now.strftime('%Y-%m-%d %H:%M')} (Europe/Berlin)")
        block.append("")
    block.extend(items)
    block.append("")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(block) + "\n")
    except OSError as e:
        return {"ok": False, "error": f"Protokoll nicht schreibbar: {e}"}
    return {"ok": True, "path": path, "created": fresh, "lines": len(items)}


# --- Plan & Aktivität (SoT: recurring.json + tasks.json) -------------------


def plan_overview(include_done_tasks: bool = False) -> dict:
    """Wiederkehrende To-dos und Glyph-Aufgaben — die Sicht aus Plan & Aktivität."""
    from . import recurring as _rec
    from . import tasks as _tasks

    try:
        _rec.ensure_migrated()
    except Exception:
        pass
    items = []
    for it in _rec.list_items():
        sched = it.get("schedule") or {}
        items.append({
            "id": it.get("id"),
            "title": it.get("title"),
            "schedule": sched,
            "paused": bool(it.get("paused")),
            "allow_write": bool(it.get("allow_write")),
            "script": it.get("script"),
            "pass": it.get("pass"),
            "last_run_at": it.get("last_run_at"),
            "last_status": it.get("last_status"),
        })
    auftraege = []
    for t in _tasks.list_items():
        if not include_done_tasks and t.get("status") == "done":
            continue
        auftraege.append({
            "id": t.get("id"),
            "title": t.get("title"),
            "status": t.get("status"),
            "target": t.get("target"),
            "source": t.get("source"),
            "pass": t.get("pass"),
            "updated_at": t.get("updated_at"),
        })
    return {"ok": True, "jobs_root": root(), "recurring": items, "tasks": auftraege}


def overview(limit_files: int = 40, limit_runs: int = 8) -> dict:
    """Alles, was ^_tick auf einen Blick zeigt — Zähler, Ordner, letzte Läufe.

    Eine Quelle für UI und Chat: wiederkehrende To-dos (aktiv/pausiert/fällig),
    offene Glyph-Aufgaben nach Status, Job-Ordner je Box und die letzten Läufe
    aus dem recurring-Eventlog. Read-only.
    """
    from . import recurring as _rec
    from . import tasks as _tasks

    ensure_layout()
    files_cap = max(1, min(int(limit_files or 40), 200))
    boxes: Dict[str, dict] = {}
    for box in ("Daily", "Inbox", "Blocked", "Done"):
        jobs = list_jobs(box)
        jobs.sort(key=lambda j: (j.get("date") or "", j.get("name") or ""), reverse=(box == "Done"))
        boxes[box] = {"count": len(jobs), "files": jobs[:files_cap]}

    try:
        _rec.ensure_migrated()
    except Exception:
        pass
    todos = _rec.list_items()
    active = [t for t in todos if not t.get("paused")]
    paused = [t for t in todos if t.get("paused")]
    due_ids: List[str] = []
    for t in active:
        try:
            if _rec.is_due(t):
                due_ids.append(str(t.get("id")))
        except Exception:
            continue

    runs: List[dict] = []
    try:
        for ev in reversed(_rec.list_events(limit=max(1, min(int(limit_runs or 8), 50)))):
            runs.append({
                "ts": ev.get("ts"),
                "id": ev.get("id"),
                "title": ev.get("title"),
                "ok": ev.get("ok"),
                "preview": (str(ev.get("preview") or ""))[:200],
                "force": bool(ev.get("force")),
            })
    except Exception:
        runs = []

    task_items = _tasks.list_items()
    by_status: Dict[str, int] = {}
    for t in task_items:
        key = str(t.get("status") or "new")
        by_status[key] = by_status.get(key, 0) + 1
    open_tasks = [t for t in task_items if t.get("status") != "done"]

    return {
        "ok": True,
        "jobs_root": root(),
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "counts": {
            "todos_active": len(active),
            "todos_paused": len(paused),
            "todos_due": len(due_ids),
            "todos_total": len(todos),
            "tasks_open": len(open_tasks),
            "tasks_total": len(task_items),
            "jobs_daily": boxes["Daily"]["count"],
            "jobs_inbox": boxes["Inbox"]["count"],
            "jobs_blocked": boxes["Blocked"]["count"],
            "jobs_done": boxes["Done"]["count"],
        },
        "job_boxes": boxes,
        "todos_due_ids": due_ids,
        "todos": [
            {
                "id": t.get("id"),
                "title": t.get("title"),
                "schedule": t.get("schedule"),
                "paused": bool(t.get("paused")),
                "allow_write": bool(t.get("allow_write")),
                "script": t.get("script"),
                "pass": t.get("pass"),
                "last_run_at": t.get("last_run_at"),
                "last_status": t.get("last_status"),
            }
            for t in todos
        ],
        "tasks": [
            {
                "id": t.get("id"),
                "title": t.get("title"),
                "status": t.get("status"),
                "target": t.get("target"),
                "pass": t.get("pass"),
                "updated_at": t.get("updated_at"),
            }
            for t in open_tasks
        ],
        "tasks_by_status": by_status,
        "last_runs": runs,
    }


def create_routine(payload: dict) -> dict:
    """Legt ein wiederkehrendes To-do an (erscheint in Plan & Aktivität)."""
    from . import recurring as _rec

    data = dict(payload or {})
    sched = data.get("schedule")
    if isinstance(sched, str):
        data["schedule"] = {"kind": "daily", "time": sched.strip()}
    if not data.get("pass"):
        return {"ok": False, "error": "Kein Fertig-Kriterium (pass) — Job ohne prüfbare Zeile wird nicht angelegt."}
    try:
        _rec.ensure_migrated()
        item = _rec.create_item(data)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "item": item}


def update_routine(item_id: str, patch: dict) -> dict:
    """Ändert ein To-do (Zeitplan, Prompt, allow_write) oder pausiert es."""
    from . import recurring as _rec

    data = dict(patch or {})
    if isinstance(data.get("schedule"), str):
        data["schedule"] = {"kind": "daily", "time": data["schedule"].strip()}
    try:
        _rec.ensure_migrated()
        item = _rec.update_item(str(item_id or "").strip(), data)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "item": item}


def set_routine_paused(item_id: str, paused: bool = True) -> dict:
    from . import recurring as _rec

    try:
        item = _rec.set_paused(str(item_id or "").strip(), bool(paused))
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "item": item}


def run_routine(item_id: str, force: bool = True) -> dict:
    """Löst ein To-do jetzt aus (wie Kalender → Ausführen)."""
    from . import recurring as _rec

    rid = str(item_id or "").strip()
    if not rid:
        return {"ok": False, "error": "id fehlt."}
    try:
        _rec.ensure_migrated()
        result = _rec.run_item(rid, force=bool(force))
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if not isinstance(result, dict):
        return {"ok": False, "error": "Unerwartetes Ergebnis."}
    return {"ok": bool(result.get("ok", True)), **result}


def finish_task(task_id: str, content: str, summary: str = "", by: str = "_tick") -> dict:
    """Schließt eine Glyph-Aufgabe (Artefakt + status=done)."""
    from . import tasks as _tasks

    try:
        item = _tasks.finish_item(task_id, content, summary=summary, by=by)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "item": item}
