# glyph-agent

Lokale Engine hinter dem Glyph-Profil **glyph-agent**: Vault-Gedächtnis, Recherche-Tools und Cloud-Antwort. Eigenes Domänenvokabular — getrennt von Glyph-UI.

## Orient (für ^_tick / Agenten)

1. Diese Datei zuerst lesen — **nicht** blind ListDir/Grep über `core/`.
2. Aufgabe → **Node** → nur deren Quellen. MODE entscheidet den Pfad (`agent` vs `code`).
3. Neue Route/Tool/Registry → Map hier updaten; SoT-Pfade (`~/.glyph/*`) nicht im Code hardcoden ohne Registry.

## System map

| Node | Tut | Quellen | Hängt an / produziert |
|------|-----|---------|------------------------|
| **HTTP** | `/chat`, `/health`, `/vault/find`, vaults/workspaces/recurring/tasks CRUD | `server.py` | alle Loops |
| **Agent-Loop** | B+: VaultFind → Tools → Cloud-Denker. UI: `vault_search=false` = memory-wiki + offenes Web; Auswahl = Arbeits-Vault + Wiki | `core/tool_loop.py`, `core/agent.py`, `core/vault_preview.py`, `core/vault_scope.py` | retrieval, research, vault_tools |
| **Vault-Schreiben** | Chat-Wachstum: CreateNote/ApplyEdit unter Themen/ + Wiki. Chat nie löschen/leeren. Job `td-wiki-hygiene` darf doppelt tote Wiki-Dateien in den 30-Tage-Korb. | `core/vault_write_policy.py`, `core/vault_tools.py`, `scripts/wiki_hygiene.py` | tool_loop, jobs |
| **Code-Loop** | ^_tick (Nachfolger ^_Code): Read/Write/Grep/Shell; Freigabe Einmal/Auftrag/Task; **TaskFinish** schließt Glyph-Aufgabe (Artefakt + `done`, kein Workspace-Grant) | `core/code_loop.py`, `core/code_tools.py`, `core/tasks.py` | `workspaces_registry`, Grant-Store, `~/.glyph/tasks.json` |
| **Routine-Ordner** | ^_tick: `Daily/` · `Inbox/` · `Blocked/` · `Done/` · `Log/` unter `~/ObsidianVaults/memory-wiki/Routinen/`; Anlegen/Lesen/Verschieben/Protokoll über Routine-Tools, Plan & Aktivität über `RoutineOverview`/`RoutinePlan`/`RoutineSet`/`RoutineRun` (SoT `jobs/recurring.json` + `~/.glyph/tasks.json`); HTTP `GET /routines/overview` für die UI | `core/routines.py`, `core/tool_registry.py` (CODE_TOOLS) | Vault `memory-wiki`, `recurring`, `tasks` |
| **Tools-Registry** | Tool-Namen, Schemas, Dispatch. **ChatDatei**: PDF/Word nur als Chat-Link, kein Vault, kein Plattenrest | `core/tool_registry.py`, `core/chat_file.py` | tool_loop / code_loop |
| **Bind-Store** | Persistenz + Kern-PATCH für Vaults+Workspaces; `heads` je Kopf (grok/agent/code) | `core/bind_store.py` | vaults_registry, workspaces_registry |
| **Vaults** | `~/.glyph/vaults.json`, Pins, Privat-Block | `core/vaults_registry.py` | Index, VaultFind |
| **Workspaces** | `~/.glyph/workspaces.json` r/r+w/🔒 | `core/workspaces_registry.py` | code_tools Pfadchecks |
| **Retrieval** | Embeddings `bge-m3` + Keyword hybrid | `core/retrieval.py` | Ollama lokal |
| **Research** | Exa grob, TinyFish fein | `core/research.py`, `core/web.py` | tool_loop |
| **Suchbrücke** | Rückfrage + Verlauf → 1–2 eigenständige Suchbegriffe, **bevor** Exa/TinyFish sie sehen. Kein Call ohne Verlauf/Kontextbezug, keiner lokal; Regel-Fallback bei Fehler. Stufe `SearchBridge` (UI: `Query`) | `core/query_bridge.py`, `core/tool_loop.py` (`_run_web_precheck`, `_bridged_web_args`) | history, llm, web |
| **Swarm** | Composer-Aktion: Planer → Websuche → Synthese. Einstieg `POST /chat` mit `swarm: true` (`run_swarm`). Köpfe °_Agent / ^_Code. | `core/swarm.py` | HTTP `/chat`, Glyph-Composer |
| **Recurring/Jobs** | Plan, Catch-up, HSEQ-Seeds, Nightly-Scripts (`memory_hygiene.py`, `wiki_hygiene.py`, `session_cleanup_legacy.py`) | `core/recurring.py`, `jobs/`, `scripts/` | `jobs/recurring.json` |
| **Aufgaben** | Manuelle Übergabe zwischen Köpfen. SoT `~/.glyph/tasks.json`. Zielkopf optional, nie die ganze Session. | `core/tasks.py` | HTTP `/tasks`, Glyph Plan-Tab |
| **LLM/Provider** | Direct → Router (OpenRouter oder Orca, `OPENROUTER_URL`) | `core/llm.py`, `core/providers/` | config, Bindings |
| **Config** | Env, Models, Ports | `core/config.py`, `core/dotenv.py` | alles |
| **Verfassung** | B+-Regeln, Tabus | `CONSTITUTION.md` + **diese** CONTEXT | `~/.glyph/AGENTS.md`, MEMORY |

