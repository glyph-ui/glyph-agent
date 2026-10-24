"""Chat-Datei: PDF oder Word nur für den Chat. Kein Vault, keine Platte.

Betriebsanweisungen füllen das BGHM-Leerformular
(Maschinen und Geräte, sechs Abschnitte). Firma, Nummer, Datum
und Unterschrift bleiben leer.
"""

import base64
import io
import json
import re
import struct
import zipfile
import zlib
from pathlib import Path
import xml.etree.ElementTree as ET

_PLACEHOLDER = (
    "stelle dir vor",
    "stell dir vor",
    "hättest eine",
    "haettest eine",
    "imagine you",
    "wenn du eine hättest",
    "hier wäre eine",
    "hier waere eine",
)

_MAX_CHARS = 80_000


# anweisung, anweisungen, Tippfehler anweidung. Nicht die Wortstellung erweitern.
_BA_NOUN = r"betriebsanwei\w{0,8}"


def wants(message):
    """Erstellen einer Betriebsanweisung. Sonst bleibt die Suche.

    „ich benötige/brauche eine Betriebsanweisung für …“ erzeugt die Chat-Datei.
    Eine Vault-Notiz wird daraus nicht.
    """
    q = message or ""
    if re.search(rf"{_BA_NOUN}\s+erstellen\b", q, re.I):
        return True
    if re.search(
        rf"erstell\w{{0,12}}\s+(?:\S+\s+){{0,8}}{_BA_NOUN}\b",
        q,
        re.I,
    ):
        return True
    return bool(re.search(
        rf"\b(?:benoetige|benötige|brauche)\s+(?:\S+\s+){{0,6}}{_BA_NOUN}\b",
        q,
        re.I,
    ))


def is_placeholder(text):
    low = (text or "").lower()
    return any(p in low for p in _PLACEHOLDER)


def claims_file(answer):
    q = (answer or "").lower()
    return any(
        s in q
        for s in ("chatdatei", "als pdf", "als word", "liegt als link", "hier die datei")
    )


def title_from(body):
    lines = []
    for ln in (body or "").splitlines():
        s = ln.strip().strip("*").strip("_").strip()
        s = re.sub(r"^#+\s*", "", s).strip()
        if not s or s == "---":
            continue
        lines.append(s)
    for s in lines:
        if re.match(r"(?i)betriebsanweisung\b", s):
            return s[:80]
    return (lines[0][:80] if lines else "Datei")


_STOP_LINE = (
    "sag bescheid",
    "zwei hinweise",
    "einordnung",
    "quellen für",
    "quellen fuer",
    "grundlagen",
    "rechtsgrundlage",
    "### assistent",
)
_CHROME_LINE = re.compile(
    r"^(firma|ba-nr|ba nr|nummer|datum|unterschrift|erstellt|geprüft|geprueft)\b",
    re.I,
)


def ba_body(text):
    """Nur die sechs Abschnitte. Kopf, Punktezeilen und Nachsatz bleiben draußen."""
    lines = []
    for ln in (text or "").splitlines():
        raw = ln.strip()
        low = raw.lower().lstrip("#").strip("*_ ")
        if re.match(r"^#{0,3}\s*tool\s*$", low):
            continue
        if "chatdatei" in low and "args" in low:
            continue
        cleaned = re.sub(
            r"</?(?:tool_calls|tool_call|parameter)\b[^>]*>",
            "",
            raw,
            flags=re.I,
        ).strip()
        changed = cleaned != raw
        if changed:
            raw = cleaned
            if not raw:
                continue
            low = raw.lower().lstrip("#").strip("*_ ")
        if any(low.startswith(s) for s in _STOP_LINE):
            break
        if _CHROME_LINE.match(low):
            continue
        if "unterschrift arbeitgeber" in low or "fachkraft für arbeitssicherheit" in low:
            continue
        if low.startswith("unterweisung der beschäftigten erfolgt"):
            continue
        if re.match(r"chatdatei\s*\(", low):
            continue
        if re.search(r"\.{5,}|…{2,}|_{5,}", raw):
            rest = re.sub(r"[.\s…:_\-–—]+", "", raw)
            if len(rest) < 40:
                continue
        lines.append(raw if changed else ln)
    return "\n".join(lines).strip()


def _py_string(blob, key):
    m = re.search(rf"{key}\s*=\s*", blob or "", re.I)
    if not m:
        return ""
    s = blob[m.end():]
    for q in ('"""', "'''"):
        if s.startswith(q):
            end = s.find(q, len(q))
            return s[len(q):end] if end > 0 else ""
    if s[:1] in "\"'":
        end = s.find(s[0], 1)
        return s[1:end] if end > 0 else ""
    return ""


def _read_quoted(s, i):
    """Wert nach title: oder text=. Ende-Index liegt hinter dem schließenden Anführungszeichen."""
    if s.startswith('"""', i) or s.startswith("'''", i):
        q = s[i:i + 3]
        end = s.find(q, i + 3)
        if end < 0:
            return s[i + 3:], len(s)
        return s[i + 3:end], end + 3
    if i >= len(s) or s[i] not in "\"'":
        return "", i
    q = s[i]
    i += 1
    out = []
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            out.append(s[i + 1])
            i += 2
            continue
        if c == q:
            return "".join(out), i + 1
        out.append(c)
        i += 1
    return "".join(out), n


def _scan_call_args(rest):
    """ChatDatei(title:"…", text:"…", format:"docx") — auch mit = statt :."""
    i = 0
    n = len(rest or "")
    args = {}
    end = None
    while i < n:
        while i < n and rest[i] in " \t\r\n,":
            i += 1
        if i >= n:
            break
        if rest[i] == ")":
            end = i + 1
            break
        m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", rest[i:])
        if not m:
            break
        key = m.group(0).lower()
        i += m.end()
        while i < n and rest[i] in " \t":
            i += 1
        if i >= n or rest[i] not in ":=":
            break
        i += 1
        while i < n and rest[i] in " \t":
            i += 1
        val, i = _read_quoted(rest, i)
        if val:
            args[key] = val
    text = str(args.get("text") or args.get("content") or "").strip()
    if not text:
        return None
    return args, end


