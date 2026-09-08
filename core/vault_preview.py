# -*- coding: utf-8 -*-
"""Manuelle Ordner-Suche: Trefferliste für den °_Agent-Toggle (kein LLM)."""
import os
import time

from . import retrieval, vault_scope, vault_tools
from .vault_web_fallback import run_deadline

_MAX_HITS = 12
_EXCERPT = 240
# Stay under glyph-ui.com Cloudflare HTTP timeout (100s) and UI proxy (90s).
PREVIEW_BUDGET_S = 70
# Disk-Namen zuerst zeigen. Index darf nachziehen, aber nicht die Liste blockieren.
INDEX_WAIT_WITH_HITS_S = 4.0
INDEX_WAIT_EMPTY_S = 20.0

def preview_vault_hits(query, top_k=8, budget_s=None, now=None):
    """Namens-Treffer auf Disk + VaultFind-Eltern (+ ListVaultDir bei Inventar).

    Liefert {ok, query, status, hits:[{id, kind, path, title, excerpt, score}]}.
    Pfade kanonisch `/VaultName/rel`. Ordner mit passendem Namen stehen vorn —
    auch wenn der Embedding-Index die Datei noch nicht kennt.
    Disk-Namen laufen vor VaultFind, damit ein hängendes Ollama nicht 502 erzeugt.
    """
    q = (query or "").strip()
    if not q:
        return {"ok": True, "query": "", "status": "empty", "hits": []}

    budget = PREVIEW_BUDGET_S if budget_s is None else float(budget_s)
    start = now if now is not None else time.monotonic()
    deadline = start + max(0.0, budget)

    # Wiki = internes Gedächtnis, nie auswählbar: Treffer aus memory-wiki
    # fliegen hier raus (Dateien, Ordner, Elternordner). Internes Retrieval
    # (vault_scope.wiki_roots) bleibt unangetastet.
    wiki_roots = tuple(vault_scope.wiki_roots())

    def _is_wiki_hit(path):
        return bool(path) and vault_scope.path_in_roots(path, wiki_roots)

    files = []
    folders = {}
    folder_by_disk = {}
    seen_files = set()
    file_by_disk = {}

    def _put_folder(path, score):
        canon = vault_tools.canon_vault_path(path)
        if not canon or canon in (".", "/"):
            return
        if _is_wiki_hit(canon):
            return
        leaf = canon.rstrip("/").rsplit("/", 1)[-1].lower()
        if leaf == "vorlagen":
            return
        disk = _disk_id(canon)
        prev_key = folder_by_disk.get(disk)
        if prev_key is not None:
            prev = folders.get(prev_key)
            keep = _prefer_canon(prev_key, canon)
            if keep != prev_key:
                folders.pop(prev_key, None)
                hit = _folder_hit(canon, score)
                if prev and _score_val(prev.get("score")) > _score_val(score):
                    hit["score"] = prev.get("score")
                folders[canon] = hit
                folder_by_disk[disk] = canon
            elif prev is not None and _score_val(score) > _score_val(prev.get("score")):
                prev["score"] = score
            return
        folders[canon] = _folder_hit(canon, score)
        folder_by_disk[disk] = canon

    def _put_file(hit):
        if not hit:
            return
        canon = vault_tools.canon_vault_path(hit["path"])
        if not canon:
            return
        if _is_wiki_hit(canon):
            return
        hit = dict(hit)
        hit["path"] = canon
        hit["id"] = f"file:{canon}"
        if not str(hit.get("excerpt") or "").strip():
            hit["excerpt"] = _snip_excerpt(canon)
        disk = _disk_id(canon)
        if disk in file_by_disk:
            i = file_by_disk[disk]
            oldp = files[i].get("path") or ""
            if _prefer_canon(oldp, canon) == canon:
                files[i] = hit
            return
        if canon in seen_files:
            return
        seen_files.add(canon)
        file_by_disk[disk] = len(files)
        files.append(hit)
        parent = _parent_folder(canon)
        if parent:
            _put_folder(parent, hit.get("score"))

    # 1) Dateiname/Ordnername auf Disk — bevor Index-Hits den Slot füllen
    try:
        named = vault_tools.match_vault_entries(q)
    except Exception:
        named = []
    named_folders = []
    for row in named:
        if not isinstance(row, dict):
            continue
        path = row.get("path") or ""
        if row.get("kind") == "folder":
            _put_folder(path, row.get("score"))
            named_folders.append(path)
        else:
            _put_file(_named_file_hit(row))

    def _vault_find():
        try:
            return retrieval.vault_find(q, top_k=top_k)
        except Exception:
            return {"status": "empty", "results": []}

    named_have = bool(named_folders or files)
    remaining = deadline - time.monotonic()
    if named_have:
        index_wait = min(INDEX_WAIT_WITH_HITS_S, max(0.0, remaining))
    else:
        index_wait = min(INDEX_WAIT_EMPTY_S, max(0.0, remaining))
    index_deadline = time.monotonic() + index_wait
    found = run_deadline(_vault_find, index_deadline, {"status": "empty", "results": []})
    if not isinstance(found, dict):
        found = {"status": "empty", "results": []}

    # 2) VaultFind (Index/Hybrid)
    for row in found.get("results") or []:
        if not isinstance(row, dict):
            continue
        _put_file(_file_hit(row))

    from . import tool_loop

    # 3) Inventar: nur den genannten Ordner listen, nicht alle Vault-Roots
    if tool_loop._is_vault_list_question(q):
        paths = []
        infer = getattr(tool_loop, "_infer_vault_list_paths", None)
        if callable(infer):
            paths = list(infer(q) or [])
        else:
            one = tool_loop._infer_vault_list_path(q)
            if one:
                paths = [one]
        if not paths and named_folders:
            paths = named_folders[:3]
        generic = not any(len(t) >= 6 for t in vault_tools._tokenize_query(q))
        if not paths and generic:
            paths = ["."]
        for lpath in paths[:3]:
            listing = None
            try:
                listing = vault_tools.list_vault_dir(lpath)
            except Exception:
                listing = None
            for entry in (listing or {}).get("entries") or []:
                if not isinstance(entry, dict):
                    continue
                path = str(entry.get("path") or "").strip()
                if not path:
                    continue
                if entry.get("type") == "dir":
                    _put_folder(path, None)
                else:
                    _put_file(
                        {
                            "id": f"file:{path}",
                            "kind": "file",
                            "path": path,
                            "title": str(entry.get("name") or path.rsplit("/", 1)[-1]),
                            "excerpt": "",
                            "score": None,
                        }
                    )

    # Namens-Ordner (max 4) + Dateien, dann restliche Ordner — Dateien nicht
    # von Index-Elternordnern aus dem 12er-Limit drücken.
    named_first = sorted(
        [h for h in folders.values() if _score_val(h.get("score")) >= 70],
        key=lambda h: (-_score_val(h.get("score")), h.get("path") or ""),
    )
    named_ids = {h["path"] for h in named_first}
    other_folders = [h for p, h in folders.items() if p not in named_ids]
    top_folders = named_first[:4]
    named_files = sorted(
        [h for h in files if _score_val(h.get("score")) >= 70],
        key=lambda h: (-_score_val(h.get("score")), h.get("path") or ""),
    )
    named_file_ids = {h.get("id") for h in named_files}
    other_files = [h for h in files if h.get("id") not in named_file_ids]
    hits = named_files + top_folders + other_folders + named_first[4:] + other_files
    hits = hits[:_MAX_HITS]
    # Starke Treffer = Namens-Treffer (>=70) oder Embedding >= _EMBED_FLOOR.
    # Schwache Nur-Embedding-Treffer (~0.35-0.5, auch bei Nonsens-Queries)
    # duerfen den KomNet/DGUV-Web-Fallback nicht blockieren.
    # Namens-Treffer (Datei/Ordner auf Disk) sind das verlaessliche Signal.
    # Die Embeddings sind derzeit zu flach (Nonsens ~0.5-0.66), um eine
    # absolute Schwelle zu rechtfertigen — ohne Namens-Treffer gilt der
    # Vault als leer und der Web-Fallback darf ran.
    strong = bool(named_files or named_first)
    fallback = None
    tried = []
    if not strong:
        from . import vault_web_fallback

        fb = vault_web_fallback.fallback_web_hits(
            q, vault_hit_count=0, deadline=deadline
        )
        hits = list(fb.get("hits") or [])[:_MAX_HITS]
        fallback = fb.get("source")
        tried = list(fb.get("tried") or [])
    status = "success" if hits else (found.get("status") or "empty")
    return {
        "ok": True,
        "query": q,
        "status": status,
        "hits": hits,
        "selected": len(hits),
        "sources": [h["path"] for h in hits if h.get("kind") == "file"],
        "fallback": fallback,
        "tried": tried,
    }


