#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ordner-Suche: leerer Vault → KomNet einmal, sonst DGUV (Exa+TinyFish)."""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import vault_preview, vault_web_fallback, web


class FallbackOrderTests(unittest.TestCase):
    def test_vault_hits_skip_web(self):
        called = []

        def komnet(_q):
            called.append("komnet")
            return [{"kind": "web", "path": "https://www.komnet.nrw.de/x", "source": "komnet"}]

        fb = vault_web_fallback.fallback_web_hits(
            "PSA", vault_hit_count=2, search_komnet=komnet, search_dguv=lambda q: called.append("dguv") or []
        )
        self.assertEqual(fb["hits"], [])
        self.assertEqual(called, [])

    def test_komnet_hits_skip_dguv(self):
        called = []
        komnet_hit = {
            "id": "web:https://www.komnet.nrw.de/_sitetools/dialog/1",
            "kind": "web",
            "path": "https://www.komnet.nrw.de/_sitetools/dialog/1",
            "title": "KomNet",
            "excerpt": "x",
            "source": "komnet",
        }

        def komnet(_q):
            called.append("komnet")
            return [komnet_hit]

        def dguv(_q):
            called.append("dguv")
            return [{"kind": "web", "path": "https://www.dguv.de/x", "source": "dguv"}]

        fb = vault_web_fallback.fallback_web_hits(
            "PSA", vault_hit_count=0, search_komnet=komnet, search_dguv=dguv
        )
        self.assertEqual(fb["source"], "komnet")
        self.assertEqual(fb["tried"], ["komnet"])
        self.assertEqual(fb["hits"][0]["path"], komnet_hit["path"])
        self.assertEqual(called, ["komnet"])

    def test_empty_komnet_falls_to_dguv(self):
        called = []
        dguv_hit = {
            "id": "web:https://www.dguv.de/de/psa.jsp",
            "kind": "web",
            "path": "https://www.dguv.de/de/psa.jsp",
            "title": "PSA",
            "excerpt": "DGUV Regel",
            "source": "dguv",
        }

        def komnet(_q):
            called.append("komnet")
            return []

        def dguv(_q):
            called.append("dguv")
            return [dguv_hit]

        fb = vault_web_fallback.fallback_web_hits(
            "PSA", vault_hit_count=0, search_komnet=komnet, search_dguv=dguv
        )
        self.assertEqual(fb["source"], "dguv")
        self.assertEqual(fb["tried"], ["komnet", "dguv"])
        self.assertEqual(called, ["komnet", "dguv"])
        self.assertEqual(fb["hits"][0]["source"], "dguv")

    def test_komnet_restricts_domain_via_search(self):
        seen = {}
        rows = [
            {"title": "Dialog", "url": "https://www.komnet.nrw.de/_sitetools/dialog/1", "snippet": "ArbSchG"},
            {"title": "Fremd", "url": "https://example.com/psa", "snippet": "nein"},
        ]

        def search(q, count=8, include_domains=None):
            seen["q"] = q
            seen["domains"] = include_domains
            return rows

        hits = vault_web_fallback.search_komnet_site("PSA", search=search)
        self.assertEqual(seen["q"], "PSA")
        self.assertIn("komnet.nrw.de", seen["domains"])
        urls = [h["path"] for h in hits]
        self.assertEqual(urls, ["https://www.komnet.nrw.de/_sitetools/dialog/1"])
        self.assertEqual(hits[0]["source"], "komnet")
        self.assertEqual(hits[0]["kind"], "web")

    def test_dguv_keeps_only_dguv_hosts(self):
        rows = [
            {"title": "BG", "url": "https://www.dguv.de/de/psa.jsp", "snippet": "Regel"},
            {"title": "Arzt", "url": "https://diva-online.dguv.de/diva-online/?typ=kliniken", "snippet": "Klinik"},
            {"title": "Fremd", "url": "https://example.com/psa", "snippet": "nein"},
            {"title": "Pub", "url": "https://publikationen.dguv.de/regel-112-189", "snippet": "PSA"},
        ]
        hits = vault_web_fallback.search_dguv(
            "PSA", search=lambda q, count=8, include_domains=None: rows
        )
        urls = [h["path"] for h in hits]
        self.assertIn("https://www.dguv.de/de/psa.jsp", urls)
        self.assertIn("https://publikationen.dguv.de/regel-112-189", urls)
        self.assertTrue(all(h["source"] == "dguv" for h in hits))
        self.assertFalse(any("diva-online" in u or "example.com" in u for u in urls))


