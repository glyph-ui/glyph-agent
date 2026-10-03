# -*- coding: utf-8 -*-
"""Tests für core/pdf_index.py: Vektorspeicher, Suche, Dedup, Excerpt, FTS."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import pdf_index, retrieval


def _reset_cache():
    pdf_index._CACHE.update({"gen": None, "V": None, "live": None, "rowids": None, "n": 0})


class PdfIndexTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="glyph-pdfidx-")
        self._old_index = retrieval.INDEX_PATH
        retrieval.INDEX_PATH = os.path.join(self.td.name, "vault_index.json")
        _reset_cache()
        self.conn = pdf_index.connect()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        retrieval.INDEX_PATH = self._old_index
        _reset_cache()
        self.td.cleanup()

    def _vec(self, i):
        v = [0.0] * pdf_index.DIM
        v[i % pdf_index.DIM] = 1.0
        return v

    def _add(self, path, texts):
        chunks = [{"section": f"S{n}", "text": t} for n, t in enumerate(texts)]
        return pdf_index.add_file(
            self.conn, path, os.path.basename(path), chunks,
            [self._vec(i) for i in range(len(texts))],
            {"mtime": 1.0, "size": 2, "status": "ok", "chars": sum(len(t) for t in texts), "engine": "pdftotext"},
        )

    def test_store_and_search_orders_by_similarity(self):
        self._add("/V/a.pdf", ["Sicherheitsschränke Lagerung Gefahrstoffe", "Drehmomentschluessel Pruefintervall"])
        res = pdf_index.search(self._vec(0), candidate_k=2)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["path"], "/V/a.pdf")
        self.assertAlmostEqual(res[0]["score"], 1.0, places=4)
        self.assertGreaterEqual(res[0]["score"], res[1]["score"])

    def test_search_empty_store_is_empty(self):
        self.assertEqual(pdf_index.search(self._vec(0), candidate_k=5), [])

    def test_global_dedup_skips_identical_chunk(self):
        r1 = self._add("/V/a.pdf", ["identischer Text ueber Gefahrstoffe im Lager"])
        r2 = self._add("/V/b.pdf", ["identischer Text ueber Gefahrstoffe im Lager"])
        self.assertEqual(r1["chunks"], 1)
        self.assertEqual(r2["chunks"], 0)
        self.assertEqual(r2["skipped"], 1)

    def test_duplicate_file_gets_dup_of_and_excerpt(self):
        """Reine Duplikate verweisen auf die kanonische Datei — Vorschau bleibt gefüllt."""
        self._add("/V/a.pdf", ["identischer Text ueber Gefahrstoffe im Lager"])
        r = self._add("/V/b.pdf", ["identischer Text ueber Gefahrstoffe im Lager"])
        self.assertEqual(r["chunks"], 0)
        self.assertEqual(r["dup_of"], "/V/a.pdf")
        st = pdf_index.file_state(self.conn, "/V/b.pdf")
        self.assertEqual(st["status"], "dup")
        self.assertEqual(st["dup_of"], "/V/a.pdf")
        self.assertIn("Gefahrstoffe", pdf_index.excerpt_for("/V/b.pdf"))

    def test_non_duplicate_file_has_no_dup_of(self):
        self._add("/V/a.pdf", ["eigener Text eins"])
        self._add("/V/b.pdf", ["voellig anderer Text zwei"])
        st = pdf_index.file_state(self.conn, "/V/b.pdf")
        self.assertEqual(st["status"], "ok")
        self.assertIsNone(st["dup_of"])

    def test_readd_replaces_path(self):
        self._add("/V/a.pdf", ["erste Fassung des Textes"])
        self._add("/V/a.pdf", ["zweite Fassung des Textes"])
        rows = self.conn.execute("SELECT text FROM docs WHERE path=?", ("/V/a.pdf",)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("zweite", rows[0][0])

    def test_excerpt_for(self):
        self._add("/V/a.pdf", ["Betriebsanweisung Umgang mit Augenschutz BGR 192"])
        self.assertIn("BGR 192", pdf_index.excerpt_for("/V/a.pdf"))
        self.assertEqual(pdf_index.excerpt_for("/V/fehlt.pdf"), "")

    def test_keyword_search_fts(self):
        self._add("/V/a.pdf", ["Pruefmittelliste fehlt Drehmomentschluessel Intervall"])
        self._add("/V/b.pdf", ["voellig anderer Inhalt ueber Brandschutzordnung"])
        hits = pdf_index.keyword_search("Drehmomentschluessel")
        self.assertTrue(hits, hits)
        self.assertEqual(hits[0]["path"], "/V/a.pdf")

    def test_file_state_roundtrip(self):
        self._add("/V/a.pdf", ["Inhalt"])
        st = pdf_index.file_state(self.conn, "/V/a.pdf")
        self.assertEqual(st["status"], "ok")
        self.assertEqual(st["mtime"], 1.0)
        self.assertEqual(st["chunks"], 1)

    def test_roots_filter(self):
        self._add("/Vault A/a.pdf", ["Text ueber Lagerung"])
        self._add("/Vault B/b.pdf", ["Text ueber Lagerung"])
        res = pdf_index.search(self._vec(0), candidate_k=5, roots=["/Vault A"])
        self.assertTrue(all(r["path"].startswith("/Vault A") for r in res), res)

    def test_stats(self):
        self._add("/V/a.pdf", ["eins", "zwei"])
        s = pdf_index.stats()
        self.assertEqual(s["chunks"], 2)
        self.assertEqual(s["paths"], 1)
        self.assertGreater(s["vectors_bytes"], 0)


class RecallTimeTests(PdfIndexTests):
    """Zeitpunkt- und Differenz-Recall (docs_history)."""

    def _set_ts(self, path, ts):
        self.conn.execute("UPDATE docs SET ts=? WHERE path=?", (ts, path))
        self.conn.commit()

    def _set_until(self, path, ts):
        self.conn.execute("UPDATE docs_history SET until_ts=? WHERE path=?", (ts, path))
        self.conn.commit()

    def test_ts_epoch_accepts_formats(self):
        self.assertEqual(pdf_index.ts_epoch("2026-01-01T00:00:00+0000"), 1767225600)
        self.assertEqual(pdf_index.ts_epoch("2026-01-01T00:00:00Z"), 1767225600)
        self.assertEqual(pdf_index.ts_epoch("2026-01-01T00:00:00+00:00"), 1767225600)
        self.assertIsNotNone(pdf_index.ts_epoch("2026-01-01"))
        self.assertEqual(pdf_index.ts_epoch(1788094188), 1788094188)
        self.assertIsNone(pdf_index.ts_epoch("kein Datum"))

    def test_parse_time_rejects_garbage(self):
        with self.assertRaises(ValueError):
            pdf_index.parse_time("vorgestern")

    def test_readd_archives_old_chunks(self):
        self._add("/V/a.pdf", ["erste Fassung ueber Gefahrstoffe"])
        self._add("/V/a.pdf", ["zweite Fassung ueber Brandschutz"])
        rows = self.conn.execute(
            "SELECT text, until_ts, reason FROM docs_history WHERE path=?", ("/V/a.pdf",)
        ).fetchall()
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("erste Fassung", rows[0][0])
        self.assertIsNotNone(rows[0][1])
        self.assertEqual(rows[0][2], "replaced")

    def test_search_as_of_finds_superseded_version(self):
        self._add("/V/a.pdf", ["erste Fassung ueber Gefahrstoffe"])
        self._set_ts("/V/a.pdf", "2026-01-01T10:00:00+0000")
        self._add("/V/a.pdf", ["zweite Fassung ueber Brandschutz"])
        self._set_ts("/V/a.pdf", "2026-06-01T10:00:00+0000")
        self._set_until("/V/a.pdf", "2026-06-01T10:00:00+0000")

        alt = pdf_index.search_as_of(self._vec(0), "2026-03-01")
        self.assertEqual(len(alt), 1, alt)
        self.assertIn("erste Fassung", alt[0]["text"])
        self.assertEqual(alt[0]["version"], "history")

        neu = pdf_index.search_as_of(self._vec(0), "2026-09-01")
        self.assertEqual(len(neu), 1, neu)
        self.assertIn("zweite Fassung", neu[0]["text"])
        self.assertEqual(neu[0]["version"], "current")

    def test_search_as_of_before_anything_is_empty(self):
        self._add("/V/a.pdf", ["irgendein Text"])
        self._set_ts("/V/a.pdf", "2026-06-01T10:00:00+0000")
        self.assertEqual(pdf_index.search_as_of(self._vec(0), "2026-01-01"), [])

    def test_changes_since_reports_added_and_changed(self):
        self._add("/V/alt.pdf", ["erste Fassung"])
        self._set_ts("/V/alt.pdf", "2026-01-01T10:00:00+0000")
        self._add("/V/alt.pdf", ["zweite Fassung"])
        self._set_ts("/V/alt.pdf", "2026-06-01T10:00:00+0000")
        self._set_until("/V/alt.pdf", "2026-06-01T10:00:00+0000")
        self._add("/V/neu.pdf", ["frischer Inhalt"])
        self._set_ts("/V/neu.pdf", "2026-06-01T10:00:00+0000")

        res = pdf_index.changes_since("2026-03-01")
        self.assertEqual(res["counts"]["changed"], 1, res)
        self.assertEqual(res["changed"][0]["path"], "/V/alt.pdf")
        self.assertEqual(res["counts"]["added"], 1, res)
        self.assertEqual(res["added"][0]["path"], "/V/neu.pdf")
        self.assertEqual(res["counts"]["removed"], 0, res)

    def test_changes_since_ignores_unchanged_reindex(self):
        """Neu indexiert, aber identischer Text -> kein Eintrag (Vergleich ueber text_sha)."""
        self._add("/V/a.pdf", ["unveraenderter Text"])
        self._set_ts("/V/a.pdf", "2026-01-01T10:00:00+0000")
        self._add("/V/a.pdf", ["unveraenderter Text"])
        self._set_ts("/V/a.pdf", "2026-06-01T10:00:00+0000")
        self._set_until("/V/a.pdf", "2026-06-01T10:00:00+0000")
        res = pdf_index.changes_since("2026-03-01")
        self.assertEqual(res["counts"], {"added": 0, "changed": 0, "removed": 0}, res)

    def test_changes_since_reports_removed(self):
        self._add("/V/a.pdf", ["verschwindet wieder"])
        self._set_ts("/V/a.pdf", "2026-01-01T10:00:00+0000")
        self._add("/V/a.pdf", [], [])          # Datei ohne Text -> Chunks weg
        self._set_until("/V/a.pdf", "2026-06-01T10:00:00+0000")
        res = pdf_index.changes_since("2026-03-01")
        self.assertEqual(res["counts"]["removed"], 1, res)
        self.assertEqual(res["removed"][0]["path"], "/V/a.pdf")


if __name__ == "__main__":
    unittest.main()
