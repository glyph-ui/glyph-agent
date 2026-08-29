# -*- coding: utf-8 -*-
"""
Memory-Lese-Tools für ^_Code — OpenClaw-Memory als Kontext über Sessions.

Read-only: MEMORY.md + Tageslogs aus dem OpenClaw-Workspace. Kein Schreiben.
Pfad: ~/.openclaw/workspace (MEMORY.md, memory/YYYY-MM-DD.md)
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

from . import config, log

_MEMORY_DIR_NAMES = ("memory",)


def _memory_root() -> Path:
    """OpenClaw-Workspace-Root (MEMORY.md + memory/)."""
    # 1. Explizit konfiguriert
    raw = os.environ.get("OPENCLAW_WORKSPACE", "") or getattr(
        config, "OPENCLAW_WORKSPACE", ""
    )
    if raw:
        p = Path(raw).expanduser()
        if p.is_dir():
            return p
    # 2. Bekannte Home-Pfade
    for cand in (
        Path.home() / ".openclaw" / "workspace",
        Path.home() / ".glyph" / "memory",
    ):
        if cand.is_dir():
            return cand
    return Path.home() / ".openclaw" / "workspace"


def _read_file(p: Path, max_chars: int = 6000) -> str:
    try:
        if not p.is_file():
            return ""
        raw = p.read_text(encoding="utf-8", errors="replace")
        return raw[:max_chars]
    except OSError:
        return ""


def read_memory(scope: str = "recent", max_chars: int = 8000) -> dict:
    """Liest Memory-Ausschnitte. scope: recent | all | MEMORY.md | <date>."""
    root = _memory_root()
    mem_file = root / "MEMORY.md"
    mem_dir = root / "memory"
    scope = str(scope or "recent").strip().lower()

    if scope == "memory.md" or scope == "mem":
        content = _read_file(mem_file, max_chars)
        return {
            "ok": True,
            "scope": "MEMORY.md",
            "path": str(mem_file),
            "content": content,
        }

    if scope in ("all", "alles"):
        parts = []
        m = _read_file(mem_file, max_chars)
        if m:
            parts.append(f"# MEMORY.md\n{m}")
        if mem_dir.is_dir():
            for f in sorted(mem_dir.glob("*.md"))[-14:]:
                c = _read_file(f, 4000)
                if c:
                    parts.append(f"# {f.name}\n{c}")
        return {"ok": True, "scope": "all", "content": "\n\n".join(parts)[:max_chars * 3]}

    # recent: MEMORY.md + letzte 3 Tageslogs
    parts = []
    m = _read_file(mem_file, max_chars)
    if m:
        parts.append(f"# MEMORY.md\n{m}")
    if mem_dir.is_dir():
        today = date.today()
        for i in range(3):
            d = today - timedelta(days=i)
            f = mem_dir / f"{d.isoformat()}.md"
            c = _read_file(f, 5000)
            if c:
                parts.append(f"# {f.name}\n{c}")
    content = "\n\n".join(parts)[:max_chars]
    log.log("code_read_memory", scope=scope, chars=len(content))
    return {"ok": True, "scope": scope, "content": content}
