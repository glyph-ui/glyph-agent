#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""preview_vault_hits: Dateien + Elternordner, ohne LLM."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OK = 0
FAIL = 0


def check(name, cond, detail=""):
    global OK, FAIL
    print(f"  {'✅' if cond else '❌'} {name} {detail}")
    if cond:
        OK += 1
    else:
        FAIL += 1


def test_empty_query():
    print("\n[1] leere Query → keine Treffer:")
    from core import vault_preview

    res = vault_preview.preview_vault_hits("  ")
    check("ok", res.get("ok") is True, str(res.get("ok")))
    check("empty", res.get("status") == "empty" and res.get("hits") == [], str(res))


def test_files_and_parent_folders():
    print("\n[2] VaultFind-Dateien + Elternordner:")
    from core import config, vault_preview
    import core.retrieval as retrieval_mod

    old_find = retrieval_mod.vault_find
    old_paths = list(config.VAULT_PATHS)
    old_path = config.VAULT_PATH
    retrieval_mod.vault_find = lambda query, top_k=None, min_score=None, **kw: {
        "status": "success",
        "query": query,
        "selected": 1,
        "results": [
            {
                "path": "/HSEQ Sync/00 Arbeitsfluss/Eingang/PSA.md",
                "title": "PSA.md",
                "text": "PSA Pflicht im Produktionsbereich.",
                "score": 0.88,
            }
        ],
    }
    try:
        config.VAULT_PATHS = []
        config.VAULT_PATH = ""
        res = vault_preview.preview_vault_hits("PSA")
    finally:
        retrieval_mod.vault_find = old_find
        config.VAULT_PATHS = old_paths
        config.VAULT_PATH = old_path
    kinds = [h.get("kind") for h in res.get("hits") or []]
    paths = [h.get("path") for h in res.get("hits") or []]
    check("ok", res.get("ok") is True, str(res.get("ok")))
    check("folder zuerst", kinds[:1] == ["folder"], f"-> {kinds}")
    check(
        "Elternordner",
        "/HSEQ Sync/00 Arbeitsfluss/Eingang" in paths,
        f"-> {paths}",
    )
    check("Datei", "/HSEQ Sync/00 Arbeitsfluss/Eingang/PSA.md" in paths, f"-> {paths}")
    file_hit = next(h for h in res["hits"] if h["kind"] == "file")
    check("excerpt", "PSA Pflicht" in (file_hit.get("excerpt") or ""), file_hit.get("excerpt"))


def _setup_two_vaults(td):
    hseq = os.path.join(td, "HSEQ Sync")
    asi = os.path.join(td, "ASI, BS. UWS, QM, EM")
    h_as = os.path.join(hseq, "Arbeitssicherheit")
    a_as = os.path.join(asi, "Arbeitssicherheit", "Information")
    os.makedirs(h_as)
    os.makedirs(a_as)
    os.makedirs(os.path.join(hseq, "Themen"))
    for extra in ("Energierecht", "Brandschutz", "Schulung", "SIGIKO"):
        os.makedirs(os.path.join(asi, extra))
    with open(os.path.join(h_as, "00 MOC - Arbeitssicherheit.md"), "w", encoding="utf-8") as f:
        f.write("# HSEQ Hub\nKein Fachwissen, nur Arbeitsfluss.\n")
    with open(os.path.join(h_as, "Allgemeine Information.md"), "w", encoding="utf-8") as f:
        f.write("![[Allgemeine Information 08 _ 2026 – Arbeitsschuhe.msg]]\n")
    with open(
        os.path.join(asi, "Arbeitssicherheit", "00 MOC - Arbeitssicherheit.md"),
        "w",
        encoding="utf-8",
    ) as f:
        f.write("# ASI Archiv\nFachwissen Arbeitssicherheit.\n")
    with open(os.path.join(a_as, "00 MOC - Information.md"), "w", encoding="utf-8") as f:
        f.write("# Information\nArchiv-MOC Information.\n")
    return hseq, asi


