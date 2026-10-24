# -*- coding: utf-8 -*-
"""
Inbox → Vault: gemischte Drops klassifizieren, kopieren, wikilinken.

OCR/Index suchen nur, was im HSEQ-Vault liegt. Dieses Modul füllt den Vault
(Kopie) und hängt Wikilinks in eine MOC. Privat landet im Privat-Vault und
geht nicht in den Suchindex.

Quellen (Aufrufer): iCloud Scanordner, ~/Downloads, iCloud «01 Beruf».
Nur kopieren, nie löschen. Passwörter-Ordner: skip.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import unicodedata

INGEST_EXTS = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".md", ".png", ".jpg", ".jpeg", ".heic", ".heif", ".tif", ".tiff",
)

_HSEQ_TOP = frozenset({
    "arbeitssicherheit", "brandschutz", "umweltmanagement", "umwelt",
    "qualitatsmanagement", "regelwerke", "sigiko", "tuv sud", "tuv",
    "energierecht",
})
_INTERN_TOP = frozenset({
    "bewerbung", "zeugnisse", "stundennachweise", "kundensammlung",
    "datenbanken",
})
_HSEQ_NEEDLES = (
    "betriebsanweisung", "trgs", "dguv", "gefahrstoff", "arbeitssicherheit",
    "brandschutz", "betrsichv", "arbschg", "gefstoff", "sigeko", "sifako",
    "unterweisung", "iso 45001", "iso45001", "iso 9001", "iso9001",
    "iso 14001", "iso14001", "asr a", "asr-a", "gbu", "psa", "regelwerk",
    "awsv", "immission", "lagerung von gefahrstoffen", "leitern und tritte",
    "qualitatsmanagement", "umweltmanagement", "auditbericht",
)
_PRIVAT_NEEDLES = (
    "implantat", "zwangsvollstreckung", "mietvertrag", "personalausweis",
    "fuhrerschein", "krankenkasse", "steuerbescheid", "lohnsteuer",
    "kindergeld", "scheidung", "testament", "kontoauszug", "impfpass",
    "rezept", "mahnung privat",
)

_ASI_MOC = "Themen/00 Eingang-Dateien.md"
_PRIVAT_MOC = "00 Eingang.md"
_INTERN_MOC = "Beruf-intern/00 Dateien.md"


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFC", s or "").lower()
    s = s.replace("ß", "ss")
    return s.translate(str.maketrans("äöü", "aou"))


def _parts(rel: str):
    return [p for p in rel.replace("\\", "/").split("/") if p and p != "."]


def _is_password_part(p: str) -> bool:
    f = _fold(p)
    return (
        f.startswith("passw")
        or "passwort" in f
        or "password" in f
        or f in ("credentials", "secrets")
    )


def _after_beruf(parts):
    for i, p in enumerate(parts):
        if _fold(p) in ("01 beruf", "beruf"):
            return parts[i + 1 :]
    return None


def classify(rel: str) -> str:
    """hseq | intern | privat | skip."""
    parts = _parts(rel)
    if not parts:
        return "skip"
    if any(_is_password_part(p) for p in parts):
        return "skip"
    name = _fold(parts[-1])
    rest = _after_beruf(parts)
    if rest:
        top = _fold(rest[0]) if rest else ""
        if _is_password_part(rest[0] if rest else ""):
            return "skip"
        if top in _INTERN_TOP:
            return "intern"
        if top in _HSEQ_TOP:
            return "hseq"
        return "intern"
    blob = _fold("/".join(parts))
    if any(n in blob for n in _HSEQ_NEEDLES):
        return "hseq"
    if any(n in blob for n in _PRIVAT_NEEDLES):
        return "privat"
    return "privat"


def dest_rel(kind: str, src_rel: str, day: str) -> str:
    parts = _parts(src_rel)
    name = parts[-1] if parts else "datei"
    if kind == "privat":
        return f"00 Eingang/{day}/{name}"
    rest = _after_beruf(parts)
    if kind == "intern":
        tail = "/".join(rest) if rest else name
        return f"Beruf-intern/{tail}"
    if rest and rest[0] and _fold(rest[0]) in _HSEQ_TOP:
        return "/".join(rest)
    return f"00 Arbeitsfluss/Eingang-Dateien/{day}/{name}"


def skip_index_rel(rel: str) -> bool:
    parts = _parts(rel)
    if not parts:
        return True
    if parts[0] == "Beruf-intern":
        return True
    if parts[0].startswith("."):
        return True
    return False


def wikilink_line(rel: str) -> str:
    return f"- [[{rel.replace(chr(92), '/')}]]"


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_hash_cache(path):
    if not path or not os.path.isfile(path):
        return {}
    try:
        import json
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_hash_cache(path, data):
    if not path:
        return
    import json
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def collect_hashes(root: str, cache_path: str | None = None) -> set:
    out = set()
    cache = _load_hash_cache(cache_path) if cache_path else {}
    dirty = False
    if not root or not os.path.isdir(root):
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and not _is_password_part(d)]
        for fn in filenames:
            if fn.startswith("."):
                continue
            if os.path.splitext(fn)[1].lower() not in INGEST_EXTS:
                continue
            p = os.path.join(dirpath, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            hit = cache.get(p)
            if (
                isinstance(hit, dict)
                and hit.get("mtime") == st.st_mtime
                and hit.get("size") == st.st_size
                and hit.get("sha")
            ):
                out.add(hit["sha"])
                continue
            try:
                sha = file_sha256(p)
            except OSError:
                continue
            out.add(sha)
            cache[p] = {"mtime": st.st_mtime, "size": st.st_size, "sha": sha}
            dirty = True
    if dirty and cache_path:
        _save_hash_cache(cache_path, cache)
    return out


def _append_moc(abs_moc: str, rel: str):
    line = wikilink_line(rel)
    os.makedirs(os.path.dirname(abs_moc), exist_ok=True)
    if os.path.isfile(abs_moc):
        with open(abs_moc, encoding="utf-8") as fh:
            body = fh.read()
        if line in body.splitlines():
            return
        nl = "" if body.endswith("\n") else "\n"
        with open(abs_moc, "a", encoding="utf-8") as fh:
            fh.write(f"{nl}{line}\n")
        return
    title = os.path.splitext(os.path.basename(abs_moc))[0]
    with open(abs_moc, "w", encoding="utf-8") as fh:
        fh.write(
            f"---\ntype: moc\n---\n# {title}\n\n"
            f"Neue Dateien im Vault (Kopie, Original bleibt).\n\n{line}\n"
        )


def _iter_source_files(sources):
    for src in sources:
        if not src or not os.path.isdir(src):
            continue
        label = os.path.basename(os.path.abspath(src.rstrip(os.sep)))
        for dirpath, dirnames, filenames in os.walk(src):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            rel_dir = os.path.relpath(dirpath, src)
            for fn in sorted(filenames):
                if fn.startswith("."):
                    continue
                rel_inside = fn if rel_dir == "." else os.path.join(rel_dir, fn)
                display = os.path.join(label, rel_inside).replace("\\", "/")
                ext = os.path.splitext(fn)[1].lower()
                if ext not in INGEST_EXTS and classify(display) != "skip":
                    continue
                yield os.path.join(dirpath, fn), display


def sync(sources, asi_root, privat_root, day, limit=0, hashes=None, cache_path=None):
    """Kopiert neue Dateien. limit=0 heißt unbegrenzt (Tests)."""
    known = set(hashes or ())
    known |= collect_hashes(asi_root, cache_path=cache_path)
    known |= collect_hashes(privat_root, cache_path=cache_path)
    stats = {
        "copied_hseq": 0,
        "copied_privat": 0,
        "copied_intern": 0,
        "skipped_dup": 0,
        "skipped_secret": 0,
        "index_files": [],
    }
    copies = 0
    for abs_src, display in _iter_source_files(sources):
        kind = classify(display)
        if kind == "skip":
            stats["skipped_secret"] += 1
            continue
        try:
            sha = file_sha256(abs_src)
        except OSError:
            continue
        if sha in known:
            stats["skipped_dup"] += 1
            continue
        rel = dest_rel(kind, display, day)
        root = privat_root if kind == "privat" else asi_root
        dest = os.path.join(root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(abs_src, dest)
        known.add(sha)
        copies += 1
        if kind == "hseq":
            stats["copied_hseq"] += 1
            _append_moc(os.path.join(asi_root, _ASI_MOC.replace("/", os.sep)), rel)
            if not skip_index_rel(rel):
                stats["index_files"].append(dest)
        elif kind == "intern":
            stats["copied_intern"] += 1
            _append_moc(os.path.join(asi_root, _INTERN_MOC.replace("/", os.sep)), rel)
        else:
            stats["copied_privat"] += 1
            _append_moc(os.path.join(privat_root, _PRIVAT_MOC.replace("/", os.sep)), rel)
        if limit and copies >= limit:
            break
    return stats
