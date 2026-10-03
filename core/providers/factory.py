# -*- coding: utf-8 -*-
"""
Provider-Factory — wählt den aktiven Modell-Provider anhand der Konfiguration.

Der Rest des Codes (Agent, CLI) bezieht den Provider NUR über get_provider()
und kennt nie die konkrete Klasse. Damit bleibt der Modellaustausch zentral
(in core/config.py gesteuert).

Chat-Provider: direct | openrouter | fallback | local.
"local" = lokaler OpenAI-Server (Ternary Bonsai 2 27B), bewusst ohne Cloud-Fallback.
"""
from .. import config
from .direct import DirectProvider
from .local import LocalProvider
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


def _make_direct(kind="agent"):
    if str(kind or "agent").lower() == "code":
        return DirectProvider(
            model=getattr(config, "CODE_OPENROUTER_MODEL", None) or "deepseek-v4-flash-vision-exp",
            fallback_model=_config_fallback("CODE_OPENROUTER_FALLBACK_MODEL"),
        )
    return DirectProvider(
        model=getattr(config, "AGENT_OPENROUTER_MODEL", None) or "deepseek-v4-flash-vision-exp",
        fallback_model=_config_fallback("AGENT_OPENROUTER_FALLBACK_MODEL"),
    )


def _resolve_hybrid_provider(kind="agent"):
    """Hybrid: Direct primär, OpenRouter als Reserve.

    Zeitbewusst: während konfigurierter Peak-Fenster (Standard: DeepSeek
    Mo–Fr 01–04 & 06–10 UTC) wird OpenRouter als Primär genommen, weil
    Direct dort teurer ist als der OpenRouter-Preis. Außerhalb → Direct.
    """
    try:
        from .. import peak as _peak

        if _peak.is_peak():
            return _make_openrouter(kind)
    except Exception:
        pass
    return _make_direct(kind)


def _or_slug_or(primary, fallback):
    """OpenRouter-tauglicher Primary-Slug.

    Im openrouter-Modus MUSS das Modell ein OR-Slug sein (vendor/model).
    Eine Direct-ID ohne Slash (deepseek-v4-flash-vision-exp) kann OpenRouter
    nicht ansprechen → dann das Fallback-Modell als Primary nehmen, wenn es
    ein OR-Slug ist. Sonst: Primary unverändert (führt zu klarem Fehler).
    """
    p = str(primary or "").strip()
    fb = str(fallback or "").strip()
    if p and "/" in p:
        return p
    if fb and "/" in fb:
        return fb
    return p or "deepseek/deepseek-v4-flash-0731"


def _make_openrouter(kind="agent"):
    mode = str(getattr(config, "MODE", "agent") or "agent").lower()
    kind = str(kind or "agent").lower()
    if mode == "openrouter-chat":
        primary = getattr(config, "OPENROUTER_MODEL", None) or "deepseek-v4-flash-vision-exp"
        fb = _config_fallback("OPENROUTER_FALLBACK_MODEL")
    elif kind == "code":
        # Code-Kopf hat EIGENE Runtime-Modelle (CODE_OPENROUTER_MODEL) —
        # vorher bekam er stillschweigend das Agent-Paar (Bug: DS-V4V-Primary
        # als Direct-ID auf OpenRouter → 404 → stiller glm-Fallback).
        primary = (
            getattr(config, "CODE_OPENROUTER_MODEL", None)
            or "deepseek-v4-flash-vision-exp"
        )
        fb = _config_fallback("CODE_OPENROUTER_FALLBACK_MODEL", "OPENROUTER_FALLBACK_MODEL")
    else:
        primary = (
            getattr(config, "AGENT_OPENROUTER_MODEL", None)
            or getattr(config, "OPENROUTER_MODEL", None)
            or "deepseek-v4-flash-vision-exp"
        )
        fb = _config_fallback(
            "AGENT_OPENROUTER_FALLBACK_MODEL", "OPENROUTER_FALLBACK_MODEL"
        )
    # Nur-OpenRouter-Modus: Direct-ID auf OR-Slug mappen, sonst 404/400.
    if mode != "openrouter-chat":
        mapped = _or_slug_or(primary, fb)
        if mapped != primary:
            primary = mapped
            fb = None
    return OpenRouterProvider(model=primary, fallback_model=fb)


_PROVIDERS = {
    "mlx": lambda: MLXProvider(),
    "local": lambda kind="agent": LocalProvider(),
    "direct": _make_direct,
    "openrouter": lambda kind="agent": _make_openrouter(kind),
    "hybrid": lambda kind="agent": _resolve_hybrid_provider(kind),
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


def cloud_provider(kind="agent"):
    """Hybrid-Hop für Jobs. Nie local, nie der Chat-Singleton."""
    return _resolve_hybrid_provider(kind)


def get_provider(kind="agent"):
    """
    Liefert den aktiven ModelProvider (Singleton pro kind).
    Auswahl über config.PROVIDER / CODE_PROVIDER ('direct' | 'openrouter' | 'hybrid' | 'local' | 'mlx').
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
        try:
            _singletons[kind] = factory(kind)
        except TypeError:
            _singletons[kind] = factory()
    return _singletons[kind]


def reset_provider():
    """Setzt die Singletons zurück (Tests / Provider-Wechsel zur Laufzeit)."""
    _singletons.clear()