def test_hseq_folder_and_new_note_in_preview():
    """Index/ASI-Hits dürfen HSEQ-Ordner + neue Datei nicht verdrängen."""
    print("\n[3] HSEQ Arbeitssicherheit + Allgemeine Information trotz ASI-Index:")
    import tempfile
    from core import config, vault_preview
    import core.retrieval as retrieval_mod

    old_find = retrieval_mod.vault_find
    old_paths = list(config.VAULT_PATHS)
    old_path = config.VAULT_PATH
    retrieval_mod.vault_find = lambda query, top_k=None, min_score=None, **kw: {
        "status": "success",
        "query": query,
        "selected": 8,
        "results": [
            {
                "path": f"/ASI, BS. UWS, QM, EM/Arbeitssicherheit/{sub}/00 MOC - {sub}.md",
                "title": f"00 MOC - {sub}.md",
                "text": f"{sub} im Hauptarchiv.",
                "score": 0.92 - i * 0.01,
            }
            for i, sub in enumerate((
                "Information",
                "Technische Regeln",
                "Maschinen und Arbeitsmittel",
                "Betriebssicherheit",
                "Brand-EX-Schutz",
                "PSA",
                "Baustellen",
                "Betriebsanweisungen",
            ))
        ],
    }
    with tempfile.TemporaryDirectory(prefix="glyph-preview-as-") as td:
        hseq, asi = _setup_two_vaults(td)
        config.VAULT_PATHS = [asi, hseq]  # ASI primär — wie live
        config.VAULT_PATH = asi
        try:
            res = vault_preview.preview_vault_hits("Arbeitssicherheit")
            paths = [h.get("path") for h in res.get("hits") or []]
            kinds = {h.get("path"): h.get("kind") for h in res.get("hits") or []}
            check(
                "HSEQ-Ordner Arbeitssicherheit",
                kinds.get("/HSEQ Sync/Arbeitssicherheit") == "folder",
                f"-> {paths}",
            )
            check(
                "Allgemeine Information.md unter HSEQ",
                "/HSEQ Sync/Arbeitssicherheit/Allgemeine Information.md" in paths,
                f"-> {paths}",
            )

            res2 = vault_preview.preview_vault_hits("Allgemeine Information")
            paths2 = [h.get("path") for h in res2.get("hits") or []]
            check(
                "Datei Allgemeine Information",
                "/HSEQ Sync/Arbeitssicherheit/Allgemeine Information.md" in paths2,
                f"-> {paths2}",
            )
            check(
                "Elternordner zur Datei",
                "/HSEQ Sync/Arbeitssicherheit" in paths2,
                f"-> {paths2}",
            )

            res3 = vault_preview.preview_vault_hits("arbeitsicherheit")
            paths3 = [h.get("path") for h in res3.get("hits") or []]
            check(
                "Tippfehler 1×s findet Ordner",
                "/HSEQ Sync/Arbeitssicherheit" in paths3,
                f"-> {paths3}",
            )

            res4 = vault_preview.preview_vault_hits("was liegt im Ordner Arbeitssicherheit")
            paths4 = [h.get("path") for h in res4.get("hits") or []]
            check(
                "Inventar: HSEQ-Ordner, nicht nur ASI-Root",
                "/HSEQ Sync/Arbeitssicherheit" in paths4,
                f"-> {paths4}",
            )
            check(
                "Inventar: kein ASI-Energierecht-Dump",
                not any((p or "").endswith("/Energierecht") for p in paths4),
                f"-> {paths4}",
            )
        finally:
            retrieval_mod.vault_find = old_find
            config.VAULT_PATHS = old_paths
            config.VAULT_PATH = old_path



