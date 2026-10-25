#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests: ReadNote und ListVaultDir sperren Privat-Vaults (Registry mode=private),
auch wenn der Vault-Name nicht in BLOCKED_DIRS steht (Peniel, HSEQ Sync).
Auch Dateinamen gehen nicht an das Modell.

Kein Netzwerk. Temporäre Mini-Vaults, Registry per patch.
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import vault_tools, config


class PrivateVaultAccessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = os.path.realpath(self._tmp.name)
        self.priv = os.path.join(base, "Peniel")
        self.open = os.path.join(base, "Offen")
        for root in (self.priv, self.open):
            os.makedirs(root)
            with open(os.path.join(root, "notiz.md"), "w", encoding="utf-8") as f:
                f.write("Inhalt")
        self._old_paths = list(config.VAULT_PATHS)
        config.VAULT_PATHS = [self.priv, self.open]
        self._p_priv = patch("core.vaults_registry.private_paths", return_value=[self.priv])
        self._p_rw = patch("core.vaults_registry.writable_paths", return_value=[self.open])
        self._p_priv.start()
        self._p_rw.start()

    def tearDown(self):
        self._p_rw.stop()
        self._p_priv.stop()
        config.VAULT_PATHS = self._old_paths
        self._tmp.cleanup()

    def test_private_vault_name_not_in_blocklist(self):
        self.assertFalse(vault_tools._is_blocked("Peniel/notiz.md"))

    def test_read_note_private_vault_denied(self):
        with self.assertRaises(PermissionError) as ctx:
            vault_tools.read_note("Peniel/notiz.md")
        self.assertIn("Privat-Vault", str(ctx.exception))

    def test_read_note_private_vault_abs_denied(self):
        with self.assertRaises(PermissionError):
            vault_tools.read_note(os.path.join(self.priv, "notiz.md"))

    def test_read_note_open_vault_ok(self):
        note = vault_tools.read_note("Offen/notiz.md")
        self.assertEqual(note["content"], "Inhalt")

    def test_list_private_vault_denied(self):
        for p in ("Peniel", "/Peniel", self.priv):
            res = vault_tools.list_vault_dir(p)
            self.assertEqual(res["status"], "error", p)
            self.assertEqual(res["entries"], [], p)
            self.assertIn("Privat-Vault", res["error"], p)

    def test_list_private_subdir_denied(self):
        os.makedirs(os.path.join(self.priv, "Fertig"))
        res = vault_tools.list_vault_dir("Peniel/Fertig")
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["entries"], [])

    def test_list_root_overview_skips_private_vault(self):
        res = vault_tools.list_vault_dir(".")
        self.assertEqual(res["status"], "success")
        vaults = {e["vault"] for e in res["entries"]}
        self.assertIn("Offen", vaults)
        self.assertNotIn("Peniel", vaults)

    def test_read_note_code_head_keeps_access(self):
        note = vault_tools.read_note("Peniel/notiz.md", head="code")
        self.assertEqual(note["content"], "Inhalt")

    def test_read_pdf_private_vault_denied(self):
        from core import pdf_tools

        with open(os.path.join(self.priv, "bericht.pdf"), "wb") as f:
            f.write(b"%PDF-1.4\n")
        with self.assertRaises(PermissionError):
            pdf_tools._resolve_pdf_path("Peniel/bericht.pdf")

    def test_index_hit_filter(self):
        from core import vault_scope

        names = vault_scope.private_vault_names()
        self.assertIn("peniel", names)
        self.assertTrue(vault_scope.is_private_hit("/Peniel/Fertig/x.md", names))
        self.assertTrue(vault_scope.is_private_hit("Peniel/x.md", names))
        self.assertFalse(vault_scope.is_private_hit("/Offen/x.md", names))

    def test_semantic_search_drops_private_docs(self):
        from core import retrieval

        docs = [
            {"path": "/Peniel/notiz.md", "title": "Maßnahmenplan Ort",
             "text": "Maßnahmenplan", "embedding": [1.0, 0.0]},
            {"path": "/Offen/notiz.md", "title": "Maßnahmenplan",
             "text": "Maßnahmenplan", "embedding": [1.0, 0.0]},
        ]
        with patch("core.retrieval.load_index", return_value={"docs": docs}), \
                patch("core.retrieval.embed_text", return_value=[1.0, 0.0]), \
                patch("core.retrieval._pdf_candidates", return_value=[
                    {"path": "/Peniel/b.pdf", "score": 0.99, "text": "x"}]):
            res = retrieval.search("Maßnahmenplan", top_k=5, min_score=0.0)
        paths = [r["path"] for r in res["results"]]
        self.assertIn("/Offen/notiz.md", paths)
        self.assertFalse(any(p.startswith("/Peniel/") for p in paths), paths)

    def test_list_open_vault_ok(self):
        res = vault_tools.list_vault_dir("Offen")
        self.assertEqual(res["status"], "success")
        self.assertEqual([e["name"] for e in res["entries"]], ["notiz.md"])


if __name__ == "__main__":
    unittest.main()