def _snip_excerpt(path):
    """Kurzer Notiz-Text für die Trefferliste, ohne LLM."""
    if not str(path or "").lower().endswith(".md"):
        return ""
    try:
        abs_p = vault_tools._resolve_vault_path(path)
    except Exception:
        abs_p = None
    if not abs_p or not os.path.isfile(abs_p):
        return ""
    try:
        with open(abs_p, encoding="utf-8", errors="replace") as fh:
            raw = fh.read(2000)
    except OSError:
        return ""
    body = raw
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            body = body[end + 4:]
    body = " ".join(body.split())
    if len(body) > _EXCERPT:
        body = body[:_EXCERPT].rstrip() + "…"
    return body



def _disk_id(path):
    """Gleiche Datei auf der Platte, egal welcher Vault-Pfad."""
    try:
        abs_p = vault_tools._resolve_vault_path(path)
    except Exception:
        abs_p = None
    if abs_p:
        try:
            return "disk:" + os.path.realpath(abs_p)
        except OSError:
            pass
    return "path:" + (vault_tools.canon_vault_path(path) or "")


def _prefer_canon(a, b):
    """Lieber /Fachvault/rel als /Benutzername/ObsidianVaults/..."""
    def rank(p):
        p = vault_tools.canon_vault_path(p) or ""
        n = p.lstrip("/").split("/", 1)[0]
        home = os.path.basename(os.path.expanduser("~"))
        if n == home or "ObsidianVaults" in p:
            return 2
        if n:
            return 0
        return 1
    if rank(b) < rank(a):
        return b
    return a


