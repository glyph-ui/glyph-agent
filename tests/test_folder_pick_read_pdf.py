#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Folder-Pick + File-Tap: namenspassende PDFs im selben Turn ReadPdf.
Nie den Nutzer fragen, welche Datei zu oeffnen.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

VORLAGEN = "/ASI, BS. UWS, QM, EM/Arbeitssicherheit/Vorlagen"
KRANE = VORLAGEN + "/016_Krane.pdf"
GLEIS = (
    "/ASI, BS. UWS, QM, EM/Arbeitssicherheit/DGUV Vorschriften/"
    "DGUV Information 201-021_guv_i-781.pdf"
)
ABSTURZ = (
    "/ASI, BS. UWS, QM, EM/Arbeitssicherheit/Schulung/"
    "Leitlinie zur DGUV_Information_212-515.pdf"
)
DGUV_215 = (
    "/ASI, BS. UWS, QM, EM/Arbeitssicherheit/"
    "DGUV_Information_215-410_745_042015.pdf"
)
FZ_209 = (
    "/ASI, BS. UWS, QM, EM/Arbeitssicherheit/Information/"
    "DGUV Information 209-007 Fahrzeuginstandhaltung.pdf"
)
HL_BGHM = (
    "/ASI, BS. UWS, QM, EM/Gesetzte und Verordnungen/"
    "Handlungsleitfaden_DGUV_Vorschrift_BGHM.pdf"
)
GB_BGHM = (
    "/ASI, BS. UWS, QM, EM/Arbeitssicherheit/Vorlagen/"
    "07_00 Muster-Gefaehrdungsbeurteilungen der BGHM.pdf"
)
INFO_DIR = "/ASI, BS. UWS, QM, EM/Arbeitssicherheit/Information"
BGHM_Q = (
    "Kannst du mir helfen die Anfrage zu bewerten: Die BGHM fragt konkret "
    "wie folgt: Liegen fuer Wirbelsaeulenbelastung Gefaehrdungsbeurteilungen"
)
KRANE_TEXT = "Krane: UVV und Befaehigung. Pruefung vor Inbetriebnahme."
FZ_TEXT = "Fahrzeuginstandhaltung: Dummy-PDF-Text fuer Tests. " + ("Y" * 40)
GLEIS_TEXT = "Gleisbereich: Sicherung gegen Bahnverkehr. " + ("X" * 800)
ASK = "Soll ich 016_Krane.pdf oeffnen?"
ASK2 = "Welche Datei zuerst?"
ANSWER = "Laut 016_Krane.pdf: UVV und Befaehigung, Pruefung vor Inbetriebnahme."


def _listing_vorlagen():
    return {
        "status": "success",
        "path": VORLAGEN.lstrip("/"),
        "vault": "ASI, BS. UWS, QM, EM",
        "count": 4,
        "truncated": False,
        "error": None,
        "entries": [
            {
                "name": "DGUV Information 201-021_guv_i-781.pdf",
                "path": GLEIS.lstrip("/"),
                "type": "file",
            },
            {
                "name": "001_Allgemein.pdf",
                "path": VORLAGEN.lstrip("/") + "/001_Allgemein.pdf",
                "type": "file",
            },
            {
                "name": "016_Krane.pdf",
                "path": KRANE.lstrip("/"),
                "type": "file",
            },
            {
                "name": "Leitlinie zur DGUV_Information_212-515.pdf",
                "path": ABSTURZ.lstrip("/"),
                "type": "file",
            },
            {
                "name": "026_Betrieb und Instandhaltung von Foerderbaendern.pdf",
                "path": VORLAGEN.lstrip("/")
                + "/026_Betrieb und Instandhaltung von Foerderbaendern.pdf",
                "type": "file",
            },
        ],
    }


def _pdf_content(path):
    p = (path or "").replace("\\", "/")
    if p.endswith("016_Krane.pdf") or "016_Krane" in p:
        return KRANE_TEXT
    if "201-021" in p or "Gleis" in p:
        return GLEIS_TEXT
    if "209-007" in p or "Fahrzeuginstandhaltung" in p:
        return FZ_TEXT
    return "Fremd-PDF ohne Kran-Bezug."


def _mixed_209_selection():
    return [
        {"kind": "file", "path": HL_BGHM, "title": "Handlungsleitfaden_DGUV_Vorschrift_BGHM.pdf"},
        {
            "kind": "file",
            "path": GB_BGHM,
            "title": "07_00 Muster-Gefaehrdungsbeurteilungen der BGHM.pdf",
        },
        {
            "kind": "file",
            "path": FZ_209,
            "title": "DGUV Information 209-007 Fahrzeuginstandhaltung.pdf",
        },
    ]


