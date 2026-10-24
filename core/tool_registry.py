# -*- coding: utf-8 -*-
"""
Tool-Registry — deklariert die dem Modell bekannten Werkzeuge + deren
Erkennung/Ausführung. Die eigentliche Logik bleibt in vault_tools/web.py
(unverändert); hier nur die dünne, kontrollierte Zuordnung:

    ToolName (JSON vom Modell)  ->  Python-Aufruf  ->  Ergebnis

Sicherheitsprinzipien:
  - Whitelist: nur diese Tools sind ausführbar (unbekannte -> abgelehnt).
  - Pfad-Sicherheit kommt aus vault_tools (kein ../-Escape).
  - Schreibende Tools sind MARKIERT (write=True) und brauchen im Chat-Flow
    eine Bestätigung, bevor sie ausgeführt werden.
"""
import json
import re

from . import log, vault_tools, web, retrieval

# DeepSeek V4 DSML — kanonisch U+FF5C, oft als ASCII || oder | durchgereicht.
_DSML_INNER = "\uff5cDSML\uff5c"
_DSML_PIPE = r"(?:\|{1,2}|\uff5c)"
_DSML_MARK = rf"{_DSML_PIPE}(?:\s*{_DSML_PIPE})?\s*DSML\s*{_DSML_PIPE}(?:\s*{_DSML_PIPE})?"
_DSML_TAG_RE = re.compile(
    rf"<\s*(/?)\s*{_DSML_MARK}\s*([A-Za-z_][\w]*)([^>]*)>",
    re.IGNORECASE,
)

# --- Tool-Schema (wird auch dem Modell im System-Prompt beschrieben) ---

