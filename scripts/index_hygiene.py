#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Index-Hygiene — Datenschutz-Nachweis für die Suche.

Der Vektorindex (vault_index.json) und der PDF-Index speisen VaultFind und die
Volltextsuche — was dort liegt, sieht das Modell auf Zuruf. Diese Prüfung
belegt, dass die Ausschlussregeln greifen (QM-Kontrolle, sie löscht nichts):

1. **Wiki-Sources:** heikle Dateien unter memory-wiki/sources (Lektion des
   OpenClaw-Unsafe-Local-Ingest).
2. **Vault-Index:** heikle Pfade im semantischen Index.
3. **PDF-Index:** heikle Pfade in pdf_index.sqlite (Datei-Ebene) — vor 2026-10-02
   ungeprüft, dabei liegen dort ~2.900 PDFs.
4. **Privat-Leak:** Pfade aus privaten Vaults in JEDEM Index — härteste
   Invariante: private Vaults sind per ReadNote lesbar, dürfen aber nie
   indexiert sein, ganz gleich wie eine Datei heißt.

Schreibt Bericht nach logs/index_hygiene_report.json. Exit 1 bei Befund.
Läuft nach jedem Index-Aufbau automatisch mit (vault_index_refresh.py).

Aufruf: python3 scripts/index_hygiene.py
Copyright (c) 2026 Alexander Hubert — MIT License
"""
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import config  # noqa: E402

config.reload_vault_paths()

REPORT_PATH = os.path.join(ROOT, "logs", "index_hygiene_report.json")
PDF_DB = os.path.join(ROOT, "logs", "pdf_index.sqlite")

# Muster, die im Wiki/Index heikel sind (Lektion OpenClaw unsafe-local / Privat-Spiegel)
# Nicht: generische HSEQ-unsafe-local-themen/schulung (Fachkopien).
HEIKLE_PATTERNS = [
    r"privat",
    r"passwort",
    r"password",
    r"geheim",
    r"personenbezogen",
    r"secrets?",
    r"health",
    r"behörden-recht",
    r"behoerden-recht",
    r"behörd",
    r"behoerd",
    r"familie-",
    r"unterhalt",
    r"jugendamt",
    r"recovery",
    r"_recovery",
    r"wiki-import",
    r"unsafe-local-behörd",
    r"unsafe-local-behoerd",
    r"unsafe-local-familie",
    r"unsafe-local-finanzen",
]

WIKI = None
for v in getattr(config, "VAULT_PATHS", []):
    if "memory-wiki" in v or "OpenClaw" in v:
        WIKI = v
        break


def _matches(path: str):
    low = path.lower()
    return [p for p in HEIKLE_PATTERNS if re.search(p, low)]


def scan_wiki_sources():
    """Heikle Dateien unter memory-wiki/sources."""
    out = []
    if not WIKI or not os.path.isdir(WIKI):
        return out, []
    sources = os.path.join(WIKI, "sources")
    if not os.path.isdir(sources):
        return out, []
    alle = []
    for root, _dirs, files in os.walk(sources):
        for fn in files:
            if not fn.endswith(".md"):
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, WIKI)
            alle.append(rel)
            m = _matches(rel)
            if m:
                out.append({"path": rel, "patterns": m, "size": os.path.getsize(full)})
    return out, alle


def scan_index():
    """Heikle Pfade im semantischen Vault-Index. Liefert (Treffer, alle Pfade)."""
    idx_path = os.path.join(ROOT, "logs", "vault_index.json")
    if not os.path.isfile(idx_path):
        return [], []
    with open(idx_path, encoding="utf-8") as f:
        data = json.load(f)
    pfade = sorted({d.get("path") or "" for d in (data.get("docs") or [])})
    out = [{"path": p, "patterns": m} for p in pfade if (m := _matches(p))]
    return out, pfade


def scan_pdf_index():
    """Heikle Pfade in der PDF-Index-Dateiliste. Liefert (Treffer, alle Pfade)."""
    if not os.path.isfile(PDF_DB):
        return [], []
    try:
        conn = sqlite3.connect(f"file:{PDF_DB}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        # Sandbox-fest: notfalls über eine Arbeitskopie lesen
        tmp = os.path.join(tempfile.gettempdir(), "pdf_index_hygiene.sqlite")
        shutil.copyfile(PDF_DB, tmp)
        conn = sqlite3.connect(tmp)
    try:
        rows = [r[0] or "" for r in conn.execute("select path from files").fetchall()]
    finally:
        conn.close()
    pfade = sorted(set(rows))
    out = [{"path": p, "patterns": m} for p in pfade if (m := _matches(p))]
    return out, pfade


def scan_private_leak(index_paths, pdf_paths):
    """
    Private Vaults dürfen in KEINEM Index stehen — egal wie die Datei heißt.
    Index-Pfade tragen den Vault-Namen als erstes Segment (/Vault/rel).
    Liefert (Treffer, Namen der privaten Vaults).
    """
    try:
        from core import vaults_registry

        priv = [os.path.realpath(p) for p in vaults_registry.private_paths()]
    except Exception:
        priv = []
    namen = {os.path.basename(r) for r in priv}
    out = []
    for label, paths in (("vault_index", index_paths), ("pdf_index", pdf_paths)):
        for p in paths:
            name = p.strip("/").split("/", 1)[0]
            if name in namen:
                out.append({"index": label, "path": p})
    return out, sorted(namen)


def run_check(write_report=True) -> dict:
    """Führt alle vier Prüfungen aus und schreibt den Bericht. Liefert das Dict."""
    wiki_hits, _wiki_all = scan_wiki_sources()
    index_hits, index_paths = scan_index()
    pdf_hits, pdf_paths = scan_pdf_index()
    leak_hits, priv_namen = scan_private_leak(index_paths, pdf_paths)
    befunde = len(wiki_hits) + len(index_hits) + len(pdf_hits) + len(leak_hits)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "wiki_root": WIKI,
        "wiki_heikle_sources": len(wiki_hits),
        "index_unique_paths": len(index_paths),
        "index_heikle_paths": len(index_hits),
        "pdf_unique_paths": len(pdf_paths),
        "pdf_heikle_paths": len(pdf_hits),
        "private_vaults": priv_namen,
        "private_leaks": len(leak_hits),
        "blocked_dirs": list(getattr(config, "BLOCKED_DIRS", []) or []),
        "wiki_samples": wiki_hits[:40],
        "index_samples": index_hits[:40],
        "pdf_samples": pdf_hits[:40],
        "private_leak_samples": leak_hits[:40],
        "befunde": befunde,
        "status": "ok" if befunde == 0 else "befund",
        "recommendation": (
            "Ohne Befund: nichts tun. Bei Befund: die betroffene Datei aus dem "
            "Quellordner entfernen (nicht nur aus dem Index löschen — der nächste "
            "Aufbau zieht sie sonst wieder ein), danach "
            "python3 scripts/vault_index_refresh.py --force"
        ),
    }
    if write_report:
        os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
        tmp = REPORT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, REPORT_PATH)
    return report


def main() -> int:
    report = run_check(write_report=True)
    print("=== Index-Hygiene ===")
    print(f"Status: {report['status'].upper()}")
    print(f"Wiki heikle Sources:  {report['wiki_heikle_sources']}")
    print(f"Vault-Index heikel:   {report['index_heikle_paths']} (von {report['index_unique_paths']} Pfaden)")
    print(f"PDF-Index heikel:     {report['pdf_heikle_paths']} (von {report['pdf_unique_paths']} Pfaden)")
    print(
        f"Privat-Leaks:         {report['private_leaks']} "
        f"(private Vaults: {', '.join(report['private_vaults']) or '—'})"
    )
    print(f"Bericht: {REPORT_PATH}")
    for label, key in (
        ("Wiki", "wiki_samples"),
        ("Vault-Index", "index_samples"),
        ("PDF-Index", "pdf_samples"),
        ("Privat", "private_leak_samples"),
    ):
        if report[key][:8]:
            print(f"Beispiele {label}:")
            for h in report[key][:8]:
                extra = h.get("patterns") or [h.get("index")]
                print(f"  - {h.get('path')}  ({', '.join(x for x in extra if x)})")
    return 1 if report["befunde"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