**Zwei Kontexte:** UI-Begriffe → `~/glyph-ui/CONTEXT.md`. Engine hier. Nicht vermischen.

**Crux:**  
- Workspace-Recht `r+w` = beschreibbar, nicht Auto-Write. Apply braucht **Freigabe** (Einmal / Auftrag / Task) plus Grant-Store. Elevated und hart-deny gelten weiter.  
- °_Agent default **kein Shell**. Shell nur `MODE=code|tick` (^_tick).  
- Embeddings **nur** lokal `bge-m3` — nie Cloud-Embedding.

## Language

**Nutzerantworten:** stop-slop (immer) — Kern zuerst, kein AI-Slop, keine erfundenen Normen.

**Fortschreiben**:
°_Agent legt an und ergänzt Notizen unter HSEQ `Themen/` und Wiki `concepts|entities|syntheses` (neue Source unter `sources/`). Chat: kein Löschen, kein Leeren, kein Umschreiben von Eingang oder Sources. Chat ohne Freigabe-Dialog auf diesen Pfaden. Wiki-Hygiene-Job: nur memory-wiki, doppelt tot → `_hygiene-trash/` (30 Tage).
_Avoid_: Vault-Delete aus dem Chat; Auto-Write auf Vorlagen/Fertig/Hauptarchiv; Freigabe-Popup für Themen-Wachstum; Hygiene auf HSEQ/Privat

**Cloud-Antwort**:
Die vom Cloud-Denker formulierte Nutzerantwort im B+-Pfad (nach VaultFind/Web/Tools). In UI und Nutzer-Doku so nennen.
_Avoid_: OpenRouter-Antwort, OpenRouter-Chat (als Nutzerbegriff)

**Cloud-Denker**:
Die Rolle des LLM, das die Antwort formuliert. Technik/Config darf den Provider nennen; Nutzer-UI nicht.
_Avoid_: OpenRouter als Synonym für diese Rolle in UI-Texten

**VaultFind**:
Ein nach außen sichtbares Finde-Werkzeug; intern hybrid Embedding + Keyword über erlaubte Vaults.
_Avoid_: VaultRecall, VaultSearch (Aliase, kein neuer Begriff)

**Ordner-Suche**:
Manueller Arbeits-Vault-Zugriff aus der UI (Modus Vault bei °_Agent). `POST /vault/find` liefert Treffer ohne LLM (Arbeits-Vault; leer → KomNet, sonst DGUV). `/chat` mit `vault_search=false`: memory-wiki + TinyFish/Exa + offenes Web, kein Arbeits-Vault. KomNet und DGUV sind dabei normale Webtreffer, kein Verbot. `vault_selected`: gewählte Arbeits-Vault-/KomNet-/DGUV-Treffer plus memory-wiki. Ohne diese Felder bleibt B+ (Jobs). TinyFish und Exa immer.
_Avoid_: Arbeits-Vault ohne Apfel; Wiki hinter den Apfel; HTML-Scrape der Zielseite; offene Websuche als Apfel-Fallback

**OpenRouter (Technik)**:
Env- und Provider-IDs des Router-Hops (`OPENROUTER_URL`, `OPENROUTER_API_KEY`, Modus `openrouter`). **Kein** Glyph-UI-Profil. UI-Name: **Router**.
_Avoid_: OpenRouter-Profil (entfernt); OpenRouter als sichtbarer Slot-Name

**Router**:
Slash-Hop: `OPENROUTER_URL` + `OPENROUTER_API_KEY`. Default `https://openrouter.ai/api/v1`; Orca `https://api.orcarouter.ai/v1`. `~/.glyph-ui/bindings.json` überschreibt `.env` für URL und Key (wie Direct-Host).
_Avoid_: Slash-ID immer nach openrouter.ai hart verdrahten

**B+**:
Aktuelle Architektur: lokal Gedächtnis/Suche, Web nur bei Bedarf, Cloud-Denker formuliert, Antwort + Trace.
_Avoid_: „3-Stufen-Fallback“ / lokaler Chat-Fallback (existiert nicht mehr; Direct Vision-Exp → Router-Reserve)

**Workspace-Recht**:
Capability eines angebundenen Code-Roots: ungebunden / `r` / `r+w` / `private`. SoT `~/.glyph/workspaces.json`. `r+w` erlaubt Lesen und macht Apply möglich — Apply selbst braucht eine **Freigabe**. Kein allgemeines `w` im Kabelsalat.
_Avoid_: r+w = WriteFile allow; Recht als Shell-/Netz-Berechtigung; `w` als Standard-Mode

**Freigabe**:
Zeitlich begrenzte Erlaubnis für Apply. Stufen: **Einmal**, **Auftrag**, **Task**. Nie Session-weit, nie immer. Regex-Whitelist ist untere Grenze, nicht die Berechtigung.
_Avoid_: Session-Always; auto-allow unter r+w; Whitelist = Permission

**Auftrag**:
Eine Nutzeranweisung bzw. ein Änderungssatz. Grant stirbt mit Abschluss, Abbruch, Fehler oder Zeitlimit. Nicht Recurring.
_Avoid_: Job (SoT = Recurring); Session