# Vault/Recherche-Tools (MODE=agent) — kein Shell, nur Vault-Roots.
TOOLS = [
    {
        "name": "VaultFind",
        "description": (
            "EIN Finde-Werkzeug für den Obsidian-Vault (B+ Hybrid): "
            "0.7 lokale Embeddings (bge-m3) + 0.3 Keyword-Volltext. "
            "NUR Vault-Daten, KEINE Web-Recherche. Bevorzugt dieses Tool für alle Vault-Fragen."
        ),
        "args": {"query": "str", "top_k": "int (optional)", "min_score": "float (optional)"},
        "write": False,
    },
    {
        # Alias → VaultFind (Abwärtskompatibilität)
        "name": "VaultRecall",
        "description": "Alias für VaultFind (Hybrid Embedding+Keyword). Bevorzuge VaultFind.",
        "args": {"query": "str", "top_k": "int (optional)", "min_score": "float (optional)"},
        "write": False,
    },
    {
        # Alias → VaultFind
        "name": "VaultSearch",
        "description": "Alias für VaultFind (Hybrid). Bevorzuge VaultFind.",
        "args": {"query": "str", "limit": "int (optional, Default 10)"},
        "write": False,
    },
    {
        "name": "RecallAsOf",
        "description": (
            "Zeitpunkt-Recall: semantische Vault-Suche im Index-Stand eines Datums "
            "('was wussten wir am 2025-11-01?'). Rein lokal, read-only."
        ),
        "args": {
            "query": "str",
            "as_of": "str, Datum (YYYY-MM-DD oder ISO-8601)",
            "top_k": "int (optional, Default 8)",
        },
        "write": False,
    },
    {
        "name": "RecallChangedSince",
        "description": (
            "Differenz-Recall: was hat sich seit einem Datum geaendert "
            "(neu / geaendert / entfernt), pro Pfad. Rein lokal, read-only."
        ),
        "args": {
            "since": "str, Datum (YYYY-MM-DD oder ISO-8601)",
            "limit": "int (optional, Default 50)",
        },
        "write": False,
    },
    {
        # OpenClaw-Wiki-Alias → VaultFind
        "name": "WikiSearch",
        "description": "Alias für VaultFind (OpenClaw-Wiki-Kompatibilität). Bevorzuge VaultFind.",
        "args": {"query": "str", "top_k": "int (optional)", "min_score": "float (optional)"},
        "write": False,
    },
    {
        "name": "ListVaultDir",
        "description": (
            "Listet Dateien/Ordner im Obsidian-Vault (nur Lesen). "
            "Für Inventar-Fragen ('was liegt im Eingang?', 'welche Dateien in Fertig?'). "
            "Pfad relativ zu einem Vault, optional mit Vault-Präfix "
            "(z.B. '00 Arbeitsfluss/Eingang' oder 'HSEQ Sync/00 Arbeitsfluss/Eingang'). "
            "Index-Pfade mit führendem Slash ('/HSEQ Sync/Themen') gelten gleich — "
            "das ist kein Dateisystem-Root. Leer/'.' = Top-Level aller Vaults. "
            "Kein Ersatz für VaultFind (Inhaltssuche)."
        ),
        "args": {
            "path": "str (optional, Default '.')",
            "limit": "int (optional, Default 200)",
            "extensions": "list[str] (optional, z.B. ['.md','.pdf'])",
        },
        "write": False,
    },
    {
        "name": "FileHash",
        "description": (
            "SHA-256-Prüfsumme einer Vault-Datei (nur Lesen). Ersatz für "
            "`shasum -a 256`, das im Agent-Modus nicht verfügbar ist. "
            "Für Abgleiche, die Änderungsdaten misstrauen (Sync verfälscht sie) "
            "— z.B. Rohnotiz gegen Verarbeitungslog prüfen."
        ),
        "args": {"path": "str, z.B. 00 Arbeitsfluss/Eingang/notiz.md"},
        "write": False,
    },
    {
        "name": "ReadNote",
        "description": "Liest eine Notiz aus dem Vault (relativer Pfad).",
        "args": {"path": "str, z.B. Themen/PSA.md"},
        "write": False,
    },
    {
        # OpenClaw-Wiki-Alias → ReadNote
        "name": "WikiGet",
        "description": "Alias für ReadNote (OpenClaw-Wiki-Kompatibilität).",
        "args": {"path": "str, z.B. Themen/PSA.md"},
        "write": False,
    },
    {
        "name": "Summarize",
        "description": "Lässt das Modell eine Notiz zusammenfassen/analysieren.",
        "args": {"path": "str, z.B. Themen/PSA.md", "hint": "str (optional)"},
        "write": False,
    },
    {
        "name": "CreateNote",
        "description": (
            "Legt eine NEUE Notiz an (überschreibt nie Bestehendes). "
            "Chat: Themen/ und Wiki concepts|entities|syntheses|sources. Nie leer."
        ),
        "args": {"path": "str", "content": "str"},
        "write": True,
    },
    {
        "name": "ProposeEdit",
        "description": "Erzeugt einen Änderungs-VORSCHLAG (Diff) für eine Notiz. SCHREIBT NOCH NICHTS.",
        "args": {"path": "str", "instruction": "str, was geändert werden soll"},
        "write": False,
    },
    {
        "name": "ApplyEdit",
        "description": (
            "Ergänzt/ändert eine Notiz (Backup + Revision). "
            "Chat ohne Popup: Themen/ und Wiki concepts|entities|syntheses. "
            "Nie löschen, nie leeren, nie Eingang/Sources umschreiben."
        ),
        "args": {"path": "str", "new_content": "str, kompletter neuer Inhalt"},
        "write": True,
    },
    {
        # OpenClaw-Wiki-Alias → ApplyEdit (write + confirm)
        "name": "WikiApply",
        "description": "Alias für ApplyEdit. Gleich: Wachstum, kein Löschen.",
        "args": {"path": "str", "new_content": "str, kompletter neuer Inhalt"},
        "write": True,
    },
    {
        "name": "WikiStatus",
        "description": (
            "Read-only Wiki-Status aus OpenClaw agent-digest.json "
            "(pageCounts, claimHealth, …). Kein Schreiben."
        ),
        "args": {},
        "write": False,
    },
    {
        "name": "WebSearch",
        "description": (
            "Web-Recherche: Exa und TinyFish parallel (source=both). NUR anonymisierte Suchbegriffe. "
            "Für konkrete URLs: ExtractUrl/FetchUrl/BrowseUrl."
        ),
        "args": {
            "query": "str, anonymisierter Suchbegriff",
            "count": "int (optional)",
            "source": "str (optional: 'both' | 'exa' | 'tinyfish')",
        },
        "write": False,
    },
    {
        "name": "DeepResearch",
        "description": (
            "Autonome Multi-Agenten Swarm-Recherche. Dekomponiert komplexe Themen, "
            "sucht parallel über Quellen, extrahiert Claims und erstellt einen B+-Belegbericht."
        ),
        "args": {"topic": "str, Thema oder Fragestellung für Deep Research"},
        "write": False,
    },
    {
        "name": "ExtractUrl",
        "description": "FEINE Recherche: URL besuchen + strukturierte Daten (TinyFish). NUR öffentliche URLs.",
        "args": {"url": "str", "goal": "str, was extrahiert werden soll (mit JSON-Schema)"},
        "write": False,
    },
    {
        "name": "FetchUrl",
        "description": "FEINE Recherche: öffentlichen URL-Inhalt als Markdown/Text (TinyFish).",
        "args": {"url": "str"},
        "write": False,
    },
    {
        "name": "BrowseUrl",
        "description": (
            "URL besuchen und kurze Zusammenfassung holen (TinyFish Extract, goal=Summary). "
            "Für Überblick ohne eigenes JSON-Schema."
        ),
        "args": {"url": "str", "goal": "str (optional, Default: Zusammenfassung der Seite)"},
        "write": False,
    },
    {
        "name": "ReadPdf",
        "description": (
            "Liest Text aus einer PDF-Datei im Vault. Lesen erlaubt. "
            "Ingest = Folge-Schreiben via CreateNote/ApplyEdit laut "
            "vault_write_policy (Allowlist), nicht dieses Tool. "
            "Nie PDF-Datei schreiben. Nur Vault-Pfade; Zeichen-Cap. "
            "Nach Vault-Listing oder Suche sofort die 1–2 PDFs lesen, "
            "deren Dateiname zur Frage passt. Niemals den Nutzer fragen, "
            "welche Datei zu öffnen."
        ),
        "args": {"path": "str, vault-relativer .pdf-Pfad", "max_chars": "int (optional)"},
        "write": False,
    },
    {
        "name": "MailList",
        "description": (
            "Listet E-Mail-Envelopes via himalaya CLI (graceful wenn fehlt). Read-only."
        ),
        "args": {
            "folder": "str (optional, Default INBOX)",
            "query": "str (optional, himalaya Filter)",
            "limit": "int (optional, Default 20)",
            "account": "str (optional)",
        },
        "write": False,
    },
    {
        "name": "MailRead",
        "description": "Liest eine E-Mail via himalaya (message id). Read-only.",
        "args": {
            "id": "str|int, Envelope-ID",
            "folder": "str (optional, Default INBOX)",
            "account": "str (optional)",
            "preview": "bool (optional, kein seen-Flag)",
        },
        "write": False,
    },
    {
        "name": "MessageSend",
        "description": (
            "Sendet eine Nachricht über openclaw message send (Gateway). "
            "Braucht Nutzer-Bestätigung. Graceful wenn openclaw/Gateway fehlt."
        ),
        "args": {
            "target": "str, Empfänger/Kanal-Ziel",
            "message": "str, Nachrichtentext",
            "channel": "str (optional, z.B. telegram|discord|…)",
            "account": "str (optional)",
        },
        "write": True,
    },
    {
        "name": "ObsidianOpen",
        "description": (
            "Optional: öffnet eine Vault-Notiz in der Obsidian-App (kepano-CLI). "
            "Nur Pfade innerhalb erlaubter Vaults. Schreibt nichts."
        ),
        "args": {"path": "str, relativer oder vault-relativer .md-Pfad"},
        "write": False,
    },
    {
        "name": "ChatDatei",
        "description": (
            "Erzeugt auf Wunsch eine PDF- oder Word-Datei nur für den Chat. "
            "format: pdf oder docx. title: Überschrift des Links. "
            "text: vollständiger Inhalt. Schreibt nicht in den Vault und nicht auf die Platte. "
            "Betriebsanweisung: das Leerformular nur lesen und die Füllung als Chat-Datei ausgeben. "
            "Abschnitte "
            "1 Anwendungsbereich, 2 Gefahren für Mensch und Umwelt, "
            "3 Schutzmaßnahmen und Verhaltensregeln, "
            "4 Verhalten bei Störungen und im Gefahrfall, "
            "5 Verhalten bei Unfällen – Erste Hilfe, "
            "6 Instandhaltung, Entsorgung. "
            "Firma, Nummer, Datum und Unterschrift leer. "
            "Inhalt als kurze Stichpunkte, eine Zeile je Punkt. "
            "Zwischenüberschriften wie Auswahl, Aufstellen, Benutzen sind erlaubt. "
            "Kein Fließtext, kein Aufsatz. Keine Vorstellung. "
            "Nur aufrufen, wenn der Nutzer eine Betriebsanweisung erstellen will. "
            "Sonst nicht aufrufen, die Suche bleibt. "
            "Den Inhalt nicht noch einmal als Chat-Text wiederholen."
        ),
        "args": {
            "title": "str, Überschrift",
            "text": "str, vollständiger Dateiinhalt",
            "format": "pdf oder docx",
        },
        "write": False,
    },
]

