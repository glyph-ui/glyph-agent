# -*- coding: utf-8 -*-
"""
Provider-Factory — wählt den aktiven Modell-Provider anhand der Konfiguration.

Der Rest des Codes (Agent, CLI) bezieht den Provider NUR über get_provider()
und kennt nie die konkrete Klasse. Damit bleibt der Modellaustausch zentral
(in core/config.py gesteuert).

Chat-Provider: direct | openrouter | fallback.
Kein lokaler Chat-Provider.
"""
from .. import config
from .direct import DirectProvider
from .mlx import MLXProvider
from .openrouter import OpenRouterProvider
from .fallback import FallbackProvider

def _config_fallback(*attrs, default="deepseek/deepseek-v4-flash-0731"):
    """Leerer String aus Hot-Apply = kein Fallback; sonst Config-Wert oder Default."""
    for name in attrs:
        if not hasattr(config, name):
            continue
        raw = getattr(config, name)
        if raw == "":
            return None
        if raw:
            return raw
    return default


def _make_direct():
    return DirectProvider(
        model=getattr(config, "AGENT_OPENROUTER_MODEL", None) or "deepseek-v4-flash-vision-exp",
        fallback_model=_config_fallback("AGENT_OPENROUTER_FALLBACK_MODEL"),
    )


def _resolve_hybrid_provider():
    """Hybrid: Direct primär, OpenRouter als Reserve.

    Zeitbewusst: während konfigurierter Peak-Fenster (Standard: DeepSeek
    Mo–Fr 01–04 & 06–10 UTC) wird OpenRouter als Primär genommen, weil
    Direct dort teurer ist als der OpenRouter-Preis. Außerhalb → Direct.
    """
    try:
        from .. import peak as _peak

        if _peak.is_peak():
            return _make_openrouter()
    except Exception:
        pass
    return _make_direct()


def _make_openrouter():
    mode = str(getattr(config, "MODE", "agent") or "agent").lower()
    if mode == "openrouter-chat":
        primary = getattr(config, "OPENROUTER_MODEL", None) or "deepseek-v4-flash-vision-exp"
        fb = _config_fallback("OPENROUTER_FALLBACK_MODEL")
    else:
        primary = (
            getattr(config, "AGENT_OPENROUTER_MODEL", None)
            or getattr(config, "OPENROUTER_MODEL", None)
            or "deepseek-v4-flash-vision-exp"
        )
        fb = _config_fallback(
            "AGENT_OPENROUTER_FALLBACK_MODEL", "OPENROUTER_FALLBACK_MODEL"
        )
    return OpenRouterProvider(model=primary, fallback_model=fb)


_PROVIDERS = {
    "mlx": lambda: MLXProvider(),
    "direct": _make_direct,
    "openrouter": _make_openrouter,
    "hybrid": _resolve_hybrid_provider,
    "fallback": lambda: FallbackProvider(),
}

_singletons = {}


def _resolve_provider_name(kind="agent"):
    """Provider-Name für einen Kopf (agent|code)."""
    try:
        from .. import runtime_models as _rm

        return _rm._provider_for(kind)
    except Exception:
        pass
    name = getattr(config, "PROVIDER", "hybrid") or "hybrid"
    if name == "ollama":
        name = "direct"
    return name


def get_provider(kind="agent"):
    """
    Liefert den aktiven ModelProvider (Singleton pro kind).
    Auswahl über config.PROVIDER / CODE_PROVIDER ('direct' | 'openrouter' | 'hybrid' | 'mlx').
    Unbekannt → direct (B+-Default).
    """
    kind = str(kind or "agent").lower()
    if kind not in ("agent", "code"):
        kind = "agent"
    if _singletons.get(kind) is None:
        name = _resolve_provider_name(kind)
        # Veralteter Name "ollama" als Chat-Provider: hart auf direct umbiegen.
        if name == "ollama":
            name = "direct"
        factory = _PROVIDERS.get(name)
        if factory is None:
            raise ValueError(f"Unbekannte Provider-Konfiguration: {name}")
        _singletons[kind] = factory()
    return _singletons[kind]


def reset_provider():
    """Setzt die Singletons zurück (Tests / Provider-Wechsel zur Laufzeit)."""
    _singletons.clear()
