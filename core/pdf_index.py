# -*- coding: utf-8 -*-
"""
PDF-Vektorindex: SQLite-Metadaten + memmap-Vektordatei (float32, L2-normiert).

Warum ein eigener Store:
Der .md-Index (`logs/vault_index.json`) hält Embeddings als JSON-Text (~22 KB pro
Eintrag) und wird in reinem Python linear durchsucht — bei 1.080 Einträgen 1,1 s.
Für ~120.000 PDF-Chunks wären das ~2,7 GB und Minuten pro Suche. Hier: 4 KB/Chunk
als float32 in einer memmap-Datei, Suche per numpy-Matmul (BLAS).

Abgrenzung: der .md-Index bleibt unverändert; `retrieval.search()` mischt beide.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.request
from datetime import datetime

DIM = int(os.environ.get("PDF_INDEX_DIM", "1024"))
EMBED_MODEL = os.environ.get("EMBED_MODEL", "bge-m3")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBED_BATCH = int(os.environ.get("PDF_EMBED_BATCH", "64"))
SCHEMA_VERSION = "1"

_CACHE = {"gen": None, "V": None, "live": None, "rowids": None, "n": 0}


# --- Pfade ----------------------------------------------------------------

def _index_dir():
    """Neben dem .md-Index — respektiert retrieval.INDEX_PATH (Tests setzen das um)."""
    try:
        from . import retrieval

        base = os.path.dirname(os.path.abspath(retrieval.INDEX_PATH))
    except Exception:
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    os.makedirs(base, exist_ok=True)
    return base


def paths():
    """(sqlite_pfad, vektordatei)"""
    base = _index_dir()
    return os.path.join(base, "pdf_index.sqlite"), os.path.join(base, "pdf_vectors.bin")


# --- Verbindung / Schema ---------------------------------------------------

def connect():
    db, _bin = paths()
    conn = sqlite3.connect(db, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _init(conn)
    return conn


def _init(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS docs (
            rowid_   INTEGER PRIMARY KEY AUTOINCREMENT,
            path     TEXT NOT NULL,
            title    TEXT,
            section  TEXT,
            text     TEXT NOT NULL,
            text_sha TEXT,
            vec_row  INTEGER NOT NULL,
            ts       TEXT
        );
        CREATE INDEX IF NOT EXISTS docs_path ON docs(path);
        CREATE UNIQUE INDEX IF NOT EXISTS docs_text_sha ON docs(text_sha);
        CREATE TABLE IF NOT EXISTS files (
            path     TEXT PRIMARY KEY,
            mtime    REAL,
            size     INTEGER,
            sha      TEXT,
            status   TEXT,
            chars    INTEGER,
            chunks   INTEGER,
            engine   TEXT,
            dup_of   TEXT,
            ts       TEXT
        );
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
            text, content='docs', content_rowid='rowid_', tokenize='unicode61 remove_diacritics 2'
        );
        CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
            INSERT INTO docs_fts(rowid, text) VALUES (new.rowid_, new.text);
        END;
        CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
            INSERT INTO docs_fts(docs_fts, rowid, text) VALUES ('delete', old.rowid_, old.text);
        END;
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta(k, v) VALUES('schema', ?)", (SCHEMA_VERSION,)
    )
    conn.execute("INSERT OR IGNORE INTO meta(k, v) VALUES('generation', '0')")
    conn.execute("INSERT OR IGNORE INTO meta(k, v) VALUES('next_vec_row', '0')")
    # Migration: dup_of nachrüsten (Bestands-DBs)
    try:
        if "dup_of" not in {r[1] for r in conn.execute("PRAGMA table_info(files)")}:
            conn.execute("ALTER TABLE files ADD COLUMN dup_of TEXT")
    except sqlite3.OperationalError:
        pass
    # FTS nachziehen, falls die Tabelle neu angelegt wurde (nur dann teuer)
    try:
        if (conn.execute("SELECT 1 FROM docs LIMIT 1").fetchone()
                and not conn.execute("SELECT 1 FROM docs_fts LIMIT 1").fetchone()):
            conn.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")
    except sqlite3.OperationalError:
        pass
    conn.commit()


def _meta_get(conn, key, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row[0] if row else default


def _meta_set(conn, key, value):
    conn.execute("INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                 (key, str(value)))


def _bump(conn):
    gen = int(_meta_get(conn, "generation", "0") or 0) + 1
    _meta_set(conn, "generation", gen)
    _CACHE["gen"] = None  # erzwingt Neuladen in search()


# --- Embeddings ------------------------------------------------------------

def embed_texts(texts):
    """Batch-Embeddings über Ollama /api/embed. Liefert Liste Vektoren (oder [])."""
    texts = [t for t in (texts or []) if t and t.strip()]
    if not texts:
        return []
    out = []
    for i in range(0, len(texts), EMBED_BATCH):
        chunk = texts[i:i + EMBED_BATCH]
        body = json.dumps({"model": EMBED_MODEL, "input": chunk}).encode()
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/embed", data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=600) as resp:
            data = json.loads(resp.read().decode())
        embs = data.get("embeddings") or []
        if len(embs) != len(chunk):
            raise RuntimeError(f"Embedding-Anzahl {len(embs)} != {len(chunk)}")
        out.extend(embs)
    return out


# --- Schreiben -------------------------------------------------------------

def _text_sha(text):
    return hashlib.sha1(" ".join((text or "").split()).encode("utf-8", "replace")).hexdigest()


def _append_vectors(vectors):
    """Hängt normierte float32-Vektoren an die memmap-Datei. Liefert Startzeile."""
    import numpy as np

    _db, binp = paths()
    start = None
    with open(binp, "ab") as fh:
        start = fh.tell() // (DIM * 4)
        for v in vectors:
            arr = np.asarray(v, dtype=np.float32).reshape(-1)
            if arr.size != DIM:
                raise ValueError(f"Vektorlänge {arr.size} != {DIM}")
            n = float(np.linalg.norm(arr))
            if n > 0:
                arr = arr / n
            fh.write(arr.tobytes())
    return start


def add_file(conn, path, title, chunks, vectors, file_meta=None):
    """
    Ersetzt alle Chunks eines Pfads. chunks: [{section,text,ts}], vectors: passend.
    Dedupliziert global über text_sha. Liefert {chunks, skipped, vec_row}.
    """
    path = str(path)
    conn.execute("DELETE FROM docs WHERE path=?", (path,))

    seen = set()
    keep_chunks, keep_vecs = [], []
    for c, v in zip(chunks, vectors):
        text = (c.get("text") or "").strip()
        if not text:
            continue
        sha = _text_sha(text)
        if sha in seen:
            continue
        row = conn.execute("SELECT 1 FROM docs WHERE text_sha=?", (sha,)).fetchone()
        if row:
            continue
        seen.add(sha)
        keep_chunks.append((c, sha))
        keep_vecs.append(v)

    start = int(_meta_get(conn, "next_vec_row", "0") or 0)
    if keep_vecs:
        real_start = _append_vectors(keep_vecs)
        start = real_start
        _meta_set(conn, "next_vec_row", start + len(keep_vecs))

    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    for i, (c, sha) in enumerate(keep_chunks):
        conn.execute(
            "INSERT INTO docs(path, title, section, text, text_sha, vec_row, ts) VALUES(?,?,?,?,?,?,?)",
            (path, title or c.get("title") or os.path.basename(path), c.get("section") or "",
             (c.get("text") or "").strip(), sha, start + i, c.get("ts") or now),
        )
    # Datei ohne eigene Chunks, aber mit Inhalt = reines Duplikat: auf die
    # kanonische Datei verweisen, damit Vorschau/Suche sie nicht als leer zeigen.
    dup_of = None
    if file_meta is not None and chunks and not keep_chunks:
        first = (chunks[0].get("text") or "").strip()
        if first:
            row = conn.execute(
                "SELECT path FROM docs WHERE text_sha=? LIMIT 1", (_text_sha(first),)
            ).fetchone()
            if row and row[0] != path:
                dup_of = row[0]
                file_meta = dict(file_meta)
                file_meta["status"] = "dup"
    if file_meta is not None:
        conn.execute(
            "INSERT INTO files(path, mtime, size, sha, status, chars, chunks, engine, dup_of, ts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
            "mtime=excluded.mtime, size=excluded.size, sha=excluded.sha, status=excluded.status, "
            "chars=excluded.chars, chunks=excluded.chunks, engine=excluded.engine, "
            "dup_of=excluded.dup_of, ts=excluded.ts",
            (path, file_meta.get("mtime"), file_meta.get("size"), file_meta.get("sha"),
             file_meta.get("status"), file_meta.get("chars"), len(keep_chunks),
             file_meta.get("engine"), dup_of, now),
        )
    _bump(conn)
    conn.commit()
    return {"chunks": len(keep_chunks), "skipped": len(chunks) - len(keep_chunks),
            "vec_row": start, "dup_of": dup_of}


def file_state(conn, path):
    row = conn.execute(
        "SELECT mtime, size, sha, status, chars, chunks, engine, dup_of FROM files WHERE path=?",
        (str(path),),
    ).fetchone()
    if not row:
        return None
    keys = ("mtime", "size", "sha", "status", "chars", "chunks", "engine", "dup_of")
    return dict(zip(keys, row))


# --- Suche -----------------------------------------------------------------

def _load_cache(conn):
    import numpy as np

    gen = _meta_get(conn, "generation", "0")
    if _CACHE["gen"] == gen and _CACHE["V"] is not None:
        return _CACHE
    _db, binp = paths()
    n = 0
    V = None
    if os.path.isfile(binp):
        size = os.path.getsize(binp)
        n = size // (DIM * 4)
        if n:
            V = np.memmap(binp, dtype=np.float32, mode="r", shape=(n, DIM))
    live = np.zeros(n, dtype=bool)
    rowids = np.full(n, -1, dtype=np.int64)
    rows = conn.execute("SELECT rowid_, vec_row FROM docs").fetchall()
    if rows:
        arr = np.asarray(rows, dtype=np.int64)
        ok = (arr[:, 1] >= 0) & (arr[:, 1] < n)
        arr = arr[ok]
        if len(arr):
            live[arr[:, 1]] = True
            rowids[arr[:, 1]] = arr[:, 0]
    _CACHE.update({"gen": gen, "V": V, "live": live, "rowids": rowids, "n": n})
    return _CACHE


def search(q_emb, candidate_k=50, roots=None):
    """Nächste Nachbarn über alle PDF-Chunks. Liefert Liste {score,path,title,section,text,ts}."""
    import numpy as np

    conn = connect()
    try:
        cache = _load_cache(conn)
        V, live, rowids = cache["V"], cache["live"], cache["rowids"]
        if V is None or not live.any():
            return []
        q = np.asarray(q_emb, dtype=np.float32).reshape(-1)
        if q.size != DIM:
            return []
        nq = float(np.linalg.norm(q))
        if nq <= 0:
            return []
        q = q / nq
        scores = np.asarray(V @ q, dtype=np.float32)
        scores[~live] = -np.inf
        k = int(min(max(candidate_k, 1) * (3 if roots else 1), int(live.sum())))
        if k <= 0:
            return []
        idx = np.argpartition(-scores, k - 1)[:k]
        idx = idx[np.argsort(-scores[idx])]
        sel = [(float(scores[i]), int(rowids[i])) for i in idx]
        ids = [rid for _s, rid in sel if rid >= 0]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        meta = {
            r[0]: r
            for r in conn.execute(
                f"SELECT rowid_, path, title, section, text, ts FROM docs WHERE rowid_ IN ({marks})",
                ids,
            )
        }
        out = []
        for s, rid in sel:
            m = meta.get(rid)
            if not m:
                continue
            out.append({"score": round(s, 4), "path": m[1], "title": m[2],
                        "section": m[3], "text": m[4], "ts": m[5]})
        if roots:
            try:
                from . import vault_scope as _vs

                out = [r for r in out if _vs.path_in_roots(r.get("path"), roots)]
            except Exception:
                pass
        return out
    finally:
        conn.close()


def excerpt_for(path, max_chars=240, _depth=0):
    """Erster Chunk-Text eines PDFs (für die Apfel-Trefferliste) — ohne OCR.

    Reine Duplikate haben keine eigenen Chunks: dann die kanonische Datei fragen.
    """
    try:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT text FROM docs WHERE path=? ORDER BY rowid_ LIMIT 1", (str(path),)
            ).fetchone()
            if not row and _depth == 0:
                dup = conn.execute("SELECT dup_of FROM files WHERE path=?", (str(path),)).fetchone()
                if dup and dup[0]:
                    conn.close()
                    return excerpt_for(dup[0], _depth=1)
            if not row:
                return ""
            txt = " ".join((row[0] or "").split())
            return txt[:max_chars].rstrip() + ("…" if len(txt) > max_chars else "")
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        return ""


def keyword_search(query, limit=20, roots=None):
    """
    FTS5-Volltextsuche über alle PDF-Chunks. Liefert [{path, hits, section, text}]
    — aggregiert pro Datei, absteigend nach Trefferzahl (Form wie search_vault).
    """
    import re

    terms = [t for t in re.split(r"[^\wäöüÄÖÜß]+", (query or "").lower()) if len(t) >= 3]
    if not terms:
        return []
    seen = []
    for t in terms:
        if t not in seen:
            seen.append(t)
    match = " OR ".join(f'"{t}" OR {t}*' for t in seen)
    conn = connect()
    try:
        try:
            rows = conn.execute(
                "SELECT d.path, d.section, d.text FROM docs_fts f JOIN docs d ON d.rowid_ = f.rowid "
                "WHERE docs_fts MATCH ? ORDER BY bm25(docs_fts) LIMIT ?",
                (match, max(1, limit) * 12),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        agg = {}
        for path, section, text in rows:
            e = agg.setdefault(path, {"path": path, "hits": 0, "section": section or "", "text": text or ""})
            e["hits"] += 1
        out = sorted(agg.values(), key=lambda e: (-e["hits"], e["path"]))
        if roots:
            try:
                from . import vault_scope as _vs

                out = [e for e in out if _vs.path_in_roots(e["path"], roots)]
            except Exception:
                pass
        return out[:limit]
    finally:
        conn.close()


def stats():
    conn = connect()
    try:
        files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        by_status = dict(conn.execute("SELECT status, COUNT(*) FROM files GROUP BY status").fetchall())
        chunks = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        paths_n = conn.execute("SELECT COUNT(DISTINCT path) FROM docs").fetchone()[0]
        _db, binp = paths()
        size = os.path.getsize(binp) if os.path.isfile(binp) else 0
        return {"files": files,
                "files_ok": by_status.get("ok", 0),
                "files_dup": by_status.get("dup", 0),
                "files_empty": by_status.get("empty", 0),
                "files_error": by_status.get("error", 0),
                "chunks": chunks, "paths": paths_n, "vectors_bytes": size, "dim": DIM,
                "sqlite": paths()[0], "bin": binp}
    finally:
        conn.close()


# --- Zeitpunkt-Recall --------------------------------------------------------
#
# docs         = aktueller Stand
# docs_history = ersetzte Chunks (siehe add_file)
#
# Beide tragen `ts` (wann der Chunk in den Index kam = Wissenszeit) und die
# Historie zusaetzlich `until_ts` (wann er ersetzt wurde). Daraus zwei Betriebsarten:
#   search_as_of()  — Suche im Stand von damals  ("was wussten wir am ...?")
#   changes_since() — Differenz damals/heute      ("was ist neu seit ...?")

_TS_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


def ts_epoch(value):
    """Index-Zeitstempel -> Unix-Sekunden. ISO (mit/ohne Offset), 'Z', Datum oder Epoch-Int."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if not s:
        return None
    if s.lstrip("-").isdigit():
        return int(s)
    txt = s[:-1] + "+0000" if s.endswith("Z") else s
    # "+02:00" -> "+0200": strptime %z akzeptiert die Doppelpunktform nicht ueberall.
    if re.search(r"[+-]\d{2}:\d{2}$", txt):
        txt = txt[:-3] + txt[-2:]
    for fmt in _TS_FORMATS:
        try:
            return int(datetime.strptime(txt, fmt).timestamp())
        except ValueError:
            continue
    return None


