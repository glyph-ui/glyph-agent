#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests: ListVaultDir + verbesserte Keyword-Suche (Dateiname/Tokens).

Kein Netzwerk, kein Ollama. Nutzt echte Vault-Pfade aus config, falls vorhanden;
sonst temporären Mini-Vault.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import vault_tools, tool_registry, tool_loop, config


FZ_STEM = "DGUV Information 209-007 Fahrzeuginstandhaltung"
HL_STEM = "Handlungsleitfaden_DGUV_Vorschrift_BGHM"
GB_STEM = "07_00 Muster-Gefaehrdungsbeurteilungen der BGHM"


class DistinctivePdfNameScoreTests(unittest.TestCase):
    """209-007 / Fahrzeuginstandhaltung schlaegt generisches DGUV/Handlungsleitfaden."""

    def test_number_and_rare_word_beat_generic_dguv(self):
        for q in ("209-007", "209", "007", "Fahrzeuginstandhaltung"):
            fz = vault_tools.name_match_score(q, FZ_STEM)
            hl = vault_tools.name_match_score(q, HL_STEM)
            self.assertGreaterEqual(fz, 70, (q, fz))
            self.assertGreater(fz, hl, (q, fz, hl))
            self.assertLess(hl, 70, (q, hl))

    def test_dguv_plus_209007_does_not_prefer_handlungsleitfaden(self):
        q = "DGUV 209-007 Fahrzeuginstandhaltung"
        fz = vault_tools.name_match_score(q, FZ_STEM)
        hl = vault_tools.name_match_score(q, HL_STEM)
        gb = vault_tools.name_match_score(q, GB_STEM)
        self.assertGreaterEqual(fz, 70, fz)
        self.assertGreater(fz, hl, (fz, hl))
        self.assertLess(hl, 70, hl)
        self.assertLess(gb, 70, gb)

    def test_digit_tokens_kept_in_filename(self):
        n_sp, n_c, _n_d = vault_tools._fold_name(FZ_STEM)
        toks = [t for t in n_sp.split() if t]
        self.assertIn("209", toks)
        self.assertIn("007", toks)
        self.assertIn("209007", n_c)

    def test_krane_still_beats_vorlagen(self):
        q = "Was ist in einem KFZ Betrieb zu Krane zu beachten, was muss vorliegen?"
        self.assertGreaterEqual(vault_tools.name_match_score(q, "016_Krane"), 70)
        self.assertLess(vault_tools.name_match_score(q, "Vorlagen"), 70)



class NameMatchCoverageTests(unittest.TestCase):
    """Multi-Token: kein starker Score nur durch ein kurzes Neben-Token."""

    KANTEN_PDF = "GBO-Kanten-Langband-Tellerschleifmaschine.pdf"

    def test_stolperkate_kanten_does_not_boost_kanten_pdf(self):
        q = "Stolperkate Kanten"
        self.assertLess(
            vault_tools.name_match_score(q, self.KANTEN_PDF),
            70,
            "nur Token kanten darf Stolperkate-Query nicht stark matchen",
        )

    def test_longest_token_hit_keeps_strong_score(self):
        q = "Stolperkate Kanten"
        self.assertGreaterEqual(
            vault_tools.name_match_score(q, "Stolperkate-etwas.pdf"),
            70,
            "längstes Distinkt-Token stolperkate muss stark bleiben",
        )

    def test_single_token_kanten_still_strong(self):
        self.assertGreaterEqual(
            vault_tools.name_match_score("Kanten", self.KANTEN_PDF),
            70,
        )

    def test_two_matching_tokens_still_strong(self):
        self.assertGreaterEqual(
            vault_tools.name_match_score("Kanten Langband", self.KANTEN_PDF),
            70,
        )


