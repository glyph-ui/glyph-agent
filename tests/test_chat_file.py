"""ChatDatei erzeugt PDF/Word-Bytes und schreibt nichts auf die Platte."""
import base64
import io
import unittest
import zipfile

from core import chat_file, tool_registry


class ChatFileTests(unittest.TestCase):
    def test_pdf_bytes(self):
        out = chat_file.run({
            "title": "Leitern und Tritte",
            "format": "pdf",
            "text": "Anwendungsbereich\nFester Untergrund.",
        })
        self.assertTrue(out["ok"], out)
        raw = base64.b64decode(out["result"]["data"])
        self.assertTrue(raw.startswith(b"%PDF"))
        self.assertIn(b"Leitern und Tritte", raw)
        self.assertEqual(out["result"]["name"], "Leitern und Tritte.pdf")

    def test_docx_bytes(self):
        out = tool_registry.execute(
            "ChatDatei",
            {"title": "Hinweis", "format": "word", "text": "Nur im Chat."},
            mode="agent",
        )
        self.assertTrue(out["ok"], out)
        raw = base64.b64decode(out["result"]["data"])
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml = z.read("word/document.xml").decode("utf-8")
        self.assertIn("Hinweis", xml)
        self.assertIn("Nur im Chat.", xml)
        self.assertTrue(out["result"]["name"].endswith(".docx"))

    def test_from_answer_when_user_asked_for_a_document(self):
        msg = "Betriebsanweisung erstellen Drucker"
        answer = (
            "Hier die Vorlage.\n\n"
            "**Betriebsanweisung: Drucker/Kopierer**\n\n"
            "1. Anwendungsbereich\n"
            "Gilt für alle Beschäftigten, die Drucker im Büro bedienen.\n"
            "Sag Bescheid, wenn ich das als PDF in den Vault legen soll."
        )
        out = chat_file.from_answer(msg, answer)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["result"]["title"], "Betriebsanweisung: Drucker/Kopierer")
        self.assertTrue(out["result"]["name"].endswith(".docx"))
        self.assertNotIn("Sag Bescheid", out["result"]["text"])

    def test_from_answer_uses_the_template_not_the_claim(self):
        history = [{
            "role": "assistant",
            "content": (
                "**Betriebsanweisung: Drucker/Kopierer**\n\n"
                "## 1. Anwendungsbereich\n"
                + "\n".join(f"Regel {i}: Gerät nur nach Anleitung bedienen." for i in range(12))
            ),
        }]
        self.assertIsNone(chat_file.from_answer(
            "wo ist die datei",
            "Erledigt. Die Datei liegt als Link im Chat.",
            history,
        ))
        out = chat_file.from_answer(
            "Betriebsanweisung erstellen",
            "Erledigt. Die Datei liegt als Link im Chat.",
            history,
        )
        self.assertTrue(out["ok"], out)
        self.assertIn("Anwendungsbereich", out["result"]["text"])
        self.assertNotIn("liegt als Link", out["result"]["text"])
        self.assertTrue(out["result"]["name"].endswith(".docx"))

    def test_from_answer_skips_plain_questions(self):
        self.assertIsNone(chat_file.from_answer("Was ist eine GBU?", "Eine GBU ist eine Beurteilung der Arbeit."))
        self.assertFalse(chat_file.wants("Betriebsanweisung Drucker als PDF"))
        self.assertFalse(chat_file.wants("such die Betriebsanweisung Leitern"))
        self.assertTrue(chat_file.wants("Bitte Betriebsanweisung erstellen für den Drucker"))
        self.assertTrue(chat_file.wants("erstellemir eine betriebsanweisung für drucker"))
        self.assertTrue(chat_file.wants("erstelle mir eine Betriebsanweisung für den Drucker"))
        self.assertFalse(chat_file.wants("was steht in der Betriebsanweisung Drucker"))
        self.assertTrue(chat_file.wants(
            "bitte erstelle eine betriebsanweidung Leiter und tritte"
        ))
        self.assertTrue(chat_file.wants(
            "ich benötige eine Betriebsanweisung für handhubwagen"
        ))
        self.assertFalse(chat_file.wants(
            "eine Betriebsanweisung für den Drucker erstellen"
        ))

    def test_placeholder_is_rejected(self):
        out = chat_file.run({
            "title": "Betriebsanweisung",
            "format": "pdf",
            "text": "Stelle dir vor du hättest eine Betriebsanweisung.",
        })
        self.assertFalse(out["ok"])
        self.assertFalse(chat_file.is_betriebsanweisung(
            "Betriebsanweisung", "Stell dir vor du hättest eine.",
        ))

    def test_aushang_is_not_a_betriebsanweisung(self):
        text = (
            "Aushang Leitern\n"
            "1. Anwendungsbereich\n"
            "Nur auf festem Boden.\n"
            "2. Gefahren\n"
            "Abrutschen.\n"
        )
        self.assertFalse(chat_file.is_betriebsanweisung("Aushang Leitern", text))
        out = chat_file.run({"title": "Aushang Leitern", "format": "pdf", "text": text})
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["result"]["name"].endswith(".pdf"))
        self.assertNotIn("Firma:", out["result"]["text"])

    def test_placeholder_answer_uses_the_real_template(self):
        history = [{
            "role": "assistant",
            "content": (
                "Betriebsanweisung: Drucker/Kopierer\n"
                "1. Anwendungsbereich\n"
                "Gilt für alle Beschäftigten.\n"
                "2. Gefahren\n"
                "Heiße Oberflächen.\n"
            ),
        }]
        self.assertIsNone(chat_file.from_answer(
            "PDF",
            "Stelle dir vor du hättest eine Betriebsanweisung.",
            history,
        ))
        out = chat_file.from_answer(
            "Betriebsanweisung erstellen",
            "Stelle dir vor du hättest eine Betriebsanweisung.",
            history,
        )
        self.assertTrue(out["ok"], out)
        self.assertIn("Heiße Oberflächen", out["result"]["text"])
        self.assertNotIn("Stelle dir vor", out["result"]["text"])

    def test_betriebsanweisung_uses_bghm_form(self):
        out = chat_file.run({
            "title": "Betriebsanweisung: Drucker/Kopierer",
            "format": "pdf",
            "text": (
                "1. Anwendungsbereich\n"
                "Gilt für Drucker im Büro.\n"
                "2. Gefahren für Mensch und Umwelt\n"
                "Heiße Fixiereinheit.\n"
                "3. Schutzmaßnahmen und Verhaltensregeln\n"
                "Nur nach Betriebsanleitung bedienen.\n"
                "4. Verhalten bei Störungen und im Gefahrfall\n"
                "Bei Rauch ausschalten.\n"
                "5. Verhalten bei Unfällen – Erste Hilfe\n"
                "Notruf 112.\n"
                "6. Instandhaltung, Entsorgung\n"
                "Tonerkartuschen zurückgeben.\n"
            ),
        })
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["result"]["name"].endswith(".docx"))
        raw = base64.b64decode(out["result"]["data"])
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml = z.read("word/document.xml").decode("utf-8")
        self.assertIn("Gilt für Drucker im Büro.", xml)
        self.assertIn("Heiße Fixiereinheit.", xml)
        self.assertIn("Anwendungsbereich", xml)
        self.assertIn("Firma:", xml)
        self.assertIn("Datum:", out["result"]["text"])
        self.assertNotIn("Stelle dir vor", xml)

    def test_stoerung_und_unfall_landen_in_abschnitt_4_und_5(self):
        text = (
            "Betriebsanweisung: Drucker/Kopierer (Bürogeräte)\n"
            "1. Anwendungsbereich\n"
            "Gilt für alle Beschäftigten, die Drucker/Kopierer im Büro bedienen.\n"
            "2. Gefahren\n"
            "Heiße Oberflächen (Fixiereinheit).\n"
            "3. Verhaltensregeln\n"
            "Gerät nur gemäß Betriebsanleitung bedienen.\n"
            "4. Brandschutz\n"
            "Bei Rauch sofort ausschalten.\n"
            "5. Umwelt/Entsorgung\n"
            "Leere Tonerkartuschen zurückgeben.\n"
            "6. Unterweisung\n"
            "Beschäftigte werden vor Aufnahme der Tätigkeit unterwiesen.\n"
            "7. Verhalten bei Störung/Unfall\n"
            "Störung melden, nicht selbst eingreifen. Erste Hilfe gemäß Aushang.\n"
        )
        sections = chat_file.bghm_sections(text)
        s4 = sections["4. Verhalten bei Störungen und im Gefahrfall"]
        s5 = sections["5. Verhalten bei Unfällen – Erste Hilfe"]
        s6 = sections["6. Instandhaltung, Entsorgung"]
        self.assertIn("Bei Rauch sofort ausschalten.", s4)
        self.assertIn("Störung melden, nicht selbst eingreifen.", s4)
        self.assertNotIn("Erste Hilfe gemäß Aushang.", s4)
        self.assertIn("Erste Hilfe gemäß Aushang.", s5)
        self.assertNotIn("Störung melden", s5)
        self.assertIn("Leere Tonerkartuschen zurückgeben.", s6)
        self.assertIn("vor Aufnahme der Tätigkeit unterwiesen.", s6)
        out = chat_file.run({"title": "Betriebsanweisung Drucker", "format": "pdf", "text": text})
        self.assertTrue(out["ok"], out)
        raw = base64.b64decode(out["result"]["data"])
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml = z.read("word/document.xml").decode("utf-8")
        self.assertIn("Erste Hilfe gemäß Aushang.", xml)
        self.assertIn("Firma:", xml)
        self.assertNotIn("Sag Bescheid", xml)

    def test_gefahren_fuer_mensch_und_umwelt_bleiben_in_abschnitt_2(self):
        text = (
            "Betriebsanweisung Leitern und Tritte\n"
            "1. Anwendungsbereich\n"
            "Gilt für Leitern und Tritte.\n"
            "2. Gefahren für Mensch und Umwelt\n"
            "Absturz aus der Höhe.\n"
            "ISO 7010 W012\n"
            "Stromschlag an spannungsführenden Teilen.\n"
            "kommunale Rücknahme).\n"
            "3. Schutzmaßnahmen und Verhaltensregeln\n"
            "Nur nach Betriebsanleitung arbeiten.\n"
            "5. Verhalten bei Unfällen – Erste Hilfe\n"
            "Notruf 112.\n"
            "6. Instandhaltung, Entsorgung\n"
            "Prüfung durch eine befähigte Person.\n"
        )
        sections = chat_file.bghm_sections(text)
        s1 = sections["1. Anwendungsbereich"]
        s2 = sections["2. Gefahren für Mensch und Umwelt"]
        s3 = sections["3. Schutzmaßnahmen und Verhaltensregeln"]
        s5 = sections["5. Verhalten bei Unfällen – Erste Hilfe"]
        s6 = sections["6. Instandhaltung, Entsorgung"]
        self.assertNotIn("Betriebsanweisung Leitern", s1)
        self.assertIn("Absturz aus der Höhe.", s2)
        self.assertNotIn("W008", s2)
        self.assertIn("ISO 7010 W012", s2)
        self.assertNotIn("Absturz", s6)
        self.assertNotIn("kommunale Rücknahme", s2)
        self.assertIn("Prüfung durch eine befähigte Person.", s6)
        self.assertIn("ISO 7010 M002", s3)
        self.assertNotIn("M012", s3)
        self.assertIn("ISO 7010 E003", s5)
        out = chat_file.run({
            "title": "Betriebsanweisung Leitern und Tritte",
            "format": "docx",
            "text": text,
        })
        self.assertTrue(out["ok"], out)
        raw = base64.b64decode(out["result"]["data"])
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml = z.read("word/document.xml").decode("utf-8")
            rels = z.read("word/_rels/document.xml.rels").decode("utf-8")
            book = z.read("word/media/iso7010-m002.png")
            cross = z.read("word/media/iso7010-e003.png")
            bolt = z.read("word/media/iso7010-w012.png")
        self.assertEqual(xml.count("Absturz aus der Höhe."), 1)
        self.assertNotIn("kommunale Rücknahme", xml)
        self.assertNotIn("M002", xml)
        self.assertNotIn("E003", xml)
        self.assertNotIn("W012", xml)
        self.assertIn('descr="Betriebsanleitung"', xml)
        self.assertIn('descr="Erste Hilfe"', xml)
        self.assertIn('descr="Elektrische Spannung"', xml)
        self.assertIn('w:w="11906"', xml)
        self.assertIn('w:h="16838"', xml)
        self.assertGreaterEqual(xml.count("<w:tbl"), 2)
        self.assertIn("relationships/image", rels)
        self.assertEqual(book, (chat_file._ISO_DIR / "M002.png").read_bytes())
        self.assertEqual(cross, (chat_file._ISO_DIR / "E003.png").read_bytes())
        self.assertGreater(len(book), 2000)
        self.assertGreater(len(cross), 2000)
        self.assertEqual(bolt, (chat_file._ISO_DIR / "W012.png").read_bytes())

    def test_echte_zeichen_und_kein_nachsatz(self):
        text = (
            "Betriebsanweisung Drucker\n"
            "1. Anwendungsbereich\n"
            "Gilt für Drucker im Büro.\n"
            "3. Schutzmaßnahmen und Verhaltensregeln\n"
            "Gerät nur nach Hersteller-Betriebsanleitung bedienen.\n"
            "5. Verhalten bei Unfällen – Erste Hilfe\n"
            "Augen mit Wasser spülen.\n"
            "Grundlagen: BAuA Drucker und Kopierer\n"
        )
        out = chat_file.run({
            "title": "Betriebsanweisung Drucker",
            "format": "docx",
            "text": text,
        })
        self.assertTrue(out["ok"], out)
        plain = out["result"]["text"]
        self.assertTrue(plain.startswith("Betriebsanweisung\n"))
        self.assertNotIn("Betriebsanweisung Drucker", plain)
        self.assertIn("ISO 7010 M002", plain)
        self.assertIn("ISO 7010 E003", plain)
        self.assertNotIn("Grundlagen", plain)
        raw = base64.b64decode(out["result"]["data"])
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml = z.read("word/document.xml").decode("utf-8")
            book = z.read("word/media/iso7010-m002.png")
            cross = z.read("word/media/iso7010-e003.png")
        self.assertNotIn("Grundlagen", xml)
        self.assertNotIn("M002", xml)
        self.assertNotIn("E003", xml)
        self.assertIn("Augen mit Wasser spülen.", xml)
        self.assertEqual(book, (chat_file._ISO_DIR / "M002.png").read_bytes())
        self.assertEqual(cross, (chat_file._ISO_DIR / "E003.png").read_bytes())

    def test_drucker_zeichen_rechts_auf_a4(self):
        text = (
            "Betriebsanweisung Drucker\n"
            "2. Gefahren für Mensch und Umwelt\n"
            "Tonerstaub kann Haut und Atemwege reizen.\n"
            "Heiße Fixiereinheit, Verbrennungsgefahr.\n"
            "4. Verhalten bei Störungen und im Gefahrfall\n"
            "Vor dem Öffnen den Stecker ziehen.\n"
            "3. Schutzmaßnahmen und Verhaltensregeln\n"
            "Kein Hautkontakt, Handschuhe benutzen.\n"
            "Nur nach Betriebsanleitung arbeiten.\n"
            "5. Verhalten bei Unfällen – Erste Hilfe\n"
            "Augen mit Wasser spülen.\n"
        )
        sections = chat_file.bghm_sections(text)
        s2 = sections["2. Gefahren für Mensch und Umwelt"]
        s3 = sections["3. Schutzmaßnahmen und Verhaltensregeln"]
        self.assertTrue(s2.startswith("ISO 7010 W071 · W017 · W012"))
        self.assertTrue(s3.startswith("ISO 7010 M009 · M002"))
        out = chat_file.run({"title": "Betriebsanweisung Drucker", "format": "docx", "text": text})
        self.assertTrue(out["ok"], out)
        raw = base64.b64decode(out["result"]["data"])
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml = z.read("word/document.xml").decode("utf-8")
            dust = z.read("word/media/iso7010-w071.png")
            heat = z.read("word/media/iso7010-w017.png")
            bolt = z.read("word/media/iso7010-w012.png")
            gloves = z.read("word/media/iso7010-m009.png")
        for code in ("W071", "W017", "W012", "M009", "M002", "E003"):
            self.assertNotIn(code, xml)
        self.assertIn('descr="Gesundheitsgefährdender Stoff"', xml)
        self.assertIn('descr="Heiße Oberfläche"', xml)
        self.assertIn('descr="Handschutz"', xml)
        self.assertIn('w:w="11906"', xml)
        self.assertEqual(dust, (chat_file._ISO_DIR / "W071.png").read_bytes())
        self.assertEqual(heat, (chat_file._ISO_DIR / "W017.png").read_bytes())
        self.assertEqual(bolt, (chat_file._ISO_DIR / "W012.png").read_bytes())
        self.assertEqual(gloves, (chat_file._ISO_DIR / "M009.png").read_bytes())

    def test_abschnitte_werden_stichpunkte(self):
        text = (
            "Betriebsanweisung Leitern und Tritte\n"
            "1. Anwendungsbereich\n"
            "Gilt für Leitern auf dem Betriebsgelände, insbesondere:\n"
            "Stehleitern und Anlegeleitern.\n"
            "2. Gefahren für Mensch und Umwelt\n"
            "Absturz von der Leiter.\n"
            "3. Schutzmaßnahmen und Verhaltensregeln\n"
            "Auswahl und Zustand\n"
            "Vor jeder Benutzung prüfen.\n"
            "Nur nach Betriebsanleitung arbeiten.\n"
        )
        sections = chat_file.bghm_sections(text)
        s1 = sections["1. Anwendungsbereich"]
        s2 = sections["2. Gefahren für Mensch und Umwelt"]
        s3 = sections["3. Schutzmaßnahmen und Verhaltensregeln"]
        self.assertIn("insbesondere:", s1)
        self.assertIn("• Stehleitern und Anlegeleitern.", s1)
        self.assertIn("• Absturz von der Leiter.", s2)
        self.assertIn("Auswahl und Zustand\n• Vor jeder Benutzung prüfen.", s3)
        self.assertNotIn("• Auswahl und Zustand", s3)
        out = chat_file.run({"title": "Betriebsanweisung Leitern und Tritte", "format": "docx", "text": text})
        raw = base64.b64decode(out["result"]["data"])
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml = z.read("word/document.xml").decode("utf-8")
        self.assertIn("• Vor jeder Benutzung prüfen.", xml)
        self.assertIn("<w:b", xml)

    def test_fill_leaves_the_form_unchanged(self):
        form = chat_file.bghm_form_path()
        before = form.read_bytes()
        stamp = form.stat().st_mtime_ns
        out = chat_file.run({
            "title": "Betriebsanweisung Drucker",
            "format": "docx",
            "text": "1. Anwendungsbereich\nGilt für Drucker im Büro.\n2. Gefahren\nHeiße Fixiereinheit.\n",
        })
        self.assertTrue(out["ok"], out)
        self.assertEqual(form.read_bytes(), before)
        self.assertEqual(form.stat().st_mtime_ns, stamp)

    def test_reads_the_vault_form_when_it_exists(self):
        import shutil
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "leer.docx"
            shutil.copyfile(chat_file._BGHM_FORM, copy)
            before = copy.read_bytes()
            stamp = copy.stat().st_mtime_ns
            old = chat_file._VAULT_FORM
            chat_file._VAULT_FORM = copy
            try:
                self.assertEqual(chat_file.bghm_form_path(), copy)
                out = chat_file.run({
                    "title": "Betriebsanweisung Drucker",
                    "format": "pdf",
                    "text": "1. Anwendungsbereich\nGilt für Drucker im Büro.\n",
                })
            finally:
                chat_file._VAULT_FORM = old
            self.assertTrue(out["ok"], out)
            self.assertEqual(copy.read_bytes(), before)
            self.assertEqual(copy.stat().st_mtime_ns, stamp)
            raw = base64.b64decode(out["result"]["data"])
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                xml = z.read("word/document.xml").decode("utf-8")
            self.assertIn("Gilt für Drucker im Büro.", xml)

    def test_narrated_tool_block_becomes_the_form(self):
        import json
        payload = {
            "title": "Betriebsanweisung – Drucker",
            "text": (
                "# Betriebsanweisung – Drucker\n\n"
                "Firma: ...........................   BA-Nr.: ...........................\n\n"
                "## 1. Anwendungsbereich\n\n"
                "Gilt für Drucker im Büro.\n\n"
                "## 2. Gefahren für Mensch und Umwelt\n\n"
                "Toner nicht einatmen.\n\n"
                "## 6. Instandhaltung, Entsorgung\n\n"
                "Tonerkartuschen zurückgeben.\n\n"
                "Unterschrift Arbeitgeber: ...........................\n"
            ),
        }
        answer = (
            "### Tool\n"
            "ChatDatei args="
            + json.dumps(payload, ensure_ascii=False)
            + ' result={"ok": true, "file": "Betriebsanweisung_Drucker.docx"}\n\n'
            "### Assistent\n"
            "Einordnung aus den Belegen: hier steht ein Nachsatz, der nicht in die Datei gehört."
        )
        untouched, nothing = chat_file.chat_delivery("was liegt zu Druckern im Vault", answer)
        self.assertIsNone(nothing)
        self.assertNotIn("ChatDatei", untouched)
        self.assertIn("Einordnung", untouched)
        sentence, inner = chat_file.chat_delivery("Betriebsanweisung erstellen", answer)
        self.assertEqual(sentence, chat_file.ONE_LINE)
        self.assertTrue(inner["name"].endswith(".docx"))
        self.assertIn("Toner nicht einatmen.", inner["text"])
        self.assertNotIn("Einordnung", inner["text"])
        self.assertNotIn("BA-Nr", inner["text"])
        self.assertNotIn("Unterschrift Arbeitgeber", inner["text"])
        raw = base64.b64decode(inner["data"])
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml = z.read("word/document.xml").decode("utf-8")
        self.assertIn("Toner nicht einatmen.", xml)
        self.assertIn("Firma:", xml)
        self.assertNotIn("BA-Nr", xml)
        self.assertNotIn("Einordnung", xml)
        spoken = (
            "Betriebsanweisung Drucker\n\n"
            "ChatDatei(\n"
            "  title=\"Betriebsanweisung Drucker\",\n"
            "  format=\"docx\",\n"
            "  text=\"\"\"# Betriebsanweisung\n"
            "Firma: ______________\n"
            "## 1. Anwendungsbereich\n"
            "Gilt für Drucker im Büro.\n"
            "## 2. Gefahren für Mensch und Umwelt\n"
            "Heiße Fixiereinheit.\n"
            "Unterschrift Arbeitgeber: ______________\"\"\"\n"
            ")\n"
        )
        sentence, inner = chat_file.chat_delivery(
            "erstellemir eine betriebsanweisung für drucker", spoken,
        )
        self.assertEqual(sentence, chat_file.ONE_LINE)
        self.assertIn("Heiße Fixiereinheit.", inner["text"])
        self.assertNotIn("Unterschrift Arbeitgeber", inner["text"])
        self.assertNotIn("ChatDatei(", inner["text"])
        follow = (
            "ChatDatei(\n"
            "  title=\"Betriebsanweisung Drucker\",\n"
            "  format=\"docx\",\n"
            "  text=\"\"\"# Betriebsanweisung\n"
            "## 1. Anwendungsbereich\n"
            "Gilt für Drucker im Büro und an Multifunktionsgeräten.\n"
            "## 2. Gefahren für Mensch und Umwelt\n"
            "Heiße Fixiereinheit und Tonerstaub.\n"
            "Unterschrift Arbeitgeber: ______________\"\"\"\n"
            ")\n\n"
            "Die Datei liegt als Link im Chat — die Drucker-Betriebsanweisung ist fertig. "
            "Rechtsgrundlage: BetrSichV.\n"
        )
        sentence, inner = chat_file.chat_delivery(
            "und die Zeichen?",
            follow,
            [{"role": "user", "content": "erstelle mir eine Betriebsanweisung für den Drucker"}],
        )
        self.assertEqual(sentence, chat_file.ONE_LINE)
        self.assertNotIn("ChatDatei", sentence)
        self.assertNotIn("Rechtsgrundlage", sentence)
        self.assertIn("Heiße Fixiereinheit", inner["text"])
        search_answer = follow + ("Hinweis aus dem Vault. " * 40)
        kept, no_file = chat_file.chat_delivery(
            "was steht in der Betriebsanweisung Drucker",
            search_answer,
            [{"role": "user", "content": "erstelle mir eine Betriebsanweisung für den Drucker"}],
        )
        self.assertIsNone(no_file)
        self.assertNotIn("ChatDatei", kept)
        self.assertIn("Hinweis aus dem Vault", kept)
        again = chat_file.from_answer(
            "Betriebsanweisung erstellen", "nochmal", [{"role": "assistant", "content": answer}],
        )
        self.assertTrue(again["ok"], again)
        self.assertIn("Toner nicht einatmen.", again["result"]["text"])

    def test_xml_tool_call_wird_datei_und_kein_block(self):
        answer = (
            "<tool_calls>\n"
            "<tool_call name=\"ChatDatei\">\n"
            "<parameter name=\"title\">Betriebsanweisung: Leitern und Tritte</parameter>\n"
            "<parameter name=\"format\">docx</parameter>\n"
            "<parameter name=\"text\">Betriebsanweisung – Leitern und Tritte\n"
            "1. Anwendungsbereich\n"
            "Gilt für tragbare Leitern und Tritte.\n"
            "2. Gefahren für Mensch und Umwelt\n"
            "Absturz von der Leiter.\n"
            "3. Schutzmaßnahmen und Verhaltensregeln\n"
            "Aufstellen\n"
            "Nur auf festem Untergrund aufstellen.\n"
            "</parameter>\n"
            "</tool_call>\n"
            "</tool_calls>\n"
            "Die Datei liegt als Link im Chat.\n"
        )
        sentence, inner = chat_file.chat_delivery(
            "bitte erstelle eine betriebsanweidung Leiter und tritte",
            answer,
        )
        self.assertEqual(sentence, chat_file.ONE_LINE)
        self.assertNotIn("tool_call", sentence)
        self.assertNotIn("<", sentence)
        self.assertTrue(inner["name"].endswith(".docx"))
        self.assertIn("Absturz von der Leiter.", inner["text"])
        self.assertIn("• Nur auf festem Untergrund aufstellen.", inner["text"])
        self.assertNotIn("tool_call", inner["text"])
        self.assertNotIn("liegt als Link", inner["text"])
        parsed = tool_registry.try_parse_tool_call(answer)
        self.assertEqual(parsed[0], "ChatDatei")
        self.assertIn("Absturz von der Leiter.", parsed[1]["text"])
        self.assertEqual(parsed[1]["format"], "docx")

    def test_colon_call_wird_datei_ohne_fehlertext(self):
        import json
        body = (
            "Betriebsanweisung Handhubwagen\n"
            "1. Anwendungsbereich\n"
            "Gilt für Handhubwagen (Gabelhubwagen).\n"
            "2. Gefahren für Mensch und Umwelt\n"
            "Quetschgefahr an den Gabeln.\n"
            "4. Verhalten bei Störungen und im Gefahrfall\n"
            "Defekt kennzeichnen (z. B. „Außer Betrieb“) und melden.\n"
            "6. Instandhaltung, Entsorgung\n"
            "Hydrauliköl fachgerecht entsorgen.\n"
            "Unterschrift Arbeitgeber: ______\n"
        )
        call = (
            'ChatDatei(title:"Betriebsanweisung Handhubwagen", text:"'
            + body
            + '", format:"docx")'
        )
        answer = (
            "lten, Last ablassen.\n"
            + call
            + " · "
            + call
            + "\n— fehlgeschlagen: Unbekanntes Tool 'ChatDatei(title:\"x\"'\n"
            "Der Datei-Aufruf ist fehlgeschlagen.\n"
        )
        sentence, inner = chat_file.chat_delivery(
            "ich benötige eine Betriebsanweisung für handhubwagen",
            answer,
        )
        self.assertEqual(sentence, chat_file.ONE_LINE)
        self.assertNotIn("ChatDatei", sentence)
        self.assertNotIn("fehlgeschlagen", sentence.lower())
        self.assertIn("Hydrauliköl fachgerecht entsorgen.", inner["text"])
        self.assertIn("Quetschgefahr an den Gabeln.", inner["text"])
        self.assertNotIn("Unterschrift Arbeitgeber", inner["text"])
        self.assertNotIn("Unbekanntes Tool", inner["text"])
        parsed = tool_registry.try_parse_tool_call(
            json.dumps({"tool": call, "args": {}}, ensure_ascii=False)
        )
        self.assertEqual(parsed[0], "ChatDatei")
        self.assertIn("Quetschgefahr an den Gabeln.", parsed[1]["text"])
        self.assertEqual(parsed[1]["format"], "docx")

    def test_name_arguments_wird_datei_und_kein_json(self):
        import json
        payload = {
            "name": "ChatDatei",
            "arguments": {
                "title": "Betriebsanweisung Handhubwagen",
                "format": "docx",
                "text": (
                    "1. Anwendungsbereich\n"
                    "Gilt für Handhubwagen.\n"
                    "2. Gefahren für Mensch und Umwelt\n"
                    "Quetschen an den Gabeln.\n"
                ),
            },
        }
        answer = (
            "Die Datei liegt als Link im Chat.\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        sentence, inner = chat_file.chat_delivery(
            "ich benötige eine Betriebsanweisung für handhubwagen",
            answer,
        )
        self.assertEqual(sentence, chat_file.ONE_LINE)
        self.assertNotIn("{", sentence)
        self.assertNotIn("arguments", sentence)
        self.assertIn("Quetschen an den Gabeln.", inner["text"])
        parsed = tool_registry.try_parse_tool_call(answer)
        self.assertEqual(parsed[0], "ChatDatei")
        self.assertEqual(parsed[1]["format"], "docx")
        leaked = (
            'Die Datei liegt als Link im Chat.\n'
            '{"name": "ChatDatei", "arguments": {"title": "Betriebsanweisung Handhubwagen", "'
        )
        visible, no_file = chat_file.chat_delivery(
            "ich benötige eine Betriebsanweisung für handhubwagen",
            leaked,
        )
        self.assertIsNone(no_file)
        self.assertNotIn("ChatDatei", visible)
        self.assertNotIn("{", visible)

    def test_rejects_empty(self):
        out = chat_file.run({"title": "Leer", "format": "pdf", "text": "  "})
        self.assertFalse(out["ok"])


if __name__ == "__main__":
    unittest.main()
