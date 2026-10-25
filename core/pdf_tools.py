# -*- coding: utf-8 -*-
"""
PDF-Lesen für den Agenten-Modus (MODE=agent).

Kette pro SEITE (nicht pro Dokument):
  Datei → pdftotext (Textlayer) → Seiten-Gate → OCR nur für Scan-Seiten →
  gemergter Text → Chunking/Embedding (build_pdf_index/retrieval).

Warum pro Seite: gemischte PDFs (Textlayer + eingescannte Anlagen) hatten vorher
genau die Scan-Seiten verschluckt — das Dokument galt als „hat Textlayer", die
OCR wurde nie gestartet, die Seiten fehlten im Index.

Nur Vault-Pfade (wie vault_tools), via `pdftotext` CLI (poppler).
Graceful degrade wenn CLI fehlt. Kein Shell-Arbitrary, fester argv.

Zwei OCR-Backends (Auswahl: ocr_backend() / Env OCR_BACKEND=auto|vision|lighton|off):
  - "lighton": LightOnOCR-3 über lokalen OpenAI-kompatiblen Endpunkt
               (Env LIGHTON_OCR_URL, z.B. http://127.0.0.1:8790/v1;
               LIGHTON_OCR_MODEL, Default "lightonocr-3-0.8b-mlx").
               Liefert sauberes Markdown inkl. Tabellenstruktur — besser für
               formular-/tabellenlastige HSEQ-Dokumente.
  - "vision":  macOS Vision-Helfer `scripts/ocr_pdf.swift` → `~/.glyph/bin/ocr_pdf`
               (bauen: `sh scripts/build-ocr.sh`). Immer lokal verfügbar,
               reiner Text ohne Layout.
Auto: lighton wenn der Endpunkt offen ist, sonst vision.
Ohne Backend bleibt der Textlayer stehen; read_pdf meldet ehrlich `ok: False`,
statt ein leeres Dokument als Erfolg auszugeben.
"""
from __future__ import annotations

import base64
import glob
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request

from . import config, log, vault_tools

_MAX_PDF_CHARS = 40_000
_DEFAULT_TIMEOUT = 30
_OCR_MIN_CHARS = 40          # pro Seite: darunter kein echter Textlayer → OCR
_PAGE_OCR_TIMEOUT = 45       # Vision-OCR pro Seite
_LIGHTON_TIMEOUT = int(os.environ.get("LIGHTON_OCR_TIMEOUT", "180"))  # MLX, eine Seite
_PAGE_OCR_SCALE = "2.0"
_OCR_MAX_PAGES = int(os.environ.get("OCR_MAX_PAGES", "40"))
_LIGHTON_PROBE_TTL = 300     # Sekunden bis der Endpunkt erneut geprobt wird
_LIGHTON_DEFAULT_URL = "http://127.0.0.1:8790/v1"
_LIGHTON_LOCK = threading.Lock()  # Client: eine Anfrage, MLX auf dem Server ist seriell

_LIGHTON_PROMPT = (
    "Transkribiere diese Dokumentenseite vollständig und exakt als sauberes "
    "Markdown. Tabellen als Markdown-Tabellen, Aufzählungen als Listen. "
    "Gib nur den Seitentext aus, keine Kommentare und keine Einleitung."
)

_LIGHTON_STATE = {"ok": None, "ts": 0.0}


