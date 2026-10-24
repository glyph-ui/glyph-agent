# -*- coding: utf-8 -*-
"""Claude-kompatibler Direct-Hop: Haiku 5.5 lehnt temperature ab."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.providers.openrouter import OpenRouterProvider


def _ok_response():
    class Resp:
        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": "ok"}}]}
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    return Resp()


class AnthropicPayloadTests(unittest.TestCase):
    def _capture(self, url, model):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            return _ok_response()

        provider = OpenRouterProvider(url=url, model=model, api_key="sk-test")
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            text = provider._chat_completion(
                [{"role": "user", "content": "ping"}],
                temperature=0.4,
                timeout=5,
                model=model,
                url=url,
            )
        self.assertEqual(text, "ok")
        return captured

    def test_anthropic_host_omits_temperature(self):
        captured = self._capture(
            "https://api.anthropic.com/v1", "claude-haiku-5-5"
        )
        self.assertEqual(
            captured["url"], "https://api.anthropic.com/v1/chat/completions"
        )
        self.assertNotIn("temperature", captured["body"])
        self.assertEqual(captured["body"]["model"], "claude-haiku-5-5")

    def test_other_host_keeps_temperature(self):
        captured = self._capture(
            "https://api.deepseek.com", "deepseek-v4-flash-vision-exp"
        )
        self.assertEqual(captured["body"]["temperature"], 0.4)


if __name__ == "__main__":
    unittest.main()