**Task**:
Benannte Arbeit, nicht die Chat-Session. Grant: `task_id`, Workspace-Root, Pfadpräfixe, Aktionsklassen, Ablauf, Widerruf. Nur explizit schließen (oder Workspace-Wechsel, Widerruf, 2h Inaktivität). Neuer Prompt startet keinen Task. Aktion außerhalb Scope → Grant greift nicht + Hinweis.
_Avoid_: Session-Grant; Chat als Scope; Prompt-Themenklassifikation; mit **Aufgabe** (Übergabe) verwechseln

**Aufgabe**:
Manuell übergebene Arbeit zwischen Köpfen. Neu braucht `pass` und einen Beleg aus **Meldung + Antwort**. Speichert nur diesen Snapshot (plus kompakter Trace, Anhang-Pfade) in `~/.glyph/tasks.json`. Zielkopf optional. Status `analysis` ist Workflow, kein Kopf. Status `done` nur mit `artifact`. Plan-UI **Erledigt** = `DELETE /tasks/<id>` (Eintrag weg, kein Pfad).
_Avoid_: Aufgabe ohne Meldung; Session-Zeiger; ganze Session; Vault-Inhalt; Analyse als Kopf; Recurring-To-do; Plan-Erledigt als Link-Pflicht

**Änderungssatz**:
Gesammelte Dateiänderungen, ein Gesamt-Diff, atomarer Apply nach Freigabe. Danach Tests; Fail → kein Commit.
_Avoid_: Datei-für-Datei Apply nach Sammel-Review; Auto-Commit

**Aktionsklasse**:
`file_change` · `test` · `git_commit` · `network` · Paketinstall · Deploy/Remote. Ein Grant für `client/src/**` deckt nicht `server/`. `git commit` immer explizit; `npm install`/`npx` immer explizit; Netzwerk mit Zielhost; push/deploy immer einzeln.
_Avoid_: git commit in der Whitelist als stilles Allow

## Settled decisions (Direct-Hop 2026-08-12)

- °_Agent und ^_Code: Direct `deepseek-v4-flash-vision-exp` (Text + Bilder) → Router-Reserve (Slash-ID). Router-Host `OPENROUTER_URL` Default `https://openrouter.ai/api/v1`; Orca `https://api.orcarouter.ai/v1`. Kein Gemini-Default, kein Auto-Hop nur bei Screenshot.
- Key: `DIRECT_API_KEY` (Alias `DEEPSEEK_API_KEY`) + `DIRECT_API_URL` (Default `https://api.deepseek.com`). Router-Key `OPENROUTER_API_KEY` (OpenRouter `sk-or-…` oder Orca `sk-orca-…`). OpenAI-kompatibel, nicht DeepSeek-exklusiv.
- Thinking = API-Default. Chat `EXTERNAL_MAX_CHARS=8000`. Kein Tiny/Free.

## Settled decisions (grill 2026-08-05)

- UI-Sprache: **glyph-agent / Cloud-Antwort**; **OpenRouter** nur in Config/CONSTITUTION/Technik.
- Domain-Doku: eigenes `CONTEXT.md` hier (Engine); Glyph-UI hat eigenes CONTEXT — zwei Kontexte.
- Live-Test „grün“: Antwort + Steps **und** VaultFind erkennbar (Q8=B) — gilt für Jobs/B+ ohne UI-Toggle; interaktives °_Agent seit 2026-08-15 nur bei Ordner-Suche-Toggle.

### Settled decisions (Ordner-Suche 2026-08-15)

- Interaktives °_Agent: keine automatische Vault-/Ordner-Suche. Toggle in der UI.
- ACP-Adapter setzt immer `vault_search` (false außer Toggle an). Fehlt `_meta` → aus, nicht B+.
- `vault_search=false` → kein VaultFind/ListVaultDir-Precheck, Tools gesperrt.
- `vault_selected` → nur gewählte Treffer als Kontext, kein neuer VaultFind. `kind: web` = KomNet/DGUV-URL, kein ListVaultDir. Datei-Tap und Ordner-Pick: ReadPdf der 1–2 namenspassenden PDFs im selben Turn (nicht Listenplatz). Nie den Nutzer fragen, welche Datei zu öffnen.
- `/vault/find` ohne Vault-Treffer: Exa+TinyFish auf `komnet.nrw.de`; leer → dieselben auf `dguv.de`. Kein HTML-Scrape. Nicht Jobs/B+.
- WebSearch (Tool + Precheck + Swarm): Default `source=both` — Exa und TinyFish parallel, URLs mergen. Ohne Domainfilter: offene Suche; Amtssuche (Gesetz, Rechtsprechung, Behörden, KomNet, DGUV) **nur bei Rechts-/HSEQ-Fragen** (`research.is_legal_query`). Sortierung nach Relevanz zur Anfrage, Stufe bricht den Gleichstand; Ratgeber ans Ende, nichts verworfen. `include_domains` bleibt harter Filter. ExtractUrl/FetchUrl bleibt TinyFish für konkrete Seiten.
- `/chat` ohne Flag: B+ unverändert (HSEQ-Jobs, Scripts).
- Kein ADR nötig — CONTEXT reicht (Q9=C).