# CODE-Tools (^_tick / MODE=code|tick) — Workspace-Roots, kein VaultFind.
CODE_TOOLS = [
    {
        "name": "ListDir",
        "description": (
            "Listet Dateien/Ordner relativ zu einem Workspace-Root. "
            "Optional recursive (max depth 2) mit Entry-Cap."
        ),
        "args": {
            "path": "str (optional, Default '.')",
            "recursive": "bool (optional)",
            "max_depth": "int (optional, max 2)",
        },
        "write": False,
    },
    {
        "name": "ReadFile",
        "description": (
            "Liest eine Datei innerhalb der CODE_WORKSPACE_ROOTS (Text, UTF-8). "
            "Optional offset/limit in Zeilen (1-basiert)."
        ),
        "args": {
            "path": "str",
            "offset": "int (optional, 1-basierte Startzeile)",
            "limit": "int (optional, max. Zeilen)",
        },
        "write": False,
    },
    {
        "name": "Grep",
        "description": (
            "Sucht Regex/Text in Dateien unter path (nur Workspace-Roots). "
            "rg wenn vorhanden, sonst Python-Walk. Cap auf Treffer."
        ),
        "args": {
            "pattern": "str",
            "path": "str (optional, Default '.')",
            "max_hits": "int (optional, Default 50)",
            "case_insensitive": "bool (optional)",
        },
        "write": False,
    },
    {
        "name": "WriteFile",
        "description": (
            "Schreibt/überschreibt eine Datei (kompletter Inhalt) mit Backup. "
            "Unter r+w: stagen, Apply nach Freigabe (Einmal/Auftrag/Task). Kein Löschen."
        ),
        "args": {"path": "str", "content": "str, kompletter neuer Dateiinhalt"},
        "write": True,
    },
    {
        "name": "SearchReplace",
        "description": (
            "Ersetzt old→new exakt einmal in einer Datei (1 Treffer Pflicht). "
            "Backup wie WriteFile. Unter r+w: stagen, Apply nach Freigabe."
        ),
        "args": {"path": "str", "old": "str, exakter bisheriger Text", "new": "str, Ersatz"},
        "write": True,
    },
    {
        "name": "RunCommand",
        "description": (
            "Shell im Workspace (r+w). Lesen (git status/diff, ls) ohne Popup. "
            "Test/Build/Commit/Install brauchen Freigabe. Elevated "
            "(git push/pull/fetch, Compound, npm run service:*) immer Einmal. "
            "Hart verboten: rm/sudo/…."
        ),
        "args": {
            "command": "str",
            "cwd": "str (optional, relativ zu Root)",
            "timeout": "int Sekunden optional, max 120",
        },
        "write": True,  # Policy entscheidet Popup vs auto
    },
    {
        "name": "VaultFind",
        "description": "Vault-Suche (Hybrid) über die freigeschalteten Vaults (Graph). Read-only.",
        "args": {"query": "str", "top_k": "int optional, Default 8"},
        "write": False,
    },
    {
        "name": "ReadNote",
        "description": "Liest eine Vault-Notiz (relativer Pfad). Nur wenn Vault im Graph freigeschaltet. Read-only.",
        "args": {"path": "str, relativer Notizpfad im Vault"},
        "write": False,
    },
    {
        "name": "WikiGet",
        "description": "Liest eine memory-wiki-Seite (concepts/entities/syntheses). Read-only.",
        "args": {"lookup": "str, Seitenname oder Pfad"},
        "write": False,
    },
    {
        "name": "WebSearch",
        "description": "Web-Suche (Exa + TinyFish). Read-only.",
        "args": {"query": "str", "count": "int optional, Default 5"},
        "write": False,
    },
    {
        "name": "ExtractUrl",
        "description": "Holt eine URL (TinyFish) und extrahiert Text. Read-only.",
        "args": {"url": "str", "goal": "str optional, Extraktionsziel"},
        "write": False,
    },
    {
        "name": "MailList",
        "description": "Listet E-Mails (himalaya, INBOX). Read-only.",
        "args": {"folder": "str optional, Default INBOX", "query": "str optional", "limit": "int optional, max 100"},
        "write": False,
    },
    {
        "name": "MailRead",
        "description": "Liest eine E-Mail (himalaya). Read-only.",
        "args": {"id": "str, Message-ID", "folder": "str optional"},
        "write": False,
    },
    {
        "name": "ReadMemory",
        "description": "Liest OpenClaw-Memory (MEMORY.md + Tageslogs) für Kontext über Sessions. Read-only.",
        "args": {"scope": "str optional: recent (Default) | all | MEMORY.md"},
        "write": False,
    },
    {
        "name": "VaultWrite",
        "description": "Schreibt eine Vault-Notiz (neu oder Änderung mit Backup). Nur wenn Vault im Graph auf rw für ^_Code. Braucht Freigabe (Einmal/Auftrag/Task).",
        "args": {"path": "str, relativer Notizpfad im Vault", "content": "str, kompletter neuer Inhalt"},
        "write": True,
    },
    {
        "name": "TaskFinish",
        "description": (
            "Schließt eine Glyph-Aufgabe: schreibt das Artefakt nach "
            "~/.glyph/tasks/<id>.md und setzt status=done. "
            "id steht in der Meldung (Zeile Id:). Chat-Text allein reicht nicht. "
            "Kein Workspace-Write, keine Freigabe."
        ),
        "args": {
            "id": "str, Aufgaben-Id",
            "content": "str, Markdown-Artefakt (Befund/Ergebnis)",
            "summary": "str optional, eine Zeile für den Plan",
        },
        "write": True,
    },
    # --- ^_tick: Job-Ordner + Plan & Aktivität ---
    {
        "name": "RoutineOverview",
        "description": (
            "Ein Blick auf die ganze Routine-Lage: Zähler (To-dos aktiv/pausiert/fällig, "
            "offene Glyph-Aufgaben, Job-Ordner je Box), Dateien in Daily/Inbox/Blocked/Done "
            "und die letzten Läufe. Für Fragen wie 'wie viele offene Aufgaben?'. Read-only."
        ),
        "args": {},
        "write": False,
    },
    {
        "name": "RoutineJobs",
        "description": (
            "Listet die Job-Dateien im Routine-Ordner (Daily/, Inbox/, Blocked/) "
            "mit Datum, Größe und Kopfzeile. Daily zuerst, dann Inbox. "
            "Quelle der täglichen Aufträge — read-only."
        ),
        "args": {
            "box": "str optional: Daily | Inbox | Blocked (leer = alle + Done-Zähler)",
            "include_done": "bool optional, Done/ mitlisten",
        },
        "write": False,
    },
    {
        "name": "RoutineRead",
        "description": "Liest eine Job-Datei vollständig (Name aus RoutineJobs). Read-only.",
        "args": {
            "name": "str, Dateiname (Präfix genügt, wenn eindeutig)",
            "box": "str optional: Daily | Inbox | Blocked",
        },
        "write": False,
    },
    {
        "name": "RoutineJobWrite",
        "description": (
            "Legt eine NEUE Job-Datei in Daily/ oder Inbox/ an (Auftragsformat: "
            "JOB/Ziel/Quelle/Zielort/Schritte/Ausschluss/Erledigt-wenn). "
            "Nie überschreiben — existiert die Datei, bleibt sie unverändert. Braucht Freigabe."
        ),
        "args": {
            "box": "str: Daily | Inbox",
            "name": "str, Dateiname (ohne .md)",
            "body": "str, vollständiger Job-Text",
        },
        "write": True,
    },
    {
        "name": "RoutineMove",
        "description": (
            "Verschiebt einen Job (typisch Inbox → Done nach Erledigung, Inbox → Blocked). "
            "Done bekommt das Datum im Dateinamen, Blocked genau eine Frage an den Dateianfang. "
            "Kopie zuerst; Kollision lässt alles unverändert."
        ),
        "args": {
            "name": "str, Dateiname",
            "to": "str: Done | Blocked | Daily | Inbox",
            "question": "str, nur für Blocked: genau eine konkrete Frage",
        },
        "write": False,
    },
    {
        "name": "RoutineLog",
        "description": (
            "Hängt Zeilen an das Laufprotokoll Log/YYYY-MM-DD_HHMM.md (append-only): "
            "pro Job Status (erledigt/teilweise/blockiert), betroffene Dateien, Auffälligkeiten."
        ),
        "args": {"lines": "list[str] oder str"},
        "write": False,
    },
    {
        "name": "RoutinePlan",
        "description": (
            "Zeigt Plan & Aktivität: wiederkehrende To-dos (id, Zeitplan, pausiert, letzter Lauf) "
            "und offene Glyph-Aufgaben. Read-only."
        ),
        "args": {"include_done_tasks": "bool optional"},
        "write": False,
    },
    {
        "name": "RoutineSet",
        "description": (
            "Legt ein wiederkehrendes To-do an (id leer) oder ändert es (id gesetzt): "
            "Zeitplan, Prompt, Fertig-Kriterium, allow_write, pausiert. "
            "Erscheint in Plan & Aktivität. Kein Löschen. Braucht Freigabe."
        ),
        "args": {
            "id": "str optional, leere id = neu anlegen",
            "title": "str",
            "prompt": "str, was ohne Rückfragen zu tun ist (absolute Pfade)",
            "pass": "str, eine prüfbare Fertig-Zeile (Pflicht bei neu)",
            "schedule_kind": "str: daily | weekly | monthly",
            "schedule_time": "str HH:MM",
            "weekday": "int optional 0=Mo … 6=So (weekly)",
            "paused": "bool optional",
            "allow_write": "bool optional",
        },
        "write": True,
    },
    {
        "name": "RoutineRun",
        "description": (
            "Löst ein wiederkehrendes To-do jetzt aus (wie Kalender → Ausführen). "
            "Läuft synchron im Agent-Loop. Braucht Freigabe."
        ),
        "args": {"id": "str, To-do-Id", "force": "bool optional, Default true"},
        "write": True,
    },
]

