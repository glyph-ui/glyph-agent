# -*- coding: utf-8 -*-
"""
PDF-Lesen für den Agenten-Modus (MODE=agent).

Nur Vault-Pfade (wie vault_tools), via `pdftotext` CLI (poppler).
Graceful degrade wenn CLI fehlt. Kein Shell-Arbitrary, fester argv.

Scan-PDFs (ohne Textlayer) werden per Vision-OCR gelesen — Helfer:
`scripts/ocr_pdf.swift` → `~/.glyph/bin/ocr_pdf` (bauen: `sh scripts/build-ocr.sh`).
Ohne Helfer meldet read_pdf ehrlich `ok: False`, statt ein leeres Dokument
als Erfolg auszugeben (das Modell hätte sonst eine Antwort erfunden).
"""
from __future__ import annotations

import os
import shutil
import subprocess

from . import config, log, vault_tools

_MAX_PDF_CHARS = 40_000
_DEFAULT_TIMEOUT = 30
_OCR_MIN_CHARS = 40          # darunter: kein echter Textlayer → OCR versuchen
_OCR_TIMEOUT = 180           # pro PDF (Scan, mehrseitig)
_OCR_MAX_PAGES = 40          # Schutz vor 300-Seiten-Scans


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


def _run_ocr(bin_path, resolved, max_pages=None):
    """OCR über die Seiten eines Scan-PDF. Liefert (text, error)."""
    pages = int(max_pages or _OCR_MAX_PAGES)
    try:
        proc = subprocess.run(
            [bin_path, resolved, str(pages), "2.0"],
            capture_output=True,
            text=True,
            timeout=_OCR_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "", f"OCR-Timeout nach {_OCR_TIMEOUT}s"
    except OSError as e:
        return "", str(e)
    if proc.returncode != 0:
        return "", ((proc.stderr or "").strip() or f"exit {proc.returncode}")[:300]
    return proc.stdout or "", None


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
    if not resolved.lower().endswith(".pdf"):
        raise ValueError(f"Kein PDF-Pfad: {path}")
    if not os.path.isfile(resolved):
        raise FileNotFoundError(f"PDF nicht gefunden: {path}")
    return resolved, rel


def read_pdf(path, max_chars=None, ocr=None):
    """
    Extrahiert Text aus Vault-PDF: erst pdftotext, bei fehlendem Textlayer OCR.
    ocr=None (auto) | ocr=False (OCR aus).
    Liefert {path, content, chars, truncated, engine, ocr} oder graceful error-dict.
    """
    resolved, rel = _resolve_pdf_path(path)
    cap = int(max_chars or _MAX_PDF_CHARS)
    cap = max(500, min(cap, 200_000))

    bin_path = _pdftotext_bin()
    if not bin_path:
        log.log("read_pdf_skipped", path=rel, reason="pdftotext_missing")
        return {
            "ok": False,
            "path": rel,
            "content": "",
            "chars": 0,
            "truncated": False,
            "engine": None,
            "ocr": False,
            "error": "PDF-Text nicht lesbar (Extraktion nicht verfügbar).",
        }

    try:
        proc = subprocess.run(
            [bin_path, "-layout", "-enc", "UTF-8", resolved, "-"],
            capture_output=True,
            text=True,
            timeout=_DEFAULT_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log.log("read_pdf_timeout", path=rel)
        return {
            "ok": False,
            "path": rel,
            "content": "",
            "chars": 0,
            "truncated": False,
            "engine": "pdftotext",
            "ocr": False,
            "error": f"pdftotext Timeout nach {_DEFAULT_TIMEOUT}s",
        }
    except OSError as e:
        return {
            "ok": False,
            "path": rel,
            "content": "",
            "chars": 0,
            "truncated": False,
            "engine": "pdftotext",
            "ocr": False,
            "error": str(e),
        }

    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or f"exit {proc.returncode}"
        log.log("read_pdf_error", path=rel, error=err[:200])
        return {
            "ok": False,
            "path": rel,
            "content": "",
            "chars": 0,
            "truncated": False,
            "engine": "pdftotext",
            "ocr": False,
            "error": err[:500],
        }

    text = proc.stdout or ""
    engine = "pdftotext"
    ocr_used = False

    # Kein Textlayer (Scan) → OCR. Ohne Helfer: ehrlich scheitern statt leer liefern.
    if not _has_text(text):
        if ocr is False:
            log.log("read_pdf_no_textlayer", path=rel, chars=len(text), ocr="off")
            return {
                "ok": False,
                "path": rel,
                "content": "",
                "chars": 0,
                "truncated": False,
                "engine": "pdftotext",
                "ocr": False,
                "error": "PDF enthält keinen Textlayer (vermutlich Scan).",
            }
        bin_ocr = _ocr_bin()
        if not bin_ocr:
            log.log("read_pdf_no_textlayer", path=rel, chars=len(text))
            return {
                "ok": False,
                "path": rel,
                "content": "",
                "chars": 0,
                "truncated": False,
                "engine": "pdftotext",
                "ocr": False,
                "error": "PDF enthält keinen Textlayer (vermutlich Scan) — OCR nicht verfügbar.",
            }
        ocr_text, ocr_err = _run_ocr(bin_ocr, resolved)
        if _has_text(ocr_text):
            text, engine, ocr_used = ocr_text, "vision-ocr", True
        else:
            log.log("read_pdf_ocr_failed", path=rel, error=(ocr_err or "leer")[:200])
            return {
                "ok": False,
                "path": rel,
                "content": "",
                "chars": 0,
                "truncated": False,
                "engine": "vision-ocr",
                "ocr": True,
                "error": f"PDF ist ein Scan; OCR lieferte keinen Text ({ocr_err or 'leer'}).",
            }

    truncated = len(text) > cap
    text = text[:cap]
    log.log("read_pdf", path=rel, chars=len(text), truncated=truncated, engine=engine)
    return {
        "ok": True,
        "path": rel,
        "content": text,
        "chars": len(text),
        "truncated": truncated,
        "engine": engine,
        "ocr": ocr_used,
        "error": None,
    }


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
