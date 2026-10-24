# -*- coding: utf-8 -*-
"""
Office-Text-Extraktion ohne Fremdpakete: docx und xlsx sind ZIP+XML.

docx: word/document.xml — Absätze <w:t>, Tabellen zeilenweise mit " | ".
xlsx: xl/sharedStrings.xml + xl/worksheets/sheet*.xml — Zellen zeilenweise,
      Zahlen und Formeln als Werte (inlineStr/shared/num).
Kette: Datei → Text → Chunking → Embedding (build_pdf_index --office).

Warum stdlib: Der glyph-agent-Lauf soll kein pip-Abhängigkeitsrisiko bekommen;
python-docx/openpyxl bräuchten Wartung im Requirements. Die XML-Formen der
beiden Formate sind stabil.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile

_XLSX_MAX_ROWS = 20000
_DOCX_MAX_CHARS = 2_000_000

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _xml_texts(xml_bytes, tag_ns):
    """Alle <tag>text</tag>-Inhalte eines Namensraums, in Dokumentordnung."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    return [el for el in root.iter(f"{tag_ns}t") if el.text]


def _is_ole(path):
    """Alte Word-Binärdatei (.doc, auch falsch als .docx benannt)? OLE-Magic."""
    try:
        with open(path, "rb") as fh:
            return fh.read(8) == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    except OSError:
        return False


def read_doc_binary(abs_path):
    """Altes .doc (OLE) über macOS textutil → Text. Liefert (text, error)."""
    import subprocess
    try:
        proc = subprocess.run(
            ["textutil", "-stdout", "-convert", "txt", str(abs_path)],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except FileNotFoundError:
        return "", "textutil nicht verfügbar (.doc-Binärdatei)"
    except subprocess.TimeoutExpired:
        return "", "textutil Timeout nach 60s"
    if proc.returncode != 0:
        return "", (proc.stderr or "").strip()[:200] or f"textutil exit {proc.returncode}"
    return (proc.stdout or "").strip(), None


def read_docx(abs_path):
    """docx → Text: Absätze als Zeilen, Tabellen als ' | '-Zeilen.

    Falsch benannte .doc-Binärdateien (OLE-Magic) laufen über textutil.
    """
    if _is_ole(abs_path):
        text, err = read_doc_binary(abs_path)
        if err and not text:
            return "", err + " (Datei ist altes Word-Binärformat, nicht OOXML)"
        return text, None
    out = []
    with zipfile.ZipFile(abs_path) as zf:
        names = set(zf.namelist())
        # Word-Reparatur-Artefakt: Hauptteil als document2.xml statt document.xml
        main = "word/document.xml"
        if main not in names:
            alts = sorted(n for n in names
                          if re.fullmatch(r"word/document\d*\.xml", n))
            if not alts:
                return "", "docx ohne word/document.xml"
            main = alts[0]
        xml = zf.read(main)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        return "", f"docx nicht lesbar: {e}"
    body = root.find(f"{_W_NS}body")
    if body is None:
        return "", "docx ohne body"

    def _para_text(p):
        return "".join(t.text or "" for t in p.iter(f"{_W_NS}t")).strip()

    for el in body:  # direkte Kinder: <w:p> und <w:tbl> — keine Doppelung aus Tabellenzellen
        if el.tag == f"{_W_NS}p":
            txt = _para_text(el)
            if txt:
                out.append(txt)
        elif el.tag == f"{_W_NS}tbl":
            for tr in el.findall(f"{_W_NS}tr"):
                cells = []
                for tc in tr.findall(f"{_W_NS}tc"):
                    cells.append(" ".join(
                        _para_text(p) for p in tc.findall(f"{_W_NS}p")
                    ).strip())
                if any(cells):
                    out.append(" | ".join(cells))
    text = "\n".join(out)[:_DOCX_MAX_CHARS]
    return text, None


def _col_letters(ref):
    """'AB12' → 'AB' (Spaltenbuchstaben der Zellreferenz)."""
    return re.match(r"([A-Z]+)", ref or "").group(1) if ref and re.match(r"([A-Z]+)", ref or "") else ""


def read_xlsx(abs_path):
    """xlsx → Text: je Blatt Zeilen als ' | '-Zeilen (Header-Zeile zuerst)."""
    sheets = {}
    shared = []
    try:
        with zipfile.ZipFile(abs_path) as zf:
            try:
                sroot = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                for si in sroot.iter(f"{_S_NS}si"):
                    shared.append("".join(t.text or "" for t in si.iter(f"{_S_NS}t")))
            except (KeyError, ET.ParseError):
                shared = []
            names = [
                n for n in zf.namelist()
                if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)
            ]
            for name in sorted(names):
                try:
                    root = ET.fromstring(zf.read(name))
                except ET.ParseError:
                    continue
                rows = []
                for row in root.iter(f"{_S_NS}row"):
                    cells = {}
                    for c in row.findall(f"{_S_NS}c"):
                        ref = c.get("r") or ""
                        col = _col_letters(ref)
                        t = c.get("t")
                        val = ""
                        v = c.find(f"{_S_NS}v")
                        if t == "s" and v is not None and v.text:
                            try:
                                val = shared[int(v.text)]
                            except (ValueError, IndexError):
                                val = ""
                        elif t == "inlineStr":
                            val = "".join(
                                x.text or "" for x in c.iter(f"{_S_NS}t")
                            )
                        elif v is not None and v.text:
                            val = v.text
                        if val:
                            cells[col] = val
                    if cells:
                        ordered = [cells[k] for k in sorted(cells)]
                        rows.append(" | ".join(ordered))
                if rows:
                    sheet_title = name.rsplit("/", 1)[-1].replace(".xml", "")
                    sheets[sheet_title] = rows[:_XLSX_MAX_ROWS]
    except (zipfile.BadZipFile, KeyError) as e:
        return "", f"xlsx nicht lesbar: {e}"
    parts = []
    for title, rows in sheets.items():
        parts.append(f"## {title}\n" + "\n".join(rows))
    return "\n\n".join(parts), None


def read_pptx(abs_path):
    """pptx/pptm → Text: je Folie die Text-Runs (a:t), Folienkopf '## Folie N'."""
    try:
        with zipfile.ZipFile(abs_path) as zf:
            names = [n for n in zf.namelist()
                     if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
            parts = []
            for name in sorted(names, key=lambda n: int(re.search(r"\d+", n).group())):
                try:
                    root = ET.fromstring(zf.read(name))
                except ET.ParseError:
                    continue
                num = re.search(r"\d+", name).group()
                lines = []
                for p in root.iter():
                    if not p.tag.endswith("}p"):
                        continue
                    txt = "".join(t.text or "" for t in p.iter()
                                  if t.tag.endswith("}t")).strip()
                    if txt:
                        lines.append(txt)
                if lines:
                    parts.append(f"## Folie {num}\n" + "\n".join(lines))
            return "\n\n".join(parts), None
    except (zipfile.BadZipFile, KeyError) as e:
        return "", f"pptx nicht lesbar: {e}"


def read_office(abs_path):
    """docx/xlsx/pptx/pptm → (text, error). Andere Endungen → ('', Fehler)."""
    low = str(abs_path).lower()
    if low.endswith(".doc") and not low.endswith(".docx"):
        return read_docx(abs_path)  # OLE → textutil
    if low.endswith(".docx"):
        return read_docx(abs_path)
    if low.endswith(".xlsx"):
        return read_xlsx(abs_path)
    if low.endswith((".pptx", ".pptm")):
        return read_pptx(abs_path)
    return "", f"Kein Office-Format: {abs_path}"