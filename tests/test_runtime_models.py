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


def test_apply_direct_replaces_live_key():
    """Graph-Schreiben muss den Singleton neu bauen — Mutieren reicht nicht."""
    os.environ["DIRECT_API_KEY"] = "old-key-73c9"
    os.environ["DIRECT_API_URL"] = "https://api.deepseek.com"
    os.environ["OPENROUTER_API_KEY"] = "old-or-9199"
    config.PROVIDER = "direct"
    factory.reset_provider()
    stale = factory.get_provider()
    assert stale.provider_name == "direct"
    assert stale.api_key == "old-key-73c9"

    snap = runtime_models.apply_direct(
        {"api_key": "new-key-3e4e", "openrouter_key": "new-or-0001"}
    )
    live = factory.get_provider()
    assert live is not stale
    assert live.api_key == "new-key-3e4e"
    assert os.environ.get("DIRECT_API_KEY") == "new-key-3e4e"
    assert os.environ.get("OPENROUTER_API_KEY") == "new-or-0001"
    assert getattr(live, "_or_key", "") == "new-or-0001"
    assert snap["direct"]["key_set"] is True


def test_provider_alias_normalization():
    assert runtime_models._norm_provider("direct") == "direct"
    assert runtime_models._norm_provider("openrouter") == "openrouter"
    assert runtime_models._norm_provider("hybrid") == "direct"
    assert runtime_models._norm_provider("fallback") == "openrouter"
    assert runtime_models._norm_provider("HyBrId") == "direct"
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
    assert snap["provider"] == "direct"
    assert config.AGENT_PRIMARY_PROVIDER == "direct"
    p = factory.get_provider()
    assert p.provider_name == "direct"


def test_apply_provider_invalid():
    try:
        runtime_models.apply_provider({"provider": "mlx"})
    except ValueError:
        pass
    else:
        raise AssertionError("mlx darf nicht akzeptiert werden")


if __name__ == "__main__":
    test_apply_shared_and_clear_fallback()
    test_code_override()
    test_apply_vision_sets_code_vision_model()
    test_apply_direct_url()
    test_apply_direct_replaces_live_key()
    test_provider_alias_normalization()
    test_apply_provider_openrouter()
    test_apply_provider_hybrid_is_direct()
    test_apply_provider_invalid()
    print("ok")
