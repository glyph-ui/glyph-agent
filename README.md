# glyph-agent — persönlicher lokaler Obsidian-Assistent (B+)

Schlanker Agent: **lokales Vault-Gedächtnis** + **Cloud-Denker** (OpenRouter).
Spielregeln: [`CONSTITUTION.md`](./CONSTITUTION.md).

## Architektur B+ (aktuell)

```text
Nutzerfrage
  → VaultFind (Hybrid: 0.7 Embedding bge-m3 + 0.3 Keyword)   [lokal]
  → Web bei Bedarf: Exa = grob · TinyFish = fein              [API]
  → Direct deepseek-v4-flash-vision-exp formuliert           [Cloud]
       → bei Ausfall: OpenRouter deepseek/deepseek-v4-flash-0731
  → Antwort + Trace/Steps
```

Ollama läuft **nur** für **Embeddings** (`bge-m3`) — **kein** Chat.

## Betriebsarten

| MODE | Bedeutung | Standard |
|------|-----------|----------|
| `agent` | VaultFind + Tools + Cloud-Antwort (Vision-Exp → Flash-0731) | Default |
| `code` | `^_Code` — Workspace-Tools + Vision-Exp (kein VaultFind) | per Request `mode: "code"` oder Env |
| `openrouter-chat` | Reiner Chat — kein Wiki/Vault/Tools | — |

### MODE=`code` (^_Code / C′)

Separater Pfad, **nicht** der Vault-Default:

```text
Nutzerfrage (mode=code)
  → ListDir / ReadFile / Grep / SearchReplace / WriteFile / RunCommand
  → nur CODE_WORKSPACE_ROOTS (existierende Dirs)
  → Direct deepseek-v4-flash-vision-exp formuliert
       → bei Ausfall: OpenRouter deepseek/deepseek-v4-flash-0731
  → Write/Shell → pending_confirmation + resume_token (Glyph-Genehmigung)
```

| Variable | Default | Bedeutung |
|----------|---------|-----------|
| `CODE_OPENROUTER_MODEL` | `deepseek-v4-flash-vision-exp` | Denker (Text + Bilder) |
| `CODE_OPENROUTER_FALLBACK_MODEL` | `deepseek/deepseek-v4-flash-0731` | Reserve wie °_Agent |
| `CODE_WORKSPACE_ROOTS` | `~/glyph-ui,~/glyph-agent,~/.openclaw/workspace` (+ `~/grok-chat-ui` wenn Dir existiert) | erlaubte Roots (nur existierende) |
| `CODE_WORKSPACE_ONLY` | `true` | roots-only (v1 immer; Env reserviert) |
| `CODE_SHELL_TIMEOUT` | `60` | Shell-Timeout (s) |
| `CHAT_TIMEOUT` | `60` | Hartes Total-Timeout pro OpenRouter-Chat-Call (s) |
| `CODE_CHAT_TIMEOUT` | `180` | OpenRouter-Wall-Clock im CODE-Modus (DeepSeek Multi-Round) |
| `CODE_SHELL_ALLOW` | (Builtin-Whitelist) | Regex-Liste, Trenner `\|\|` |
| `CODE_MAX_ROUNDS` | `32` | Tool-Loop-Runden |
| `CODE_MESSAGE_CHARS` | `64000` | Cap für CODE `messages[]` (älteste Turns weg) |

**Stabilität:** `server.py` nutzt `ThreadingHTTPServer` (hängender `/chat` blockiert nicht `/health`).
Jeder Cloud-Call hat Wall-Clock-Timeout (Worker + `future.result`); ACP-Client bricht per
`GLYPH_AGENT_TIMEOUT` ab (Default CODE 8 min / agent 5 min).

Tools: **ListDir** (optional recursive depth≤2), **ReadFile** (offset/limit Zeilen), **Grep**,
**SearchReplace** (exakt 1 Treffer, Backup), **WriteFile** (Diff+Backup), **RunCommand** (Whitelist + Deny).  
Write/Shell brauchen Confirm; ohne `confirm`/`resume_token`+`allow` → `pending_confirmation`.
Shell-Whitelist u. a.: git status/diff/log/add/commit/stash (kein push), mkdir/touch/cp/diff, python3/node Scripts.

### Provider

