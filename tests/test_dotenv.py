#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UI-Bindings müssen Router-Host laden — nicht nur Direct-URL/Key."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import dotenv


class LoadUiBindingsRouterUrl(unittest.TestCase):
    def test_overwrites_openrouter_url_from_bindings(self):
        """Graph-Router-Host gewinnt gegen Default/env — analog DIRECT_API_URL."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "bindings.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "keys": {"OPENROUTER_API_KEY": "sk-orca-test"},
                    "settings": {
                        "OPENROUTER_URL": "https://api.orcarouter.ai/v1",
                        "DIRECT_API_URL": "https://api.deepseek.com",
                    },
                },
                f,
            )
        os.environ["OPENROUTER_URL"] = "https://openrouter.ai/api/v1"
        os.environ["DIRECT_API_URL"] = "https://stale.example/v1"
        dotenv.load_ui_bindings(path)
        self.assertEqual(os.environ.get("OPENROUTER_URL"), "https://api.orcarouter.ai/v1")
        self.assertEqual(os.environ.get("DIRECT_API_URL"), "https://api.deepseek.com")


if __name__ == "__main__":
    unittest.main()