### Settled decisions (Websuche-Relevanz 2026-10-09)

Anlass: „test“ als Suchanfrage lieferte Testverordnung/DGUV-Testseiten — die Amtssuche lief für **jede** Anfrage mit und die Stufen-Sortierung schob amtliche Treffer ohne Bezug nach vorn.

- **Relevanz vor Quellenrang:** `source_rank.relevance(query, row)` (Wortstamm-Abgleich Query ↔ Titel/Snippet/URL, Titel doppelt, eigenes Wort > Kompositum) sortiert die Treffer; `source_rank.band` (Stufe) entscheidet nur bei Gleichstand (`rank_key`). Treffer ohne Bezug (relevance 0) bleiben erhalten, landen aber hinter jedem passenden Treffer.
- **Amtssuche nur bei Rechts-/HSEQ-Fragen:** `research.is_legal_query` (Signale: Gesetz/Verordnung/Vorschrift/Prüfung/DGUV/DIN …). „test test test“ → nur offene Suche.
- **Volltext statt Snippets:** die `attach_top=2` relevantesten Treffer bekommen `text` (TinyFish Fetch, max. 8.000 Zeichen, 12 s Timeout, parallel). Swarm setzt `attach_top=0` (verdichtet auf Snippets). Fetch-Ausfall lässt die Suche unverändert stehen.
- Prompt (`research.policy_prompt_snippet`): „Mindestens Stufe 1/2 zitieren“ gilt **nur bei Rechtsfragen**; bei Sachfragen zählt der Inhalt. Nicht passende Treffer nicht aufzählen.
- Tests: `tests/test_source_rank.py` (Relevanz, Amtssuche-Gating, Top-2-Volltext).

### Settled decisions (Suchbrücke 2026-10-11)

Anlass: Die Nachfrage „wie oft muss ich das tun?" ging **wörtlich** an Exa/TinyFish (Treffer: Winterdienst, Postzustellung). Der Web-Precheck kannte nur die aktuelle Nachricht; der Verlauf lag vor, wurde aber nie an die Suche übergeben. `_derive_web_query` war rein regelbasiert.

- **Brücke vor der Suche:** `core/query_bridge.build_plan(user_message, prior_history)` gibt `{standalone, queries, terms, source}`. `source=passthrough` = unverändert wörtlich (erste Frage, eigenständige Frage), `llm` = Brücken-Call, `heuristic` = Regel-Fallback.
- **Wann ein Call läuft** (`needs_context`): nur mit Verlauf UND Kontextbezug — ≤3 Wörter, eindeutiges Verweiswort (`das`, `es`, `davon`, …) oder Rückfrage-Formulierung ohne eigenen Fachbegriff. Ein Norm-Kürzel oder langer Komposit-Begriff (`TRGS 600`, `Gefährdungsbeurteilung`) gilt als eigenständig → kein Call. **Erste Frage ohne Verlauf bleibt unverändert.**
- **Modell:** aktives Agent-Modell (heute Haiku 5.5), `SEARCH_BRIDGE_MODEL` kann es überschreiben. `temperature=0`, hartes Zeitlimit `SEARCH_BRIDGE_TIMEOUT` (Default 8 s) über `llm.chat(..., timeout=)`.
- **Lokaler Kopf:** kein Brücken-Call (Prefill ~90 s) — es bleibt beim Regel-Fallback.
- **Zwei Suchläufe:** bis zu `SEARCH_BRIDGE_QUERIES` (Default 2) Anfragen an Exa/TinyFish, Treffer reihum gemischt und URL-dedupliziert (`_merge_web_hits`, Kontext-Cap `WEB_CTX_HITS`, Default 7). Steps: `SearchBridge` → `WebSearch` (×n) → `LLM` → `answer`.
- **Ein Plan pro Turn:** `web_plan_holder` teilt den Plan zwischen Precheck und Modell-Runde; eine vom Modell gestellte Rückfrage wird ebenfalls durch `_bridged_web_args` geleitet (kein zweiter Brücken-Call).
- **Datenschutz:** an das Modell gehen nur Verlaufstext + Rückfrage (derselbe Hop, der ohnehin antwortet); an Exa/TinyFish nur die fertige Suchanfrage — nie Vault-Inhalt.
- **Aus-Schalter:** `GLYPH_SEARCH_BRIDGE=0` (dann wieder wörtlich wie vorher).
- Tests: `tests/test_query_bridge.py`, `tests/test_web_bridge_wiring.py`; UI-Label `SearchBridge → Query` in `glyph-ui/server/stepBanner.mjs` + `glyph-agent-acp.mjs`.

## Settled decisions (C′ 2026-08-07)