def _listing_dguv_info():
    return {
        "status": "success",
        "path": INFO_DIR.lstrip("/"),
        "vault": "ASI, BS. UWS, QM, EM",
        "count": 2,
        "truncated": False,
        "error": None,
        "entries": [
            {
                "name": "Handlungsleitfaden_DGUV_Vorschrift_BGHM.pdf",
                "path": HL_BGHM.lstrip("/"),
                "type": "file",
            },
            {
                "name": "DGUV Information 209-007 Fahrzeuginstandhaltung.pdf",
                "path": FZ_209.lstrip("/"),
                "type": "file",
            },
        ],
    }


def _patch_pdf_and_list(tool_loop, pdf_tools, reads):
    old_pdf = pdf_tools.read_pdf
    old_list = tool_loop._run_list_vault_dir

    def fake_pdf(path, max_chars=None):
        reads.append(path)
        text = _pdf_content(path)
        return {
            "ok": True,
            "path": path,
            "content": text,
            "chars": len(text),
            "truncated": False,
            "engine": "pdftotext",
            "error": None,
        }

    def fake_list(path):
        return _listing_vorlagen()

    pdf_tools.read_pdf = fake_pdf
    tool_loop._run_list_vault_dir = fake_list
    return old_pdf, old_list


def _restore(tool_loop, pdf_tools, old_pdf, old_list):
    pdf_tools.read_pdf = old_pdf
    tool_loop._run_list_vault_dir = old_list


def _read_names(reads):
    return [str(p).replace("\\", "/").rsplit("/", 1)[-1] for p in reads]