def _xml_span(raw):
    """<tool_calls><tool_call name="ChatDatei">…</tool_call></tool_calls> als Text."""
    call = re.search(r"<tool_call\s+name=\"ChatDatei\"", raw or "", re.I)
    if not call:
        return None
    wrap = re.search(r"<tool_calls\b", raw, re.I)
    start = call.start()
    if wrap and wrap.start() <= start:
        start = wrap.start()
    tail = raw[call.end():]
    end = re.search(r"</tool_calls>", tail, re.I)
    if end:
        return start, call.end() + end.end()
    end = re.search(r"</tool_call>", tail, re.I)
    if end:
        return start, call.end() + end.end()
    return None


def _xml_call(answer):
    span = _xml_span(answer)
    if not span:
        return None
    block = (answer or "")[span[0]:span[1]]
    args = {}
    for pm in re.finditer(
        r"<parameter\s+name=\"([^\"]+)\"\s*>(.*?)</parameter>",
        block,
        re.DOTALL | re.IGNORECASE,
    ):
        key = (pm.group(1) or "").strip()
        if key:
            args[key] = (pm.group(2) or "").strip()
    text = str(args.get("text") or args.get("content") or "").strip()
    if not text:
        return None
    out = {"title": str(args.get("title") or "").strip(), "text": text}
    if args.get("format"):
        out["format"] = str(args.get("format")).strip()
    return out


def _name_args_span(raw):
    """{"name":"ChatDatei","arguments":{…}} — auch abgeschnitten bis zum Textende."""
    m = re.search(r"\{\s*\"name\"\s*:\s*\"ChatDatei\"", raw or "", re.I)
    if not m:
        return None
    try:
        _obj, end = json.JSONDecoder().raw_decode(raw[m.start():])
    except json.JSONDecodeError:
        return m.start(), len(raw)
    return m.start(), m.start() + end


def _args_dict(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return obj if isinstance(obj, dict) else None
    return None


def _name_args_call(answer):
    span = _name_args_span(answer)
    if not span:
        return None
    blob = (answer or "")[span[0]:span[1]]
    try:
        obj = json.loads(blob)
    except json.JSONDecodeError:
        obj = None
    args = _args_dict((obj or {}).get("arguments"))
    if args is None:
        args = _args_dict((obj or {}).get("args")) or {}
    text = str(args.get("text") or args.get("content") or "").strip()
    partial = obj is None
    if not text:
        m = re.search(r'"text"\s*:\s*"((?:\\.|[^"\\])*)', blob, re.S)
        if m:
            text = (
                m.group(1)
                .replace("\\n", "\n")
                .replace('\\"', '"')
                .replace("\\\\", "\\")
                .strip()
            )
        partial = True
    if not text:
        return None
    if partial and "anwendungsbereich" not in text.lower():
        return None
    title = str(args.get("title") or "").strip()
    if not title:
        tm = re.search(r'"title"\s*:\s*"((?:\\.|[^"\\])*)"', blob)
        title = tm.group(1).strip() if tm else ""
    out = {"title": title, "text": text}
    fmt = str(args.get("format") or "").strip()
    if fmt:
        out["format"] = fmt
    return out


def _narration_span(answer):
    """Start und Ende eines als Text geschriebenen ChatDatei-Aufrufs."""
    raw = answer or ""
    named = _name_args_span(raw)
    if named:
        return named
    xml = _xml_span(raw)
    if xml:
        return xml
    m = re.search(r"ChatDatei\s+args\s*=\s*", raw, re.I)
    if m:
        start = raw.find("{", m.end() - 1)
        if start >= 0:
            try:
                _obj, end = json.JSONDecoder().raw_decode(raw[start:])
            except json.JSONDecodeError:
                end = None
            if end is not None:
                stop = start + end
                tail = re.match(r"\s*result\s*=\s*", raw[stop:], re.I)
                if tail and raw[stop + tail.end():stop + tail.end() + 1] == "{":
                    try:
                        _r, rend = json.JSONDecoder().raw_decode(raw[stop + tail.end():])
                    except json.JSONDecodeError:
                        rend = None
                    if rend is not None:
                        stop = stop + tail.end() + rend
                return m.start(), stop
    call = re.search(r"ChatDatei\s*\(", raw, re.I)
    if not call:
        return None
    scanned = _scan_call_args(raw[call.end():])
    if scanned and scanned[1]:
        return call.start(), call.end() + scanned[1]
    rest = raw[call.end():]
    for quote in ('"""', "'''"):
        open_at = rest.find(quote)
        if open_at < 0:
            continue
        close_at = rest.find(quote, open_at + len(quote))
        if close_at < 0:
            continue
        paren = rest.find(")", close_at + len(quote))
        if paren < 0:
            continue
        return call.start(), call.end() + paren + 1
    return None


def narrated_call(answer):
    """Modell schreibt den Aufruf als Text statt das Werkzeug zu rufen."""
    named = _name_args_call(answer)
    if named:
        return named
    xml = _xml_call(answer)
    if xml:
        return xml
    m = re.search(r"ChatDatei\s+args\s*=\s*", answer or "", re.I)
    if m:
        start = (answer or "").find("{", m.end() - 1)
        if start >= 0:
            try:
                obj, _end = json.JSONDecoder().raw_decode(answer[start:])
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict) and str(obj.get("text") or obj.get("content") or "").strip():
                return obj
    call = re.search(r"ChatDatei\s*\(", answer or "", re.I)
    if not call:
        return None
    scanned = _scan_call_args(answer[call.end():])
    if scanned:
        args, _end = scanned
        text = str(args.get("text") or args.get("content") or "").strip()
        out = {"title": str(args.get("title") or "").strip(), "text": text}
        if args.get("format"):
            out["format"] = str(args.get("format")).strip()
        return out
    rest = answer[call.end():]
    text = _py_string(rest, "text") or _py_string(rest, "content")
    if not text.strip():
        return None
    out = {"title": _py_string(rest, "title"), "text": text}
    fmt = _py_string(rest, "format")
    if fmt:
        out["format"] = fmt
    return out


