# -*- coding: utf-8 -*-
"""Gemeinsame, manuell übergebene Glyph-Aufgaben.

Eine Aufgabe ist kein Recurring-To-do: Sie transportiert ausgewählte Belege
zwischen Köpfen, ohne deren Sessions oder Vault-Kontext pauschal zu teilen.
Persistenz liegt bewusst in ~/.glyph neben Vertrag, Memory und Skills.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
STORE_PATH = os.path.expanduser("~/.glyph/tasks.json")
STATUSES = {"new", "analysis", "needs_input", "ready_to_build", "building", "review", "done", "blocked"}
HEADS = {"grok", "_code", "codex", "_tick", "glyph-agent"}
_ARTIFACT_MAX = 80_000


def artifact_dir() -> str:
    """Markdown-Artefakte neben der SoT-Datei (Tests: neben temp STORE_PATH)."""
    return os.path.join(os.path.dirname(os.path.abspath(STORE_PATH)), "tasks")


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _load() -> dict:
    try:
        with open(STORE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"version": 1, "items": []}


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="tasks-", suffix=".json", dir=os.path.dirname(STORE_PATH))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, STORE_PATH)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _clean_text(raw: Any, limit: int) -> str:
    return str(raw or "").replace("\x00", "").strip()[:limit]


def _jsonable(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k)[:80]: _jsonable(v, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        return [_jsonable(v, depth + 1) for v in value[:40]]
    return str(value)[:200]


def _clean_trace(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {}
    keep = {}
    for key in ("provider", "model", "fallback_used", "request_id"):
        if key in raw:
            val = raw[key]
            if val is None or isinstance(val, (str, int, float, bool)):
                keep[key] = val
    if isinstance(raw.get("tool_calls"), list):
        calls = []
        for item in raw["tool_calls"][:24]:
            if not isinstance(item, dict):
                continue
            calls.append({
                "name": _clean_text(item.get("name") or item.get("tool"), 80),
                "status": _clean_text(item.get("status"), 40),
            })
        if calls:
            keep["tool_calls"] = calls
    if isinstance(raw.get("steps"), list):
        keep["steps"] = [_jsonable(x) for x in raw["steps"][:40]]
    retrieval = raw.get("retrieval")
    if isinstance(retrieval, dict):
        sources = retrieval.get("sources")
        keep["retrieval"] = {
            "type": _clean_text(retrieval.get("type"), 40),
            "mode": _clean_text(retrieval.get("mode"), 40),
            "status": _clean_text(retrieval.get("status"), 40),
            "selected": retrieval.get("selected") if isinstance(retrieval.get("selected"), int) else None,
            "candidates": retrieval.get("candidates") if isinstance(retrieval.get("candidates"), int) else None,
            "threshold": retrieval.get("threshold") if isinstance(retrieval.get("threshold"), (int, float)) else None,
            "sources": [str(x)[:240] for x in sources[:8]] if isinstance(sources, list) else [],
        }
    return keep


def _clean_attachment(raw: Any) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    name = _clean_text(raw.get("name"), 240)
    path = _clean_text(raw.get("path") or raw.get("uri"), 1000)
    if not name and not path:
        return None
    size = raw.get("size")
    try:
        size_n = int(size) if size is not None and str(size) != "" else 0
    except (TypeError, ValueError):
        size_n = 0
    return {
        "name": name,
        "path": path,
        "mimeType": _clean_text(raw.get("mimeType") or raw.get("mime"), 80),
        "size": size_n,
    }


def _normalize(raw: dict) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    title = _clean_text(raw.get("title"), 200)
    if not title:
        return None
    status = _clean_text(raw.get("status") or "new", 40)
    if status not in STATUSES:
        status = "new"
    target = _clean_text(raw.get("target"), 40)
    if target and target not in HEADS:
        target = ""
    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
    return {
        "id": _clean_text(raw.get("id"), 80) or uuid.uuid4().hex[:12],
        "title": title,
        "status": status,
        "target": target,
        "source": _clean_text(raw.get("source"), 40) or "glyph-agent",
        "workspace": _clean_text(raw.get("workspace"), 1000) or None,
        "summary": _clean_text(raw.get("summary"), 12000),
        "pass": _clean_text(raw.get("pass"), 400),
        "artifact": _clean_text(raw.get("artifact"), 1000),
        "evidence": {
            "prompt": _clean_text(evidence.get("prompt"), 8000),
            "answer": _clean_text(evidence.get("answer"), 16000),
            "trace": _clean_trace(evidence.get("trace")),
            "attachments": [x for x in (_clean_attachment(a) for a in (evidence.get("attachments") or [])[:8]) if x],
        },
        "created_at": _clean_text(raw.get("created_at"), 40) or _now(),
        "updated_at": _clean_text(raw.get("updated_at"), 40) or _now(),
        "events": [x for x in (raw.get("events") or []) if isinstance(x, dict)][-50:],
    }


def list_items() -> list[dict]:
    return [x for x in (_normalize(i) for i in _load().get("items") or []) if x]


def get_item(item_id: str) -> Optional[dict]:
    return next((x for x in list_items() if x["id"] == item_id), None)


def _require_finish(item: dict) -> None:
    if item["status"] == "done" and not item.get("artifact"):
        raise ValueError("Fertig braucht ein Artefakt — Pfad oder Ort des Ergebnisses")


def create_item(payload: dict) -> dict:
    now = _now()
    item = _normalize({**(payload or {}), "id": uuid.uuid4().hex[:12], "created_at": now, "updated_at": now})
    if not item:
        raise ValueError("Aufgabe braucht einen Titel")
    if not item.get("pass"):
        raise ValueError("Aufgabe braucht ein Fertig-Kriterium")
    ev = item.get("evidence") or {}
    if not (ev.get("prompt") or "").strip() or not (ev.get("answer") or "").strip():
        raise ValueError("Aufgabe braucht Meldung und Antwort")
    _require_finish(item)
    item["events"] = [{"at": now, "type": "created", "by": item["source"], "text": "Aufgabe übergeben"}]
    data = _load()
    data.setdefault("items", []).append(item)
    _save(data)
    return item


def update_item(item_id: str, payload: dict) -> dict:
    data = _load()
    for index, raw in enumerate(data.get("items") or []):
        if str(raw.get("id") or "") != item_id:
            continue
        before = _normalize(raw)
        merged = {**raw, **(payload or {}), "id": item_id, "updated_at": _now()}
        item = _normalize(merged)
        if not item:
            raise ValueError("Aufgabe braucht einen Titel")
        _require_finish(item)
        if before and item["status"] != before["status"]:
            item["events"].append({"at": item["updated_at"], "type": "status", "by": _clean_text((payload or {}).get("by"), 40) or "user", "text": item["status"]})
        data["items"][index] = item
        _save(data)
        return item
    raise ValueError("Aufgabe nicht gefunden")


def delete_item(item_id: str) -> bool:
    """Entfernt die Aufgabe aus dem Plan. False wenn unbekannt."""
    wanted = str(item_id or "")
    if not wanted:
        return False
    data = _load()
    items = data.get("items") or []
    kept = [raw for raw in items if str(raw.get("id") or "") != wanted]
    if len(kept) == len(items):
        return False
    data["items"] = kept
    _save(data)
    return True


def finish_item(item_id: str, content: str, summary: str = "", by: str = "_code") -> dict:
    """Schreibt das Artefakt und setzt status=done. Für ^_Code TaskFinish."""
    item = get_item(item_id)
    if not item:
        raise ValueError("Aufgabe nicht gefunden")
    body = str(content or "").replace("\x00", "").strip()
    if not body:
        raise ValueError("Fertig braucht ein Artefakt — Inhalt fehlt")
    if len(body) > _ARTIFACT_MAX:
        body = body[: _ARTIFACT_MAX - 20] + "\n…[gekürzt]"
    dest_dir = artifact_dir()
    os.makedirs(dest_dir, exist_ok=True)
    fname = f"{item['id']}.md"
    dest = os.path.join(dest_dir, fname)
    fd, tmp = tempfile.mkstemp(prefix="task-art-", suffix=".md", dir=dest_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
            if not body.endswith("\n"):
                f.write("\n")
        os.replace(tmp, dest)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    home_tasks = os.path.join(os.path.expanduser("~/.glyph"), "tasks")
    try:
        live = os.path.realpath(dest_dir) == os.path.realpath(home_tasks)
    except OSError:
        live = False
    artifact = f"~/.glyph/tasks/{fname}" if live else dest
    return update_item(item_id, {
        "status": "done",
        "artifact": artifact,
        "summary": summary or body.split("\n", 1)[0][:200],
        "by": by or "_code",
    })


def handoff_prompt(item_id: str) -> str:
    item = get_item(item_id)
    if not item:
        raise ValueError("Aufgabe nicht gefunden")
    ev = item["evidence"]
    attachment_lines = []
    for att in ev.get("attachments") or []:
        name = _clean_text(att.get("name") or att.get("path"), 240)
        path = _clean_text(att.get("path"), 1000)
        if name:
            attachment_lines.append(f"- {name}" + (f" ({path})" if path and path != name else ""))
    return "\n".join(x for x in [
        f"# Glyph-Aufgabe: {item['title']}",
        f"Id: {item['id']}",
        f"Status: {item['status']} · Übergeben von: {item['source']}",
        f"Zielkopf: {item['target'] or 'noch nicht zugewiesen'}",
        f"Workspace: {item['workspace']}" if item.get("workspace") else "",
        f"Fertig wenn: {item['pass']}" if item.get("pass") else "",
        f"Artefakt: {item['artifact']}" if item.get("artifact") else "",
        "\n## Übergabe\n" + item["summary"] if item["summary"] else "",
        "\n## Ursprüngliche Meldung\n" + ev["prompt"] if ev["prompt"] else "",
        "\n## Bisheriges Ergebnis\n" + ev["answer"] if ev["answer"] else "",
        "\n## Übergebene Anhänge\n" + "\n".join(attachment_lines) if attachment_lines else "",
        "\nArbeite nur auf Basis dieser Aufgabe. Vor-Antwort ist Beleg — nicht neu den Tree walken. "
        "CONTEXT-Map zuerst, nur Lücken lesen. Rückfragen als Status `needs_input`. "
        "Keine Build-Änderung ohne Nutzerauftrag. "
        "Fertig = Tool TaskFinish (id, content, summary) — Chat allein schließt nicht.",
    ] if x)