def parse_time(value):
    """Nutzer-Eingabe ('2025-11-01', ISO, 'YYYY-MM-DD HH:MM') -> Unix-Sekunden."""
    ep = ts_epoch(value)
    if ep is None:
        raise ValueError(
            f"Datum nicht lesbar: {value!r} - erwartet YYYY-MM-DD oder ISO-8601."
        )
    return ep


def _valid_at(conn, at_ep):
    """Zeilen, die zum Zeitpunkt at_ep gueltig waren: (vec_row, path, title, section, text, ts, version)."""
    out = []
    # Aktueller Stand gilt, wenn der Chunk vor at_ep in den Index kam.
    for path, title, section, text, ts, vec in conn.execute(
        "SELECT path, title, section, text, ts, vec_row FROM docs"
    ):
        ep = ts_epoch(ts)
        if ep is None or ep <= at_ep:
            out.append((vec, path, title, section, text, ts, "current"))
    # Historie war zwischen ts und until_ts gueltig.
    for path, title, section, text, ts, until, vec in conn.execute(
        "SELECT path, title, section, text, ts, until_ts, vec_row FROM docs_history"
    ):
        start, end = ts_epoch(ts), ts_epoch(until)
        if start is not None and start > at_ep:
            continue
        if end is not None and end <= at_ep:
            continue
        out.append((vec, path, title, section, text, ts, "history"))
    return out


