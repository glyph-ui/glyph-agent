# -*- coding: utf-8 -*-
"""
OpenRouterProvider — Cloud-Modell über OpenRouter.

Kette (B+):
  1. Primär: config AGENT_OPENROUTER_MODEL (Default deepseek-v4-flash-vision-exp)
  2. Fallback: AGENT_OPENROUTER_FALLBACK_MODEL (Default deepseek/deepseek-v4-flash-0731)

Kein lokaler Chat-Fallback. Ohne API-Key: harter Fehler.

WICHTIG (Datenschutz):
Dieser Provider sendet den übergebenen Text an OpenRouter (Cloud).
Der Tool-Loop entscheidet, was hier ankommt — nur minimierte Ausschnitte,
nie der vollständige Vault. Key: OPENROUTER_API_KEY aus .env.

Timeout (Stabilität):
  Socket-Timeout allein reicht nicht — resp.read() kann hängen.
  _chat_completion erzwingt ein hartes Total-Timeout (Wall-Clock) per
  Worker-Thread + future.result(deadline), analog extract_tinyfish.
"""
import json
import logging
import os
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request

from . import ModelProvider
from .. import log as _agent_log
from .. import config as _cfg

log = logging.getLogger("glyph-agent.openrouter")


def _anthropic_compat(url):
    """Claude-Platform lehnt temperature ab (Haiku 5.5: HTTP 400)."""
    try:
        host = (urllib.parse.urlparse(str(url or "")).hostname or "").lower()
    except Exception:
        return False
    return host == "api.anthropic.com"


def _part_text(part):
    """Text aus einem Content-Part (OpenAI / Gemini / OpenRouter)."""
    if part is None:
        return ""
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return str(part)
    ptype = str(part.get("type") or "").lower()
    if "encrypted" in ptype:
        return ""
    for key in ("text", "content"):
        val = part.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def _tool_call_json(blob):
    """Tool-Call-JSON aus einem Reasoning-Feld (Gemini-Quirk) — sonst ''."""
    t = str(blob or "").strip()
    if not t.startswith("{"):
        return ""
    try:
        obj = json.loads(t)
    except Exception:
        return ""
    if isinstance(obj, dict) and obj.get("tool"):
        return json.dumps(obj, ensure_ascii=False)
    return ""


def _message_text(data):
    """Finaler Antworttext aus einer Chat-Completions-Antwort.

    Behandelt auch native OpenAI/OpenRouter `tool_calls` (Gemini liefert bei langem
    Kontext oft `finish_reason: tool_calls` OHNE content-Text): diese werden in das
    JSON-Format `{"tool": ..., "args": ...}` übersetzt, das try_parse_tool_call
    versteht — sonst gingen sie als „leere Antwort“ verloren.

    Gemini 3.x über OpenRouter legt den Nutztext oft in `reasoning` statt
    `content` / `reasoning_content`. Ohne diese Felder stirbt ^_Code mit
    „leere Antwort“, obwohl der HTTP-Call 200 war.
    """
    choice = ((data or {}).get("choices") or [{}])[0] or {}
    msg = choice.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        parts_out = []
        for p in content:
            if not isinstance(p, dict):
                continue
            ptype = str(p.get("type") or "").lower()
            if "encrypted" in ptype:
                continue
            if "reasoning" in ptype:
                # Denkpart: nur Tool-Call-JSON retten, Denkprosa überspringen.
                t = _tool_call_json(p.get("reasoning") or p.get("text") or "")
            else:
                t = _part_text(p)
            if t:
                parts_out.append(t)
        content = "".join(parts_out)
    text = str(content or "").strip()
    if text:
        return text
    # Native tool_calls (finish_reason=tool_calls) -> JSON-Text für den Tool-Loop.
    tcs = msg.get("tool_calls") or []
    if tcs:
        parts = []
        for tc in tcs:
            fn = (tc or {}).get("function") or {}
            name = str(fn.get("name") or "").strip()
            args_raw = fn.get("arguments") or ""
            args = {}
            if isinstance(args_raw, dict):
                args = args_raw
            else:
                try:
                    parsed = json.loads(args_raw) if str(args_raw).strip() else {}
                    args = parsed if isinstance(parsed, dict) else {}
                except Exception:
                    args = {}
            if name:
                parts.append(
                    json.dumps({"tool": name, "args": args}, ensure_ascii=False)
                )
        if parts:
            return "\n".join(parts)
    # Reasoning-Felder: nur Tool-Call-JSON (Gemini-Quirk) — Denkprosa nie als Antwort.
    for blob in (
        msg.get("reasoning_content"),
        msg.get("reasoning"),
        msg.get("thought"),
    ):
        t = _tool_call_json(blob)
        if t:
            return t
    legacy_choice_text = str(choice.get("text") or "").strip()
    if legacy_choice_text:
        return legacy_choice_text
    details = msg.get("reasoning_details") or choice.get("reasoning_details") or []
    if isinstance(details, list):
        harvested = []
        for d in details:
            # Nur Tool-Call-JSON aus Details retten — keine Denkprosa.
            t = _tool_call_json(_part_text(d))
            if t:
                harvested.append(t)
        if harvested:
            return "\n".join(harvested).strip()
    return ""


