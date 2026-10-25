# -*- coding: utf-8 -*-
"""
Code-Tools für den ^_Code-Modus (C′).

Nur Workspace-Roots, Diff+Backup beim Schreiben, Shell nur per Whitelist.
Kein VaultFind, keine Obsidian-Pfade — getrennte Sicherheitsdomäne vom Vault-Agenten.
"""
from __future__ import annotations

import difflib
import glob
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from . import config, log
from . import code_grants

# Absolute Maxima (Defense in Depth)
_MAX_READ_BYTES = 512 * 1024
_MAX_WRITE_BYTES = 1024 * 1024
_MAX_LIST_ENTRIES = 200
_MAX_LIST_RECURSIVE = 400
_MAX_LIST_DEPTH = 2
_MAX_CMD_OUTPUT = 80_000
_MAX_GREP_HITS = 50
_MAX_GREP_FILE_BYTES = 512 * 1024
_SKIP_DIR_NAMES = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
}


def workspace_roots():
    """Kanonische erlaubte Roots (realpath) — SoT workspaces.json, Fallback Env/Config.

    Store geladen + nichts accessible → [] (kein Default-rw).
    CODE_WORKSPACE_ROOTS nur wenn Store fehlt oder CODE_WORKSPACES_USE_REGISTRY=false.
    """
    if getattr(config, "CODE_WORKSPACES_USE_REGISTRY", True):
        try:
            from . import workspaces_registry as wr
            wr.load_store()  # Datei existiert oder wurde geseedet
            return list(wr.accessible_roots())  # auch []
        except Exception:
            pass
    roots = []
    seen = set()
    for r in getattr(config, "CODE_WORKSPACE_ROOTS", []) or []:
        r = (r or "").strip()
        if not r:
            continue
        try:
            expanded = os.path.expanduser(r)
            if not os.path.isdir(expanded):
                continue
            real = os.path.realpath(expanded)
        except OSError:
            continue
        if real in seen:
            continue
        seen.add(real)
        roots.append(real)
    return roots


def _ordered_roots():
    """Primary first, then remaining accessible roots."""
    roots = workspace_roots()
    if not roots:
        return []
    primary = None
    if getattr(config, "CODE_WORKSPACES_USE_REGISTRY", True):
        try:
            from . import workspaces_registry as wr
            primary = wr.primary_root()
        except Exception:
            primary = None
    if primary and primary in roots:
        return [primary] + [r for r in roots if r != primary]
    return list(roots)


def _deny_secret(cand):
    """Secret-/Config-Dateien im erlaubten Zimmer sind gesperrt (Safe im Raum).

    True, wenn der aufgeloeste absolute Pfad eine Secret-/Config-Datei mit
    Schluesseln, Tokens oder privaten Daten adressiert. Blockiert Lesen UND
    Schreiben fuer alle Datei-Tools (read_file/write_file/search_replace/grep).
    """
    if not cand:
        return True
    name = os.path.basename(str(cand)).lower()
    parts = str(cand).split(os.sep)
    # .env-Dateien (und Varianten) — aber Beispiel-/Template-Dateien erlauben
    env_templ = name.startswith(".env.example") or name.startswith(".env.sample") \
        or name.startswith(".env.template")
    if name == ".env" or (name.startswith(".env.") and not env_templ):
        return True
    # Sonstige Secret-/Config-Dateinamen
    if name in (
        ".npmrc", ".pypirc", ".netrc", "credentials", "credentials.json",
        "secrets.json", "service_account.json", "kubeconfig",
        "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "authorized_keys",
        "config.json", "known_hosts",
    ):
        return True
    # Stets gesperrte private Unterordner innerhalb eines Roots
    if any(seg in (".ssh", ".aws", ".gnupg", ".kube", "gcloud") for seg in parts):
        return True
    return False


def _resolve_path(path):
    """
    Löst path relativ zu einem Workspace-Root auf.
    Absoluter Pfad: muss unter einem Root liegen.
    Relativer Pfad: Primary-Root zuerst, dann übrige.
    Liefert (abs_path, root) oder (None, None).
    """
    if path is None or str(path).strip() == "":
        return None, None
    raw = str(path).strip()
    roots = _ordered_roots()
    if not roots:
        return None, None

    if os.path.isabs(raw):
        cand = os.path.realpath(raw)
        # longest matching root wins
        best = None
        best_len = -1
        for root in roots:
            if cand == root or cand.startswith(root + os.sep):
                if len(root) > best_len:
                    best = (cand, root)
                    best_len = len(root)
        if best and _deny_secret(best[0]):
            return None, None
        return best if best else (None, None)

    # Relativ: Root-Name (Basename) direkt auflösen, sonst Primary zuerst,
    # dann übrige Roots (projektrelativer Pfad kann in jedem Root liegen).
    # 0) "glyph-ui/client/…" → /…/glyph-ui/client/…  (Basename ersetzt den Root,
    #    statt ihn doppelt zu stapeln). Sonst würde "glyph-ui/client/..." zu
    #    "/…/glyph-ui/glyph-ui/client/..." aufgelöst.
    for root in roots:
        base = os.path.basename(root.rstrip("/"))
        if base and (raw == base or raw.startswith(base + "/")):
            rest = raw[len(base):].lstrip("/")
            cand = os.path.realpath(os.path.join(root, rest)) if rest else root
            if cand == root or cand.startswith(root + os.sep):
                return (None, None) if _deny_secret(cand) else (cand, root)
    # 1) Basename eines bekannten Roots → Root selbst (z. B. "glyph-ui" → /…/glyph-ui)
    for root in roots:
        if os.path.basename(root.rstrip("/")) == raw:
            return root, root
    # 2) Zugehörigkeit: erst Primary, dann übrige Roots — für Schreib-Tools
    #    (neue Dateien existieren noch nicht, nur die Zugehörigkeit zählt).
    ordered = roots
    primary = _ordered_roots()
    if primary and primary[0] in ordered:
        ordered = [primary[0]] + [r for r in ordered if r != primary[0]]
    first_zip = None
    for root in ordered:
        cand = os.path.realpath(os.path.join(root, raw))
        if cand == root or cand.startswith(root + os.sep):
            if _deny_secret(cand):
                continue
            if first_zip is None:
                first_zip = (cand, root)
            # Existierender Treffer gewinnt (Lese-Fall: Datei liegt real dort)
            if os.path.exists(cand):
                return cand, root
    # 3) Kein existierender Treffer, aber Zugehörigkeit gefunden → zurück
    if first_zip is not None:
        return first_zip
    return None, None


