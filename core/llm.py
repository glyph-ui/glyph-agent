# -*- coding: utf-8 -*-
"""
Modelldienst — schmale Brücke auf die ModelProvider-Schnittstelle.

WICHTIG (Architektur): agent.py und die CLI rufen NUR diese Funktionen auf,
nie direkt einen Provider. Dadurch bleibt das Modell austauschbar, ohne dass
an der Agenten-/Tool-Schicht etwas geändert wird:

    core/llm.chat() / core/llm.generate()
        -> providers.get_provider()  (Direct Vision-Exp → OpenRouter Flash-0731)
            -> konkretes Modell

Die tatsächliche Provider-Implementierung liegt in core/providers/*.
Diese Datei darf KEINE provider-spezifische Logik enthalten.
"""
import contextvars
import inspect
from contextlib import contextmanager

from .providers import factory

# Job-Thread: Cloud-Hop, ohne den Chat-Singleton (oft local) umzuschalten.
_job_cloud = contextvars.ContextVar("glyph_job_cloud", default=None)


@contextmanager
def cloud_job():
    """Dieser Thread nutzt den Hybrid-Cloud-Hop. Andere Threads bleiben unverändert."""
    token = _job_cloud.set(factory.cloud_provider("agent"))
    try:
        yield
    finally:
        _job_cloud.reset(token)


def get_provider(kind="agent"):
    """Liefert den aktiven ModelProvider (für Logs/UI-Info).

    kind="code" → Provider, der für ^_Code aufgelöst wird (CODE_PROVIDER),
    sonst der Agent-Provider (AGENT_PROVIDER). Der Singleton wird pro kind
    gehalten, damit °_Agent und ^_Code getrennte Provider-Modi haben können.
    Innerhalb von cloud_job() liefert der Agent-Kopf den Cloud-Hop, nicht Bonsai.
    """
    override = _job_cloud.get()
    if override is not None and str(kind or "agent").lower() != "code":
        return override
    return factory.get_provider(kind)


def short_model_label(model_id):
    """Nutzerlabel für Think-Steps: Vision-Exp / Pro / Direct-Flash / OpenRouter-Flash."""
    mid = str(model_id or "").strip()
    if not mid:
        return "?"
    low = mid.lower()
    if "vision" in low and "deepseek" in low:
        return "DeepSeek v4 vision"
    if "deepseek-v4-pro" in low:
        return "DeepSeek v4 pro"
    if "deepseek-v4-flash-0731" in low:
        return "OpenRouter v4 flash" if "/" in mid else "DeepSeek v4 flash"
    if "deepseek-v4-flash" in low:
        return "OpenRouter v4 flash" if "/" in mid else "DeepSeek v4 flash"
    if "gemini" in low:
        name = mid.split("/")[-1].split(":")[0]
        rest = name.lower()
        for prefix in ("google-gemini-", "gemini-"):
            if rest.startswith(prefix):
                rest = rest[len(prefix):]
                break
        rest = rest.replace("gemini", "").replace("-", " ").strip()
        return f"Gemini {rest}".strip() if rest else "Gemini"
    name = mid.split("/")[-1]
    return name.split(":")[0] or mid


def thinker_step_detail(kind="agent", model=None):
    """Think-Step-Detail aus Runtime — nie die alte Kette flash-0731 → free."""
    from . import config

    kind = (kind or "agent").lower()
    if kind == "code":
        configured = getattr(config, "CODE_OPENROUTER_MODEL", "deepseek-v4-flash-vision-exp")
        prefix = "^_Code"
    else:
        configured = getattr(config, "AGENT_OPENROUTER_MODEL", "deepseek-v4-flash-vision-exp")
        prefix = "Cloud-Denker"
    # Der lokale Kopf heisst auch so: sonst steht "Cloud-Denker" ueber einer
    # Antwort, die den Rechner nie verlassen hat (Ternary Bonsai 2 27B).
    try:
        if getattr(get_provider(kind), "provider_name", "") == "local":
            prefix = "Lokal-Denker"
    except Exception:
        pass
    used = model
    if not used:
        try:
            p = get_provider(kind)
        except Exception:
            p = None
        last = getattr(p, "last_used", None) if p else None
        active = getattr(p, "_active_model", None) if p else None
        live = getattr(p, "model", None) if p else None
        used = active if (last and active) else (live or configured)
    return f"{prefix} denkt ({short_model_label(used)})"


def _accepts_timeout(provider):
    """True, wenn der Provider ein `timeout`-Argument kennt (alle außer mlx)."""
    try:
        params = inspect.signature(provider.chat).parameters
    except (TypeError, ValueError):
        return False
    return "timeout" in params


def chat(system, user, temperature=0.3, num_ctx=8192, kind="agent", timeout=None):
    """Chat-Aufruf an den aktiven Provider. Liefert Antworttext (str).

    timeout: optionales Wall-Clock-Limit in Sekunden (kurze Hilfs-Calls wie die
    Suchbrücke). Provider ohne `timeout`-Argument werden unverändert aufgerufen.
    """
    provider = get_provider(kind)
    if timeout is None:
        return provider.chat(system, user, temperature, num_ctx)
    if _accepts_timeout(provider):
        return provider.chat(system, user, temperature, num_ctx, timeout=timeout)
    return provider.chat(system, user, temperature, num_ctx)


def generate(prompt, temperature=0.3, num_ctx=8192, kind="agent"):
    """Freier Generierungs-Aufruf an den aktiven Provider. Liefert str."""
    return get_provider(kind).generate(prompt, temperature, num_ctx)