| `AGENT_PRIMARY_PROVIDER` | Verhalten |
|--------------------------|-----------|
| **`openrouter`** (B+-Default) | DeepSeek V4 Flash → free bei Ausfall. Kein lokaler Chat. |
| `fallback` | Alias derselben 2-Stufen-Cloud-Kette. |

## Architektur-Prinzip

```
Glyph (Browser) → glyph-agent HTTP (server.py:18899) → Tool-Loop → OpenRouter (Flash → free)
                                                              ↓
                                                  Vault (Obsidian) · Web (Exa/TinyFish)
```

- **Keine große Agenten-Bibliothek**, keine MCP.
- Werkzeuge = einfache Python-Funktionen (`core/vault_tools.py`) mit Whitelist + Bestätigung.
- Nur Python-stdlib + Ollama für Embeddings; Web-Recherche optional Exa/TinyFish.

## Sicherheitsmaßnahmen

1. **Pfad-Sicherheit:** Zugriff nur innerhalb des Vaults (Block gegen `../`-Escape/Symlinks).
2. **Kein Löschen/Umbenennen** — nur Lesen, Erstellen, Bearbeiten.
3. **Änderungsvorschau (Diff):** Vor jedem Schreiben zeigt der Agent einen Unified-Diff;
   du bestätigst, sonst wird nichts geschrieben.
4. **Backup + Revisionsnummer:** Vor jedem Schreiben wird der alte Inhalt in `vault/backups/`
   gesichert (R1, R2, …) — atomar via Temp+rename.
5. **Zentrale Schreibfunktion:** Schreiben läuft NUR über `apply_edit` — nie direkt anderswo.
6. **Prompt-Injection-Schutz:** Vault-Dateien sind DATEN, keine Anweisungen.
7. **Geschützte Ordner:** `BLOCKED_DIRS` (Default: private, privat, secrets, health, geheim,
   persönlich, personenbezogen, recovery, …) werden von Suche/Lesen/Editieren ausgeschlossen
   (env-überschreibbar). Zusätzlich: heikle **Dateinamen**-Muster (Behörden/Familie/Unterhalt-Spiegel
   im Wiki, `wiki-import`, …) via `_is_blocked` — HSEQ-fachliche `unsafe-local-themen/…` bleiben.
8. **Cloud-Audit:** Jede Übertragung an OpenRouter wird in `logs/actions.jsonl` protokolliert
   (provider, model, Zeichen, Zeit). Kein Senden ohne `OPENROUTER_API_KEY`.
9. **Datenschutz-Schranke:** `EXTERNAL_MAX_CHARS` (Default 4000) kürzt Kontext vor Cloud-Übergabe.
10. **Modus-Trennung:** `openrouter-chat` hat KEINE Tools/Vault/Dateien — isolierte Oberfläche.
11. **Vault-Verträge:** `AGENTS.md` in HSEQ Sync (+ memory-wiki) wird in den System-Prompt geladen
    (Arbeitsregeln; Fachnotizen bleiben DATEN).

## Nutzung

### Lokaler HTTP-Dienst (Hauptweg über Glyph)

```bash
python3 server.py          # /chat, /health, /models auf 127.0.0.1:18899
curl http://127.0.0.1:18899/health
curl http://127.0.0.1:18899/models
curl -X POST http://127.0.0.1:18899/chat -H 'Content-Type: application/json' \
     -d '{"message": "Was steht im Vault zu Brandschutz?"}'
# Hot-Apply OpenRouter Primary/Fallback (nächste Nachricht, ohne Restart):
curl -X POST http://127.0.0.1:18899/models -H 'Content-Type: application/json' \
     -d '{"shared":{"primary":"deepseek/deepseek-v4-flash-0731","fallback":"inclusionai/ling-3.0-tiny:free"}}'
# Probe (eine ID testen):
curl -X POST http://127.0.0.1:18899/models/probe -H 'Content-Type: application/json' \
     -d '{"model":"inclusionai/ling-3.0-tiny:free"}'
```

Antwort enthält `used_provider` / `used_model` (welches Modell geantwortet hat —
DeepSeek Flash oder Free-Fallback). Ohne `confirm`-Liste werden Schreib-Tools abgelehnt.

SoT für UI-getriebene Models: Glyph **Anbindung** (`~/.glyph-ui/bindings.json`) → Bridge pushed an `POST /models`.

### Kommandozeile (CLI)

