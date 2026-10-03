#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for runtime model hot-apply."""
import os
import sys
import importlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Ensure key so provider can construct (no network in apply_models)
os.environ.setdefault("OPENROUTER_API_KEY", "test-key-not-real")
os.environ.setdefault("AGENT_PRIMARY_PROVIDER", "openrouter")
os.environ.setdefault("MODE", "agent")

from core.providers import factory
from core import config
import core.runtime_models as runtime_models


def test_apply_shared_and_clear_fallback():
    factory.reset_provider()
    importlib.reload(config)
    importlib.reload(factory)
    importlib.reload(runtime_models)

    snap = runtime_models.apply_models(
        {
            "shared": {
                "primary": "deepseek/deepseek-v4-flash-0731",
                "fallback": "inclusionai/ling-3.0-tiny:free",
            }
        }
    )
    assert snap["shared"]["primary"] == "deepseek/deepseek-v4-flash-0731"
    assert snap["shared"]["fallback"] == "inclusionai/ling-3.0-tiny:free"
    assert snap["code"]["primary"] == "deepseek/deepseek-v4-flash-0731"

    snap2 = runtime_models.apply_models(
        {
            "shared": {
                "primary": "deepseek/deepseek-v4-flash-0731",
                "fallback": "",
            }
        }
    )
    assert snap2["shared"]["fallback"] is None
    p = factory.get_provider()
    assert p.model == "deepseek/deepseek-v4-flash-0731"
    assert not p.fallback_model


def test_code_override():
    factory.reset_provider()
    snap = runtime_models.apply_models(
        {
            "shared": {"primary": "a/primary", "fallback": "a/fb"},
            "code": {"primary": "b/code", "fallback": ""},
        }
    )
    assert snap["shared"]["primary"] == "a/primary"
    assert snap["code"]["primary"] == "b/code"
    assert snap["code"]["fallback"] is None
    assert snap["code"]["override"] is True
    assert config.CODE_OPENROUTER_MODEL == "b/code"
    assert config.CODE_VISION_MODEL == "b/code"


def test_apply_vision_sets_code_vision_model():
    factory.reset_provider()
    snap = runtime_models.apply_models(
        {
            "shared": {
                "primary": "deepseek-v4-flash-vision-exp",
                "fallback": "deepseek/deepseek-v4-flash-0731",
            },
            "code": {
                "primary": "deepseek-v4-flash-vision-exp",
                "fallback": "",
            },
        }
    )
    assert snap["shared"]["primary"] == "deepseek-v4-flash-vision-exp"
    assert snap["code"]["primary"] == "deepseek-v4-flash-vision-exp"
    assert config.CODE_OPENROUTER_MODEL == "deepseek-v4-flash-vision-exp"
    assert config.CODE_VISION_MODEL == "deepseek-v4-flash-vision-exp"
    assert os.environ.get("CODE_VISION_MODEL") == "deepseek-v4-flash-vision-exp"


def test_apply_direct_url():
    factory.reset_provider()
    snap = runtime_models.apply_direct({"url": "https://api.deepseek.com/"})
    assert config.DIRECT_API_URL == "https://api.deepseek.com"
    assert snap["direct"]["url"] == "https://api.deepseek.com"


def test_apply_direct_openrouter_url():
    """Router-Host (OpenRouter oder Orca) muss den Slash-Hop umbiegen."""
    os.environ["DIRECT_API_KEY"] = "sk-direct-test"
    os.environ["OPENROUTER_API_KEY"] = "sk-orca-test"
    os.environ["OPENROUTER_URL"] = "https://openrouter.ai/api/v1"
    config.AGENT_PROVIDER = "direct"
    config.CODE_PROVIDER = "direct"
    config.PROVIDER = "direct"
    config.AGENT_PRIMARY_PROVIDER = "direct"
    factory.reset_provider()
    stale = factory.get_provider("agent")
    assert getattr(stale, "_or_url", "") == "https://openrouter.ai/api/v1"

    snap = runtime_models.apply_direct(
        {"openrouter_url": "https://api.orcarouter.ai/v1"}
    )
    live = factory.get_provider("agent")
    assert live is not stale
    assert os.environ.get("OPENROUTER_URL") == "https://api.orcarouter.ai/v1"
    assert config.OPENROUTER_URL == "https://api.orcarouter.ai/v1"
    assert getattr(live, "_or_url", "") == "https://api.orcarouter.ai/v1"
    assert (snap.get("direct") or {}).get("openrouter_url") == "https://api.orcarouter.ai/v1"


