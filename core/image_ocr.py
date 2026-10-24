# -*- coding: utf-8 -*-
"""
Bild-OCR-Stufe: JPG/PNG/TIFF/HEIC als eigene Dokumente im Vault-Index.

Vorher waren ~312 Bilder im Vault blind (Handy-Fotos, gescannte Blätter als
Bild). Kette: Bild → (bei Bedarf PNG-Normalisierung via sips) → OCR-Backend →
Text → Chunking → Embedding.

Backends wie bei PDFs (core/pdf_tools.ocr_backend):
  - "lighton": LightOnOCR-3 (Daten-URI direkt an den lokalen Endpunkt)
  - "vision":  macOS sips wandelt das Bild in ein Einseiten-PDF, dann der
               vorhandene Vision-Helfer (~/.glyph/bin/ocr_pdf).
"""
from __future__ import annotations

import os
import subprocess
import tempfile

from . import pdf_tools, vault_tools

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".heif")
_MAX_IMAGE_CHARS = 100_000
_TIMEOUT = 120


def _sips_bin():
    for cand in (
        os.environ.get("SIPS_BIN"),
        "/usr/bin/sips",
        "/opt/homebrew/bin/sips",
    ):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def resolve_image_path(path):
    """Vault-Pfad zu Bild auflösen; BLOCKED_DIRS greifen (wie bei PDFs)."""
    if not path or not str(path).strip():
        raise ValueError("ReadImage: path fehlt")
    raw = str(path).strip()
    resolved = vault_tools._resolve_vault_path(raw)
    if not resolved:
        raise ValueError(vault_tools._outside_vault_error(raw))
    rel = vault_tools._rel_to_root(resolved)
    if vault_tools._is_blocked(rel):
        raise PermissionError(f"Geschützter Ordner — Bild-Zugriff verweigert: {rel}")
    if not resolved.lower().endswith(IMAGE_EXTS):
        raise ValueError(f"Kein Bildformat: {path}")
    if not os.path.isfile(resolved):
        raise FileNotFoundError(f"Bild nicht gefunden: {path}")
    return resolved, rel


def _as_png_bytes(abs_path):
    """Bild → PNG-Bytes (HEIC/TIFF via sips normalisieren)."""
    low = abs_path.lower()
    if low.endswith(".png"):
        with open(abs_path, "rb") as fh:
            return fh.read(), None
    sips = _sips_bin()
    if not sips:
        return None, "sips nicht verfügbar (PNG-Normalisierung fehlgeschlagen)"
    try:
        with tempfile.TemporaryDirectory(prefix="glyph-img-ocr-") as td:
            out = os.path.join(td, "img.png")
            proc = subprocess.run(
                [sips, "-s", "format", "png", abs_path, "--out", out],
                capture_output=True, timeout=_TIMEOUT, check=False,
            )
            if proc.returncode != 0 or not os.path.isfile(out):
                return None, (proc.stderr or b"").decode("utf-8", "replace")[:200]
            with open(out, "rb") as fh:
                return fh.read(), None
    except subprocess.TimeoutExpired:
        return None, f"sips Timeout nach {_TIMEOUT}s"
    except OSError as e:
        return None, str(e)


def _ocr_image_lighton(png_bytes):
    """Bild direkt an den LightOnOCR-Endpunkt (gleicher Lock/Timeout wie PDF-Seiten)."""
    return pdf_tools._lighton_chat(png_bytes)


def _ocr_image_vision(abs_path):
    """Bild → Einseiten-PDF (sips) → vorhandener Vision-Helfer."""
    ocr_bin = pdf_tools._ocr_bin()
    if not ocr_bin:
        return "", "Vision-OCR-Helfer nicht installiert"
    sips = _sips_bin()
    if not sips:
        return "", "sips nicht verfügbar"
    try:
        with tempfile.TemporaryDirectory(prefix="glyph-img-ocr-") as td:
            pdf_path = os.path.join(td, "img.pdf")
            proc = subprocess.run(
                [sips, "-s", "format", "pdf", abs_path, "--out", pdf_path],
                capture_output=True, timeout=_TIMEOUT, check=False,
            )
            if proc.returncode != 0 or not os.path.isfile(pdf_path):
                return "", (proc.stderr or b"").decode("utf-8", "replace")[:200]
            text, err = pdf_tools._ocr_page_vision(pdf_path, 1)
            return text, err
    except subprocess.TimeoutExpired:
        return "", f"sips Timeout nach {_TIMEOUT}s"
    except OSError as e:
        return "", str(e)


def read_image(path, backend=None, ocr=None):
    """
    Bild → Text per OCR. Liefert {ok, path, content, chars, engine, error}.
    """
    resolved, rel = resolve_image_path(path)
    if ocr is False:
        return {"ok": False, "path": rel, "content": "", "chars": 0,
                "engine": None, "error": "OCR aus — Bild hat keine Textebene."}
    be = pdf_tools.ocr_backend(backend)
    if not be:
        return {"ok": False, "path": rel, "content": "", "chars": 0,
                "engine": None, "error": "Kein OCR-Backend verfügbar."}

    if be == "lighton":
        png, err = _as_png_bytes(resolved)
        if not png:
            return {"ok": False, "path": rel, "content": "", "chars": 0,
                    "engine": "lighton-ocr", "error": err}
        text, oerr = _ocr_image_lighton(png)
        engine = "lighton-ocr"
        if not (text or "").strip():
            vtext, verr = _ocr_image_vision(resolved)
            if (vtext or "").strip():
                text, oerr, engine = vtext, None, "vision-ocr"
            else:
                oerr = oerr or verr
    else:
        text, oerr = _ocr_image_vision(resolved)
        engine = "vision-ocr"

    text = (text or "").strip()[:_MAX_IMAGE_CHARS]
    if not text:
        pdf_tools.log.log("image_ocr_empty", path=rel, backend=be,
                          error=(oerr or "leer")[:120])
        return {"ok": False, "path": rel, "content": "", "chars": 0,
                "engine": engine,
                "error": f"Bild-OCR lieferte keinen Text ({oerr or 'leer'})."}
    pdf_tools.log.log("image_ocr", path=rel, chars=len(text), engine=engine)
    return {"ok": True, "path": rel, "content": text, "chars": len(text),
            "engine": engine, "error": None}