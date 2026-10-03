#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Baut/aktualisiert den PDF-Vektorindex (core/pdf_index.py) über alle Vault-PDFs.

- Extraktion: pdf_tools.read_pdf (pdftotext, bei Scans automatisch Vision-OCR)
- Chunking: retrieval.split_document (1200 Zeichen, absatzbewusst)
- Embeddings: bge-m3 via Ollama /api/embed (Batch)
- Inkrementell: unveränderte Dateien (mtime+size) werden übersprungen
- Wiederaufsetzbar: Abbruch jederzeit, Neustart macht dort weiter

Aufruf:
  python3 scripts/build_pdf_index.py                 # alle Vaults, inkrementell
  python3 scripts/build_pdf_index.py --limit 50      # nur 50 Dateien (Test)
  python3 scripts/build_pdf_index.py --force         # alles neu
  python3 scripts/build_pdf_index.py --dry-run       # nur zählen
  python3 scripts/build_pdf_index.py --workers 6     # parallele Extraktion
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import config, pdf_index, pdf_tools, retrieval, vault_tools  # noqa: E402

CHUNK_CHARS = int(os.environ.get("PDF_CHUNK_CHARS", "1200"))


def _skip_dir(rel):
    parts = [p for p in rel.replace("\\", "/").split("/") if p and p != "."]
    if not parts:
        return False
    if any(p.startswith(".") for p in parts):
        return True
    if "backups" in parts:
        return True
    try:
        if vault_tools._is_blocked("/".join(parts)):
            return True
    except Exception:
        pass
    return False


def agent_rw_vault_roots():
    """Vault-Wurzeln, die der °_Agent lesen darf — Private (inkl. Home) ausgeschlossen."""
    from core import vaults_registry as vr

    priv = {os.path.realpath(p) for p in vr.private_paths()}
    out = []
    for v in vr.list_vaults():
        if not v.get("enabled", True):
            continue
        try:
            if vr._agent_mode(v, home_head="agent") != "rw":
                continue
        except Exception:
            continue
        p = v.get("path") or ""
        if not p or not os.path.isdir(p):
            continue
        if os.path.realpath(p) in priv:
            continue
        out.append(p)
    return out


def iter_pdfs(roots, only_root=None):
    for root in roots:
        root_r = os.path.realpath(root)
        if only_root and root_r != os.path.realpath(only_root):
            continue
        for dirpath, dirnames, filenames in os.walk(root_r):
            rel = os.path.relpath(dirpath, root_r)
            dirnames[:] = [d for d in dirnames if not _skip_dir(os.path.join(rel, d))]
            if _skip_dir(rel):
                continue
            for fn in sorted(filenames):
                if fn.lower().endswith(".pdf") and not fn.startswith("."):
                    yield os.path.join(dirpath, fn)