TOOL_MAP = {t["name"]: t for t in TOOLS}
CODE_TOOL_MAP = {t["name"]: t for t in CODE_TOOLS}


def tools_for_mode(mode="agent"):
    """Tool-Liste je Betriebsart."""
    m = (mode or "agent").lower()
    if m in ("code", "tick"):
        return CODE_TOOLS
    return TOOLS


def tool_map(mode="agent"):
    m = (mode or "agent").lower()
    if m in ("code", "tick"):
        return CODE_TOOL_MAP
    return TOOL_MAP


def tool_schema_prompt(mode="agent", exclude=None):
    """Erzeugt die Tool-Beschreibung für den System-Prompt des Modells."""
    skip = {str(x) for x in (exclude or []) if x}
    lines = ['Verfügbare Werkzeuge (antworte mit JSON {"tool": Name, "args": {...}}):']
    for t in tools_for_mode(mode):
        if t["name"] in skip:
            continue
        args = ", ".join(f"{k}:{v}" for k, v in t["args"].items())
        lines.append(f"- {t['name']}({args}) — {t['description']}")
    return "\n".join(lines)


# --- Erkennung & Ausführung ---

def looks_like_dsml(text):
    """True, wenn die Antwort DeepSeek-DSML-Tool-Markup enthält."""
    if not text:
        return False
    return _DSML_TAG_RE.search(text) is not None


