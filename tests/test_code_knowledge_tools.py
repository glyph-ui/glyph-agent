#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests für Stufe 1+2: Wissens-/Reichweiten-Tools in ^_Code (read-only)."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import code_tools, config


class KnowledgeToolsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        import core.vaults_registry as vr

        self.vr = vr
        self._orig_dir = vr.GLYPH_DIR
        self._orig_store = vr.USER_STORE
        self._orig_defaults = vr.DEFAULTS_STORE
        vr.GLYPH_DIR = self.root
        vr.USER_STORE = os.path.join(self.root, "vaults.json")
        vr.DEFAULTS_STORE = os.path.join(self.root, "vaults.defaults.json")
        with open(vr.USER_STORE, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "vaults": []}, f)

    def tearDown(self):
        self.vr.GLYPH_DIR = self._orig_dir
        self.vr.USER_STORE = self._orig_store
        self.vr.DEFAULTS_STORE = self._orig_defaults
        self._tmp.cleanup()

    def _add_vault(self, vid, path, head):
        with open(self.vr.USER_STORE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "version": 1,
                    "vaults": [
                        {
                            "id": vid,
                            "name": vid,
                            "path": path,
                            "mode": "rw",
                            "heads": {"grok": "rw", "agent": "rw", "code": head},
                            "enabled": True,
                        }
                    ],
                },
                f,
            )

    def test_vault_heads_disabled_by_default(self):
        self.assertFalse(code_tools.vault_heads_enabled())

    def test_vault_heads_enabled_when_bound(self):
        self._add_vault("v1", os.path.realpath(self.root), "r")
        self.assertTrue(code_tools.vault_heads_enabled())

    def test_vault_find_blocked_without_head(self):
        r = code_tools.vault_find("audit")
        self.assertFalse(r["ok"])
        self.assertIn("Graph", r["error"])

    def test_vault_find_ok_with_head(self):
        self._add_vault("v1", os.path.realpath(self.root), "r")
        with mock.patch("core.retrieval.vault_find", return_value={"results": [{"title": "x"}]}):
            r = code_tools.vault_find("audit")
        self.assertTrue(r["ok"])
        self.assertEqual(r["hits"], [{"title": "x"}])

    def test_web_search_ok(self):
        with mock.patch("core.web.web_search", return_value=[{"title": "t"}]):
            r = code_tools.web_search("frage")
        self.assertTrue(r["ok"])
        self.assertEqual(r["results"], [{"title": "t"}])

    def test_mail_list_ok(self):
        with mock.patch("core.comm_tools.mail_list", return_value={"ok": True, "envelopes": []}):
            r = code_tools.mail_list()
        self.assertTrue(r["ok"])

    def test_read_memory_ok(self):
        r = code_tools.read_memory("recent")
        # Sollte immer ein ok-Ergebnis liefern (auch ohne Dateien)
        self.assertTrue(r["ok"])
        self.assertIn("content", r)

    def test_permission_allow_readonly(self):
        for name in ("VaultFind", "ReadNote", "WikiGet", "WebSearch", "ExtractUrl", "MailList", "MailRead", "ReadMemory"):
            d = code_tools.permission_decision(name, {})
            self.assertEqual(d["action"], "allow", name)


if __name__ == "__main__":
    unittest.main()


class VaultWriteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        import core.vaults_registry as vr

        self.vr = vr
        self._orig_dir = vr.GLYPH_DIR
        self._orig_store = vr.USER_STORE
        self._orig_defaults = vr.DEFAULTS_STORE
        vr.GLYPH_DIR = self.root
        vr.USER_STORE = os.path.join(self.root, "vaults.json")
        vr.DEFAULTS_STORE = os.path.join(self.root, "vaults.defaults.json")
        self._write_vault(os.path.realpath(self.root), "r")
        from core import code_grants

        code_grants.reset()

    def tearDown(self):
        self.vr.GLYPH_DIR = self._orig_dir
        self.vr.USER_STORE = self._orig_store
        self.vr.DEFAULTS_STORE = self._orig_defaults
        self._tmp.cleanup()

    def _write_vault(self, path, head):
        with open(self.vr.USER_STORE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "version": 1,
                    "vaults": [
                        {
                            "id": "v1",
                            "name": "v1",
                            "path": path,
                            "mode": "rw",
                            "heads": {"grok": "rw", "agent": "rw", "code": head},
                            "enabled": True,
                        }
                    ],
                },
                f,
            )

    def test_vault_write_denied_without_rw(self):
        self._write_vault(os.path.realpath(self.root), "r")
        d = code_tools.permission_decision("VaultWrite", {"path": "x.md", "content": "y"})
        self.assertEqual(d["action"], "deny")
        r = code_tools.vault_write("x.md", "y")
        self.assertFalse(r["ok"])

    def test_vault_write_requires_grant_with_rw(self):
        self._write_vault(os.path.realpath(self.root), "rw")
        d = code_tools.permission_decision("VaultWrite", {"path": "x.md", "content": "y"})
        self.assertEqual(d["action"], "requires_grant")

    def test_vault_write_applies_with_grant(self):
        self._write_vault(os.path.realpath(self.root), "rw")
        from core import code_grants

        code_grants.issue(
            scope="once",
            workspace_root=os.path.realpath(self.root),
            path_prefixes=["/"],
            action_classes=("file_change",),
        )
        d = code_tools.permission_decision("VaultWrite", {"path": "x.md", "content": "hallo"})
        self.assertEqual(d["action"], "allow")
        r = code_tools.vault_write("x.md", "hallo")
        self.assertTrue(r["ok"], r)
        # Datei existiert jetzt
        import os as _os

        self.assertTrue(_os.path.exists(_os.path.join(self.root, "x.md")))