def extract_one(abs_path):
    """Läuft im Thread: Pfad → (canon, title, chunks, engine, chars, error)."""
    canon = vault_tools.canon_vault_path(abs_path)
    title = os.path.basename(abs_path)
    try:
        res = pdf_tools.read_pdf(canon)
    except Exception as e:  # noqa: BLE001
        return canon, title, [], None, 0, f"{type(e).__name__}: {e}"[:200]
    if not res.get("ok"):
        return canon, title, [], res.get("engine"), 0, (res.get("error") or "unbekannt")[:200]
    text = res.get("content") or ""
    chunks = retrieval.split_document(text, title, canon, max_chars=CHUNK_CHARS)
    return canon, title, chunks, res.get("engine"), len(text), None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="nur N Dateien verarbeiten")
    ap.add_argument("--force", action="store_true", help="auch unveränderte neu indexieren")
    ap.add_argument("--dry-run", action="store_true", help="nur zählen, nichts schreiben")
    ap.add_argument("--workers", type=int, default=4, help="parallele Extraktion (OCR)")
    ap.add_argument("--root", default="", help="nur dieser Vault-Pfad")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    try:
        config.reload_vault_paths()
    except Exception:
        pass
    if args.root:
        roots = [args.root]
    else:
        roots = agent_rw_vault_roots()
    if not roots:
        print("Keine Vaults konfiguriert (Agent-rw).", file=sys.stderr)
        return 2
    print("Vaults: " + " | ".join(os.path.basename(r) for r in roots), flush=True)

    files = list(iter_pdfs(roots, only_root=args.root or None))
    if args.limit:
        files = files[: args.limit]
    total = len(files)

    conn = None if args.dry_run else pdf_index.connect()
    if conn is not None:
        # Einmalige Migration: Dateien aus Läufen vor dup_of/empty haben status='ok'
        # ohne Chunks → erneut prüfen, damit dup_of gesetzt und die Vorschau geht.
        cur = conn.execute(
            "UPDATE files SET status='stale' WHERE status='ok' AND COALESCE(chunks,0)=0"
        )
        if cur.rowcount:
            print(f"Migration: {cur.rowcount} Dateien ohne Chunks werden neu geprüft", flush=True)
        conn.commit()
    todo = []
    for abs_path in files:
        canon = vault_tools.canon_vault_path(abs_path)
        try:
            st = os.stat(abs_path)
        except OSError:
            continue
        if not args.force and conn is not None:
            state = pdf_index.file_state(conn, canon)
            if (state and state.get("status") in ("ok", "dup", "empty")
                    and abs((state.get("mtime") or 0) - st.st_mtime) < 1
                    and state.get("size") == st.st_size):
                continue
        todo.append((abs_path, canon, st.st_mtime, st.st_size))

    print(f"PDFs gefunden: {total} | zu indexieren: {len(todo)}"
          f"{' (dry-run)' if args.dry_run else ''}", flush=True)
    if args.dry_run:
        return 0

    t0 = time.time()
    done = chunks_n = errors = skipped_chunks = 0
    chars_n = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for (canon, title, chunks, engine, chars, err), (abs_path, _c, mtime, size) in zip(
            pool.map(extract_one, [t[0] for t in todo]), todo
        ):
            done += 1
            if err:
                errors += 1
                if not args.quiet:
                    print(f"  ! {os.path.basename(abs_path)[:60]}: {err}", flush=True)
                # "kein Text vorhanden" (Skizze/Bild ohne Textlayer) ist kein
                # Fehler, den ein erneuter Lauf beheben könnte → status 'empty',
                # damit der Nachtlauf sie nicht jede Nacht neu per OCR versucht.
                no_text = ("keinen Textlayer" in err) or ("OCR lieferte keinen Text" in err)
                if conn is not None:
                    conn.execute(
                        "INSERT INTO files(path, mtime, size, sha, status, chars, chunks, engine, ts) "
                        "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
                        "mtime=excluded.mtime, size=excluded.size, status=excluded.status, "
                        "chars=excluded.chars, engine=excluded.engine, ts=excluded.ts",
                        (canon, mtime, size, None, "empty" if no_text else "error", 0, 0, engine,
                         time.strftime("%Y-%m-%dT%H:%M:%S%z")),
                    )
                    conn.commit()
                if no_text:
                    errors -= 1
            elif chunks:
                vecs = pdf_index.embed_texts([c["text"] for c in chunks])
                if len(vecs) != len(chunks):
                    errors += 1
                    continue
                res = pdf_index.add_file(
                    conn, canon, title, chunks, vecs,
                    {"mtime": mtime, "size": size, "sha": None, "status": "ok",
                     "chars": chars, "engine": engine},
                )
                chunks_n += res["chunks"]
                skipped_chunks += res["skipped"]
                chars_n += chars or 0
            else:
                if conn is not None:
                    pdf_index.add_file(conn, canon, title, [], [],
                                       {"mtime": mtime, "size": size, "sha": None, "status": "empty",
                                        "chars": chars or 0, "engine": engine})

            if done % 25 == 0 or done == len(todo):
                dt = time.time() - t0
                rate = done / dt if dt else 0
                eta = (len(todo) - done) / rate if rate else 0
                print(f"  [{done}/{len(todo)}] {rate*60:.0f} Dateien/min | "
                      f"{chunks_n} Chunks | {chars_n/1e6:.1f} Mio Zeichen | "
                      f"Fehler {errors} | ETA {eta/60:.1f} min", flush=True)

    print(f"\nFertig in {(time.time()-t0)/60:.1f} min: "
          f"{done} Dateien, {chunks_n} neue Chunks ({skipped_chunks} Duplikate übersprungen), "
          f"{chars_n/1e6:.1f} Mio Zeichen, {errors} Fehler", flush=True)
    print(json_stats(), flush=True)
    return 0


def json_stats():
    import json

    return json.dumps(pdf_index.stats(), indent=1, ensure_ascii=False)


if __name__ == "__main__":
    raise SystemExit(main())
