# -*- coding: utf-8 -*-
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import recurring


class RecurringFinishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_store = recurring.STORE_PATH
        self.old_events = recurring.EVENTS_PATH
        recurring.STORE_PATH = os.path.join(self.tmp.name, "recurring.json")
        recurring.EVENTS_PATH = os.path.join(self.tmp.name, "events.jsonl")

    def tearDown(self):
        recurring.STORE_PATH = self.old_store
        recurring.EVENTS_PATH = self.old_events
        self.tmp.cleanup()

    def test_pass_is_optional_and_preserved(self):
        item = recurring.create_item({
            "title": "Briefing",
            "prompt": "Inbox lesen",
            "schedule": {"kind": "daily", "time": "07:30"},
            "pass": "Ein Slack-Post mit Links, keine Mail gesendet",
        })
        self.assertEqual(item["pass"], "Ein Slack-Post mit Links, keine Mail gesendet")
        loaded = recurring.get_item(item["id"])
        self.assertEqual(loaded["pass"], item["pass"])
        bare = recurring.create_item({
            "title": "Alt",
            "prompt": "HSEQ",
            "schedule": {"kind": "daily", "time": "18:00"},
        })
        self.assertNotIn("pass", bare)

    def test_message_appends_pass_and_leer_rule(self):
        msg = recurring.message_for_run({
            "prompt": "Daily YYYY-MM-DD schreiben",
            "pass": "Daily mit 3-Zeilen-Briefing",
        })
        self.assertIn("Daily ", msg)
        self.assertNotIn("YYYY-MM-DD", msg)
        self.assertIn("Fertig nur wenn: Daily mit 3-Zeilen-Briefing", msg)
        self.assertIn("erste Zeile genau LEER", msg)
        plain = recurring.message_for_run({"prompt": "Nur HSEQ"})
        self.assertEqual(plain, "Nur HSEQ")
        self.assertNotIn("LEER", plain)

    def test_classify_empty_only_with_pass(self):
        self.assertEqual(recurring.classify_run_status(True, "LEER", True), "empty")
        self.assertEqual(recurring.classify_run_status(True, "LEER: nichts", True), "empty")
        self.assertEqual(recurring.classify_run_status(True, "LEER", False), "ok")
        self.assertEqual(recurring.classify_run_status(True, "", True), "error")
        self.assertEqual(recurring.classify_run_status(True, "Daily geschrieben", True), "ok")
        self.assertEqual(recurring.classify_run_status(False, "LEER", True), "error")
        bare = '{"tool": "ReadNote", "args": {"path": "x.md"}}'
        self.assertTrue(recurring.answer_is_bare_tool(bare))
        self.assertEqual(recurring.classify_run_status(True, bare, False), "error")
        self.assertFalse(recurring.answer_is_bare_tool("HSEQ: keine Änderungen."))

    def test_eingang_empty_needs_folder_and_log(self):
        phrase = "HSEQ: keine Änderungen.\n\nBegründung: keine Belege."
        self.assertFalse(recurring.eingang_reads_done([]))
        self.assertFalse(recurring.eingang_reads_done([
            {"tool": "ListVaultDir", "ok": True, "args": {"path": "00 Arbeitsfluss/Eingang"}},
        ]))
        self.assertTrue(recurring.eingang_reads_done([
            {"tool": "ListVaultDir", "ok": True, "args": {"path": "00 Arbeitsfluss/Eingang"}},
            {"tool": "ReadNote", "ok": True, "args": {"path": "00 Arbeitsfluss/Fertig/Verarbeitungslog.md"}},
        ]))
        self.assertFalse(recurring.eingang_reads_done([
            {"tool": "ListVaultDir", "ok": True, "canceled": "step-repeat",
             "args": {"path": "00 Arbeitsfluss/Eingang"}},
            {"tool": "ReadNote", "ok": True, "args": {"path": "00 Arbeitsfluss/Fertig/Verarbeitungslog.md"}},
        ]))
        calls = [
            {"tool": "ListVaultDir", "ok": True, "args": {"path": "00 Arbeitsfluss/Eingang"}},
        ]
        note = recurring.tool_loop._step_repeat("ListVaultDir", {"path": "00 Arbeitsfluss/Eingang"}, calls, True)
        self.assertIn("Nicht wiederholen", note or "")
        same = recurring.tool_loop._step_repeat(
            "ReadNote",
            {"path": "00 Arbeitsfluss/Fertig/Verarbeitungslog.md"},
            [{"tool": "ReadNote", "ok": True, "args": {"path": "00 Arbeitsfluss/Fertig/Verarbeitungslog.md"}}],
            True,
        )
        self.assertIn("schon gelesen", same or "")
        self.assertIsNone(recurring.tool_loop._step_repeat("FileHash", {"path": "a.md"}, calls, True))
        self.assertIn(phrase.split("\n", 1)[0], phrase)

    def test_cloud_job_hides_local_singleton(self):
        from core import llm
        from core.providers.factory import _singletons
        from core.providers.local import LocalProvider
        saved = _singletons.get("agent")
        _singletons["agent"] = LocalProvider()
        try:
            self.assertEqual(llm.get_provider("agent").provider_name, "local")
            with llm.cloud_job():
                self.assertNotEqual(llm.get_provider("agent").provider_name, "local")
                self.assertFalse(recurring.tool_loop._is_local_provider())
            self.assertEqual(llm.get_provider("agent").provider_name, "local")
        finally:
            if saved is None:
                _singletons.pop("agent", None)
            else:
                _singletons["agent"] = saved

    def test_empty_run_stamps_and_is_not_ok(self):
        item = recurring.create_item({
            "title": "Briefing",
            "prompt": "Inbox",
            "schedule": {"kind": "daily", "time": "00:00"},
            "pass": "Ein Post",
        })
        recurring._set_run_result(item["id"], "empty", "LEER", "2026-08-23")
        loaded = recurring.get_item(item["id"])
        self.assertEqual(loaded["last_status"], "empty")
        self.assertEqual(loaded["last_stamp"], "2026-08-23")

    def test_max_rounds_for_item(self):
        self.assertEqual(recurring.max_rounds_for({}), 24)
        self.assertEqual(recurring.max_rounds_for({"max_rounds": 12}), 12)
        self.assertEqual(recurring.max_rounds_for({"max_rounds": 99}), 24)
        self.assertEqual(recurring.max_rounds_for({"max_rounds": 0}), 24)

    def test_normalize_keeps_max_rounds(self):
        item = recurring._normalize_item({
            "id": "td-x",
            "title": "X",
            "prompt": "Y",
            "schedule": {"kind": "daily", "time": "18:00"},
            "max_rounds": 12,
        })
        self.assertEqual(item["max_rounds"], 12)


if __name__ == "__main__":
    unittest.main()
