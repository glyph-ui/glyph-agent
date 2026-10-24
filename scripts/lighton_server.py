#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LightOnOCR-3 (MLX, Apple Silicon) als lokaler OpenAI-kompatibler Endpunkt.

Dient der Glyph-OCR-Stufe (core/pdf_tools.py, Backend "lighton") und liest
Seitenbilder (PNG) als Markdown mit Tabellenstruktur. Läuft als LaunchAgent
(scripts/lighton-serve.sh → ~/Library/LaunchAgents/com.lxndrhbrt.lighton-ocr.plist).

Endpoints:
  GET  /v1/models                  — Erreichbarkeitsprobe (pdf_tools._lighton_ready)
  POST /v1/chat/completions        — messages mit image_url (data:image/png;base64,…)

Aufruf (manuell):  sh scripts/lighton-serve.sh
Port/Modell:       Env LIGHTON_PORT (Default 8790), LIGHTON_MODEL_PATH
"""
import base64
import json
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_PATH = os.environ.get(
    "LIGHTON_MODEL_PATH",
    os.path.expanduser("~/.glyph/models/lightonocr-3-0.8B-MLX/8bit"),
)
PORT = int(os.environ.get("LIGHTON_PORT", "8790"))
MAX_TOKENS = int(os.environ.get("LIGHTON_MAX_TOKENS", "4000"))

from mlx_vlm import generate, load  # noqa: E402
from mlx_vlm.prompt_utils import apply_chat_template  # noqa: E402
from mlx_vlm.utils import load_config  # noqa: E402

print(f"[lighton] lade {MODEL_PATH} …", flush=True)
_T0 = time.time()
model, processor = load(MODEL_PATH)
config = load_config(MODEL_PATH)
print(f"[lighton] bereit in {time.time() - _T0:.1f}s", flush=True)

LOCK = threading.Lock()  # MLX-Generierung ist nicht thread-safe


def _extract(body):
    """messages → (prompt_text, png_bytes oder None)."""
    prompt_parts = []
    png = None
    for msg in body.get("messages") or []:
        content = msg.get("content")
        if isinstance(content, str):
            prompt_parts.append(content)
            continue
        for part in content or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                prompt_parts.append(str(part.get("text") or ""))
            elif part.get("type") == "image_url":
                url = str(((part.get("image_url") or {}).get("url")) or "")
                if url.startswith("data:") and "," in url:
                    png = base64.b64decode(url.split(",", 1)[1])
    return "\n".join(p for p in prompt_parts if p.strip()), png


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # kurz halten
        pass

    def _send(self, code, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return

    def do_GET(self):
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            self._send(200, {"data": [{"id": "lightonocr-3-0.8b-mlx"}]})
        elif self.path == "/health":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/v1/chat/completions"):
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as e:
            self._send(400, {"error": f"bad request: {e}"})
            return
        prompt, png = _extract(body)
        if png is None:
            self._send(400, {"error": "kein image_url (data:image/png;base64,…) in messages"})
            return
        t0 = time.time()
        with tempfile.TemporaryDirectory(prefix="lighton-") as td:
            img_path = os.path.join(td, "page.png")
            with open(img_path, "wb") as fh:
                fh.write(png)
            try:
                with LOCK:
                    chat_prompt = apply_chat_template(
                        processor, config, prompt or "Convert this page to markdown.",
                        num_images=1,
                    )
                    out = generate(
                        model, processor, chat_prompt, [img_path],
                        max_tokens=MAX_TOKENS, verbose=False,
                    )
            except Exception as e:  # noqa: BLE001
                self._send(500, {"error": f"{type(e).__name__}: {e}"[:300]})
                return
        # mlx_vlm liefert GenerationResult (mit .text); str() wäre "GenerationResult(text=…)"
        text = getattr(out, "text", None) or str(out)
        text = str(text).strip()
        dt = round(time.time() - t0, 2)
        print(f"[lighton] {dt}s {len(text)}c", flush=True)
        self._send(200, {
            "id": "lighton-ocr",
            "object": "chat.completion",
            "model": "lightonocr-3-0.8b-mlx",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"latency_s": dt},
        })


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[lighton] höre auf http://127.0.0.1:{PORT}/v1", flush=True)
    srv.serve_forever()