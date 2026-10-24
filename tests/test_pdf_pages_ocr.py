#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests: seitenweise PDF-Extraktion mit OCR-Gate pro Seite (gemischte PDFs)."""
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


# > 500 Zeichen, damit max_chars-Kappung messbar ist
_LONG = "Seite eins hat genug Textlayer-Inhalt fuer die Schwelle. 0123456789 " * 12


class PerPageExtractionTests(unittest.TestCase):
    """Gemischtes PDF: Seite 1 Textlayer, Seite 2 Scan → genau Seite 2 wird OCR."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="glyph-pdf-pages-")
        self.root = self.td.name
        self._old_paths = list(config.VAULT_PATHS)
        self._old_path = config.VAULT_PATH
        config.VAULT_PATHS = [self.root]
        config.VAULT_PATH = self.root
        with open(os.path.join(self.root, "mixed.pdf"), "wb") as f:
            f.write(b"%PDF-1.4\n")
        self._old_ptt = pdf_tools._pdftotext_bin
        self._old_ocr = pdf_tools._ocr_bin
        self._old_backend = pdf_tools.ocr_backend
        # pdftotext-Fake: Seite 1 mit Text, Seite 2 nur Seitenstempel (Form-Feed)
        self.ptt = _script(
            os.path.join(self.root, "pdftotext-fake"),
            "printf '"
            + _LONG
            + "\\n\\f12\\n'",
        )
        # ocr_pdf-Aufruf: <bin> <pdf> 0 2.0 <seite> → Seitennummer in $4
        self.ocr = _script(
            os.path.join(self.root, "ocr-fake"),
            'echo "--- Seite $4 ---"\n'
            'echo "OCR-Seite zwei: gepruefte Leiter, BGR 192."',
        )
        pdf_tools._pdftotext_bin = lambda: self.ptt
        pdf_tools._ocr_bin = lambda: self.ocr

    def tearDown(self):
        pdf_tools._pdftotext_bin = self._old_ptt
        pdf_tools._ocr_bin = self._old_ocr
        pdf_tools.ocr_backend = self._old_backend
        config.VAULT_PATHS = self._old_paths
        config.VAULT_PATH = self._old_path
        self.td.cleanup()

    def test_nur_scanseite_wird_ocr(self):
        pdf_tools.ocr_backend = lambda preferred=None: "vision"
        res = pdf_tools.read_pdf("mixed.pdf")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["engine"], "pdftotext+vision-ocr")
        self.assertTrue(res["ocr"])
        self.assertEqual(res["pages"], 2)
        self.assertEqual(res["ocr_pages"], 1)
        self.assertIn("Seite eins", res["content"])
        self.assertIn("BGR 192", res["content"])

    def test_gemischt_ohne_ocr_bleibt_halb_ok(self):
        pdf_tools.ocr_backend = lambda preferred=None: None
        res = pdf_tools.read_pdf("mixed.pdf")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["engine"], "pdftotext")
        self.assertIn("Seite eins", res["content"])
        self.assertNotIn("BGR 192", res["content"])

    def test_ocr_aus_liefert_textlayer(self):
        pdf_tools.ocr_backend = lambda preferred=None: "vision"
        res = pdf_tools.read_pdf("mixed.pdf", ocr=False)
        self.assertTrue(res["ok"], res)
        self.assertFalse(res["ocr"])
        self.assertNotIn("BGR 192", res["content"])

    def test_chars_ist_vollstaendig_nicht_gekappt(self):
        pdf_tools.ocr_backend = lambda preferred=None: "vision"
        res = pdf_tools.read_pdf("mixed.pdf", max_chars=500)
        self.assertTrue(res["ok"], res)
        self.assertGreater(res["chars"], len(res["content"]))
        self.assertTrue(res["truncated"])


class OcrOutputParsingTests(unittest.TestCase):
    def test_seitenmarker(self):
        raw = "--- Seite 3 ---\nDritter Inhalt\n--- Seite 4 ---\nVierter"
        self.assertEqual(pdf_tools._parse_ocr_output(raw, 3), "Dritter Inhalt")
        self.assertEqual(pdf_tools._parse_ocr_output(raw, 4), "Vierter")

    def test_rohoutput_ohne_marker(self):
        self.assertEqual(pdf_tools._parse_ocr_output("Nackter Text", 7), "Nackter Text")

    def test_backend_auswahl(self):
        self.assertIsNone(pdf_tools.ocr_backend("off"))
        old_ready = pdf_tools._lighton_ready
        pdf_tools._lighton_ready = lambda: False
        try:
            self.assertIsNone(pdf_tools.ocr_backend("lighton"))
        finally:
            pdf_tools._lighton_ready = old_ready
        old = pdf_tools._ocr_bin
        pdf_tools._ocr_bin = lambda: None
        try:
            self.assertIsNone(pdf_tools.ocr_backend("vision"))
        finally:
            pdf_tools._ocr_bin = old
        self.assertEqual(pdf_tools.ocr_backend("vision"), "vision")
        pdf_tools._lighton_ready = lambda: True
        try:
            self.assertEqual(pdf_tools.ocr_backend("lighton"), "lighton")
            self.assertEqual(pdf_tools.ocr_backend("auto"), "lighton")
        finally:
            pdf_tools._lighton_ready = old_ready


class LightonRuntimeTests(unittest.TestCase):
    """Feintuning: Default-URL, Timeout, Seiten-Fallback Vision."""

    def test_default_url_ohne_env(self):
        old = os.environ.pop("LIGHTON_OCR_URL", None)
        try:
            self.assertEqual(pdf_tools._lighton_base(), "http://127.0.0.1:8790/v1")
        finally:
            if old is not None:
                os.environ["LIGHTON_OCR_URL"] = old

    def test_lighton_timeout_laenger_als_vision(self):
        self.assertGreaterEqual(pdf_tools._LIGHTON_TIMEOUT, 120)
        self.assertGreater(pdf_tools._LIGHTON_TIMEOUT, pdf_tools._PAGE_OCR_TIMEOUT)

    def test_lighton_leer_faellt_auf_vision(self):
        td = tempfile.TemporaryDirectory(prefix="glyph-lighton-fb-")
        root = td.name
        old_paths, old_path = list(config.VAULT_PATHS), config.VAULT_PATH
        config.VAULT_PATHS = [root]
        config.VAULT_PATH = root
        with open(os.path.join(root, "scan.pdf"), "wb") as f:
            f.write(b"%PDF-1.4\n")
        ptt = _script(
            os.path.join(root, "ptt"),
            ':',
        )
        old_ptt, old_ocr, old_be = pdf_tools._pdftotext_bin, pdf_tools._ocr_bin, pdf_tools.ocr_backend
        old_lighton = pdf_tools._ocr_page_lighton
        pdf_tools._pdftotext_bin = lambda: ptt
        pdf_tools._ocr_bin = lambda: _script(
            os.path.join(root, "ocr"),
            'echo "VISION-FALLBACK BGR 192 Acetylen"',
        )
        pdf_tools.ocr_backend = lambda preferred=None: "lighton"
        pdf_tools._ocr_page_lighton = lambda *a, **k: ("", "timeout")
        try:
            res = pdf_tools.read_pdf("scan.pdf")
            self.assertTrue(res["ok"], res)
            self.assertIn("BGR 192", res["content"])
            self.assertEqual(res["engine"], "vision-ocr")
            self.assertEqual(res["ocr_pages"], 1)
        finally:
            pdf_tools._pdftotext_bin = old_ptt
            pdf_tools._ocr_bin = old_ocr
            pdf_tools.ocr_backend = old_be
            pdf_tools._ocr_page_lighton = old_lighton
            config.VAULT_PATHS = old_paths
            config.VAULT_PATH = old_path
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
