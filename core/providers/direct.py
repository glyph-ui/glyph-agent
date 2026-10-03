# -*- coding: utf-8 -*-
"""
DirectProvider — OpenAI-kompatibler Primär-Hop (DeepSeek, Grok, …).

Kette (gegrillt 2026-08-12, Router-Host 2026-09-07):
  1. DIRECT_API_URL + DIRECT_API_KEY (Alias: DEEPSEEK_API_KEY)
     Modell ohne Slash = Direct-ID, z. B. deepseek-v4-flash-vision-exp
  2. Bei Ausfall: Router (OPENROUTER_URL + OPENROUTER_API_KEY)
     Modell mit Slash = Router-Slug. Host default openrouter.ai;
     Orca: https://api.orcarouter.ai/v1

Kein Tiny/Free. Ohne Direct-Key und ohne OpenRouter-Key: harter Fehler.
"""
import logging
import os

from .. import config as _cfg
from .openrouter import OpenRouterProvider

log = logging.getLogger("glyph-agent.direct")


def uses_openrouter_slug(model):
    """OpenRouter-IDs enthalten immer vendor/model. Direct-IDs nicht."""
    return "/" in str(model or "")


def resolve_direct_key():
    return (
        os.environ.get("DIRECT_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
        or ""
    ).strip()


class DirectProvider(OpenRouterProvider):
    def __init__(self, url=None, model=None, api_key=None, fallback_model=None):
        direct_url = (
            url
            or getattr(_cfg, "DIRECT_API_URL", None)
            or os.environ.get("DIRECT_API_URL", "https://api.deepseek.com")
        )
        direct_key = api_key if api_key is not None else resolve_direct_key()
        super().__init__(
            url=direct_url,
            model=model,
            api_key=direct_key,
            fallback_model=fallback_model,
        )
        self._or_url = (
            getattr(_cfg, "OPENROUTER_URL", None)
            or os.environ.get("OPENROUTER_URL")
            or "https://openrouter.ai/api/v1"
        )
        self._or_key = os.environ.get("OPENROUTER_API_KEY", "")

    @property
    def provider_name(self):
        return "direct"

    @property
    def model_name(self):
        if self.last_used == "openrouter":
            return self.fallback_model or self.model
        if self.last_used == "direct":
            return self.model
        if self.fallback_model and self.fallback_model != self.model:
            return f"{self.model} → {self.fallback_model}"
        return self.model

    def _ensure_key(self):
        # Hop-spezifisch in _hop_creds / _with_free_fallback.
        return

    def _hop_creds(self, model):
        """(url, key, hop_name) für dieses Modell."""
        if uses_openrouter_slug(model):
            return self._or_url, self._or_key, "openrouter"
        return self.url, self.api_key, "direct"

    def _with_free_fallback(self, messages, temperature, timeout=None):
        """Direct-Primär → OpenRouter-Fallback. Setzt last_used / _active_model."""
        primary = self.model
        url, key, hop = self._hop_creds(primary)
        # Google-400 „Requests ending with a model turn“: letzte Message muss user sein.
        msgs = list(messages or [])
        if msgs and str(msgs[-1].get("role") or "").lower() == "assistant":
            msgs = msgs + [{"role": "user", "content": "Fortfahren."}]
        last_err = None
        if key:
            try:
                text = self._chat_completion(
                    msgs,
                    temperature,
                    timeout=timeout,
                    model=primary,
                    url=url,
                    api_key=key,
                )
                if not text:
                    raise RuntimeError(
                        f"Modell '{primary}' lieferte eine leere Antwort (kein content)"
                    )
                self.last_used = hop
                self._active_model = primary
                return text
            except Exception as e1:
                last_err = e1
        else:
            last_err = RuntimeError(
                "DIRECT_API_KEY (oder DEEPSEEK_API_KEY) nicht gesetzt"
                if hop == "direct"
                else "OPENROUTER_API_KEY nicht gesetzt"
            )

        fb = self.fallback_model
        if not fb or fb == primary:
            raise last_err
        fb_url, fb_key, fb_hop = self._hop_creds(fb)
        if not fb_key:
            raise RuntimeError(
                f"Primär '{primary}' fehlgeschlagen ({last_err}) und "
                f"Fallback-Hop {fb_hop} ohne Key."
            ) from last_err
        log.warning(
            "Direct-Primär '%s' fehlgeschlagen (%s) — Fallback '%s' via %s",
            primary, last_err, fb, fb_hop,
        )
        text = self._chat_completion(
            msgs,
            temperature,
            timeout=timeout,
            model=fb,
            url=fb_url,
            api_key=fb_key,
        )
        if not text:
            raise RuntimeError(
                f"Fallback-Modell '{fb}' lieferte ebenfalls eine leere Antwort"
            ) from last_err
        self.last_used = fb_hop
        self._active_model = fb
        return text
