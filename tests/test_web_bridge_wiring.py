# -*- coding: utf-8 -*-
"""Verdrahtung der Suchbrücke im Tool-Loop (core.tool_loop).

Sichert: Treffer-Mischung, geteilter Plan (EIN Brücken-Call pro Turn) und dass
eine vom Modell gestellte Rückfrage die Brücke passiert, eine eigenständige
Query aber unverändert bleibt.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import query_bridge, tool_loop


HISTORY = [
    {"role": "user", "content": (
        "muss ich alle Gefahrstoffe auf Ersatz prüfen? TRGS 600"
    )},
    {"role": "assistant", "content": "Kurz: ja, priorisiert nach Gefährdung."},
]


class MergeWebHitsTests(unittest.TestCase):
    def test_dedupe_nach_url_und_reihum(self):
        a = [{"url": "https://a"}, {"url": "https://b"}]
        b = [{"url": "https://b"}, {"url": "https://c"}]
        got = tool_loop._merge_web_hits([a, b], limit=3)
        self.assertEqual([r["url"] for r in got],
                         ["https://a", "https://b", "https://c"])

    def test_limit(self):
        got = tool_loop._merge_web_hits([[{"url": f"https://{i}"} for i in range(10)]],
                                        limit=4)
        self.assertEqual(len(got), 4)

    def test_leer(self):
        self.assertEqual(tool_loop._merge_web_hits([[], None, []]), [])

    def test_ohne_url_faellt_auf_titel(self):
        got = tool_loop._merge_web_hits([[{"title": "TRGS 600"}, {"title": "TRGS 600"}],
                                         [{"title": "KomNet"}]], limit=5)
        self.assertEqual(len(got), 2)


class WebPlanSharingTests(unittest.TestCase):
    def setUp(self):
        self._build = query_bridge.build_plan
        self.calls = []

    def tearDown(self):
        query_bridge.build_plan = self._build

    def test_plan_wird_geteilt(self):
        def fake(user_message, prior_history=None, emit=None):
            self.calls.append(user_message)
            return {"standalone": "", "queries": ["Gefahrstoffe Ersatz Frist"],
                    "terms": [], "source": "llm"}

        query_bridge.build_plan = fake
        holder = {}
        p1 = tool_loop._web_plan("wie oft muss ich das tun?", HISTORY, holder)
        p2 = tool_loop._web_plan("wie oft muss ich das tun?", HISTORY, holder)
        self.assertIs(p1, p2)
        self.assertEqual(len(self.calls), 1)


class BridgedWebArgsTests(unittest.TestCase):
    def setUp(self):
        self._build = query_bridge.build_plan
        self._needs = query_bridge.needs_context
        self.followup = "wie oft muss ich das tun?"
        self.standalone = "TRGS 600 Ersatzstoffprüfung Frist"

    def tearDown(self):
        query_bridge.build_plan = self._build
        query_bridge.needs_context = self._needs

    def _stub(self, queries):
        query_bridge.build_plan = lambda um, ph=None, emit=None: {
            "standalone": "", "queries": queries, "terms": [], "source": "llm"}

    def test_rueckfrage_wird_ersetzt(self):
        self._stub(["Ersatzstoffprüfung Gefahrstoffe Wiederholung Frist"])
        args = tool_loop._bridged_web_args(
            {"query": self.followup}, self.followup, HISTORY)
        self.assertEqual(args["query"],
                         "Ersatzstoffprüfung Gefahrstoffe Wiederholung Frist")

    def test_eigenstaendige_query_bleibt(self):
        self._stub(["SOLL NICHT VERWENDET WERDEN"])
        args = tool_loop._bridged_web_args(
            {"query": self.standalone}, self.standalone, HISTORY)
        self.assertEqual(args["query"], self.standalone)

    def test_ohne_verlauf_keine_aenderung(self):
        self._stub(["SOLL NICHT VERWENDET WERDEN"])
        args = tool_loop._bridged_web_args({"query": self.followup}, self.followup, [])
        self.assertEqual(args["query"], self.followup)

    def test_leere_query_bleibt(self):
        self._stub(["x"])
        self.assertEqual(tool_loop._bridged_web_args({}, self.followup, HISTORY), {})

    def test_quelle_und_count_bleiben(self):
        self._stub(["Ersatzstoffprüfung Frist"])
        args = tool_loop._bridged_web_args(
            {"query": self.followup, "count": 7, "source": "both"},
            self.followup, HISTORY)
        self.assertEqual(args["count"], 7)
        self.assertEqual(args["source"], "both")


class DerivedQueryFallbackTests(unittest.TestCase):
    def test_delegiert_an_die_bruecke(self):
        self.assertEqual(tool_loop._derive_web_query("was kostet ein zwergdackel?"),
                         query_bridge.plain_query("was kostet ein zwergdackel?"))


if __name__ == "__main__":
    unittest.main()