def _pdftotext_bin():
    for cand in (
        os.environ.get("PDFTOTEXT_BIN"),
        "/opt/homebrew/bin/pdftotext",
        "/usr/local/bin/pdftotext",
        shutil.which("pdftotext"),
    ):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _pdftoppm_bin():
    for cand in (
        os.environ.get("PDFTOPPM_BIN"),
        "/opt/homebrew/bin/pdftoppm",
        "/usr/local/bin/pdftoppm",
        shutil.which("pdftoppm"),
    ):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _ocr_bin():
    """Vision-OCR-Helfer (macOS, PDFKit + Vision, kein Fremdpaket)."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for cand in (
        os.environ.get("OCR_PDF_BIN"),
        os.path.expanduser("~/.glyph/bin/ocr_pdf"),
        os.path.join(repo, "scripts", "ocr_pdf"),
    ):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _has_text(text):
    """Echter Textlayer? Whitespace zählt nicht."""
    return len("".join((text or "").split())) >= _OCR_MIN_CHARS


# --- OCR-Backends -----------------------------------------------------------

def _lighton_base():
    return (os.environ.get("LIGHTON_OCR_URL") or _LIGHTON_DEFAULT_URL).strip().rstrip("/")


def _lighton_model():
    return (os.environ.get("LIGHTON_OCR_MODEL") or "lightonocr-3-0.8b-mlx").strip()


def _lighton_ready():
    """LightOnOCR-Endpunkt erreichbar? Kurzer Probe-Lookup, gecacht."""
    base = _lighton_base()
    if not base:
        return False
    now = time.monotonic()
    if _LIGHTON_STATE["ok"] is not None and now - _LIGHTON_STATE["ts"] < _LIGHTON_PROBE_TTL:
        return bool(_LIGHTON_STATE["ok"])
    ok = False
    try:
        req = urllib.request.Request(f"{base}/models", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            ok = 200 <= getattr(resp, "status", 200) < 500
    except Exception:
        ok = False
    _LIGHTON_STATE.update(ok=ok, ts=now)
    return ok


def ocr_backend(preferred=None):
    """Welche OCR-Stufe greift: 'lighton' | 'vision' | None (keine OCR möglich)."""
    want = (preferred or os.environ.get("OCR_BACKEND") or "auto").strip().lower()
    if want in ("off", "none", ""):
        return None
    if want == "vision":
        return "vision" if _ocr_bin() else None
    if want == "lighton":
        return "lighton" if _lighton_ready() else None
    if _lighton_ready():
        return "lighton"
    return "vision" if _ocr_bin() else None


def _parse_ocr_output(raw, page_no):
    """Tolerant: '--- Seite N ---'-Abschnitte (ocr_pdf), sonst Roh-Output."""
    text = (raw or "").strip()
    if not text:
        return ""
    if "--- Seite " not in text:
        return text
    marker = f"--- Seite {page_no} ---"
    if marker in text:
        part = text.split(marker, 1)[1]
        nxt = part.find("--- Seite ")
        return (part[:nxt] if nxt >= 0 else part).strip()
    # anderer Marker (anders nummeriert / Fake): erster Abschnitt ohne Kopfzeile
    sections = text.split("--- Seite ")
    for sec in sections[1:]:
        body = sec.split("\n", 1)
        if len(body) == 2 and body[1].strip():
            return body[1].strip()
    return text


def _ocr_page_vision(abs_path, page_no):
    """Eine Seite über macOS Vision. Liefert (text, error)."""
    bin_path = _ocr_bin()
    if not bin_path:
        return "", "Vision-OCR-Helfer nicht installiert"
    try:
        proc = subprocess.run(
            [bin_path, abs_path, "0", _PAGE_OCR_SCALE, str(page_no)],
            capture_output=True,
            text=True,
            timeout=_PAGE_OCR_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "", f"OCR-Timeout nach {_PAGE_OCR_TIMEOUT}s"
    except OSError as e:
        return "", str(e)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or f"exit {proc.returncode}"
        return "", err[:300]
    return _parse_ocr_output(proc.stdout, page_no), None


def _render_page_png(abs_path, page_no):
    """Eine PDF-Seite als PNG (poppler pdftoppm). Liefert Bytes oder None."""
    bin_path = _pdftoppm_bin()
    if not bin_path:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="glyph-ocr-page-") as td:
            prefix = os.path.join(td, "page")
            proc = subprocess.run(
                [bin_path, "-png", "-r", "150", "-f", str(page_no), "-l", str(page_no),
                 abs_path, prefix],
                capture_output=True,
                timeout=_PAGE_OCR_TIMEOUT,
                check=False,
            )
            if proc.returncode != 0:
                return None
            hits = sorted(glob.glob(prefix + "*.png"))
            if not hits:
                return None
            with open(hits[0], "rb") as fh:
                return fh.read()
    except Exception:
        return None


def _lighton_chat(png_bytes):
    """PNG → LightOnOCR. Client-Lock, damit der 45s-Timeout nicht in der Queue stirbt."""
    payload = {
        "model": _lighton_model(),
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _LIGHTON_PROMPT},
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64,"
                           + base64.b64encode(png_bytes).decode("ascii"),
                }},
            ],
        }],
    }
    req = urllib.request.Request(
        f"{_lighton_base()}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _LIGHTON_LOCK:
        try:
            with urllib.request.urlopen(req, timeout=_LIGHTON_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return "", f"lighton fetch fehlgeschlagen: {e}"[:300]
    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:
        return "", "lighton Antwort ohne content"
    if isinstance(content, list):
        content = "\n".join(
            str(p.get("text") or "") for p in content if isinstance(p, dict)
        )
    return str(content or "").strip(), None


def _ocr_page_lighton(abs_path, page_no):
    """Eine Seite über LightOnOCR (OpenAI-kompatibler lokaler Endpunkt)."""
    png = _render_page_png(abs_path, page_no)
    if not png:
        return "", "Seite konnte nicht gerendert werden (pdftoppm fehlt?)"
    return _lighton_chat(png)


def _ocr_page(backend, abs_path, page_no):
    """(text, error, used_backend). LightOn leer/Fehler → Vision."""
    if backend == "lighton":
        text, err = _ocr_page_lighton(abs_path, page_no)
        if (text or "").strip():
            return text, None, "lighton"
        vtext, verr = _ocr_page_vision(abs_path, page_no)
        if (vtext or "").strip():
            log.log("pdf_ocr_lighton_fallback_vision", path=abs_path, page=page_no,
                    lighton_err=(err or "leer")[:120])
            return vtext, None, "vision"
        return "", err or verr, "lighton"
    text, err = _ocr_page_vision(abs_path, page_no)
    return text, err, "vision"


# --- Seitenweise Extraktion -------------------------------------------------

def extract_pdf_pages(abs_path, ocr=None, backend=None):
    """
    Seitenweise Extraktion mit OCR-Gate pro Seite.

    ocr=None (auto) | ocr=False (nie OCR).
    Liefert (pages, info):
      pages: [{page, text, engine}]  — engine "pdftotext" | "<backend>-ocr"
      info:  {engine, pages, ocr_pages, error}
    """
    pages, err = pdf_pages_text(abs_path)
    if err:
        return [], {"engine": "pdftotext", "pages": 0, "ocr_pages": 0, "error": err}
    scan = [p for p in pages if not _has_text(p["text"])]
    if not scan:
        for p in pages:
            p["engine"] = "pdftotext"
        return pages, {"engine": "pdftotext", "pages": len(pages), "ocr_pages": 0,
                       "error": None}
    if ocr is False:
        return pages, {"engine": "pdftotext", "pages": len(pages), "ocr_pages": 0,
                       "error": f"{len(scan)} Seite(n) ohne Textlayer (OCR aus)"}
    be = ocr_backend(backend)
    if not be:
        return pages, {"engine": "pdftotext", "pages": len(pages), "ocr_pages": 0,
                       "error": f"{len(scan)} Seite(n) ohne Textlayer — OCR nicht verfügbar"}
    for p in pages:
        p.setdefault("engine", "pdftotext")
    ocr_done = 0
    used = set()
    for p in scan[:_OCR_MAX_PAGES]:
        text, oerr, used_be = _ocr_page(be, abs_path, p["page"])
        if text.strip():
            p["text"] = text
            p["engine"] = f"{used_be}-ocr"
            used.add(used_be)
            ocr_done += 1
        else:
            p["engine"] = "pdftotext"
            p["ocr_error"] = oerr
    if len(scan) > _OCR_MAX_PAGES:
        log.log("pdf_ocr_page_cap", path=abs_path, scan=len(scan), cap=_OCR_MAX_PAGES)
    text_pages = sum(1 for p in pages if p["text"].strip())
    ocr_tag = "+".join(f"{b}-ocr" for b in ("lighton", "vision") if b in used) or f"{be}-ocr"
    if ocr_done and text_pages == ocr_done:
        engine = ocr_tag
    elif ocr_done:
        engine = f"pdftotext+{ocr_tag}"
    else:
        engine = "pdftotext"
    info = {"engine": engine, "pages": len(pages), "ocr_pages": ocr_done, "error": None}
    if not any(p["text"].strip() for p in pages):
        fail = next((p.get("ocr_error") for p in pages if p.get("ocr_error")), "leer")
        info["engine"] = f"{be}-ocr"
        info["error"] = f"OCR lieferte keinen Text ({fail})"
    return pages, info


def read_pdf(path, max_chars=None, ocr=None, backend=None):
    """
    Extrahiert Text aus Vault-PDF: pdftotext pro Seite, Scan-Seiten per OCR.
    ocr=None (auto) | ocr=False (OCR aus).
    Liefert {path, content, chars, truncated, engine, ocr, pages, ocr_pages, error}
    oder graceful error-dict. `chars` ist IMMER die volle Extraktionslänge,
    `truncated` zeigt die Kappung für den Tool-Kontext an.
    """
    resolved, rel = _resolve_pdf_path(path)
    cap = int(max_chars or _MAX_PDF_CHARS)
    cap = max(500, min(cap, 2_000_000))

    if not _pdftotext_bin():
        log.log("read_pdf_skipped", path=rel, reason="pdftotext_missing")
        return {
            "ok": False,
            "path": rel,
            "content": "",
            "chars": 0,
            "truncated": False,
            "engine": None,
            "ocr": False,
            "pages": 0,
            "ocr_pages": 0,
            "error": "PDF-Text nicht lesbar (Extraktion nicht verfügbar).",
        }

    pages, info = extract_pdf_pages(resolved, ocr=ocr, backend=backend)
    full = "\n\n".join(p["text"].strip() for p in pages if p["text"].strip())
    chars = len(full)

    if not full:
        detail = info.get("error") or ""
        if "OCR lieferte keinen Text" in detail:
            err = f"PDF ist ein Scan; {detail}"
        elif "OCR nicht verfügbar" in detail:
            err = "PDF enthält keinen Textlayer (vermutlich Scan) — OCR nicht verfügbar."
        elif "OCR aus" in detail:
            err = "PDF enthält keinen Textlayer (vermutlich Scan)."
        elif detail:
            err = detail
        else:
            err = "PDF enthält keinen Textlayer (vermutlich Scan)."
        log.log("read_pdf_no_textlayer", path=rel, engine=info.get("engine"))
        return {
            "ok": False,
            "path": rel,
            "content": "",
            "chars": 0,
            "truncated": False,
            "engine": info.get("engine"),
            "ocr": bool(info.get("ocr_pages")),
            "pages": info.get("pages") or 0,
            "ocr_pages": info.get("ocr_pages") or 0,
            "error": err,
        }

    truncated = chars > cap
    text = full[:cap]
    engine = info.get("engine")
    log.log("read_pdf", path=rel, chars=chars, truncated=truncated, engine=engine,
            ocr_pages=info.get("ocr_pages") or 0)
    return {
        "ok": True,
        "path": rel,
        "content": text,
        "chars": chars,
        "truncated": truncated,
        "engine": engine,
        "ocr": bool(info.get("ocr_pages")),
        "pages": info.get("pages") or 0,
        "ocr_pages": info.get("ocr_pages") or 0,
        "error": None,
    }


def _resolve_pdf_path(path):
    """Vault-Pfad zu PDF auflösen; BLOCKED_DIRS greifen."""
    if not path or not str(path).strip():
        raise ValueError("ReadPdf: path fehlt")
    raw = str(path).strip()
    resolved = vault_tools._resolve_vault_path(raw)
    if not resolved:
        raise ValueError(vault_tools._outside_vault_error(raw))

    rel = vault_tools._rel_to_root(resolved)
    if vault_tools._is_blocked(rel):
        raise PermissionError(f"Geschützter Ordner — PDF-Zugriff verweigert: {rel}")
    if vault_tools._is_private(resolved):
        raise PermissionError(f"Privat-Vault — PDF-Zugriff gesperrt (Schloss): {rel}")
    if not resolved.lower().endswith(".pdf"):
        raise ValueError(f"Kein PDF-Pfad: {path}")
    if not os.path.isfile(resolved):
        raise FileNotFoundError(f"PDF nicht gefunden: {path}")
    return resolved, rel


def pdf_pages_text(abs_path):
    """Seitenweise pdftotext (Form-Feed). Liefert (pages, error).

    pages: [{page, text}, ...] — kein OCR, nur Textlayer. Für die UI-Suche.
    """
    bin_path = _pdftotext_bin()
    if not bin_path:
        return [], "PDF-Text nicht lesbar (Extraktion nicht verfügbar)."
    try:
        proc = subprocess.run(
            [bin_path, "-layout", "-enc", "UTF-8", abs_path, "-"],
            capture_output=True,
            text=True,
            timeout=_DEFAULT_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return [], f"pdftotext Timeout nach {_DEFAULT_TIMEOUT}s"
    except OSError as e:
        return [], str(e)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or f"pdftotext exit {proc.returncode}"
        return [], err[:200]
    parts = (proc.stdout or "").split("\f")
    pages = []
    for i, part in enumerate(parts, 1):
        text = part.strip()
        if not text and i == len(parts):
            continue
        pages.append({"page": i, "text": text})
    if not pages:
        pages = [{"page": 1, "text": ""}]
    return pages, None
