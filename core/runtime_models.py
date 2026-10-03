# -*- coding: utf-8 -*-
"""
Runtime model config — hot-apply OpenRouter primary/fallback without process restart.

Source of truth for UI-driven changes is glyph-ui bindings; this module mutates
in-process config + the live provider so the next /chat uses the new IDs.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

from . import config
from . import llm
from .providers import factory


def _norm_id(value: Any) -> str:
    return str(value or "").strip()


def _norm_fallback(value: Any, *, present: bool) -> Optional[str]:
    """Empty string / whitespace → no fallback (None). Missing handled by caller."""
    if not present:
        return None
    s = _norm_id(value)
    return s or None


PROVIDER_ALIASES = {
    "direct": "direct",
    "openrouter": "openrouter",
    "fallback": "openrouter",  # alter Alias: nur OpenRouter
    "hybrid": "hybrid",        # zeitbewusst: Direct ↔ OpenRouter
    "local": "local",          # lokaler Server (Ternary Bonsai 2 27B)
}


def _norm_provider(value: Any) -> str:
    """Normalize a provider id to a factory-known name (direct|openrouter|hybrid)."""
    raw = _norm_id(value).lower()
    return PROVIDER_ALIASES.get(raw, raw)


def _provider_for(kind: str) -> str:
    """Gewählter Provider-Modus für einen Kopf (agent|code).

    Reihenfolge: CODE_PROVIDER / AGENT_PROVIDER → globaler PROVIDER → Default.
    """
    if kind == "code":
        raw = _norm_id(getattr(config, "CODE_PROVIDER", None))
        if raw:
            return _norm_provider(raw)
    else:
        raw = _norm_id(getattr(config, "AGENT_PROVIDER", None))
        if raw:
            return _norm_provider(raw)
    raw = _norm_id(getattr(config, "AGENT_PRIMARY_PROVIDER", None))
    if raw:
        return _norm_provider(raw)
    raw = _norm_id(getattr(config, "PROVIDER", "hybrid"))
    return _norm_provider(raw)


def effective_provider(kind: str = "agent") -> str:
    """Effektiver Hop für einen Kopf (agent|code) zum aktuellen Zeitpunkt.

    hybrid → direct (off-peak) oder openrouter (Peak).
    direct/openrouter → unverändert.
    """
    mode = _provider_for(kind)
    if mode != "hybrid":
        return mode
    try:
        from . import peak as _peak

        if _peak.is_peak():
            return "openrouter"
    except Exception:
        pass
    return "direct"


def active_provider() -> str:
    """Current configured provider mode (direct|openrouter|hybrid) — Agent-Kopf."""
    return _provider_for("agent")


def _is_peak_now() -> bool:
    """True, wenn gerade ein Peak-Fenster läuft (für Status/UI)."""
    try:
        from . import peak as _peak

        return bool(_peak.is_peak())
    except Exception:
        return False


def apply_provider(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Hot-apply the active provider mode + rebuild the singleton.

    Body shape:
      {"provider": "direct" | "openrouter" | "hybrid" | "fallback",
       "kind": "agent" | "code"}   # kind optional, Default agent

    Semantics:
      direct     → Direct-API (Primary) ohne Fallback
      openrouter → nur OpenRouter
      hybrid     → Direct primär, OpenRouter als Fallback  (Default)
      fallback   → Alias für openrouter

    kind=agent → setzt AGENT_PROVIDER (+ globalen PROVIDER als Fallback)
    kind=code  → setzt CODE_PROVIDER (Code-Kopf unabhängig)
    Rebuilds the provider singleton so the next /chat uses the new hop.
    """
    body = payload if isinstance(payload, dict) else {}
    raw = _norm_id(body.get("provider") or body.get("mode"))
    if not raw:
        return current_models_snapshot()
    mode = _norm_provider(raw)
    if mode not in ("direct", "openrouter", "hybrid", "local"):
        raise ValueError(
            f"Unbekannter Provider: {raw!r} "
            "(erlaubt: direct, openrouter, hybrid, local, fallback)"
        )
    kind = _norm_id(body.get("kind") or "agent").lower()
    if kind == "code":
        config.CODE_PROVIDER = mode
        _set_env("CODE_PROVIDER", mode)
    else:
        config.AGENT_PROVIDER = mode
        config.AGENT_PRIMARY_PROVIDER = mode
        config.PROVIDER = mode
        _set_env("AGENT_PROVIDER", mode)
        _set_env("AGENT_PRIMARY_PROVIDER", mode)
    factory.reset_provider()
    try:
        llm.get_provider(kind)
    except Exception:
        pass
    return current_models_snapshot()