def search_as_of(q_emb, as_of, candidate_k=50, roots=None):
    """
    Semantische Suche im Index-Stand von `as_of` (Zeitpunkt-Recall).
    Trefferform wie search(), zusaetzlich 'version' ('current' | 'history').
    """
    import numpy as np

    at_ep = parse_time(as_of)
    conn = connect()
    try:
        rows = _valid_at(conn, at_ep)
        if not rows:
            return []
        _db, binp = paths()
        if not os.path.isfile(binp):
            return []
        n = os.path.getsize(binp) // (DIM * 4)
        if not n:
            return []
        q = np.asarray(q_emb, dtype=np.float32).reshape(-1)
        if q.size != DIM:
            return []
        nq = float(np.linalg.norm(q))
        if nq <= 0:
            return []
        q = q / nq
        V = np.memmap(binp, dtype=np.float32, mode="r", shape=(n, DIM))
        scores = np.asarray(V @ q, dtype=np.float32)
        meta = {}
        live = np.zeros(n, dtype=bool)
        for vec, path, title, section, text, ts, version in rows:
            if 0 <= vec < n and not live[vec]:
                live[vec] = True
                meta[vec] = (path, title, section, text, ts, version)
        if not live.any():
            return []
        scores[~live] = -np.inf
        k = int(min(max(candidate_k, 1) * (3 if roots else 1), int(live.sum())))
        if k <= 0:
            return []
        idx = np.argpartition(-scores, k - 1)[:k]
        idx = idx[np.argsort(-scores[idx])]
        out = []
        for i in idx:
            s = float(scores[i])
            if s == -np.inf:
                continue
            path, title, section, text, ts, version = meta[int(i)]
            out.append({"score": round(s, 4), "path": path, "title": title,
                        "section": section, "text": text, "ts": ts, "version": version})
        if roots:
            try:
                from . import vault_scope as _vs

                out = [e for e in out if _vs.path_in_roots(e["path"], roots)]
            except Exception:
                pass
        return out
    finally:
        conn.close()


