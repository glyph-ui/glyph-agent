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

from core import (  # noqa: E402
    config,
    dotenv,
    dsgvo,
    image_ocr,
    office_text,
    pdf_index,
    pdf_tools,
    retrieval,
    vault_tools,
)


def cap_workers(n, backend):
    """LightOnOCR ist MLX-seriell — parallele HTTP-Clients laufen in den Timeout."""
    n = max(1, int(n or 1))
    if backend == "lighton" and n > 1 and not os.environ.get("PDF_INDEX_WORKERS_FORCE"):
        return 1
    return n

CHUNK_CHARS = int(os.environ.get("PDF_CHUNK_CHARS", "1200"))
# Index bekommt den VOLLTEXT (keine Tool-Kappe von 40k) — sonst fehlen bei
# dicken Regelwerken die hinteren Seiten im Index. Der Chunker teilt ohnehin.
INDEX_MAX_CHARS = int(os.environ.get("PDF_INDEX_MAX_CHARS", "2000000"))

IMAGE_EXTS = image_ocr.IMAGE_EXTS
OFFICE_EXTS = (".docx", ".xlsx", ".pptx", ".pptm", ".doc")


def _iter_files(roots, exts, only_root=None):
    """Vault-Dateien mit den gegebenen Endungen (Blocklisten wie bei PDFs)."""
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
                if fn.lower().endswith(exts) and not fn.startswith("."):
                    yield os.path.join(dirpath, fn)


def iter_pdfs(roots, only_root=None):
    yield from _iter_files(roots, (".pdf",), only_root)


def _skip_dir(rel):
    parts = [p for p in rel.replace("\\", "/").split("/") if p and p != "."]
    if not parts:
        return False
    if any(p.startswith(".") for p in parts):
        return True
    if "backups" in parts:
        return True
    if parts[0] == "Beruf-intern":
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


def _clean_chunks(chunks):
    """Leere Chunks raus — sonst zählt embed_texts weniger Vektoren als Chunks
    vorhanden sind (stiller Fehler: Datei bleibt unbehandelt liegen)."""
    return [c for c in (chunks or []) if (c.get("text") or "").strip()]


def _redact_chunks(chunks):
    """Index-Redaktion: Firmentoken (BERATERgroup → Beratung) aus Chunk-Texten,
    damit der Name nicht in Suche/Treffer landet — Dateiinhalte bleiben unangetastet
    (für PDFs, die nicht verlustfrei redigierbar sind)."""
    for c in chunks or []:
        if isinstance(c, dict) and c.get("text"):
            c["text"], _ = dsgvo.redact_tokens(c["text"])
    return chunks


def extract_one(abs_path):
    """Läuft im Thread: Pfad → (canon, title, chunks, engine, chars, err, pages, ocr_pages)."""
    canon = vault_tools.canon_vault_path(abs_path)
    title = os.path.basename(abs_path)
    low = abs_path.lower()
    try:
        if low.endswith(OFFICE_EXTS):
            text, oerr = office_text.read_office(abs_path)
            if oerr:
                return canon, title, [], "office", 0, oerr[:200], 0, 0
            if not text.strip():
                return canon, title, [], "office", 0, "Kein Text im Dokument", 0, 0
            chunks = _redact_chunks(_clean_chunks(retrieval.split_document(text, title, canon, max_chars=CHUNK_CHARS)))
            if low.endswith(".doc") and not low.endswith(".docx"):
                engine = "doc-textutil"
            elif low.endswith(".docx"):
                engine = "docx-xml"
            elif low.endswith(".pptx") or low.endswith(".pptm"):
                engine = "pptx-xml"
            else:
                engine = "xlsx-xml"
            return canon, title, chunks, engine, len(text), None, 0, 0
        if low.endswith(IMAGE_EXTS):
            res = image_ocr.read_image(canon)
            ocr_n = 1 if res.get("ok") else 0
            if not res.get("ok"):
                return (canon, title, [], res.get("engine"), 0,
                        (res.get("error") or "unbekannt")[:200], 0, ocr_n)
            text = res.get("content") or ""
            chunks = _redact_chunks(_clean_chunks(retrieval.split_document(text, title, canon, max_chars=CHUNK_CHARS)))
            return (canon, title, chunks, res.get("engine"),
                    res.get("chars") or len(text), None, 0, ocr_n)
        res = pdf_tools.read_pdf(canon, max_chars=INDEX_MAX_CHARS)
    except Exception as e:  # noqa: BLE001
        return canon, title, [], None, 0, f"{type(e).__name__}: {e}"[:200], 0, 0
    pages_n = res.get("pages") or 0
    ocr_n = res.get("ocr_pages") or 0
    if not res.get("ok"):
        return (canon, title, [], res.get("engine"), 0,
                (res.get("error") or "unbekannt")[:200], pages_n, ocr_n)
    text = res.get("content") or ""
    chunks = _redact_chunks(_clean_chunks(retrieval.split_document(text, title, canon, max_chars=CHUNK_CHARS)))
    return (canon, title, chunks, res.get("engine"), res.get("chars") or len(text),
            None, pages_n, ocr_n)