- **glyph-agent Default** bleibt **Vault-only** (kein Shell, kein allgemeines Repo-Schreiben).
- **`MODE=code|tick`** (per Request `mode: "code"|"tick"`): ^_tick-Pfad (`tick` = Code-Fähigkeiten + Routine-Kopf, eigene Rolle `tick_role()` und Routine-Tools) — Tools `ListDir` / `ReadFile` / `Grep` / `SearchReplace` / `WriteFile` / `RunCommand`, Denker `CODE_OPENROUTER_MODEL` (Default `deepseek-v4-flash-vision-exp`).
- **Workspaces-SoT:** `~/.glyph/workspaces.json` (Modes `r` / `rw` / `private`); Registry `core/workspaces_registry.py`. Fallback `CODE_WORKSPACE_ROOTS` nur wenn Store fehlt oder `CODE_WORKSPACES_USE_REGISTRY=false`; geladene leere/disabled Registry ist `[]`.
- Write/SearchReplace unter **`r+w` ohne Popup**. Whitelist-Shell unter `r+w` ohne Popup. **Aufgehoben 2026-08-22** (Freigabe Einmal/Auftrag/Task).
- **Elevated** braucht Glyph-Popup (`pending_confirmation` + `resume_token`): `git push|pull|fetch`, Compound, `npm run service:*`. Kein Session-Always für elevated. **Erweitert 2026-08-22:** kein Session-Always für Schreiben/Shell überhaupt.
- Fail nach Allow / unter r+w: **hard_error** + Banner (echter Grund). Hart-Deny: `rm`/`sudo`/… bleibt tot.
- Shell: Whitelist + Hard-Deny + Elevated-Klassifikation + Timeout; nur angebundene Roots.

## Settled decisions (Wiederkehrende To-dos 2026-08-08)

- **SoT:** `jobs/recurring.json` + `core/recurring.py`. UI: Glyph Kalender → Tab **Plan**.
- Schema: title, prompt, schedule daily|weekly (Europe/Berlin), paused, allow_write, optional `pass`, last_* (`ok` · `error` · `empty`).
- `pass` gesetzt: Prompt bekommt „Fertig nur wenn“; erste Zeile `LEER` → `last_status=empty` (Stamp ja, kein Fertig-Löschen). Ohne `pass` unverändert (HSEQ-Seeds).
- Ausführen: Freitext → tool_loop; allow_write → Auto-Confirm nur HSEQ-Pfade. Globaler Job-Lock.
- Scheduler: `POST /recurring/run-due` via `jobs-catchup.sh` (15 Min). **Zusätzlich einmal beim Agent-Start** (Hintergrund-Thread, ~2s Delay; Catch-up-Plist kann vor dem Agent laufen). Keine Plist pro To-do.
- Migration: 3 HSEQ-Seeds (`td-eingang` 18:00, `td-handover` 18:30, `td-lernen` Fr 19:00).
- Fertig: last_status=ok → UI „Fertig“ klickbar löscht To-do. Events → Systemzeile in Glyph-UI.
- ACP-Plan-Leiste bleibt getrennt (Session).

## Settled decisions (Aufgaben-Übergabe 2026-08-22)

- SoT: `~/.glyph/tasks.json` via `core/tasks.py` + HTTP `/tasks`.
- Zielkopf optional. Default leer. Köpfe: `grok` · `_code` · `glyph-agent` · `codex`. **Kein** Kopf `analysis`.
- Belege: Prompt, Antwort, kompakter Trace, Anhang-Metadaten (Pfad/Name) — keine Blobs, keine ganze Session, kein Session-Zeiger (°_Agent speichert keine Sessions).
- Anlegen braucht `pass` **und** Meldung+Antwort. PATCH `done` nur mit `artifact`. Plan **Erledigt** löscht den Eintrag (`DELETE /tasks/<id>`), kein Pfad. Chat-Belege sind Kontext, kein Abschluss. Alte Items ohne Paar laden weiter. ^_Code schließt über Tool **TaskFinish** (nicht WriteFile in den Workspace).
- Prompt fordert: keine Build-Änderung ohne Nutzerauftrag; Rückfragen als Status `needs_input`. Vor-Antwort nicht neu explorieren. Fertig = TaskFinish.
- Sichtbar im Glyph-Kalender Tab Plan; Übernehmen legt den Prompt in den Composer. Kein automatischer Kopfwechsel.

## Settled decisions (Second-Brain Gaps 2026-08-09)

- Pro-Vault-Vertrag: `HSEQ Sync/AGENTS.md`; Wiki: erweiterte `memory-wiki/AGENTS.md` (Ingest ≥1 Synthese-Seite).
- Skills: `vault-ingest`, `merken`, `einmal-job` (+ hseq-*); IDs Alias `hseq-*` = recurring `td-*`.
- einmal-job (2026-08-20): Wiederkehrendes 1× mit Plan→Ja, dann Recurring (Kalender → Plan). Irreversibel: Plan→Ja. Leben-Admin nicht in Vault. Kein Grok-Bot-Cloud. Recurring nicht per Hand in `recurring.json`. `pass` Pflicht für neue Jobs; ohne prüfbares Fertig kein Recurring.
- merken (2026-08-13, Wiki-Vorlage 2026-08-28): Schicht-Router — HSEQ-Themen / MEMORY §2 / Repo-CONTEXT / AGENTS nach Ja. Wiki-Ergebnis: Vorlage Aufgabe/Lösung/Datei/Suchbegriffe, Chat-Ja, Ablehnen ohne Suchwert, bestehende Seite zuerst. Keine neuen `summaries/` / `grok-sessions/`, keine Auto-Skills. Was nicht sofort ins Ziel darf → `~/.glyph/memory/pending-contract.md` (°_Agent darf genau diese eine Datei außerhalb der Vaults).
- Wiki-Hygiene (2026-08-28): `td-wiki-hygiene` verschiebt doppelt tote Wiki-Dateien (`summaries/`, `grok-sessions/`, `unsafe-local-*`, Source-Waisen) nach `_hygiene-trash/` (30 Tage). Chat löscht nicht. Report `reports/hygiene.md`, nicht pending. Regeln nicht nachts. Session-Schließen ohne Wiki-Dump.
- Handover: Pflicht-**3-Zeilen-Briefing** (Neu / Offen / Konflikt-Stale); Eingang+Log = Beleg, nicht Offen.
- Index: `_is_blocked` + Hygiene blocken heikle Privat-/Behörden-Pfadmuster (nicht alle `unsafe-local-*`).
- Privat bleibt außerhalb `VAULT_PATHS` (Red Line).