def current_models_snapshot() -> Dict[str, Any]:
    """Structured model state for /health and apply responses."""
    agent_primary = _norm_id(
        getattr(config, "AGENT_OPENROUTER_MODEL", None)
        or getattr(config, "OPENROUTER_MODEL", None)
    )
    agent_fb_raw = getattr(config, "AGENT_OPENROUTER_FALLBACK_MODEL", None)
    if agent_fb_raw is None:
        agent_fb_raw = getattr(config, "OPENROUTER_FALLBACK_MODEL", None)
    # Explicit empty string from hot-apply = no fallback (not "unset")
    agent_fb = _norm_id(agent_fb_raw) or None

    code_primary = _norm_id(getattr(config, "CODE_OPENROUTER_MODEL", None)) or agent_primary
    code_fb_raw = getattr(config, "CODE_OPENROUTER_FALLBACK_MODEL", None)
    if code_fb_raw is None and not hasattr(config, "CODE_OPENROUTER_FALLBACK_MODEL"):
        code_fb = agent_fb
    elif code_fb_raw is None:
        # attribute missing vs set to None
        code_fb = agent_fb
    else:
        code_fb = _norm_id(code_fb_raw) or None

    code_overrides = (
        code_primary != agent_primary
        or (code_fb or None) != (agent_fb or None)
    )

    p = None
    try:
        p = llm.get_provider()
    except Exception:
        p = None

    live_primary = _norm_id(getattr(p, "model", None)) if p else agent_primary
    live_fb = None
    if p is not None:
        live_fb = _norm_id(getattr(p, "fallback_model", None)) or None

    return {
        "shared": {
            "primary": agent_primary or live_primary,
            "fallback": agent_fb,
        },
        "code": {
            "primary": code_primary,
            "fallback": code_fb,
            "override": code_overrides,
        },
        "active": {
            "primary": live_primary or agent_primary,
            "fallback": live_fb if live_fb is not None else agent_fb,
            "provider": getattr(p, "provider_name", None) if p else None,
            "label": getattr(p, "model_name", None) if p else None,
        },
        "provider": effective_provider("agent"),
        "provider_mode": _provider_for("agent"),
        "provider_peak": _is_peak_now(),
        "code_provider": effective_provider("code"),
        "code_provider_mode": _provider_for("code"),
        "code_model": code_primary,
        "code_fallback_model": code_fb,
        "direct": {
            "url": getattr(config, "DIRECT_API_URL", None)
            or os.environ.get("DIRECT_API_URL")
            or "https://api.deepseek.com",
            "openrouter_url": getattr(config, "OPENROUTER_URL", None)
            or os.environ.get("OPENROUTER_URL")
            or "https://openrouter.ai/api/v1",
            "key_set": bool(
                os.environ.get("DIRECT_API_KEY", "").strip()
                or os.environ.get("DEEPSEEK_API_KEY", "").strip()
            ),
        },
    }


def _set_env(name: str, value: Optional[str]) -> None:
    if value is None or value == "":
        # Keep key present as empty so code that reads env gets "no fallback"
        os.environ[name] = ""
    else:
        os.environ[name] = value


