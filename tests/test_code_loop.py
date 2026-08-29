# -*- coding: utf-8 -*-
"""Unit tests for ^_Code loop policy (trim, Orient, retry, Grep-x3)."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import code_loop, config


class TrimHistoryTests(unittest.TestCase):
    def test_keeps_first_user_and_last_tool(self):
        hist = [
            {"role": "user", "content": "TASK " + ("x" * 100)},
            {"role": "assistant", "content": "grep1"},
            {"role": "user", "content": "OLD RESULT " + ("y" * 400)},
            {"role": "assistant", "content": "grep2"},
            {"role": "user", "content": "LATEST TOOL RESULT unique-tail"},
        ]
        kept = code_loop.trim_code_history(hist, budget=250)
        self.assertEqual(kept[0]["content"][:4], "TASK")
        self.assertIn("unique-tail", kept[-1]["content"])
        self.assertLess(len(kept), len(hist))

    def test_under_budget_unchanged(self):
        hist = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]
        self.assertEqual(code_loop.trim_code_history(hist, 1000), hist)


class OrientExtractTests(unittest.TestCase):
    def test_orient_and_map_only(self):
        md = (
            "# Repo\n\n"
            "## Orient\n1. Read this.\n\n"
            "## System map\n| Node | Tut |\n\n"
            "## Language\nComposer: input\n"
        )
        got = code_loop.extract_orient_map(md)
        self.assertIn("## Orient", got)
        self.assertIn("## System map", got)
        self.assertNotIn("## Language", got)
        self.assertNotIn("Composer:", got)

    def test_real_glyph_agent_context(self):
        path = os.path.join(os.path.dirname(__file__), "..", "CONTEXT.md")
        with open(path, encoding="utf-8") as f:
            got = code_loop.extract_orient_map(f.read())
        self.assertIn("## Orient", got)
        self.assertIn("## System map", got)
        self.assertNotIn("## Language", got)
        self.assertNotIn("Settled decisions", got)


class RetryAndHardStopTests(unittest.TestCase):
    def test_search_replace_miss_is_retryable(self):
        r = {"ok": False, "error": "SearchReplace: old-String nicht gefunden (0 Treffer)"}
        self.assertTrue(code_loop.is_retryable_write_fail("SearchReplace", r))
        self.assertFalse(code_loop.should_hard_stop("SearchReplace", r))

    def test_search_replace_n_hits_retryable(self):
        r = {"ok": False, "error": "SearchReplace: old-String kommt 3× vor — muss exakt 1 Treffer sein"}
        self.assertTrue(code_loop.is_retryable_write_fail("SearchReplace", r))
        self.assertFalse(code_loop.should_hard_stop("SearchReplace", r))

    def test_user_deny_hard(self):
        r = {"ok": False, "error": "Vom Nutzer in Glyph abgelehnt."}
        self.assertTrue(code_loop.should_hard_stop("RunCommand", r))

    def test_shell_timeout_hard(self):
        r = {"ok": True, "result": {"timeout": True, "exit_code": -1}}
        self.assertTrue(code_loop.should_hard_stop("RunCommand", r))

    def test_private_hard(self):
        r = {"ok": False, "error": "Schreiben verboten: Mode private (braucht r+w)"}
        self.assertTrue(code_loop.should_hard_stop("WriteFile", r))


class RepeatKeyTests(unittest.TestCase):
    def test_same_grep_same_key(self):
        a = code_loop.repeat_tool_key("Grep", {"pattern": "foo", "path": "."})
        b = code_loop.repeat_tool_key("Grep", {"pattern": "foo", "path": "."})
        c = code_loop.repeat_tool_key("Grep", {"pattern": "bar", "path": "."})
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_write_not_tracked(self):
        self.assertIsNone(code_loop.repeat_tool_key("WriteFile", {"path": "a.py"}))


class PickTestTests(unittest.TestCase):
    def test_package_json_npm(self):
        d = tempfile.TemporaryDirectory()
        try:
            with open(os.path.join(d.name, "package.json"), "w") as fh:
                fh.write("{}")
            self.assertEqual(code_loop.pick_test_command(d.name), "npm test")
        finally:
            d.cleanup()

    def test_prefers_test_unit_over_full_test(self):
        """glyph-ui `npm test` = unit + smoke. Nach Write nur die Unit-Suite."""
        d = tempfile.TemporaryDirectory()
        try:
            with open(os.path.join(d.name, "package.json"), "w") as fh:
                fh.write(
                    '{"scripts":{"test":"npm run test:unit && npm run smoke",'
                    '"test:unit":"node --test"}}'
                )
            self.assertEqual(code_loop.pick_test_command(d.name), "npm run test:unit")
        finally:
            d.cleanup()

    def test_tests_dir_pytest(self):
        d = tempfile.TemporaryDirectory()
        try:
            os.mkdir(os.path.join(d.name, "tests"))
            self.assertEqual(code_loop.pick_test_command(d.name), "pytest")
        finally:
            d.cleanup()

    def test_empty_none(self):
        d = tempfile.TemporaryDirectory()
        try:
            self.assertIsNone(code_loop.pick_test_command(d.name))
        finally:
            d.cleanup()


class CodeRoleTests(unittest.TestCase):
    def test_role_mentions_einmal_job(self):
        text = code_loop.code_role()
        # Einmal-Job-Regel ist in den AGENTS-§Code (Contract-Snippet) gewandert —
        # nicht mehr doppelt in der Rolle.
        snippet = code_loop._shared_contract_snippet()
        self.assertIn("einmal-job", text + snippet)
        self.assertIn("EINMAL-JOB", text + snippet)

    def test_agent_role_mentions_einmal_job(self):
        from core.tool_loop import _role

        text = _role()
        self.assertIn("einmal-job", text)
        self.assertIn("EINMAL-JOB", text)

    def test_role_follows_live_code_model(self):
        old = getattr(config, "CODE_OPENROUTER_MODEL", None)
        try:
            config.CODE_OPENROUTER_MODEL = "google/gemini-3.7-flash"
            text = code_loop.code_role()
            self.assertIn("Gemini", text)
            self.assertIn("google/gemini-3.7-flash", text)
            self.assertNotIn("DeepSeek V4 Flash (", text)
        finally:
            config.CODE_OPENROUTER_MODEL = old

    def test_role_default_is_vision_exp(self):
        old = getattr(config, "CODE_OPENROUTER_MODEL", None)
        try:
            config.CODE_OPENROUTER_MODEL = "deepseek-v4-flash-vision-exp"
            text = code_loop.code_role()
            self.assertIn("deepseek-v4-flash-vision-exp", text)
            self.assertIn("DeepSeek V4 Vision", text)
        finally:
            config.CODE_OPENROUTER_MODEL = old


class CallCodeLlmModelTests(unittest.TestCase):
    """Hart: Text und Bilder gehen über CODE_OPENROUTER_MODEL, nicht CODE_VISION_MODEL."""

    def _fake(self):
        class Fake:
            model = "old"
            fallback_model = "old-fb"

            def chat_messages(self, payload, **kw):
                self.seen = self.model
                self.seen_fb = self.fallback_model
                return "ok"

        return Fake()

    def test_text_uses_code_primary(self):
        fake = self._fake()
        old_m = getattr(config, "CODE_OPENROUTER_MODEL", None)
        old_v = getattr(config, "CODE_VISION_MODEL", None)
        old_fb = getattr(config, "CODE_OPENROUTER_FALLBACK_MODEL", None)
        old_r = getattr(config, "CODE_RETRIES", None)
        try:
            config.CODE_OPENROUTER_MODEL = "deepseek-v4-flash-vision-exp"
            config.CODE_VISION_MODEL = "SHOULD-NOT-USE"
            config.CODE_OPENROUTER_FALLBACK_MODEL = "deepseek/deepseek-v4-flash-0731"
            config.CODE_RETRIES = 0
            with patch.object(code_loop.llm, "get_provider", return_value=fake):
                text = code_loop._call_code_llm([{"role": "user", "content": "hi"}])
            self.assertEqual(text, "ok")
            self.assertEqual(fake.seen, "deepseek-v4-flash-vision-exp")
            self.assertEqual(fake.seen_fb, "deepseek/deepseek-v4-flash-0731")
        finally:
            config.CODE_OPENROUTER_MODEL = old_m
            config.CODE_VISION_MODEL = old_v
            config.CODE_OPENROUTER_FALLBACK_MODEL = old_fb
            config.CODE_RETRIES = old_r

    def test_images_use_code_primary_not_vision_env(self):
        fake = self._fake()
        old_m = getattr(config, "CODE_OPENROUTER_MODEL", None)
        old_v = getattr(config, "CODE_VISION_MODEL", None)
        old_fb = getattr(config, "CODE_OPENROUTER_FALLBACK_MODEL", None)
        old_r = getattr(config, "CODE_RETRIES", None)
        try:
            config.CODE_OPENROUTER_MODEL = "deepseek-v4-flash-vision-exp"
            config.CODE_VISION_MODEL = "SHOULD-NOT-USE"
            config.CODE_OPENROUTER_FALLBACK_MODEL = "deepseek/deepseek-v4-flash-0731"
            config.CODE_RETRIES = 0
            images = [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,xx"},
                }
            ]
            with patch.object(code_loop.llm, "get_provider", return_value=fake):
                text = code_loop._call_code_llm(
                    [{"role": "user", "content": "siehe Screenshot"}],
                    images=images,
                )
            self.assertEqual(text, "ok")
            self.assertEqual(fake.seen, "deepseek-v4-flash-vision-exp")
            self.assertIsNone(fake.seen_fb)
        finally:
            config.CODE_OPENROUTER_MODEL = old_m
            config.CODE_VISION_MODEL = old_v
            config.CODE_OPENROUTER_FALLBACK_MODEL = old_fb
            config.CODE_RETRIES = old_r


class RecoverAfterEmptyThinkerTests(unittest.TestCase):
    def test_lists_successful_tools(self):
        text = code_loop.recover_answer_from_tools(
            "Modell 'google/gemini-3.7-flash' lieferte eine leere Antwort (kein content)",
            [
                {"tool": "WriteFile", "args": {"path": "scripts/wiki_hygiene.py"}, "ok": True},
                {"tool": "SearchReplace", "args": {"path": "jobs/recurring.json"}, "ok": True},
            ],
        )
        self.assertIsNotNone(text)
        self.assertIn("wiki_hygiene.py", text)
        self.assertIn("recurring.json", text)
        self.assertIn("Arbeit liegt auf Disk", text)
        self.assertNotIn("CODE-Denker fehlgeschlagen", text)

    def test_none_without_successful_tools(self):
        self.assertIsNone(code_loop.recover_answer_from_tools("leer", []))
        self.assertIsNone(
            code_loop.recover_answer_from_tools(
                "leer",
                [{"tool": "ReadFile", "args": {"path": "x"}, "ok": False}],
            )
        )


class ConfigRoundsTests(unittest.TestCase):
    def test_default_at_least_32(self):
        self.assertGreaterEqual(int(config.CODE_MAX_ROUNDS), 32)
        self.assertGreaterEqual(int(getattr(config, "CODE_MESSAGE_CHARS", 0)), 64000)


if __name__ == "__main__":
    unittest.main()