## Settled decisions (Fortschreiben 2026-08-26)

- Chat °_Agent: CreateNote/ApplyEdit ohne Popup unter `Themen/` und Wiki `concepts|entities|syntheses`. Neue Datei unter `sources/` ja; Apply auf sources/Eingang nein.
- Kein Delete-Tool im Chat. Leerer Create/Apply wird abgewiesen. Backup bleibt. Job-Korb: ADR 0002 Ausnahme `td-wiki-hygiene`.
- Nicht aus dem Chat: Vorlagen, Fertig, Hauptarchiv, Privat. Vorlagen nur Job `td-lernen`.
- ADR `docs/adr/0002-vault-grow-no-delete.md`.

## Settled decisions (Shared SoT alle Profile 2026-08-09)

- **Eine** Querschnitts-Wahrheit: `~/.glyph/AGENTS.md` — Grok, ^_Code, °_Agent.
- Skills gemeinsam: `~/.glyph/skills/` (Glyph-UI `skillRootsForProfile` für alle Profile).
- Grok: `~/.grok/rules/glyph-shared.md`. Agent/Code: Prompt-Injection der SoT-Datei.
- Nutzer muss Geklärtes nicht pro Profil neu erzählen.

## Settled decisions (zentrale Memory 2026-08-09)

- **Memory:** `~/.glyph/MEMORY.md` (kuratiert: Resümee + Lektionen aus Fehlern).
- Nicht unter OpenClaw; Stub + Backup unter `~/.openclaw/workspace/MEMORY.md*`.
- Tages-Archiv: `~/.glyph/memory/openclaw-dailies/` → alte OpenClaw-Dailies.
- Glyph übernimmt Operatives; OpenClaw Auslauf (Rest-Crons 03:00/06:00 bis Ersatz).

## Settled decisions (^_Code Workspaces Phase 1 — 2026-08-11)

- **Problem:** Allow + Fail — Code fragt freigeben, setzt aber nichts um.
- **Capability first** (Phase 1), Kabelsalat-UI Workspaces = Phase 2.
- SoT `~/.glyph/workspaces.json`; nur Profil **`^_Code`** (Grok unberührt).
- Defaults: glyph-ui + glyph-agent = `r+w`; openclaw-workspace = `r`.
- Write flüssig unter r+w; Popup nur elevated; hart-deny bleibt; Fail = hard stop + Banner. **Aufgehoben 2026-08-22.**

## Settled decisions (^_Code Loop — Grill 2026-08-13)

- **Auftrag** (früher in diesem Grill „Job“): Mehrdatei E2E (lesen → schreiben → Test → kurz sagen). „Job“ bleibt Recurring.
- **Verlauf:** echte `messages[]` an Direct/OpenRouter. Budget `CODE_MESSAGE_CHARS` (64k). Älteste Turns weg; **erstes User + letztes Tool-Ergebnis bleiben**. Kein Flatten, das neue Ergebnisse abschneidet.
- **Orient:** alle **r+w**-`CONTEXT.md` (Abschnitte Orient + System map) im System-Prompt. `r`-Roots eine Zeile. Kein Blind-Grep.
- **Denker:** Flash bleibt; kein Provider-Wechsel ohne neuen Beleg.
- **Runden:** `CODE_MAX_ROUNDS=32`. Timeout 180s.
- **SearchReplace 0×/N×:** zurück in die Loop. Hart tot nur Deny / 🔒 / Nutzer-Ablehnung / Shell-Timeout / Test-Fail.
- **Grep/ListDir/ReadFile 3× gleich:** nicht ausführen — „Map nutzen oder antworten“. ReadFile-Key **ohne** offset/limit (sonst Datei-Walk bis Schleifenschutz).
- **Nach Write/Replace:** fokussierte Tests im Grant (`test`); Ergebnis sichtbar; Fail → kein Commit. `npm run service:*` bleibt einzeln (Deploy/Elevated).
- **Glyph-Aufgabe (2026-09-08):** Handoff trägt `Id:`. Vor-Antwort = Beleg, nicht neu walken. Fertig = Tool **TaskFinish** (schreibt `~/.glyph/tasks/<id>.md`, PATCH `done`) — ohne Freigabe, ohne Workspace-Root. Chat allein schließt nicht.

## Settled decisions (^_Code Workspaces Phase 2 — 2026-08-11)