def _resolve_chat_timeout(timeout=None):
    """Wall-Clock-Sekunden für einen OpenRouter-Chat-Call."""
    if timeout is not None:
        try:
            return max(1, int(timeout))
        except (TypeError, ValueError):
            pass
    # Lokaler Kopf: eigener, laengerer Timeout (siehe config.LOCAL_CHAT_TIMEOUT)
    try:
        from . import factory as _factory

        if getattr(_factory.get_provider("agent"), "provider_name", "") == "local":
            return max(1, int(getattr(_cfg, "LOCAL_CHAT_TIMEOUT", 600) or 600))
    except Exception:
        pass
    mode = getattr(_cfg, "MODE", "agent") or "agent"
    if str(mode).lower() in ("code", "tick"):
        raw = getattr(_cfg, "CODE_CHAT_TIMEOUT", None)
    else:
        raw = getattr(_cfg, "CHAT_TIMEOUT", None)
    if raw is None:
        raw = getattr(_cfg, "CHAT_TIMEOUT", 60)
    try:
        return max(1, int(raw or 60))
    except (TypeError, ValueError):
        return 60


def _content_chars(content):
    """Zeichenzahl für Logging — str oder multimodal list (text + image_url)."""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        n = 0
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                n += len(str(part.get("text") or ""))
            elif part.get("type") == "image_url":
                # Bild zählt pauschal (Base64 nicht voll mitzählen)
                n += 2000
        return n
    return len(str(content))


def user_content_with_images(text, images=None):
    """OpenAI multimodal user content: Text + optionale image_url-Parts.

    images: Liste von {type:'image_url', image_url:{url}} oder rohen
    {mime, data} / {mimeType, data}-Dicts.
    """
    text = text if text is not None else ""
    if not images:
        return text
    parts = []
    t = str(text).strip()
    if t:
        parts.append({"type": "text", "text": str(text)})
    else:
        parts.append({"type": "text", "text": "(Bild angehängt — bitte beschreiben/analysieren.)"})
    for img in images:
        if not isinstance(img, dict):
            continue
        if img.get("type") == "image_url" and isinstance(img.get("image_url"), dict):
            url = img["image_url"].get("url")
            if url:
                parts.append({"type": "image_url", "image_url": {"url": url}})
            continue
        mime = str(img.get("mime") or img.get("mimeType") or "image/png").lower()
        data = img.get("data") or img.get("content") or ""
        if not data:
            continue
        data = str(data).replace("data:" + mime + ";base64,", "")
        if str(data).startswith("data:"):
            # already a data URI
            parts.append({"type": "image_url", "image_url": {"url": str(data)}})
        else:
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{data}"},
            })
    return parts if len(parts) > 1 or (parts and parts[0].get("type") == "image_url") else (text or "")