class WebSearchBothTests(unittest.TestCase):
    def test_both_merges_exa_then_tinyfish_unique(self):
        exa = [{"title": "A", "url": "https://a.example/x", "snippet": "1"}]
        tf = [
            {"title": "A2", "url": "https://a.example/x/", "snippet": "dup"},
            {"title": "B", "url": "https://b.example/y", "snippet": "2"},
        ]
        with mock.patch.object(web, "search_exa", return_value=exa), mock.patch.object(
            web, "search_tinyfish", return_value=tf
        ):
            rows = web.web_search("q", count=5, source="both")
        urls = [r["url"] for r in rows]
        self.assertEqual(urls, ["https://a.example/x", "https://b.example/y"])

    def test_both_keeps_tinyfish_if_exa_fails(self):
        tf = [{"title": "T", "url": "https://tf.example/z", "snippet": "ok"}]

        def boom(*_a, **_k):
            raise RuntimeError("exa down")

        with mock.patch.object(web, "search_exa", side_effect=boom), mock.patch.object(
            web, "search_tinyfish", return_value=tf
        ):
            rows = web.web_search("q", count=5, source="both")
        self.assertEqual(rows[0]["url"], "https://tf.example/z")

    def test_default_source_is_both(self):
        self.assertEqual(web.web_search.__defaults__[1], "both")


class PreviewHooksFallback(unittest.TestCase):
    def test_preview_uses_fallback_when_vault_empty(self):
        from core import config
        import core.retrieval as retrieval_mod
        import core.vault_tools as vault_tools_mod

        old_find = retrieval_mod.vault_find
        old_match = vault_tools_mod.match_vault_entries
        old_paths = list(config.VAULT_PATHS)
        old_path = config.VAULT_PATH
        retrieval_mod.vault_find = lambda query, top_k=None, min_score=None, **kw: {
            "status": "empty",
            "query": query,
            "selected": 0,
            "results": [],
        }
        vault_tools_mod.match_vault_entries = lambda q: []
        config.VAULT_PATHS = []
        config.VAULT_PATH = ""
        komnet_hit = {
            "id": "web:https://www.komnet.nrw.de/_sitetools/dialog/9",
            "kind": "web",
            "path": "https://www.komnet.nrw.de/_sitetools/dialog/9",
            "title": "Dialog",
            "excerpt": "KomNet",
            "source": "komnet",
        }
        try:
            with mock.patch.object(
                vault_web_fallback,
                "fallback_web_hits",
                return_value={"hits": [komnet_hit], "tried": ["komnet"], "source": "komnet"},
            ):
                res = vault_preview.preview_vault_hits("unbekanntesfachwortxyz")
        finally:
            retrieval_mod.vault_find = old_find
            vault_tools_mod.match_vault_entries = old_match
            config.VAULT_PATHS = old_paths
            config.VAULT_PATH = old_path
        self.assertEqual(res["fallback"], "komnet")
        self.assertEqual(res["hits"][0]["kind"], "web")
        self.assertEqual(res["status"], "success")


class PreviewWeakEmbeddingTests(unittest.TestCase):
    def test_fallback_when_only_weak_embedding_hits(self):
        from core import config
        import core.retrieval as retrieval_mod
        import core.vault_tools as vault_tools_mod

        old_find = retrieval_mod.vault_find
        old_match = vault_tools_mod.match_vault_entries
        old_paths = list(config.VAULT_PATHS)
        old_path = config.VAULT_PATH
        retrieval_mod.vault_find = lambda query, top_k=None, min_score=None, **kw: {
            "status": "success",
            "query": query,
            "selected": 2,
            "results": [
                {"path": "/HSEQ Sync/x.md", "title": "x", "section": "", "text": "",
                 "score": 0.40, "vector_score": 0.40, "keyword_score": 0.0},
                {"path": "/HSEQ Sync/y.md", "title": "y", "section": "", "text": "",
                 "score": 0.45, "vector_score": 0.45, "keyword_score": 0.0},
            ],
        }
        vault_tools_mod.match_vault_entries = lambda q: []
        config.VAULT_PATHS = []
        config.VAULT_PATH = ""
        komnet_hit = {
            "id": "web:https://www.komnet.nrw.de/x",
            "kind": "web",
            "path": "https://www.komnet.nrw.de/x",
            "title": "KomNet Antwort",
            "excerpt": "KomNet",
            "source": "komnet",
        }
        try:
            with mock.patch.object(
                vault_web_fallback,
                "fallback_web_hits",
                return_value={"hits": [komnet_hit], "tried": ["komnet"], "source": "komnet"},
            ):
                res = vault_preview.preview_vault_hits("zxqvwurpfj")
        finally:
            retrieval_mod.vault_find = old_find
            vault_tools_mod.match_vault_entries = old_match
            config.VAULT_PATHS = old_paths
            config.VAULT_PATH = old_path
        self.assertEqual(res["fallback"], "komnet")
        self.assertEqual(res["hits"][0]["kind"], "web")