def has_scan_pages(abs_path):
    """Günstiger Probing-Check: enthält die Datei Seiten ohne Textlayer?"""
    pages, err = pdf_tools.pdf_pages_text(abs_path)
    if err:
        return False
    return any(not pdf_tools._has_text(p["text"]) for p in pages)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="nur N Dateien verarbeiten")
    ap.add_argument("--force", action="store_true", help="auch unveränderte neu indexieren")
    ap.add_argument("--dry-run", action="store_true", help="nur zählen, nichts schreiben")
    ap.add_argument("--workers", type=int, default=4, help="parallele Extraktion (OCR)")
    ap.add_argument("--root", default="", help="nur dieser Vault-Pfad")
    ap.add_argument("--reocr", action="store_true",
                    help="Dateien mit Scan-Seiten neu extrahieren (alte Läufe "
                         "ließen gemischte PDFs halb weg)")
    ap.add_argument("--filelist", default="",
                    help="nur diese Pfade (Datei mit einem Pfad pro Zeile, "
                         "absolut oder Vault-Pfad)")
    ap.add_argument("--no-images", action="store_true",
                    help="Bilder (JPG/PNG/TIFF/HEIC) nicht indexieren")
    ap.add_argument("--no-office", action="store_true",
                    help="docx/xlsx nicht indexieren")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    dotenv.load_dotenv()
    try:
        config.reload_vault_paths()
    except Exception:
        pass
    be = pdf_tools.ocr_backend()
    capped = cap_workers(args.workers, be)
    if capped != args.workers:
        print(f"OCR-Backend {be}: workers {args.workers} → {capped} "
              f"(MLX serialisiert; PDF_INDEX_WORKERS_FORCE=1 zum Override)", flush=True)
        args.workers = capped
    if args.root:
        roots = [args.root]
    else:
        roots = agent_rw_vault_roots()
    if not roots:
        print("Keine Vaults konfiguriert (Agent-rw).", file=sys.stderr)
        return 2
    print("Vaults: " + " | ".join(os.path.basename(r) for r in roots), flush=True)

    exts = [".pdf"]
    if not args.no_images:
        exts += list(IMAGE_EXTS)
    if not args.no_office:
        exts += list(OFFICE_EXTS)
    files = list(_iter_files(roots, tuple(exts), only_root=args.root or None))
    if args.filelist:
        want_abs, want_canon = set(), set()
        with open(args.filelist, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                if os.path.isfile(line):
                    want_abs.add(os.path.realpath(line))
                else:
                    want_canon.add(line.lstrip("/"))
        files = [f for f in files
                 if os.path.realpath(f) in want_abs
                 or vault_tools.canon_vault_path(f).lstrip("/") in want_canon]
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
            unchanged = (state and state.get("status") in ("ok", "dup", "empty")
                         and abs((state.get("mtime") or 0) - st.st_mtime) < 1
                         and state.get("size") == st.st_size)
            if unchanged:
                if not args.reocr:
                    continue
                # Alte Läufe indexierten gemischte PDFs ohne ihre Scan-Seiten —
                # genau die bekommen hier einen zweiten Durchlauf. Dateien, die
                # schon mit dem Ziel-Backend (lighton) gearbeitet wurden, bleiben
                # aus — macht den Lauf unterbrechbar/fortsetzbar.
                if "lighton" in str(state.get("engine") or ""):
                    continue
                if not has_scan_pages(abs_path):
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
        for (canon, title, chunks, engine, chars, err, pages_n, ocr_n), (abs_path, _c, mtime, size) in zip(
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
                no_text = (("keinen Textlayer" in err) or ("OCR lieferte keinen Text" in err)
                        or ("Kein Text im Dokument" in err))
                if conn is not None:
                    conn.execute(
                        "INSERT INTO files(path, mtime, size, sha, status, chars, chunks, engine, "
                        "pages, ocr_pages, ts) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
                        "mtime=excluded.mtime, size=excluded.size, status=excluded.status, "
                        "chars=excluded.chars, engine=excluded.engine, pages=excluded.pages, "
                        "ocr_pages=excluded.ocr_pages, ts=excluded.ts",
                        (canon, mtime, size, None, "empty" if no_text else "error", 0, 0, engine,
                         pages_n, ocr_n, time.strftime("%Y-%m-%dT%H:%M:%S%z")),
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
                     "chars": chars, "engine": engine,
                     "pages": pages_n, "ocr_pages": ocr_n},
                )
                chunks_n += res["chunks"]
                skipped_chunks += res["skipped"]
                chars_n += chars or 0
            else:
                if conn is not None:
                    pdf_index.add_file(conn, canon, title, [], [],
                                       {"mtime": mtime, "size": size, "sha": None, "status": "empty",
                                        "chars": chars or 0, "engine": engine,
                                        "pages": pages_n, "ocr_pages": ocr_n})

            if err or ocr_n or done % 25 == 0 or done == len(todo):
                dt = time.time() - t0
                rate = done / dt if dt else 0
                eta = (len(todo) - done) / rate if rate else 0
                tag = f" | {os.path.basename(abs_path)[:48]}" if (err or ocr_n) else ""
                ocr_s = f" ocr={ocr_n}" if ocr_n else ""
                print(f"  [{done}/{len(todo)}] {rate*60:.0f} Dateien/min | "
                      f"{chunks_n} Chunks | {chars_n/1e6:.1f} Mio Zeichen | "
                      f"Fehler {errors} | ETA {eta/60:.1f} min{ocr_s}{tag}", flush=True)

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
