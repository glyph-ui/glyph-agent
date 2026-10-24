# -*- coding: utf-8 -*-
"""Tests für die Suchbrücke (core.query_bridge).

Der Live-Fehler: „wie oft muss ich das tun?" ging wörtlich an Exa/TinyFish und
lieferte Winterdienst/Postzustellung. Diese Tests sichern die Regeln, die das
verhindern — und den Fallback, wenn das Modell nichts Brauchbares liefert.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import query_bridge


HISTORY_FACH = [
    {"role": "user", "content": (
        "wie sieht's mit gefahrstoffersetzten aus? worauf muss ich schauen und "
        "muss ich alle gefahrstoffe auf den Ersatz prüfen?"
    )},
    {"role": "assistant", "content": (
        "Kurz: ja, grundsätzlich alle Gefahrstoffe, aber in einer priorisierten "
        "Reihenfolge. Laut KomNet (Dialog 3668) gilt § 6 GefStoffV …"
    )},
]


class TestNeedsContext(unittest.TestCase):
    def test_ohne_verlauf_nie(self):
        # Erste Frage war nie das Problem — kein Modellcall, unverändert suchen.
        self.assertFalse(query_bridge.needs_context("wie oft muss ich das tun?", []))
        self.assertFalse(query_bridge.needs_context("wie oft muss ich das tun?", None))

    def test_kurze_rueckfrage(self):
        self.assertTrue(query_bridge.needs_context("wie oft muss ich das tun?", HISTORY_FACH))
        self.assertTrue(query_bridge.needs_context("und wie lange?", HISTORY_FACH))
        self.assertTrue(query_bridge.needs_context("warum?", HISTORY_FACH))

    def test_verweiswort(self):
        self.assertTrue(query_bridge.needs_context(
            "muss ich das wirklich für jeden stoff machen?", HISTORY_FACH))

    def test_eigenstaendige_frage_bleibt(self):
        # Fachbegriff + Norm: kein Brücken-Call, direkt suchen.
        self.assertFalse(query_bridge.needs_context(
            "Welche Fristen gelten nach TRGS 600 für die Ersatzstoffprüfung?",
            HISTORY_FACH))
        self.assertFalse(query_bridge.needs_context(
            "Wie oft muss die Gefährdungsbeurteilung aktualisiert werden?",
            HISTORY_FACH))
        self.assertFalse(query_bridge.needs_context(
            "Was kostet ein Zwergdackel?", HISTORY_FACH))


class TestPlainQuery(unittest.TestCase):
    def test_preis_frage(self):
        # Bestehendes Verhalten (vorher _derive_web_query): Wertbegriff anhängen.
        self.assertEqual(query_bridge.plain_query("was kostet ein zwergdackel?"),
                         "kostet ein zwergdackel Preis")

    def test_ohne_preis(self):
        self.assertEqual(query_bridge.plain_query("  wie oft prüfen? "),
                         "wie oft prüfen?")


class TestHeuristicPlan(unittest.TestCase):
    def test_passthrough_ohne_bezug(self):
        plan = query_bridge.heuristic_plan("TRGS 600 Ersatzstoffprüfung Frist", HISTORY_FACH)
        self.assertEqual(plan["source"], "passthrough")
        self.assertEqual(plan["queries"], ["TRGS 600 Ersatzstoffprüfung Frist"])

    def test_fallback_haengt_fachbegriffe_an(self):
        plan = query_bridge.heuristic_plan("wie oft muss ich das tun?", HISTORY_FACH)
        self.assertEqual(plan["source"], "heuristic")
        q = plan["queries"][0]
        # Der nackte Verweis ist weg, der Fachbegriff aus dem Verlauf drin.
        self.assertIn("gefahrstoffe", q.lower())
        self.assertIn("wie oft muss ich das tun?", q)


class TestKeyTerms(unittest.TestCase):
    def test_normen_und_komposita(self):
        terms = query_bridge.key_terms(
            "Laut TRGS 600 und § 7 GefStoffV prüft man Gefahrstoffe.")
        self.assertIn("TRGS", terms)
        self.assertIn("600", terms)
        self.assertIn("GefStoffV", terms)
        self.assertIn("Gefahrstoffe", terms)

    def test_fulwoerter_raus(self):
        self.assertNotIn("Welche", query_bridge.key_terms("Welche Frist gilt?"))


class TestParsePlan(unittest.TestCase):
    def test_json(self):
        plan = query_bridge.parse_plan(
            '{"standalone": "Wie oft ist die Ersatzstoffprüfung zu wiederholen?",'
            ' "queries": ["Ersatzstoffprüfung Gefahrstoffe Wiederholung Frist",'
            ' "TRGS 600 Ersatzstoffprüfung"], "terms": ["Ersatzstoffprüfung"]}'
        )
        self.assertEqual(len(plan["queries"]), 2)
        self.assertIn("TRGS 600", plan["queries"][1])
        self.assertEqual(plan["terms"], ["Ersatzstoffprüfung"])

    def test_codefence_und_vorrede(self):
        plan = query_bridge.parse_plan(
            "Klar, hier:\n```json\n{\"queries\": [\"Gefahrstoffe Ersatzprüfung Frist\"]}\n```"
        )
        self.assertEqual(plan["queries"], ["Gefahrstoffe Ersatzprüfung Frist"])

    def test_zeilen_ohne_json(self):
        plan = query_bridge.parse_plan("- Gefahrstoffe Ersatzprüfung Frist\n- TRGS 600")
        self.assertEqual(plan["queries"][0], "Gefahrstoffe Ersatzprüfung Frist")

    def test_prosa_ist_keine_anfrage(self):
        self.assertIsNone(query_bridge.parse_plan("Ich habe leider keine Ahnung."))
        self.assertIsNone(query_bridge.parse_plan("Dazu finde ich hier nichts."))

    def test_leer_ist_none(self):
        self.assertIsNone(query_bridge.parse_plan(""))
        self.assertIsNone(query_bridge.parse_plan('{"queries": []}'))


class TestBuildPlan(unittest.TestCase):
    def setUp(self):
        self._chat = query_bridge._chat
        self._local = query_bridge._is_local

    def tearDown(self):
        query_bridge._chat = self._chat
        query_bridge._is_local = self._local

    def test_ohne_bezug_kein_modellcall(self):
        calls = []
        query_bridge._chat = lambda s, u: calls.append(u) or "{}"
        query_bridge._is_local = lambda: False
        plan = query_bridge.build_plan("TRGS 600 Frist", HISTORY_FACH)
        self.assertEqual(plan["source"], "passthrough")
        self.assertEqual(calls, [])

    def test_bruecke_nutzt_das_modell(self):
        seen = {}

        def fake_chat(system, user):
            seen["system"] = system
            seen["user"] = user
            return '{"standalone": "Wie oft ist die Ersatzstoffprüfung fällig?",' \
                   ' "queries": ["Ersatzstoffprüfung Gefahrstoffe Frist Wiederholung"],' \
                   ' "terms": ["Ersatzstoffprüfung"]}'

        query_bridge._chat = fake_chat
        query_bridge._is_local = lambda: False
        steps = []
        plan = query_bridge.build_plan("wie oft muss ich das tun?", HISTORY_FACH,
                                       emit=steps.append)
        self.assertEqual(plan["source"], "llm")
        self.assertEqual(plan["queries"],
                         ["Ersatzstoffprüfung Gefahrstoffe Frist Wiederholung"])
        # Der Verlauf geht mit in den Prompt — sonst ist die Rückfrage nicht auflösbar.
        self.assertIn("Gefahrstoffe", seen["user"])
        self.assertIn("RÜCKFRAGE: wie oft muss ich das tun?", seen["user"])
        self.assertEqual([s["action"] for s in steps], ["SearchBridge", "SearchBridge"])
        self.assertEqual(steps[1]["status"], "done")

    def test_modell_haengt_faellt_auf_regel_zurueck(self):
        def boom(system, user):
            raise TimeoutError("bridge timeout")

        query_bridge._chat = boom
        query_bridge._is_local = lambda: False
        plan = query_bridge.build_plan("wie oft muss ich das tun?", HISTORY_FACH)
        self.assertEqual(plan["source"], "heuristic")
        self.assertIn("gefahrstoffe", plan["queries"][0].lower())

    def test_modellantwort_ohne_json_faellt_zurueck(self):
        query_bridge._chat = lambda s, u: "Ich habe leider keine Ahnung."
        query_bridge._is_local = lambda: False
        plan = query_bridge.build_plan("wie oft muss ich das tun?", HISTORY_FACH)
        self.assertEqual(plan["source"], "heuristic")

    def test_lokaler_kopf_ohne_call(self):
        calls = []
        query_bridge._chat = lambda s, u: calls.append(u) or "{}"
        query_bridge._is_local = lambda: True
        plan = query_bridge.build_plan("wie oft muss ich das tun?", HISTORY_FACH)
        self.assertEqual(plan["source"], "heuristic")
        self.assertEqual(calls, [])


class TestSlimHistory(unittest.TestCase):
    def test_kuerzt_und_haelt_rollen(self):
        long = [{"role": "assistant", "content": "x" * 5000}] + HISTORY_FACH
        turns = query_bridge.slim_history(long)
        self.assertTrue(all(len(t["content"]) <= 720 for t in turns))
        self.assertEqual(turns[-1]["role"], "assistant")

    def test_unbekannte_rollen_raus(self):
        turns = query_bridge.slim_history([{"role": "system", "content": "x"}] * 3)
        self.assertEqual(turns, [])


if __name__ == "__main__":
    unittest.main()
