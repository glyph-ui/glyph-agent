#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
glyph-agent HTTP-Server — lokale Agenten- und Tool-Schicht als Dienst.

Bietet POST /chat an, damit der Glyph-ACP-Adapter (oder ein anderes Frontend)
die Tool-Orchestrierung nutzen kann, ohne die Agentenlogik selbst zu tragen.

  POST /chat   { "message": "...", "confirm": null, "history": [{role, content}, ...] }
               -> {"answer": str, "rounds": int, "tool_calls": [...], "ok": bool}
  history: optional prior Turns (user/assistant) für Multi-Turn-Nachfragen.

  GET  /jobs        -> Legacy-Liste (seed-Namen); neu: /recurring
  POST /jobs/run    -> Legacy; neu: POST /recurring/<id>/run

  GET    /recurring              Liste wiederkehrender To-dos (+ Migration)
  POST   /recurring              Anlegen {title,prompt,schedule,allow_write,paused,pass?}
  PATCH  /recurring/<id>         Update
  DELETE /recurring/<id>         Löschen
  DELETE /tasks/<id>             Aufgabe aus dem Plan entfernen
  POST   /recurring/<id>/run     {force?: true} einmal / fällig
  POST   /recurring/<id>/pause   {paused: true|false}
  POST   /recurring/run-due      Scheduler: alle fälligen
  GET    /recurring/events?after= ISO-Events für UI-Systemzeilen

  GET  /health -> {"status": "ok", "provider": "...", "model": "..."}

  GET    /vaults              Kabelsalat-Snapshot (~/.glyph/vaults.json)
  POST   /vaults              Anbinden {input, mode?, head?}
  PATCH  /vaults/<id>         mode|heads|primary|move|enabled|name|pins
  DELETE /vaults/<id>         Lösen
  POST   /vaults/<id>/pins    {path, label?}
  DELETE /vaults/<id>/pins    body {path}

  GET    /workspaces          Kabelsalat-Snapshot (~/.glyph/workspaces.json) — ^_Code
  POST   /workspaces          Anbinden {input|path, mode?, head?}
  PATCH  /workspaces/<id>     mode|heads|primary|move|enabled|name
  DELETE /workspaces/<id>     Lösen

