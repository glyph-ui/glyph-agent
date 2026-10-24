#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vault-Treffer sind da: antworten, nicht Notizen schreiben bis der Schleifenschutz greift."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import pdf_tools, tool_loop

QUERY = "ich benötige eine Betriebsanweisung für Leiter und Tritte, bitte"
PDF = "ASI, BS. UWS, QM, EM/Arbeitssicherheit/Vorlagen/008_Leitern und Tritte.pdf"
MARKER = "STANDSICHERHEIT-VOR-BENUTZUNG-PRUEFEN"


def test_write_ask_detects_save_not_need():
    assert not tool_loop._user_wants_vault_write(QUERY)
    assert not tool_loop._user_wants_vault_write("Beschreibe die Leiterprüfung")
    assert tool_loop._user_wants_vault_write("Bitte erstelle eine Notiz dazu")
    assert tool_loop._user_wants_vault_write("Speichere das im Arbeitsordner")


def _patch(llm, reads):
    old = {
        "local": tool_loop._is_local_provider,
        "llm": tool_loop._call_llm,
        "find": tool_loop._run_vault_find,
        "web": tool_loop._run_web_precheck,
        "pdf": pdf_tools.read_pdf,
    }

    def read_pdf(path, **kwargs):
        reads.append(path)
        return {
            "ok": True,
            "path": path,
            "content": f"Leiter und Tritte. {MARKER}. Auf Schäden prüfen.",
            "chars": 60,
            "truncated": False,
            "engine": "test",
            "error": None,
        }

    tool_loop._is_local_provider = lambda kind="agent": False
    tool_loop._call_llm = llm
    tool_loop._run_vault_find = lambda *a, **k: {
        "status": "empty", "selected": 0, "results": [], "sources": [],
    }
    tool_loop._run_web_precheck = lambda *a, **k: {
        "tool_calls": [], "tool_results": [], "steps": [],
        "history_append": None, "log_key": None, "log_data": {},
    }
    pdf_tools.read_pdf = read_pdf
    return old


def _restore(old):
    tool_loop._is_local_provider = old["local"]
    tool_loop._call_llm = old["llm"]
    tool_loop._run_vault_find = old["find"]
    tool_loop._run_web_precheck = old["web"]
    pdf_tools.read_pdf = old["pdf"]


def _run(llm, query=QUERY, max_rounds=4):
    reads = []
    executed = []
    old_exec = tool_loop.tool_registry.execute

    def execute(name, args, confirm=None):
        executed.append(name)
        return {"ok": True, "result": {"path": (args or {}).get("path"), "content": "x"}}

    old = _patch(llm, reads)
    tool_loop.tool_registry.execute = execute
    try:
        res = tool_loop.run(
            query,
            max_rounds=max_rounds,
            confirm=lambda *_a, **_k: True,
            vault_search=True,
            vault_selected=[{"kind": "file", "path": PDF, "title": "008_Leitern und Tritte.pdf"}],
        )
    finally:
        tool_loop.tool_registry.execute = old_exec
        _restore(old)
    res["_reads"] = reads
    res["_executed"] = executed
    return res


def test_need_ba_answers_instead_of_creating_a_note():
    calls = {"n": 0}

    def llm(messages, extra_system=None, images=None, web_only=False, allow_web=True):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                '{"tool": "CreateNote", "args": {"path": '
                '"HSEQ Sync/Themen/Betriebsanweisung Leitern und Tritte.md", '
                '"content": "neu"}}'
            )
        return f"Die Betriebsanweisung steht in 008. Kern: {MARKER}."

    res = _run(llm)
    assert res["ok"] is True
    assert "CreateNote" not in res["_executed"], res["_executed"]
    assert MARKER in (res.get("answer") or "")
    assert "Zu viele Tool-Runden" not in (res.get("answer") or "")
    assert res["_reads"] == [PDF]


def test_explicit_note_still_writes():
    calls = {"n": 0}

    def llm(messages, extra_system=None, images=None, web_only=False, allow_web=True):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                '{"tool": "CreateNote", "args": {"path": '
                '"HSEQ Sync/Themen/BA Leitern.md", "content": "notiz"}}'
            )
        return "Notiz liegt unter Themen/BA Leitern.md."

    res = _run(llm, query="Bitte erstelle eine Notiz zur Leiterprüfung.")
    assert res["_executed"] == ["CreateNote"], res["_executed"]
    assert res["ok"] is True
    assert "Zu viele Tool-Runden" not in (res.get("answer") or "")


def test_reread_and_round_limit_use_the_loaded_pdf():
    def llm(messages, extra_system=None, images=None, web_only=False, allow_web=True):
        return '{"tool": "ReadPdf", "args": {"path": "%s"}}' % PDF

    res = _run(llm, max_rounds=4)
    assert res["ok"] is True
    assert res["_reads"] == [PDF], res["_reads"]
    assert "ReadPdf" not in res["_executed"], res["_executed"]
    assert MARKER in (res.get("answer") or "")
    assert "Zu viele Tool-Runden" not in (res.get("answer") or "")


def test_read_note_does_not_run_when_the_pdf_is_loaded():
    calls = {"n": 0}

    def llm(messages, extra_system=None, images=None, web_only=False, allow_web=True):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                '{"tool": "ReadNote", "args": {"path": '
                '"HSEQ Sync/Themen/Betriebsanweisung Leitern und Tritte.md"}}'
            )
        return f"Betriebsanweisung aus der Vorlage. {MARKER}. Anstellwinkel 65 bis 75 Grad."

    res = _run(llm)
    assert "ReadNote" not in res["_executed"], res["_executed"]
    assert MARKER in (res.get("answer") or "")
    assert "gezielt nach der Datei" not in (res.get("answer") or "")
    assert calls["n"] == 2


def test_loaded_pdf_keeps_the_prose_instead_of_rereading():
    calls = {"n": 0}
    prose = (
        "Die Betriebsanweisung für Leitern und Tritte steht in der Vorlage 008. "
        "Anstellwinkel 65 bis 75 Grad. Anlegeleitern über 3 m nur mit Quertraverse. "
        "Oberste vier Sprossen nicht betreten. Sichtprüfung vor jeder Nutzung. "
        + MARKER
    )

    def llm(messages, extra_system=None, images=None, web_only=False, allow_web=True):
        calls["n"] += 1
        if calls["n"] == 1:
            return prose
        return (
            '{"tool": "ReadNote", "args": {"path": '
            '"HSEQ Sync/Themen/BA Leitern und Tritte.md"}}'
        )

    res = _run(llm, max_rounds=8)
    assert res["ok"] is True
    assert res["_executed"] == [], res["_executed"]
    assert res.get("answer") == prose
    assert "gezielt nach der Datei" not in (res.get("answer") or "")
    assert calls["n"] == 2


def test_round_limit_quotes_pdf_when_the_model_keeps_calling_tools():
    def llm(messages, extra_system=None, images=None, web_only=False, allow_web=True):
        return '{"tool": "VaultFind", "args": {"query": "leiter"}}'

    res = _run(llm, max_rounds=1)
    assert res["ok"] is True
    assert MARKER in (res.get("answer") or "")
    assert "Zu viele Tool-Runden" not in (res.get("answer") or "")
    assert PDF in (res.get("answer") or "")


if __name__ == "__main__":
    test_write_ask_detects_save_not_need()
    test_need_ba_answers_instead_of_creating_a_note()
    test_explicit_note_still_writes()
    test_reread_and_round_limit_use_the_loaded_pdf()
    test_round_limit_quotes_pdf_when_the_model_keeps_calling_tools()
    print("ok")