def hide_narration(answer):
    raw = answer or ""
    for _ in range(6):
        span = _narration_span(raw)
        if not span:
            break
        raw = (raw[:span[0]] + raw[span[1]:]).strip()
    return raw.strip()


def _aside_text(answer):
    """Text neben dem Aufruf, ohne die Behauptung, die Datei liege schon im Chat."""
    kept = []
    for ln in hide_narration(answer).splitlines():
        low = ln.strip().lower().lstrip("#").strip("*_ ")
        if not low or low in ("tool", "assistent"):
            continue
        if low.startswith("die datei liegt"):
            continue
        if low.startswith("rechtsgrundlage"):
            continue
        kept.append(ln.strip())
    return "\n".join(kept).strip()


def _thin_aside(answer):
    return len(_aside_text(answer)) < 400


def recent_create(message, history=None):
    """Die letzte Nutzerzeile im Verlauf hat das Erstellen verlangt."""
    if wants(message):
        return True
    last = ""
    for item in history or []:
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        text = item.get("content") or item.get("text") or ""
        if isinstance(text, str) and text.strip():
            last = text
    return bool(last) and wants(last)


def take_narration(message, answer, history=None):
    """Aufruf als Text nur annehmen, wenn erstellt werden soll und kaum Fließtext daneben steht."""
    narrated = narrated_call(answer)
    if not narrated:
        return None
    if wants(message):
        return narrated
    if _thin_aside(answer) and recent_create(message, history):
        return narrated
    return None


def body_from(answer):
    return ba_body(answer)


def _assistant_texts(history):
    out = []
    for item in history or []:
        if not isinstance(item, dict):
            continue
        if item.get("role") != "assistant":
            continue
        text = item.get("content") or item.get("text") or ""
        if isinstance(text, str) and text.strip():
            out.append(text)
    return out


def document_text(answer, history=None):
    """Den eigentlichen Vorlagentext, nicht die Ankündigung „hier die Datei“."""
    pool = []
    for src in [answer, *_assistant_texts(history)]:
        narrated = narrated_call(src)
        if narrated:
            text = ba_body(str(narrated.get("text") or narrated.get("content") or ""))
        else:
            text = body_from(src)
        if len(text) < 80 or is_placeholder(text) or _is_aushang(text):
            continue
        low = text.lower()
        if "anwendungsbereich" in low or ("betriebsanweisung" in low and "gefahren" in low):
            pool.append(text)
    if not pool:
        return ""
    return max(pool, key=len)


ONE_LINE = "Die Datei liegt als Link im Chat."


def from_answer(message, answer, history=None):
    """Nur nach „Betriebsanweisung erstellen“. Eine behauptete Datei reicht nicht."""
    if take_narration(message, answer, history) is None and not (
        wants(message) and document_text(answer, history)
    ):
        return None
    narrated = narrated_call(answer)
    if narrated:
        body = ba_body(str(narrated.get("text") or narrated.get("content") or ""))
        title = str(narrated.get("title") or "").strip() or title_from(body)
    else:
        body = document_text(answer, history)
        title = title_from(body)
    if not body:
        return None
    fmt = "docx" if re.search(r"\b(word|docx)\b", message or "", re.I) else "pdf"
    if narrated and narrated.get("format"):
        fmt = str(narrated.get("format"))
    return run({"title": title, "text": body, "format": fmt})


def chat_delivery(message, answer, history=None):
    """Sichtbarer Satz plus Datei, wenn der Chat eine Vorlage verlangt oder behauptet."""
    out = from_answer(message, answer, history)
    if not out or not out.get("ok"):
        if _narration_span(answer or ""):
            return _aside_text(answer), None
        return answer, None
    return ONE_LINE, out.get("result") or None


_ASSET_DIR = Path(__file__).resolve().parent.parent / "assets"
_BGHM_FORM = _ASSET_DIR / "bghm-betriebsanweisung-leer.docx"
_ISO_DIR = _ASSET_DIR / "iso7010"
# Leerformular im Vault, nur lesen. Die Füllung bleibt die Chat-Einmaldatei.
_VAULT_FORM = Path(
    "/Users/lxndrhbrt/ObsidianVaults/HSEQ Sync/Vorlagen/"
    "Betriebsanweisung/BGHM-Leerformular Maschinen und Geraete.docx"
)


def bghm_form_path():
    if _VAULT_FORM.is_file():
        return _VAULT_FORM
    return _BGHM_FORM
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_BGHM_HEADS = (
    ("1. Anwendungsbereich", ("anwendungsbereich",)),
    ("2. Gefahren für Mensch und Umwelt", ("gefahren",)),
    ("3. Schutzmaßnahmen und Verhaltensregeln", ("schutzmaß", "schutzmass", "verhaltensregel")),
    ("4. Verhalten bei Störungen und im Gefahrfall", ("störung", "stoerung", "gefahrfall", "brandschutz")),
    ("5. Verhalten bei Unfällen – Erste Hilfe", ("unfall", "erste hilfe")),
    ("6. Instandhaltung, Entsorgung", ("instandhaltung", "entsorgung", "umwelt", "unterweisung")),
)