def prose_before_dsml(text):
    """Sichtbare Prosa vor dem ersten DSML-Tag — Markup selbst nie zurück."""
    if not text:
        return ""
    m = _DSML_TAG_RE.search(text)
    if not m:
        return (text or "").strip()
    return text[: m.start()].strip()


def _normalize_dsml(text):
    """Alle Token-Varianten auf kanonisch <｜DSML｜tag …>."""

    def repl(m):
        slash = m.group(1) or ""
        name = m.group(2)
        rest = m.group(3) or ""
        return f"<{slash}{_DSML_INNER}{name}{rest}>"

    return _DSML_TAG_RE.sub(repl, text)


def _coerce_dsml_value(raw, string_flag):
    raw = (raw or "").strip()
    flag = (string_flag or "").lower()
    if flag == "true":
        return raw
    if flag == "false":
        low = raw.lower()
        if low == "true":
            return True
        if low == "false":
            return False
        if re.fullmatch(r"-?\d+", raw):
            return int(raw)
        if re.fullmatch(r"-?\d+\.\d+", raw):
            return float(raw)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return raw


def _parse_dsml_tool_call(text):
    """Erster DSML-invoke → (name, args) oder None.

    Close-Tags: XML `</｜DSML｜parameter>` *oder* DeepSeek-nativ
    `<｜DSML｜parameter>` ohne Attribute (Screenshot-Leak).
    """
    if not looks_like_dsml(text):
        return None
    s = _normalize_dsml(text)
    token = re.escape(_DSML_INNER)
    inv = re.search(
        rf"<{token}invoke\s+name=\"([^\"]+)\"\s*>(.*?)"
        rf"(?:</{token}invoke>|<{token}invoke>|"
        rf"</{token}(?:tool_calls|function_calls)>|$)",
        s,
        re.DOTALL | re.IGNORECASE,
    )
    if not inv:
        return None
    name = (inv.group(1) or "").strip()
    if not name:
        return None
    body = inv.group(2) or ""
    args = {}
    for pm in re.finditer(
        rf"<{token}parameter\s+name=\"([^\"]+)\"(?:\s+string=\"(true|false)\")?\s*>"
        rf"(.*?)"
        rf"(?:</{token}parameter>|<{token}parameter>"
        rf"|(?=<{token}parameter\s)|(?=</?{token}invoke))",
        body,
        re.DOTALL | re.IGNORECASE,
    ):
        key = (pm.group(1) or "").strip()
        if not key:
            continue
        args[key] = _coerce_dsml_value(pm.group(3), pm.group(2))
    return (name, args)


def _known_tool_names():
    known = set()
    try:
        from . import tool_registry as _self

        known = set(_self.tool_map("agent")) | set(_self.tool_map("code"))
    except Exception:
        pass
    return known