class FolderPickAutoReadTests(unittest.TestCase):
    def test_folder_pick_reads_krane_not_gleisbereich(self):
        from core import tool_loop, pdf_tools

        reads = []
        old_pdf, old_list = _patch_pdf_and_list(tool_loop, pdf_tools, reads)
        try:
            out = tool_loop._selected_vault_outcome(
                [{"kind": "folder", "path": VORLAGEN, "title": "Vorlagen"}],
                "Kannst du mir Informationen zu Kran im Betrieb geben",
            )
        finally:
            _restore(tool_loop, pdf_tools, old_pdf, old_list)

        names = _read_names(reads)
        tools = [c.get("tool") for c in out.get("tool_calls") or []]
        hist = out.get("history_append") or ""
        results = []
        for tr in out.get("tool_results") or []:
            if tr.get("tool") == "VaultFind":
                payload = (tr.get("result") or {}).get("result") or {}
                results = payload.get("results") or []

        self.assertIn("ReadPdf", tools, tools)
        self.assertIn("016_Krane.pdf", names, names)
        self.assertNotIn("DGUV Information 201-021_guv_i-781.pdf", names, names)
        self.assertFalse(
            any("Foerderbaendern" in n or "Betrieb und Instandhaltung" in n for n in names),
            names,
        )
        self.assertLessEqual(len(reads), 2, names)
        self.assertTrue(
            any("016_Krane.pdf" in str(r.get("path") or "") for r in results),
            results,
        )
        self.assertIn(KRANE_TEXT, hist)
        self.assertNotIn("soll ich", hist.lower())

    def test_file_tap_ranks_by_name_not_list_order(self):
        from core import tool_loop, pdf_tools

        reads = []
        old_pdf, old_list = _patch_pdf_and_list(tool_loop, pdf_tools, reads)
        try:
            selected = [
                {"kind": "file", "path": GLEIS, "title": "DGUV Information 201-021_guv_i-781.pdf"},
                {"kind": "file", "path": DGUV_215, "title": "DGUV_Information_215-410_745_042015.pdf"},
                {"kind": "file", "path": KRANE, "title": "016_Krane.pdf"},
                {"kind": "file", "path": ABSTURZ, "title": "Leitlinie zur DGUV_Information_212-515.pdf"},
            ]
            out = tool_loop._selected_vault_outcome(
                selected,
                "Was ist in einem KFZ Betrieb zu Krane zu beachten?",
            )
        finally:
            _restore(tool_loop, pdf_tools, old_pdf, old_list)

        names = _read_names(reads)
        self.assertEqual(names[:1], ["016_Krane.pdf"], names)
        self.assertNotIn("DGUV Information 201-021_guv_i-781.pdf", names, names)
        self.assertLessEqual(len(reads), 2, names)
        hist = out.get("history_append") or ""
        self.assertIn(KRANE_TEXT, hist)
        self.assertNotIn("Gleisbereich", hist)

    def test_file_tap_does_not_read_four_unrelated(self):
        from core import tool_loop, pdf_tools

        reads = []
        old_pdf, old_list = _patch_pdf_and_list(tool_loop, pdf_tools, reads)
        try:
            selected = [
                {"kind": "file", "path": GLEIS, "title": "a.pdf"},
                {"kind": "file", "path": DGUV_215, "title": "b.pdf"},
                {"kind": "file", "path": ABSTURZ, "title": "c.pdf"},
                {"kind": "file", "path": KRANE, "title": "016_Krane.pdf"},
            ]
            tool_loop._selected_vault_outcome(selected, "Krane")
        finally:
            _restore(tool_loop, pdf_tools, old_pdf, old_list)
        self.assertEqual(_read_names(reads), ["016_Krane.pdf"])

    def test_mixed_selection_reads_209007_not_handlungsleitfaden(self):
        from core import tool_loop, pdf_tools

        for query in (
            "209-007",
            "Fahrzeuginstandhaltung",
            "DGUV 209-007 Fahrzeuginstandhaltung",
        ):
            reads = []
            old_pdf, old_list = _patch_pdf_and_list(tool_loop, pdf_tools, reads)
            try:
                out = tool_loop._selected_vault_outcome(
                    _mixed_209_selection(),
                    query,
                )
            finally:
                _restore(tool_loop, pdf_tools, old_pdf, old_list)
            names = _read_names(reads)
            self.assertTrue(names, query)
            self.assertIn("DGUV Information 209-007 Fahrzeuginstandhaltung.pdf", names, names)
            self.assertEqual(
                names[:1],
                ["DGUV Information 209-007 Fahrzeuginstandhaltung.pdf"],
                (query, names),
            )
            self.assertFalse(
                any("Handlungsleitfaden" in n for n in names),
                (query, names),
            )
            self.assertLessEqual(len(reads), 2, names)
            hist = out.get("history_append") or ""
            self.assertIn(FZ_TEXT[:20], hist)

    def test_file_tap_209007_wins_over_bghm_query(self):
        from core import tool_loop, pdf_tools

        reads = []
        old_pdf, old_list = _patch_pdf_and_list(tool_loop, pdf_tools, reads)
        try:
            tool_loop._selected_vault_outcome(_mixed_209_selection(), BGHM_Q)
        finally:
            _restore(tool_loop, pdf_tools, old_pdf, old_list)
        names = _read_names(reads)
        self.assertIn("DGUV Information 209-007 Fahrzeuginstandhaltung.pdf", names, names)
        self.assertFalse(
            any("Handlungsleitfaden" in n for n in names),
            names,
        )
        self.assertLessEqual(len(reads), 2, names)

    def test_folder_pick_reads_209007_not_list_first_dguv(self):
        from core import tool_loop, pdf_tools

        reads = []
        old_pdf = pdf_tools.read_pdf
        old_list = tool_loop._run_list_vault_dir

        def fake_pdf(path, max_chars=None):
            reads.append(path)
            text = _pdf_content(path)
            return {
                "ok": True,
                "path": path,
                "content": text,
                "chars": len(text),
                "truncated": False,
                "engine": "pdftotext",
                "error": None,
            }

        def fake_list(path):
            return _listing_dguv_info()

        pdf_tools.read_pdf = fake_pdf
        tool_loop._run_list_vault_dir = fake_list
        try:
            out = tool_loop._selected_vault_outcome(
                [{"kind": "folder", "path": INFO_DIR, "title": "Information"}],
                "Kannst du mir DGUV 209-007 Fahrzeuginstandhaltung geben",
            )
        finally:
            pdf_tools.read_pdf = old_pdf
            tool_loop._run_list_vault_dir = old_list

        names = _read_names(reads)
        self.assertIn("DGUV Information 209-007 Fahrzeuginstandhaltung.pdf", names, names)
        self.assertEqual(
            names[:1],
            ["DGUV Information 209-007 Fahrzeuginstandhaltung.pdf"],
            names,
        )
        self.assertFalse(
            any("Handlungsleitfaden" in n for n in names),
            names,
        )
        self.assertLessEqual(len(reads), 2, names)
        hist = out.get("history_append") or ""
        self.assertIn(FZ_TEXT[:20], hist)
        self.assertNotIn("soll ich", hist.lower())


