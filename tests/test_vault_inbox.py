#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vault-Inbox: Klassifikation gemischter Drops, Dedup, Wikilink, kein Privat-Index."""
import hashlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import recurring, vault_inbox  # noqa: E402


class ClassifyTests(unittest.TestCase):
    def test_passwoerter_ordner_ist_skip(self):
        self.assertEqual(
            vault_inbox.classify("01 Beruf/Passwörter/wifi.txt"),
            "skip",
        )
        self.assertEqual(
            vault_inbox.classify("01 Beruf/Passwoerter/foo.pdf"),
            "skip",
        )

    def test_beruf_arbeitssicherheit_ist_hseq(self):
        self.assertEqual(
            vault_inbox.classify("01 Beruf/Arbeitssicherheit/01 Regelwerk/TRGS 510.pdf"),
            "hseq",
        )

    def test_betriebsanweisung_download_ist_hseq(self):
        self.assertEqual(
            vault_inbox.classify("Downloads/Betriebsanweisung_Fahrzeuge.pdf"),
            "hseq",
        )

    def test_scan_gesundheit_ist_privat(self):
        self.assertEqual(
            vault_inbox.classify("Scanordner/2026-07-28 Implantatmodell- und Kostenplan.pdf"),
            "privat",
        )

    def test_zwangsvollstreckung_ist_privat(self):
        self.assertEqual(
            vault_inbox.classify("Scanordner/2026-09-22 Zwangsvollstreckung.pdf"),
            "privat",
        )

    def test_unbekannt_ist_privat(self):
        self.assertEqual(vault_inbox.classify("Scanordner/Bishara.pdf"), "privat")

    def test_bewerbung_nicht_in_hseq_index(self):
        self.assertEqual(
            vault_inbox.classify("01 Beruf/Bewerbung/Lebenslauf.pdf"),
            "intern",
        )


class DestAndLinkTests(unittest.TestCase):
    def test_hseq_scan_landet_in_eingang_dateien(self):
        dest = vault_inbox.dest_rel("hseq", "Scanordner/foo.pdf", day="2026-10-10")
        self.assertTrue(dest.startswith("00 Arbeitsfluss/Eingang-Dateien/2026-10-10/"))
        self.assertTrue(dest.endswith("foo.pdf"))

    def test_beruf_as_behaelt_baum(self):
        dest = vault_inbox.dest_rel(
            "hseq", "01 Beruf/Arbeitssicherheit/01 Regelwerk/vorschrift1.pdf",
            day="2026-10-10",
        )
        self.assertEqual(dest, "Arbeitssicherheit/01 Regelwerk/vorschrift1.pdf")

    def test_intern_unter_beruf_intern(self):
        dest = vault_inbox.dest_rel(
            "intern", "01 Beruf/Bewerbung/Lebenslauf.pdf", day="2026-10-10",
        )
        self.assertEqual(dest, "Beruf-intern/Bewerbung/Lebenslauf.pdf")

    def test_wikilink_zeile(self):
        self.assertEqual(
            vault_inbox.wikilink_line("Arbeitssicherheit/TRGS 510.pdf"),
            "- [[Arbeitssicherheit/TRGS 510.pdf]]",
        )


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="glyph-inbox-")
        self.root = self.td.name
        self.src = os.path.join(self.root, "src")
        self.asi = os.path.join(self.root, "ASI")
        self.privat = os.path.join(self.root, "Privat")
        os.makedirs(os.path.join(self.src, "Downloads"))
        os.makedirs(os.path.join(self.src, "Scanordner"))
        os.makedirs(os.path.join(self.src, "01 Beruf", "Arbeitssicherheit"))
        os.makedirs(os.path.join(self.src, "01 Beruf", "Passwörter"))
        os.makedirs(self.asi)
        os.makedirs(self.privat)

    def tearDown(self):
        self.td.cleanup()

    def _write(self, rel, body=b"x"):
        p = os.path.join(self.src, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(body)
        return p

    def test_dedup_kopiert_nicht_doppelt(self):
        blob = b"%PDF-1.4 betriebsanweisung test\n"
        self._write("Downloads/Betriebsanweisung_Fahrzeuge.pdf", blob)
        already = os.path.join(self.asi, "Arbeitssicherheit", "alt.pdf")
        os.makedirs(os.path.dirname(already), exist_ok=True)
        with open(already, "wb") as f:
            f.write(blob)
        res = vault_inbox.sync(
            sources=[os.path.join(self.src, "Downloads")],
            asi_root=self.asi,
            privat_root=self.privat,
            day="2026-10-10",
        )
        self.assertEqual(res["copied_hseq"], 0)
        self.assertEqual(res["skipped_dup"], 1)
        self.assertEqual(res["index_files"], [])

    def test_hseq_kopie_und_link_privat_nicht_im_index(self):
        self._write("Downloads/Betriebsanweisung_Leitern.pdf", b"%PDF hseq\n")
        self._write("Scanordner/Implantatmodell.pdf", b"%PDF privat\n")
        self._write("01 Beruf/Passwörter/wifi.txt", b"secret\n")
        res = vault_inbox.sync(
            sources=[
                os.path.join(self.src, "Downloads"),
                os.path.join(self.src, "Scanordner"),
                os.path.join(self.src, "01 Beruf"),
            ],
            asi_root=self.asi,
            privat_root=self.privat,
            day="2026-10-10",
        )
        self.assertEqual(res["copied_hseq"], 1)
        self.assertEqual(res["copied_privat"], 1)
        self.assertEqual(res["skipped_secret"], 1)
        self.assertTrue(os.path.isfile(os.path.join(
            self.asi, "00 Arbeitsfluss/Eingang-Dateien/2026-10-10/Betriebsanweisung_Leitern.pdf")))
        self.assertTrue(os.path.isfile(os.path.join(
            self.privat, "00 Eingang/2026-10-10/Implantatmodell.pdf")))
        moc_path = os.path.join(self.asi, "Themen/00 Eingang-Dateien.md")
        with open(moc_path, encoding="utf-8") as fh:
            moc = fh.read()
        self.assertIn("[[00 Arbeitsfluss/Eingang-Dateien/2026-10-10/Betriebsanweisung_Leitern.pdf]]", moc)
        self.assertEqual(len(res["index_files"]), 1)
        self.assertTrue(res["index_files"][0].endswith("Betriebsanweisung_Leitern.pdf"))
        self.assertFalse(any("Implantat" in p for p in res["index_files"]))
        self.assertFalse(os.path.exists(os.path.join(self.asi, "Passwörter")))

    def test_index_skip_beruf_intern(self):
        self.assertTrue(vault_inbox.skip_index_rel("Beruf-intern/Bewerbung/x.pdf"))
        self.assertFalse(vault_inbox.skip_index_rel("Arbeitssicherheit/x.pdf"))


class ScriptAllowTests(unittest.TestCase):
    def test_vault_inbox_sync_auf_allowlist(self):
        path = recurring._resolve_job_script("vault_inbox_sync.py")
        self.assertIsNotNone(path)
        self.assertTrue(path.endswith("vault_inbox_sync.py"))


if __name__ == "__main__":
    unittest.main()