```bash
python3 -m scripts.cli search "Altöl"
python3 -m scripts.cli read "Themen/PSA.md"
python3 -m scripts.cli summarize "Themen/PSA.md"
python3 -m scripts.cli summarize "Themen/PSA.md" "mit Fokus auf Fristen"
python3 -m scripts.cli create "test/Neue Notiz.md" "Abfallregister vorhanden"
python3 -m scripts.cli propose "Themen/PSA.md" "Füge Abschnitt Prüfintervalle ein"
python3 -m scripts.cli backups
python3 -m scripts.cli web "TRGS 510 Aktuelle Anforderungen"
```

### Selbsttest

```bash
python3 tests/test_providers.py        # Provider-Routen + Free-Fallback-Pfad
python3 tests/test_runtime_models.py   # Hot-Apply / Snapshot
```

## Konfiguration

Alle Werte in `core/config.py` bzw. per Umgebungsvariable/`.env`.

**Primär-Vault:** `VAULT_PATH` → z. B. `/Users/<du>/ObsidianVaults/HSEQ Sync`.

**Betriebsart:**

| Variable | Default | Bedeutung |
|----------|---------|-----------|
| `MODE` | `agent` | `agent` \| `openrouter-chat` |
| `AGENT_PRIMARY_PROVIDER` | `direct` | `direct` \| `openrouter` \| `fallback` |
| `AGENT_OPENROUTER_MODEL` | `deepseek-v4-flash-vision-exp` | Primär-Cloud-Denker |
| `AGENT_OPENROUTER_FALLBACK_MODEL` | `deepseek/deepseek-v4-flash-0731` | OpenRouter bei Direct-Ausfall |
| `OPENROUTER_MODEL` | `deepseek-v4-flash-vision-exp` | Modell im `openrouter-chat`-Modus |
| `OPENROUTER_FALLBACK_MODEL` | `deepseek/deepseek-v4-flash-0731` | Reserve im Chat-Modus |
| `EXTERNAL_MAX_CHARS` | `4000` | Kürzung vor Cloud-Übergabe |
| `OPENROUTER_API_KEY` | — | Pflicht für Chat |
| `GLYPH_SEARCH_BRIDGE` | `1` | Suchbrücke: Rückfrage + Verlauf → eigenständige Suchbegriffe (0 = aus, wörtlich wie vorher) |
| `GLYPH_SEARCH_BRIDGE_MODEL` | (leer) | Modell der Suchbrücke; leer = aktives Agent-Modell |
| `GLYPH_SEARCH_BRIDGE_TIMEOUT` | `8` | Hartes Zeitlimit des Brücken-Calls (s), danach Regel-Fallback |
| `GLYPH_SEARCH_BRIDGE_QUERIES` | `2` | Suchanfragen pro Turn (Think → Search → Search) |
| `GLYPH_SEARCH_BRIDGE_TURNS` / `_CHARS` | `6` / `2500` | Verlaufsfenster im Brücken-Prompt |
| `WEB_CTX_HITS` | `7` | Treffer-Obergrenze im Antwort-Kontext (gemischt über alle Anfragen) |

**Beispiel:**

```env
MODE=agent
AGENT_PRIMARY_PROVIDER=direct
AGENT_OPENROUTER_MODEL=deepseek-v4-flash-vision-exp
AGENT_OPENROUTER_FALLBACK_MODEL=deepseek/deepseek-v4-flash-0731
CODE_OPENROUTER_MODEL=deepseek-v4-flash-vision-exp
OPENROUTER_API_KEY=sk-or-…
```

## Projektstruktur

```
glyph-agent/
├── server.py           # HTTP: /chat, /health, GET|POST /models, /models/probe
├── core/
│   ├── config.py       # zentrale Konfiguration (+ CODE_*)
│   ├── runtime_models.py  # Hot-Apply Primary/Fallback ohne Restart
│   ├── llm.py          # Provider-Brücke
│   ├── tool_loop.py    # Agenten-Loop (Vault)
│   ├── code_loop.py    # CODE-Loop (^_Code)
│   ├── code_tools.py   # ListDir/Read/Grep/SearchReplace/Write/Run
│   ├── tool_registry.py
│   ├── vault_tools.py  # + wiki_status
│   ├── pdf_tools.py    # ReadPdf (pdftotext)
│   ├── comm_tools.py   # MailList/MailRead/MessageSend
│   ├── agent.py
│   ├── web.py          # + BrowseUrl
│   └── providers/
│       ├── openrouter.py # DeepSeek Flash → free
│       ├── fallback.py   # Alias derselben Kette
│       └── factory.py
├── scripts/cli.py
├── tests/
├── vault/backups/
└── logs/
```

