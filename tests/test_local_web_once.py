#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lokal: eine Web-Suche, nur Exa. Zweite Web-Aktion wird nicht ausgeführt."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import tool_loop


def test_precheck_exa_and_no_second_search():
    calls = []
    answers = {"n": 0}

    def web_search(query, count=5, source="both", include_domains=None):
        calls.append({"query": query, "count": count, "source": source})
        return [{"title": "ISO", "url": "https://example.com/iso", "snippet": "Frist"}]

    def llm(messages, extra_system=None, images=None, web_only=False, allow_web=True):
        answers["n"] += 1
        if answers["n"] == 1:
            return '{"tool": "WebSearch", "args": {"query": "iso nochmal", "source": "both"}}'
        return "Zwei Sätze. Quelle: https://example.com/iso."

    tool_loop.web.web_search = web_search
    tool_loop._is_local_provider = lambda kind="agent": True
    tool_loop._call_llm = llm
    tool_loop._run_vault_find = lambda *a, **k: {
        "status": "empty", "selected": 0, "results": [], "sources": [],
    }

    res = tool_loop.run(
        "Was kostet der gesetzliche Mindestlohn aktuell?",
        max_rounds=3,
        vault_search=False,
    )
    assert calls == [{"query": calls[0]["query"], "count": 3, "source": "exa"}], calls
    assert len(calls) == 1
    assert res["ok"] is True
    assert "example.com/iso" in (res.get("answer") or "")
    web_tools = [t for t in res["tool_calls"] if t["tool"] == "WebSearch"]
    assert len(web_tools) == 2
    assert web_tools[1].get("ok") is False


def test_recherche_request_triggers_exa():
    assert tool_loop._local_wants_public_web("Recherchiere das kurz.")
    assert tool_loop._local_wants_public_web("Such mir den aktuellen Stand.")
    assert not tool_loop._local_wants_public_web("Beschreibe den Unterschied zwischen Apfel und Birne.")

    calls = []

    def web_search(query, count=5, source="both", include_domains=None):
        calls.append({"count": count, "source": source})
        return [{"title": "B", "url": "https://example.com/b", "snippet": "läuft lokal"}]

    def llm(messages, extra_system=None, images=None, web_only=False, allow_web=True):
        return "Läuft lokal. Quelle: https://example.com/b."

    tool_loop.web.web_search = web_search
    tool_loop._is_local_provider = lambda kind="agent": True
    tool_loop._call_llm = llm
    res = tool_loop.run(
        "Recherchiere, wie das lokal läuft.",
        max_rounds=2,
        vault_search=False,
    )
    assert calls == [{"count": 3, "source": "exa"}], calls
    assert res["ok"] is True


def test_local_plain_topic_does_not_search():
    calls = []

    def web_search(query, count=5, source="both", include_domains=None):
        calls.append(source)
        return [{"title": "x", "url": "https://example.com/x", "snippet": "x"}]

    def llm(messages, extra_system=None, images=None, web_only=False, allow_web=True):
        if not getattr(llm, "seen", False):
            llm.seen = True
            return '{"tool": "WebSearch", "args": {"query": "nochmal"}}'
        return "Direkt, ohne Quelle."

    llm.seen = False
    tool_loop.web.web_search = web_search
    tool_loop._is_local_provider = lambda kind="agent": True
    tool_loop._call_llm = llm
    res = tool_loop.run(
        "Beschreibe den Unterschied zwischen Apfel und Birne.",
        max_rounds=3,
        vault_search=False,
    )
    assert calls == [], calls
    assert res["ok"] is True
    assert "Direkt" in (res.get("answer") or "")


def test_third_hit_keeps_full_snippet():
    tool_loop._is_local_provider = lambda kind="agent": True
    marker = "DRITTER-TREFFER-ENDE"
    rows = []
    for i in range(3):
        tail = marker if i == 2 else f"ende-{i}"
        rows.append({
            "title": f"Titel {i}",
            "url": f"https://example.com/{i}",
            "snippet": ("x" * (tool_loop.LOCAL_WEB_SNIPPET_CHARS - len(tail))) + tail,
        })
    text = tool_loop._local_web_history(rows, "exa")
    assert marker in text, text[-240:]
    assert "nicht die erste URL" in text
    assert "gekuerzt" not in text


if __name__ == "__main__":
    test_precheck_exa_and_no_second_search()
    test_recherche_request_triggers_exa()
    test_local_plain_topic_does_not_search()
    test_third_hit_keeps_full_snippet()
    print("ok")