- Kabelsalat-UI analog Vaults: Buch → Tab **Workspaces**, Hub `^_Code`.
- API: `GET|POST /workspaces`, `PATCH|DELETE /workspaces/<id>` → Registry CRUD.
- UI-Proxy: glyph-ui `/api/workspaces` → Agent.
- Modes: `r` · `rw` · `private` (UI: gesperrt). Kein allgemeines `w`. Keine Pins, kein Obsidian-URI.
- Anbinden: existierendes Verzeichnis (Pfad oder Name unter `$HOME`).

## Settled decisions (^_Code Freigabe — 2026-08-22)

- Formel: Workspace-Recht + Freigabe = Aktion. `r+w` ist Capability.
- `permission_decision`: WriteFile/SearchReplace unter `r+w` → `requires_grant` (`once` \| `auftrag` \| `task`), nicht `allow`.
- Grant-Store neben `resume_token`: `grant_id`, `task_id`/`auftrag_id`, Workspace-Root, Pfadpräfixe, Aktionsklassen, Ablauf, Widerruf. Jede Tool-Aktion prüft den Scope erneut.
- Änderungssatz sammeln → ein Gesamt-Diff → eine Freigabe → transaktionaler Apply → Tests. Nächste Agentenidee = neuer Satz. Kein Auto-Commit.
- Task nur explizit schließen. Aktion außerhalb Pfade/Aktionsklassen → Grant greift nicht; Hinweis statt Themenklassifikation.
- Shell: Lesen (`git status`/`diff`, `ls`) unter r+w ohne Freigabe; Testen = Einmal oder Auftrag; lokale Änderung = Auftrag/Task; `npm install`/`npx` immer explizit; Netzwerk eigenes Popup; `git commit` immer Diff+Freigabe; push/deploy/remote immer einzeln. Whitelist = untere Grenze.
- Kein Session-Always, kein Immer, kein `w` im Kabelsalat. Hart-Deny unverändert.
- ADR `docs/adr/0001-task-scoped-grants.md`. UI: `glyph-ui/docs/adr/0003-task-scoped-grants.md`.
- Danach, nicht in diesem Schnitt: Plan vor Änderungen, Git-Worktree pro Task, isolierte Jobs, Audit-Verlauf.

## Settled decisions (Vault-Inventar 2026-08-08)

- **ListVaultDir** (Agent-Modus): Ordner im Vault listen (read-only). Für „was liegt im Eingang/Fertig?“ — Precheck bei Inventar-Fragen. Index-Pfade `/VaultName/…` (führender Slash, wie VaultFind) sind Vault-Präfix, kein Dateisystem-Root. Fehlertext nennt angebundene Vaults — nicht als „ungebunden“ lesen.
- **VaultFind** bleibt Inhaltssuche (Hybrid Embedding+Keyword); listet keine Ordner.
- **Ordner-Suche Preview** (`POST /vault/find`): zusätzlich Datei- und Ordner**namen** auf Disk (kein Index). Gleichnamige Ordner in mehreren Vaults beide zeigen; Tippfehler `ss`/`s`. Inventar-Fallback `.` (alle Vault-Roots) nur ohne konkreten Ordnernamen — sonst wird der Hauptarchiv-Root nicht den HSEQ-Ordner verdrängen.
- **search_vault**: Token-Suche + Dateiname/Pfad (nicht nur Fullstring im Body). Datum nur im Namen zählt.
- Inventar-Fragen mit Jahreszahlen im Namen lösen **kein** Web-Precheck aus (domain-lokal).
- **Ranking:** Primär-Vault (`VAULT_PATHS[0]`, HSEQ Sync) und `00 Arbeitsfluss/Eingang|Fertig` vor OpenClaw-Wiki-`sources/` / `unsafe-local-*`-Hash-Slugs. Body-Treffer-Cap, damit lange Archiv-Kopien Datums-Queries nicht dominieren. Nutzer trifft gültige Live-Dateien, nicht alte Source-Nummern.
- **Namens-Match Satzfrage:** ein markantes Dateiname-Token reicht (`kran`/`krane` → `016_Krane.pdf`). Nicht alle Query-Tokens. Kein Token-Levenshtein (`vorliegen` ≠ `Vorlagen`). Dateien vor Ordnern; Ordner namens Vorlagen kein Treffer, Dateien darin schon. Gewählte PDFs (Datei-Tap **und** Ordner-Pick): nach Listing die 1–2 Dateien ReadPdf, deren Name zur Frage passt — nicht Listenplatz, nicht die ersten beliebigen. Text in `results[].text`. Nie den Nutzer fragen, welche PDF zu öffnen.

## Settled decisions (OpenClaw-Tools 2026-08-07)

- **°_Agent**: Wiki-Aliase `WikiSearch`→VaultFind, `WikiGet`→ReadNote, `WikiApply`→ApplyEdit; `WikiStatus` liest agent-digest (read-only).
- `BrowseUrl` = TinyFish Extract mit Summary-Goal; `ReadPdf` liest Vault-PDFs; PDF-Ingest = ReadPdf → Claims → CreateNote/ApplyEdit Allowlist (nie PDF-Datei schreiben); Mail via himalaya; `MessageSend` via openclaw (write+confirm).
- Kein Shell im Agent-Modus. Externe CLIs graceful degrade.

## Settled decisions (Digitalisierungs-Kette 2026-10-09)

