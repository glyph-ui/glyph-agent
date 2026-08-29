# -*- coding: utf-8 -*-
"""
CODE-Modus-Loop (^_Code / C′).

Direct deepseek-v4-flash-vision-exp → OpenRouter Flash-0731 + Code-Tools
(Read/Write/List/Run). Text und Screenshots dasselbe Modell — kein Auto-Hop.
Kein VaultFind, kein Web-Precheck.
Write/Shell unter r+w brauchen Freigabe (Einmal / Auftrag / Task).
Elevated Shell: immer Einmal. Grant-Store neben resume_token.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time

from . import config, llm, log, tool_registry
from . import code_tools
from . import code_grants

MAX_ROUNDS = getattr(config, "CODE_MAX_ROUNDS", 32) or 32

# Resume-State für Genehmigungen (In-Memory, Prozess-lokal)
_PENDING_LOCK = threading.Lock()
_PENDING = {}  # token -> state dict
_PENDING_TTL_S = 15 * 60


def code_denker_label():
    """Lesbares Denker-Label aus dem konfigurierten CODE_OPENROUTER_MODEL.
    Nicht hart verdrahtet — folgt der tatsächlichen Konfig (dynamisch)."""
    raw = str(getattr(config, "CODE_OPENROUTER_MODEL", "") or "").strip()
    base = raw.rsplit("/", 1)[-1].lower()
    base = base.split(":")[0]
    if "gemini" in base:
        return "Google Gemini Flash"
    if "vision" in base and "deepseek" in base:
        return "DeepSeek V4 Vision"
    if "deepseek" in base or base.startswith("deepseek-") or base.startswith("v4"):
        return "DeepSeek V4 Flash"
    if raw:
        # z.B. openai/gpt-4o → GPT-4o; anthropic/claude → Claude …
        label = base.split("-")[0]
        return (label or raw).title()
    return "DeepSeek V4 Flash"


def code_role():
    """System-Prompt — live aus Config (Hot-Apply), nicht beim Import eingefroren."""
    model = getattr(config, "CODE_OPENROUTER_MODEL", "deepseek-v4-flash-vision-exp")
    fallback = getattr(config, "CODE_OPENROUTER_FALLBACK_MODEL", None) or ""
    denker = code_denker_label()
    route = "OpenRouter" if "/" in str(model) else "Direct"
    fb = str(fallback).strip()
    fb_txt = f", Fallback {fb}" if fb else ""
    return (
        "Du bist ^_Code: ein Code-Agent in Glyph. Denker: "
        f"{denker} ({model}) {route}{fb_txt}. "
        "Kein Tiny/Free.\n"
        "Du arbeitest NUR in Workspace-Roots aus `~/.glyph/workspaces.json` "
        "(Modes: r = lesen, r+w = beschreibbar — Apply nur mit Freigabe, "
        "🔒 private = tot).\n"
        "Werkzeuge: ListDir, ReadFile (offset/limit), Grep, SearchReplace (exakt 1 Treffer), "
        "WriteFile (Diff+Backup), RunCommand.\n"
        "Regeln:\n"
        "- Antworte auf Deutsch; knapper Stil, präzise und handlungsorientiert.\n"
        "- STOP_SLOP: Kern zuerst, aktiv, konkret. Keine Floskeln "
        "(Gerne, Absolut, Zusammenfassend…, Es ist wichtig zu beachten, Als KI…, "
        "I hope this helps, Let’s dive in). Keine erfundenen Normen/Facts.\n"
        "- Schreib-/Shell-Regeln (Stagen, Freigabe Einmal/Auftrag/Task, Shell-Whitelist, "
        "Elevated, Hart verboten, Build-Pflicht): siehe SHARED SoT §Code unten — nicht "
        "doppelt hier.\n"
        "- Bevorzuge EINEN Befehl pro RunCommand; Compound nur wenn nötig (dann Freigabe).\n"
        "- WAHRHEIT: Datei-Änderungen/Shell-Erfolge NUR behaupten, wenn das Tool-Ergebnis "
        "ok=true war. Ohne erfolgreichen Write/Replace/Command: ehrlich sagen, was fehlt "
        "(Fehler, Ablehnung, Timeout) — nie „erledigt“ erfinden.\n"
        "- Kein Vault, kein Obsidian, keine privaten Pfade außerhalb der Roots.\n"
        f"- Bei Modell-Fragen: nenne {denker} und den konfigurierten Denker, Profil ^_Code.\n"
        "- SHARED SoT: `~/.glyph/AGENTS.md` §Code gilt (Jobs/Vaults/Skills/Red Line) — "
        "nicht im Chat neu verhandeln, was dort und in Repo-CONTEXT geklärt ist.\n"
        "- EINMAL-JOB: Wiederkehrendes 1× Plan→Ja, dann Recurring (Kalender→Plan). "
        "Nicht recurring.json per Hand. Irreversibel: Plan→Ja. "
        "Leben-Admin nicht in Vault. Skill `einmal-job`.\n"
        "- KORREKTUR: Chat vs. AGENTS/CONTEXT → Konflikt nennen, Vertrag gewinnt. "
        "Vorschlag nach ~/.glyph/memory/pending-contract.md, nicht nur in den Chat. "
        "AGENTS/MEMORY nur nach Auftrag. Repo-Verhalten → CONTEXT.md.\n"
        "- ORIENT: Orient+System map der r+w-Workspaces liegen im System-Prompt (Runde 1). "
        "Kein Blind-Walk (ListDir/Grep über den ganzen Tree). Gezielt die genannten Quellen. "
        "Map veraltet → nach Struktur-Change dort nachziehen, nicht parallele Doku erfinden.\n"
        "- GROSSE DATEIEN (z. B. App.jsx >100k Zeichen): NICHT komplett in vielen "
        "ReadFile-offset-Häppchen lesen. Nimm zuerst Grep gezielt nach Symbol/Funktion; "
        "lies nur konkrete Stellen mit ReadFile + großem limit. Keine Endlos-Read-Loops "
        "über die ganze Datei. Prinzip: gezielt statt vollständig, außer der volle Inhalt "
        "ist wirklich nötig.\n"
    )


def _shared_contract_snippet(max_chars=3200):
    """Gemeinsame Wahrheit für ^_Code — nur Code-relevante Teile.

    - pending-contract (aktuelle offene Punkte)
    - AGENTS.md: NUR der '## Code (^_Code)'-Abschnitt (Rest: Grok/HSEQ/OpenClaw
      ist für Code-Aufgaben irrelevant)
    - MEMORY.md wird hier NICHT geladen (Projekt-Historie/HSEQ-Lektionen sind
      für Datei-/Shell-Aufgaben nicht nötig; spart ~1600 Z pro Call)
    """
    import os as _os
    import re as _re

    parts = []
    try:
        from . import vault_tools as _vt

        pending = _vt.pending_contract_prompt_block(max_body=800)
    except Exception:
        pending = None
    if pending:
        parts.append(pending)

    agents_path = _os.path.expanduser("~/.glyph/AGENTS.md")
    try:
        with open(agents_path, encoding="utf-8", errors="replace") as f:
            body = f.read().strip()
    except OSError:
        body = ""
    if body:
        # Nur '## Code (^_Code)' bis zur nächsten '## '-Überschrift
        m = _re.search(r"^## Code\b.*?(?=^## |\Z)", body, _re.MULTILINE | _re.DOTALL)
        if m:
            code_section = m.group(0).strip()
        else:
            # Fallback: erste 2000 Zeichen (wie vorher)
            code_section = body[:2000].strip()
        if code_section:
            if len(code_section) > 2400:
                code_section = code_section[:2380] + "\n…[gekürzt]"
            parts.append(f"### SHARED SoT · AGENTS.md (§Code)\n{code_section}")

    if not parts:
        return ""
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        text = text[: max_chars - 20] + "\n…[gekürzt]"
    return (
        "\n\nSHARED (~/.glyph — befolgen, nicht neu verhandeln):\n" + text
    )


def _purge_stale_pending():
    now = time.time()
    with _PENDING_LOCK:
        dead = [k for k, v in _PENDING.items() if now - v.get("ts", 0) > _PENDING_TTL_S]
        for k in dead:
            _PENDING.pop(k, None)


def _save_pending(state):
    _purge_stale_pending()
    token = secrets.token_hex(12)
    state = dict(state)
    state["ts"] = time.time()
    with _PENDING_LOCK:
        _PENDING[token] = state
    return token


def _peek_pending(token):
    """Liefert State ohne zu konsumieren (Resume darf Token mehrfach nutzen)."""
    with _PENDING_LOCK:
        state = _PENDING.get(token)
        if not state:
            return None
        if time.time() - state.get("ts", 0) > _PENDING_TTL_S:
            _PENDING.pop(token, None)
            return None
        return dict(state)


def _drop_pending(token):
    """Entfernt Token nach erfolgreichem Resume."""
    with _PENDING_LOCK:
        _PENDING.pop(token, None)


def _pop_pending(token):
    with _PENDING_LOCK:
        return _PENDING.pop(token, None)


def _content_len(content):
    if content is None:
        return 0
    if isinstance(content, list):
        n = 0
        for p in content:
            if isinstance(p, dict):
                n += len(str(p.get("text") or ""))
        return n
    return len(str(content))


def _messages_chars(messages):
    return sum(_content_len(m.get("content")) for m in messages or [])


def extract_orient_map(md, max_chars=2800):
    """## Orient + ## System map bis zur nächsten anderen ##-Überschrift."""
    if not md:
        return ""
    lines = str(md).splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith("## ") and "orient" in line.lower():
            start = i
            break
    if start is None:
        return ""
    out = []
    seen_map = False
    for i in range(start, len(lines)):
        line = lines[i]
        if i > start and line.startswith("## "):
            title = line[3:].strip().lower()
            if title.startswith("system map") or title.startswith("system-map"):
                seen_map = True
            elif seen_map or not title.startswith("orient"):
                break
        out.append(line)
    text = "\n".join(out).strip()
    if max_chars > 0 and len(text) > max_chars:
        text = text[: max_chars - 20] + "\n…[gekürzt]"
    return text