## Tools

### agent (Vault / B+ / °_Agent)

| Tool | Rolle |
|------|--------|
| **VaultFind** | Hybrid Embedding+Keyword. Aliase: VaultRecall, VaultSearch, **WikiSearch** |
| ReadNote / **WikiGet** / Summarize | Vault lesen |
| CreateNote / ProposeEdit / ApplyEdit / **WikiApply** | Vault schreiben (Confirm) |
| **WikiStatus** | agent-digest Stats (read-only) |
| WebSearch (Exa) | grobe Websuche — Anfrage kommt aus der **Suchbrücke** (`core/query_bridge.py`), die Rückfragen mit dem Chat-Verlauf auflöst |
| ExtractUrl / FetchUrl / **BrowseUrl** (TinyFish) | feine Zielseiten / Summary |
| **ReadPdf** | PDF im Vault via pdftotext (graceful) |
| **MailList** / **MailRead** | himalaya (graceful) |
| **MessageSend** | openclaw message send (Confirm, graceful) |
| ObsidianOpen | optional kepano-CLI, pfadgebunden |

Kein Shell im Agent-Modus.

### code (^_Code)

| Tool | Rolle |
|------|--------|
| ListDir / ReadFile / **Grep** | Workspace lesen (nur `CODE_WORKSPACE_ROOTS`) |
| **SearchReplace** / WriteFile | Schreiben mit Backup; unter Workspace **r+w ohne Popup** |
| RunCommand | Whitelist unter r+w ohne Popup; **elevated** (push/compound/service) = Glyph-Confirm |

## Shared SoT + Memory (Grok · ^_Code · °_Agent)

| | |
|--|--|
| **Vertrag** | `~/.glyph/AGENTS.md` |
| **Memory (Lektionen)** | `~/.glyph/MEMORY.md` |
| **Skills** | `~/.glyph/skills/` |
| **Grok** | `~/.grok/rules/glyph-shared.md` + `glyph-memory.md` |
| **Code/Agent** | System-Prompt lädt AGENTS + MEMORY |

OpenClaw-Memory ist umgezogen (Stub unter `.openclaw/workspace/MEMORY.md`).  
Geklärtes und Lektionen stehen in Dateien — im Chat **nicht** neu erzählen.

## Vaults & Verträge

| Vault | Rechte | Vertrag |
|-------|--------|---------|
| **HSEQ Sync** | lesen+schreiben | `…/HSEQ Sync/AGENTS.md` |
| ASI, BS. UWS, QM, EM | lesen | Facharchiv |
| memory-wiki | lesen+schreiben | `…/memory-wiki/AGENTS.md` |
| Peniel | lesen | — |
| **Privat** | **nie** | Red Line (nicht in `VAULT_PATHS`) |

Jobs / Skills / Prompts: siehe HSEQ `AGENTS.md` und `Vorlagen/Jobs/`.

## Skills (`~/.glyph/skills/` · auch `~/.glyph-agent/skills/`)

| Skill | Alias / ID | Rolle |
|-------|------------|--------|
| `hseq-eingang` | `td-eingang` | 18:00 Eingang → Fertig |
| `hseq-handover` | `td-handover` | 18:30 Daily + **3-Zeilen-Briefing** |
| `hseq-aus-fertig-lernen` | `td-lernen` | Fr 19:00 max. 1 Compounding-Edit |
| `vault-ingest` | — | Quelle → Claims/Links (≥1 Synthese) |
| `merken` | — | 1 Erkenntnis → Schicht; Wiki nach Vorlage + Chat-Ja; Ablehnen ohne Suchwert |
| `einmal-job` | — | 1× Plan→Ja, dann Recurring (Kalender → Plan) |

```bash
python3 scripts/run_job.py hseq-eingang --force          # = td-eingang
python3 scripts/run_job.py hseq-handover --force
python3 scripts/run_job.py hseq-aus-fertig-lernen --force
python3 scripts/index_hygiene.py                         # heikle Pfade prüfen
```

Recurring SoT: `jobs/recurring.json` · UI: Glyph Kalender → Plan.

Index-Hygiene: `python3 scripts/index_hygiene.py`

> „Persönliche Funktionalität vor Produktarchitektur."
