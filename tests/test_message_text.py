#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenRouter _message_text: Gemini reasoning / native tool_calls.

Kernregel (2026-08-30): Denkspuren (reasoning/thought) werden NIEMALS als
Antworttext durchgereicht. Einzige Ausnahme: Tool-Call-JSON im reasoning-Feld
(Gemini-Quirk) wird gerettet. Gedanke ≠ Antwort.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.providers.openrouter import _message_text


class MessageTextTests(unittest.TestCase):
    def test_plain_content(self):
        data = {"choices": [{"message": {"content": "Hallo"}}]}
        self.assertEqual(_message_text(data), "Hallo")

    def test_gemini_reasoning_tool_json_still_rescued(self):
        data = {
            "choices": [{
                "message": {
                    "content": "",
                    "reasoning": '{"tool": "ReadFile", "args": {"path": "CONTEXT.md"}}',
                }
            }]
        }
        text = _message_text(data)
        self.assertIn("ReadFile", text)
        self.assertIn("CONTEXT.md", text)

    def test_reasoning_prose_never_as_answer(self):
        """Denkprosa im reasoning-Feld darf NICHT als Antwort erscheinen."""
        data = {
            "choices": [{
                "message": {
                    "content": "",
                    "reasoning": "Der Nutzer fragt, ob ich Speech-Funktionen kann. "
                                 "Ich sollte prüfen, ob Ollama läuft. Moment, ich prüfe...",
                }
            }]
        }
        self.assertEqual(_message_text(data), "")

    def test_thought_prose_never_as_answer(self):
        data = {
            "choices": [{
                "message": {
                    "content": "",
                    "thought": "Plan: erst Grep, dann lesen, dann antworten.",
                }
            }]
        }
        self.assertEqual(_message_text(data), "")

    def test_reasoning_content_prose_never_as_answer(self):
        data = {
            "choices": [{
                "message": {
                    "content": "",
                    "reasoning_content": "Der Nutzer will X. Ich überlege Y.",
                }
            }]
        }
        self.assertEqual(_message_text(data), "")

    def test_gemini_content_parts_reasoning(self):
        data = {
            "choices": [{
                "message": {
                    "content": [
                        {"type": "reasoning", "reasoning": '{"tool": "Grep", "args": {"pattern": "x"}}'},
                        {"type": "text", "text": ""},
                    ]
                }
            }]
        }
        self.assertIn("Grep", _message_text(data))

    def test_mixed_parts_reasoning_prose_plus_text(self):
        """Gedanke + Antwort in Parts: nur der Antworttext kommt durch."""
        data = {
            "choices": [{
                "message": {
                    "content": [
                        {"type": "reasoning", "reasoning": "Ich überlege gerade laut..."},
                        {"type": "text", "text": "Die Antwort ist 42."},
                    ]
                }
            }]
        }
        self.assertEqual(_message_text(data), "Die Antwort ist 42.")

    def test_mixed_parts_thought_inside_text_part_is_kept(self):
        """text-Parts mit sichtbarem Denken sind Modell-Antwort — nicht unsere Baustelle."""
        data = {
            "choices": [{
                "message": {
                    "content": [
                        {"type": "text", "text": "Antwort."},
                    ]
                }
            }]
        }
        self.assertEqual(_message_text(data), "Antwort.")

    def test_native_tool_calls(self):
        data = {
            "choices": [{
                "message": {
                    "content": "",
                    "tool_calls": [{
                        "function": {
                            "name": "WriteFile",
                            "arguments": json.dumps({"path": "a.py", "content": "x"}),
                        }
                    }],
                }
            }]
        }
        text = _message_text(data)
        obj = json.loads(text)
        self.assertEqual(obj["tool"], "WriteFile")
        self.assertEqual(obj["args"]["path"], "a.py")

    def test_empty(self):
        self.assertEqual(_message_text({"choices": [{"message": {"content": ""}}]}), "")
        self.assertEqual(_message_text({}), "")

    def test_skips_encrypted_reasoning_details(self):
        data = {
            "choices": [{
                "message": {
                    "content": "",
                    "reasoning_details": [
                        {"type": "reasoning.encrypted", "data": "abcd"},
                    ],
                }
            }]
        }
        self.assertEqual(_message_text(data), "")

    def test_choice_text_legacy_still_works(self):
        data = {"choices": [{"message": {"content": ""}, "text": "Legacy-Antwort"}]}
        self.assertEqual(_message_text(data), "Legacy-Antwort")


if __name__ == "__main__":
    unittest.main()