class OpenRouterProvider(ModelProvider):
    def __init__(self, url=None, model=None, api_key=None, fallback_model=None):
        self.url = (
            url
            or getattr(_cfg, "OPENROUTER_URL", None)
            or os.environ.get("OPENROUTER_URL")
            or "https://openrouter.ai/api/v1"
        )
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        if getattr(_cfg, "MODE", "agent") == "agent":
            default_model = getattr(_cfg, "AGENT_OPENROUTER_MODEL", None) or os.environ.get(
                "AGENT_OPENROUTER_MODEL", "deepseek-v4-flash-vision-exp"
            )
        else:
            default_model = getattr(_cfg, "OPENROUTER_MODEL", None) or os.environ.get(
                "OPENROUTER_MODEL", "deepseek-v4-flash-vision-exp"
            )
        self.model = model or default_model
        # Fallback hinter dem Primärmodell (Direct/OR-Slug → OpenRouter Flash-0731).
        if fallback_model is not None:
            self.fallback_model = fallback_model or None
        elif getattr(_cfg, "MODE", "agent") == "agent":
            raw = getattr(_cfg, "AGENT_OPENROUTER_FALLBACK_MODEL", None)
            if raw is None:
                raw = os.environ.get(
                    "AGENT_OPENROUTER_FALLBACK_MODEL",
                    "deepseek/deepseek-v4-flash-0731",
                )
            self.fallback_model = raw or None
        else:
            raw = getattr(_cfg, "OPENROUTER_FALLBACK_MODEL", None)
            if raw is None:
                raw = os.environ.get(
                    "OPENROUTER_FALLBACK_MODEL",
                    "deepseek/deepseek-v4-flash-0731",
                )
            self.fallback_model = raw or None
        # openrouter | openrouter:free — für Trace / used_model
        self.last_used = None
        self._active_model = self.model

    @property
    def provider_name(self):
        return "openrouter"

    @property
    def model_name(self):
        # Aktuell genutztes Modell nach dem Turn; vorher die Kette.
        if self.last_used == "openrouter:free":
            return self.fallback_model or self.model
        if self.last_used == "openrouter":
            return self.model
        if self.fallback_model and self.fallback_model != self.model:
            return f"{self.model} → {self.fallback_model}"
        return self.model

    def _ensure_key(self):
        if not self.api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY nicht gesetzt (glyph-agent/.env). "
                "Cloud-Modell nicht verfügbar."
            )

    def _chat_completion(
        self, messages, temperature, timeout=None, model=None, url=None, api_key=None
    ):
        """Chat-Completions mit hartem Total-Timeout.

        urlopen(timeout=…) ist nur Socket-Timeout; resp.read() kann trotzdem
        hängen. Deshalb: Request im Worker, future.result(timeout=wall) bricht
        den Caller hart ab (Thread kann nachlaufen bis Socket-Timeout greift).
        url/api_key überschreiben den Provider-Default (Direct → OpenRouter-Hop).
        """
        use_key = (api_key if api_key is not None else self.api_key) or ""
        use_url = (url if url is not None else self.url) or ""
        if not use_key:
            self._ensure_key()
        m = model or self.model
        wall = _resolve_chat_timeout(timeout)
        total_chars = sum(_content_chars(x.get("content")) for x in messages)
        _agent_log.log(
            "cloud_send",
            provider=self.provider_name,
            model=m,
            chars=total_chars,
            n_messages=len(messages),
            timeout=wall,
        )
        payload = {
            "model": m,
            "messages": messages,
            "stream": False,
        }
        # Haiku 5.5 auf api.anthropic.com: "`temperature` is deprecated".
        if not _anthropic_compat(use_url):
            payload["temperature"] = temperature
        # Reasoning-Pflicht-Modelle (z.B. GLM 5.3 Flash) denken ohne max_tokens
        # endlos → 60s+ Latenz. Ein vernünftiges Cap hält Antwort + Tool-JSON
        # möglich, begrenzt aber das Reasoning. Konfigurierbar via MAX_TOKENS.
        max_tokens = getattr(_cfg, "MAX_TOKENS", 0) or 0
        if max_tokens > 0:
            payload["max_tokens"] = max_tokens
        # Reasoning-Modelle (GLM 5.3 Flash, DeepSeek-Reasoner): Denkspur aktiv
        # zügeln — sonst frisst das Reasoning das max_tokens-Budget und die echte
        # Antwort bleibt leer (→ Retry/Fallback statt Antwort). OpenRouter-Norm:
        # reasoning: {effort}. REASONING_EFFORT in .env überschreibt; leer = aus.
        effort = str(os.environ.get("REASONING_EFFORT", "") or "").strip().lower()
        if not effort:
            slug = str(m or "").lower()
            if any(h in slug for h in ("glm", "reasoner", "thinking")):
                effort = "low"
        if effort in ("low", "medium", "high"):
            payload["reasoning"] = {"effort": effort}
        elif effort in ("off", "none", "disabled"):
            payload["reasoning"] = {"enabled": False, "exclude": True}
        req = urllib.request.Request(
            f"{str(use_url).rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {use_key}",
            },
            method="POST",
        )

        box = {"data": None, "err": None}

        def _do_request():
            try:
                # Socket-Timeout ≤ Wall, damit der Worker nicht ewig nachläuft.
                with urllib.request.urlopen(req, timeout=wall) as resp:
                    raw = resp.read()
                box["data"] = json.loads(raw.decode("utf-8"))
            except Exception as e:
                box["err"] = e

        # Daemon-Thread: Total-Timeout per join(deadline); Prozess-Exit wartet nicht.
        # (ThreadPoolExecutor-Worker sind non-daemon und halten Tests/Shutdown auf.)
        worker = threading.Thread(
            target=_do_request,
            name=f"or-chat-{m[:24]}",
            daemon=True,
        )
        worker.start()
        worker.join(timeout=wall)
        if worker.is_alive():
            log.warning(
                "OpenRouter chat total-timeout nach %ss (model=%s)",
                wall, m,
            )
            try:
                _agent_log.log(
                    "cloud_timeout",
                    provider=self.provider_name,
                    model=m,
                    timeout=wall,
                )
            except Exception:
                pass
            raise TimeoutError(
                f"OpenRouter chat timeout nach {wall}s (model={m})"
            )

        if box["err"] is not None:
            e = box["err"]
            if isinstance(e, socket.timeout):
                raise TimeoutError(
                    f"OpenRouter chat timeout nach {wall}s (model={m}): {e}"
                ) from e
            if isinstance(e, urllib.error.HTTPError):
                # Body einbetten, sonst sieht man nur „400 Bad Request“ ohne Grund.
                try:
                    ebody = e.read().decode("utf-8", errors="replace")[:600]
                except Exception:
                    ebody = ""
                log.error(
                    "HTTP %s bei Modell '%s': %s",
                    e.code, m, ebody or e.reason,
                )
                if ebody:
                    raise RuntimeError(
                        f"HTTP {e.code} {e.reason} von Modell '{m}': {ebody}"
                    ) from e
                raise e
            if isinstance(e, urllib.error.URLError):
                reason = getattr(e, "reason", e)
                if isinstance(reason, socket.timeout) or "timed out" in str(e).lower():
                    raise TimeoutError(
                        f"OpenRouter chat timeout nach {wall}s (model={m}): {e}"
                    ) from e
            raise e

        data = box["data"] or {}
        try:
            from ..usage_meter import add_payload
            add_payload(data)
        except Exception:
            pass
        text = _message_text(data)
        # Leere Completion ist KEIN Erfolg: das Modell hat nichts Sinnvolles geliefert
        # (z. B. nur reasoning_content oder leerer content), obwohl der HTTP-Call ok war.
        # Als Fehler klassifizieren, damit _with_free_fallback auf das Fallback-Modell
        # wechselt statt eine hängende/leere Antwort nach oben zu reichen.
        if not text:
            log.warning(
                "Leere Antwort von Modell '%s' (provider=%s) — als Fehler behandelt",
                m, self.provider_name,
            )
            try:
                _agent_log.log(
                    "cloud_empty",
                    provider=self.provider_name,
                    model=m,
                    timeout=wall,
                )
            except Exception:
                pass
            raise RuntimeError(
                f"Modell '{m}' lieferte eine leere Antwort (kein content)"
            )
        return text

    def _with_free_fallback(self, messages, temperature, timeout=None):
        """Primär → Free. Setzt last_used / _active_model.
        Ohne API-Key kein Free-Versuch (gleicher Key, gleicher Fail)."""
        self._ensure_key()
        # Wasserdicht gegen Google-400 „Requests ending with a model turn“:
        # egal welcher Aufrufer (chat/chat_messages/generate), die letzte Message
        # muss user sein. Kopie, damit der Aufrufer-Liste nichts mutiert wird.
        msgs = list(messages or [])
        if msgs and str(msgs[-1].get("role") or "").lower() == "assistant":
            msgs = msgs + [{"role": "user", "content": "Fortfahren."}]
        try:
            text = self._chat_completion(
                msgs, temperature, timeout=timeout, model=self.model
            )
            self.last_used = "openrouter"
            self._active_model = self.model
            return text
        except Exception as e1:
            if not self.fallback_model or self.fallback_model == self.model:
                raise
            log.warning(
                "OpenRouter '%s' fehlgeschlagen (%s) — Free-Modell '%s'",
                self.model, e1, self.fallback_model,
            )
            text = self._chat_completion(
                messages, temperature, timeout=timeout, model=self.fallback_model
            )
            if not text:
                raise RuntimeError(
                    f"Fallback-Modell '{self.fallback_model}' lieferte ebenfalls "
                    "eine leere Antwort"
                ) from e1
            self.last_used = "openrouter:free"
            self._active_model = self.fallback_model
            # Nur bei echten :free-Modellen anhängen — glm-5.3-flash kostet
            # Geld ($0.075/$0.25) und ist nur ein normaler Fallback-Hop.
            if str(self.fallback_model or "").lower().endswith(":free"):
                return text.rstrip() + (
                    f"\n\n_(OpenRouter: kostenloses Modell {self.fallback_model} verwendet.)_"
                )
            return text

    def generate(self, prompt, temperature=0.3, num_ctx=8192, timeout=None):
        return self._with_free_fallback(
            [{"role": "user", "content": prompt}], temperature, timeout=timeout
        )

    def chat(self, system, user, temperature=0.3, num_ctx=8192, timeout=None):
        """Chat. `user` darf str ODER multimodal list (text + image_url) sein."""
        return self._with_free_fallback(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature,
            timeout=timeout,
        )

    def chat_messages(self, messages, temperature=0.3, timeout=None):
        """Echte Multi-Turn-Messages (kein Flatten auf system+user)."""
        return self._with_free_fallback(
            list(messages or []), temperature, timeout=timeout
        )