class PreviewBudgetTests(unittest.TestCase):
    def test_named_hits_survive_hanging_vault_find(self):
        from core import config, vault_preview
        import core.retrieval as retrieval_mod
        import core.vault_tools as vault_tools_mod

        old_find = retrieval_mod.vault_find
        old_match = vault_tools_mod.match_vault_entries
        old_paths = list(config.VAULT_PATHS)
        old_path = config.VAULT_PATH
        started = []

        def hang(_query, top_k=None, min_score=None, **kw):
            started.append(True)
            time.sleep(2)
            return {
                "status": "success",
                "query": _query,
                "selected": 1,
                "results": [
                    {
                        "path": "/HSEQ Sync/should-not-appear.md",
                        "title": "late",
                        "text": "too late",
                        "score": 0.99,
                    }
                ],
            }

        retrieval_mod.vault_find = hang
        vault_tools_mod.match_vault_entries = lambda q: [
            {
                "kind": "folder",
                "path": "/HSEQ Sync/Arbeitssicherheit/PSA",
                "title": "PSA",
                "score": 100,
            }
        ]
        config.VAULT_PATHS = []
        config.VAULT_PATH = ""
        t0 = time.monotonic()
        try:
            res = vault_preview.preview_vault_hits("PSA", budget_s=0.25)
        finally:
            retrieval_mod.vault_find = old_find
            vault_tools_mod.match_vault_entries = old_match
            config.VAULT_PATHS = old_paths
            config.VAULT_PATH = old_path
        elapsed = time.monotonic() - t0
        paths = [h.get("path") for h in res.get("hits") or []]
        self.assertLess(elapsed, 1.0)
        self.assertTrue(started)
        self.assertIn("/HSEQ Sync/Arbeitssicherheit/PSA", paths)
        self.assertNotIn("/HSEQ Sync/should-not-appear.md", paths)
        self.assertEqual(res.get("ok"), True)

    def test_fallback_skipped_when_deadline_passed(self):
        called = []

        def komnet(_q):
            called.append("komnet")
            return [{"kind": "web", "path": "https://www.komnet.nrw.de/x", "source": "komnet"}]

        fb = vault_web_fallback.fallback_web_hits(
            "PSA",
            vault_hit_count=0,
            search_komnet=komnet,
            deadline=time.monotonic() - 1,
        )
        self.assertEqual(fb["hits"], [])
        self.assertEqual(called, [])

    def test_preview_budget_fits_cloudflare(self):
        from core import vault_preview

        self.assertLessEqual(vault_preview.PREVIEW_BUDGET_S, 70)
        self.assertGreaterEqual(vault_preview.PREVIEW_BUDGET_S, 20)


class SelectedWebContextTests(unittest.TestCase):
    def test_web_hits_do_not_list_vault_dir(self):
        from core import tool_loop

        selected = [
            {
                "id": "web:https://www.komnet.nrw.de/_sitetools/dialog/1",
                "kind": "web",
                "path": "https://www.komnet.nrw.de/_sitetools/dialog/1",
                "title": "PSA",
                "excerpt": "ArbSchG",
                "source": "komnet",
            }
        ]
        listed = []

        def fake_list(path):
            listed.append(path)
            return {"path": path, "count": 0, "entries": []}

        orig = tool_loop._run_list_vault_dir
        tool_loop._run_list_vault_dir = fake_list
        try:
            out = tool_loop._selected_vault_outcome(selected, "PSA")
        finally:
            tool_loop._run_list_vault_dir = orig
        self.assertEqual(listed, [])
        tools = [c["tool"] for c in out.get("tool_calls") or []]
        self.assertEqual(tools, ["WebSearch"])
        self.assertNotIn("VaultFind", tools)
        self.assertIn("komnet.nrw.de", out.get("history_append") or "")

    def test_normalize_keeps_web_kind(self):
        import server as agent_server

        out = agent_server._normalize_vault_selected(
            [
                {
                    "kind": "web",
                    "path": "https://www.dguv.de/de/psa.jsp",
                    "title": "PSA",
                    "excerpt": "Regel",
                    "source": "dguv",
                }
            ]
        )
        self.assertEqual(out[0]["kind"], "web")
        self.assertEqual(out[0]["source"], "dguv")


if __name__ == "__main__":
    unittest.main()