def mode_for_resolved(root):
    """Workspace-Mode für ein Root: r | rw | private | None."""
    if not root:
        return None
    if not getattr(config, "CODE_WORKSPACES_USE_REGISTRY", True):
        return "rw"  # Tests / Legacy-Config: r+w
    try:
        from . import workspaces_registry as wr
        return wr.mode_for_root(root) or "r"
    except Exception:
        return "rw"


def _rel_display(abs_path, root):
    try:
        return os.path.relpath(abs_path, root)
    except ValueError:
        return abs_path


def list_dir(path=".", recursive=False, max_depth=None):
    """Listet Verzeichnis unter Workspace-Root.

    recursive=True: max. Tiefe 2 (cap), mit Gesamteinträge-Cap.
    """
    abs_path, root = _resolve_path(path or ".")
    if not abs_path:
        raise ValueError(f"Pfad außerhalb der Workspace-Roots: {path}")
    if not os.path.isdir(abs_path):
        raise ValueError(f"Kein Verzeichnis: {path}")

    want_rec = bool(recursive)
    depth_cap = _MAX_LIST_DEPTH
    if max_depth is not None:
        try:
            depth_cap = max(0, min(int(max_depth), _MAX_LIST_DEPTH))
        except (TypeError, ValueError):
            depth_cap = _MAX_LIST_DEPTH

    entries = []
    truncated = False

    if not want_rec:
        try:
            names = sorted(os.listdir(abs_path))
        except OSError as e:
            raise ValueError(str(e)) from e
        for name in names[:_MAX_LIST_ENTRIES]:
            full = os.path.join(abs_path, name)
            kind = "dir" if os.path.isdir(full) else "file"
            try:
                size = os.path.getsize(full) if kind == "file" else None
            except OSError:
                size = None
            entries.append({"name": name, "kind": kind, "size": size})
        truncated = len(names) > _MAX_LIST_ENTRIES
    else:
        # Rekursiv mit Depth- und Entry-Cap
        def _walk(cur, depth):
            nonlocal truncated
            try:
                names = sorted(os.listdir(cur))
            except OSError:
                return
            for name in names:
                if len(entries) >= _MAX_LIST_RECURSIVE:
                    truncated = True
                    return
                if name in _SKIP_DIR_NAMES or name.startswith("."):
                    # .git etc. überspringen; versteckte nur flach erlauben wenn nicht skip
                    if name in _SKIP_DIR_NAMES:
                        continue
                full = os.path.join(cur, name)
                rel = os.path.relpath(full, abs_path)
                kind = "dir" if os.path.isdir(full) else "file"
                try:
                    size = os.path.getsize(full) if kind == "file" else None
                except OSError:
                    size = None
                entries.append({"name": rel.replace(os.sep, "/"), "kind": kind, "size": size})
                if kind == "dir" and depth < depth_cap and name not in _SKIP_DIR_NAMES:
                    _walk(full, depth + 1)
                if truncated:
                    return

        _walk(abs_path, 0)

    log.log(
        "code_list_dir",
        path=_rel_display(abs_path, root),
        n=len(entries),
        recursive=want_rec,
    )
    return {
        "path": _rel_display(abs_path, root),
        "root": root,
        "entries": entries,
        "truncated": truncated,
        "recursive": want_rec,
        "max_depth": depth_cap if want_rec else 0,
    }


def read_file(path, max_bytes=None, offset=None, limit=None):
    """Liest Textdatei (UTF-8, replacement) innerhalb der Roots.

    offset/limit: optional Zeilen (1-basiert offset; limit = max. Zeilen).
    """
    abs_path, root = _resolve_path(path)
    if not abs_path:
        raise ValueError(f"Pfad außerhalb der Workspace-Roots: {path}")
    if not os.path.isfile(abs_path):
        raise ValueError(f"Datei nicht gefunden: {path}")
    byte_limit = int(max_bytes or _MAX_READ_BYTES)
    with open(abs_path, "rb") as f:
        data = f.read(byte_limit + 1)
    truncated = len(data) > byte_limit
    data = data[:byte_limit]
    text = data.decode("utf-8", errors="replace")

    line_offset = None
    line_limit = None
    total_lines = None
    if offset is not None or limit is not None:
        lines = text.splitlines(keepends=True)
        total_lines = len(lines)
        try:
            # 1-basiert; offset=1 = erste Zeile; offset=0/None = ab Anfang
            start = int(offset) if offset is not None else 1
        except (TypeError, ValueError):
            start = 1
        if start < 1:
            start = 1
        try:
            n = int(limit) if limit is not None else None
        except (TypeError, ValueError):
            n = None
        line_offset = start
        line_limit = n
        idx0 = start - 1
        if n is None:
            slice_lines = lines[idx0:]
        else:
            slice_lines = lines[idx0 : idx0 + max(0, n)]
        text = "".join(slice_lines)
        # Zeilen-Fenster: truncated wenn nicht alle Zeilen oder Byte-Cap
        if idx0 + len(slice_lines) < total_lines:
            truncated = True

    log.log(
        "code_read_file",
        path=_rel_display(abs_path, root),
        chars=len(text),
        offset=line_offset,
        limit=line_limit,
    )
    out = {
        "path": _rel_display(abs_path, root),
        "content": text,
        "chars": len(text),
        "truncated": truncated,
    }
    if line_offset is not None:
        out["offset"] = line_offset
        out["limit"] = line_limit
        out["total_lines"] = total_lines
    return out


