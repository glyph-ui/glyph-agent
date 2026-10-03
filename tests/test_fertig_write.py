#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fertig-Berichte: anlegen ja, bestehende nicht überschreiben (außer Log)."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config, vault_tools


class FertigOverwriteTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="glyph-fertig-")
        self.fertig = os.path.join(self.td.name, "Fertig")
        os.makedirs(self.fertig)
        self.backup = os.path.join(self.td.name, "backups")
        os.makedirs(self.backup)
        self.bericht = os.path.join(
            self.fertig, "2026-09-16 Auditbericht - Musterkunde Musterstadt.md"
        )
        self.log = os.path.join(self.fertig, "Verarbeitungslog.md")
        with open(self.bericht, "w", encoding="utf-8") as f:
            f.write("ORIGINAL-MUSTER\n")
        with open(self.log, "w", encoding="utf-8") as f:
            f.write("# log\nalt\n")
        self._backup = config.BACKUP_DIR
        config.BACKUP_DIR = self.backup

        def resolve(path):
            name = os.path.basename(str(path or "").replace("\\", "/"))
            return os.path.join(self.fertig, name)

        def rel(resolved):
            return "HSEQ Sync/00 Arbeitsfluss/Fertig/" + os.path.basename(resolved)

        self._p_res = patch.object(vault_tools, "_resolve_vault_path", side_effect=resolve)
        self._p_rel = patch.object(vault_tools, "_rel_to_root", side_effect=rel)
        self._p_priv = patch("core.vaults_registry.is_private_path", return_value=False)
        self._p_wr = patch("core.vaults_registry.is_writable_path", return_value=True)
        self._p_res.start()
        self._p_rel.start()
        self._p_priv.start()
        self._p_wr.start()

    def tearDown(self):
        self._p_wr.stop()
        self._p_priv.stop()
        self._p_rel.stop()
        self._p_res.stop()
        config.BACKUP_DIR = self._backup
        self.td.cleanup()

    def test_apply_edit_refuses_existing_fertig_report(self):
        out = vault_tools.apply_edit(
            "HSEQ Sync/00 Arbeitsfluss/Fertig/2026-09-16 Auditbericht - Musterkunde Musterstadt.md",
            "UEBERSCHREIBEN\n",
        )
        self.assertFalse(out.get("applied"))
        self.assertEqual(out.get("reason"), "exists")
        with open(self.bericht, encoding="utf-8") as f:
            self.assertEqual(f.read(), "ORIGINAL-MUSTER\n")

    def test_apply_edit_allows_verarbeitungslog(self):
        out = vault_tools.apply_edit(
            "HSEQ Sync/00 Arbeitsfluss/Fertig/Verarbeitungslog.md",
            "# log\nneu\n",
        )
        self.assertTrue(out.get("applied"))
        with open(self.log, encoding="utf-8") as f:
            self.assertEqual(f.read(), "# log\nneu\n")

    def test_apply_edit_creates_missing_fertig_file(self):
        out = vault_tools.apply_edit(
            "HSEQ Sync/00 Arbeitsfluss/Fertig/2026-09-21 Auditbericht - Testort.md",
            "NEU\n",
        )
        self.assertTrue(out.get("applied"))
        self.assertTrue(out.get("created"))
        neu = os.path.join(self.fertig, "2026-09-21 Auditbericht - Testort.md")
        with open(neu, encoding="utf-8") as f:
            self.assertEqual(f.read(), "NEU\n")


if __name__ == "__main__":
    unittest.main()
