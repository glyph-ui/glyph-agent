#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Coverage-Audit: Welche Vault-Dokumente fehlen im Index oder sind lückenhaft?

Rein lesend (Dateisystem + pdf_index.sqlite + md-Index), schreibt nichts.
Stufen pro PDF:
  ok        indexiert, aktuell, keine offenen Scan-Seiten
  missing   gar nicht im Index
  stale     Datei hat sich seit dem Indexlauf geändert
  rescan    hat Scan-Seiten, die ein alter Lauf nicht indexiert hat (--reocr nötig)
  error     Indexlauf scheiterte (status='error')
Für .md: missing/stale gegen den md-Index (retrieval.INDEX_PATH).

Aufruf:
  python3 scripts/pdf_coverage.py                 # Übersicht
  python3 scripts/pdf_coverage.py --detail        # + Pfade je Klasse
  python3 scripts/pdf_coverage.py --write-filelist /tmp/reocr.txt
                                                  # Pfade für build_pdf_index --filelist
  python3 scripts/pdf_coverage.py --json out.json # vollständiges Ergebnis
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import config, pdf_index, retrieval, vault_tools  # noqa: E402
import build_pdf_index as bpi  # noqa: E402  (gleicher Ordner: iter_pdfs, has_scan_pages)


def md_files(roots, only_root=None):
    for root in roots:
        root_r = os.path.realpath(root)
        if only_root and root_r != os.path.realpath(only_root):
            continue
        for dirpath, dirnames, filenames in os.walk(root_r):
            rel = os.path.relpath(dirpath, root_r)
            dirnames[:] = [d for d in dirnames if not bpi._skip_dir(os.path.join(rel, d))]
            if bpi._skip_dir(rel):
                continue
            for fn in sorted(filenames):
                if fn.lower().endswith(".md") and not fn.startswith("."):
                    yield os.path.join(dirpath, fn)


def md_indexed_paths():
    try:
        index = retrieval.load_index()
    except Exception:
        return {}
    out = {}
    for d in (index or {}).get("docs", []):
        p = str(d.get("path") or "")
        if p:
            out.setdefault(p, 0)
            out[p] += 1
    return out


def audit(roots, only_root=None, with_scan_check=True):
    conn = pdf_index.connect()
    try:
        res = {"pdf": {"ok": [], "missing": [], "stale": [], "rescan": [], "error": [],
                       "empty": []},
               "md": {"ok": 0, "missing": [], "stale": []}}
        for abs_path in bpi.iter_pdfs(roots, only_root=only_root):
            canon = vault_tools.canon_vault_path(abs_path)
            try:
                st = os.stat(abs_path)
            except OSError:
                continue
            state = pdf_index.file_state(conn, canon)
            if state is None:
                bucket = "missing"
            elif state.get("status") == "error":
                bucket = "error"
            elif (state.get("mtime") is not None
                  and (abs((state.get("mtime") or 0) - st.st_mtime) >= 1
                       or state.get("size") != st.st_size)):
                bucket = "stale"
            elif (with_scan_check
                  and not (state.get("ocr_pages") or 0)
                  and bpi.has_scan_pages(abs_path)):
                bucket = "rescan"
            elif state.get("status") == "empty":
                bucket = "empty"
            else:
                bucket = "ok"
            res["pdf"][bucket].append(canon)

        indexed = md_indexed_paths()
        for abs_path in md_files(roots, only_root=only_root):
            canon = vault_tools.canon_vault_path(abs_path)
            if canon not in indexed:
                res["md"]["missing"].append(canon)
            else:
                res["md"]["ok"] += 1
        return res
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="", help="nur dieser Vault-Pfad")
    ap.add_argument("--detail", action="store_true", help="Pfade je Klasse ausgeben")
    ap.add_argument("--write-filelist", default="",
                    help="Pfade der Klasse rescan+missing+stale als Liste schreiben")
    ap.add_argument("--json", default="", help="Ergebnis als JSON schreiben")
    args = ap.parse_args()

    try:
        config.reload_vault_paths()
    except Exception:
        pass
    roots = [args.root] if args.root else bpi.agent_rw_vault_roots()
    if not roots:
        print("Keine Vaults konfiguriert (Agent-rw).", file=sys.stderr)
        return 2

    res = audit(roots, only_root=args.root or None)

    p = res["pdf"]
    total = sum(len(v) for v in p.values())
    print(f"PDFs geprüft: {total}")
    for key in ("ok", "rescan", "stale", "missing", "error", "empty"):
        print(f"  {key:8s} {len(p[key])}")
    print(f"MD-Dateien: {res['md']['ok']} indexiert, {len(res['md']['missing'])} fehlend")

    if args.detail:
        for key in ("rescan", "stale", "missing", "error"):
            if p[key]:
                print(f"\n[{key}]")
                for rel in p[key]:
                    print(f"  {rel}")
        if res["md"]["missing"]:
            print("\n[md missing]")
            for rel in res["md"]["missing"]:
                print(f"  {rel}")

    if args.write_filelist:
        todo = p["rescan"] + p["stale"] + p["missing"]
        with open(args.write_filelist, "w", encoding="utf-8") as fh:
            fh.write("\n".join(todo) + ("\n" if todo else ""))
        print(f"\nFilelist geschrieben ({len(todo)} Pfade): {args.write_filelist}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(res, fh, ensure_ascii=False, indent=2)
        print(f"JSON geschrieben: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