def _is_aushang(text):
    """Ein Aushang ist keine Betriebsanweisung."""
    for ln in (text or "").splitlines():
        s = ln.strip().lstrip("#").strip("*_ ").strip()
        if not s or s == "---":
            continue
        return bool(re.match(r"(?i)aush[aä]ng", s))
    return False


def is_betriebsanweisung(title, text):
    blob = f"{title}\n{text}"
    if is_placeholder(blob) or _is_aushang(title) or _is_aushang(text):
        return False
    low = blob.lower()
    if "betriebsanweisung" not in low:
        return False
    return "anwendungsbereich" in low or "gefahren" in low


# Bildzeichen nach DIN ISO 7010. Keine Kennnummer im Text.
# W071 ist das Zeichen für gesundheitsgefährdende Stoffe. Einen eigenen Tonerstaub gibt es nicht.
_SIGNS = (
    ("2.", (
        (("toner", "staub"), "W071"),
        (("heiß", "heisse", "heiße", "hitze", "verbrenn", "fixier"), "W017"),
        (("elektr", "spannung", "strom", "stecker", "netzstecker"), "W012"),
    )),
    ("3.", (
        (("handschuh", "hautkontakt", "haut"), "M009"),
        (("anleitung",), "M002"),
    )),
    ("5.", (
        (("erste hilfe", "notruf"), "E003"),
    )),
)
_SIGN_NAME = {
    "W071": "Gesundheitsgefährdender Stoff",
    "W017": "Heiße Oberfläche",
    "W012": "Elektrische Spannung",
    "M009": "Handschutz",
    "M002": "Betriebsanleitung",
    "E003": "Erste Hilfe",
}
_SIGN_ORDER = list(_SIGN_NAME)


def _heads_for(low):
    hits = []
    for head, keys in _BGHM_HEADS:
        if any(k in low for k in keys):
            hits.append(head)
    # „Gefahren für Mensch und Umwelt“ ist Abschnitt 2, nicht die Entsorgung.
    if "gefahren" in low and not any(k in low for k in ("entsorgung", "instandhaltung", "unterweisung")):
        hits = [h for h in hits if not h.startswith("6.")]
    return hits


def _is_heading(raw, low):
    """Überschrift, kein Satz. „Störung melden …“ bleibt Fließtext."""
    if len(raw) >= 90 or raw.endswith((".", "!", "?")):
        return False
    if not _heads_for(low):
        return False
    if not re.match(r"^\d+\.", raw) and len(raw.split()) > 8:
        return False
    return True


def _sentences(line):
    parts = re.split(r"(?<=[.!?])\s+", line.strip())
    return [p.strip() for p in parts if p.strip()] or [line]


def _signed(head, sentence, seen):
    """Kennzeichen direkt vor den Satz, der es auslöst. Nicht über den ganzen Abschnitt."""
    out = []
    low = sentence.lower()
    for prefix, rules in _SIGNS:
        if not head.startswith(prefix):
            continue
        for keys, code in rules:
            if code in seen or not any(k in low for k in keys):
                continue
            seen.add(code)
            out.append(f"ISO 7010 {code}")
    out.append(sentence)
    return out


def bghm_sections(text):
    """Freitext in die sechs Abschnitte des Leerformulars legen."""
    buckets = {head: [] for head, _ in _BGHM_HEADS}
    seen = {head: set() for head, _ in _BGHM_HEADS}
    current = [_BGHM_HEADS[0][0]]
    for line in (text or "").splitlines():
        raw = line.strip().lstrip("#").strip("*_ ").strip()
        if not raw or raw == "---":
            for head in current:
                buckets[head].append("")
            continue
        low = raw.lower()
        if re.match(r"(?i)betriebsanweisung\b", raw) and not _heads_for(low):
            continue
        if _is_heading(raw, low):
            current = _heads_for(low)
            continue
        piece = raw.lstrip("-• ").strip()
        if piece[:1].islower() and len(piece) < 60:
            continue
        if re.match(r"(?i)^ISO 7010\b", piece):
            continue
        if len(current) == 1:
            for bit in _signed(current[0], piece, seen[current[0]]):
                if bit not in buckets[current[0]]:
                    buckets[current[0]].append(bit)
            continue
        # Eine Zeile wie „Störung/Unfall“ trägt Sätze für Abschnitt 4 und 5.
        for sentence in _sentences(piece):
            routed = [h for h in _heads_for(sentence.lower()) if h in current] or list(current)
            for head in routed:
                for bit in _signed(head, sentence, seen[head]):
                    if bit not in buckets[head]:
                        buckets[head].append(bit)
    # Gefährdung kann in einem späteren Abschnitt stehen. Das Warnzeichen bleibt rechts in Abschnitt 2.
    blob = (text or "").lower()
    head2 = "2. Gefahren für Mensch und Umwelt"
    if any(ln.strip() for ln in buckets[head2]):
        for keys, code in _SIGNS[0][1]:
            if any(k in blob for k in keys) and not any(code in ln for ln in buckets[head2]):
                buckets[head2].insert(0, f"ISO 7010 {code}")
    # Abschnitt 5 ist Erste Hilfe, auch wenn der Satz das Wort nicht wiederholt.
    head5 = "5. Verhalten bei Unfällen – Erste Hilfe"
    lines5 = buckets[head5]
    if any(ln.strip() for ln in lines5) and "ISO 7010 E003" not in lines5:
        lines5.insert(0, "ISO 7010 E003")
    for head, lines in buckets.items():
        buckets[head] = _signs_to_the_right(lines)
    return {head: "\n".join(lines).strip() for head, lines in buckets.items()}


def _is_subhead(line):
    """Kurze Zwischenüberschrift, kein Satz."""
    s = (line or "").strip()
    if not s or s.startswith("•") or re.match(r"(?i)^ISO 7010\b", s):
        return False
    if s.endswith((".", "!", "?", ":")) or len(s) > 42:
        return False
    return 1 <= len(s.split()) <= 5