def workspace_orient_block():
    """r+w: Orient+Map. r: eine Zeile. 🔒 aus."""
    try:
        from . import workspaces_registry as wr
        items = wr.list_workspaces(include_missing=False)
    except Exception:
        return ""
    parts = []
    for w in items:
        if not w.get("enabled"):
            continue
        mode = w.get("mode") or "r"
        if mode == "private":
            continue
        name = w.get("name") or w.get("id") or "?"
        path = w.get("path") or ""
        if mode != "rw":
            parts.append(f"- {name} ({path}) — mode r, CONTEXT nicht injiziert.")
            continue
        ctx = os.path.join(path, "CONTEXT.md") if path else ""
        body = ""
        if ctx and os.path.isfile(ctx):
            try:
                with open(ctx, encoding="utf-8", errors="replace") as f:
                    body = extract_orient_map(f.read())
            except OSError:
                body = ""
        if body:
            parts.append(f"### {name} ({path}) r+w\n{body}")
        else:
            parts.append(f"- {name} ({path}) — r+w, kein CONTEXT.md Orient/Map.")
    if not parts:
        return ""
    return (
        "\n\nWORKSPACE-ORIENT (injiziert — nicht den Tree walken):\n"
        + "\n\n".join(parts)
    )


def _tool_result_summary(tool_name, result):
    """Kurzer History-Eintrag statt vollem JSON-Dump, damit der Verlauf schlank bleibt.
    Schwergewichtige Erfolge (ReadFile/SearchReplace/WriteFile) werden zusammengefasst;
    Fehler und kompakte Treffer (Grep/ListDir) bleiben voll, da kurz und wichtig."""
    ok = bool(result and result.get("ok"))
    err = str((result or {}).get("error") or "").strip()
    if not ok:
        # Fehler voll behalten (Selbstkorrektur braucht den Kontext)
        return (
            f"Tool-Ergebnis für '{tool_name}':\n"
            f"{json.dumps(result, ensure_ascii=False, default=str)}\n\n"
            "Wähle das nächste Tool (JSON) oder antworte auf Deutsch."
        )
    if tool_name in ("ReadFile",):
        body = (result.get("result") or {})
        if isinstance(body, dict):
            text = body.get("content") or body.get("text") or ""
            chars = body.get("chars") or len(text)
            head = str(text)[:65536]
            more = "" if len(str(text)) <= 65536 else "…"
            return (
                f"Tool-Ergebnis für 'ReadFile' ({body.get('path') or ''}, ~{chars} Zeichen):\n"
                f"```\n{head}{more}\n```\n"
                "(Gekürzt. Bei Bedarf mit ReadFile + offset gezielt weiterlesen.)\n"
                "Wähle das nächste Tool (JSON) oder antworte auf Deutsch."
            )
    if tool_name in ("SearchReplace", "WriteFile"):
        body = (result.get("result") or {})
        if isinstance(body, dict):
            summary = (
                f"Schreiben OK → {body.get('path') or '?'} "
                f"({body.get('old_chars') or 0}→{body.get('new_chars') or 0} Zeichen)"
            )
            return (
                f"Tool-Ergebnis für '{tool_name}':\n{summary}\n\n"
                "Wähle das nächste Tool (JSON) oder antworte auf Deutsch."
            )
    # Standard: bleibt wie bisher
    return (
        f"Tool-Ergebnis für '{tool_name}':\n"
        f"{json.dumps(result, ensure_ascii=False, default=str)}\n\n"
        "Wähle das nächste Tool (JSON) oder antworte auf Deutsch."
    )