class AskToReadBlockedTests(unittest.TestCase):
    def test_content_question_does_not_ask_which_pdf(self):
        from core import tool_loop, pdf_tools
        import core.llm as llm_mod
        import core.retrieval as retrieval_mod
        import core.web as web

        reads = []
        old_pdf, old_list = _patch_pdf_and_list(tool_loop, pdf_tools, reads)
        old_find = retrieval_mod.vault_find
        old_search = retrieval_mod.search
        old_web = web.web_search
        old_chat = llm_mod.chat
        calls = {"n": 0}

        def fake_chat(system, user, temperature=0.3, num_ctx=8192):
            i = calls["n"]
            calls["n"] += 1
            if i == 0:
                return ASK
            if i == 1:
                return ASK2
            return ANSWER

        retrieval_mod.vault_find = lambda *a, **k: {
            "status": "empty",
            "query": a[0] if a else "",
            "candidates": 0,
            "selected": 0,
            "threshold": 0.6,
            "sources": [],
            "results": [],
        }
        retrieval_mod.search = retrieval_mod.vault_find
        web.web_search = lambda *a, **k: []
        llm_mod.chat = fake_chat
        try:
            res = tool_loop.run(
                "Kannst du mir Informationen zu Kran im Betrieb geben",
                vault_search=True,
                vault_selected=[
                    {"kind": "folder", "path": VORLAGEN, "title": "Vorlagen"}
                ],
                max_rounds=4,
            )
        finally:
            _restore(tool_loop, pdf_tools, old_pdf, old_list)
            retrieval_mod.vault_find = old_find
            retrieval_mod.search = old_search
            web.web_search = old_web
            llm_mod.chat = old_chat

        names = _read_names(reads)
        answer = res.get("answer") or ""
        low = answer.lower().replace("ö", "oe")
        self.assertIn("016_Krane.pdf", names, names)
        self.assertNotIn("DGUV Information 201-021_guv_i-781.pdf", names, names)
        self.assertFalse(
            any("Foerderbaendern" in n or "Betrieb und Instandhaltung" in n for n in names),
            names,
        )
        self.assertNotIn("soll ich", low, answer)
        self.assertNotIn("welche datei zuerst", low, answer)
        self.assertNotIn("oeffnen?", low, answer)
        self.assertIn("UVV", answer)
        self.assertTrue(res.get("ok"))

    def test_is_ask_to_read_phrases(self):
        from core import tool_loop

        self.assertTrue(tool_loop._is_ask_to_read(ASK))
        self.assertTrue(tool_loop._is_ask_to_read(ASK2))
        self.assertTrue(tool_loop._is_ask_to_read("Soll ich die Datei 016_Krane.pdf lesen?"))
        self.assertFalse(tool_loop._is_ask_to_read(ANSWER))
        self.assertFalse(tool_loop._is_ask_to_read("Welche Dateien liegen im Eingang?"))

    def test_read_pdf_ohne_text_faellt_auf_index_excerpt_zurueck(self):
        """Meldung 2026-10-09: Scheel-Schrank-PDF lieferte nur den Dateinamen.

        read_pdf ohne Textlayer → Excerpt aus dem Vektorindex nachziehen,
        damit das Modell den Inhalt sieht statt den Treffer zu verwerfen.
        """
        from core import tool_loop, pdf_tools
        import core.pdf_index as pdf_index

        files = [{"path": "/V/a.pdf", "title": "a.pdf", "text": "", "score": 1.0}]
        old_pdf = pdf_tools.read_pdf
        old_exc = pdf_index.excerpt_for

        def fake_pdf(path, max_chars=None):
            return {"ok": False, "path": path, "content": "", "error": "kein Textlayer"}

        def fake_excerpt(path, *a, **k):
            return "INDEX-SNIPPET:" + str(path)

        pdf_tools.read_pdf = fake_pdf
        pdf_index.excerpt_for = fake_excerpt
        try:
            out = tool_loop._read_selected_pdfs(files, limit=1, query="sicherheitsschrank")
        finally:
            pdf_tools.read_pdf = old_pdf
            pdf_index.excerpt_for = old_exc

        self.assertEqual(len(out), 1)
        self.assertIs(out[0][1].get("ok"), False)
        self.assertEqual(files[0]["excerpt"], "INDEX-SNIPPET:/V/a.pdf")
        self.assertEqual(files[0]["text"], "INDEX-SNIPPET:/V/a.pdf")


class ReadPdfInternalFmtTests(unittest.TestCase):
    def test_readpdf_is_internal_source(self):
        from core import tool_loop

        body = tool_loop._fmt_tool_results(
            [
                {
                    "tool": "VaultFind",
                    "args": {"query": "Kran"},
                    "result": {"ok": True, "result": {"results": [{"path": KRANE}]}},
                },
                {
                    "tool": "ReadPdf",
                    "args": {"path": KRANE},
                    "result": {"ok": True, "result": {"content": KRANE_TEXT, "path": KRANE}},
                },
            ]
        )
        self.assertIn("internal_sources:", body)
        self.assertIn("ReadPdf", body)
        self.assertIn(KRANE_TEXT, body.split("internal_sources:", 1)[1])
        # PDF-Text vor VaultFind-JSON, damit die Cap ihn nicht abschneidet
        pdf_pos = body.find("ReadPdf")
        vault_pos = body.find("VaultFind")
        self.assertGreaterEqual(pdf_pos, 0)
        self.assertGreaterEqual(vault_pos, 0)
        self.assertLess(pdf_pos, vault_pos, body[:400])


if __name__ == "__main__":
    unittest.main()
