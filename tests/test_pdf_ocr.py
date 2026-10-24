# -*- coding: utf-8 -*-
"""Tests: read_pdf liest Scan-PDFs per OCR und meldet "kein Textlayer" ehrlich.

Vorher lieferte ein Scan (pdftotext exit 0, leerer stdout) `ok: True, chars: 0` —
das Modell bekam ein leeres Dokument und erfand eine Antwort.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config, pdf_tools


def _script(path, body):
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, 0o755)
    return path


class PdfOcrFallbackTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="glyph-pdf-ocr-")
        self.root = self.td.name
        self._old_paths = list(config.VAULT_PATHS)
        self._old_path = config.VAULT_PATH
        config.VAULT_PATHS = [self.root]
        config.VAULT_PATH = self.root
        for name in ("scan.pdf", "text.pdf"):
            with open(os.path.join(self.root, name), "wb") as f:
                f.write(b"%PDF-1.4\n")
        self._old_ptt = pdf_tools._pdftotext_bin
        self._old_ocr = pdf_tools._ocr_bin
        self._old_backend = pdf_tools.ocr_backend
        # Isolation: Live-LightOn (Port 8790) darf diese Vision-Tests nicht übernehmen.
        pdf_tools.ocr_backend = lambda preferred=None: (
            "vision" if pdf_tools._ocr_bin() else None
        )
        # pdftotext-Fake: scan.pdf -> kein Text, text.pdf -> genug Text
        self.ptt = _script(
            os.path.join(self.root, "pdftotext-fake"),
            'case "$4" in\n'
            '  *scan.pdf) : ;;\n'
            '  *) printf "%s" "Echter Textlayer mit ausreichend Zeichen fuer den Schwellwert 40. 0123456789" ;;\n'
            'esac',
        )
        self.ocr = _script(
            os.path.join(self.root, "ocr-fake"),
            'echo "OCR: Betriebsanweisung Umgang mit Augenschutz. BGR 192. Naechster Ueberpruefungstermin 18.10.2010."',
        )
        pdf_tools._pdftotext_bin = lambda: self.ptt

    def tearDown(self):
        pdf_tools._pdftotext_bin = self._old_ptt
        pdf_tools._ocr_bin = self._old_ocr
        pdf_tools.ocr_backend = self._old_backend
        config.VAULT_PATHS = self._old_paths
        config.VAULT_PATH = self._old_path
        self.td.cleanup()

    def test_textlayer_does_not_call_ocr(self):
        called = {"n": 0}

        def fake_ocr():
            called["n"] += 1
            return self.ocr

        pdf_tools._ocr_bin = fake_ocr
        res = pdf_tools.read_pdf("text.pdf")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["engine"], "pdftotext")
        self.assertFalse(res["ocr"])
        self.assertEqual(called["n"], 0, "OCR darf bei Textlayer nicht laufen")

    def test_scan_uses_vision_ocr(self):
        pdf_tools._ocr_bin = lambda: self.ocr
        res = pdf_tools.read_pdf("scan.pdf")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["engine"], "vision-ocr")
        self.assertTrue(res["ocr"])
        self.assertIn("BGR 192", res["content"])

    def test_scan_without_ocr_helper_is_not_ok_true(self):
        pdf_tools._ocr_bin = lambda: None
        res = pdf_tools.read_pdf("scan.pdf")
        self.assertFalse(res["ok"], res)
        self.assertIn("keinen Textlayer", res["error"])
        err = (res["error"] or "").lower()
        self.assertNotIn("brew", err)
        self.assertNotIn("poppler", err)

    def test_scan_with_failing_ocr_is_not_ok_true(self):
        bad = _script(os.path.join(self.root, "ocr-bad"), 'echo "boom" >&2; exit 1')
        pdf_tools._ocr_bin = lambda: bad
        res = pdf_tools.read_pdf("scan.pdf")
        self.assertFalse(res["ok"], res)
        self.assertEqual(res["engine"], "vision-ocr")
        self.assertIn("OCR", res["error"])

    def test_ocr_disabled_reports_scan(self):
        pdf_tools._ocr_bin = lambda: self.ocr
        res = pdf_tools.read_pdf("scan.pdf", ocr=False)
        self.assertFalse(res["ok"], res)
        self.assertIn("keinen Textlayer", res["error"])


if __name__ == "__main__":
    unittest.main()
