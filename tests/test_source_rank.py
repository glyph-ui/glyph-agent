#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rechtsquellen-Rang: Stufe an der URL, amtliche Treffer zuerst, nichts verwerfen."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import source_rank, web


class ClassifyTests(unittest.TestCase):
    def test_gesetz_und_urteil_sind_stufe_1(self):
        g = source_rank.classify("https://www.gesetze-im-internet.de/arbschg/")
        self.assertEqual(g["stufe"], 1)
        self.assertEqual(g["rolle"], "gesetz")
        u = source_rank.classify("https://www.bundesarbeitsgericht.de/entscheidung/1")
        self.assertEqual(u["stufe"], 1)
        self.assertEqual(u["rolle"], "rechtsprechung")
        self.assertEqual(source_rank.classify("https://dejure.org/dienste/vernetzung/rechtsprechung?Gericht=BAG")["rolle"], "rechtsprechung")

    def test_komnet_und_land_sind_stufe_2(self):
        k = source_rank.classify("https://www.komnet.nrw.de/_sitetools/dialog/1")
        self.assertEqual(k["stufe"], 2)
        self.assertEqual(k["rolle"], "behoerde")
        land = source_rank.classify("https://www.gewerbeaufsicht.niedersachsen.de/thema")
        self.assertEqual(land["stufe"], 2)
        self.assertEqual(land["rolle"], "behoerde")

    def test_dguv_vorschrift_und_regel_getrennt(self):
        v = source_rank.classify("https://www.dguv.de/de/versicherung/vorschrift-1")
        self.assertEqual(v["stufe"], 4)
        self.assertEqual(v["rolle"], "uvv")
        r = source_rank.classify("https://publikationen.dguv.de/regel-112-189")
        self.assertEqual(r["stufe"], 3)
        self.assertEqual(r["rolle"], "erlaeuterung")

    def test_ratgeber_ist_stufe_5_unbekannt_offen(self):
        b = source_rank.classify("https://www.wohnschutz.com/schimmel")
        self.assertEqual(b["stufe"], 5)
        self.assertEqual(b["rolle"], "ratgeber")
        self.assertIsNone(source_rank.classify("https://example.com/x"))


class RankSearchTests(unittest.TestCase):
    def test_unfiltered_puts_official_first_and_keeps_ratgeber(self):
        def exa(query, count=5, start_published_date=None, include_domains=None):
            if include_domains:
                self.assertIn("komnet.nrw.de", include_domains)
                self.assertIn("gesetze-im-internet.de", include_domains)
                self.assertIn("dejure.org", include_domains)
                return [{
                    "title": "ArbSchG",
                    "url": "https://www.gesetze-im-internet.de/arbschg/",
                    "snippet": "Gesetz",
                }]
            return [{
                "title": "Blog",
                "url": "https://wohnschutz.com/a",
                "snippet": "Meinung",
            }]

        def tf(query, count=5, location="DE", language="de", include_domains=None):
            return []

        with mock.patch.object(web, "search_exa", side_effect=exa), mock.patch.object(
            web, "search_tinyfish", side_effect=tf
        ), mock.patch.object(web, "fetch_tinyfish", side_effect=lambda *a, **k: ""):
            rows = web.web_search("Leiter prüfen", count=5, source="both")
        urls = [r["url"] for r in rows]
        self.assertTrue(urls[0].startswith("https://www.gesetze-im-internet.de/"))
        self.assertEqual(rows[0]["stufe"], 1)
        self.assertTrue(any("wohnschutz.com" in u for u in urls))
        self.assertEqual(rows[-1]["stufe"], 5)

    def test_explicit_domains_stay_a_hard_filter(self):
        seen = []

        def exa(query, count=5, start_published_date=None, include_domains=None):
            seen.append(include_domains)
            return [{"title": "K", "url": "https://www.komnet.nrw.de/x", "snippet": ""}]

        def tf(query, count=5, location="DE", language="de", include_domains=None):
            seen.append(include_domains)
            return []

        with mock.patch.object(web, "search_exa", side_effect=exa), mock.patch.object(
            web, "search_tinyfish", side_effect=tf
        ):
            web.web_search("PSA", count=5, source="both", include_domains=["komnet.nrw.de"])
        self.assertEqual(seen, [["komnet.nrw.de"], ["komnet.nrw.de"]])