Anlass: Nutzer scannt Papier und vermutet unvollständige Indexierung. Messung (84.832 Seiten, `pdftotext` je Seite): 4,4 % Seiten ohne Textebene, **19,4 % der PDFs** mit Scan-Anteil — überwiegend Betriebsanweisungen, Auditberichte, Folien.

- **Seitenweise OCR-Kette** (`core/pdf_tools.extract_pdf_pages`): Textlayer je Seite, Seiten-Gate (< 40 Zeichen), OCR **nur** für Scan-Seiten, Merge. Gemischte PDFs (395 Stück) verloren vorher ihre Scan-Seiten, weil das Gate dokumentsweit saß.
- **OCR-Backends** (`ocr_backend()` / Env `OCR_BACKEND=auto|vision|lighton|off`): `lighton` = **LightOnOCR-3-0.8B via MLX**, lokal als LaunchAgent `com.lxndrhbrt.lighton-ocr` (`scripts/lighton_server.py`, Port 8790, OpenAI-kompatibel). Default-URL `http://127.0.0.1:8790/v1` (ohne `.env`). Timeout 180s, Client-Lock (MLX seriell). LightOn leer/Timeout → Vision-Fallback pro Seite. `build_pdf_index` cappt `--workers` bei lighton auf 1. `vision` = macOS-Helfer `ocr_pdf.swift` (Seitenauswahl als 4. Argument). Auto: lighton wenn Endpunkt offen, sonst vision.
- **Index-Formate:** PDF + **Bilder** (JPG/PNG/TIFF/HEIC, `core/image_ocr.py`, sips-Normalisierung) + **docx/xlsx** (`core/office_text.py`, ZIP+XML ohne pip-Abhängigkeit). Alle landen im PDF-Vektorindex (`build_pdf_index.py`, Schalter `--no-images`/`--no-office`).
- **Index-Korrekturen:** keine 40k-Zeichen-Kappe mehr beim Ingest (dicke Regelwerke waren beschnitten); `files`-Tabelle hält `pages`/`ocr_pages`; `--reocr` (unterbrechbar: Dateien mit Engine `lighton` bleiben aus), `--filelist`.
- **Coverage-Audit** (`scripts/pdf_coverage.py`): fehlend/stale/rescan je PDF, md-Lücken; `--write-filelist` speist `--filelist`.
- Bestand 2026-10-09: 2.938 PDFs → 2.396 ok, **541 rescan**, 1 missing; md 340/344. Backlog-Neuläufe: `build_pdf_index.py --filelist <liste> --reocr`.

### Auswahl-Kopie aus iCloud (2026-10-10/11)

- Nutzerentscheidung: 01 alle · 02 gelistet (DSGVO) · 03 gelistet · 04 alle · 05 ohne persönliche Unterlagen · 06 ohne Begehungsberichte, BERATER-Namen bereinigt, Unterweisungen DSGVO · **Weiterbildung als eigene Kategorie** (ohne Gruppenarbeiten/Lernthemen/Termine/Übersicht Themen/E-Mail Nachverfolgung/Prüfungsfragen) · top-Vorlagen nein.
- **Kopie strukturtreu** nach `Arbeitssicherheit/<Quellpfad>` (macOS NFD-Fallstrick: Vergleiche immer über `unicodedata.normalize('NFC', …)`).
- **DSGVO-Redaktion an der Kopie** (`core/dsgvo.py`): Anrede+Namen (auch über XML-Runs verteilt) und Firmentoken (BERATERgroup → Beratung) in docx/xlsx/pptx/pptm; Originale in iCloud unangetastet. `.ppt` (Binär) und PDFs nicht in-file redigierbar → **Index-Redaktion** (`build_pdf_index._redact_chunks`) hält den Token aus Suche/Treffer, Inhalte bleiben.
- **BERATER.group-Ordner** kopiert als `Präsi/Unterweisungen extern/`, Präfixe aus Dateinamen entfernt.
- `office_text.read_pptx` + OFFICE_EXTS um `.pptx/.pptm` erweitert (Folie-für-Folie).
- Bekannt: `06/Präsi/Führen von Fahrzeugen.pptx` korrupt (2 Versuche, übersprungen); `01/…/Statistik_Unternehmen.pptx` und 2 Bender-docx ohne Text (status empty) — Kandidaten für Bild-OCR nachträglich.

## Settled decisions (Vault-Inbox 2026-10-10)

OCR füllt den **Index** (Suche). Der **Vault** ist das Hirn: Dateien müssen als Kopie + Wikilink dort liegen.

- Job `td-inbox-sync` (`scripts/vault_inbox_sync.py`, täglich 07:00): Quellen iCloud `Documents/Scanordner`, `~/Downloads`, iCloud `Documents/01 Beruf`. Nur kopieren, nie löschen.
- Gemischte Drops: Hash-Abgleich gegen ASI+Privat, dann Klassifikation `hseq` → ASI + `Themen/00 Eingang-Dateien.md`; `privat` → Privat-Vault `00 Eingang/` (kein Index); `intern` (Bewerbung/Zeugnisse/Stunden/Kunden/Datenbanken) → `ASI/Beruf-intern/` (Index-Skip); Passwörter-Ordner → skip.
- Unbekannt (Scan/Downloads) → Privat, nicht in Glyph-Suche.
- Index neuer HSEQ-Kopien: Nightly `pdf-index-nightly` (inkrementell). Kein Extra-Status-Log.
