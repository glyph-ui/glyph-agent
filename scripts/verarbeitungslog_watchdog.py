#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Watchdog für das HSEQ-Verarbeitungslog.

Das Verarbeitungslog `00 Arbeitsfluss/Fertig/Verarbeitungslog.md` ist die
Dedup-Basis des Jobs `td-eingang`. Ohne es bricht die Kette ab (28.09. bis
02.10.2026: über 80 Fehlläufe, weil die Datei verloren ging). Der Watchdog:

1. prüft bei jedem Lauf, ob das Log existiert und plausibel ist,
2. zieht bei jedem Lauf einen Rollier-Sicherungsstand (Snapshot) mit,
3. stellt das Log bei Verlust sofort aus dem jüngsten Snapshot wieder her
   (Fallback: ältere Revisions-Backups in `vault/backups/`),
4. meldet jeden Vorfall aktiv — in Glyphs Job-Feed
   (`logs/recurring-events.jsonl`) und in der HSEQ-Status-Notiz
   (`00 Arbeitsfluss/_hseq-status.md`, sichtbar in „00 ▶ HSEQ verarbeiten“).

Aufruf: über `scripts/jobs-catchup.sh` (alle 15 Minuten) oder manuell.
Bewusst nur Standardbibliothek und ohne glyph-agent-Importe — funktioniert
auch, wenn der Server nicht läuft.