def grep(pattern, path=".", max_hits=None, case_insensitive=False):
    """
    Sucht pattern in Dateien unter path (nur Workspace-Roots).
    Nutzt `rg` wenn im PATH, sonst Python-Walk. Max. Treffer capped.
    """
    if not pattern or not str(pattern).strip():
        raise ValueError("grep: pattern fehlt")
    pattern = str(pattern)
    abs_path, root = _resolve_path(path or ".")
    if not abs_path:
        raise ValueError(f"Pfad außerhalb der Workspace-Roots: {path}")
    if not os.path.exists(abs_path):
        raise ValueError(f"Pfad nicht gefunden: {path}")

    cap = int(max_hits or _MAX_GREP_HITS)
    cap = max(1, min(cap, 200))
    ci = bool(case_insensitive)

    hits = []
    engine = "python"
    # --- try ripgrep ---
    import shutil
    rg = shutil.which("rg")
    if rg:
        engine = "rg"
        argv = [rg, "--line-number", "--no-heading", "--color", "never",
                "--max-count", str(cap)]
        if ci:
            argv.append("-i")
        # Skip heavy dirs
        for d in _SKIP_DIR_NAMES:
            argv.extend(["--glob", f"!{d}/**"])
        argv.extend(["--", pattern, abs_path])
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=30, check=False,
            )
            # rg exit 1 = no matches; 0 = matches; 2 = error
            if proc.returncode not in (0, 1):
                engine = "python"  # fallback
            else:
                for line in (proc.stdout or "").splitlines():
                    if len(hits) >= cap:
                        break
                    # format: path:lineno:text
                    if ":" not in line:
                        continue
                    # split max 2 times from left for path that may contain :
                    # rg uses path:line:content
                    m = re.match(r"^(.*?):(\d+):(.*)$", line)
                    if not m:
                        continue
                    fpath, lineno, content = m.group(1), int(m.group(2)), m.group(3)
                    try:
                        rel = os.path.relpath(fpath, root)
                    except ValueError:
                        rel = fpath
                    hits.append({
                        "path": rel.replace(os.sep, "/"),
                        "line": lineno,
                        "text": content[:500],
                    })
        except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
            engine = "python"
            hits = []

    if engine == "python":
        hits = []
        flags = re.IGNORECASE if ci else 0
        try:
            cre = re.compile(pattern, flags)
        except re.error as e:
            raise ValueError(f"Ungültiges Regex-Pattern: {e}") from e

        def _search_file(fpath):
            try:
                if os.path.getsize(fpath) > _MAX_GREP_FILE_BYTES:
                    return
            except OSError:
                return
            try:
                with open(fpath, "rb") as f:
                    raw = f.read(_MAX_GREP_FILE_BYTES)
            except OSError:
                return
            if b"\x00" in raw[:8000]:
                return  # binär
            text = raw.decode("utf-8", errors="replace")
            for i, line in enumerate(text.splitlines(), 1):
                if cre.search(line):
                    try:
                        rel = os.path.relpath(fpath, root)
                    except ValueError:
                        rel = fpath
                    hits.append({
                        "path": rel.replace(os.sep, "/"),
                        "line": i,
                        "text": line[:500],
                    })
                    if len(hits) >= cap:
                        return

        if os.path.isfile(abs_path):
            _search_file(abs_path)
        else:
            for dirpath, dirnames, filenames in os.walk(abs_path):
                dirnames[:] = [
                    d for d in dirnames
                    if d not in _SKIP_DIR_NAMES and not d.startswith(".")
                ]
                # Stay under root
                try:
                    real_dp = os.path.realpath(dirpath)
                    if not (real_dp == root or real_dp.startswith(root + os.sep)):
                        dirnames[:] = []
                        continue
                except OSError:
                    continue
                for fn in filenames:
                    if len(hits) >= cap:
                        break
                    _search_file(os.path.join(dirpath, fn))
                if len(hits) >= cap:
                    break

    truncated = len(hits) >= cap
    log.log(
        "code_grep",
        pattern=pattern[:80],
        path=_rel_display(abs_path, root),
        hits=len(hits),
        engine=engine,
    )
    return {
        "pattern": pattern,
        "path": _rel_display(abs_path, root),
        "root": root,
        "hits": hits,
        "count": len(hits),
        "truncated": truncated,
        "engine": engine,
    }


def search_replace(path, old, new):
    """
    Ersetzt old→new exakt einmal in einer Datei (1 Treffer Pflicht).
    Backup wie WriteFile. Kein Regex.
    """
    if old is None or str(old) == "":
        raise ValueError("SearchReplace: old darf nicht leer sein")
    old = str(old)
    new = "" if new is None else str(new)
    abs_path, root = _resolve_path(path)
    if not abs_path:
        raise ValueError(f"Pfad außerhalb der Workspace-Roots: {path}")
    if not os.path.isfile(abs_path):
        raise ValueError(f"Datei nicht gefunden: {path}")

    with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    count = content.count(old)
    if count == 0:
        raise ValueError("SearchReplace: old-String nicht gefunden (0 Treffer)")
    if count > 1:
        raise ValueError(
            f"SearchReplace: old-String kommt {count}× vor — muss exakt 1 Treffer sein"
        )
    if old == new:
        return {
            "path": _rel_display(abs_path, root),
            "applied": False,
            "reason": "no_change",
        }
    new_content = content.replace(old, new, 1)
    # reuse write_file for backup + atomic write
    result = write_file(path, new_content)
    result["replaced"] = True
    result["old_chars"] = len(old)
    result["new_chars"] = len(new)
    log.log("code_search_replace", path=_rel_display(abs_path, root), applied=result.get("applied"))
    return result


def propose_write(path, content):
    """Diff-Vorschau ohne Schreiben."""
    abs_path, root = _resolve_path(path)
    if not abs_path:
        raise ValueError(f"Pfad außerhalb der Workspace-Roots: {path}")
    content = content if content is not None else ""
    if len(content.encode("utf-8")) > _MAX_WRITE_BYTES:
        raise ValueError(f"Inhalt zu groß (>{_MAX_WRITE_BYTES} Bytes)")
    old = ""
    exists = os.path.isfile(abs_path)
    if exists:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            old = f.read()
    old_lines = old.splitlines(keepends=True)
    new_lines = str(content).splitlines(keepends=True)
    rel = _rel_display(abs_path, root)
    diff = "".join(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            lineterm="",
        )
    )
    return {
        "path": rel,
        "diff": diff,
        "changed": old != content,
        "exists": exists,
        "old_chars": len(old),
        "new_chars": len(content),
    }