def _as_points(lines):
    """Sätze als Stichpunkte. Zwischenüberschriften und Einleitungen mit Doppelpunkt bleiben stehen."""
    out = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if re.match(r"(?i)^ISO 7010\b", s):
            out.append(s)
            continue
        if _is_subhead(s) or s.endswith(":"):
            out.append(s)
            continue
        point = s.lstrip("-• ").strip()
        if point:
            out.append("• " + point)
    return out


def _signs_to_the_right(lines):
    """Alle Kennzeichen eines Abschnitts in eine Zeile, der Text bleibt links."""
    codes = []
    body = []
    for ln in lines:
        stripped = ln.strip()
        if re.match(r"(?i)^ISO 7010\b", stripped):
            for code in re.findall(r"\b[WMEPF]\d{3}\b", stripped.upper()):
                if code in _SIGN_NAME and code not in codes:
                    codes.append(code)
            continue
        body.append(ln)
    body = _as_points(body)
    if codes:
        codes.sort(key=lambda code: _SIGN_ORDER.index(code) if code in _SIGN_ORDER else 99)
        body.insert(0, "ISO 7010 " + " · ".join(codes))
    return body


def _xml_text(el):
    return "".join((t.text or "") for t in el.iter(_W + "t")).strip()


_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_PIC = "{http://schemas.openxmlformats.org/drawingml/2006/picture}"
_WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_SIGN_PX = 128
_SIGN_EMU = 396000


def _png_rgba(size, rgba):
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)
        raw.extend(rgba[y * stride:(y + 1) * stride])

    def chunk(tag, data):
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def _inside_tri(x, y, a, b, c):
    (x1, y1), (x2, y2), (x3, y3) = a, b, c
    den = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
    if not den:
        return False
    w1 = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / den
    w2 = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / den
    w3 = 1 - w1 - w2
    return w1 >= -0.002 and w2 >= -0.002 and w3 >= -0.002


def _in_poly(x, y, pts):
    inside = False
    j = len(pts) - 1
    for i, (xi, yi) in enumerate(pts):
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi:
            inside = not inside
        j = i
    return inside


def _paint_poly(put, n, pts, color):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    for y in range(max(0, int(min(ys)) - 1), min(n, int(max(ys)) + 2)):
        for x in range(max(0, int(min(xs)) - 1), min(n, int(max(xs)) + 2)):
            if _in_poly(x + 0.5, y + 0.5, pts):
                put(x, y, color)