Zum Testen sind alle Pfade per Umgebungsvariable überschreibbar:
HSEQ_VAULT, HSEQ_LOG_REL, HSEQ_SNAPSHOT_DIR, HSEQ_REVISION_GLOB, HSEQ_EVENTS,
HSEQ_STATUS_REL, HSEQ_WATCHDOG_REPORT (0/1), HSEQ_KEEP.
"""
from __future__ import annotations

import glob
import json
import os
import sys
from datetime import datetime

VAULT = os.environ.get("HSEQ_VAULT", "/Users/lxndrhbrt/ObsidianVaults/HSEQ Sync")
LOG_REL = os.environ.get("HSEQ_LOG_REL", "00 Arbeitsfluss/Fertig/Verarbeitungslog.md")
SNAPSHOT_DIR = os.environ.get(
    "HSEQ_SNAPSHOT_DIR", "/Users/lxndrhbrt/glyph-agent/vault/backups/log-snapshots"
)
REVISION_GLOB = os.environ.get(
    "HSEQ_REVISION_GLOB",
    "/Users/lxndrhbrt/glyph-agent/vault/backups/HSEQ Sync__00 Arbeitsfluss__Fertig__Verarbeitungslog.R*.md",
)
EVENTS = os.environ.get("HSEQ_EVENTS", "/Users/lxndrhbrt/glyph-agent/logs/recurring-events.jsonl")
STATUS_REL = os.environ.get("HSEQ_STATUS_REL", "00 Arbeitsfluss/_hseq-status.md")
REPORT = os.environ.get("HSEQ_WATCHDOG_REPORT", "1").strip().lower() not in ("0", "false", "no")
try:
    KEEP = max(1, int(os.environ.get("HSEQ_KEEP", "5") or 5))
except ValueError:
    KEEP = 5

JOB_ID = "td-verarbeitungslog"
TITLE = "Verarbeitungslog-Watchdog"
SCRIPT_NAME = "verarbeitungslog_watchdog.py"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _log_path() -> str:
    return os.path.join(VAULT, LOG_REL)


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def _plausible(content: str) -> bool:
    """Mindestmaß an Log-Struktur: Kopfzeile + Tabellenkopf."""
    return "# Verarbeitungslog" in content and "| Rohnotiz |" in content


def _eintraege(content: str) -> int:
    return sum(1 for ln in content.splitlines() if ln.startswith("| [["))


def _snapshot_files() -> list:
    pat = os.path.join(SNAPSHOT_DIR, "Verarbeitungslog.*.md")
    return sorted(glob.glob(pat), key=lambda p: os.path.getmtime(p), reverse=True)


def _snapshot_neu(content: str) -> str:
    """Snapshot ablegen (nur bei Inhalt geändert) und auf KEEP begrenzen."""
    vorhanden = _snapshot_files()
    if vorhanden:
        try:
            if _read(vorhanden[0]) == content:
                return ""
        except OSError:
            pass
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    ziel = os.path.join(
        SNAPSHOT_DIR, "Verarbeitungslog.%s.md" % datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    _write(ziel, content)
    for alt in _snapshot_files()[KEEP:]:
        try:
            os.remove(alt)
        except OSError:
            pass
    return os.path.basename(ziel)


def _quellen() -> list:
    """Wiederherstellungs-Quellen, jüngste zuerst: Snapshots, dann Revisions-Backups."""
    alt = [p for p in glob.glob(REVISION_GLOB) if os.path.isfile(p)]
    return sorted(_snapshot_files() + alt, key=lambda p: os.path.getmtime(p), reverse=True)


def _status_schreiben(ok: bool, detail: str) -> None:
    """Status-Notiz im Format von glyph-ui `formatHseqStatus` (Mac → Sync → Knopf)."""
    mark = "OK" if ok else "FEHLER"
    zeile = "STATUS %s %s %s — %s" % (mark, JOB_ID, _now_iso(), " ".join(str(detail).split())[:240])
    inhalt = "# HSEQ Status\n\n%s\n\n" % zeile
    _write(os.path.join(VAULT, STATUS_REL), inhalt)


def _event_schreiben(preview: str) -> None:
    """Vorfall in Glyphs Job-Feed (gleicher Stream wie wiederkehrende Läufe)."""
    row = {
        "ts": _now_iso(),
        "type": "run",
        "id": JOB_ID,
        "title": TITLE,
        "ok": False,
        "preview": str(preview)[:280],
        "force": True,
        "script": SCRIPT_NAME,
    }
    try:
        os.makedirs(os.path.dirname(EVENTS), exist_ok=True)
        with open(EVENTS, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        print("warn: Ereignis nicht protokolliert: %s" % e)


def _melden(preview: str) -> None:
    if not REPORT:
        print("hinweis: Meldung unterdrückt (HSEQ_WATCHDOG_REPORT=0)")
        return
    _event_schreiben(preview)
    try:
        _status_schreiben(False, preview)
    except OSError as e:
        print("warn: Status-Notiz nicht geschrieben: %s" % e)


def main() -> int:
    ziel = _log_path()

    if os.path.isfile(ziel):
        try:
            inhalt = _read(ziel)
        except OSError as e:
            print("FEHLER: Verarbeitungslog nicht lesbar: %s" % e)
            return 1
        if _plausible(inhalt):
            snap = _snapshot_neu(inhalt)
            print(
                "ok: Verarbeitungslog vorhanden — %d Einträge, %d Zeichen%s"
                % (_eintraege(inhalt), len(inhalt), (" · neuer Snapshot %s" % snap) if snap else "")
            )
            return 0
        grund = "vorhanden, aber nicht mehr plausibel (Kopf/Tabelle fehlt)"
    else:
        grund = "fehlt"

    # Vorfall: Log weg oder unbrauchbar → aus Sicherung zurückholen.
    quellen = _quellen()
    if quellen:
        quelle = quellen[0]
        try:
            inhalt = _read(quelle)
        except OSError as e:
            print("FEHLER: Sicherung nicht lesbar (%s): %s" % (quelle, e))
            return 1
        if not _plausible(inhalt):
            print("FEHLER: jüngste Sicherung ist selbst unbrauchbar: %s" % quelle)
            return 1
        _write(ziel, inhalt)
        preview = (
            "**Verarbeitungslog %s** — wiederhergestellt aus `%s` (%d Einträge, %d Zeichen). "
            "Bitte prüfen, warum die Datei verloren ging."
            % (grund, os.path.basename(quelle), _eintraege(inhalt), len(inhalt))
        )
        print(preview)
        _melden(preview)
        return 0

    preview = (
        "**Verarbeitungslog %s** — KEINE Sicherung gefunden (weder %s noch Revisions-Backups). "
        "Manuell wiederherstellen — der Lauf td-eingang bleibt bis dahin stehen."
        % (grund, SNAPSHOT_DIR)
    )
    print(preview)
    _melden(preview)
    return 1


if __name__ == "__main__":
    sys.exit(main())