def _score_val(score):
    try:
        return float(score) if score is not None else -1.0
    except (TypeError, ValueError):
        return -1.0


def _named_file_hit(row):
    path = str(row.get("path") or "").strip()
    if not path:
        return None
    title = str(row.get("title") or path.rsplit("/", 1)[-1] or path)
    return {
        "id": f"file:{path}",
        "kind": "file",
        "path": path,
        "title": title,
        "excerpt": str(row.get("excerpt") or ""),
        "score": row.get("score"),
    }


def _file_hit(row):
    path = str(row.get("path") or "").strip()
    if not path:
        return None
    title = str(row.get("title") or path.rsplit("/", 1)[-1] or path)
    excerpt = str(row.get("text") or row.get("section") or "").strip()
    if len(excerpt) > _EXCERPT:
        excerpt = excerpt[:_EXCERPT].rstrip() + "…"
    score = row.get("score")
    try:
        score = float(score) if score is not None else None
    except (TypeError, ValueError):
        score = None
    return {
        "id": f"file:{path}",
        "kind": "file",
        "path": path,
        "title": title,
        "excerpt": excerpt,
        "score": score,
    }


def _folder_hit(path, score):
    path = vault_tools.canon_vault_path(path)
    title = path.rstrip("/").rsplit("/", 1)[-1] or path
    return {
        "id": f"folder:{path}",
        "kind": "folder",
        "path": path,
        "title": title,
        "excerpt": "",
        "score": score,
    }


def _parent_folder(path):
    p = vault_tools.canon_vault_path(path)
    if p.count("/") < 2:
        return ""
    return p.rsplit("/", 1)[0]
