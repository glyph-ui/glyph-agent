# Auftrag: PDF-Ingest für °_Agent (vault-ingest)

Du bist Grok Build auf Alexanders Mac. Eigenes CLI (`--no-leader`) — Desk-Grok nicht kapern.
Repo: /Users/lxndrhbrt/glyph-agent (schon auf Disk, NICHT clonen). Kein Commit, kein Push.

## Ziel (Akzeptanz)

Nutzer sagt „ingest diese PDF“ / Skill `vault-ingest` auf eine Vault-PDF → °_Agent:
1. Text via bestehendem **ReadPdf** / pdftotext extrahieren
2. Claims + Links extrahieren (3–12 Claims)
3. Strukturiertes Wissen schreiben **nur** in erlaubte Ziele laut PROMPT-Vault-Ingest
4. Antwort exakt 3 Zeilen: Neu / Verlinkt / Konflikt (oder —)

Heute: ReadPdf existiert; Systemprompt + AGENTS sagen „Kein PDF-Ingest/Schreiben“; Skill zeigt auf PROMPT, PDF-Pfad schwach/fehlt.

## Erlaubt schreiben (Allowlist)
- Vaults: `HSEQ Sync`; `ASI, BS. UWS, QM, EM`; `memory-wiki` (Wiki-Ingest laut Prompt)
- Ziele: HSEQ `Themen/…`; memory-wiki `sources/` (neu, immutable danach) + `concepts|entities|syntheses`
- Bestehende CreateNote/ApplyEdit + vault_write_policy nutzen

## Verbote (hard)
- Nie Privat / Peniel / _RECOVERY / hard-deny
- Keine PDF-Dateien erzeugen oder PDF-Bytes schreiben
- Kein Massen-Rewrite; max 1 Source + bis 3 kurze Seiten
- Kein Commit/Push

## Bestehende Artefakte (lesen, nicht neu erfinden)
- core/pdf_tools.py — ReadPdf via pdftotext
- core/tool_loop.py ~Z.308–310: „Kein PDF-Ingest/Schreiben.“
- core/tool_registry.py ReadPdf-Beschreibung: „Kein Ingest, kein Schreiben.“
- ~/.glyph/AGENTS.md ~Z.62: „Kein PDF-Ingest/Schreiben durch Agent.“
- ~/.glyph-agent/skills/vault-ingest/SKILL.md (Symlink von ~/.glyph/skills/vault-ingest)
- /Users/lxndrhbrt/ObsidianVaults/HSEQ Sync/Vorlagen/Jobs/PROMPT-Vault-Ingest.md
- core/vault_write_policy.py — CreateNote/ApplyEdit Prefixes

## Kleinste Änderung
1. **Prompt/Vertrag klarstellen:** „Kein PDF schreiben/erzeugen“ bleibt. „PDF-Ingest“ = PDF lesen → Claims → Markdown in Allowlist **erlaubt** (ersetzt Blanket-Ban).
2. **tool_loop** Systemrolle: Ban ersetzen durch safe allowlisted Ingest-Hinweis (Skill vault-ingest / „ingest … PDF“; ReadPdf → CreateNote/ApplyEdit nur Allowlist; Privat nie).
3. **tool_registry** ReadPdf-Beschreibung: Lesen erlaubt; Ingest = Folge-Schreiben via Grow-Tools laut Policy, nicht „nie Ingest“.
4. **SKILL.md vault-ingest:** PDF-Pfad explizit (ReadPdf → Claims → Schreiben laut PROMPT; Chat-Trigger inkl. „ingest diese PDF“).
5. **PROMPT-Vault-Ingest.md:** kurze PDF-Zeile (Quelle=PDF im Vault → ReadPdf; Source-Pfad notieren, PDF nicht umschreiben).
6. **~/.glyph/AGENTS.md:** gleiche Klarstellung (lesen ok; PDF-Datei schreiben verboten; Ingest aus PDF in Allowlist ok).
7. Optional kleiner Helper nur wenn nötig — Prefer Prompt+Skill+Policy, kein neuer großer Tool-Stack.
8. Falls Python geändert: `launchctl kickstart -k gui/$(id -u)/com.lxndrhbrt.glyph-agent`
9. Kurzer Test wenn sinnvoll (Policy/Prompt-String oder ReadPdf smoke) — kein E2E-Vault-Müll.

## Nicht tun
- Desk-Grok / leader.sock hijacken
- Home-Vault unbinden, Privat anfassen
- git commit/push
- brew/Poppler-Drama (pdftotext liegt schon)

## Fertig-Report (stdout, deutsch, kurz)
- Was fehlte
- Dateien geändert
- Trigger (Skill/Chat)
- Live? (LaunchAgent restart ja/nein) + Tests
- Explizit: kein Commit
- Eine CEO-Zeile