def write_file(path, content):
    """
    Schreibt Datei mit Backup (wie Vault ApplyEdit, aber Workspace-Roots).
    Kein Löschen, kein Umbenennen. Nur Workspace-Mode r+w.
    """
    abs_path, root = _resolve_path(path)
    if not abs_path:
        raise ValueError(f"Pfad außerhalb der Workspace-Roots: {path}")
    mode = mode_for_resolved(root)
    if mode != "rw":
        raise ValueError(
            f"Schreiben verboten: Workspace-Mode ist {mode or '?'} "
            f"(braucht r+w) — Root {root}"
        )
    content = content if content is not None else ""
    raw = str(content).encode("utf-8")
    if len(raw) > _MAX_WRITE_BYTES:
        raise ValueError(f"Inhalt zu groß (>{_MAX_WRITE_BYTES} Bytes)")

    # Keine Directory-Traversal-Reste, Parent muss unter Root bleiben
    parent = os.path.dirname(abs_path)
    parent_real = os.path.realpath(parent) if os.path.isdir(parent) else os.path.realpath(
        os.path.join(root, os.path.relpath(parent, root) if parent.startswith(root) else parent)
    )
    # Parent anlegen nur wenn unter root
    if not (parent == root or parent.startswith(root + os.sep) or os.path.realpath(parent).startswith(root + os.sep) or os.path.realpath(parent) == root):
        # join-basiert: parent of new file under root
        if not abs_path.startswith(root + os.sep) and abs_path != root:
            raise ValueError(f"Unsicherer Parent-Pfad: {path}")

    os.makedirs(os.path.dirname(abs_path), exist_ok=True)

    old = ""
    if os.path.isfile(abs_path):
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            old = f.read()
    if old == content:
        return {
            "path": _rel_display(abs_path, root),
            "applied": False,
            "reason": "no_change",
        }

    backup_dir = getattr(config, "CODE_BACKUP_DIR", None) or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "vault", "code_backups"
    )
    os.makedirs(backup_dir, exist_ok=True)
    rel = _rel_display(abs_path, root)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = rel.replace(os.sep, "__").replace("/", "__")
    backup_file = os.path.join(backup_dir, f"{safe}.{stamp}.bak")
    if old:
        with open(backup_file, "w", encoding="utf-8") as f:
            f.write(old)

    tmp = abs_path + ".tmp-glyph-code"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, abs_path)

    log.log(
        "code_write_file",
        path=rel,
        backup=os.path.basename(backup_file) if old else None,
        old_chars=len(old),
        new_chars=len(content),
    )
    return {
        "path": rel,
        "applied": True,
        "backup": os.path.basename(backup_file) if old else None,
        "created": not bool(old),
    }


# --- Shell ---

# Hart verboten — nie, auch nicht nach Elevated-Freigabe
_HARD_DENY_PATTERNS = [
    r"\brm\b",
    r"\bchmod\b",
    r"\bchown\b",
    r"\bsudo\b",
    r"\bdoas\b",
    r"\bmkfs\b",
    r"\bdd\b",
    # Redirect auf einen echten Pfad — ABER Standard-Devices (/dev/null, /dev/zero,
    # /dev/random, /dev/tty) sind harmlos. Nutzer-Freigabe soll für die gelten.
    r">\s*/dev/(?!null|zero|random|urandom|tty)\b",
    r">\s*/(?!dev/null|dev/zero|dev/random|dev/urandom|dev/tty)\S",

    r"\bcurl\b.*\|\s*(ba)?sh",
    r"\bwget\b.*\|\s*(ba)?sh",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bkill\b",
    r"\bkillall\b",
    r"\blaunchctl\b",
    r"\bdiskutil\b",
    r"\bsecurity\b",
    r"\bosascript\b",
    r"`",
    r"\$\(",
]

# Elevated: Popup (Einmal), dann ausführen — inkl. Compound und git push
_ELEVATED_PATTERNS = [
    (r"\bgit\s+push\b", "git push — schreibt Remote"),
    (r"\bgit\s+pull\b", "git pull — ändert lokalen Stand vom Remote"),
    (r"\bgit\s+fetch\b", "git fetch — holt Remote-Refs"),
    (r"^npm\s+run\s+service(:|\b)", "npm run service:* — LaunchAgent/Service"),
    (r"\bmv\b", "mv — verschiebt/umbenennt Dateien"),
]

# Compound/Pipe nur AUSSERHALB von Quotes: `rg "a|b"` ist ein Suchmuster,
# kein Pipe. Sonst verlangt jede Regex-Alternation eine Freigabe.
_COMPOUND_RE = re.compile(r"(?:&&|\|\||[;|])")


def _strip_quoted(cmd):
    """Entfernt '...'- und "..."-Inhalte, damit Quotes nicht als Pipe zaehlen."""
    out = []
    quote = None
    escaped = False
    for ch in str(cmd or ""):
        if escaped:
            escaped = False
            continue
        if quote and ch == "\\":
            escaped = True
            continue
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        out.append(ch)
    return "".join(out)

# Legacy alias for tests
_DENY_PATTERNS = list(_HARD_DENY_PATTERNS) + [
    r"\bgit\s+push\b",  # still "denied" without elevated_ok via classify
]


def _split_pipes(cmd):
    """Pipe-Segmente (Quotes bleiben erhalten). None bei &&/||/;/& oder ohne Pipe."""
    segs, buf = [], []
    quote = None
    escaped = False
    s = str(cmd or "")
    i = 0
    while i < len(s):
        ch = s[i]
        if escaped:
            buf.append(ch); escaped = False; i += 1; continue
        if quote and ch == "\\":
            buf.append(ch); escaped = True; i += 1; continue
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1; continue
        if ch in ("'", '"'):
            quote = ch; buf.append(ch); i += 1; continue
        if ch == "|":
            if i + 1 < len(s) and s[i + 1] == "|":
                return None
            segs.append("".join(buf).strip()); buf = []; i += 1; continue
        if ch in ("&", ";"):
            return None
        buf.append(ch); i += 1
    segs.append("".join(buf).strip())
    segs = [x for x in segs if x]
    return segs if len(segs) > 1 else None


def _readonly_pipe(cmd):
    """True, wenn jede Pipe-Stufe erlaubt UND nur read/test ist.

    Beispiele: `wc -l x | sort -nr | head -3` -> True; `ls | npm install x` -> False.
    """
    segs = _split_pipes(cmd)
    if not segs:
        return False
    for seg in segs:
        kind, _ = shell_classify(seg)
        if kind != "allow":
            return False
        cls, _k, _r = shell_action_class(seg)
        if cls not in ("read", "test"):
            return False
    return True


def _default_allow_patterns():
    return list(getattr(config, "CODE_SHELL_ALLOW", None) or [])