def _parse_xml_tool_call(text):
    """Erster <tool_call name="X"><parameter name="k">v</parameter>…</tool_call>."""
    if not text or "<tool_call" not in text.lower():
        return None
    inv = re.search(
        r"<tool_call\s+name=\"([^\"]+)\"\s*>(.*?)(?:</tool_call>|$)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if not inv:
        return None
    name = (inv.group(1) or "").strip()
    known = _known_tool_names()
    if not name or (known and name not in known):
        return None
    body = inv.group(2) or ""
    args = {}
    for pm in re.finditer(
        r"<parameter\s+name=\"([^\"]+)\"\s*>(.*?)</parameter>",
        body,
        re.DOTALL | re.IGNORECASE,
    ):
        key = (pm.group(1) or "").strip()
        if not key:
            continue
        args[key] = _coerce_dsml_value(pm.group(2), None)
    return (name, args)


def _parse_plain_invoke_tool_call(text):
    """Erster <invoke name="X">…</invoke>-Block OHNE DSML-Mark (ASCII-Leak).

    DeepSeek schreibt gelegentlich:
      <tool_calls>\n<invoke name="VaultFind">\n<parameter name="query">…</parameter>\n</invoke>\n</tool_calls>
    Der DSML-Parser verlangt das U+FF5C-Mark — diese ASCII-Form erkennt er nicht.
    """
    if not text or "<invoke" not in text:
        return None
    # Nur bekannte Tool-Namen akzeptieren (verhindert False Positives, wenn das
    # Modell über DSML/Invoke REDET statt es zu nutzen).
    known = _known_tool_names()
    inv = re.search(
        r"<invoke\s+name=\"([^\"]+)\"\s*>(.*?)(?:</invoke>|$)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if not inv:
        return None
    name = (inv.group(1) or "").strip()
    if not name or (known and name not in known):
        return None
    body = inv.group(2) or ""
    args = {}
    for pm in re.finditer(
        r"<parameter\s+name=\"([^\"]+)\"\s*>(.*?)</parameter>",
        body,
        re.DOTALL | re.IGNORECASE,
    ):
        key = (pm.group(1) or "").strip()
        if not key:
            continue
        args[key] = _coerce_dsml_value(pm.group(2), None)
    return (name, args)


def try_parse_tool_call(text):
    """
    Versucht, aus der Modell-Antwort einen Tool-Call zu extrahieren.
    Liefert (tool_name, args) oder None, wenn es keine Tool-Anfrage ist.
    Reihenfolge: DeepSeek-DSML (V4 leaked das als Text), dann JSON.
    Robust für EINEN oder MEHRERE verschachtelte JSON-Blöcke (gpt-5.6-luna sendet
    teils mehrere WebSearch-Blöcke). Gibt den ERSTEN gültigen Tool-Call zurück.
    Nutzt raw_decode, um verschachtelte Objekte korrekt zu parsen.
    """
    if not text or not str(text).strip():
        return None

    dsml = _parse_dsml_tool_call(text)
    if dsml is not None:
        return dsml

    # DeepSeek leaked manchmal auch <tool_calls>/<invoke> OHNE DSML-Mark (ASCII).
    # Dann: <tool_calls>…<invoke name="X"><parameter name="k">v</parameter></invoke>…
    plain = _parse_plain_invoke_tool_call(text)
    if plain is not None:
        return plain

    # Grok schreibt <tool_call name="ChatDatei"><parameter name="text">…</parameter>
    # als Antworttext. Das ist ein Aufruf, kein Chat.
    tagged = _parse_xml_tool_call(text)
    if tagged is not None:
        return tagged

    s = text.strip()
    # Markdown-Codeblock entfernen, falls die ganze Antwort einer ist
    cm = re.search(r"```(?:json)?\s*(.*?)\s*```", s, re.DOTALL)
    if cm:
        s = cm.group(1)

    decoder = json.JSONDecoder()
    idx = 0
    n = len(s)
    while idx < n:
        c = s[idx]
        if c != "{":
            idx += 1
            continue
        try:
            obj, end = decoder.raw_decode(s, idx)
        except json.JSONDecodeError:
            idx += 1
            continue
        if isinstance(obj, dict) and "tool" in obj:
            name = str(obj.get("tool") or "")
            args = obj.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            recovered = _recover_chatdatei(name, s)
            if recovered:
                return recovered
            return (name, args)
        fname = obj.get("name")
        if isinstance(fname, str) and ("arguments" in obj or "args" in obj):
            name = fname.strip()
            raw_args = obj.get("arguments") if "arguments" in obj else obj.get("args")
            args = raw_args if isinstance(raw_args, dict) else {}
            if isinstance(raw_args, str) and raw_args.strip():
                try:
                    parsed_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    parsed_args = None
                if isinstance(parsed_args, dict):
                    args = parsed_args
            recovered = _recover_chatdatei(name, s)
            if recovered:
                return recovered
            if name:
                return (name, args)
        idx = end
    return None


def _recover_chatdatei(name, full):
    """tool=\"ChatDatei(title:\\\"…\\\")\" ist der Aufruf, kein Werkzeugname."""
    low = (name or "").lower()
    if "chatdatei" not in low or name == "ChatDatei":
        return None
    from . import chat_file

    narr = chat_file.narrated_call(name) or chat_file.narrated_call(full or "")
    if not narr:
        return None
    text = str(narr.get("text") or narr.get("content") or "").strip()
    if not text:
        return None
    return ("ChatDatei", {
        "title": narr.get("title") or "",
        "text": text,
        "format": narr.get("format") or "docx",
    })


def _name_words(name):
    """Wortbestandteile eines Tool-Namens: klein, ohne Trenner, sortiert.

    `ListVaultDir` und `vault_list_dir` ergeben dieselbe Menge
    ['dir', 'list', 'vault'] — genau darum geht es: Modelle nennen Werkzeuge
    gern in snake_case oder mit anderer Wortstellung.
    """
    s = re.sub(r"[^A-Za-z0-9]+", " ", str(name or ""))
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    return sorted(w.lower() for w in s.split() if w)


def _resolve_tool_name(tmap, tool_name):
    """Findet ein registriertes Tool auch bei abweichender Schreibweise.

    Modelle rufen regelmäßig Namen auf, die es nicht gibt: `vault_list_dir`
    statt `ListVaultDir`, `websearch` statt `WebSearch`. Bisher brach die Runde
    dann mit „Unbekanntes Tool" ab und der Job endete als Fehler, obwohl das
    gemeinte Werkzeug existiert.

    Aufgelöst wird nur, wenn GENAU EIN Tool passt — bei Mehrdeutigkeit bleibt es
    beim Fehler, damit nicht geraten wird.
    """
    raw = str(tool_name or "").strip()
    if not raw or raw in tmap:
        return None, ""
    low = raw.lower()
    for name in tmap:
        if name.lower() == low:
            return name, "case"
    want = _name_words(raw)
    if want:
        hits = [n for n in tmap if _name_words(n) == want]
        if len(hits) == 1:
            return hits[0], "words"
    return None, ""


def execute(tool_name, args, confirm=None, mode="agent", allow_elevated=False):
    """
    Führt ein Tool kontrolliert aus.

    confirm: optionaler Callback confirm(tool_name, args) -> bool.
      Für write=True-Tools wird confirm genau dann aufgerufen; wenn er
      False liefert oder fehlt (und das Tool write=True ist), wird NICHT
      ausgeführt.

    mode: "agent" (Vault) | "code" (^_Code Workspace-Tools)
    allow_elevated: CODE — Elevated-Shell nach Glyph-Freigabe erlauben

    Rückgabe: dict {"ok": bool, "result": ..., "error": str|None}
    """
    mode = (mode or "agent").lower()
    tmap = tool_map(mode)
    tool = tmap.get(tool_name)
    if tool is None:
        resolved, how = _resolve_tool_name(tmap, tool_name)
        if resolved:
            log.log("tool_name_resolved", asked=str(tool_name), used=resolved, how=how)
            tool_name = resolved
            tool = tmap[resolved]
    if tool is None:
        return {"ok": False, "result": None,
                "error": f"Unbekanntes Tool '{tool_name}'. Erlaubt: {', '.join(tmap)}"}

    # Bestätigung für Schreib-Tools erzwingen
    if tool["write"]:
        if confirm is None:
            return {"ok": False, "result": None,
                    "error": f"Tool '{tool_name}' ist schreibend und benötigt Bestätigung."}
        if not confirm(tool_name, args):
            return {"ok": False, "result": None,
                    "error": "Schreib-Vorgang vom Nutzer abgebrochen."}

    try:
        if tool_name == "ChatDatei":
            from . import chat_file
            out = chat_file.run(args or {})
        elif mode in ("code", "tick"):
            out = _execute_code(tool_name, args or {}, allow_elevated=allow_elevated)
        else:
            out = _execute_agent(tool_name, args or {})
    except Exception as e:
        return {"ok": False, "result": None, "error": str(e)}
    if tool_name == "ReadPdf":
        return _propagate_inner_ok(out)
    return out


def _propagate_inner_ok(out):
    """Inneres {ok:false} nicht als Tool-Erfolg wrappen."""
    if not isinstance(out, dict) or out.get("ok") is not True:
        return out
    inner = out.get("result")
    if isinstance(inner, dict) and inner.get("ok") is False:
        return {
            "ok": False,
            "result": inner,
            "error": inner.get("error") or out.get("error") or "Tool fehlgeschlagen",
        }
    return out


def _execute_agent(tool_name, args):
    if tool_name in ("VaultFind", "VaultRecall", "VaultSearch", "WikiSearch"):
        q = args.get("query", "")
        top_k = int(args.get("top_k") or args.get("limit") or 4)
        min_score = float(args.get("min_score", 0.35))
        res = retrieval.vault_find(q, top_k=top_k, min_score=min_score)
        return {"ok": True, "result": res}
    if tool_name == "ListVaultDir":
        ext = args.get("extensions")
        if isinstance(ext, str):
            ext = [x.strip() for x in ext.split(",") if x.strip()]
        res = vault_tools.list_vault_dir(
            path=args.get("path") or ".",
            limit=int(args.get("limit") or 200),
            extensions=ext,
        )
        return {"ok": True, "result": res}
    if tool_name == "FileHash":
        res = vault_tools.file_hash(args.get("path") or "")
        # Fehlgeschlagene Prüfsumme ist ein Werkzeugfehler, kein stiller Treffer.
        return {"ok": res.get("status") == "success", "result": res,
                "error": res.get("error")}
    if tool_name in ("ReadNote", "WikiGet"):
        res = vault_tools.read_note(args.get("path"))
        return {"ok": True, "result": res}
    if tool_name == "Summarize":
        from . import agent as agent_mod
        res = agent_mod.summarize_note(args.get("path"), args.get("hint", ""))
        return {"ok": True, "result": res}
    if tool_name == "CreateNote":
        res = vault_tools.create_note(args.get("path"), args.get("content", ""))
        return {"ok": True, "result": res}
    if tool_name == "ProposeEdit":
        from . import agent as agent_mod
        prop = agent_mod.build_edit_proposal(args.get("path"), args.get("instruction", ""))
        return {"ok": True, "result": prop}
    if tool_name in ("ApplyEdit", "WikiApply"):
        res = vault_tools.apply_edit(args.get("path"), args.get("new_content", ""))
        return {"ok": True, "result": res}
    if tool_name == "WikiStatus":
        res = vault_tools.wiki_status()
        return {"ok": True, "result": res}
    if tool_name == "WebSearch":
        kwargs = {
            "query": args.get("query", ""),
            "count": int(args.get("count", 5)),
            "source": args.get("source", "both"),
        }
        if args.get("include_domains"):
            kwargs["include_domains"] = args.get("include_domains")
        res = web.web_search(**kwargs)
        return {"ok": True, "result": res}
    if tool_name in ("DeepResearch", "SwarmResearch"):
        from . import swarm
        res = swarm.run_swarm(args.get("topic", ""))
        return {"ok": True, "result": res}
    if tool_name == "ExtractUrl":
        res = web.extract_tinyfish(args.get("url", ""), args.get("goal", ""))
        return {"ok": True, "result": res}
    if tool_name == "FetchUrl":
        res = web.fetch_tinyfish(args.get("url", ""), "markdown")
        return {"ok": True, "result": res}
    if tool_name == "BrowseUrl":
        res = web.browse_url(args.get("url", ""), args.get("goal"))
        return {"ok": True, "result": res}
    if tool_name == "ReadPdf":
        from . import pdf_tools
        max_chars = args.get("max_chars")
        res = pdf_tools.read_pdf(
            args.get("path", ""),
            max_chars=int(max_chars) if max_chars is not None else None,
        )
        return {"ok": True, "result": res}
    if tool_name == "MailList":
        from . import comm_tools
        limit = args.get("limit")
        res = comm_tools.mail_list(
            folder=args.get("folder") or "INBOX",
            query=args.get("query") or "",
            limit=int(limit) if limit is not None else 20,
            account=args.get("account"),
        )
        return {"ok": True, "result": res}
    if tool_name == "MailRead":
        from . import comm_tools
        res = comm_tools.mail_read(
            msg_id=args.get("id") or args.get("msg_id"),
            folder=args.get("folder") or "INBOX",
            account=args.get("account"),
            preview=bool(args.get("preview", True)),
        )
        return {"ok": True, "result": res}
    if tool_name == "MessageSend":
        from . import comm_tools
        res = comm_tools.message_send(
            target=args.get("target", ""),
            message=args.get("message", ""),
            channel=args.get("channel"),
            account=args.get("account"),
        )
        return {"ok": True, "result": res}
    if tool_name == "ObsidianOpen":
        res = vault_tools.obsidian_open(args.get("path", ""))
        return {"ok": True, "result": res}
    return {"ok": False, "result": None, "error": f"Tool '{tool_name}' nicht implementiert."}


def _execute_code(tool_name, args, allow_elevated=False):
    from . import code_tools
    if tool_name == "ListDir":
        res = code_tools.list_dir(
            args.get("path") or ".",
            recursive=bool(args.get("recursive")),
            max_depth=args.get("max_depth"),
        )
        return {"ok": True, "result": res}
    if tool_name == "ReadFile":
        res = code_tools.read_file(
            args.get("path"),
            offset=args.get("offset"),
            limit=args.get("limit"),
        )
        return {"ok": True, "result": res}
    if tool_name == "Grep":
        res = code_tools.grep(
            args.get("pattern", ""),
            path=args.get("path") or ".",
            max_hits=args.get("max_hits"),
            case_insensitive=bool(args.get("case_insensitive")),
        )
        return {"ok": True, "result": res}
    if tool_name == "WriteFile":
        res = code_tools.write_file(args.get("path"), args.get("content", ""))
        return {"ok": True, "result": res}
    if tool_name == "SearchReplace":
        res = code_tools.search_replace(
            args.get("path"),
            args.get("old"),
            args.get("new"),
        )
        return {"ok": True, "result": res}
    if tool_name == "RunCommand":
        timeout = args.get("timeout")
        res = code_tools.run_command(
            args.get("command", ""),
            cwd=args.get("cwd"),
            timeout=int(timeout) if timeout is not None else None,
            allow_elevated=bool(allow_elevated),
        )
        # exit_code != 0 ist kein Tool-Fehler — Ergebnis trotzdem ok
        return {"ok": True, "result": res}
    # --- Stufe 1+2: Wissen & Reichweite (read-only) ---
    if tool_name == "VaultFind":
        return {"ok": True, "result": code_tools.vault_find(args.get("query", ""), top_k=args.get("top_k"))}
    if tool_name == "ReadNote":
        return {"ok": True, "result": code_tools.read_note(args.get("path", ""))}
    if tool_name == "WikiGet":
        return {"ok": True, "result": code_tools.wiki_get(args.get("lookup", ""))}
    if tool_name == "WebSearch":
        return {"ok": True, "result": code_tools.web_search(args.get("query", ""), count=args.get("count"))}
    if tool_name == "ExtractUrl":
        return {"ok": True, "result": code_tools.extract_url(args.get("url", ""), goal=args.get("goal"))}
    if tool_name == "MailList":
        return {"ok": True, "result": code_tools.mail_list(args.get("folder"), args.get("query"), args.get("limit"))}
    if tool_name == "MailRead":
        return {"ok": True, "result": code_tools.mail_read(args.get("id", ""), args.get("folder"))}
    if tool_name == "ReadMemory":
        return {"ok": True, "result": code_tools.read_memory(args.get("scope"))}
    if tool_name == "VaultWrite":
        return {"ok": True, "result": code_tools.vault_write(args.get("path", ""), args.get("content", ""))}
    if tool_name == "RecallAsOf":
        return {"ok": True, "result": code_tools.recall_as_of(
            args.get("query", ""), args.get("as_of") or args.get("date"), args.get("top_k"))}
    if tool_name == "RecallChangedSince":
        return {"ok": True, "result": code_tools.recall_changed_since(
            args.get("since") or args.get("date"), args.get("limit"))}
    if tool_name == "TaskFinish":
        inner = code_tools.task_finish(
            args.get("id") or args.get("task_id") or "",
            args.get("content") or "",
            summary=args.get("summary") or "",
        )
        if not inner.get("ok"):
            return {
                "ok": False,
                "result": inner,
                "error": inner.get("error") or "TaskFinish fehlgeschlagen",
            }
        return {"ok": True, "result": inner}
    # --- ^_tick: Job-Ordner + Plan & Aktivität ---
    if tool_name == "RoutineOverview":
        from . import routines
        return {"ok": True, "result": routines.overview()}
    if tool_name == "RoutineJobs":
        from . import routines
        box = args.get("box") or None
        jobs = routines.list_jobs(box, include_done=bool(args.get("include_done")))
        return {"ok": True, "result": {
            "root": routines.root(),
            "jobs": jobs,
            "done": len(routines.list_jobs("Done")) if not box else None,
        }}
    if tool_name == "RoutineRead":
        from . import routines
        out = routines.read_job(args.get("name") or "", args.get("box") or None)
        if not out.get("ok"):
            return {"ok": False, "result": out, "error": out.get("error")}
        return {"ok": True, "result": out}
    if tool_name == "RoutineJobWrite":
        from . import routines
        out = routines.write_job(
            args.get("box") or "Inbox",
            args.get("name") or "",
            args.get("content") or args.get("body") or "",
        )
        if not out.get("ok"):
            return {"ok": False, "result": out, "error": out.get("error")}
        return {"ok": True, "result": out}
    if tool_name == "RoutineMove":
        from . import routines
        out = routines.move_job(
            args.get("name") or "",
            args.get("to") or "",
            question=args.get("question") or "",
        )
        if not out.get("ok"):
            return {"ok": False, "result": out, "error": out.get("error")}
        return {"ok": True, "result": out}
    if tool_name == "RoutineLog":
        from . import routines
        out = routines.append_log(args.get("lines"))
        if not out.get("ok"):
            return {"ok": False, "result": out, "error": out.get("error")}
        return {"ok": True, "result": out}
    if tool_name == "RoutinePlan":
        from . import routines
        return {"ok": True, "result": routines.plan_overview(
            include_done_tasks=bool(args.get("include_done_tasks"))
        )}
    if tool_name == "RoutineSet":
        from . import routines
        item_id = str(args.get("id") or "").strip()
        schedule_kind = str(args.get("schedule_kind") or "daily").strip().lower()
        schedule = {"kind": schedule_kind, "time": str(args.get("schedule_time") or "07:00").strip()}
        if schedule_kind == "weekly" and args.get("weekday") is not None:
            schedule["weekday"] = int(args.get("weekday"))
        if item_id:
            patch = {k: v for k, v in {
                "title": args.get("title"),
                "prompt": args.get("prompt"),
                "pass": args.get("pass"),
                "schedule": schedule if (args.get("schedule_time") or args.get("weekday") is not None) else None,
                "paused": args.get("paused"),
                "allow_write": args.get("allow_write"),
            }.items() if v is not None}
            out = routines.update_routine(item_id, patch)
        else:
            out = routines.create_routine({
                "title": args.get("title") or "",
                "prompt": args.get("prompt") or "",
                "pass": args.get("pass") or "",
                "schedule": schedule,
                "paused": bool(args.get("paused")),
                "allow_write": bool(args.get("allow_write")),
            })
        if not out.get("ok"):
            return {"ok": False, "result": out, "error": out.get("error")}
        return {"ok": True, "result": out}
    if tool_name == "RoutineRun":
        from . import routines
        out = routines.run_routine(
            args.get("id") or "",
            force=bool(args.get("force", True)),
        )
        if not out.get("ok"):
            return {"ok": False, "result": out, "error": out.get("error") or "Lauf fehlgeschlagen"}
        return {"ok": True, "result": out}
    return {"ok": False, "result": None, "error": f"Tool '{tool_name}' nicht implementiert."}
