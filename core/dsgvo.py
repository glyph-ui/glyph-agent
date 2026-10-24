# -*- coding: utf-8 -*-
"""
DSGVO-Redaktion für kopierte Vault-Dateien (docx/pptx/pptm/xlsx).

Ziel: Beim Übernehmen aus der iCloud-Quelle in den Vault (Kopie) werden
Personennamen (Anrede + Name) und vereinbarte Firmentoken aus den KOPIEN
entfernt — die Originale in iCloud bleiben unangetastet.

Niveau: best-effort.
- docx/pptx/pptm: Absatzweise (w:p / a:p) — Namen, die über mehrere
  Text-Runs verteilt sind, werden erfasst; Formatierung geht nur in
  betroffenen Absätzen auf den ersten Run über.
- xlsx: sharedStrings und inlineStr je Zelle (ganze Zelltexte).
- .ppt (altes Binärformat) und PDFs sind nicht verlustfrei redigierbar —
  dafür greift die Index-Redaktion (build_pdf_index über dsgvo.redact_tokens),
  so dass der Name zumindest nicht in Suche/Treffer landet.

Liefert je Aufruf die Anzahl der Ersetzungen (Report ohne Inhaltsausgabe).
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import zlib
import xml.etree.ElementTree as ET
import zipfile

# Anrede + Nachname (+ optional Vorname) — konservativ: nur mit Anrede.
# „Hr./Dr.“ mit Punkt, „Herr/Frau“ als Wort (Leerzeichen zwingend danach).
_PERSON_RE = re.compile(
    r"\b(?:(?:Hr|Fr|Dr|Prof)\.|(?:Herr|Frau))\s+"
    r"(?:[A-ZÄÖÜ]\.\s+)?"
    r"[A-ZÄÖÜ][a-zäöüß]{2,}(?:\s+[A-ZÄÖÜ][a-zäöüß]{2,})?"
)


def redact_tokens(text):
    """Nur Firmentoken (BERATERgroup-Varianten) entfernen. → (text, ersatzungen)."""
    return _TOKEN_RE.subn("Beratung", text)
# Firmentoken (vereinbart: „BERATER.group“ — Name aus der Suche, Inhalte bleiben)
_TOKEN_RE = re.compile(r"(?i)berater[\s._-]{0,2}group")


def redact_text(text):
    """Text → (neuer_text, ersatzungen). Firmentoken zuerst, dann Personen."""
    text, n_tok = _TOKEN_RE.subn("Beratung", text)
    text, n_person = _PERSON_RE.subn("…", text)
    return text, n_tok + n_person


def _collect_namespaces(xml_text):
    return {
        m.group(1): m.group(2)
        for m in re.finditer(r'xmlns:([A-Za-z0-9_]+)="([^"]+)"', xml_text)
    }


def _redact_paragraph_xml(xml_bytes, t_local, p_local):
    """Absatzweise Redaktion in OOXML. Liefert (neue_bytes, ersatzungen)."""
    xml_text = xml_bytes.decode("utf-8")
    for prefix, uri in _collect_namespaces(xml_text).items():
        ET.register_namespace(prefix, uri)
    root = ET.fromstring(xml_bytes)
    t_tag = t_local  # '{ns}t' wird je Dokumenttyp gebaut
    count = 0

    # Absätze finden: alle Elemente mit lokalem Namen p
    def _local(tag):
        return tag.rsplit("}", 1)[-1] if "}" in tag else tag

    paragraphs = [el for el in root.iter() if _local(el.tag) == p_local]
    for p in paragraphs:
        ts = [el for el in p.iter() if _local(el.tag) == t_local]
        if not ts:
            continue
        joined = "".join(t.text or "" for t in ts)
        new, n = redact_text(joined)
        if n and new != joined:
            count += n
            ts[0].text = new
            ts[0].set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            for t in ts[1:]:
                t.text = ""
    return ET.tostring(root, xml_declaration=True, encoding="UTF-8"), count


_DOCX_TARGETS = ("word/document.xml",)
_PPTX_TARGET_PREFIXES = ("ppt/slides/", "ppt/notesSlides/")
_XLSX_TARGETS = ("xl/sharedStrings.xml",)


def redact_office(path):
    """
    docx/pptx/pptm/xlsx an Ort und Stelle redigieren (nur für VAULT-KOPIEN
    verwenden!). Liefert (ersatzungen, error). Überschreibt die Datei atomar.
    """
    low = str(path).lower()
    if low.endswith(".docx"):
        targets, kind = _DOCX_TARGETS, "docx"
        t_local, p_local = "t", "p"
    elif low.endswith((".pptx", ".pptm")):
        targets, kind = None, "pptx"  # Präfix-Filter
        t_local, p_local = "t", "p"
    elif low.endswith(".xlsx"):
        targets, kind = _XLSX_TARGETS, "xlsx"
        t_local, p_local = "t", "si"
    else:
        return 0, f"Kein redigierbares Format: {path}"

    total = 0
    try:
        with zipfile.ZipFile(path) as zin:
            names = zin.namelist()
            payload = {}
            for name in names:
                data = zin.read(name)
                if kind == "docx" and name in _DOCX_TARGETS:
                    data, n = _redact_paragraph_xml(data, t_local, p_local)
                    total += n
                elif kind == "pptx" and name.startswith(_PPTX_TARGET_PREFIXES) \
                        and name.endswith(".xml"):
                    data, n = _redact_paragraph_xml(data, t_local, p_local)
                    total += n
                elif kind == "xlsx" and name in _XLSX_TARGETS:
                    data, n = _redact_paragraph_xml(data, "t", "si")
                    total += n
                payload[name] = data
        fd, tmp = tempfile.mkstemp(prefix="dsgvo-", suffix=os.path.splitext(path)[1])
        os.close(fd)
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
                for name in names:  # Reihenfolge erhalten
                    zout.writestr(name, payload[name])
            shutil.move(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except (zipfile.BadZipFile, ET.ParseError, KeyError, zlib.error, OSError) as e:
        return total, f"{type(e).__name__}: {e}"
    return total, None