def shell_action_class(command):
    """Aktionsklasse für Grant-Checks. kind bleibt shell_classify.

    Returns: (action_class, kind, reason)
      action_class: read | test | file_change | package_install |
                    git_commit | network | deploy | deny
    """
    kind, reason = shell_classify(command)
    cmd = (command or "").strip()
    if kind == "deny":
        return "deny", kind, reason
    if re.search(r"^git\s+commit\b", cmd, re.IGNORECASE):
        return "git_commit", kind, reason
    if re.search(r"^npx\b", cmd, re.IGNORECASE) or re.search(
        r"^npm\s+(install|ci)\b", cmd, re.IGNORECASE
    ):
        return "package_install", kind, reason
    if re.search(r"\b(curl|wget)\b", cmd, re.IGNORECASE):
        return "network", kind, reason
    if kind == "elevated":
        return "deploy", kind, reason
    # sed ohne -i liest nur; mit -i schreibt es.
    if re.search(r"^sed\\b", cmd) and not re.search(r"\\s-i(\\s|$)", cmd):
        return "read", kind, reason
    if re.search(
        r"^(npm\s+(test|run\s+(test(:|\b)|lint\b|typecheck\b|check\b|smoke\b))|pytest|python3?\s+-m\s+(pytest|unittest))\b",
        cmd,
        re.IGNORECASE,
    ):
        return "test", kind, reason
    if re.search(
        r"^(ls|pwd|echo|cat|head|tail|wc|rg|grep|find|diff|sort|uniq|cut|tr|"
        r"stat|file|basename|dirname|realpath|which|awk|"
        r"git\s+(status|diff|log|show|branch|rev-parse|remote)|"
        r"python3? --version|node --version|npm --version|"
        r"npm\s+(ls|view|outdated|pack))\b",
        cmd,
        re.IGNORECASE,
    ):
        return "read", kind, reason
    return "file_change", kind, reason


def shell_classify(command):
    """
    Klassifiziert Shell-Befehl.
    Returns: (kind, reason)
      kind: "allow" | "elevated" | "deny"
      reason: human-readable (risk line or deny reason)
    """
    cmd = (command or "").strip()
    if not cmd:
        return "deny", "leerer Befehl"
    if len(cmd) > 2000:
        return "deny", "Befehl zu lang"
    if "\n" in cmd or "\r" in cmd:
        return "deny", "Mehrzeilige Befehle verboten"
    for pat in _HARD_DENY_PATTERNS:
        if re.search(pat, cmd, re.IGNORECASE):
            return "deny", f"hart verboten: {pat}"
    if _readonly_pipe(cmd):
        return "allow", None
    if _COMPOUND_RE.search(_strip_quoted(cmd)):
        return "elevated", "Compound/Pipe (&& ; | ||)"
    for pat, risk in _ELEVATED_PATTERNS:
        if re.search(pat, cmd, re.IGNORECASE):
            return "elevated", risk
    allows = _default_allow_patterns()
    if not allows:
        return "deny", "keine Shell-Whitelist konfiguriert"
    for pat in allows:
        try:
            if re.search(pat, cmd):
                return "allow", None
        except re.error:
            continue
    return "deny", "nicht in der Shell-Whitelist"


def shell_allowed(command, *, allow_elevated=False):
    """True, wenn command freigegeben werden darf (Whitelist oder elevated+Flag)."""
    kind, reason = shell_classify(command)
    if kind == "allow":
        return True, None
    if kind == "elevated":
        if allow_elevated:
            return True, None
        return False, f"Elevated-Freigabe nötig: {reason}"
    return False, reason


