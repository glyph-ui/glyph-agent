#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memory-wiki immer; Arbeits-Vault nur mit Apfel."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import vault_scope
from core import research


class WikiPathTests(unittest.TestCase):
    def test_index_path(self):
        roots = ["/Users/x/ObsidianVaults/memory-wiki"]
        self.assertTrue(vault_scope.path_in_roots("/memory-wiki/concepts/glyph.md", roots))
        self.assertFalse(vault_scope.path_in_roots("/HSEQ Sync/Themen/PSA.md", roots))
        self.assertFalse(vault_scope.path_in_roots("/ASI, BS. UWS, QM, EM/Arbeitssicherheit/x.md", roots))

    def test_abs_path(self):
        root = os.path.realpath("/tmp/memory-wiki")
        self.assertTrue(
            vault_scope.path_in_roots(os.path.join(root, "WIKI.md"), [root])
        )

    def test_empty_roots_match_nothing(self):
        self.assertFalse(vault_scope.path_in_roots("/memory-wiki/x.md", []))
        self.assertFalse(vault_scope.path_in_roots("/memory-wiki/x.md", None))

    def test_wiki_roots_skips_work_vault(self):
        from core import config

        old = list(config.VAULT_PATHS)
        try:
            config.VAULT_PATHS = [
                "/tmp/HSEQ Sync",
                "/tmp/memory-wiki",
                "/tmp/ASI, BS. UWS, QM, EM",
            ]
            roots = vault_scope.wiki_roots()
            self.assertEqual(roots, ["/tmp/memory-wiki"])
        finally:
            config.VAULT_PATHS = old


class ResearchPolicyTests(unittest.TestCase):
    def test_open_web_without_apple(self):
        t = research.policy_prompt_snippet("open")
        self.assertIn("TinyFish", t)
        self.assertIn("Exa", t)
        self.assertIn("soziale", t.lower())
        self.assertIn("allgemeine", t.lower())
        self.assertNotIn("Nicht den KomNet", t)
        self.assertIn("Stufe 1", t)
        self.assertIn("komnet.nrw.de", t)

    def test_apple_keeps_komnet_dguv_not_social(self):
        t = research.policy_prompt_snippet("apple")
        self.assertIn("TinyFish", t)
        self.assertIn("Exa", t)
        self.assertIn("KomNet", t)
        self.assertIn("DGUV", t)
        self.assertIn("keine allgemeine", t.lower())

    def test_jobs_default_still_exa_tinyfish(self):
        t = research.policy_prompt_snippet()
        self.assertIn("TinyFish", t)
        self.assertIn("Exa", t)


if __name__ == "__main__":
    unittest.main()