def _paint_disc(put, n, cx, cy, r, color):
    r2 = r * r
    for y in range(max(0, int(cy - r) - 1), min(n, int(cy + r) + 2)):
        for x in range(max(0, int(cx - r) - 1), min(n, int(cx + r) + 2)):
            if (x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2 <= r2:
                put(x, y, color)


def _paint_seg(put, n, x0, y0, x1, y1, width, color):
    rad = width / 2
    rad2 = rad * rad
    dx, dy = x1 - x0, y1 - y0
    span = dx * dx + dy * dy or 1
    for y in range(max(0, int(min(y0, y1) - rad) - 1), min(n, int(max(y0, y1) + rad) + 2)):
        for x in range(max(0, int(min(x0, x1) - rad) - 1), min(n, int(max(x0, x1) + rad) + 2)):
            px, py = x + 0.5, y + 0.5
            t = max(0, min(1, ((px - x0) * dx + (py - y0) * dy) / span))
            qx, qy = x0 + t * dx, y0 + t * dy
            if (px - qx) ** 2 + (py - qy) ** 2 <= rad2:
                put(x, y, color)


def _icon(put, n, code, ink):
    """Einfaches Bildzeichen. Kein Pixel-Code, keine Kopie von iso7010.de."""
    if code == "W012":
        _paint_poly(put, n, [(70, 50), (54, 76), (66, 76), (52, 106), (86, 70), (72, 70), (88, 50)], ink)
    elif code == "W017":
        _paint_seg(put, n, 36, 104, 96, 104, 5, ink)
        for x0, height in ((42, 16), (60, 26), (78, 16)):
            _paint_seg(put, n, x0, 98, x0, 98 - height, 4, ink)
            _paint_seg(put, n, x0, 98 - height, x0 + 12, 98 - height, 4, ink)
            _paint_seg(put, n, x0 + 12, 98 - height, x0 + 12, 98, 4, ink)
    elif code == "W008":
        _paint_disc(put, n, 78, 56, 7, ink)
        _paint_seg(put, n, 76, 64, 58, 90, 5, ink)
        _paint_seg(put, n, 70, 74, 90, 66, 4, ink)
        _paint_seg(put, n, 58, 90, 46, 106, 4, ink)
        _paint_seg(put, n, 58, 90, 74, 106, 4, ink)
    elif code == "W011":
        _paint_seg(put, n, 36, 104, 100, 96, 4, ink)
        _paint_disc(put, n, 68, 58, 7, ink)
        _paint_seg(put, n, 66, 66, 74, 88, 5, ink)
        _paint_seg(put, n, 74, 88, 96, 98, 4, ink)
        _paint_seg(put, n, 74, 88, 58, 104, 4, ink)
    elif code == "M002":
        _paint_poly(put, n, [(28, 40), (60, 34), (60, 96), (28, 90)], ink)
        _paint_poly(put, n, [(68, 34), (100, 40), (100, 90), (68, 96)], ink)
    elif code == "M008":
        _paint_poly(put, n, [(50, 46), (72, 46), (76, 74), (102, 80), (102, 98), (40, 98), (40, 80), (54, 74)], ink)
    elif code == "M009":
        _paint_poly(put, n, [(48, 36), (74, 32), (80, 48), (96, 44), (100, 62), (82, 74), (78, 100), (50, 100), (46, 70)], ink)
    elif code == "M012":
        _paint_seg(put, n, 40, 36, 40, 104, 7, ink)
        _paint_disc(put, n, 64, 46, 9, ink)
        _paint_seg(put, n, 64, 56, 84, 74, 5, ink)
    elif code == "E003":
        _paint_poly(put, n, [(56, 28), (72, 28), (72, 56), (100, 56), (100, 72), (72, 72), (72, 100), (56, 100), (56, 72), (28, 72), (28, 56), (56, 56)], ink)


def _sign_png(code):
    """Hinterlegte DIN-ISO-7010-Zeichnung. Keine eigene Skizze."""
    path = _ISO_DIR / f"{code.upper()}.png"
    if not path.is_file():
        raise FileNotFoundError(path)
    return path.read_bytes()


class _SignMedia:
    def __init__(self):
        self.by_code = {}
        self.files = []
        self._seq = 30
        self._doc = 80

    def place(self, code):
        code = code.upper()
        if code not in self.by_code:
            self._seq += 1
            self._doc += 1
            rid = f"rId{self._seq}"
            self.by_code[code] = (rid, self._doc)
            self.files.append((rid, f"word/media/iso7010-{code.lower()}.png", _sign_png(code)))
        return self.by_code[code]


def _sign_para(codes, media):
    p = ET.Element(_W + "p")
    for code in codes:
        rid, doc_id = media.place(code)
        r = ET.SubElement(p, _W + "r")
        drawing = ET.SubElement(r, _W + "drawing")
        inline = ET.SubElement(drawing, _WP + "inline")
        inline.set("distT", "0")
        inline.set("distB", "0")
        inline.set("distL", "40000")
        inline.set("distR", "40000")
        extent = ET.SubElement(inline, _WP + "extent")
        extent.set("cx", str(_SIGN_EMU))
        extent.set("cy", str(_SIGN_EMU))
        effect = ET.SubElement(inline, _WP + "effectExtent")
        for edge in ("l", "t", "r", "b"):
            effect.set(edge, "0")
        doc = ET.SubElement(inline, _WP + "docPr")
        doc.set("id", str(doc_id))
        doc.set("name", _SIGN_NAME.get(code, "Symbol"))
        doc.set("descr", _SIGN_NAME.get(code, "Symbol"))
        frame = ET.SubElement(inline, _WP + "cNvGraphicFramePr")
        locks = ET.SubElement(frame, _A + "graphicFrameLocks")
        locks.set("noChangeAspect", "1")
        graphic = ET.SubElement(inline, _A + "graphic")
        data = ET.SubElement(graphic, _A + "graphicData")
        data.set("uri", "http://schemas.openxmlformats.org/drawingml/2006/picture")
        pic = ET.SubElement(data, _PIC + "pic")
        nv = ET.SubElement(pic, _PIC + "nvPicPr")
        cnv = ET.SubElement(nv, _PIC + "cNvPr")
        cnv.set("id", str(doc_id))
        cnv.set("name", f"{_SIGN_NAME.get(code, 'Symbol')}.png")
        ET.SubElement(nv, _PIC + "cNvPicPr")
        fill = ET.SubElement(pic, _PIC + "blipFill")
        blip = ET.SubElement(fill, _A + "blip")
        blip.set(_REL + "embed", rid)
        stretch = ET.SubElement(fill, _A + "stretch")
        ET.SubElement(stretch, _A + "fillRect")
        sp = ET.SubElement(pic, _PIC + "spPr")
        xfrm = ET.SubElement(sp, _A + "xfrm")
        off = ET.SubElement(xfrm, _A + "off")
        off.set("x", "0")
        off.set("y", "0")
        ext = ET.SubElement(xfrm, _A + "ext")
        ext.set("cx", str(_SIGN_EMU))
        ext.set("cy", str(_SIGN_EMU))
        geom = ET.SubElement(sp, _A + "prstGeom")
        geom.set("prst", "rect")
        ET.SubElement(geom, _A + "avLst")
    return p


def _type_size(sections):
    """Schrift so wählen, dass die sechs Abschnitte auf der A4-Seite bleiben."""
    lines = 0
    for body in sections.values():
        for ln in (body or "").splitlines():
            if not ln.strip() or re.match(r"(?i)^ISO 7010\b", ln.strip()):
                continue
            # Linke Spalte ist schmaler, weil rechts die Zeichen stehen.
            lines += max(1, (len(ln) + 70) // 74)
    if lines > 58:
        return "13", "156"
    if lines > 44:
        return "15", "180"
    return "16", "200"


def _para(line, sz="16", line_h="200"):
    p = ET.Element(_W + "p")
    ppr = ET.SubElement(p, _W + "pPr")
    spacing = ET.SubElement(ppr, _W + "spacing")
    spacing.set(_W + "before", "0")
    spacing.set(_W + "after", "0")
    spacing.set(_W + "line", line_h)
    spacing.set(_W + "lineRule", "exact")
    if line.startswith("• "):
        ind = ET.SubElement(ppr, _W + "ind")
        ind.set(_W + "left", "220")
        ind.set(_W + "hanging", "180")
    elif _is_subhead(line):
        spacing.set(_W + "before", "40")
    r = ET.SubElement(p, _W + "r")
    rpr = ET.SubElement(r, _W + "rPr")
    if _is_subhead(line):
        ET.SubElement(rpr, _W + "b")
        ET.SubElement(rpr, _W + "bCs")
    size = ET.SubElement(rpr, _W + "sz")
    size.set(_W + "val", sz)
    size2 = ET.SubElement(rpr, _W + "szCs")
    size2.set(_W + "val", sz)
    t = ET.SubElement(r, _W + "t")
    if line.startswith(" ") or line.endswith(" "):
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    t.text = line
    return p


def _fill_cell(tc, text, media, sz="16", line_h="200"):
    for child in list(tc):
        if child.tag == _W + "p":
            tc.remove(child)
    lines = [ln for ln in (text or "").splitlines()]
    codes = []
    if lines and re.match(r"(?i)^ISO 7010\b", lines[0].strip()):
        codes = [c for c in re.findall(r"\b[WMEPF]\d{3}\b", lines[0].upper()) if c in _SIGN_NAME]
        lines = lines[1:]
    paras = [_para(ln, sz, line_h) for ln in lines] or [_para("", sz, line_h)]
    if not codes:
        for p in paras:
            tc.append(p)
        return
    tbl = ET.Element(_W + "tbl")
    tbl_pr = ET.SubElement(tbl, _W + "tblPr")
    tbl_w = ET.SubElement(tbl_pr, _W + "tblW")
    tbl_w.set(_W + "w", "5000")
    tbl_w.set(_W + "type", "pct")
    borders = ET.SubElement(tbl_pr, _W + "tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        node = ET.SubElement(borders, _W + edge)
        node.set(_W + "val", "nil")
    grid = ET.SubElement(tbl, _W + "tblGrid")
    for width in ("8600", "2682"):
        col = ET.SubElement(grid, _W + "gridCol")
        col.set(_W + "w", width)
    tr = ET.SubElement(tbl, _W + "tr")
    left = ET.SubElement(tr, _W + "tc")
    left_pr = ET.SubElement(left, _W + "tcPr")
    left_w = ET.SubElement(left_pr, _W + "tcW")
    left_w.set(_W + "w", "8600")
    left_w.set(_W + "type", "dxa")
    for p in paras:
        left.append(p)
    right = ET.SubElement(tr, _W + "tc")
    right_pr = ET.SubElement(right, _W + "tcPr")
    right_w = ET.SubElement(right_pr, _W + "tcW")
    right_w.set(_W + "w", "2682")
    right_w.set(_W + "type", "dxa")
    valign = ET.SubElement(right_pr, _W + "vAlign")
    valign.set(_W + "val", "top")
    right.append(_sign_para(codes, media))
    tc.append(tbl)


def _with_sign_parts(src_xml, media):
    if not media.files:
        return src_xml
    extra = "".join(
        f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/{name.split("/")[-1]}"/>'
        for rid, name, _blob in media.files
    )
    return src_xml.replace("</Relationships>", extra + "</Relationships>")


def _with_png_type(src_xml):
    if 'Extension="png"' in src_xml:
        return src_xml
    return src_xml.replace(
        'Extension="rels"',
        'Extension="png" ContentType="image/png"/><Default Extension="rels"',
        1,
    )


def build_bghm_docx(title, text):
    form = bghm_form_path()
    if not form.is_file():
        raise FileNotFoundError(form)
    sections = bghm_sections(text)
    sz, line_h = _type_size(sections)
    media = _SignMedia()
    raw = zipfile.ZipFile(form).read("word/document.xml")
    decoded = raw.decode("utf-8")
    for prefix, uri in re.findall(r'xmlns:([A-Za-z0-9]+)="([^"]+)"', decoded):
        ET.register_namespace(prefix, uri)
    ET.register_namespace("pic", "http://schemas.openxmlformats.org/drawingml/2006/picture")
    root = ET.fromstring(raw)
    body = root.find(_W + "body")
    tbl = next(c for c in list(body) if c.tag == _W + "tbl")
    rows = tbl.findall(_W + "tr")
    heads = {head for head, _ in _BGHM_HEADS}
    for i, tr in enumerate(rows[:-1]):
        label = _xml_text(tr)
        matched = next((head for head in heads if head in label), None)
        if not matched or i + 1 >= len(rows):
            continue
        nxt = rows[i + 1]
        if any(h in _xml_text(nxt) for h in heads):
            continue
        tc = nxt.find(_W + "tc")
        if tc is not None:
            _fill_cell(tc, sections.get(matched, ""), media, sz, line_h)
    out_xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(form) as src, zipfile.ZipFile(buf, "w") as dst:
        for info in src.infolist():
            if info.filename == "word/document.xml":
                data = out_xml
            elif info.filename == "word/_rels/document.xml.rels":
                data = _with_sign_parts(src.read(info.filename).decode("utf-8"), media).encode("utf-8")
            elif info.filename == "[Content_Types].xml" and media.files:
                data = _with_png_type(src.read(info.filename).decode("utf-8")).encode("utf-8")
            else:
                data = src.read(info.filename)
            dst.writestr(info, data)
        for _rid, name, blob in media.files:
            dst.writestr(name, blob)
    return buf.getvalue()


def bghm_plain(_title, text):
    sections = bghm_sections(text)
    lines = ["Betriebsanweisung", "Firma:", "Nummer:", ""]
    for head, _ in _BGHM_HEADS:
        lines.append(head)
        body = sections.get(head, "")
        if body:
            lines.append(body)
        lines.append("")
    lines.extend(["Datum:", "Unterschrift:"])
    return "\n".join(lines).strip()


def run(args):
    args = args or {}
    title = str(args.get("title") or "").strip()
    text = str(args.get("text") or args.get("content") or "").strip()
    fmt = _format(args.get("format") or args.get("kind") or "pdf")
    if fmt is None:
        return {"ok": False, "result": None, "error": "format ist pdf oder docx"}
    if not text:
        return {"ok": False, "result": None, "error": "text fehlt"}
    if is_placeholder(text):
        return {
            "ok": False,
            "result": None,
            "error": "Das ist keine Betriebsanweisung.",
        }
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS]
    if not title:
        title = next((ln.strip() for ln in text.splitlines() if ln.strip()), "Datei")[:80]
    plain = text
    if is_betriebsanweisung(title, text):
        fmt = "docx"
        text = ba_body(text) or text
        raw = build_bghm_docx(title, text)
        plain = bghm_plain(title, text)
    else:
        raw = build_docx(title, text) if fmt == "docx" else build_pdf(title, text)
    ext = "docx" if fmt == "docx" else "pdf"
    mime = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if fmt == "docx"
        else "application/pdf"
    )
    return {
        "ok": True,
        "result": {
            "title": title,
            "name": _filename(title, ext),
            "format": fmt,
            "mime": mime,
            "text": plain,
            "data": base64.b64encode(raw).decode("ascii"),
        },
    }


def _format(raw):
    f = str(raw or "").strip().lower()
    if f in ("pdf",):
        return "pdf"
    if f in ("docx", "doc", "word", "worddatei"):
        return "docx"
    return None


def _filename(title, ext):
    name = re.sub(r'[\\/:*?"<>|]+', "", title).strip()
    name = re.sub(r"\s+", " ", name)[:60].strip() or "Datei"
    return f"{name}.{ext}"


def _lines(title, text):
    rows = [("title", title)]
    for para in text.splitlines():
        piece = para.strip()
        if not piece:
            rows.append(("blank", ""))
            continue
        if piece.startswith("#"):
            rows.append(("h", piece.lstrip("#").strip() or piece))
            continue
        buf = ""
        for word in piece.split():
            trial = f"{buf} {word}".strip()
            if len(trial) > 90 and buf:
                rows.append(("p", buf))
                buf = word
            else:
                buf = trial
        if buf:
            rows.append(("p", buf))
    return rows


def build_pdf(title, text):
    pages = []
    chunk = []
    y = 800

    def take(kind, line):
        nonlocal y
        if kind == "title":
            y -= 28
        elif kind == "h":
            y -= 22
        elif kind == "blank":
            y -= 10
        else:
            y -= 15

    for kind, line in _lines(title, text):
        need = 28 if kind == "title" else 22 if kind == "h" else 10 if kind == "blank" else 15
        if chunk and y - need < 56:
            pages.append(chunk)
            chunk = []
            y = 800
        chunk.append((kind, line))
        take(kind, line)
    if chunk:
        pages.append(chunk)
    if not pages:
        pages = [[("title", title or "Datei")]]
    streams = [_page_stream(p) for p in pages]
    return _pdf_doc(streams)


def _esc(text):
    raw = text.encode("latin-1", "replace").decode("latin-1")
    return raw.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _page_stream(rows):
    parts = ["BT"]
    y = 800
    for kind, line in rows:
        if kind == "blank":
            y -= 10
            continue
        if kind == "title":
            parts.append("/F2 16 Tf")
            y_use = y
            y -= 28
        elif kind == "h":
            parts.append("/F2 13 Tf")
            y_use = y
            y -= 22
        else:
            parts.append("/F1 11 Tf")
            y_use = y
            y -= 15
        parts.append(f"1 0 0 1 50 {y_use} Tm ({_esc(line)}) Tj")
    parts.append("ET")
    return "\n".join(parts).encode("latin-1", "replace")


def _pdf_doc(streams):
    objects = []

    def add(body):
        objects.append(body)
        return len(objects)

    font = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )
    fontb = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
    )
    content_ids = []
    for stream in streams:
        content_ids.append(
            add(
                b"<< /Length "
                + str(len(stream)).encode("ascii")
                + b" >>\nstream\n"
                + stream
                + b"\nendstream"
            )
        )
    page_ids = []
    pages_id_placeholder = len(objects) + 1 + len(content_ids)
    # pages object comes after page objects
    for cid in content_ids:
        page_ids.append(
            add(
                (
                    f"<< /Type /Page /Parent {pages_id_placeholder} 0 R "
                    f"/MediaBox [0 0 595 842] "
                    f"/Contents {cid} 0 R "
                    f"/Resources << /Font << /F1 {font} 0 R /F2 {fontb} 0 R >> >> >>"
                ).encode("ascii")
            )
        )
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    pages_id = add(
        f"<< /Type /Pages /Count {len(page_ids)} /Kids [{kids}] >>".encode("ascii")
    )
    if pages_id != pages_id_placeholder:
        raise RuntimeError("PDF-Seitenbaum verrutscht")
    catalog = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("ascii"))
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f"{i} 0 obj\n".encode("ascii"))
        out.extend(body)
        out.extend(b"\nendobj\n")
    xref_at = len(out)
    out.extend(f"xref\n0 {len(objects)+1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        (
            f"trailer << /Size {len(objects)+1} /Root {catalog} 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(out)


def build_docx(title, text):
    def p(value, bold=False, size=22):
        b = "<w:b/>" if bold else ""
        safe = (
            value.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        return (
            "<w:p><w:r><w:rPr>"
            f"{b}<w:sz w:val=\"{size}\"/><w:szCs w:val=\"{size}\"/>"
            "</w:rPr><w:t xml:space=\"preserve\">"
            f"{safe}</w:t></w:r></w:p>"
        )

    body = [p(title, bold=True, size=32)]
    for line in text.splitlines():
        piece = line.strip()
        if not piece:
            body.append("<w:p/>")
            continue
        if piece.startswith("#"):
            body.append(p(piece.lstrip("#").strip(), bold=True, size=26))
        else:
            body.append(p(piece))
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(body)}"
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/>'
        "</w:sectPr></w:body></w:document>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
        '<w:name w:val="Normal"/>'
        '<w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/>'
        '<w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr></w:style></w:styles>'
    )
    ctypes = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )
    docrels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
        "</Relationships>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ctypes)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document.encode("utf-8"))
        z.writestr("word/styles.xml", styles)
        z.writestr("word/_rels/document.xml.rels", docrels)
    return buf.getvalue()