class RelevanceTests(unittest.TestCase):
    def test_stemps_finden_formen_und_stoppwoerter_fliegen(self):
        self.assertEqual(source_rank.query_terms("Bitte such mir die Prüfung"), ["pruf"])
        self.assertIn("leit", source_rank.query_terms("Leiter prüfen"))
        self.assertEqual(source_rank.query_terms("test test test"), ["test"])

    def test_eigenes_wort_schlaegt_kompositum(self):
        eigenes = {"title": "Test für Anfänger", "url": "https://blog.example/test"}
        kompositum = {
            "title": "Coronavirus-Testverordnung",
            "url": "https://gesetze-im-internet.de/tv",
            "snippet": "Gesetz",
        }
        self.assertGreater(
            source_rank.relevance("test", eigenes),
            source_rank.relevance("test", kompositum),
        )

    def test_relevanz_schlaegt_stufe(self):
        """Amtlicher, aber nicht passender Treffer landet hinter dem passenden."""
        offen = [{
            "title": "Leiter prüfen: Anleitung",
            "url": "https://blog.example/leiter-pruefen",
            "snippet": "So prüfst du Leitern",
        }]

        def exa(query, count=5, start_published_date=None, include_domains=None):
            if include_domains:
                return [{
                    "title": "Coronavirus-Testverordnung",
                    "url": "https://www.gesetze-im-internet.de/tv",
                    "snippet": "Gesetz",
                }]
            return offen

        def tf(query, count=5, location="DE", language="de", include_domains=None):
            return []

        with mock.patch.object(web, "search_exa", side_effect=exa), mock.patch.object(
            web, "search_tinyfish", side_effect=tf
        ), mock.patch.object(web, "fetch_tinyfish", side_effect=lambda *a, **k: ""):
            rows = web.web_search("Leiter prüfen", count=5, source="both")
        self.assertIn("blog.example", rows[0]["url"])
        self.assertGreater(rows[0]["relevance"], 0)
        # nichts verworfen: der Amtstreffer steht, aber hinten
        self.assertIn("gesetze-im-internet.de", rows[-1]["url"])
        self.assertEqual(rows[-1]["relevance"], 0)


class AmtssucheNurBeiRechtsfragenTests(unittest.TestCase):
    def _run(self, query):
        seen = []

        def exa(q, count=5, start_published_date=None, include_domains=None):
            seen.append(include_domains)
            return []

        def tf(q, count=5, location="DE", language="de", include_domains=None):
            seen.append(include_domains)
            return []

        with mock.patch.object(web, "search_exa", side_effect=exa), mock.patch.object(
            web, "search_tinyfish", side_effect=tf
        ):
            web.web_search(query, count=5, source="both")
        return seen

    def test_ohne_rechtsbezug_keine_amtssuche(self):
        seen = self._run("test test test")
        self.assertTrue(all(d is None for d in seen), seen)

    def test_rechtsfrage_startet_amtssuche(self):
        seen = self._run("Leiter prüfen")
        self.assertTrue(any(d for d in seen), seen)

    def test_is_legal_query(self):
        from core import research
        self.assertFalse(research.is_legal_query("test test test"))
        self.assertFalse(research.is_legal_query("Was bedeutet dieses Wort?"))
        self.assertFalse(research.is_legal_query("finden und isolieren"))
        self.assertTrue(research.is_legal_query("Prüfung der Leiter"))
        self.assertTrue(research.is_legal_query("Was sagt das ArbSchG?"))
        self.assertTrue(research.is_legal_query("DGUV Vorschrift 1"))
        self.assertTrue(research.is_legal_query("DIN 2304"))


class TopVolltextTests(unittest.TestCase):
    def test_top2_bekommen_text_rest_bleibt_snippet(self):
        import json

        rows = [
            {"title": "A", "url": "https://a.example/1", "snippet": "test"},
            {"title": "B", "url": "https://b.example/2", "snippet": "test"},
            {"title": "C", "url": "https://c.example/3", "snippet": "test"},
        ]

        def exa(query, count=5, start_published_date=None, include_domains=None):
            return list(rows)

        def tf(query, count=5, location="DE", language="de", include_domains=None):
            return []

        def fetch(url, fmt="markdown", timeout=None):
            return json.dumps({"content": f"Volltext von {url}"})

        with mock.patch.object(web, "search_exa", side_effect=exa), mock.patch.object(
            web, "search_tinyfish", side_effect=tf
        ), mock.patch.object(web, "fetch_tinyfish", side_effect=fetch):
            out = web.web_search("test", count=5, source="both")
        self.assertIn("Volltext von", out[0].get("text", ""))
        self.assertIn("Volltext von", out[1].get("text", ""))
        self.assertNotIn("text", out[2])

    def test_fetchausfall_laesst_suche_stehen(self):
        rows = [{"title": "A", "url": "https://a.example/1", "snippet": "test"}]

        def exa(query, count=5, start_published_date=None, include_domains=None):
            return list(rows)

        def tf(query, count=5, location="DE", language="de", include_domains=None):
            return []

        with mock.patch.object(web, "search_exa", side_effect=exa), mock.patch.object(
            web, "search_tinyfish", side_effect=tf
        ), mock.patch.object(web, "fetch_tinyfish", side_effect=RuntimeError("down")):
            out = web.web_search("test", count=5, source="both")
        self.assertEqual(len(out), 1)
        self.assertNotIn("text", out[0])


if __name__ == "__main__":
    unittest.main()