def run_command(command, cwd=None, timeout=None, allow_elevated=False):
    """
    Führt Shell unter Workspace-Root aus.
    Whitelist: shlex.split (shell=False).
    Elevated Compound: bash -lc nach Freigabe (Hard-Deny gilt weiter).
    """
    kind, reason = shell_classify(command)
    if kind == "deny":
        raise ValueError(f"Shell abgelehnt: {reason}")
    if kind == "elevated" and not allow_elevated:
        raise ValueError(f"Shell abgelehnt: Elevated-Freigabe nötig: {reason}")

    roots = _ordered_roots()
    if not roots:
        raise ValueError("Keine Workspace-Roots konfiguriert")

    work = cwd or roots[0]
    abs_cwd, root = _resolve_path(work)
    if not abs_cwd or not os.path.isdir(abs_cwd):
        abs_cwd, root = roots[0], roots[0]
        if cwd:
            raise ValueError(f"cwd außerhalb Workspace-Roots: {cwd}")

    mode = mode_for_resolved(root)
    if mode != "rw":
        raise ValueError(
            f"Shell verboten: Workspace-Mode ist {mode or '?'} (braucht r+w)"
        )

    timeout_s = int(timeout or getattr(config, "CODE_SHELL_TIMEOUT", 60) or 60)
    timeout_s = max(1, min(timeout_s, 300))

    # Pipes brauchen eine Shell: elevated Compound ODER erlaubte read-only-Pipe.
    use_shell = bool(
        re.search(r"(?:&&|\|\||[;|])", (command or "").strip())
    )
    if use_shell:
        argv = ["bash", "-lc", command]
        bin_name = "bash"
    else:
        try:
            argv = shlex.split(command)
        except ValueError as e:
            raise ValueError(f"Befehl nicht parsbar: {e}") from e
        if not argv:
            raise ValueError("leerer Befehl")
        # Globs wie die Shell expandieren — nur Dateimuster, keine Substitution.
        if any("$" not in a and any(c in a for c in "*?[") for a in argv[1:]):
            expanded = [argv[0]]
            for a in argv[1:]:
                if "$" not in a and any(c in a for c in "*?["):
                    hits = sorted(glob.glob(os.path.join(abs_cwd, a)))
                    if hits:
                        expanded.extend(
                            os.path.relpath(h, abs_cwd) for h in hits
                        )
                        continue
                expanded.append(a)
            argv = expanded
        bin_name = os.path.basename(argv[0])

    env = os.environ.copy()
    env["LANG"] = env.get("LANG") or "en_US.UTF-8"

    log.log(
        "code_run_command",
        cmd=command[:200],
        cwd=_rel_display(abs_cwd, root),
        elevated=bool(allow_elevated and kind == "elevated"),
        shell_lc=use_shell,
    )
    try:
        proc = subprocess.run(
            argv,
            cwd=abs_cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        err = (e.stderr or "") if isinstance(e.stderr, str) else f"Timeout nach {timeout_s}s"
        return {
            "command": command,
            "cwd": _rel_display(abs_cwd, root),
            "exit_code": -1,
            "stdout": out[:_MAX_CMD_OUTPUT],
            "stderr": err[:_MAX_CMD_OUTPUT],
            "timeout": True,
            "elevated": kind == "elevated",
        }
    except FileNotFoundError:
        raise ValueError(f"Befehl nicht gefunden: {bin_name}") from None

    stdout = (proc.stdout or "")[:_MAX_CMD_OUTPUT]
    stderr = (proc.stderr or "")[:_MAX_CMD_OUTPUT]
    return {
        "command": command,
        "cwd": _rel_display(abs_cwd, root),
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "timeout": False,
        "truncated": len(proc.stdout or "") > _MAX_CMD_OUTPUT
        or len(proc.stderr or "") > _MAX_CMD_OUTPUT,
        "elevated": kind == "elevated",
    }


def _grant_fields(root, rel, action_class, preview):
    hint = code_grants.outside_task_hint(root, rel, action_class)
    return {
        "action_class": action_class,
        "workspace_root": root or "",
        "rel_path": rel or "",
        "outside_task": bool(hint),
        "hint": hint,
        "preview": preview,
        "elevated": False,
        "risk": hint or "",
    }


def permission_decision(tool_name, args):
    """
    Policy für ^_Code-Tools.
    Returns dict:
      action: "allow" | "confirm" | "requires_grant" | "deny"
      reason: str
      elevated: bool
      risk: str
      preview: str
    """
    args = args or {}
    name = tool_name or ""

    if name in ("ListDir", "ReadFile", "Grep"):
        path = args.get("path") or "."
        abs_path, root = _resolve_path(path)
        if not abs_path:
            return {
                "action": "deny",
                "reason": f"Pfad außerhalb der Workspace-Roots: {path}",
                "elevated": False,
                "risk": "",
                "preview": "",
            }
        return {
            "action": "allow",
            "reason": "",
            "elevated": False,
            "risk": "",
            "preview": "",
        }

    # Glyph-Aufgabe schließen: SoT ~/.glyph/tasks, kein Workspace-Root, kein Popup.
    if name == "TaskFinish":
        return {
            "action": "allow",
            "reason": "Aufgabe-Abschluss (Artefakt + done)",
            "elevated": False,
            "risk": "",
            "preview": "",
        }

    # Stufe 1+2: read-only Wissens-/Reichweiten-Tools — immer erlaubt
    # (Vault-Zugriff intern durch Graph-Head geschützt).
    if name in (
        "VaultFind", "ReadNote", "WikiGet",
        "WebSearch", "ExtractUrl",
        "MailList", "MailRead", "ReadMemory",
    ):
        return {
            "action": "allow",
            "reason": "read-only Wissens-Tool",
            "elevated": False,
            "risk": "",
            "preview": "",
        }

    if name in ("WriteFile", "SearchReplace"):
        path = args.get("path")
        abs_path, root = _resolve_path(path)
        preview = preview_for_confirm(name, args)
        if not abs_path:
            return {
                "action": "deny",
                "reason": f"Pfad außerhalb der Workspace-Roots: {path}",
                "elevated": False,
                "risk": "",
                "preview": preview,
            }
        mode = mode_for_resolved(root)
        if mode != "rw":
            return {
                "action": "deny",
                "reason": (
                    f"Schreiben verboten: Mode {mode or '?'} "
                    f"(braucht r+w) — {root}"
                ),
                "elevated": False,
                "risk": "",
                "preview": preview,
            }
        rel = _rel_display(abs_path, root)
        hit = code_grants.matching(root, rel, "file_change")
        if hit:
            return {
                "action": "allow",
                "reason": code_grants.why_allowed(hit),
                "elevated": False,
                "risk": "",
                "preview": preview,
                "grant_id": hit.get("grant_id"),
                "allowed_by": code_grants.why_allowed(hit),
            }
        extra = _grant_fields(root, rel, "file_change", preview)
        return {
            "action": "requires_grant",
            "reason": extra["hint"] or "Freigabe für Dateiänderung",
            **extra,
        }

    if name == "VaultWrite":
        if not _vault_write_enabled():
            return {
                "action": "deny",
                "reason": "Kein Vault für ^_Code auf Schreiben (rw) geschaltet — Graph: Kopf ^_Code am Vault auf Schreiben.",
                "elevated": False,
                "risk": "",
                "preview": "",
            }
        path = args.get("path") or ""
        preview = f"VaultWrite → {path}\n{str(args.get('content') or '')[:300]}"
        # Vault-Root des Pfads ermitteln (für Grant-Matching)
        root = ""
        rel = str(path or "")
        try:
            from . import config as _cfg
            from . import vault_tools as _vt

            _cfg.reload_vault_paths()
            resolved = _vt._resolve_vault_path(rel)
            if resolved:
                for r in getattr(_cfg, "VAULT_PATHS", []) or []:
                    rr = os.path.realpath(r)
                    if resolved == rr or resolved.startswith(rr + os.sep):
                        root = rr
                        break
        except Exception:
            pass
        if not root:
            return {
                "action": "deny",
                "reason": f"Pfad nicht in einem Vault: {path}",
                "elevated": False,
                "risk": "",
                "preview": preview,
            }
        hit = code_grants.matching(root, rel, "file_change")
        if hit:
            return {
                "action": "allow",
                "reason": code_grants.why_allowed(hit),
                "elevated": False,
                "risk": "",
                "preview": preview,
                "grant_id": hit.get("grant_id"),
                "allowed_by": code_grants.why_allowed(hit),
            }
        return {
            "action": "requires_grant",
            "reason": "Freigabe für Vault-Änderung",
            "elevated": False,
            "risk": "",
            "preview": preview,
        }

    if name == "RunCommand":
        cmd = args.get("command") or ""
        cwd = args.get("cwd") or "."
        abs_cwd, root = _resolve_path(cwd)
        if not abs_cwd:
            roots = _ordered_roots()
            if not roots:
                return {
                    "action": "deny",
                    "reason": "Keine Workspace-Roots",
                    "elevated": False,
                    "risk": "",
                    "preview": preview_for_confirm(name, args),
                }
            abs_cwd, root = roots[0], roots[0]
        mode = mode_for_resolved(root)
        preview = preview_for_confirm(name, args)
        if mode != "rw":
            return {
                "action": "deny",
                "reason": f"Shell verboten: Mode {mode or '?'} (braucht r+w)",
                "elevated": False,
                "risk": "",
                "preview": preview,
            }
        action_class, kind, reason = shell_action_class(cmd)
        if kind == "deny" or action_class == "deny":
            return {
                "action": "deny",
                "reason": f"Shell abgelehnt: {reason}",
                "elevated": False,
                "risk": "",
                "preview": preview,
            }
        if kind == "elevated" or action_class in code_grants.ALWAYS_ONCE:
            risk = reason or action_class
            extra = _grant_fields(
                root, _rel_display(abs_cwd, root), action_class, preview
            )
            return {
                "action": "requires_grant" if kind != "elevated" else "confirm",
                "reason": risk,
                "elevated": kind == "elevated",
                "risk": risk,
                "preview": f"⚠ {risk}\n\n{preview}" if kind == "elevated" else preview,
                "action_class": action_class,
                "workspace_root": extra["workspace_root"],
                "rel_path": extra["rel_path"],
                "outside_task": extra["outside_task"],
                "hint": extra["hint"],
            }
        if action_class in ("read", "test"):
            return {
                "action": "allow",
                "reason": "Lesen/Verifikation unter r+w",
                "action_class": action_class,
                "elevated": False,
                "risk": "",
                "preview": preview,
            }
        rel = _rel_display(abs_cwd, root)
        hit = code_grants.matching(root, rel, action_class)
        if hit:
            why = code_grants.why_allowed(hit)
            return {
                "action": "allow",
                "reason": why,
                "elevated": False,
                "risk": "",
                "preview": preview,
                "grant_id": hit.get("grant_id"),
                "allowed_by": why,
            }
        extra = _grant_fields(root, rel, action_class, preview)
        return {
            "action": "requires_grant",
            "reason": extra["hint"] or f"Freigabe für {action_class}",
            **extra,
        }

    # --- ^_tick: Job-Ordner + Plan & Aktivität ---
    # Eigener Ordnerzustand (Log, Verschieben, Lesen) läuft ohne Popup — er ist
    # auf ROUTINES_ROOT begrenzt und append-only bzw. kollisionssicher.
    if name in (
        "RoutineJobs",
        "RoutineRead",
        "RoutineLog",
        "RoutineMove",
        "RoutinePlan",
        "RoutineOverview",
    ):
        return {
            "action": "allow",
            "reason": "Routine-Ordner (^_tick)",
            "elevated": False,
            "risk": "",
            "preview": preview_for_confirm(name, args),
        }
    # Neue Jobs, Plan-Änderungen und Läufe ändern die Auftragslage → Freigabe.
    if name in ("RoutineJobWrite", "RoutineSet", "RoutineRun"):
        return {
            "action": "requires_grant",
            "reason": "^_tick ändert Aufträge/Plan — Freigabe nötig",
            "elevated": False,
            "risk": "",
            "preview": preview_for_confirm(name, args),
            "hint": "Freigabe für Routine-Änderung",
            "grant_scopes": ["once", "auftrag", "task"],
            "suggested": {},
        }

    # Unknown write-ish: confirm
    if name:
        return {
            "action": "confirm",
            "reason": "unbekanntes Tool",
            "elevated": False,
            "risk": "",
            "preview": preview_for_confirm(name, args),
        }
    return {
        "action": "deny",
        "reason": "leeres Tool",
        "elevated": False,
        "risk": "",
        "preview": "",
    }


def preview_for_confirm(tool_name, args):
    """Kurzer Preview-Text für Glyph-Genehmigungsdialog."""
    args = args or {}
    if tool_name == "WriteFile":
        try:
            prop = propose_write(args.get("path"), args.get("content", ""))
            diff = (prop.get("diff") or "")[:2500]
            return f"WriteFile → {prop.get('path')}\n{diff or '(neue Datei / kein Diff)'}"
        except Exception as e:
            return f"WriteFile → {args.get('path')}: {e}"
    if tool_name == "SearchReplace":
        old = str(args.get("old") or "")
        new = str(args.get("new") or "")
        path = args.get("path")
        preview_old = old if len(old) <= 400 else old[:400] + "…"
        preview_new = new if len(new) <= 400 else new[:400] + "…"
        return (
            f"SearchReplace → {path}\n"
            f"--- old ---\n{preview_old}\n"
            f"+++ new ---\n{preview_new}"
        )
    if tool_name == "RunCommand":
        kind, risk = shell_classify(args.get("command") or "")
        base = f"RunCommand → {args.get('command')}\ncwd={args.get('cwd') or '.'}"
        if kind == "elevated" and risk:
            return f"⚠ {risk}\n{base}"
        return base
    if tool_name == "VaultWrite":
        path = args.get("path") or ""
        content = str(args.get("content") or "")
        prev = content if len(content) <= 400 else content[:400] + "…"
        return f"VaultWrite → {path}\n{prev}"
    if tool_name == "RoutineJobWrite":
        body = str(args.get("body") or args.get("content") or "")
        prev = body if len(body) <= 500 else body[:500] + "…"
        return f"Neuer Job → {args.get('box')}/{args.get('name')}.md\n{prev}"
    if tool_name == "RoutineMove":
        extra = f"\nFrage: {args.get('question')}" if args.get("question") else ""
        return f"Job verschieben → {args.get('to')}/: {args.get('name')}{extra}"
    if tool_name == "RoutineLog":
        lines = args.get("lines")
        text = "\n".join(str(x) for x in lines) if isinstance(lines, (list, tuple)) else str(lines or "")
        prev = text if len(text) <= 500 else text[:500] + "…"
        return f"Laufprotokoll (append)\n{prev}"
    if tool_name == "RoutineSet":
        if args.get("id"):
            return f"To-do ändern: {args.get('id')}\n{args}"
        return (
            f"Neues To-do: {args.get('title')}\n"
            f"Zeitplan: {args.get('schedule_kind') or 'daily'} {args.get('schedule_time') or ''}\n"
            f"Fertig: {args.get('pass')}"
        )
    if tool_name == "RoutineRun":
        return f"To-do jetzt auslösen: {args.get('id')}"
    return f"{tool_name}: {args}"


# --- Stufe 1+2: Wissen & Reichweite (read-only, Graph-gesteuert) ---


def vault_heads_enabled() -> bool:
    """True, wenn mindestens ein Vault für ^_Code freigeschaltet ist
    (heads.code in vaults.json ≠ unbound — Graph-Schalter)."""
    try:
        from . import vaults_registry as vr

        vr.load_store()
        for v in vr.list_vaults() or []:
            heads = (v or {}).get("heads") or {}
            if str(heads.get("code") or "unbound").strip() not in ("", "unbound"):
                return True
    except Exception:
        pass
    return False


_NO_VAULT = (
    "Kein Vault für ^_Code freigeschaltet — im Graph (Anbindung) Kopf ^_Code am "
    "Vault auf Lesen/Schreiben stellen."
)


def recall_as_of(query, as_of, top_k=8):
    """
    Zeitpunkt-Recall: semantische Suche im Index-Stand eines Datums.
    Rein lokal (bge-m3), read-only. Beantwortet "was wussten wir am ...?".
    """
    if not vault_heads_enabled():
        return {"ok": False, "error": _NO_VAULT}
    try:
        from . import pdf_index, retrieval

        q = str(query or "").strip()
        if not q:
            return {"ok": False, "error": "Query fehlt."}
        if not as_of:
            return {"ok": False, "error": "as_of fehlt (YYYY-MM-DD)."}
        emb = retrieval.embed_text(q)
        if not emb:
            return {"ok": False, "error": "Embedding fehlgeschlagen (Ollama/bge-m3 erreichbar?)."}
        k = max(1, int(top_k or 8))
        hits = pdf_index.search_as_of(emb, as_of, candidate_k=k * 4)
        return {"ok": True, "as_of": str(as_of), "query": q, "hits": hits[:k]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def recall_changed_since(since, limit=50):
    """
    Differenz-Recall: was ist seit einem Datum neu, geaendert oder entfernt.
    Rein lokal, read-only. Beantwortet "was ist neu seit ...?".
    """
    if not vault_heads_enabled():
        return {"ok": False, "error": _NO_VAULT}
    try:
        from . import pdf_index

        if not since:
            return {"ok": False, "error": "since fehlt (YYYY-MM-DD)."}
        res = pdf_index.changes_since(since, limit=max(1, int(limit or 50)))
        return {"ok": True, **res}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def vault_find(query, top_k=8):
    """Vault-Suche (Hybrid) über die freigeschalteten Vaults. Read-only."""
    if not vault_heads_enabled():
        return {
            "ok": False,
            "error": "Kein Vault für ^_Code freigeschaltet — im Graph (Anbindung) Kopf ^_Code am Vault auf Lesen/Schreiben stellen.",
        }
    try:
        from . import retrieval

        res = retrieval.vault_find(str(query or ""), top_k=int(top_k or 8))
        if isinstance(res, dict):
            hits = res.get("results") or res.get("selected") or res.get("candidates") or []
            return {"ok": True, "hits": hits, "meta": {k: res[k] for k in ("status", "query", "mode") if k in res}}
        return {"ok": True, "hits": res or []}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def read_note(path):
    """Liest eine Vault-Notiz (relativer Pfad im Vault). Read-only."""
    if not vault_heads_enabled():
        return {"ok": False, "error": "Kein Vault für ^_Code freigeschaltet (Graph)."}
    try:
        from . import config as _cfg
        from . import vault_tools

        _cfg.reload_vault_paths()
        res = vault_tools.read_note(str(path or ""), head="code")
        return {"ok": True, "path": res.get("path"), "content": (res.get("content") or "")[:8000]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def wiki_get(lookup):
    """Liest eine Wiki-Seite (memory-wiki) per lookup. Read-only."""
    if not vault_heads_enabled():
        return {"ok": False, "error": "Kein Vault für ^_Code freigeschaltet (Graph)."}
    try:
        from . import config as _cfg
        from . import vault_tools

        _cfg.reload_vault_paths()
        res = vault_tools.read_note(str(lookup or ""), head="code")
        return {"ok": True, "path": res.get("path"), "content": (res.get("content") or "")[:8000]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def web_search(query, count=5):
    """Web-Suche (Exa + TinyFish). Read-only."""
    try:
        from . import web

        rows = web.web_search(str(query or ""), count=int(count or 5))
        return {"ok": True, "results": rows or []}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def extract_url(url, goal=None):
    """Holt eine URL (TinyFish) und extrahiert Text. Read-only."""
    try:
        from . import web

        text = web.extract_tinyfish(str(url or ""), goal or "Extrahiere relevante Fakten")
        return {"ok": True, "content": (text or "")[:8000]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def mail_list(folder="INBOX", query="", limit=20):
    """Listet E-Mails (himalaya). Read-only."""
    try:
        from . import comm_tools

        res = comm_tools.mail_list(
            folder=str(folder or "INBOX"),
            query=str(query or ""),
            limit=int(limit or 20),
        )
        return res
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def mail_read(msg_id, folder="INBOX"):
    """Liest eine E-Mail (himalaya). Read-only."""
    try:
        from . import comm_tools

        res = comm_tools.mail_read(str(msg_id or ""), folder=str(folder or "INBOX"))
        return res
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def read_memory(scope="recent"):
    """Liest OpenClaw-Memory (MEMORY.md + Tageslogs) für Session-Kontext.
    scope: recent (letzte 3 Tage) | all | MEMORY.md. Read-only."""
    try:
        from . import memory_tools

        return memory_tools.read_memory(str(scope or "recent"))
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def _vault_write_enabled() -> bool:
    """True, wenn mindestens ein Vault für ^_Code auf rw geschaltet ist."""
    try:
        from . import vaults_registry as vr

        vr.load_store()
        for v in vr.list_vaults() or []:
            heads = (v or {}).get("heads") or {}
            if str(heads.get("code") or "unbound").strip() == "rw":
                return True
    except Exception:
        pass
    return False


def vault_write(path, content):
    """Schreibt eine Vault-Notiz (create neu / apply_edit mit Backup).
    Nur wenn ein Vault im Graph auf rw für ^_Code geschaltet ist.
    Erfordert Grant (Einmal/Auftrag/Task) — wie WriteFile."""
    if not _vault_write_enabled():
        return {
            "ok": False,
            "error": "Kein Vault für ^_Code auf Schreiben (rw) geschaltet — im Graph Kopf ^_Code am Vault auf Schreiben stellen.",
        }
    try:
        from . import config as _cfg
        from . import vault_tools

        _cfg.reload_vault_paths()
        p = str(path or "").strip()
        c = str(content or "")
        # Existiert die Notiz?
        exists = True
        try:
            vault_tools.read_note(p, head="code")
        except Exception:
            exists = False
        if exists:
            res = vault_tools.apply_edit(p, c)
            return {"ok": True, "path": res.get("path"), "action": "edit", "chars": res.get("new_chars")}
        res = vault_tools.create_note(p, c)
        return {"ok": True, "path": res.get("path"), "action": "create", "created": res.get("created")}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


def task_finish(task_id, content, summary=""):
    """Schließt eine Glyph-Aufgabe: Artefakt auf Disk + status=done.

    Kein Workspace-Root. SoT ~/.glyph/tasks.json + ~/.glyph/tasks/<id>.md.
    """
    from . import tasks as _tasks

    tid = str(task_id or "").strip()
    if not tid:
        return {"ok": False, "error": "TaskFinish braucht id (Zeile Id: in der Meldung)."}
    try:
        item = _tasks.finish_item(tid, content=content, summary=summary, by="_code")
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except OSError as e:
        return {"ok": False, "error": f"Artefakt nicht schreibbar: {e}"}
    return {
        "ok": True,
        "id": item["id"],
        "status": item["status"],
        "artifact": item.get("artifact") or "",
        "summary": item.get("summary") or "",
    }