def apply_models(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Apply shared (+ optional code) model pair from a JSON body.

    Body shape:
      {
        "shared": { "primary": "...", "fallback": "" },
        "code":   { "primary": "", "fallback": "" }   # empty primary → use shared
      }

    Empty fallback ⇒ no fallback (None). Missing code block ⇒ code uses shared.
    """
    body = payload if isinstance(payload, dict) else {}
    shared = body.get("shared") if isinstance(body.get("shared"), dict) else {}
    # Flat shortcuts
    if not shared and body.get("primary"):
        shared = {
            "primary": body.get("primary"),
            "fallback": body.get("fallback"),
        }

    primary = _norm_id(shared.get("primary") or body.get("model"))
    if not primary:
        raise ValueError("shared.primary ist Pflicht (Model-ID)")

    # fallback key: if present (even empty) honor it; if absent keep previous? Spec: UI always sends.
    # Prefer explicit: if "fallback" in shared use it; elif top-level; else no fallback when applying full shared.
    if "fallback" in shared:
        fallback = _norm_fallback(shared.get("fallback"), present=True)
    elif "fallback" in body:
        fallback = _norm_fallback(body.get("fallback"), present=True)
    else:
        fallback = None

    code_in = body.get("code") if isinstance(body.get("code"), dict) else None
    if code_in is None or not _norm_id(code_in.get("primary")):
        code_primary = primary
        code_fallback = fallback
    else:
        code_primary = _norm_id(code_in.get("primary"))
        if "fallback" in code_in:
            code_fallback = _norm_fallback(code_in.get("fallback"), present=True)
        else:
            code_fallback = None

    # Mutate config module (code_loop / tool_loop read these)
    config.AGENT_OPENROUTER_MODEL = primary
    config.AGENT_OPENROUTER_FALLBACK_MODEL = fallback or ""
    config.OPENROUTER_MODEL = primary
    config.OPENROUTER_FALLBACK_MODEL = fallback or ""
    config.CODE_OPENROUTER_MODEL = code_primary
    config.CODE_OPENROUTER_FALLBACK_MODEL = code_fallback or ""
    config.CODE_VISION_MODEL = code_primary
    _set_env("CODE_VISION_MODEL", code_primary)

    _set_env("AGENT_OPENROUTER_MODEL", primary)
    _set_env("AGENT_OPENROUTER_FALLBACK_MODEL", fallback)
    _set_env("OPENROUTER_MODEL", primary)
    _set_env("OPENROUTER_FALLBACK_MODEL", fallback)
    _set_env("CODE_OPENROUTER_MODEL", code_primary)
    _set_env("CODE_OPENROUTER_FALLBACK_MODEL", code_fallback)

    # Live provider (agent/openrouter-chat path)
    try:
        p = llm.get_provider("agent")
    except Exception:
        factory.reset_provider()
        p = llm.get_provider("agent")

    if p is not None:
        p.model = primary
        p.fallback_model = fallback
        if hasattr(p, "_active_model"):
            p._active_model = primary
        if hasattr(p, "last_used"):
            p.last_used = None

    # Code-Singleton (falls schon erzeugt) ebenfalls aktualisieren
    try:
        pc = llm.get_provider("code")
        if pc is not None:
            pc.model = code_primary
            pc.fallback_model = code_fallback
            if hasattr(pc, "_active_model"):
                pc._active_model = code_primary
            if hasattr(pc, "last_used"):
                pc.last_used = None
    except Exception:
        pass

    return current_models_snapshot()


def apply_direct(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Hot-apply Direct URL/Key. Rebuilds the provider singleton."""
    body = payload if isinstance(payload, dict) else {}
    url = _norm_id(body.get("url") or body.get("DIRECT_API_URL"))
    if url:
        config.DIRECT_API_URL = url.rstrip("/")
        _set_env("DIRECT_API_URL", config.DIRECT_API_URL)

    key_present = any(k in body for k in ("api_key", "DIRECT_API_KEY", "key"))
    if key_present:
        raw = body.get("api_key")
        if raw is None:
            raw = body.get("DIRECT_API_KEY")
        if raw is None:
            raw = body.get("key")
        key = _norm_id(raw)
        _set_env("DIRECT_API_KEY", key or None)
        if not key:
            os.environ.pop("DIRECT_API_KEY", None)

    or_present = any(
        k in body for k in ("openrouter_key", "OPENROUTER_API_KEY")
    )
    if or_present:
        raw_or = body.get("openrouter_key")
        if raw_or is None:
            raw_or = body.get("OPENROUTER_API_KEY")
        or_key = _norm_id(raw_or)
        _set_env("OPENROUTER_API_KEY", or_key or None)
        if not or_key:
            os.environ.pop("OPENROUTER_API_KEY", None)

    or_url = _norm_id(
        body.get("openrouter_url")
        or body.get("OPENROUTER_URL")
        or body.get("router_url")
    )
    if or_url:
        config.OPENROUTER_URL = or_url.rstrip("/")
        _set_env("OPENROUTER_URL", config.OPENROUTER_URL)

    factory.reset_provider()
    try:
        llm.get_provider("agent")
    except Exception:
        pass
    try:
        llm.get_provider("code")
    except Exception:
        pass

    snap = current_models_snapshot()
    snap["direct"] = {
        "url": getattr(config, "DIRECT_API_URL", None),
        "openrouter_url": getattr(config, "OPENROUTER_URL", None)
        or os.environ.get("OPENROUTER_URL")
        or "https://openrouter.ai/api/v1",
        "key_set": bool(
            os.environ.get("DIRECT_API_KEY", "").strip()
            or os.environ.get("DEEPSEEK_API_KEY", "").strip()
        ),
    }
    return snap


def hop_creds_for_model(model_id: str, hop: Optional[str] = None) -> Tuple[str, str, str]:
    """UI-Vertrag: ohne Slash = Direct-Host+Key, mit Slash = Router.

    Der globale Provider-Modus (direct|openrouter|hybrid) gilt für Chat,
    nicht für den Test-Button. Returns (url, key, hop_name).
    """
    from .providers.direct import resolve_direct_key, uses_openrouter_slug

    mid = _norm_id(model_id)
    forced = _norm_provider(hop) if hop else ""
    if forced in ("direct", "openrouter"):
        hop_name = forced
    else:
        hop_name = "openrouter" if uses_openrouter_slug(mid) else "direct"
    if hop_name == "openrouter":
        url = (
            getattr(config, "OPENROUTER_URL", None)
            or os.environ.get("OPENROUTER_URL")
            or "https://openrouter.ai/api/v1"
        )
        key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
        return url, key, "openrouter"
    url = (
        getattr(config, "DIRECT_API_URL", None)
        or os.environ.get("DIRECT_API_URL")
        or "https://api.deepseek.com"
    )
    return url, resolve_direct_key(), "direct"


def probe_model(
    model_id: str,
    timeout: int = 45,
    kind: str = "agent",
    hop: Optional[str] = None,
) -> Dict[str, Any]:
    """Ping genau diese Modell-ID auf dem Hop der Slash-Regel.

    Ohne Slash → Direct. Mit Slash → Router. Unabhängig vom Live-Provider.
    """
    mid = _norm_id(model_id)
    if not mid:
        raise ValueError("model ist Pflicht")

    url, key, hop_name = hop_creds_for_model(mid, hop)
    if not key:
        raise RuntimeError(
            "DIRECT_API_KEY nicht gesetzt"
            if hop_name == "direct"
            else "OPENROUTER_API_KEY nicht gesetzt"
        )

    p = llm.get_provider(kind)
    if getattr(p, "provider_name", "") not in ("openrouter", "fallback", "direct"):
        raise RuntimeError(f"Probe nur für Direct/OpenRouter, aktiv: {getattr(p, 'provider_name', '?')}")

    old_model = getattr(p, "model", None)
    old_fb = getattr(p, "fallback_model", None)
    old_active = getattr(p, "_active_model", None)
    old_last = getattr(p, "last_used", None)
    try:
        p.model = mid
        p.fallback_model = None  # probe exact model only
        if hasattr(p, "last_used"):
            p.last_used = None
        # Prefer raw completion to avoid free-fallback side effects
        if hasattr(p, "_chat_completion"):
            text = p._chat_completion(
                [
                    {"role": "system", "content": "Reply with exactly: ok"},
                    {"role": "user", "content": "ping"},
                ],
                temperature=0,
                timeout=timeout,
                model=mid,
                url=url,
                api_key=key,
            )
        else:
            text = p.chat(
                "Reply with exactly: ok",
                "ping",
                temperature=0,
                timeout=timeout,
            )
        return {
            "ok": True,
            "model": mid,
            "hop": hop_name,
            "preview": (text or "")[:200],
        }
    finally:
        if old_model is not None:
            p.model = old_model
        p.fallback_model = old_fb
        if hasattr(p, "_active_model") and old_active is not None:
            p._active_model = old_active
        if hasattr(p, "last_used"):
            p.last_used = old_last
