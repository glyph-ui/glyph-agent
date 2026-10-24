#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests: Office-Extraktion (docx/xlsx, ZIP+XML) und Bild-OCR-Stufe."""
import os
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, SCRIPTS)

from core import config, image_ocr, office_text, pdf_tools  # noqa: E402
import build_pdf_index as bpi  # noqa: E402

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _make_docx(path):
    xml = (
        '<?xml version="1.0"?>'
        f'<w:document xmlns:w="{_W}"><w:body>'
        f'<w:p><w:r><w:t>Hallo Welt Betriebssicherheit</w:t></w:r></w:p>'
        f'<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Maschine</w:t></w:r></w:p></w:tc>'
        f'<w:tc><w:p><w:r><w:t>geprueft</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
        f'</w:body></w:document>'
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", xml)


def _make_xlsx(path):
    sst = (
        '<?xml version="1.0"?>'
        f'<sst xmlns="{_S}"><si><t>Apfel</t></si><si><t>Birne</t></si></sst>'
    )
    sheet = (
        '<?xml version="1.0"?>'
        f'<worksheet xmlns="{_S}"><sheetData>'
        f'<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
        f'<row r="2"><c r="A2"><v>42</v></c></row>'
        f'</sheetData></worksheet>'
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/sharedStrings.xml", sst)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)


class OfficeTextTests(unittest.TestCase):
    def test_docx_absaetze_und_tabelle(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "test.docx")
            _make_docx(p)
            text, err = office_text.read_docx(p)
            self.assertIsNone(err)
            self.assertIn("Hallo Welt Betriebssicherheit", text)
            self.assertIn("Maschine | geprueft", text)

    def test_xlsx_zeilen_mit_shared_strings(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "test.xlsx")
            _make_xlsx(p)
            text, err = office_text.read_xlsx(p)
            self.assertIsNone(err)
            self.assertIn("Apfel | Birne", text)
            self.assertIn("42", text)

    def test_read_office_dispatch(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "x.docx")
            _make_docx(p)
            text, err = office_text.read_office(p)
            self.assertIsNone(err)
            self.assertIn("Betriebssicherheit", text)
        text, err = office_text.read_office("/tmp/x.txt")
        self.assertEqual(text, "")
        self.assertIn("Kein Office-Format", err)


class ImageOcrTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="glyph-img-ocr-")
        self.root = self.td.name
        self._old_paths = list(config.VAULT_PATHS)
        self._old_path = config.VAULT_PATH
        config.VAULT_PATHS = [self.root]
        config.VAULT_PATH = self.root

    def tearDown(self):
        config.VAULT_PATHS = self._old_paths
        config.VAULT_PATH = self._old_path
        self.td.cleanup()

    def _png(self):
        p = os.path.join(self.root, "foto.png")
        with open(p, "wb") as f:
            f.write(b"\x89PNG\r\n")
        return os.path.basename(p)

    def test_read_image_mit_lighton_mock(self):
        with mock.patch.object(pdf_tools, "ocr_backend", return_value="lighton"), \
             mock.patch.object(image_ocr, "_ocr_image_lighton",
                               return_value=("BETRIEBSANWEISUNG\nAcetylen", None)):
            res = image_ocr.read_image(self._png())
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["engine"], "lighton-ocr")
        self.assertIn("Acetylen", res["content"])

    def test_read_image_ohne_backend(self):
        with mock.patch.object(pdf_tools, "ocr_backend", return_value=None):
            res = image_ocr.read_image(self._png())
        self.assertFalse(res["ok"])
        self.assertIn("Kein OCR-Backend", res["error"])

    def test_falsches_format_abgelehnt(self):
        with self.assertRaises(ValueError):
            image_ocr.read_image("irgendwas.txt")


class WorkerCapTests(unittest.TestCase):
    def test_lighton_wird_auf_1_gesetzt(self):
        self.assertEqual(bpi.cap_workers(4, "lighton"), 1)
        self.assertEqual(bpi.cap_workers(4, "vision"), 4)
        self.assertEqual(bpi.cap_workers(0, "lighton"), 1)

    def test_force_override(self):
        old = os.environ.get("PDF_INDEX_WORKERS_FORCE")
        os.environ["PDF_INDEX_WORKERS_FORCE"] = "1"
        try:
            self.assertEqual(bpi.cap_workers(4, "lighton"), 4)
        finally:
            if old is None:
                os.environ.pop("PDF_INDEX_WORKERS_FORCE", None)
            else:
                os.environ["PDF_INDEX_WORKERS_FORCE"] = old


class ExtractOneDispatchTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="glyph-office-idx-")
        self.root = self.td.name
        self._old_paths = list(config.VAULT_PATHS)
        self._old_path = config.VAULT_PATH
        config.VAULT_PATHS = [self.root]
        config.VAULT_PATH = self.root
        _make_docx(os.path.join(self.root, "notiz.docx"))
        _make_xlsx(os.path.join(self.root, "tabelle.xlsx"))
        with open(os.path.join(self.root, "scan.png"), "wb") as f:
            f.write(b"\x89PNG\r\n")

    def tearDown(self):
        config.VAULT_PATHS = self._old_paths
        config.VAULT_PATH = self._old_path
        self.td.cleanup()

    def test_docx_und_xlsx_werden_gechunkt(self):
        canon, title, chunks, engine, chars, err, pages, ocr = bpi.extract_one(
            os.path.join(self.root, "notiz.docx"))
        self.assertIsNone(err)
        self.assertEqual(engine, "docx-xml")
        self.assertGreater(len(chunks), 0)
        self.assertIn("Betriebssicherheit", chunks[0]["text"])

        canon, title, chunks, engine, chars, err, pages, ocr = bpi.extract_one(
            os.path.join(self.root, "tabelle.xlsx"))
        self.assertIsNone(err)
        self.assertEqual(engine, "xlsx-xml")
        self.assertIn("Apfel | Birne", chunks[0]["text"])

    def test_bild_dispatch(self):
        p = os.path.join(self.root, "scan.png")
        with open(p, "wb") as f:
            f.write(b"\x89PNG\r\n")
        with mock.patch.object(image_ocr, "read_image",
                               return_value={"ok": True, "path": "scan.png",
                                             "content": "Inhalt vom Foto",
                                             "chars": 15, "engine": "lighton-ocr",
                                             "error": None}):
            canon, title, chunks, engine, chars, err, pages, ocr = bpi.extract_one(p)
        self.assertIsNone(err)
        self.assertEqual(engine, "lighton-ocr")
        self.assertEqual(ocr, 1)
        self.assertIn("Inhalt vom Foto", chunks[0]["text"])

    def test_walk_findet_alle_drei(self):
        with open(os.path.join(self.root, "plan.pdf"), "wb") as f:
            f.write(b"%PDF-1.4\n")
        files = sorted(os.path.basename(f) for f in bpi._iter_files(
            [self.root], bpi.IMAGE_EXTS + bpi.OFFICE_EXTS + (".pdf",)))
        self.assertEqual(files, ["notiz.docx", "plan.pdf", "scan.png", "tabelle.xlsx"])


if __name__ == "__main__":
    unittest.main()