def test_apply_direct_replaces_live_key():
    """Graph-Schreiben muss den Singleton neu bauen — Mutieren reicht nicht."""
    os.environ["DIRECT_API_KEY"] = "old-key-73c9"
    os.environ["DIRECT_API_URL"] = "https://api.deepseek.com"
    os.environ["OPENROUTER_API_KEY"] = "old-or-9199"
    config.AGENT_PROVIDER = "direct"
    config.CODE_PROVIDER = "direct"
    config.PROVIDER = "direct"
    config.AGENT_PRIMARY_PROVIDER = "direct"
    factory.reset_provider()
    stale = factory.get_provider("agent")
    assert stale.provider_name == "direct"
    assert stale.api_key == "old-key-73c9"

    snap = runtime_models.apply_direct(
        {"api_key": "new-key-3e4e", "openrouter_key": "new-or-0001"}
    )
    live = factory.get_provider("agent")
    assert live is not stale
    assert live.api_key == "new-key-3e4e"
    assert os.environ.get("DIRECT_API_KEY") == "new-key-3e4e"
    assert os.environ.get("OPENROUTER_API_KEY") == "new-or-0001"
    assert getattr(live, "_or_key", "") == "new-or-0001"
    assert snap["direct"]["key_set"] is True


def test_provider_alias_normalization():
    assert runtime_models._norm_provider("direct") == "direct"
    assert runtime_models._norm_provider("openrouter") == "openrouter"
    assert runtime_models._norm_provider("hybrid") == "hybrid"
    assert runtime_models._norm_provider("fallback") == "openrouter"
    assert runtime_models._norm_provider("HyBrId") == "hybrid"
    assert runtime_models._norm_provider("DeePSeek") == "deepseek"


def test_apply_provider_openrouter():
    factory.reset_provider()
    importlib.reload(config)
    importlib.reload(factory)
    importlib.reload(runtime_models)
    snap = runtime_models.apply_provider({"provider": "openrouter"})
    assert snap["provider"] == "openrouter"
    assert config.AGENT_PRIMARY_PROVIDER == "openrouter"
    assert config.PROVIDER == "openrouter"
    p = factory.get_provider()
    assert p.provider_name == "openrouter"


def test_apply_provider_hybrid_is_direct():
    factory.reset_provider()
    snap = runtime_models.apply_provider({"provider": "hybrid"})
    assert snap["provider_mode"] == "hybrid"
    # Effektiver Hop hängt von der Uhrzeit ab (Peak→openrouter, sonst direct)
    assert snap["provider"] in ("direct", "openrouter")
    p = factory.get_provider()
    assert p.provider_name in ("direct", "openrouter")


def test_provider_invalid():
    try:
        runtime_models.apply_provider({"provider": "mlx"})
    except ValueError:
        pass
    else:
        raise AssertionError("mlx darf nicht akzeptiert werden")


def test_hybrid_peak_hop():
    """Hybrid + Peak-Fenster (simuliert) → effektiver Hop = openrouter."""
    old = os.environ.get("HYBRID_PEAK_WINDOWS")
    try:
        os.environ["HYBRID_PEAK_WINDOWS"] = (
            '{"utc": [[0, 24]], "weekdays": [0,1,2,3,4,5,6]}'
        )
        factory.reset_provider()
        snap = runtime_models.apply_provider({"provider": "hybrid"})
        p = factory.get_provider("agent")
        assert snap["provider"] == "openrouter", snap["provider"]
        assert snap["provider_peak"] is True
        assert p.provider_name == "openrouter"
    finally:
        if old is None:
            os.environ.pop("HYBRID_PEAK_WINDOWS", None)
        else:
            os.environ["HYBRID_PEAK_WINDOWS"] = old
        factory.reset_provider()


def test_provider_per_kind():
    """Agent und Code können getrennte Provider-Modi haben."""
    factory.reset_provider()
    # Agent → openrouter, Code → hybrid
    runtime_models.apply_provider({"provider": "openrouter", "kind": "agent"})
    runtime_models.apply_provider({"provider": "hybrid", "kind": "code"})
    assert config.AGENT_PROVIDER == "openrouter"
    assert config.CODE_PROVIDER == "hybrid"
    snap = runtime_models.current_models_snapshot()
    assert snap["provider_mode"] == "openrouter"
    assert snap["code_provider_mode"] == "hybrid"
    # Effektive Hops
    assert snap["provider"] == "openrouter"
    assert snap["code_provider"] in ("direct", "openrouter")
    # Singletons getrennt
    pa = factory.get_provider("agent")
    pc = factory.get_provider("code")
    assert pa.provider_name == "openrouter"
    assert pc.provider_name in ("direct", "openrouter")