class TokenizeAndSearchTests(unittest.TestCase):
    def test_tokenize_keeps_date_drops_stopwords(self):
        toks = vault_tools._tokenize_query(
            "Im eingang sollten weitere dateien liegen wie z.b. 2026-06-29"
        )
        self.assertIn("2026-06-29", toks)
        self.assertIn("eingang", toks)
        self.assertNotIn("sollten", toks)
        self.assertNotIn("wie", toks)

    def test_search_vault_hits_filename_date(self):
        """Datum nur im Dateinamen muss Treffer erzeugen (Kernbug)."""
        with tempfile.TemporaryDirectory(prefix="glyph-vault-") as td:
            eingang = os.path.join(td, "00 Arbeitsfluss", "Eingang")
            os.makedirs(eingang)
            path = os.path.join(eingang, "2026-06-29 Internes Audit Rosier.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write("# Audit\nInhalt ohne Datumsstring, nur Rosier Sylt.\n")
            old = list(config.VAULT_PATHS)
            try:
                config.VAULT_PATHS = [td]
                config.VAULT_PATH = td
                hits = vault_tools.search_vault("2026-06-29", limit=10)
                paths = [h["path"] for h in hits]
                self.assertTrue(
                    any("2026-06-29" in p for p in paths),
                    f"Dateiname nicht gefunden: {paths}",
                )
                # Natürliche Frage (früher 0 Treffer durch Fullstring-Match)
                hits2 = vault_tools.search_vault(
                    "Im eingang sollten weitere dateien liegen wie z.b. 2026-06-29",
                    limit=10,
                )
                paths2 = [h["path"] for h in hits2]
                self.assertTrue(
                    any("2026-06-29" in p for p in paths2),
                    f"Token-Suche fehlgeschlagen: {paths2}",
                )
            finally:
                config.VAULT_PATHS = old
                config.VAULT_PATH = old[0] if old else td


class ListVaultDirTests(unittest.TestCase):
    def test_list_vault_dir_entries(self):
        with tempfile.TemporaryDirectory(prefix="glyph-vault-") as td:
            eingang = os.path.join(td, "00 Arbeitsfluss", "Eingang")
            os.makedirs(eingang)
            for name in (
                "2026-06-29 Internes Audit Rosier.md",
                "2026-07-15 Umweltschutz Senger.md",
                "README.md",
            ):
                with open(os.path.join(eingang, name), "w", encoding="utf-8") as f:
                    f.write("x\n")
            old = list(config.VAULT_PATHS)
            try:
                config.VAULT_PATHS = [td]
                config.VAULT_PATH = td
                res = vault_tools.list_vault_dir("00 Arbeitsfluss/Eingang")
                self.assertEqual(res["status"], "success", res)
                names = {e["name"] for e in res["entries"]}
                self.assertIn("2026-06-29 Internes Audit Rosier.md", names)
                self.assertIn("2026-07-15 Umweltschutz Senger.md", names)
                self.assertGreaterEqual(res["count"], 3)
            finally:
                config.VAULT_PATHS = old
                config.VAULT_PATH = old[0] if old else td

    def test_list_vault_dir_blocked_and_outside(self):
        with tempfile.TemporaryDirectory(prefix="glyph-vault-") as td:
            old = list(config.VAULT_PATHS)
            try:
                config.VAULT_PATHS = [td]
                config.VAULT_PATH = td
                res = vault_tools.list_vault_dir("/tmp")
                self.assertEqual(res["status"], "error")
                self.assertEqual(res["count"], 0)
            finally:
                config.VAULT_PATHS = old
                config.VAULT_PATH = old[0] if old else td

    def test_registry_and_execute(self):
        names = {t["name"] for t in tool_registry.TOOLS}
        self.assertIn("ListVaultDir", names)
        self.assertFalse(tool_registry.TOOL_MAP["ListVaultDir"]["write"])
        with tempfile.TemporaryDirectory(prefix="glyph-vault-") as td:
            os.makedirs(os.path.join(td, "sub"))
            with open(os.path.join(td, "sub", "a.md"), "w", encoding="utf-8") as f:
                f.write("hi")
            old = list(config.VAULT_PATHS)
            try:
                config.VAULT_PATHS = [td]
                config.VAULT_PATH = td
                out = tool_registry.execute(
                    "ListVaultDir", {"path": "sub"}, mode="agent"
                )
                self.assertTrue(out["ok"], out)
                self.assertEqual(out["result"]["status"], "success")
                self.assertGreaterEqual(out["result"]["count"], 1)
            finally:
                config.VAULT_PATHS = old
                config.VAULT_PATH = old[0] if old else td


class ListQuestionRoutingTests(unittest.TestCase):
    def test_is_list_question_and_path(self):
        self.assertTrue(tool_loop._is_vault_list_question(
            "Welche dokkumente liegen bei mir in Obsidian Ordner sync im eingang?"
        ))
        self.assertTrue(tool_loop._is_vault_list_question(
            "Im eingang sollten weitere dateien liegen wie z.b. 2026-06-29"
        ))
        self.assertFalse(tool_loop._is_vault_list_question(
            "Was ist der Unterschied zwischen Arbeitsschutz und Arbeitssicherheit?"
        ))
        self.assertEqual(
            tool_loop._infer_vault_list_path("was liegt im Eingang?"),
            "00 Arbeitsfluss/Eingang",
        )
        self.assertEqual(
            tool_loop._infer_vault_list_path("Zeig Fertig-Ordner"),
            "00 Arbeitsfluss/Fertig",
        )


class MatchVaultNameTests(unittest.TestCase):
    """Ordner-/Dateiname auf Disk, unabhängig vom Embedding-Index."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="glyph-name-match-")
        self.hseq = os.path.join(self.td.name, "HSEQ Sync")
        self.asi = os.path.join(self.td.name, "ASI, BS. UWS, QM, EM")
        os.makedirs(os.path.join(self.hseq, "Arbeitssicherheit"))
        os.makedirs(os.path.join(self.asi, "Arbeitssicherheit", "Information"))
        wiki = os.path.join(self.td.name, "memory-wiki")
        sources = os.path.join(wiki, "sources")
        os.makedirs(sources)
        with open(
            os.path.join(self.hseq, "Arbeitssicherheit", "Allgemeine Information.md"),
            "w",
            encoding="utf-8",
        ) as f:
            f.write("![[hinweis.msg]]\n")
        with open(
            os.path.join(sources, "unsafe-local-arbeitssicherheit-abc.md"),
            "w",
            encoding="utf-8",
        ) as f:
            f.write("# Kopie Arbeitssicherheit\n")
        self.old_paths = list(config.VAULT_PATHS)
        self.old_path = config.VAULT_PATH
        config.VAULT_PATHS = [self.asi, self.hseq, wiki]
        config.VAULT_PATH = self.asi

    def tearDown(self):
        config.VAULT_PATHS = self.old_paths
        config.VAULT_PATH = self.old_path
        self.td.cleanup()

    def test_match_both_arbeitssicherheit_folders(self):
        hits = vault_tools.match_vault_entries("Arbeitssicherheit")
        folders = [h["path"] for h in hits if h.get("kind") == "folder"]
        self.assertIn("/HSEQ Sync/Arbeitssicherheit", folders, hits)
        self.assertIn("/ASI, BS. UWS, QM, EM/Arbeitssicherheit", folders, hits)

    def test_match_allgemeine_information_file(self):
        hits = vault_tools.match_vault_entries("Allgemeine Information")
        files = [h["path"] for h in hits if h.get("kind") == "file"]
        self.assertIn(
            "/HSEQ Sync/Arbeitssicherheit/Allgemeine Information.md",
            files,
            hits,
        )

    def test_match_typo_one_s(self):
        hits = vault_tools.match_vault_entries("arbeitsicherheit")
        folders = [h["path"] for h in hits if h.get("kind") == "folder"]
        self.assertIn("/HSEQ Sync/Arbeitssicherheit", folders, hits)

    def test_infer_list_paths_named_folder(self):
        paths = tool_loop._infer_vault_list_paths("was liegt im Ordner Arbeitssicherheit")
        self.assertIn("/HSEQ Sync/Arbeitssicherheit", paths, paths)
        self.assertNotIn(".", paths)

    def test_match_skips_wiki_sources(self):
        hits = vault_tools.match_vault_entries("Arbeitssicherheit")
        paths = [h["path"] for h in hits]
        self.assertFalse(
            any("sources" in (p or "").lower() or "unsafe-local" in (p or "").lower() for p in paths),
            paths,
        )

    def test_sentence_hits_krane_pdf_not_folder_dump(self):
        """Satzfrage: Dateiname-Token reicht; kein Vorlagen/Information-Dump."""
        vorlagen = os.path.join(self.asi, "Arbeitssicherheit", "Vorlagen")
        info = os.path.join(self.asi, "Arbeitssicherheit", "Information")
        betrieb = os.path.join(self.asi, "Arbeitssicherheit", "Betriebssicherheit")
        os.makedirs(vorlagen, exist_ok=True)
        os.makedirs(info, exist_ok=True)
        os.makedirs(betrieb, exist_ok=True)
        with open(os.path.join(vorlagen, "016_Krane.pdf"), "wb") as f:
            f.write(b"%PDF-1.4\n")
        with open(os.path.join(betrieb, "00 MOC - Betriebssicherheit.md"), "w", encoding="utf-8") as f:
            f.write("x\n")
        with open(
            os.path.join(info, "DGUV Information 209-007 Fahrzeuginstandhaltung.pdf"),
            "wb",
        ) as f:
            f.write(b"%PDF-1.4\n")
        for i in range(12):
            with open(os.path.join(vorlagen, f"v-{i:02d}.md"), "w", encoding="utf-8") as f:
                f.write("x\n")
            with open(os.path.join(info, f"i-{i:02d}.md"), "w", encoding="utf-8") as f:
                f.write("x\n")
        q = "Was ist in einem KFZ Betrieb zu Krane zu beachten, was muss vorliegen?"
        self.assertGreaterEqual(
            vault_tools.name_match_score(q, "016_Krane"),
            70,
            "Satz muss 016_Krane treffen",
        )
        self.assertGreaterEqual(
            vault_tools.name_match_score("Kran im KFZ", "016_Krane"),
            70,
            "kran muss krane treffen",
        )
        self.assertLess(
            vault_tools.name_match_score(q, "Vorlagen"),
            70,
            "vorliegen darf Vorlagen nicht treffen",
        )
        hits = vault_tools.match_vault_entries(q)
        files = [h["path"] for h in hits if h.get("kind") == "file"]
        folders = [h["path"] for h in hits if h.get("kind") == "folder"]
        self.assertTrue(
            any(p.endswith("016_Krane.pdf") for p in files),
            files,
        )
        self.assertTrue(
            files and files[0].endswith("016_Krane.pdf"),
            f"Krane nicht vorn: {files}",
        )
        self.assertFalse(
            any(p.endswith("00 MOC - Betriebssicherheit.md") for p in files),
            files,
        )
        self.assertFalse(
            any((p or "").rstrip("/").endswith("/Vorlagen") for p in folders),
            folders,
        )
        self.assertFalse(
            any((p or "").rstrip("/").endswith("/Information") for p in folders),
            folders,
        )
        dump = [
            p
            for p in files
            if "/Information/" in p and not p.endswith("016_Krane.pdf")
            and "Fahrzeuginstandhaltung" not in p
        ]
        self.assertEqual(dump, [], files)


class RankingPrefersLiveOverArchive(unittest.TestCase):
    """Primär-Vault / Arbeitsfluss vor Wiki-sources/unsafe-local-Hash-Kopien."""

    def test_live_beats_unsafe_local_slug(self):
        with tempfile.TemporaryDirectory(prefix="glyph-rank-") as td:
            hseq = os.path.join(td, "HSEQ Sync")
            wiki = os.path.join(td, "memory-wiki")
            eingang = os.path.join(hseq, "00 Arbeitsfluss", "Eingang")
            sources = os.path.join(wiki, "sources")
            os.makedirs(eingang)
            os.makedirs(sources)
            live = "2026-08-01 Internes Audit Live.md"
            with open(os.path.join(eingang, live), "w", encoding="utf-8") as f:
                f.write("# Live\nKurzer Inhalt ohne Spam.\n")
            # Lange Archiv-Kopie mit gleichem Datum + Hash-Slug (früher #1)
            arch = (
                "unsafe-local-00-arbeitsfluss-70dc75d2-2026-08-01-"
                "internes-audit-live-md-d1978aae.md"
            )
            with open(os.path.join(sources, arch), "w", encoding="utf-8") as f:
                f.write("# Kopie\n" + ("2026-08-01 " * 40) + "\n")
            old = list(config.VAULT_PATHS)
            try:
                config.VAULT_PATHS = [hseq, wiki]
                config.VAULT_PATH = hseq
                hits = vault_tools.search_vault("2026-08-01", limit=10)
                self.assertTrue(hits, "keine Treffer")
                top = hits[0]
                self.assertEqual(top["vault"], "HSEQ Sync", hits)
                self.assertIn("Eingang", top["path"], hits)
                self.assertNotIn("unsafe-local", top["path"].lower(), hits)
                # Natürliche Inventar-Frage
                hits2 = vault_tools.search_vault(
                    "welche dateien liegen im eingang 2026-08-01", limit=5
                )
                self.assertTrue(hits2)
                self.assertIn("Eingang", hits2[0]["path"], hits2)
            finally:
                config.VAULT_PATHS = old
                config.VAULT_PATH = old[0] if old else hseq


class IndexSlashPathResolve(unittest.TestCase):
    """VaultFind-Index liefert '/HSEQ Sync/…' — ListVaultDir/ReadNote müssen das akzeptieren."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="glyph-index-slash-")
        self.hseq = os.path.join(self.td.name, "HSEQ Sync")
        themen = os.path.join(self.hseq, "Themen")
        os.makedirs(themen)
        with open(os.path.join(themen, "PSA.md"), "w", encoding="utf-8") as f:
            f.write("# PSA\nPflicht im Produktionsbereich.\n")
        self.old_paths = list(config.VAULT_PATHS)
        self.old_path = config.VAULT_PATH
        config.VAULT_PATHS = [self.hseq]
        config.VAULT_PATH = self.hseq

    def tearDown(self):
        config.VAULT_PATHS = self.old_paths
        config.VAULT_PATH = self.old_path
        self.td.cleanup()

    def test_resolve_leading_slash_vault_prefix(self):
        want = os.path.realpath(os.path.join(self.hseq, "Themen"))
        self.assertEqual(
            vault_tools._resolve_vault_path("/HSEQ Sync/Themen"),
            want,
        )
        self.assertEqual(
            vault_tools._resolve_vault_path("HSEQ Sync/Themen"),
            want,
        )

    def test_list_vault_dir_index_slash(self):
        res = vault_tools.list_vault_dir("/HSEQ Sync/Themen")
        self.assertEqual(res["status"], "success", res)
        names = {e["name"] for e in res["entries"]}
        self.assertIn("PSA.md", names)
        self.assertNotIn("außerhalb", (res.get("error") or "").lower())

    def test_read_note_index_slash(self):
        note = vault_tools.read_note("/HSEQ Sync/Themen/PSA.md")
        self.assertIn("Produktionsbereich", note["content"])

    def test_outside_abs_still_denied(self):
        res = vault_tools.list_vault_dir("/tmp")
        self.assertEqual(res["status"], "error")
        self.assertIn("außerhalb", res["error"])
        self.assertIn("HSEQ Sync", res["error"])

    def test_error_names_bound_vaults_not_unbound(self):
        res = vault_tools.list_vault_dir("/gibt-es-nicht")
        self.assertEqual(res["status"], "error")
        self.assertIn("angebunden: HSEQ Sync", res["error"])
        self.assertNotIn("nicht als Vault angebunden", res["error"])

    def test_role_line_lists_bound_vault(self):
        line = tool_loop._bound_vaults_role_line()
        self.assertIn("HSEQ Sync", line)
        self.assertIn("Angebundene Vaults", line)
        self.assertIn("/Name/Ordner", line)
        bound = line.split("Pfad", 1)[0]
        self.assertNotIn("Privat", bound)


class VaultPrefixPathResolve(unittest.TestCase):
    """ReadNote mit Vault-Präfix 'HSEQ Sync/…' muss greifen (Job-Pfade)."""

    def test_prefix_resolve_live_if_present(self):
        from core import vault_tools, config

        roots = getattr(config, "VAULT_PATHS", [])
        if not roots:
            self.skipTest("keine VAULT_PATHS")
        hseq = None
        for r in roots:
            if "HSEQ Sync" in r and os.path.isdir(r):
                hseq = r
                break
        if not hseq:
            self.skipTest("HSEQ Sync nicht konfiguriert")
        # relativ ohne Präfix
        a = vault_tools.read_note("00 Arbeitsfluss/Eingang/README.md")
        self.assertTrue(a.get("content") is not None or a.get("chars", 0) >= 0)
        # mit Vault-Präfix (früher: Notiz nicht gefunden)
        b = vault_tools.read_note("HSEQ Sync/00 Arbeitsfluss/Eingang/README.md")
        self.assertEqual(a.get("chars"), b.get("chars"))


class LiveEingangSmoke(unittest.TestCase):
    """Optional: echter HSEQ-Vault — dynamisch, keine hardcodierte Datumsnummer."""

    def test_live_eingang_prefers_current_files(self):
        hseq = None
        for p in getattr(config, "VAULT_PATHS", []):
            if os.path.basename(os.path.realpath(p)) == "HSEQ Sync":
                hseq = p
                break
        if not hseq or not os.path.isdir(hseq):
            self.skipTest("HSEQ Sync Vault nicht konfiguriert")
        res = vault_tools.list_vault_dir("00 Arbeitsfluss/Eingang")
        self.assertEqual(res["status"], "success", res)
        entries = [e for e in (res.get("entries") or []) if e.get("type") == "file"]
        md = [e for e in entries if (e.get("name") or "").endswith(".md")
              and (e.get("name") or "").lower() != "readme.md"]
        self.assertTrue(md, f"Eingang ohne Arbeitsdateien: {entries}")
        # Datumstoken aus *aktueller* Datei, nicht hardcodiert
        import re
        date_tok = None
        sample_name = md[0]["name"]
        m = re.search(r"\d{4}-\d{2}-\d{2}", sample_name)
        if m:
            date_tok = m.group(0)
        hits = vault_tools.search_vault(
            date_tok or sample_name.split()[0], limit=15
        )
        paths = [h["path"] for h in hits]
        vaults = [h.get("vault") for h in hits]
        # Top-Treffer: HSEQ Sync, kein unsafe-local-Archiv
        self.assertTrue(hits, "Suche leer")
        self.assertEqual(hits[0].get("vault"), "HSEQ Sync", list(zip(vaults, paths)))
        self.assertFalse(
            any("unsafe-local" in (p or "").lower() for p in paths[:3]),
            f"Archiv-Kopien in Top-3: {paths[:3]}",
        )
        # Mindestens ein Treffer im Arbeitsfluss (Eingang oder Fertig)
        self.assertTrue(
            any(
                "00 Arbeitsfluss" in (h.get("path") or "")
                and h.get("vault") == "HSEQ Sync"
                for h in hits
            ),
            f"Kein HSEQ-Arbeitsfluss-Treffer: {paths}",
        )


if __name__ == "__main__":
    unittest.main()
