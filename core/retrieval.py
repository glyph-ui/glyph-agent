# -*- coding: utf-8 -*-
"""
Vault-Recall (Stufe B): semantische Suche über den Vault mit lokalen Embeddings.

- Embeddings: bge-m3 via Ollama (lokal, DSGVO-sauber, modellunabhängig).
- Vektorindex: persistent in einer JSON-Datei (Embedding + Metadaten), Hash-basiert.
- Retrieval: Kosinus-Ähnlichkeit, top_k + Mindestschwellwert (konfigurierbar).
- Quellen bleiben in den Treffern erhalten (Dokument, Titel, Pfad, Abschnitt).
- Strikte Trennung von WebSearch (eigenes Modul; agiert nie automatisch).

Sicherheit/Datenschutz: Nur lokale Embeddings; nichts verlässt den Rechner.
"""
import hashlib
import json
import os
import time
import urllib.request

from . import config, log

EMBED_MODEL = os.environ.get("EMBED_MODEL", "bge-m3")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
# Vektorindex-Datei (unter logs/, leicht, persistiert zwischen Läufen).
INDEX_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "logs", "vault_index.json"
)


# --- Embedding ---------------------------------------------------------------

def embed_text(text):
    """Erzeugt ein Embedding für text via lokalem Ollama-Modell. Liefert Liste[float]."""
    if not text or not text.strip():
        return []
    payload = {"model": EMBED_MODEL, "prompt": text.strip()}
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embeddings",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("embedding", [])


# --- Abschnitts-Splitting ----------------------------------------------------

def split_document(content, title, path, max_chars=1200):
    """
    Zerlegt einen Dokument-Inhalt in sinnvolle Abschnitte (an Absatz-/Überschrift-
    Grenzen, ~max_chars). Liefert Liste von {id, title, path, section, text, ts}.
    """
    from . import config as _cfg
    text = (content or "").strip()
    if not text:
        return []
    # Grob an Leerzeilen/Überschriften aufteilen, dann zu Blöcken bündeln.
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks = []
    buf = ""
    for p in paragraphs:
        if len(buf) + len(p) + 2 <= max_chars:
            buf = f"{buf}\n\n{p}" if buf else p
        else:
            if buf:
                chunks.append(buf)
            # Einzelner Absatz größer als max_chars: hart zerlegen (Embedding-Modell
            # hat Token-Limit; ein Riesen-Chunk durfte vorher mit HTTP 500 fehlschlagen).
            if len(p) > max_chars:
                for i in range(0, len(p), max_chars):
                    chunks.append(p[i:i + max_chars].strip())
                buf = ""
            else:
                buf = p
    if buf:
        chunks.append(buf)

    ts = int(time.time())
    out = []
    for i, chunk in enumerate(chunks):
        sec = chunk.split("\n", 1)[0][:80] if chunk else f"{filename_stub(title)}-{i}"
        out.append({
            "id": f"{hashlib.sha1(f'{path}#{i}'.encode()).hexdigest()[:16]}",
            "title": title,
            "path": path,
            "section": sec,
            "text": chunk,
            "ts": ts,
        })
    return out


def filename_stub(s):
    return "".join(c for c in (s or "doc") if c.isalnum() or c in " _-")[:40] or "doc"


# --- Kosinus-Ähnlichkeit -----------------------------------------------------