Läuft standardmäßig auf 127.0.0.1:PORT (nur lokal). Keine externen Abhängigkeiten.
Enthält KEINE Bestätigungs-UI: für Schreib-Tools liefert der Server ein
"pending_confirmation"-Ergebnis bzw. lehnt ohne confirm ab (siehe tool_loop).
"""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# .env früh laden (glyph-agent/.env), dann Bindings-Keys falls Env leer.
try:
    from core.dotenv import load_dotenv, load_ui_bindings
    load_dotenv()
    load_ui_bindings()
except Exception:
    pass

from core import config, tool_loop, llm
from core import code_loop
from core import jobs as agent_jobs
from core import recurring as agent_recurring
from core import vaults_registry as agent_vaults
from core import workspaces_registry as agent_workspaces
from core import code_grants as agent_grants
from core import tasks as agent_tasks

PORT = int(os.environ.get("GLYPH_AGENT_PORT", "18899"))
HOST = os.environ.get("GLYPH_AGENT_HOST", "127.0.0.1")

# Kabelsalat: Vault-Pfade aus ~/.glyph/vaults.json
try:
    config.reload_vault_paths()
except Exception:
    pass

# --- Textanhänge (Stufe 1) + PDF wenn extrahierbar ---
_ATTACH_TEXT_MIMES = {
    "text/plain", "text/markdown", "text/x-markdown", "text/html", "text/csv",
    "text/tab-separated-values", "application/json", "application/xml", "text/xml",
    "text/yaml", "application/yaml", "text/x-log",
}
_ATTACH_TEXT_EXTS = {"txt", "md", "markdown", "csv", "json", "xml", "yaml", "yml", "log", "html"}
_ATTACH_PDF_MIMES = {"application/pdf"}
_ATTACH_PDF_EXTS = {"pdf"}
_ATTACH_MAX_CHARS = 2 * 1024 * 1024  # pro Anhang
_ATTACH_PDF_MAX_CHARS = 40_000


def _extract_pdf_text_from_bytes(raw_bytes, name="anhang.pdf"):
    """Extrahiert Text aus PDF-Bytes via pdftotext (temp file). Graceful None."""
    import shutil
    import subprocess
    import tempfile

    bin_path = (
        os.environ.get("PDFTOTEXT_BIN")
        or shutil.which("pdftotext")
        or ("/opt/homebrew/bin/pdftotext" if os.path.isfile("/opt/homebrew/bin/pdftotext") else None)
        or ("/usr/local/bin/pdftotext" if os.path.isfile("/usr/local/bin/pdftotext") else None)
    )
    if not bin_path or not os.access(bin_path, os.X_OK):
        return None, "pdftotext fehlt"
    if not raw_bytes:
        return None, "leer"
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(raw_bytes)
            tmp_path = tmp.name
        try:
            proc = subprocess.run(
                [bin_path, "-layout", "-enc", "UTF-8", tmp_path, "-"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        if proc.returncode != 0:
            return None, (proc.stderr or "pdftotext Fehler")[:200]
        text = (proc.stdout or "")[:_ATTACH_PDF_MAX_CHARS]
        if not text.strip():
            return None, "kein Text extrahiert"
        return text, None
    except Exception as e:
        return None, str(e)[:200]


def _embed_attachments(message, attachments):
    """Bettet Text- und PDF-Anhänge sicher in die Nachricht ein.
    Text-MIMEs/ext direkt; PDF via pdftotext wenn möglich, sonst Hinweis.
    Liefert die (ggf. erweiterte) message."""
    import base64
    if not attachments:
        return message or ""
    parts = [message] if (message and message.strip()) else []
    skipped = []
    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        name = att.get("name") or "datei"
        # Dateiname escapen (nur Basisname, keine Kontrollzeichen)
        name = os.path.basename(str(name)).replace("\x00", "")[:200] or "datei"
        mime = str(att.get("mime") or "").lower()
        content = att.get("content") or ""
        ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
        is_text = (
            mime in _ATTACH_TEXT_MIMES
            or (not mime and ext in _ATTACH_TEXT_EXTS)
            or (mime == "application/octet-stream" and ext in _ATTACH_TEXT_EXTS)
        )
        is_pdf = mime in _ATTACH_PDF_MIMES or ext in _ATTACH_PDF_EXTS

        if is_pdf and not is_text:
            # content: Bytes, base64, Roh-%PDF, oder bereits extrahierter Text
            text = None
            err = None
            enc = str(att.get("encoding") or "").lower()
            if isinstance(content, (bytes, bytearray)):
                text, err = _extract_pdf_text_from_bytes(bytes(content), name)
            elif not str(content).strip():
                err = "leer"
            elif enc in ("base64", "b64") or str(content)[:8].startswith("JVBERi"):
                try:
                    raw = base64.b64decode(str(content), validate=False)
                    text, err = _extract_pdf_text_from_bytes(raw, name)
                except Exception as e:
                    err = f"base64: {e}"
            elif str(content).lstrip().startswith("%PDF"):
                text, err = _extract_pdf_text_from_bytes(
                    str(content).encode("latin-1", errors="replace"), name
                )
            else:
                # Client hat Text bereits extrahiert
                text = str(content)[:_ATTACH_PDF_MAX_CHARS]

            if text and str(text).strip():
                parts.append(f"[Anhang PDF: {name}]\n{text}\n[Ende Anhang: {name}]")
            else:
                skipped.append(f"{name} (PDF nicht lesbar: {err or 'unbekannt'})")
            continue

        if not is_text:
            skipped.append(f"{name} (kein erlaubter Text-Typ: {mime or 'unbekannt'})")
            continue
        if not str(content).strip():
            skipped.append(f"{name} (leer)")
            continue
        if len(str(content)) > _ATTACH_MAX_CHARS:
            raise ValueError(f"Textanhang zu groß: {name} (> {_ATTACH_MAX_CHARS} Zeichen)")
        parts.append(f"[Anhang: {name}]\n{content}\n[Ende Anhang: {name}]")
    if skipped:
        parts.append("[Übergangen: " + "; ".join(skipped) + "]")
    return "\n\n".join(parts) if parts else (message or "")


def _normalize_images(raw):
    """OpenAI image_url-Parts aus POST /chat.images (Whitelist png/jpeg/webp/gif)."""
    if not raw:
        return []
    allowed = {"image/png", "image/jpeg", "image/webp", "image/gif"}
    out = []
    for img in raw if isinstance(raw, list) else []:
        if not isinstance(img, dict):
            continue
        if img.get("type") == "image_url" and isinstance(img.get("image_url"), dict):
            url = str(img["image_url"].get("url") or "")
            if not url.startswith("data:image/"):
                continue
            # data:image/png;base64,...
            try:
                header = url.split(",", 1)[0]
                mime = header.split(";")[0].split(":", 1)[1].lower()
            except Exception:
                continue
            if mime == "image/jpg":
                mime = "image/jpeg"
            if mime not in allowed:
                continue
            out.append({"type": "image_url", "image_url": {"url": url}})
            continue
        mime = str(img.get("mime") or img.get("mimeType") or "").lower()
        data = img.get("data") or img.get("content") or ""
        if mime == "image/jpg":
            mime = "image/jpeg"
        if mime not in allowed or not data:
            continue
        data = str(data)
        if data.startswith("data:"):
            out.append({"type": "image_url", "image_url": {"url": data}})
        else:
            out.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{data}"},
            })
    return out[:8]  # hard cap (matches UI MAX_ATTACHMENTS)


def _handle_chat(payload, send=None):
    try:
        config.reload_vault_paths()
    except Exception:
        pass
    """Verarbeitet eine /chat-Anfrage.

    send: optionaler Callback send(dict) for Live-Streaming. Wenn gesetzt, wird
          jede Stufe (z.B. VaultFind start/done, WebSearch, OpenRouter, Antwort-
          texte) als JSON-Line an send() übergeben, sobald sie eintritt — UND die
          finale Antwort am Ende. Fehlt send, wird alles blockierend berechnet
          und nur das Ergebnis-Dict zurückgegeben (rückwärtskompatibel).
    """
    from core import usage_meter
    usage_meter.reset()

    message = (payload or {}).get("message", "")
    attachments = (payload or {}).get("attachments")
    images = _normalize_images((payload or {}).get("images"))
    # Prior Turns: list of {role, content} — Multi-Turn-Nachfragen (ACP speichert sie).
    conversation_history = (payload or {}).get("history")
    if conversation_history is None:
        conversation_history = (payload or {}).get("messages")
    # Textanhänge in die Nachricht einbetten (deutlich gekennzeichnet),
    # damit das Modell den Inhalt als Kontext bekommt.
    message = _embed_attachments(message, attachments)
    # Resume nach Glyph-Genehmigung darf ohne neue message laufen.
    is_resume = bool((payload or {}).get("resume_token"))
    if not message.strip() and not images and not is_resume:
        err = {"ok": False, "answer": "Leere Nachricht.", "rounds": 0, "tool_calls": []}
        if send:
            send({"type": "done", **err})
        return err

    # Per-Request-Modus (C′): Payload.mode hat Vorrang; sonst Prozess-MODE.
    # "code" = ^_Code (DeepSeek + Workspace-Tools); Default = Vault-Agent.
    if (payload or {}).get("mode"):
        req_mode = str((payload or {}).get("mode")).lower()
    else:
        req_mode = str(getattr(config, "MODE", "agent") or "agent").lower()

    if req_mode == "openrouter-chat":
        # Reiner OpenRouter-Chat: KEIN Tool-Loop, KEIN Vault, KEINE Tools.
        from core import llm as _llm
        system = (
            "Du bist glyph-agent (reiner Chat-Modus). Cloud-Denker: "
            "deepseek-v4-flash-vision-exp (Direct), Fallback OpenRouter "
            "deepseek/deepseek-v4-flash-0731. Kein Tiny/Free. "
            "Du hast KEINEN Zugriff auf Dateien, einen Vault, Tools oder das Internet. "
            "Antworte nur aus deinem eigenen Wissen. "
            "Bei Modell-Fragen: nenne das aktive Runtime-Modell "
            "(Primär deepseek-v4-flash-vision-exp, sonst OpenRouter Flash-0731), kein Wiki/Tool nötig."
        )
        try:
            from core import history as chat_history
            prior, _ = chat_history.build_history_for_loop(
                message, conversation_history
            )
            user_text = message
            if prior:
                block = chat_history.format_prior_block(prior)
                user_text = (
                    "Bisheriger Chat-Verlauf:\n\n"
                    + block
                    + "\n\n---\nAktuelle Nachricht:\n"
                    + message
                )
            if images:
                from core.providers.openrouter import user_content_with_images
                user_payload = user_content_with_images(user_text, images)
            else:
                user_payload = user_text
            answer = _llm.chat(system, user_payload)
            res = {"ok": True, "answer": answer, "rounds": 1, "tool_calls": [], "chat_mode": "openrouter-chat"}
            if send:
                send({"type": "done", **res})
            return res
        except Exception as e:
            res = {"ok": False, "answer": f"OpenRouter-Chat fehlgeschlagen: {e}", "rounds": 1, "tool_calls": [], "chat_mode": "openrouter-chat"}
            if send:
                send({"type": "done", **res})
            return res

    # Bestätigungsliste aus Payload (write-Tools / Shell).
    confirm_allow = (payload or {}).get("confirm")
    def confirm(tool_name, args):
        if not isinstance(confirm_allow, list):
            return False
        for c in confirm_allow:
            if isinstance(c, dict) and c.get("tool") == tool_name and c.get("args") == args:
                return True
        return False

    def confirm_agent(tool_name, args):
        from core import vault_write_policy

        if vault_write_policy.allow_chat_write(tool_name, args):
            return True
        return confirm(tool_name, args)

    def on_event(event):
        if send:
            send(event)

    # Composer-Swarm (°_Agent / ^_Code): Planer → Suche → Synthese, kein Chat-Loop.
    if (payload or {}).get("swarm"):
        from core import swarm as swarm_mod
        result = swarm_mod.run_swarm(message, on_event=on_event)
        p = llm.get_provider()
        used_model = getattr(p, "_active_model", None) or p.model_name
        result = {
            "used_provider": p.provider_name,
            "used_model": used_model,
            "mode": "swarm",
            "pending_confirmation": False,
            **result,
        }
        if send:
            send({"type": "done", **result})
        return result

    # --- CODE-Modus (^_Code) / TICK-Modus (^_tick): DeepSeek + Workspace-Tools, kein Vault ---
    if req_mode in ("code", "tick"):
        resume_token = (payload or {}).get("resume_token")
        allow_pending = (payload or {}).get("allow_pending")
        # allow_pending kann True/False sein; None = kein Resume
        if resume_token is not None and allow_pending is None and (payload or {}).get("allow") is not None:
            allow_pending = bool((payload or {}).get("allow"))
        result = code_loop.run_code(
            message,
            confirm=confirm,
            on_event=on_event,
            resume_token=resume_token,
            allow_pending=allow_pending,
            images=images,
            conversation_history=conversation_history,
            grant_scope=(payload or {}).get("grant_scope"),
            grant_spec=(payload or {}).get("grant_spec"),
            head=("tick" if req_mode == "tick" else "code"),
        )
        p = llm.get_provider("code")
        used_model = (
            (result.get("trace") or {}).get("model")
            or getattr(config, "CODE_OPENROUTER_MODEL", None)
            or getattr(p, "_active_model", None)
            or p.model_name
        )
        result = {
            "used_provider": p.provider_name,
            "used_model": used_model,
            "mode": req_mode,
            **result,
        }
        if "pending_confirmation" not in result:
            result["pending_confirmation"] = False
        if send:
            send({"type": "done", **result})
        return result

    # Agentenmodus: kontrollierter Tool-Loop mit Bestätigung für Schreib-Tools.
    vault_search = (payload or {}).get("vault_search")
    if vault_search is not None:
        vault_search = bool(vault_search)
    vault_selected = None
    if "vault_selected" in (payload or {}):
        vault_selected = _normalize_vault_selected((payload or {}).get("vault_selected"))
    result = tool_loop.run(
        message,
        confirm=confirm_agent,
        on_event=on_event,
        images=images,
        conversation_history=conversation_history,
        vault_search=vault_search,
        vault_selected=vault_selected,
    )
    result = _ensure_chat_file(message, result, send, conversation_history)
    # Modell-Info anhängen (Primär Luna oder Free-Fallback).
    p = llm.get_provider()
    used_model = getattr(p, "_active_model", None) or p.model_name
    result = {
        "used_provider": p.provider_name,
        "used_model": used_model,
        "mode": "agent",
        "pending_confirmation": False,
        **result,
    }
    if send:
        send({"type": "done", **result})
    return result


def _ensure_chat_file(message, result, send, history=None):
    """Datei-Wunsch ohne ChatDatei: aus Vorlage im Verlauf trotzdem den Chat-Link bauen."""
    if not isinstance(result, dict):
        return result
    calls = result.get("tool_calls") or []
    if any(isinstance(c, dict) and c.get("tool") == "ChatDatei" and c.get("ok") for c in calls):
        return result
    from core import chat_file

    out = chat_file.from_answer(message, result.get("answer") or "", history)
    if not out or not out.get("ok"):
        return result
    inner = out.get("result") or {}
    if send and inner.get("data"):
        send({
            "type": "chat_file",
            "title": inner.get("title") or "Datei",
            "name": inner.get("name") or "Datei.pdf",
            "mime": inner.get("mime") or "application/pdf",
            "text": inner.get("text") or "",
            "data": inner.get("data"),
        })
    return result


def _normalize_vault_selected(raw, limit=12):
    """Gewählte Ordner-Suche-Treffer aus der UI — klein und pfadbegrenzt."""
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:limit]:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path or len(path) > 500:
            continue
        raw_kind = str(item.get("kind") or "")
        if raw_kind == "web" or path.startswith("http://") or path.startswith("https://"):
            kind = "web"
        elif raw_kind == "folder":
            kind = "folder"
        else:
            kind = "file"
        excerpt = str(item.get("excerpt") or item.get("text") or "")[:800]
        source = str(item.get("source") or "")[:40]
        if kind == "web" and not source:
            source = "dguv" if "dguv.de" in path.lower() else "komnet"
        out.append({
            "id": str(item.get("id") or f"{kind}:{path}")[:200],
            "kind": kind,
            "path": path,
            "title": str(item.get("title") or path.rsplit("/", 1)[-1])[:200],
            "excerpt": excerpt,
            "score": item.get("score"),
            "source": source,
        })
    return out


def _handle_vault_find(payload):
    """POST /vault/find — Trefferliste für den UI-Toggle, kein LLM."""
    from core import vault_preview

    query = str((payload or {}).get("query") or (payload or {}).get("message") or "").strip()
    try:
        top_k = int((payload or {}).get("top_k") or 8)
    except (TypeError, ValueError):
        top_k = 8
    return vault_preview.preview_vault_hits(query, top_k=top_k)


def _handle_health():
    from core import runtime_models

    p = llm.get_provider()
    snap = runtime_models.current_models_snapshot()
    return {
        "status": "ok",
        "provider": p.provider_name,
        "model": p.model_name,
        "code_model": snap.get("code_model")
        or getattr(config, "CODE_OPENROUTER_MODEL", None),
        "code_fallback_model": snap.get("code_fallback_model"),
        "primary_model": (snap.get("shared") or {}).get("primary"),
        "fallback_model": (snap.get("shared") or {}).get("fallback"),
        "models": snap,
        "modes": ["agent", "code", "tick", "openrouter-chat"],
    }


def _read_json_body(handler):
    length = int(handler.headers.get("Content-Length", 0) or 0)
    raw = handler.rfile.read(length) if length else b"{}"
    try:
        return json.loads(raw.decode("utf-8") or "{}"), None
    except json.JSONDecodeError:
        return None, "Invalid JSON"


def _handle_models_post(payload):
    from core import runtime_models

    body = payload or {}
    if isinstance(body.get("direct"), dict):
        runtime_models.apply_direct(body.get("direct"))
    if (
        isinstance(body.get("shared"), dict)
        or body.get("primary")
        or body.get("model")
    ):
        snap = runtime_models.apply_models(body)
    else:
        snap = runtime_models.current_models_snapshot()
    # Provider-Modus (direct | openrouter | hybrid) hot-applyen
    # kind=code → nur ^_Code, sonst °_Agent
    if isinstance(body.get("provider"), str) and body["provider"].strip():
        snap = runtime_models.apply_provider(
            {"provider": body["provider"], "kind": body.get("kind") or "agent"}
        )
    elif isinstance(body.get("mode"), str) and body["mode"].strip():
        snap = runtime_models.apply_provider(
            {"provider": body["mode"], "kind": body.get("kind") or "agent"}
        )
    return {"ok": True, "models": snap, **_handle_health()}


def _handle_models_probe(payload):
    from core import runtime_models

    body = payload or {}
    model = body.get("model") or body.get("primary") or ""
    kind = str(body.get("kind") or "agent").lower()
    hop = body.get("hop") or body.get("provider") or None
    timeout = body.get("timeout")
    try:
        timeout_i = int(timeout) if timeout is not None else 45
    except (TypeError, ValueError):
        timeout_i = 45
    result = runtime_models.probe_model(
        str(model), timeout=timeout_i, kind=kind, hop=hop
    )
    # Attach context_length hint from OpenRouter public catalog when possible
    try:
        import urllib.request

        mid = str(model).strip()
        req = urllib.request.Request(
            f"{getattr(config, 'OPENROUTER_URL', 'https://openrouter.ai/api/v1').rstrip('/')}/models",
            headers={"Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for m in data.get("data") or []:
            if str(m.get("id") or "") == mid:
                cl = m.get("context_length")
                if cl:
                    result["context_length"] = int(cl)
                break
    except Exception:
        pass
    return result


def main():
    # ThreadingHTTPServer: ein hängender /chat blockiert nicht /health und
    # andere Requests (Kern-Fix gegen Server-Einfrieren bei OpenRouter-Hang).
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    # Nur diese lokalen App-Origins duerfen den Engine direkt ansprechen.
    _APP_ORIGINS = ("app://obsidian.md", "null")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # leiser
            pass

        def _cors_origin(self):
            origin = str(self.headers.get("Origin") or "").strip()
            return origin if origin in _APP_ORIGINS else ""

        def do_OPTIONS(self):
            origin = self._cors_origin()
            self.send_response(204)
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header(
                    "Access-Control-Allow-Methods",
                    "GET, POST, PATCH, DELETE, OPTIONS",
                )
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Max-Age", "600")
                self.send_header("Vary", "Origin")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            origin = self._cors_origin()
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(body)

        def _handle_vault_file(self, qs):
            """Rohdatei aus dem Vault (PDF/Text) für das Anzeigefenster der UI.

            Gleiche Scope-Regeln wie ReadPdf: nur Vault-Pfade; BLOCKED_DIRS und
            private Vaults werden nicht ausgeliefert.
            """
            import mimetypes
            import urllib.parse as _up

            params = _up.parse_qs(qs or "")
            raw = (params.get("path") or [""])[0]
            if not raw:
                self._send(400, {"ok": False, "error": "path fehlt"})
                return
            try:
                config.reload_vault_paths()
            except Exception:
                pass
            try:
                from core import vault_tools, vaults_registry

                abs_p = vault_tools._resolve_vault_path(raw)
                if not abs_p or not os.path.isfile(abs_p):
                    self._send(404, {"ok": False, "error": "Datei nicht gefunden"})
                    return
                rel = vault_tools._rel_to_root(abs_p)
                if vault_tools._is_blocked(rel):
                    self._send(403, {"ok": False, "error": "Geschützter Ordner"})
                    return
                real = os.path.realpath(abs_p)
                # Privat-Erkennung über den *nächstgelegenen* Vault-Root: der Home-Vault
                # (/Users/<user>) ist Elternverzeichnis aller anderen Vaults — ein reiner
                # Präfix-Vergleich würde damit alles blockieren.
                roots = [
                    os.path.realpath(v)
                    for v in (getattr(config, "VAULT_PATHS", None) or [])
                    if v
                ]
                owner = max(
                    (r for r in roots if real == r or real.startswith(r + os.sep)),
                    key=len,
                    default=None,
                )
                priv = {os.path.realpath(p) for p in vaults_registry.private_paths()}
                if owner in priv:
                    self._send(403, {"ok": False, "error": "Privater Vault"})
                    return
            except Exception as e:  # noqa: BLE001
                self._send(400, {"ok": False, "error": str(e)[:200]})
                return
            fmt = (params.get("format") or [""])[0].strip().lower()
            if fmt in ("pages", "text"):
                if not abs_p.lower().endswith(".pdf"):
                    self._send(400, {"ok": False, "error": "kein PDF", "pages": []})
                    return
                from core import pdf_tools

                pages, err = pdf_tools.pdf_pages_text(abs_p)
                if err:
                    self._send(200, {"ok": False, "error": err, "pages": pages or []})
                    return
                self._send(200, {"ok": True, "pages": pages, "path": rel})
                return
            ext = os.path.splitext(abs_p)[1].lower()
            ctype = mimetypes.guess_type(abs_p)[0] or "application/octet-stream"
            if ext in (".md", ".txt", ".json", ".csv"):
                ctype = "text/plain; charset=utf-8"
            try:
                with open(abs_p, "rb") as fh:
                    data = fh.read()
            except OSError as e:
                self._send(500, {"ok": False, "error": str(e)})
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header(
                "Content-Disposition", f'inline; filename="{os.path.basename(abs_p)}"'
            )
            origin = self._cors_origin()
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            full = self.path
            path = full.split("?", 1)[0]
            qs = full.split("?", 1)[1] if "?" in full else ""
            if path == "/health" or path.startswith("/health"):
                try:
                    config.reload_vault_paths()
                except Exception:
                    pass
                self._send(200, _handle_health())
            elif path in ("/vault/file", "/vault/file/"):
                self._handle_vault_file(qs)
            elif path in ("/vaults", "/vaults/"):
                try:
                    config.reload_vault_paths()
                    self._send(200, agent_vaults.public_snapshot())
                except Exception as e:
                    self._send(500, {"ok": False, "error": str(e)})
            elif path in ("/workspaces", "/workspaces/"):
                try:
                    self._send(200, agent_workspaces.public_snapshot())
                except Exception as e:
                    self._send(500, {"ok": False, "error": str(e)})
            elif path in ("/tasks", "/tasks/"):
                self._send(200, {"ok": True, "items": agent_tasks.list_items()})
            elif path.startswith("/tasks/") and path.rstrip("/").endswith("/prompt"):
                item_id = path.rstrip("/").split("/")[-2]
                try:
                    self._send(200, {"ok": True, "prompt": agent_tasks.handoff_prompt(item_id)})
                except ValueError as e:
                    self._send(404, {"ok": False, "error": str(e)})
            elif path in ("/code/grants", "/code/grants/"):
                try:
                    self._send(200, agent_grants.public_snapshot())
                except Exception as e:
                    self._send(500, {"ok": False, "error": str(e)})
            elif path in ("/routines/overview", "/routines/overview/"):
                try:
                    from core import routines as agent_routines

                    self._send(200, agent_routines.overview())
                except Exception as e:
                    self._send(500, {"ok": False, "error": str(e)})
            elif path in ("/recurring", "/recurring/"):
                try:
                    mig = agent_recurring.ensure_migrated()
                    self._send(
                        200,
                        {
                            "ok": True,
                            "items": agent_recurring.list_items(),
                            "migration": mig,
                        },
                    )
                except Exception as e:
                    self._send(500, {"ok": False, "error": str(e)})
            elif path in ("/recurring/events", "/recurring/events/"):
                after = ""
                for part in qs.split("&"):
                    if part.startswith("after="):
                        from urllib.parse import unquote

                        after = unquote(part[6:])
                try:
                    self._send(
                        200,
                        {
                            "ok": True,
                            "events": agent_recurring.list_events(after_ts=after),
                        },
                    )
                except Exception as e:
                    self._send(500, {"ok": False, "error": str(e)})
            elif path in ("/jobs", "/jobs/"):
                self._send(200, {"ok": True, "jobs": agent_jobs.list_jobs()})
            elif path in ("/models", "/models/"):
                # Snapshot only (same shape as /health["models"]); mutate via POST.
                try:
                    from core import runtime_models

                    self._send(200, {"ok": True, "models": runtime_models.current_models_snapshot()})
                except Exception as e:
                    self._send(500, {"ok": False, "error": str(e)})
            else:
                self._send(404, {"error": "Not found"})

        def do_PATCH(self):
            path = self.path.split("?", 1)[0]
            if path.startswith("/tasks/") and path.count("/") == 2:
                item_id = path.rstrip("/").split("/")[-1]
                payload, err = _read_json_body(self)
                if err:
                    self._send(400, {"error": err, "ok": False})
                    return
                try:
                    self._send(200, {"ok": True, "item": agent_tasks.update_item(item_id, payload or {})})
                except ValueError as e:
                    msg = str(e)
                    code = 404 if "nicht gefunden" in msg.lower() else 400
                    self._send(code, {"ok": False, "error": msg})
                except OSError as e:
                    self._send(500, {"ok": False, "error": f"Aufgabe nicht speicherbar: {e}"})
                return
            if path.startswith("/vaults/") and path.count("/") >= 2:
                parts = path.rstrip("/").split("/")
                # /vaults/<id> or /vaults/<id>/pins
                if len(parts) >= 3 and parts[1] == "vaults":
                    vid = parts[2]
                    payload, err = _read_json_body(self)
                    if err:
                        self._send(400, {"error": err, "ok": False})
                        return
                    try:
                        if len(parts) >= 4 and parts[3] == "pins":
                            item = agent_vaults.add_pin(
                                vid,
                                str((payload or {}).get("path") or ""),
                                str((payload or {}).get("label") or ""),
                            )
                            self._send(200, {"ok": True, "vault": item})
                        else:
                            item = agent_vaults.update_vault(vid, payload or {})
                            self._send(200, {"ok": True, "vault": item})
                    except ValueError as e:
                        self._send(400, {"ok": False, "error": str(e)})
                    except Exception as e:
                        self._send(500, {"ok": False, "error": str(e)})
                    return
            if path.startswith("/workspaces/") and path.count("/") >= 2:
                parts = path.rstrip("/").split("/")
                if len(parts) >= 3 and parts[1] == "workspaces":
                    wid = parts[2]
                    payload, err = _read_json_body(self)
                    if err:
                        self._send(400, {"error": err, "ok": False})
                        return
                    try:
                        item = agent_workspaces.update_workspace(wid, payload or {})
                        self._send(200, {"ok": True, "workspace": item})
                    except ValueError as e:
                        self._send(400, {"ok": False, "error": str(e)})
                    except Exception as e:
                        self._send(500, {"ok": False, "error": str(e)})
                    return
            if path.startswith("/recurring/") and path.count("/") >= 2:
                item_id = path.rstrip("/").split("/")[-1]
                if item_id in ("run-due", "events"):
                    self._send(404, {"error": "Not found"})
                    return
                payload, err = _read_json_body(self)
                if err:
                    self._send(400, {"error": err, "ok": False})
                    return
                try:
                    item = agent_recurring.update_item(item_id, payload or {})
                    self._send(200, {"ok": True, "item": item})
                except ValueError as e:
                    self._send(400, {"ok": False, "error": str(e)})
                except Exception as e:
                    self._send(500, {"ok": False, "error": str(e)})
                return
            self._send(404, {"error": "Not found"})

        def do_DELETE(self):
            path = self.path.split("?", 1)[0]
            if path.startswith("/vaults/") and path.count("/") >= 2:
                parts = path.rstrip("/").split("/")
                if len(parts) >= 3 and parts[1] == "vaults":
                    vid = parts[2]
                    try:
                        if len(parts) >= 4 and parts[3] == "pins":
                            payload, err = _read_json_body(self)
                            if err:
                                self._send(400, {"error": err, "ok": False})
                                return
                            item = agent_vaults.remove_pin(
                                vid, str((payload or {}).get("path") or "")
                            )
                            self._send(200, {"ok": True, "vault": item})
                        else:
                            ok = agent_vaults.detach(vid)
                            self._send(200 if ok else 404, {"ok": ok, "id": vid})
                    except ValueError as e:
                        self._send(400, {"ok": False, "error": str(e)})
                    except Exception as e:
                        self._send(500, {"ok": False, "error": str(e)})
                    return
            if path.startswith("/workspaces/") and path.count("/") >= 2:
                parts = path.rstrip("/").split("/")
                if len(parts) >= 3 and parts[1] == "workspaces":
                    wid = parts[2]
                    try:
                        ok = agent_workspaces.detach(wid)
                        self._send(200 if ok else 404, {"ok": ok, "id": wid})
                    except ValueError as e:
                        self._send(400, {"ok": False, "error": str(e)})
                    except Exception as e:
                        self._send(500, {"ok": False, "error": str(e)})
                    return
            if path.startswith("/recurring/") and path.count("/") >= 2:
                item_id = path.rstrip("/").split("/")[-1]
                try:
                    ok = agent_recurring.delete_item(item_id)
                    self._send(200 if ok else 404, {"ok": ok, "id": item_id})
                except Exception as e:
                    self._send(500, {"ok": False, "error": str(e)})
                return
            if path.startswith("/tasks/") and path.count("/") == 2:
                item_id = path.rstrip("/").split("/")[-1]
                try:
                    ok = agent_tasks.delete_item(item_id)
                    self._send(200 if ok else 404, {"ok": ok, "id": item_id})
                except Exception as e:
                    self._send(500, {"ok": False, "error": str(e)})
                return
            self._send(404, {"error": "Not found"})

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path in ("/tasks", "/tasks/"):
                payload, err = _read_json_body(self)
                if err:
                    self._send(400, {"error": err, "ok": False})
                    return
                try:
                    self._send(200, {"ok": True, "item": agent_tasks.create_item(payload or {})})
                except ValueError as e:
                    self._send(400, {"ok": False, "error": str(e)})
                except OSError as e:
                    self._send(500, {"ok": False, "error": f"Aufgabe nicht speicherbar: {e}"})
                return
            if path in ("/vaults", "/vaults/"):
                payload, err = _read_json_body(self)
                if err:
                    self._send(400, {"error": err, "ok": False})
                    return
                try:
                    raw_in = str(
                        (payload or {}).get("input")
                        or (payload or {}).get("path")
                        or (payload or {}).get("uri")
                        or ""
                    )
                    mode = str((payload or {}).get("mode") or "r")
                    head = str((payload or {}).get("head") or "").strip() or None
                    res = agent_vaults.attach(raw_in, mode=mode, head=head)
                    self._send(200, {"ok": True, **res})
                except ValueError as e:
                    self._send(400, {"ok": False, "error": str(e)})
                except Exception as e:
                    self._send(500, {"ok": False, "error": str(e)})
                return
            if path.startswith("/vaults/") and path.count("/") >= 2:
                parts = path.rstrip("/").split("/")
                if len(parts) >= 4 and parts[1] == "vaults" and parts[3] == "pins":
                    vid = parts[2]
                    payload, err = _read_json_body(self)
                    if err:
                        self._send(400, {"error": err, "ok": False})
                        return
                    try:
                        item = agent_vaults.add_pin(
                            vid,
                            str((payload or {}).get("path") or ""),
                            str((payload or {}).get("label") or ""),
                        )
                        self._send(200, {"ok": True, "vault": item})
                    except ValueError as e:
                        self._send(400, {"ok": False, "error": str(e)})
                    except Exception as e:
                        self._send(500, {"ok": False, "error": str(e)})
                    return
            if path in ("/code/grants/close-task", "/code/grants/close-task/"):
                try:
                    ok = agent_grants.close_task()
                    self._send(200, {"ok": True, "closed": ok, **agent_grants.public_snapshot()})
                except Exception as e:
                    self._send(500, {"ok": False, "error": str(e)})
                return
            if path.startswith("/code/grants/") and path.rstrip("/").endswith("/revoke"):
                parts = path.rstrip("/").split("/")
                gid = parts[-2] if len(parts) >= 3 else ""
                try:
                    ok = agent_grants.revoke(gid)
                    self._send(200, {"ok": True, "revoked": ok, **agent_grants.public_snapshot()})
                except Exception as e:
                    self._send(500, {"ok": False, "error": str(e)})
                return
            if path in ("/workspaces", "/workspaces/"):
                payload, err = _read_json_body(self)
                if err:
                    self._send(400, {"error": err, "ok": False})
                    return
                try:
                    raw_in = str(
                        (payload or {}).get("input")
                        or (payload or {}).get("path")
                        or ""
                    )
                    mode = str((payload or {}).get("mode") or "r")
                    head = str((payload or {}).get("head") or "").strip() or None
                    res = agent_workspaces.attach(raw_in, mode=mode, head=head)
                    self._send(200, {"ok": True, **res})
                except ValueError as e:
                    self._send(400, {"ok": False, "error": str(e)})
                except Exception as e:
                    self._send(500, {"ok": False, "error": str(e)})
                return
            if path in ("/models", "/models/"):
                payload, err = _read_json_body(self)
                if err:
                    self._send(400, {"error": err, "ok": False})
                    return
                try:
                    self._send(200, _handle_models_post(payload))
                except ValueError as e:
                    self._send(400, {"error": str(e), "ok": False})
                except Exception as e:
                    self._send(500, {"error": str(e), "ok": False})
                return
            if path in ("/models/probe", "/models/probe/", "/models/test", "/models/test/"):
                payload, err = _read_json_body(self)
                if err:
                    self._send(400, {"error": err, "ok": False})
                    return
                try:
                    self._send(200, _handle_models_probe(payload))
                except ValueError as e:
                    self._send(400, {"error": str(e), "ok": False})
                except Exception as e:
                    self._send(500, {"error": str(e), "ok": False})
                return
            # --- recurring todos ---
            if path in ("/recurring", "/recurring/"):
                payload, err = _read_json_body(self)
                if err:
                    self._send(400, {"error": err, "ok": False})
                    return
                try:
                    agent_recurring.ensure_migrated()
                    item = agent_recurring.create_item(payload or {})
                    self._send(200, {"ok": True, "item": item})
                except ValueError as e:
                    self._send(400, {"ok": False, "error": str(e)})
                except Exception as e:
                    self._send(500, {"ok": False, "error": str(e)})
                return
            if path in ("/recurring/run-due", "/recurring/run-due/"):
                try:
                    agent_recurring.ensure_migrated()
                    self._send(200, agent_recurring.run_due())
                except Exception as e:
                    self._send(500, {"ok": False, "error": str(e)})
                return
            if path.startswith("/recurring/") and path.rstrip("/").endswith("/run"):
                parts = path.rstrip("/").split("/")
                # /recurring/<id>/run
                item_id = parts[-2] if len(parts) >= 3 else ""
                payload, err = _read_json_body(self)
                if err:
                    payload = {}
                try:
                    result = agent_recurring.run_item(
                        item_id, force=bool((payload or {}).get("force", True))
                    )
                    self._send(200, result)
                except Exception as e:
                    self._send(500, {"ok": False, "error": str(e), "id": item_id})
                return
            if path.startswith("/recurring/") and path.rstrip("/").endswith("/pause"):
                parts = path.rstrip("/").split("/")
                item_id = parts[-2] if len(parts) >= 3 else ""
                payload, err = _read_json_body(self)
                if err:
                    self._send(400, {"error": err, "ok": False})
                    return
                paused = True if payload is None else bool((payload or {}).get("paused", True))
                try:
                    item = agent_recurring.set_paused(item_id, paused)
                    self._send(200, {"ok": True, "item": item})
                except ValueError as e:
                    self._send(400, {"ok": False, "error": str(e)})
                except Exception as e:
                    self._send(500, {"ok": False, "error": str(e)})
                return
            if path in ("/jobs/run", "/jobs/run/"):
                # Legacy: map seed job names → recurring ids
                payload, err = _read_json_body(self)
                if err:
                    self._send(400, {"error": err, "ok": False})
                    return
                job_id = str((payload or {}).get("job") or (payload or {}).get("id") or "").strip()
                alias = {
                    "hseq-eingang": "td-eingang",
                    "hseq-handover": "td-handover",
                    "hseq-aus-fertig-lernen": "td-lernen",
                }
                rid = alias.get(job_id, job_id)
                try:
                    agent_recurring.ensure_migrated()
                    result = agent_recurring.run_item(
                        rid, force=bool((payload or {}).get("force", True))
                    )
                    self._send(200, result)
                except Exception as e:
                    self._send(500, {"error": str(e), "ok": False, "job": job_id})
                return
            if path in ("/vault/find", "/vault/find/"):
                payload, err = _read_json_body(self)
                if err:
                    self._send(400, {"error": err, "ok": False})
                    return
                try:
                    result = _handle_vault_find(payload)
                    self._send(200, result)
                except BrokenPipeError:
                    return
                except Exception as e:
                    try:
                        self._send(500, {"ok": False, "error": str(e), "hits": []})
                    except BrokenPipeError:
                        return
                return
            if path == "/chat" or path.startswith("/chat"):
                payload, err = _read_json_body(self)
                if err:
                    self._send(400, {"error": err})
                    return
                try:
                    # Streaming-Modus (NDJSON): Client sendet explizit den Header —
                    # dann werden Stufen/Teil-Antworten live als JSON-Lines geflusht.
                    stream = (self.headers.get("Accept", "") == "application/x-ndjson")
                    if not stream:
                        result = _handle_chat(payload)
                        from core import usage_meter
                        snap = usage_meter.snapshot()
                        if isinstance(result, dict) and snap["total"] > 0:
                            result = {**result, "usage": snap}
                        # bei write-Tool ohne Freigabe -> 200 mit pending-Flag
                        self._send(200, {"pending_confirmation": False, **result})
                        return
                    # --- NDJSON-Stream ---
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    def send(obj):
                        from core.tool_loop import ClientCancelled
                        from core import usage_meter
                        import errno as _errno
                        try:
                            if isinstance(obj, dict) and obj.get("type") == "done":
                                snap = usage_meter.snapshot()
                                if snap["total"] > 0:
                                    obj = dict(obj)
                                    obj["usage"] = snap
                            line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
                            self.wfile.write(line)
                            self.wfile.flush()
                        except BrokenPipeError as e:
                            raise ClientCancelled(str(e)) from e
                        except ConnectionResetError as e:
                            raise ClientCancelled(str(e)) from e
                        except OSError as e:
                            # Client aborted (session/cancel → fetch abort).
                            if e.errno in (
                                getattr(_errno, "EPIPE", -1),
                                getattr(_errno, "ECONNRESET", -1),
                                getattr(_errno, "ECONNABORTED", -1),
                            ):
                                raise ClientCancelled(str(e)) from e
                            pass
                        except Exception:
                            pass
                    try:
                        _handle_chat(payload, send=send)
                    except Exception as e:
                        from core.tool_loop import ClientCancelled
                        if isinstance(e, ClientCancelled):
                            # Socket already gone — nothing left to write.
                            return
                        raise
                except Exception as e:
                    if "stream" in locals() and stream:
                        try:
                            send({"type": "error", "error": str(e)})
                        except Exception:
                            pass
                    else:
                        self._send(500, {"error": str(e)})
            else:
                self._send(404, {"error": "Not found"})

    config.ensure_dirs()
    # Start-Validierung: Direct- oder OpenRouter-Key — ohne beides kein Start.
    try:
        provider = llm.get_provider()
    except Exception as e:
        print(f"❌ Provider-Initialisierung fehlgeschlagen: {e}", file=sys.stderr)
        sys.exit(1)
    pname = getattr(provider, "provider_name", "")
    has_direct = bool(
        os.environ.get("DIRECT_API_KEY", "").strip()
        or os.environ.get("DEEPSEEK_API_KEY", "").strip()
    )
    has_or = bool(os.environ.get("OPENROUTER_API_KEY", "").strip())
    if pname == "direct" and not has_direct and not has_or:
        print(
            "❌ AGENT_PRIMARY_PROVIDER=direct braucht DIRECT_API_KEY "
            "(oder DEEPSEEK_API_KEY) — OpenRouter-Key nur als Fallback.\n"
            "   Start abgebrochen.",
            file=sys.stderr,
        )
        sys.exit(1)
    if pname in ("openrouter", "fallback") and not has_or:
        print(
            "❌ AGENT_PRIMARY_PROVIDER=openrouter erfordert OPENROUTER_API_KEY.\n"
            "   Start abgebrochen.",
            file=sys.stderr,
        )
        sys.exit(1)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    # Daemon-Threads: Server-Stop wartet nicht auf hängende Chat-Worker.
    server.daemon_threads = True
    print(f"glyph-agent HTTP-Dienst läuft auf http://{HOST}:{PORT}")
    print(f"  Provider: {provider.provider_name}, Modell: {provider.model_name}")
    print(
        f"  Timeout: CHAT={getattr(config, 'CHAT_TIMEOUT', 60)}s "
        f"CODE={getattr(config, 'CODE_CHAT_TIMEOUT', 60)}s · threaded"
    )
    print("  POST /chat  |  POST /vault/find  |  GET /health  |  GET|POST /models  |  POST /models/probe")
    print("  GET|POST /recurring  |  POST /recurring/<id>/run|pause  |  POST /recurring/run-due")
    try:
        agent_recurring.ensure_migrated()
    except Exception as e:
        print(f"  (recurring migration warn: {e})", file=sys.stderr)

    # Einmal bei Start: fällige To-dos nachziehen (Catch-up-Plist kann vor dem Agent laufen).
    def _run_due_once_on_start():
        delay = float(os.environ.get("GLYPH_RECURRING_STARTUP_DELAY_S", "2") or "2")
        if delay > 0:
            time.sleep(delay)
        try:
            from core import log as agent_log

            agent_log.log("recurring_startup_run_due_begin")
            result = agent_recurring.run_due()
            ran = result.get("ran", 0) if isinstance(result, dict) else "?"
            msg = f"recurring run-due (Start): ran={ran}"
            print(msg, flush=True)
            agent_log.log("recurring_startup_run_due_done", ran=ran)
        except Exception as e:
            print(f"  (recurring run-due Start warn: {e})", file=sys.stderr, flush=True)
            try:
                from core import log as agent_log

                agent_log.log("recurring_startup_run_due_error", error=str(e)[:300])
            except Exception:
                pass

    threading.Thread(
        target=_run_due_once_on_start,
        name="recurring-run-due-startup",
        daemon=True,
    ).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBeendet.")
        server.server_close()


if __name__ == "__main__":
    main()