def _signatures(conn, at_ep):
    """path -> set(text_sha) fuer den Stand at_ep; at_ep None = heutiger Stand."""
    sig = {}
    for path, sha, ts in conn.execute("SELECT path, text_sha, ts FROM docs"):
        if at_ep is not None:
            ep = ts_epoch(ts)
            if ep is not None and ep > at_ep:
                continue
        sig.setdefault(path, set()).add(sha or "")
    if at_ep is not None:
        for path, sha, ts, until in conn.execute(
            "SELECT path, text_sha, ts, until_ts FROM docs_history"
        ):
            start, end = ts_epoch(ts), ts_epoch(until)
            if start is not None and start > at_ep:
                continue
            if end is not None and end <= at_ep:
                continue
            sig.setdefault(path, set()).add(sha or "")
    return sig


def changes_since(since, limit=200):
    """
    Differenz-Recall: was hat sich seit `since` geaendert?
    Vergleicht den Index-Stand von damals mit heute, pro Pfad: added / changed / removed.
    Verglichen wird ueber text_sha — eine Neuindexierung mit identischem Text
    erzeugt also KEINEN Eintrag.
    """
    since_ep = parse_time(since)
    conn = connect()
    try:
        then = _signatures(conn, since_ep)
        heute = _signatures(conn, None)
        added, changed, removed = [], [], []
        for path in sorted(set(then) | set(heute)):
            a, b = then.get(path, set()), heute.get(path, set())
            if a == b:
                continue
            if not a:
                added.append({"path": path, "chunks": len(b)})
            elif not b:
                removed.append({"path": path, "chunks": len(a)})
            else:
                changed.append({
                    "path": path,
                    "chunks_before": len(a),
                    "chunks_now": len(b),
                    "new": len(b - a),
                    "gone": len(a - b),
                })
        return {
            "since": str(since),
            "added": added[:limit],
            "changed": changed[:limit],
            "removed": removed[:limit],
            "counts": {"added": len(added), "changed": len(changed), "removed": len(removed)},
        }
    finally:
        conn.close()