def cosine(a, b):
    """Kosinus-Ähnlichkeit zweier Embeddings. Handschriftlich (keine numpy-Pflicht)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# --- Index-Persistenz (JSON) -------------------------------------------------

_INDEX_CACHE = {"key": None, "data": None, "matrix": None, "matrix_pos": None, "matrix_key": None}


def _index_file_key():
    try:
        st = os.stat(INDEX_PATH)
        return (INDEX_PATH, int(st.st_mtime_ns), st.st_size)
    except OSError:
        return None


def load_index():
    """Index geladen. Gecacht über (Pfad, mtime, Größe) — 24 MB JSON nicht pro Suche parsen."""
    key = _index_file_key()
    if key is not None and _INDEX_CACHE.get("key") == key and _INDEX_CACHE.get("data") is not None:
        return _INDEX_CACHE["data"]
    try:
        with open(INDEX_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {"version": 1, "docs": []}
        key = None
    _INDEX_CACHE["key"] = key
    _INDEX_CACHE["data"] = data
    return data


def save_index(index):
    os.makedirs(os.path.dirname(INDEX_PATH), exist_ok=True)
    tmp = INDEX_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)
    os.replace(tmp, INDEX_PATH)
    _INDEX_CACHE["data"] = index
    _INDEX_CACHE["key"] = _index_file_key()
    _INDEX_CACHE["matrix"] = None
    _INDEX_CACHE["matrix_key"] = None


def doc_hash(text):
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


# --- Index-Aufbau / Aktualisierung ------------------------------------------

def index_document(title, path, content, meta=None):
    """
    Indiziert/aktualisiert ein Dokument im Vektorindex. Ersetzt alte Embeddings des
    Pfads bei Änderung (Hash), ignoriert bei unverändertem Hash. Entfernt gelöschte.
    """
    index = load_index()
    h = doc_hash(content)
    existing = [d for d in index["docs"] if d["path"] == path]
    if existing and existing[0]["hash"] == h:
        return {"status": "unchanged", "path": path}

    sections = split_document(content, title, path)
    entries = []
    for sec in sections:
        emb = embed_text(sec["text"])
        if not emb:
            # Chunk ohne Embedding NICHT still verwerfen: der Hash würde das
            # Dokument als erledigt markieren und die Suche verlöre Teile davon.
            # Besser sichtbar fehlschlagen — der nächste Aufbau versucht erneut.
            raise RuntimeError(f"Embedding fehlgeschlagen: {path}")
        entries.append({**sec, "embedding": emb})
    # Alte Einträge desselben Pfads entfernen, neue einfügen.
    index["docs"] = [d for d in index["docs"] if d["path"] != path]
    for e in entries:
        meta_clean = {k: v for k, v in (meta or {}).items()}
        e["hash"] = h
        e["meta"] = meta_clean
        index["docs"].append(e)
    index["docs"].sort(key=lambda d: d["path"])
    save_index(index)
    return {"status": "indexed", "path": path, "sections": len(entries)}


def remove_document(path):
    """Entfernt alle Einträge eines Pfads aus dem Index (gelöschtes Dokument)."""
    index = load_index()
    before = len(index["docs"])
    index["docs"] = [d for d in index["docs"] if d["path"] != path]
    save_index(index)
    return {"removed": before - len(index["docs"])}


def build_index_from_vault(vault_path=None, quiet=False):
    """
    Baut/aktualisiert den Vektorindex direkt aus dem Obsidian-Vault.

    Iteriert alle .md-Dateien unterhalb der konfigurierten Vaults (config.VAULT_PATHS,
    bzw. vault_path falls gesetzt) unter Ausschluss von Obsidian-internen Ordnern ('.'),
    'backups' und BLOCKED_DIRS — identische Filterlogik wie vault_tools.search_vault.
    Ruft pro Datei index_document() auf (Hash-basiert: nur geänderte neu indexed,
    unveränderte übersprungen, gelöschte entfernt).

    Rückgabe: dict Zähler
      {discovered, indexed, unchanged, skipped, failed, chunks, index_path, duration_s}
      + optional log_lines (wenn quiet=False).
    """
    import os as _os
    import time as _time
    from . import config as _config
    from . import vault_tools as _vt

    if vault_path:
        roots = [vault_path]
    else:
        roots = list(getattr(_config, "VAULT_PATHS", None) or [])
        if not any(r for r in roots if r):
            try:
                _config.reload_vault_paths()
            except Exception:
                pass
            roots = list(getattr(_config, "VAULT_PATHS", None) or [])
    roots = [_os.path.realpath(r) for r in roots if r]
    if not roots:
        # Leere Roots dürfen den Index nicht als „alles gelöscht“ wischen.
        return {"error": "keine Vault-Pfade — Index unverändert"}
    # Private vaults: lesbar per ReadNote, aber nicht in Embedding-Index
    try:
        from . import vaults_registry as _vr

        priv = { _os.path.realpath(p) for p in _vr.private_paths() }
        if not vault_path:
            roots = [r for r in roots if r not in priv]
    except Exception:
        pass
    invalid = [r for r in roots if not _os.path.isdir(r)]
    if invalid:
        return {"error": f"Vault-Pfad nicht gefunden: {invalid}"}

    _start_t = _time.time()
    lines = []
    def log(msg):
        if not quiet:
            lines.append(msg)

    # Zuerst gelöschte Dateien bereinigen: alle indexierten Pfade, die nicht mehr im Vault körperlich sind.
    discovered_paths = set()
    files = []  # (abs_path, index_path) wobei index_path den Vault-Präfix enthält
    for root in roots:
        for dirpath, dirnames, filenames in _os.walk(root):
            relroot = _os.path.relpath(dirpath, root)
            segs = relroot.split(_os.sep)
            # Nur UNTERORDNER filtern — der Root selbst (relroot='.') ist kein Obsidian-interner Ordner.
            if segs != ["."] and any(s.startswith(".") for s in segs):
                dirnames[:] = []
                continue
            if segs != ["."] and "backups" in segs:
                dirnames[:] = []
                continue
            if segs != ["."]:
                try:
                    if _vt._is_blocked(relroot):
                        dirnames[:] = []
                        continue
                except Exception:
                    pass
            for fn in filenames:
                if not fn.endswith(".md"):
                    continue
                full = _os.path.join(dirpath, fn)
                rel = _os.path.relpath(full, root)
                # Datei-Ebene auch gegen Blocklist prüfen + Vault-Präfix für eindeutige Pfade
                try:
                    if _vt._is_blocked(rel):
                        continue
                except Exception:
                    pass
                vname = _os.path.basename(root)
                index_path = f"/{vname}/{rel}"
                if index_path in discovered_paths:
                    continue
                files.append((full, index_path))
                discovered_paths.add(index_path)

    # Entferne im Index liegende Pfade, die nicht mehr im Vault sind.
    index = load_index()
    removed = 0
    for d in index.get("docs", []):
        if d.get("path") not in discovered_paths:
            remove_document(d.get("path"))
            removed += 1

    stats = {"indexed": 0, "unchanged": 0, "failed": 0, "chunks": 0, "removed": removed, "leer": 0}
    files.sort()
    log(f"vaults: {', '.join(_os.path.basename(r) for r in roots)}")
    log(f"documents discovered: {len(files)}")

    for full, index_path in files:
        try:
            with open(full, encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError as e:
            stats["failed"] += 1
            log(f"  SKIP lesen: {index_path} ({e})")
            continue
        if not content.strip():
            # Leere Notizen (Platzhalter/Templates) haben keinen Inhalt zu suchen
            # und würden sonst bei jedem Aufbau als „geändert" neu verhandelt —
            # ohne Chunks gibt es keinen Hash-Eintrag (2026-10-02: 3 Stück).
            stats["leer"] += 1
            continue
        title = _os.path.splitext(_os.path.basename(full))[0]
        meta = {"vault": True}
        try:
            res = index_document(title, index_path, content, meta=meta)
        except Exception as e:
            stats["failed"] += 1
            log(f"  FEHLER: {index_path} ({e})")
            continue
        if res.get("status") == "indexed":
            stats["indexed"] += 1
            stats["chunks"] += res.get("sections", 0)
        elif res.get("status") == "unchanged":
            stats["unchanged"] += 1

    log(f"documents indexed: {stats['indexed']} (geändert/neu)")
    log(f"documents unchanged: {stats['unchanged']}")
    log(f"documents empty (übersprungen): {stats['leer']}")
    log(f"chunks created: {stats['chunks']}")
    log(f"embeddings created: {stats['chunks']}")
    log(f"stale removed: {stats['removed']}")
    log(f"index written: {INDEX_PATH}")
    log(f"done in {_time.time()-_start_t:.0f}s")

    stats["discovered"] = len(files)
    stats["skipped"] = stats["unchanged"] + stats["removed"]
    stats["index_path"] = INDEX_PATH
    if not quiet:
        stats["log_lines"] = lines
    return stats


# --- Retrieval ---------------------------------------------------------------

def _normalize(text: str) -> str:
    """Kleine Normalisierung: lowercase + Whitespace komprimieren."""
    import re as _re
    return _re.sub(r"\s+", " ", (text or "").lower()).strip()


def _keyword_boost(query: str, doc: dict) -> float:
    """
    Hybrider Keyword-Boost für exakte Begriffe in Titel/Pfad (nicht Chunk-Text).

    Hebt konzeptionelle Grundlagen-Dokumente (z. B. '00 MOC - Arbeitssicherheit.md')
    über rein vektoriell ähnliche, aber thematisch engere Chunks (Maschinen etc.).
    Wirkt als Reranker auf den Top-Kandidaten — niemals als Ersatz der Vektorsuche.

    Boost-Werte moderat: bei Vektor-Scores 0.65–0.72 heben 0.04–0.16 die richtige
    Grundlagen-MOC deutlich nach vorne, ohne die Vektorsuche zu überstimmen.
    Zusätzlich: allgemeine Query-Tokens (inkl. Daten YYYY-MM-DD) im Titel/Pfad.
    """
    query_norm = _normalize(query)
    path = _normalize(doc.get("path") or "")
    title = _normalize(doc.get("title") or (path.rsplit("/", 1)[-1] if path else ""))

    # Begriffspaare, die in der Frage gemeinsam vorkommen (z. B. Arbeitsschutz + Arbeitssicherheit)
    pair_terms = []
    for t1, t2 in (("arbeitsschutz", "arbeitssicherheit"),):
        if t1 in query_norm and t2 in query_norm:
            pair_terms.append((t1, t2))

    boost = 0.0
    # Paar-Frage (Frage enthält beide Begriffe):
    # Eine Datei, die MINDESTENS EINEN der Begriffe im Titel trägt, ist bei solchen
    # Fragen sehr wahrscheinlich die Grundlagen-/Unterscheidungs-Datei — auch wenn
    # der zweite Begriff nicht in der Datei steht (z. B. '00 MOC - Arbeitssicherheit.md'
    # bei 'Arbeitsschutz vs. Arbeitssicherheit'). Voller Paar-Boost.
    for t1, t2 in pair_terms:
        if (t1 in title or t2 in title):
            boost += 0.16
            return boost
        # Kein Titel-Treffer: dann das Paar im Pfad (Ordnerstruktur) suchen
        if (t1 in path and t2 in path):
            boost += 0.16
            return boost

    # Einzelne exakte Begriffe im Dateinamen/Titel (stärker) oder Pfad (schwächer)
    for term in ("arbeitsschutz", "arbeitssicherheit"):
        if term in title:
            boost += 0.08
        elif term in path:
            boost += 0.04

    # Allgemeine Tokens aus der Frage (Dateiname/Datum) — moderat, gecappt
    try:
        from . import vault_tools as _vt
        tokens = _vt._tokenize_query(query)
    except Exception:
        tokens = []
    general = 0.0
    for term in tokens:
        if term in ("arbeitsschutz", "arbeitssicherheit"):
            continue  # bereits oben
        if len(term) < 3 and not any(c.isdigit() for c in term):
            continue
        if term in title:
            general += 0.08 if any(c.isdigit() for c in term) else 0.05
        elif term in path:
            general += 0.05 if any(c.isdigit() for c in term) else 0.03
    boost += min(general, 0.20)
    return boost


def _doc_ident(d):
    return d.get("id") or (str(d.get("path")) + "|" + str(d.get("section") or ""))


def _md_matrix():
    """(matrix, pos) aller .md-Embeddings als float32, L2-normiert — gecacht.

    Der Index bleibt im JSON-Format (Schreibpfad unverändert); nur das Rechnen
    wandert von der Python-Schleife nach BLAS: 1.080 Docs linear = 1,1 s → ~0,02 s.
    """
    try:
        import numpy as np
    except Exception:
        return None, None
    docs = (load_index().get("docs") or [])
    key = _index_file_key()
    if key is not None and _INDEX_CACHE.get("matrix_key") == key:
        mat, pos = _INDEX_CACHE.get("matrix"), _INDEX_CACHE.get("matrix_pos")
        if mat is not None and mat.shape[0] == len(docs):
            return mat, pos
    dims = {len(d.get("embedding") or []) for d in docs}
    if not docs or len(dims) != 1 or 0 in dims:
        return None, None
    dim = dims.pop()
    mat = np.zeros((len(docs), dim), dtype=np.float32)
    pos = {}
    for i, d in enumerate(docs):
        emb = d.get("embedding") or []
        mat[i] = np.asarray(emb, dtype=np.float32)
        pos[_doc_ident(d)] = i
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat = mat / norms
    _INDEX_CACHE["matrix"], _INDEX_CACHE["matrix_pos"], _INDEX_CACHE["matrix_key"] = mat, pos, key
    return mat, pos


def _score_docs(docs, q_emb):
    """(score, doc)-Paare für alle Docs. numpy wenn möglich, sonst wie bisher."""
    try:
        import numpy as np
    except Exception:
        return [(cosine(q_emb, d.get("embedding", [])), d) for d in docs]
    if not docs:
        return []
    mat, pos = _md_matrix()
    if mat is None:
        return [(cosine(q_emb, d.get("embedding", [])), d) for d in docs]
    rows = [pos.get(_doc_ident(d)) for d in docs]
    if any(r is None for r in rows):
        return [(cosine(q_emb, d.get("embedding", [])), d) for d in docs]
    q = np.asarray(q_emb, dtype=np.float32)
    if q.size != mat.shape[1]:
        return [(cosine(q_emb, d.get("embedding", [])), d) for d in docs]
    nq = float(np.linalg.norm(q)) or 1.0
    sims = mat[np.asarray(rows, dtype=np.int64)] @ (q / nq)
    return list(zip(sims.tolist(), docs))


def _pdf_candidates(q_emb, candidate_k, roots=None):
    """PDF-Chunks aus core/pdf_index.py — leer, wenn Store fehlt oder leer ist."""
    try:
        from . import pdf_index

        return pdf_index.search(q_emb, candidate_k=candidate_k, roots=roots) or []
    except Exception as e:
        # Nicht still schlucken: hier sah ein fehlendes numpy jahrelang aus wie
        # "keine PDF-Treffer" und hat die PDF-Suche lautlos abgeschaltet.
        log.log("pdf_candidates_fail", error=f"{type(e).__name__}: {e}"[:200])
        return []


def search(query, top_k=None, min_score=None, roots=None):
    """
    Semantische Suche im Vektorindex mit Hybrid-Reranking (Vektor + Keyword-Boost).

    Liefert {status, query, candidates, selected, threshold, sources, results, error?}.
    top_k/min_score konfigurierbar (Config-Defaults: VAULT_TOP_K, VAULT_MIN_SCORE).

    Zwei getrennte K-Werte:
      - CANDIDATE_K (50): Vektor-Kandidaten, auf denen der Keyword-Boost wirkt
        (damit ein Boost Rang 24 noch erreichen kann).
      - FINAL_K (5): nach Hybrid-Reranking ausgelieferte Treffer.
    Die Schwelle (0.6) bleibt unverändert und wird NACH dem Boost angewendet.

    Hinweis: Für das kanonische B+-Finde-Werkzeug siehe vault_find()
    (0.7 Embedding + 0.3 Keyword-Volltext, OpenClaw-Vorbild).
    """
    import os
    if top_k is None:
        top_k = int(os.environ.get("VAULT_TOP_K", "4"))
    if min_score is None:
        min_score = float(os.environ.get("VAULT_MIN_SCORE", "0.6"))
    candidate_k = int(os.environ.get("VAULT_CANDIDATE_K", "50"))
    final_k = top_k
    index = load_index()
    docs = index.get("docs", [])
    if roots is not None:
        from . import vault_scope as _vs

        docs = [d for d in docs if _vs.path_in_roots(d.get("path"), roots)]
    q_emb = embed_text(query)
    if not q_emb:
        return {"status": "error", "query": query, "error": "Embedding fehlgeschlagen"}

    # 1) Vektor-Score für alle Docs, sortiert absteigend, Top-CANDIDATE_K behalten
    scored = _score_docs(docs, q_emb)

    # 1b) PDF-Chunks aus dem Vektorindex dazu (gleiche Pipeline, gleiche Schwelle)
    for r in _pdf_candidates(q_emb, candidate_k, roots):
        scored.append((float(r.get("score") or 0.0), {
            "id": "pdf:" + str(r.get("path")) + ":" + str(r.get("section") or ""),
            "title": r.get("title"),
            "path": r.get("path"),
            "section": r.get("section") or "",
            "text": r.get("text") or "",
            "ts": r.get("ts"),
        }))

    if not scored:
        return {
            "status": "empty", "query": query, "candidates": 0, "selected": 0,
            "threshold": min_score, "sources": [], "results": [],
        }
    scored.sort(key=lambda x: x[0], reverse=True)
    candidates = scored[:candidate_k]

    # 2) Hybrid-Reranking: Vektor-Score + Keyword-Boost (nur auf Kandidaten)
    reranked = []
    for s, d in candidates:
        boost = _keyword_boost(query, d)
        reranked.append((s + boost, s, boost, d))
    reranked.sort(key=lambda x: x[0], reverse=True)

    # 3) Schwelle + FINAL_K: Hybrid-Score muss >= min_score sein (Boost kann drunterliegende anheben)
    selected = [
        {"score": round(hybrid, 4), "vector_score": round(vec, 4), "boost": round(b, 4),
         **{k: (v if k != "embedding" else None) for k, v in d.items()}}
        for hybrid, vec, b, d in reranked if hybrid >= min_score
    ][:final_k]
    sources = sorted({r["path"] for r in selected})
    return {
        "status": "success",
        "query": query,
        "candidates": len(candidates),
        "selected": len(selected),
        "threshold": min_score,
        "sources": sources,
        "results": selected,
        "top_k": final_k,
        "candidate_k": candidate_k,
        "error": None,
    }


# --- B+ VaultFind: 0.7 Embedding + 0.3 Keyword (OpenClaw hybrid) ------------

def vault_find(query, top_k=None, min_score=None,
               vector_weight=None, text_weight=None, roots=None):
    """
    Kanonisches Finde-Werkzeug (B+).

    Mischt:
      - semantische Treffer aus dem Embedding-Index (Gewicht vector_weight, Default 0.7)
      - Keyword-Treffer aus dem Vault-Volltext (Gewicht text_weight, Default 0.3)

    Liefert dasselbe Schema wie search(), plus mode='hybrid' und keyword_hits.
    VaultRecall/VaultSearch rufen dies intern auf (ein Werkzeug, zwei Aliase).
    """
    import os
    from . import vault_tools

    if top_k is None:
        top_k = int(os.environ.get("VAULT_TOP_K", "4"))
    if min_score is None:
        min_score = float(os.environ.get("VAULT_MIN_SCORE", "0.35"))
    if vector_weight is None:
        vector_weight = float(os.environ.get("VAULT_VECTOR_WEIGHT", "0.7"))
    if text_weight is None:
        text_weight = float(os.environ.get("VAULT_TEXT_WEIGHT", "0.3"))
    # Normieren
    wsum = (vector_weight or 0) + (text_weight or 0) or 1.0
    vw, tw = vector_weight / wsum, text_weight / wsum

    if roots is not None:
        roots = [r for r in roots if r]
        if not roots:
            return {
                "status": "empty",
                "query": query,
                "mode": "hybrid",
                "candidates": 0,
                "selected": 0,
                "threshold": min_score,
                "sources": [],
                "results": [],
                "top_k": top_k,
                "keyword_hits": 0,
            }

    # --- Embedding-Zweig (niedrigere Schwelle: Merge entscheidet) ---
    sem = search(query, top_k=max(top_k * 3, 12), min_score=0.0, roots=roots)
    vec_by_path = {}
    for r in (sem.get("results") or []):
        p = r.get("path")
        if not p:
            continue
        # vector_score bevorzugen, sonst score
        vs = float(r.get("vector_score") if r.get("vector_score") is not None else r.get("score") or 0)
        if p not in vec_by_path or vs > vec_by_path[p]["vector"]:
            vec_by_path[p] = {"vector": vs, "meta": r}
    if roots:
        from . import vault_scope as _vs

        vec_by_path = {
            p: v for p, v in vec_by_path.items() if _vs.path_in_roots(p, roots)
        }

    # --- Keyword-Zweig ---
    kw_raw = []
    try:
        kw_raw = vault_tools.search_vault(query, limit=max(top_k * 5, 20), roots=roots) or []
    except Exception:
        kw_raw = []
    # PDF-Volltext (FTS5) — gleiche Normalisierung wie die .md-Treffer
    kw_pdf = []
    try:
        from . import pdf_index as _pi

        kw_pdf = _pi.keyword_search(query, limit=max(top_k * 5, 20), roots=roots) or []
    except Exception:
        kw_pdf = []
    max_hits = max(
        [int(h.get("hits") or 0) for h in kw_raw]
        + [int(h.get("hits") or 0) for h in kw_pdf]
        + [0]
    ) or 1
    kw_by_path = {}
    for h in kw_raw:
        # search_vault liefert rel path; Index nutzt oft /VaultName/rel
        rel = h.get("path") or ""
        vault = h.get("vault") or ""
        candidates_paths = [rel]
        if vault:
            candidates_paths.append(f"/{vault}/{rel}".replace("//", "/"))
            candidates_paths.append(f"{vault}/{rel}")
        score_kw = min(1.0, float(h.get("hits") or 0) / float(max_hits))
        for p in candidates_paths:
            if p and (p not in kw_by_path or score_kw > kw_by_path[p]):
                kw_by_path[p] = score_kw
    # PDF-Treffer: kanonische Pfade, direkt auf den Index-Pfad abbildbar
    for h in kw_pdf:
        p = h.get("path") or ""
        if not p:
            continue
        sc = min(1.0, float(h.get("hits") or 0) / float(max_hits))
        if p not in kw_by_path or sc > kw_by_path[p]:
            kw_by_path[p] = sc

    # Pfade matchen: Keyword-rel → besten Index-Pfad
    def _kw_score_for_index_path(ipath):
        if ipath in kw_by_path:
            return kw_by_path[ipath]
        best = 0.0
        for kp, sc in kw_by_path.items():
            if ipath.endswith(kp) or kp in ipath or ipath.endswith("/" + kp.lstrip("/")):
                best = max(best, sc)
        return best

    all_paths = set(vec_by_path.keys())
    # Keyword-only Pfade (noch nicht im Index) auch aufnehmen
    for kp in kw_by_path:
        # nur wenn schon ein Index-Pfad matched, sonst als keyword-only
        matched = False
        for ip in vec_by_path:
            if ip.endswith(kp) or kp in ip:
                matched = True
                break
        if not matched:
            all_paths.add(kp)

    merged = []
    for p in all_paths:
        v = vec_by_path.get(p, {}).get("vector", 0.0)
        if p in vec_by_path:
            k = _kw_score_for_index_path(p)
            meta = vec_by_path[p]["meta"]
        else:
            k = kw_by_path.get(p, 0.0)
            meta = {"path": p, "title": p.rsplit("/", 1)[-1], "section": "", "text": ""}
        hybrid = vw * float(v) + tw * float(k)
        # Kanonische Live-Dateien vor Wiki-Source-Hash-Archiven (gleiche Logik wie search_vault)
        path_for_rank = meta.get("path") or p
        # Vault-Name aus Index-Pfad (/HSEQ Sync/…) oder Keyword-Hit
        vault_guess = ""
        pl = path_for_rank.replace("\\", "/").lstrip("/")
        if "/" in pl:
            vault_guess = pl.split("/", 1)[0]
        rank_bonus = vault_tools.path_source_rank(
            path_for_rank, vault_name=vault_guess, query=query
        )
        # rank_bonus ist in Keyword-Hit-Einheiten (~±50); skaliere auf 0–1-Hybrid
        hybrid = hybrid + (rank_bonus / 100.0)
        # Archiv-Kopien: nur behalten wenn Keyword/Vektor klar stark
        if vault_tools._is_archive_source_path(path_for_rank):
            if float(k) < 0.85 and float(v) < min_score:
                continue
        elif hybrid < min_score and float(v) < min_score and float(k) < 0.5:
            continue
        entry = {
            "path": meta.get("path") or p,
            "title": meta.get("title"),
            "section": meta.get("section"),
            "text": meta.get("text"),
            "score": round(hybrid, 4),
            "vector_score": round(float(v), 4),
            "keyword_score": round(float(k), 4),
            "vector_weight": vw,
            "text_weight": tw,
        }
        merged.append(entry)

    merged.sort(key=lambda x: x["score"], reverse=True)
    selected = merged[:top_k]
    sources = sorted({r["path"] for r in selected if r.get("path")})
    status = "success" if selected else ("empty" if not vec_by_path and not kw_raw else "empty")
    return {
        "status": status,
        "query": query,
        "mode": "hybrid",
        "candidates": len(all_paths),
        "selected": len(selected),
        "threshold": min_score,
        "sources": sources,
        "results": selected,
        "top_k": top_k,
        "keyword_hits": len(kw_raw) + len(kw_pdf),
        "vector_weight": vw,
        "text_weight": tw,
        "error": sem.get("error"),
    }