def test_canon_strips_disk_home():
    print("\n[4] Disk-Home wird zu /VaultName/rel:")
    from core import config, vault_tools

    old_paths = list(config.VAULT_PATHS)
    old_path = config.VAULT_PATH
    try:
        config.VAULT_PATHS = [
            "/Users/lxndrhbrt/ObsidianVaults/HSEQ Sync",
            "/Users/lxndrhbrt/ObsidianVaults/ASI, BS. UWS, QM, EM",
        ]
        config.VAULT_PATH = config.VAULT_PATHS[0]
        got = vault_tools.canon_vault_path(
            "/lxndrhbrt/ObsidianVaults/HSEQ Sync/Vorlagen/Begehungen/Begehung - Arbeitsschutz KFZ-Werkstatt.md"
        )
        check(
            "HSEQ ohne Home",
            got == "/HSEQ Sync/Vorlagen/Begehungen/Begehung - Arbeitsschutz KFZ-Werkstatt.md",
            got,
        )
        got2 = vault_tools.canon_vault_path(
            "/Users/lxndrhbrt/ObsidianVaults/ASI, BS. UWS, QM, EM/Schulung/029 Arbeitsschutzmanagement"
        )
        check(
            "ASI-Ordner",
            got2 == "/ASI, BS. UWS, QM, EM/Schulung/029 Arbeitsschutzmanagement",
            got2,
        )
        got3 = vault_tools.canon_vault_path("/HSEQ Sync/Themen/PSA.md")
        check("schon kanonisch", got3 == "/HSEQ Sync/Themen/PSA.md", got3)
    finally:
        config.VAULT_PATHS = old_paths
        config.VAULT_PATH = old_path


def test_index_hang_does_not_block_named_hits():
    print("\n[5] langsamer Index blockiert Disk-Treffer nicht:")
    import time as _time
    from core import config, vault_preview
    import core.retrieval as retrieval_mod

    old_find = retrieval_mod.vault_find
    old_paths = list(config.VAULT_PATHS)
    old_path = config.VAULT_PATH
    old_wait = vault_preview.INDEX_WAIT_WITH_HITS_S

    def _slow(*a, **k):
        _time.sleep(8)
        return {"status": "success", "results": []}

    retrieval_mod.vault_find = _slow
    vault_preview.INDEX_WAIT_WITH_HITS_S = 0.4
    with __import__("tempfile").TemporaryDirectory(prefix="glyph-preview-fast-") as td:
        hseq, asi = _setup_two_vaults(td)
        config.VAULT_PATHS = [asi, hseq]
        config.VAULT_PATH = asi
        t0 = _time.monotonic()
        try:
            res = vault_preview.preview_vault_hits("Arbeitssicherheit", budget_s=6)
        finally:
            retrieval_mod.vault_find = old_find
            vault_preview.INDEX_WAIT_WITH_HITS_S = old_wait
            config.VAULT_PATHS = old_paths
            config.VAULT_PATH = old_path
        elapsed = _time.monotonic() - t0
        paths = [h.get("path") for h in res.get("hits") or []]
        check("unter 3s", elapsed < 3.0, f"{elapsed:.2f}s")
        check(
            "HSEQ-Ordner trotzdem da",
            "/HSEQ Sync/Arbeitssicherheit" in paths,
            f"-> {paths}",
        )


def test_same_disk_file_listed_once():
    print("\n[6] dieselbe Datei auf der Platte nur einmal:")
    import tempfile
    from core import config, vault_preview
    import core.retrieval as retrieval_mod

    old_find = retrieval_mod.vault_find
    old_paths = list(config.VAULT_PATHS)
    old_path = config.VAULT_PATH
    retrieval_mod.vault_find = lambda query, top_k=None, min_score=None, **kw: {
        "status": "success",
        "results": [
            {
                "path": "/lxndrhbrt/ObsidianVaults/ASI, BS. UWS, QM, EM/Arbeitssicherheit/00 MOC - Arbeitssicherheit.md",
                "title": "00 MOC - Arbeitssicherheit.md",
                "text": "ASI Archiv",
                "score": 0.9,
            },
            {
                "path": "/ASI, BS. UWS, QM, EM/Arbeitssicherheit/00 MOC - Arbeitssicherheit.md",
                "title": "00 MOC - Arbeitssicherheit.md",
                "text": "ASI Archiv",
                "score": 0.8,
            },
        ],
    }
    with tempfile.TemporaryDirectory(prefix="glyph-preview-id-") as td:
        hseq, asi = _setup_two_vaults(td)
        config.VAULT_PATHS = [asi, hseq, td]
        config.VAULT_PATH = asi
        try:
            res = vault_preview.preview_vault_hits("Arbeitssicherheit")
        finally:
            retrieval_mod.vault_find = old_find
            config.VAULT_PATHS = old_paths
            config.VAULT_PATH = old_path
        files = [h.get("path") for h in res.get("hits") or [] if h.get("kind") == "file" and str(h.get("path") or "").endswith("00 MOC - Arbeitssicherheit.md")]
        junk = [p for p in files if "ObsidianVaults" in p or p.startswith("/lxndrhbrt")]
        check("kein Home-Pfad", junk == [], str(files))
        asi_hits = [p for p in files if p.startswith("/ASI, BS. UWS, QM, EM/")]
        check("ASI-Pfad bleibt", len(asi_hits) >= 1, str(files))