def test_provider_code_only_keeps_agent():
    """Nur Code wechseln lässt Agent-Modus unverändert."""
    factory.reset_provider()
    runtime_models.apply_provider({"provider": "openrouter", "kind": "agent"})
    runtime_models.apply_provider({"provider": "direct", "kind": "code"})
    assert config.AGENT_PROVIDER == "openrouter"
    assert config.CODE_PROVIDER == "direct"
    snap = runtime_models.current_models_snapshot()
    assert snap["provider_mode"] == "openrouter"
    assert snap["code_provider_mode"] == "direct"


def test_probe_direct_id_hits_direct_host_when_provider_is_router():
    """Graph-Pille Direkt: Testen darf nicht Orca anrufen."""
    os.environ["OPENROUTER_URL"] = "https://api.orcarouter.ai/v1"
    os.environ["OPENROUTER_API_KEY"] = "sk-orca-test"
    os.environ["DIRECT_API_URL"] = "https://api.deepseek.com"
    os.environ["DIRECT_API_KEY"] = "sk-direct-test"
    config.OPENROUTER_URL = "https://api.orcarouter.ai/v1"
    config.DIRECT_API_URL = "https://api.deepseek.com"
    config.AGENT_PROVIDER = "openrouter"
    config.PROVIDER = "openrouter"
    config.AGENT_PRIMARY_PROVIDER = "openrouter"
    factory.reset_provider()
    p = factory.get_provider("agent")
    assert p.provider_name == "openrouter"
    captured = {}

    def fake(messages, temperature, timeout=None, model=None, url=None, api_key=None):
        captured["model"] = model
        captured["url"] = url if url is not None else p.url
        captured["api_key"] = api_key if api_key is not None else p.api_key
        return "ok"

    p._chat_completion = fake
    result = runtime_models.probe_model("deepseek-v4-flash-vision-exp")
    assert result["ok"] is True
    assert captured["model"] == "deepseek-v4-flash-vision-exp"
    assert "orcarouter" not in str(captured["url"])
    assert "deepseek.com" in str(captured["url"])
    assert captured["api_key"] == "sk-direct-test"


def test_probe_slash_id_hits_router_host():
    os.environ["OPENROUTER_URL"] = "https://api.orcarouter.ai/v1"
    os.environ["OPENROUTER_API_KEY"] = "sk-orca-test"
    os.environ["DIRECT_API_URL"] = "https://api.deepseek.com"
    os.environ["DIRECT_API_KEY"] = "sk-direct-test"
    config.OPENROUTER_URL = "https://api.orcarouter.ai/v1"
    config.DIRECT_API_URL = "https://api.deepseek.com"
    config.AGENT_PROVIDER = "direct"
    config.PROVIDER = "direct"
    config.AGENT_PRIMARY_PROVIDER = "direct"
    factory.reset_provider()
    p = factory.get_provider("agent")
    captured = {}

    def fake(messages, temperature, timeout=None, model=None, url=None, api_key=None):
        captured["url"] = url if url is not None else p.url
        captured["api_key"] = api_key if api_key is not None else p.api_key
        captured["model"] = model
        return "ok"

    p._chat_completion = fake
    result = runtime_models.probe_model("z-ai/glm-5.3-flash")
    assert result["ok"] is True
    assert captured["model"] == "z-ai/glm-5.3-flash"
    assert "orcarouter.ai" in str(captured["url"])
    assert captured["api_key"] == "sk-orca-test"


if __name__ == "__main__":
    test_apply_shared_and_clear_fallback()
    test_code_override()
    test_apply_vision_sets_code_vision_model()
    test_apply_direct_url()
    test_apply_direct_openrouter_url()
    test_apply_direct_replaces_live_key()
    test_provider_alias_normalization()
    test_apply_provider_openrouter()
    test_apply_provider_hybrid_is_direct()
    test_provider_invalid()
    test_hybrid_peak_hop()
    test_provider_per_kind()
    test_provider_code_only_keeps_agent()
    test_probe_direct_id_hits_direct_host_when_provider_is_router()
    test_probe_slash_id_hits_router_host()
    print("ok")