def trim_code_history(history, budget=None, max_turns=None):
    """Älteste Turns weg. Erstes User + letztes Tool-Ergebnis bleiben.

    Kappt die Runden-Flut (Gemini verliert in >~20 hochfragmentären Turns den
    Fokus und liefert leer/stirbt). Entweder nach Zeichen-Budget (budget) ODER
    nach Message-Anzahl (max_turns) — beide halten den Kontext schlank.
    """
    history = list(history or [])
    if not history:
        return history
    # Message-Obergrenze: älteste Runden weg, erste User + letzte Ergebnisse bleiben
    if max_turns and len(history) > max_turns:
        first = history[0]
        last = history[-1]
        keep_head = max(max_turns // 2, 1)
        keep_tail = max_turns - keep_head
        head = history[1 : min(len(history) - 1, keep_head)]
        tail = history[max(len(history) - keep_tail, len(head) + 1) : len(history) - 1]
        merged = [first] + head + tail + [last]
        # Keine zwei gleichen Rollen an der Naht (OpenAI-kompatibel, sonst 400):
        # wenn head und tail mit derselben Rolle aneinandergrenzen, ersten des
        # konfligierenden Blocks droppen (Tool-Ergebnis bleibt; Duplikat weg).
        out = []
        for m in merged:
            if out and out[-1].get("role") == m.get("role") and m.get("role") != "system":
                # gleiche Rolle direkt hintereinander: den NEUEN droppen (behält
                # den früheren Kontext inkl. evtl. zugehörigem Tool-Ergebnis)
                continue
            out.append(m)
        # Dedupe Überschneidungen (gleicher Inhalt), Reihenfolge wahren, exakt kappen
        seen = set()
        dedup = []
        for m in out:
            k = (m.get("role"), str(m.get("content"))[:80])
            if k in seen:
                continue
            seen.add(k)
            dedup.append(m)
        history = dedup[-max_turns:]
    if not budget or budget <= 0:
        return history
    if _messages_chars(history) <= budget:
        return history
    first = history[0]
    last = history[-1]
    if len(history) == 1:
        return history
    middle = history[1:-1]
    kept = [first] + middle + [last]
    while len(kept) > 2 and _messages_chars(kept) > budget:
        # ältesten Turn nach dem ersten User droppen
        kept.pop(1)
        if len(kept) > 2 and kept[1].get("role") == "assistant":
            # dangling assistant ohne vorherigen user-tool: ok, nächste Runde
            pass
    if _messages_chars(kept) > budget and len(kept) > 2:
        kept = [first, last]
    return kept


def repeat_tool_key(tool_name, args):
    args = args or {}
    if tool_name == "Grep":
        return (
            "Grep",
            str(args.get("pattern") or ""),
            str(args.get("path") or "."),
            bool(args.get("case_insensitive")),
        )
    if tool_name == "ReadFile":
        return (
            "ReadFile",
            str(args.get("path") or ""),
            str(args.get("offset") or ""),
            str(args.get("limit") or ""),
        )
    if tool_name == "ListDir":
        return (
            "ListDir",
            str(args.get("path") or "."),
            bool(args.get("recursive")),
        )
    return None


def is_retryable_write_fail(tool_name, result):
    err = str((result or {}).get("error") or "")
    if tool_name != "SearchReplace":
        return False
    return (
        "0 Treffer" in err
        or "old-String nicht gefunden" in err
        or ("kommt" in err and "Treffer" in err)
    )


def should_hard_stop(tool_name, result):
    """Hart tot: Deny/🔒/Nutzer-Ablehnung/Shell-Timeout. SearchReplace-Treffer nicht."""
    err = str((result or {}).get("error") or "")
    payload = (result or {}).get("result")
    if not isinstance(payload, dict):
        payload = {}
    low = err.lower()
    if "vom nutzer" in low and "abgelehnt" in low:
        return True
    if "private" in low or "🔒" in err:
        return True
    if tool_name == "RunCommand" and payload.get("timeout"):
        return True
    if is_retryable_write_fail(tool_name, result):
        return False
    return False


def pick_test_command(root):
    import os as _os
    if not root or not _os.path.isdir(root):
        return None
    pkg = _os.path.join(root, "package.json")
    if _os.path.isfile(pkg):
        try:
            with open(pkg, encoding="utf-8") as fh:
                scripts = (json.load(fh) or {}).get("scripts") or {}
        except (OSError, ValueError):
            scripts = {}
        # `npm test` in glyph-ui = unit + smoke (spawnt Server). Nach Write nur Unit.
        if isinstance(scripts, dict) and scripts.get("test:unit"):
            return "npm run test:unit"
        return "npm test"
    if (
        _os.path.isfile(_os.path.join(root, "pytest.ini"))
        or _os.path.isfile(_os.path.join(root, "pyproject.toml"))
        or _os.path.isdir(_os.path.join(root, "tests"))
    ):
        return "pytest"
    return None


def run_workspace_tests(roots, _emit=None):
    """Whitelist-Test je beschriebenem Root. Kein service:*."""
    import os as _os
    seen = set()
    for root in roots or []:
        try:
            real = _os.path.realpath(root)
        except OSError:
            continue
        if real in seen or not _os.path.isdir(real):
            continue
        seen.add(real)
        cmd = pick_test_command(real)
        if not cmd:
            continue
        label = _os.path.basename(real.rstrip(_os.sep)) or real
        if _emit:
            _emit({
                "type": "step",
                "action": "RunCommand",
                "status": "start",
                "detail": f"Test {cmd} · {label}",
            })
        try:
            res = code_tools.run_command(cmd, cwd=real, timeout=120, allow_elevated=False)
        except Exception as e:
            if _emit:
                _emit({
                    "type": "step",
                    "action": "RunCommand",
                    "status": "error",
                    "detail": str(e)[:200],
                })
            return {
                "ok": False,
                "error": f"{cmd} in {label}: {e}",
                "root": real,
                "command": cmd,
            }
        if res.get("timeout") or int(res.get("exit_code") or 0) != 0:
            tail = (res.get("stderr") or res.get("stdout") or "Test fehlgeschlagen")[:400]
            if _emit:
                _emit({
                    "type": "step",
                    "action": "RunCommand",
                    "status": "error",
                    "detail": tail[:200],
                })
            return {
                "ok": False,
                "error": f"{cmd} in {label} exit {res.get('exit_code')}: {tail}",
                "root": real,
                "command": cmd,
            }
        if _emit:
            _emit({
                "type": "step",
                "action": "RunCommand",
                "status": "done",
                "detail": f"{cmd} ok · {label}",
            })
    return {"ok": True}


def _call_code_llm(messages, temperature=0.2, images=None):
    """CODE-Denker: echte messages[], Trim ältester Turns, letztes Tool bleibt.

    Primär immer CODE_OPENROUTER_MODEL (Default: Direct vision-exp).
    images: optionale OpenAI image_url-Parts. Flash-0731/Pro ohne Vision → 400,
    daher bei Bildern kein Fallback auf ein Nicht-Vision-Modell.
    """
    provider = llm.get_provider("code")
    primary = (
        getattr(config, "CODE_OPENROUTER_MODEL", None)
        or "deepseek-v4-flash-vision-exp"
    )
    fallback = getattr(config, "CODE_OPENROUTER_FALLBACK_MODEL", None)
    if images:
        # KEIN Fallback auf Nicht-Vision-Modelle (Flash-0731): 400
        # „This model does not support image“. Lieber klarer Fehler.
        fallback = None
    old_model = getattr(provider, "model", None)
    old_fb = getattr(provider, "fallback_model", None)
    try:
        provider.model = primary
        provider.fallback_model = fallback if fallback else None
        payload = []
        for m in messages or []:
            payload.append({
                "role": m.get("role") or "user",
                "content": m.get("content") or "",
            })
        if not payload:
            payload = [{"role": "system", "content": code_role()}]
        # Google/Gemini lehnt Requests ab, die mit einer Modell-Nachricht enden
        # ("Requests ending with a model turn are not supported", HTTP 400).
        # Nach „Ja“-Confirm / Resume war Geminis letzte Antwort die letzte Message →
        # jetzt eine leere User-Nachricht anhängen, damit der Request gültig endet.
        if payload and payload[-1].get("role") == "assistant":
            payload.append({"role": "user", "content": "Fortfahren."})
        if payload[0].get("role") == "system" and images:
            payload[0]["content"] = (
                str(payload[0].get("content") or "")
                + "\nDu kannst angehängte Screenshots/Bilder SEHEN (Vision), "
                "falls das Modell es unterstützt. Nutze sie für UI-Bugs und Layout."
            )
        budget = int(getattr(config, "CODE_MESSAGE_CHARS", 64000) or 0)
        max_turns = int(getattr(config, "CODE_MAX_TURNS", 18) or 0)
        if budget > 0 or max_turns > 0:
            sys_msgs = [m for m in payload if m.get("role") == "system"]
            hist = [m for m in payload if m.get("role") != "system"]
            reserved = _messages_chars(sys_msgs)
            hist_budget = max(4000, budget - reserved) if budget > 0 else 0
            hist = trim_code_history(hist, hist_budget, max_turns=max_turns)
            payload = sys_msgs + hist
        if images:
            from .providers.openrouter import user_content_with_images
            for m in payload:
                if m.get("role") == "user":
                    text = m.get("content") or ""
                    if isinstance(text, list):
                        text = "\n".join(
                            str(p.get("text") or "")
                            for p in text
                            if isinstance(p, dict) and p.get("type") == "text"
                        )
                    m["content"] = user_content_with_images(text, images)
                    break
        # Wasserbuchter Guard: Google/Gemini lehnt 400 ab, wenn die letzte Message
        # eine Modell-Nachricht (assistant) ist („Requests ending with a model turn").
        # Nach dem Trim/Bilder-Transform kann die letzte Rolle umkippen -> hier final
        # sicherstellen, dass sie mit einer User-Nachricht endet.
        if payload and str(payload[-1].get("role") or "").lower() == "assistant":
            payload.append({"role": "user", "content": "Fortfahren."})
        chat_timeout = int(getattr(config, "CODE_CHAT_TIMEOUT", 180) or 180)
        # Leerer-Antwort / Timeout-Retry: Gemini liefert gelegentlich einen leeren
        # Content (intermittierend). Statt sofort hart zu scheitern (oder still auf
        # ein anderes Modell zu fallen), wiederhole denselben Call bis zu CODE_RETRIES×
        # mit kurzem Backoff. Das Modell bleibt unverändert — die Wahl übernimmt kein
        # fremder Denker.
        retries = int(getattr(config, "CODE_RETRIES", 2) or 0)
        text = ""
        last_err = None
        for attempt in range(retries + 1):
            try:
                if hasattr(provider, "chat_messages"):
                    text = provider.chat_messages(
                        payload, temperature=temperature, timeout=chat_timeout
                    )
                else:
                    # Ältere Provider: letzter User + System (kein Flatten-Cap am Schwanz)
                    system = "\n\n".join(
                        str(m.get("content") or "")
                        for m in payload
                        if m.get("role") == "system"
                    )
                    last_user = ""
                    for m in reversed(payload):
                        if m.get("role") == "user":
                            last_user = m.get("content") or ""
                            break
                    text = provider.chat(
                        system, last_user, temperature=temperature, timeout=chat_timeout
                    )
                if text and text.strip():
                    break
                last_err = RuntimeError(
                    f"Modell '{primary}' lieferte eine leere Antwort (kein content)"
                )
            except Exception as e:
                last_err = e
                text = ""
            if attempt < retries:
                import time as _time
                _time.sleep(min(0.5 * (attempt + 1), 2.0))
        if not (text or "").strip():
            raise (last_err or RuntimeError("Code-Denker lieferte keine Antwort"))
        return text
    finally:
        provider.model = old_model
        provider.fallback_model = old_fb


def os_environ_int(name, default):
    import os
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _build_trace(tool_calls, tool_results=None, steps=None, model=None):
    try:
        provider = llm.get_provider("code")
        pname = getattr(provider, "provider_name", "openrouter")
        mname = model or getattr(provider, "_active_model", None) or getattr(
            config, "CODE_OPENROUTER_MODEL", "deepseek-v4-flash-vision-exp"
        )
    except Exception:
        pname, mname = "openrouter", model or "?"
    result_by_tool = {}
    for tr in tool_results or []:
        result_by_tool[tr.get("tool")] = tr.get("result") or {}
    meta = []
    for tc in tool_calls or []:
        result = result_by_tool.get(tc.get("tool")) or {}
        payload = result.get("result")
        rlen = 0
        if payload is not None:
            try:
                rlen = len(str(payload))
            except Exception:
                rlen = 0
        meta.append({
            "tool": tc.get("tool"),
            "status": "success" if tc.get("ok") else "error",
            "result_length": rlen,
            "error": result.get("error"),
        })
    return {
        "provider": pname,
        "model": mname,
        "mode": "code",
        "fallback_used": False,
        "tool_calls": meta,
        "steps": list(steps or []),
        "request_id": "local-code",
        "workspace_roots": list(code_tools.workspace_roots() or []),
    }


def _tool_schema():
    return tool_registry.tool_schema_prompt(mode="code")


def _is_write_tool(name):
    t = tool_registry.tool_map(mode="code").get(name)
    return bool(t and t.get("write"))


def _auto_confirm(_tool_name, _args):
    """Bereits durch Policy freigegeben — execute-Callback."""
    return True


def recover_answer_from_tools(err, tool_calls):
    """Nach erfolgreichen Tools: leere Denker-Antwort ist kein Totalschaden.

    Gemini 3.x liefert oft HTTP 200 ohne content, nachdem Write/Replace schon
    gelaufen sind. Der Turn darf dann nicht als „nichts passiert“ enden.
    """
    done = [tc for tc in (tool_calls or []) if tc.get("ok")]
    if not done:
        return None
    lines = []
    for tc in done:
        name = tc.get("tool") or "?"
        args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
        target = (
            args.get("path")
            or args.get("command")
            or args.get("cmd")
            or args.get("pattern")
            or ""
        )
        if target:
            lines.append(f"- {name}: `{target}`")
        else:
            lines.append(f"- {name}")
    return (
        "Arbeit liegt auf Disk; der Denker lieferte danach keine Abschluss-Antwort.\n"
        f"Grund: {err}\n\n"
        "Erledigt:\n"
        + "\n".join(lines)
        + "\n\nDateien/Befehle oben gelten — Kette nicht verworfen."
    )


def _hard_fail_answer(tool_name, result, args=None):
    err = (result or {}).get("error") or "unbekannter Fehler"
    extra = ""
    # Bei abgelehnten Shell-Befehlen den konkreten Befehl sichtbar machen,
    # damit der Abbruch nachvollziehbar ist statt stumm zu scheitern.
    if tool_name == "RunCommand" and isinstance(args, dict):
        cmd = args.get("command") or args.get("cmd") or ""
        cwd = args.get("cwd") or "."
        if cmd:
            extra = f"\n\nAbgelehnter Befehl:\n```\n{cmd}\n```\nin: `{cwd}`"
    return (
        f"**Abbruch:** `{tool_name}` fehlgeschlagen.\n\n"
        f"{err}{extra}\n\n"
        "Kette gestoppt — bitte korrigieren und erneut senden."
    )


_FILE_TOOLS = ("WriteFile", "SearchReplace")


def _changeset_preview(staged):
    parts = []
    for item in staged or []:
        parts.append(
            code_tools.preview_for_confirm(item.get("tool"), item.get("args") or {})
        )
    return "\n\n——\n\n".join(parts)[:8000]


def _apply_staged(staged, _emit):
    applied = []
    roots = set()
    for item in staged or []:
        tool_name = item.get("tool")
        args = item.get("args") or {}
        _emit({
            "type": "step",
            "action": tool_name,
            "status": "start",
            "detail": "Änderungssatz anwenden",
        })
        result = tool_registry.execute(
            tool_name,
            args,
            confirm=_auto_confirm,
            mode="code",
            allow_elevated=False,
        )
        ok = bool(result.get("ok"))
        err = (result.get("error") or "")[:200] or None
        _emit({
            "type": "step",
            "action": tool_name,
            "status": "done" if ok else "error",
            "detail": err,
        })
        if not ok:
            return False, applied, result, roots
        applied.append({"tool": tool_name, "args": args, "result": result})
        payload = result.get("result") if isinstance(result.get("result"), dict) else {}
        root = payload.get("root") or item.get("root")
        if root:
            roots.add(root)
    return True, applied, None, roots


def _suggested_grant(staged, decision, user_message):
    rels = [s.get("rel") for s in (staged or []) if s.get("rel")]
    if not rels and decision:
        rel = decision.get("rel_path")
        if rel:
            rels = [rel]
    root = ""
    if staged:
        root = staged[0].get("root") or ""
    if not root and decision:
        root = decision.get("workspace_root") or ""
    prefixes = code_grants.common_prefixes(rels)
    action_class = (decision or {}).get("action_class") or "file_change"
    classes = list(code_grants.DEFAULT_TASK_CLASSES)
    if action_class not in classes and action_class not in code_grants.ALWAYS_ONCE:
        classes.append(action_class)
    label = ""
    if user_message:
        label = str(user_message).strip().split("\n", 1)[0][:80]
    return {
        "workspace_root": root,
        "path_prefixes": prefixes,
        "action_classes": classes,
        "label": label,
    }


def _pause_grant(
    *,
    history,
    system,
    tool_calls,
    tool_results,
    steps,
    rounds,
    user_message,
    model_name,
    images,
    _emit,
    staged,
    after_tool,
    after_args,
    decision,
    auftrag_id,
    system_lean=None,
):
    suggested = _suggested_grant(staged, decision, user_message)
    preview = _changeset_preview(staged)
    if not preview and after_tool:
        preview = (decision or {}).get("preview") or code_tools.preview_for_confirm(
            after_tool, after_args or {}
        )
    hint = (decision or {}).get("hint") or ""
    if hint:
        preview = hint + "\n\n" + (preview or "")
    risk = (decision or {}).get("risk") or hint or "Freigabe für Änderungssatz"
    pending_tool = after_tool if not staged else "ChangeSet"
    pending_args = after_args if not staged else {
        "files": [s.get("rel") or (s.get("args") or {}).get("path") for s in staged],
    }
    history.append({
        "role": "assistant",
        "content": f"Freigabe nötig ({pending_tool}).",
    })
    token = _save_pending({
        "history": history,
        "system": system,
        "system_lean": system_lean,
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "steps": steps,
        "rounds": rounds,
        "user_message": user_message,
        "pending_tool": pending_tool,
        "pending_args": pending_args,
        "model_name": model_name,
        "preview": preview,
        "elevated": False,
        "risk": risk,
        "images": images,
        "staged": list(staged or []),
        "after_tool": after_tool,
        "after_args": after_args,
        "requires_grant": True,
        "action_class": (decision or {}).get("action_class") or "file_change",
        "outside_task": bool((decision or {}).get("outside_task")),
        "hint": hint,
        "suggested": suggested,
        "auftrag_id": auftrag_id,
        "workspace_root": suggested.get("workspace_root"),
    })
    _emit({
        "type": "pending_confirmation",
        "tool": pending_tool,
        "args": pending_args,
        "preview": preview,
        "elevated": False,
        "risk": risk,
        "resume_token": token,
        "requires_grant": True,
        "outside_task": bool((decision or {}).get("outside_task")),
        "hint": hint,
        "suggested": suggested,
        "grant_scopes": ["once", "auftrag", "task"],
        "active_grant": code_grants.public_snapshot().get("active_task"),
    })
    steps.append({
        "step": pending_tool,
        "status": "pending",
        "detail": (risk or "wartet auf Freigabe")[:80],
    })
    log.log(
        "code_pending_grant",
        tool=pending_tool,
        token=token[:8],
        staged=len(staged or []),
        outside_task=bool((decision or {}).get("outside_task")),
    )
    status_answer = (
        f"Freigabe nötig für **{pending_tool}**"
        + (f" — {risk}" if risk else "")
        + f".\n\n```\n{(preview or '')[:2000]}\n```\n\n"
        "Einmal · Auftrag · Task — oder ablehnen."
    )
    _emit({"type": "answer", "status": "content", "text": status_answer})
    return {
        "ok": True,
        "answer": status_answer,
        "rounds": rounds,
        "tool_calls": tool_calls,
        "pending_confirmation": True,
        "resume_token": token,
        "requires_grant": True,
        "pending": {
            "tool": pending_tool,
            "args": pending_args,
            "preview": preview,
            "elevated": False,
            "risk": risk,
            "requires_grant": True,
            "outside_task": bool((decision or {}).get("outside_task")),
            "hint": hint,
            "suggested": suggested,
            "grant_scopes": ["once", "auftrag", "task"],
            "staged_count": len(staged or []),
        },
        "active_grant": code_grants.public_snapshot().get("active_task"),
        "trace": _build_trace(
            tool_calls, tool_results, steps=steps, model=model_name
        ),
    }


def run_code(
    user_message,
    confirm=None,
    max_rounds=None,
    on_event=None,
    resume_token=None,
    allow_pending=None,
    images=None,
    conversation_history=None,
    grant_scope=None,
    grant_spec=None,
):
    """
    CODE-Tool-Loop.

    confirm: Callback(tool_name, args) -> bool  (Whitelist-Freigaben im selben Request)
    resume_token + allow_pending: Fortsetzen nach Glyph-Genehmigung
      allow_pending True  = freigegeben ausführen
      allow_pending False = abgelehnt, Modell informieren
    grant_scope: once | auftrag | task (nur Resume)
    grant_spec: optionale Scope-Edits (Pfade, Aktionsklassen, Label)
    images: optionale OpenAI image_url-Parts (Vision / Screenshots)
    conversation_history: optionale prior Turns für Multi-Turn-Nachfragen
    """
    def _emit(event):
        if on_event is None:
            return
        try:
            on_event(event)
        except Exception:
            pass

    max_rounds = int(max_rounds or MAX_ROUNDS)
    images = list(images or [])

    # --- Resume nach Genehmigung ---
    if resume_token:
        state = _peek_pending(resume_token)
        if not state:
            return {
                "ok": False,
                "answer": "Genehmigungs-Token ungültig oder abgelaufen. Bitte Anfrage neu senden.",
                "rounds": 0,
                "tool_calls": [],
                "pending_confirmation": False,
                "trace": _build_trace([], steps=[{"step": "resume", "status": "error"}]),
            }
        result = _continue_from_state(
            state, allow_pending=bool(allow_pending), confirm=confirm,
            max_rounds=max_rounds, on_event=on_event, _emit=_emit,
            grant_scope=grant_scope, grant_spec=grant_spec,
        )
        # Token erst bei Erfolg konsumieren; bei Fehler (falscher Scope/allow,
        # Ausnahme) zurücklegen, damit die UI es erneut senden kann.
        if result.get("ok") and not result.get("pending_confirmation"):
            _drop_pending(resume_token)
        return result

    from . import history as chat_history

    prior_history, history = chat_history.build_history_for_loop(
        user_message, conversation_history
    )
    if prior_history:
        try:
            from . import log as _log
            _log.log(
                "code_history",
                prior_msgs=len(prior_history),
                prior_chars=sum(len(m["content"]) for m in prior_history),
            )
        except Exception:
            pass

    orient_full = workspace_orient_block()
    base = (
        code_role()
        + _shared_contract_snippet()
        + "\n\n"
        + _tool_schema()
        + "\n\nWICHTIG: Wenn du ein Werkzeug brauchst, antworte NUR mit JSON "
        '{"tool": Name, "args": {...}}. Kein Text drumherum. '
        "Wenn KEIN Werkzeug nötig ist, antworte normal auf Deutsch."
        + (
            "\nMulti-Turn: Chat-Verlauf liegt vor. Bei Nachfragen darauf aufbauen "
            "(Dateipfade, Vereinbarungen, vorherige Schritte) — nicht von null starten."
            if prior_history
            else ""
        )
    )
    # Runde 1: volle Orient-Map (Blind-Walk vermeiden). Folge-Runden: ohne Map,
    # damit der System-Prompt schlank bleibt (spart ~5.9k Zeichen pro Call).
    # Resume/pending: Orient wurde schon gesehen → lean.
    system = orient_full + "\n\n" + base if orient_full else base
    system_lean = base
    tool_calls = []
    tool_results = []
    steps = []
    rounds = 0
    model_name = getattr(config, "CODE_OPENROUTER_MODEL", "deepseek-v4-flash-vision-exp")
    if prior_history:
        steps.append({
            "step": "history",
            "status": "success",
            "detail": f"{len(prior_history)} prior msg(s)",
        })
    if images:
        steps.append({"step": "Vision", "status": "success", "detail": f"{len(images)} Bild(er)"})
        _emit({
            "type": "step",
            "action": "Vision",
            "status": "start",
            "detail": f"{len(images)} Bild(er) — braucht Vision-fähiges CODE-Modell",
        })

    auftrag_id = code_grants.begin_auftrag()
    return _loop(
        system=system,
        system_lean=system_lean,
        history=history,
        tool_calls=tool_calls,
        tool_results=tool_results,
        steps=steps,
        rounds=rounds,
        user_message=user_message,
        confirm=confirm,
        max_rounds=max_rounds,
        _emit=_emit,
        model_name=model_name,
        images=images,
        staged=[],
        auftrag_id=auftrag_id,
    )


def _continue_from_state(
    state, allow_pending, confirm, max_rounds, on_event, _emit,
    grant_scope=None, grant_spec=None,
):
    tool_name = state["pending_tool"]
    args = state["pending_args"]
    history = state["history"]
    system = state["system"]
    system_lean = state.get("system_lean")
    tool_calls = state["tool_calls"]
    tool_results = state["tool_results"]
    steps = state["steps"]
    rounds = state["rounds"]
    user_message = state["user_message"]
    model_name = state.get("model_name") or getattr(
        config, "CODE_OPENROUTER_MODEL", "deepseek-v4-flash-vision-exp"
    )
    elevated = bool(state.get("elevated"))
    staged = list(state.get("staged") or [])
    after_tool = state.get("after_tool")
    after_args = state.get("after_args")
    auftrag_id = state.get("auftrag_id")
    written_roots = set()

    _emit({"type": "step", "action": tool_name, "status": "start",
           "detail": "nach Genehmigung" if allow_pending else "abgelehnt"})

    if not allow_pending:
        result = {
            "ok": False,
            "result": None,
            "error": "Vom Nutzer in Glyph abgelehnt.",
        }
        history.append({
            "role": "user",
            "content": _tool_result_summary(tool_name, result),
        })
        tool_calls.append({"tool": tool_name, "args": args, "ok": False})
        tool_results.append({"tool": tool_name, "args": args, "result": result})
        if should_hard_stop(tool_name, result):
            answer = _hard_fail_answer(tool_name, result)
            _emit({"type": "answer", "status": "content", "text": answer})
            return {
                "ok": False,
                "answer": answer,
                "rounds": rounds,
                "tool_calls": tool_calls,
                "pending_confirmation": False,
                "hard_error": True,
                "error": result.get("error"),
                "trace": _build_trace(
                    tool_calls, tool_results, steps=steps, model=model_name
                ),
            }
        return _loop(
            system=system,
            system_lean=system_lean,
            history=history,
            tool_calls=tool_calls,
            tool_results=tool_results,
            steps=steps,
            rounds=rounds,
            user_message=user_message,
            confirm=confirm,
            max_rounds=max_rounds,
            _emit=_emit,
            model_name=model_name,
            images=list(state.get("images") or []),
            staged=[],
            auftrag_id=auftrag_id,
            written_roots=written_roots,
        )

    scope = str(grant_scope or "once").strip().lower()
    if scope not in ("once", "auftrag", "task"):
        scope = "once"
    spec = dict(grant_spec or {})
    suggested = dict(state.get("suggested") or {})
    root = spec.get("workspace_root") or suggested.get("workspace_root") or state.get("workspace_root")
    prefixes = spec.get("path_prefixes") or suggested.get("path_prefixes") or ["."]
    classes = spec.get("action_classes") or suggested.get("action_classes") or list(
        code_grants.DEFAULT_TASK_CLASSES
    )
    label = spec.get("label") or suggested.get("label") or ""
    if scope in ("auftrag", "task"):
        code_grants.issue(
            scope,
            workspace_root=root,
            path_prefixes=prefixes,
            action_classes=classes,
            label=label,
            auftrag_id=auftrag_id,
            user_message=user_message,
        )

    if staged:
        ok, applied, fail, roots = _apply_staged(staged, _emit)
        written_roots.update(roots)
        summary = {
            "ok": ok,
            "result": {
                "applied": ok,
                "files": [
                    (a.get("args") or {}).get("path") for a in applied
                ],
                "count": len(applied),
            },
            "error": None if ok else (fail or {}).get("error"),
        }
        history.append({
            "role": "user",
            "content": _tool_result_summary("ChangeSet", summary),
        })
        tool_calls.append({"tool": "ChangeSet", "args": {"files": summary["result"]["files"]}, "ok": ok})
        tool_results.append({"tool": "ChangeSet", "args": {}, "result": summary})
        if not ok:
            answer = _hard_fail_answer("ChangeSet", fail or summary)
            _emit({"type": "answer", "status": "content", "text": answer})
            return {
                "ok": False,
                "answer": answer,
                "rounds": rounds,
                "tool_calls": tool_calls,
                "pending_confirmation": False,
                "hard_error": True,
                "error": summary.get("error"),
                "trace": _build_trace(
                    tool_calls, tool_results, steps=steps, model=model_name
                ),
            }
        after_tool = after_tool if after_tool not in _FILE_TOOLS and after_tool != "ChangeSet" else None
        staged = []
        if not after_tool:
            return _loop(
                system=system,
            system_lean=system_lean,
                history=history,
                tool_calls=tool_calls,
                tool_results=tool_results,
                steps=steps,
                rounds=rounds,
                user_message=user_message,
                confirm=confirm,
                max_rounds=max_rounds,
                _emit=_emit,
                model_name=model_name,
                images=list(state.get("images") or []),
                staged=[],
                auftrag_id=auftrag_id,
                written_roots=written_roots,
            )
        nxt = code_tools.permission_decision(after_tool, after_args or {})
        nxt_action = nxt.get("action") or "deny"
        if nxt_action == "deny":
            result = {
                "ok": False,
                "result": None,
                "error": nxt.get("reason") or "abgelehnt",
            }
            history.append({
                "role": "user",
                "content": _tool_result_summary(after_tool, result),
            })
            if _is_write_tool(after_tool):
                answer = _hard_fail_answer(after_tool, result, after_args)
                _emit({"type": "answer", "status": "content", "text": answer})
                return {
                    "ok": False,
                    "answer": answer,
                    "rounds": rounds,
                    "tool_calls": tool_calls,
                    "pending_confirmation": False,
                    "hard_error": True,
                    "error": result.get("error"),
                    "trace": _build_trace(
                        tool_calls, tool_results, steps=steps, model=model_name
                    ),
                }
            return _loop(
                system=system,
            system_lean=system_lean,
                history=history,
                tool_calls=tool_calls,
                tool_results=tool_results,
                steps=steps,
                rounds=rounds,
                user_message=user_message,
                confirm=confirm,
                max_rounds=max_rounds,
                _emit=_emit,
                model_name=model_name,
                images=list(state.get("images") or []),
                staged=[],
                auftrag_id=auftrag_id,
                written_roots=written_roots,
            )
        if nxt_action in ("requires_grant", "confirm"):
            return _pause_grant(
                history=history,
                system=system,
            system_lean=system_lean,
                tool_calls=tool_calls,
                tool_results=tool_results,
                steps=steps,
                rounds=rounds,
                user_message=user_message,
                model_name=model_name,
                images=list(state.get("images") or []),
                _emit=_emit,
                staged=[],
                after_tool=after_tool,
                after_args=after_args or {},
                decision=nxt,
                auftrag_id=auftrag_id,
            )
        tool_name = after_tool
        args = after_args or {}
        elevated = bool(nxt.get("elevated"))

    def _confirm_once(tn, a):
        if tn == tool_name and a == args:
            return True
        if confirm is not None:
            return confirm(tn, a)
        return False

    result = tool_registry.execute(
        tool_name,
        args,
        confirm=_confirm_once,
        mode="code",
        allow_elevated=elevated,
    )
    err_detail = (result.get("error") or "")[:200] or None
    _emit({
        "type": "step",
        "action": tool_name,
        "status": "done" if result.get("ok") else "error",
        "detail": err_detail,
    })
    tool_calls.append({"tool": tool_name, "args": args, "ok": result.get("ok")})
    tool_results.append({"tool": tool_name, "args": args, "result": result})
    steps.append({
        "step": tool_name,
        "status": "success" if result.get("ok") else "error",
        "detail": err_detail,
    })
    log.log(
        "code_tool_resume",
        tool=tool_name,
        ok=result.get("ok"),
        allowed=True,
        elevated=elevated,
        scope=scope,
        error=(result.get("error") or "")[:120] or None,
    )

    if not result.get("ok") and should_hard_stop(tool_name, result):
        answer = _hard_fail_answer(tool_name, result)
        _emit({"type": "answer", "status": "content", "text": answer})
        return {
            "ok": False,
            "answer": answer,
            "rounds": rounds,
            "tool_calls": tool_calls,
            "pending_confirmation": False,
            "hard_error": True,
            "error": result.get("error") or f"{tool_name} fehlgeschlagen",
            "trace": _build_trace(tool_calls, tool_results, steps=steps, model=model_name),
        }

    payload = result.get("result") if isinstance(result.get("result"), dict) else {}
    if result.get("ok") and payload.get("root"):
        written_roots.add(payload["root"])

    history.append({
        "role": "user",
        "content": _tool_result_summary(tool_name, result),
    })

    return _loop(
        system=system,
            system_lean=system_lean,
        history=history,
        tool_calls=tool_calls,
        tool_results=tool_results,
        steps=steps,
        rounds=rounds,
        user_message=user_message,
        confirm=confirm,
        max_rounds=max_rounds,
        _emit=_emit,
        model_name=model_name,
        images=list(state.get("images") or []),
        staged=[],
        auftrag_id=auftrag_id,
        written_roots=written_roots,
    )


def _loop(
    system,
    history,
    tool_calls,
    tool_results,
    steps,
    rounds,
    user_message,
    confirm,
    max_rounds,
    _emit,
    model_name,
    images=None,
    staged=None,
    auftrag_id=None,
    written_roots=None,
    system_lean=None,
):
    images = list(images or [])
    staged = list(staged or [])
    written_roots = set(written_roots or [])
    repeat_counts = {}

    def _finish(answer, *, ok=True, hard=False, error=None):
        code_grants.end_auftrag(auftrag_id)
        snap = code_grants.public_snapshot()
        if written_roots:
            tres = run_workspace_tests(list(written_roots), _emit=_emit)
            if not tres.get("ok"):
                fail = _hard_fail_answer(
                    "RunCommand",
                    {"error": tres.get("error") or "Test fehlgeschlagen"},
                )
                _emit({"type": "answer", "status": "content", "text": fail})
                steps.append({
                    "step": "RunCommand",
                    "status": "error",
                    "detail": (tres.get("error") or "")[:200],
                })
                return {
                    "ok": False,
                    "answer": fail,
                    "rounds": rounds,
                    "tool_calls": tool_calls,
                    "pending_confirmation": False,
                    "hard_error": True,
                    "error": tres.get("error"),
                    "active_grant": snap.get("active_task"),
                    "trace": _build_trace(
                        tool_calls, tool_results, steps=steps, model=model_name
                    ),
                }
        if answer:
            _emit({"type": "answer", "status": "content", "text": answer})
        return {
            "ok": ok,
            "answer": answer or "",
            "rounds": rounds,
            "tool_calls": tool_calls,
            "pending_confirmation": False,
            "hard_error": bool(hard),
            "error": error,
            "active_grant": snap.get("active_task"),
            "trace": _build_trace(
                tool_calls, tool_results, steps=steps, model=model_name
            ),
        }

    while rounds < max_rounds:
        rounds += 1
        # Runde 1: voller System-Prompt (mit Orient-Map).
        # Folge-Runden: lean (ohne Map) — Orient wurde schon gesehen, spart ~5.9k Z.
        sys_prompt = system if rounds == 1 else (system_lean or system)
        messages = [{"role": "system", "content": sys_prompt}] + history
        _emit({
            "type": "step",
            "action": "OpenRouter",
            "status": "start",
            "detail": llm.thinker_step_detail("code", model=model_name),
        })
        try:
            reply = _call_code_llm(messages, images=images or None)
        except Exception as e:
            _emit({"type": "step", "action": "OpenRouter", "status": "error", "detail": str(e)[:80]})
            recovered = recover_answer_from_tools(e, tool_calls)
            if recovered:
                steps.append({
                    "step": "OpenRouter",
                    "status": "error",
                    "detail": str(e)[:120],
                })
                try:
                    log.log(
                        "code_thinker_recover",
                        rounds=rounds,
                        err=str(e)[:160],
                        tools=len(tool_calls),
                    )
                except Exception:
                    pass
                return _finish(recovered, ok=True)
            err_answer = f"CODE-Denker fehlgeschlagen: {e}"
            _emit({"type": "answer", "status": "content", "text": err_answer})
            return {
                "ok": False,
                "answer": err_answer,
                "rounds": rounds,
                "tool_calls": tool_calls,
                "pending_confirmation": False,
                "trace": _build_trace(tool_calls, tool_results, steps=steps, model=model_name),
            }
        _emit({"type": "step", "action": "OpenRouter", "status": "done", "detail": None})

        parsed = tool_registry.try_parse_tool_call(reply)
        if parsed is None:
            if staged:
                return _pause_grant(
                    history=history,
                    system=system,
                    tool_calls=tool_calls,
                    tool_results=tool_results,
                    steps=steps,
                    rounds=rounds,
                    user_message=user_message,
                    model_name=model_name,
                    images=images,
                    _emit=_emit,
                    staged=staged,
                    after_tool=None,
                    after_args=None,
                    decision={"action_class": "file_change", "hint": ""},
                    auftrag_id=auftrag_id,
                )
            # Nur echte Nutzerantworten streamen — nie Tool-JSON/DSML als Answer.
            if tool_registry.looks_like_dsml(reply):
                prose = tool_registry.prose_before_dsml(reply)
                answer = prose or (
                    "Denker hat einen Tool-Call im DSML-Format geschickt, "
                    "der sich nicht lesen ließ. Bitte die Anfrage nochmal senden."
                )
            else:
                answer = (reply or "").strip()
            steps.append({"step": "answer", "status": "success", "detail": f"{len(answer)} Zeichen"})
            log.log("code_reply", rounds=rounds, direct=True, chars=len(answer))
            try:
                p = llm.get_provider("code")
                active = getattr(p, "_active_model", None) or model_name
            except Exception:
                active = model_name
            model_name = active
            return _finish(answer, ok=True)

        tool_name, args = parsed
        args = args or {}
        _emit({"type": "step", "action": tool_name, "status": "start", "detail": None})

        rkey = repeat_tool_key(tool_name, args)
        if rkey is not None:
            repeat_counts[rkey] = repeat_counts.get(rkey, 0) + 1
            if repeat_counts[rkey] >= 3:
                result = {
                    "ok": False,
                    "result": None,
                    "error": (
                        "Gleicher Grep/ListDir/ReadFile zum 3. Mal. "
                        "CONTEXT-Map nutzen oder auf Deutsch antworten — nicht weiter walken."
                    ),
                }
                err_detail = result["error"][:200]
                _emit({
                    "type": "step",
                    "action": tool_name,
                    "status": "error",
                    "detail": err_detail,
                })
                tool_calls.append({"tool": tool_name, "args": args, "ok": False})
                tool_results.append({"tool": tool_name, "args": args, "result": result})
                steps.append({
                    "step": tool_name,
                    "status": "error",
                    "detail": err_detail,
                })
                history.append({"role": "assistant", "content": reply})
                history.append({
                    "role": "user",
                    "content": _tool_result_summary(tool_name, result),
                })
                log.log(
                    "code_tool", tool=tool_name, rounds=rounds, ok=False, repeat=True,
                    path=str(args.get("path") or "")[:80] or None,
                    pattern=str(args.get("pattern") or "")[:60] or None,
                )
                continue

        if staged and tool_name not in _FILE_TOOLS:
            return _pause_grant(
                history=history + [{"role": "assistant", "content": reply}],
                system=system,
                tool_calls=tool_calls,
                tool_results=tool_results,
                steps=steps,
                rounds=rounds,
                user_message=user_message,
                model_name=model_name,
                images=images,
                _emit=_emit,
                staged=staged,
                after_tool=tool_name,
                after_args=args,
                decision={"action_class": "file_change", "hint": ""},
                auftrag_id=auftrag_id,
            )

        # Policy: allow | confirm (elevated) | requires_grant | deny
        decision = code_tools.permission_decision(tool_name, args)
        action = decision.get("action") or "deny"

        if action == "deny":
            result = {
                "ok": False,
                "result": None,
                "error": decision.get("reason") or "abgelehnt",
            }
            err_detail = (result.get("error") or "")[:200]
            _emit({
                "type": "step",
                "action": tool_name,
                "status": "error",
                "detail": err_detail,
            })
            tool_calls.append({"tool": tool_name, "args": args, "ok": False})
            tool_results.append({"tool": tool_name, "args": args, "result": result})
            steps.append({
                "step": tool_name,
                "status": "error",
                "detail": err_detail,
            })
            log.log("code_tool", tool=tool_name, rounds=rounds, ok=False, denied=True)
            # Write/Shell-Deny: hard stop; Read-Tools: Modell darf korrigieren
            if _is_write_tool(tool_name):
                answer = _hard_fail_answer(tool_name, result, args)
                _emit({"type": "answer", "status": "content", "text": answer})
                return {
                    "ok": False,
                    "answer": answer,
                    "rounds": rounds,
                    "tool_calls": tool_calls,
                    "pending_confirmation": False,
                    "hard_error": True,
                    "error": result.get("error"),
                    "trace": _build_trace(
                        tool_calls, tool_results, steps=steps, model=model_name
                    ),
                }
            history.append({"role": "assistant", "content": reply})
            history.append({
                "role": "user",
                "content": _tool_result_summary(tool_name, result),
            })
            continue

        if action == "requires_grant":
            allowed = False
            if confirm is not None:
                try:
                    allowed = bool(confirm(tool_name, args))
                except Exception:
                    allowed = False
            if not allowed and tool_name in _FILE_TOOLS:
                staged.append({
                    "tool": tool_name,
                    "args": args,
                    "root": decision.get("workspace_root") or "",
                    "rel": decision.get("rel_path") or args.get("path") or "",
                })
                result = {
                    "ok": True,
                    "result": {
                        "staged": True,
                        "applied": False,
                        "path": decision.get("rel_path") or args.get("path"),
                        "message": (
                            "Änderung vorgemerkt, nicht auf Disk. "
                            "Weitere Dateien stagen; Apply nach Freigabe des Änderungssatzes."
                        ),
                    },
                }
                _emit({
                    "type": "step",
                    "action": tool_name,
                    "status": "done",
                    "detail": "vorgemerkt",
                })
                tool_calls.append({"tool": tool_name, "args": args, "ok": True})
                tool_results.append({"tool": tool_name, "args": args, "result": result})
                steps.append({
                    "step": tool_name,
                    "status": "success",
                    "detail": "vorgemerkt",
                })
                history.append({"role": "assistant", "content": reply})
                history.append({
                    "role": "user",
                    "content": _tool_result_summary(tool_name, result),
                })
                log.log(
                    "code_tool_staged",
                    tool=tool_name,
                    path=str(args.get("path") or "")[:80] or None,
                    staged=len(staged),
                )
                continue
            if not allowed:
                history.append({"role": "assistant", "content": reply})
                return _pause_grant(
                    history=history,
                    system=system,
                    tool_calls=tool_calls,
                    tool_results=tool_results,
                    steps=steps,
                    rounds=rounds,
                    user_message=user_message,
                    model_name=model_name,
                    images=images,
                    _emit=_emit,
                    staged=staged,
                    after_tool=tool_name,
                    after_args=args,
                    decision=decision,
                    auftrag_id=auftrag_id,
                )

        if action == "confirm":
            # Optional: Payload-confirm-Liste (API) kann sofort erlauben
            allowed = False
            if confirm is not None:
                try:
                    allowed = bool(confirm(tool_name, args))
                except Exception:
                    allowed = False
            if not allowed:
                preview = decision.get("preview") or code_tools.preview_for_confirm(
                    tool_name, args
                )
                risk = decision.get("risk") or decision.get("reason") or ""
                history.append({"role": "assistant", "content": reply})
                token = _save_pending({
                    "history": history,
                    "system": system,
                    "tool_calls": tool_calls,
                    "tool_results": tool_results,
                    "steps": steps,
                    "rounds": rounds,
                    "user_message": user_message,
                    "pending_tool": tool_name,
                    "pending_args": args,
                    "model_name": model_name,
                    "preview": preview,
                    "elevated": bool(decision.get("elevated")),
                    "risk": risk,
                    "images": images,
                })
                _emit({
                    "type": "pending_confirmation",
                    "tool": tool_name,
                    "args": args,
                    "preview": preview,
                    "elevated": bool(decision.get("elevated")),
                    "risk": risk,
                    "resume_token": token,
                })
                steps.append({
                    "step": tool_name,
                    "status": "pending",
                    "detail": (risk or "wartet auf Glyph-Genehmigung")[:80],
                })
                log.log(
                    "code_pending",
                    tool=tool_name,
                    token=token[:8],
                    elevated=bool(decision.get("elevated")),
                )
                # Primärspur = Status (Vertrauen: kein stilles Warten ohne Text).
                status_answer = (
                    f"Freigabe nötig für **{tool_name}**"
                    + (f" — {risk}" if risk else "")
                    + f".\n\n```\n{preview[:2000]}\n```\n\n"
                    "Bitte in Glyph erlauben oder ablehnen."
                )
                _emit({"type": "answer", "status": "content", "text": status_answer})
                return {
                    "ok": True,
                    "answer": status_answer,
                    "rounds": rounds,
                    "tool_calls": tool_calls,
                    "pending_confirmation": True,
                    "resume_token": token,
                    "pending": {
                        "tool": tool_name,
                        "args": args,
                        "preview": preview,
                        "elevated": bool(decision.get("elevated")),
                        "risk": risk,
                    },
                    "trace": _build_trace(
                        tool_calls, tool_results, steps=steps, model=model_name
                    ),
                }

        # action == allow (oder confirm + payload-confirm)
        result = tool_registry.execute(
            tool_name,
            args,
            confirm=_auto_confirm,
            mode="code",
            allow_elevated=bool(decision.get("elevated")),
        )
        err_detail = (result.get("error") or "")[:200] or None
        why = (decision.get("allowed_by") or "") if result.get("ok") else ""
        _emit({
            "type": "step",
            "action": tool_name,
            "status": "done" if result.get("ok") else "error",
            "detail": err_detail or (f"erlaubt: {why}" if why else None),
        })
        tool_calls.append({"tool": tool_name, "args": args, "ok": result.get("ok")})
        tool_results.append({"tool": tool_name, "args": args, "result": result})
        steps.append({
            "step": tool_name,
            "status": "success" if result.get("ok") else "error",
            "detail": err_detail,
        })
        history.append({"role": "assistant", "content": reply})
        history.append({
            "role": "user",
            "content": _tool_result_summary(tool_name, result),
        })
        log.log(
            "code_tool",
            tool=tool_name,
            rounds=rounds,
            ok=result.get("ok"),
            error=(result.get("error") or "")[:120] or None,
            path=str(args.get("path") or "")[:80] or None,
            pattern=str(args.get("pattern") or "")[:60] or None,
            cmd=str(args.get("command") or args.get("cwd") or "")[:60] or None,
        )

        if result.get("ok") and tool_name in ("WriteFile", "SearchReplace"):
            payload = result.get("result") if isinstance(result.get("result"), dict) else {}
            root = payload.get("root")
            if root:
                written_roots.add(root)

        if should_hard_stop(tool_name, result):
            answer = _hard_fail_answer(tool_name, result)
            return _finish(answer, ok=False, hard=True, error=result.get("error"))

    steps.append({"step": "answer", "status": "error", "detail": "Runden-Limit"})
    limit_msg = "Zu viele Tool-Runden im CODE-Modus — gestoppt (Schleifenschutz)."
    return _finish(limit_msg, ok=False, error="Runden-Limit")