def test_wiki_hits_excluded_from_preview():
    """memory-wiki = internes Gedächtnis: nie Treffer im Auswahl-Panel."""
    print("\n[7] Wiki-Treffer (Datei, Ordner, Eltern) bleiben draußen:")
    import tempfile
    from core import config, vault_preview
    import core.retrieval as retrieval_mod

    old_find = retrieval_mod.vault_find
    old_paths = list(config.VAULT_PATHS)
    old_path = config.VAULT_PATH
    retrieval_mod.vault_find = lambda query, top_k=None, min_score=None, **kw: {
        "status": "success",
        "query": query,
        "selected": 3,
        "results": [
            {
                # Wiki-Datei via Index — muss raus
                "path": "/memory-wiki/Themen/Apfel.md",
                "title": "Apfel.md",
                "text": "Internes Gedächtnis.",
                "score": 0.95,
            },
            {
                # Fach-Vault-Datei — muss bleiben
                "path": "/HSEQ Sync/Themen/Apfel.md",
                "title": "Apfel.md",
                "text": "Arbeits-Vault.",
                "score": 0.85,
            },
            {
                # Wiki-Wurzelordner — muss raus
                "path": "/memory-wiki",
                "title": "memory-wiki",
                "score": 0.8,
            },
        ],
    }
    with tempfile.TemporaryDirectory(prefix="glyph-preview-wiki-") as td:
        wiki = os.path.join(td, "memory-wiki")
        hseq = os.path.join(td, "HSEQ Sync")
        os.makedirs(os.path.join(wiki, "Themen"))
        os.makedirs(os.path.join(hseq, "Themen"))
        with open(os.path.join(wiki, "Themen", "Apfel.md"), "w", encoding="utf-8") as f:
            f.write("# Apfel\nWiki-intern.\n")
        with open(os.path.join(hseq, "Themen", "Apfel.md"), "w", encoding="utf-8") as f:
            f.write("# Apfel\nFachvault.\n")
        config.VAULT_PATHS = [hseq, wiki]
        config.VAULT_PATH = hseq
        try:
            res = vault_preview.preview_vault_hits("Apfel")
        finally:
            retrieval_mod.vault_find = old_find
            config.VAULT_PATHS = old_paths
            config.VAULT_PATH = old_path
    paths = [h.get("path") for h in res.get("hits") or []]
    check("ok", res.get("ok") is True, str(res.get("ok")))
    check(
        "keine Wiki-Pfade",
        not any(str(p).startswith("/memory-wiki") for p in paths),
        f"-> {paths}",
    )
    check("Fach-Vault bleibt", "/HSEQ Sync/Themen/Apfel.md" in paths, f"-> {paths}")
    check(
        "kein Wiki-Ordner",
        "/memory-wiki/Themen" not in paths and "/memory-wiki" not in paths,
        f"-> {paths}",
    )


def main():
    print("=== Vault-Preview ===")
    test_empty_query()
    test_files_and_parent_folders()
    test_hseq_folder_and_new_note_in_preview()
    test_canon_strips_disk_home()
    test_index_hang_does_not_block_named_hits()
    test_same_disk_file_listed_once()
    test_wiki_hits_excluded_from_preview()
    print(f"\n=== Ergebnis: {OK} ok, {FAIL} Fehler ===")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
