# -*- coding: utf-8 -*-
"""
LocalProvider — lokaler OpenAI-kompatibler Server (Ternary Bonsai 2 27B).

Kein Cloud-Anteil: derselbe Rechner, 127.0.0.1. Nutzt die Request-Logik des
DirectProviders (identisches Protokoll), meldet sich aber als "local" und hat
ABSICHTLICH keinen Fallback — ein lokaler Fehler darf die Anfrage niemals
stillschweigend in die Cloud schicken.

Den Server selbst startet/stoppt Glyph (~/.local/bonsai2/bonsai-server);
dieser Provider setzt nur den Aufruf ab.
"""
from .. import config
from .direct import DirectProvider


class LocalProvider(DirectProvider):
    def __init__(self, model=None, url=None, api_key=None):
        super().__init__(
            url=url or getattr(config, "LOCAL_API_URL", "http://127.0.0.1:8080/v1"),
            model=model or getattr(config, "LOCAL_MODEL", "bonsai-2-27b-uncensored"),
            api_key=api_key or getattr(config, "LOCAL_API_KEY", "local"),
            # Leerstring statt None: sonst zieht OpenRouterProvider.__init__
            # den globalen Cloud-Fallback (openrouter.py:248) und eine lokale
            # Anfrage koennte in die Cloud abfliessen.
            fallback_model="",
        )

    @property
    def provider_name(self):
        return "local"

    @property
    def model_name(self):
        return self.model
