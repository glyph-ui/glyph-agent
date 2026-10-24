# -*- coding: utf-8 -*-
"""
Suchbrücke — Rückfragen verlaufsfrei machen, BEVOR Exa/TinyFish sie sehen.

Problem (live, 2026-10-11): Der Web-Precheck kannte nur die aktuelle Nachricht.
„wie oft muss ich das tun?" ging wörtlich an Exa/TinyFish — geliefert wurden
Treffer zu Winterdienst und Postzustellung. Der Verlauf lag im Tool-Loop die
ganze Zeit vor (core/history.py), wurde aber nie an die Suche übergeben.

Ablauf (Think → Search → Search → Think → Entwurf):
  1. Brücke: EIN Modellcall bekommt die letzten Turns + die Rückfrage und
     liefert 1–2 eigenständige Suchanfragen (JSON) — Fachbegriff aus dem
     Verlauf statt „das/es/wie oft".
  2. Suche: jede Anfrage geht an Exa/TinyFish (core/web.py).
  3. Antwort: wie bisher aus den Treffern (core/tool_loop.py).

Kosten und Schutz:
  - Kein Modellcall ohne Verlauf (nichts aufzulösen) und keiner ohne
    Kontextbezug (needs_context) — eigenständige Fragen laufen unverändert.
  - Kein Modellcall für den lokalen Kopf: dort kostet ein Call ~90 s Prefill,
    es bleibt beim regelbasierten Fallback.
  - Kurzes Zeitlimit (SEARCH_BRIDGE_TIMEOUT, Default 8 s) und immer ein
    regelbasierter Fallback: die Suche fällt nie aus, nur weil die Brücke hakt.
  - An das Modell gehen NUR Verlaufstext und die Rückfrage — derselbe Hop, der
    ohnehin antwortet. An Exa/TinyFish geht nur die fertige Suchanfrage, nie
    Vault-Inhalt (Regel aus core/web.py).

Copyright (c) 2026 Alexander Hubert · SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import json
import re

from . import config, llm, log

# --- Schalter (Env → config.py) --------------------------------------------
ENABLED = bool(getattr(config, "SEARCH_BRIDGE", True))
TIMEOUT = int(getattr(config, "SEARCH_BRIDGE_TIMEOUT", 8) or 8)
MAX_QUERIES = max(1, int(getattr(config, "SEARCH_BRIDGE_QUERIES", 2) or 2))
MODEL = str(getattr(config, "SEARCH_BRIDGE_MODEL", "") or "").strip()
# Verlauf, der in den Brücken-Prompt geht (kurz halten — nur der Bezug zählt).
HISTORY_MSGS = max(2, int(getattr(config, "SEARCH_BRIDGE_TURNS", 6) or 6))
HISTORY_CHARS = max(400, int(getattr(config, "SEARCH_BRIDGE_CHARS", 2500) or 2500))
QUERY_CHARS = 160

# --- Regel-Heuristik --------------------------------------------------------
# Eindeutige Verweise auf den Verlauf. Artikel (der/die/den) stehen bewusst
# NICHT hier — sonst würde jede lange Sachfrage umformuliert.
_DEIXIS = frozenset({
    "das", "dies", "diese", "dieser", "dieses", "diesen", "diesem",
    "es", "davon", "dazu", "dafür", "dabei", "damit", "daran", "daraus",
    "dort", "obige", "obigen", "genannte", "genannten", "erwähnte",
    "erwähnten", "besagte", "besagten", "ihn", "ihm", "ihnen",
})
# Kurze Rückfrage-Formen: allein ohne Verlauf mehrdeutig.
_FOLLOWUP_PHRASES = (
    "wie oft", "wie lange", "wie viel", "wieviel", "wie hoch", "wie viele",
    "warum", "wieso", "weshalb", "wann", "wozu", "wofür", "wofur",
    "welche", "welcher", "welches", "welchen", "welchem",
    "gilt das", "gilt die", "gilt der", "muss ich", "muss man", "muss das",
    "soll ich", "sollte ich", "darf ich", "und dann", "und wie", "was noch",
    "mehr dazu", "weitere", "sonst noch", "nochmal", "genauer", "beispiel",
    "quelle", "belege", "gibt es dazu",
)
# Technische Kürzel/Normen: machen eine Frage auch kurz eigenständig.
_ABBREV_RE = re.compile(
    r"(?:\b(?:TRGS|TRBS|TRBA|TRLV|DGUV|ASR|AMR|DIN|EN|ISO|GefStoffV|"
    r"BetrSichV|ArbSchG|ArbStättV|BaustellV|BauA|BAuA|KomNet)\b)"
    r"|§\s*\d+|\b\d{3,}\b",
    re.I,
)
# Füllwörter, die als „Fachbegriff" nichts taugen.
_TERM_STOP = frozenset({
    "bitte", "danke", "welche", "welcher", "welches", "welchen", "welchem",
    "wann", "warum", "wieso", "weshalb", "kannst", "koennen", "können",
    "muss", "müssen", "musst", "soll", "sollte", "sollten", "müsste",
    "diese", "dieser", "dieses", "diesen", "damit", "dabei", "davon",
    "dazu", "dafür", "daran", "daraus", "nicht", "noch", "auch", "aber",
    "oder", "wenn", "dann", "sich", "doch", "mal", "mehr", "alles",
})

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)
_LEAD_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")
_QUOTES = "\"'„“”‚‘’"
_WORD_RE = re.compile(r"[a-zäöüß0-9]+")
_TERM_RE = re.compile(r"\b[A-ZÄÖÜ][\wÄÖÜäöüß\-]{3,}\b|\b[a-zäöüß]{10,}\b|\b\d{3,}\b")


# --- Text-Helfer ------------------------------------------------------------
def _norm(text):
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _words(text):
    return [w for w in _WORD_RE.findall(_norm(text)) if w]


def has_domain_token(text):
    """True, wenn die Frage selbst schon einen suchbaren Fachbegriff trägt.

    Ein langer deutscher Komposit-Begriff (Gefährdungsbeurteilung) oder ein
    Norm-Kürzel (TRGS 600) macht die Frage eigenständig — dann braucht sie
    keine Brücke, auch wenn sie kurz ist.
    """
    s = str(text or "")
    if _ABBREV_RE.search(s):
        return True
    for i, raw in enumerate(s.split()):
        w = raw.strip(".,;:!?()[]{}\"'„“”")
        if len(w) >= 10 and w[0].isupper() and (i > 0 or "-" in w):
            return True
    return False


def needs_context(text, prior_history=None):
    """True: die Nachricht ist ohne Verlauf nicht sinnvoll suchbar.

    Ohne Verlauf immer False — die erste Frage war laut Live-Beobachtung nie
    das Problem und soll unverändert (ohne Modellcall) durchlaufen.
    """
    if not prior_history:
        return False
    q = _norm(text)
    words = _words(q)
    if not words:
        return False
    # Sehr kurz („und wie oft?", „warum?") — allein mehrdeutig. Ein Norm-Kürzel
    # („TRGS 600 Frist") macht auch eine kurze Frage eigenständig.
    if len(words) <= 3:
        return not has_domain_token(text)
    # Eindeutiger Verweis auf Vorheriges.
    if any(w in _DEIXIS for w in words):
        return True
    # Rückfrage-Formulierung ohne eigenen Fachbegriff.
    if len(words) <= 8 and not has_domain_token(text):
        return any(p in q for p in _FOLLOWUP_PHRASES)
    return False


def plain_query(user_message):
    """Wörtliche Suchanfrage (Preis-Fragen mit Wertbegriff, sonst die Frage).

    War bis 2026-10-11 `_derive_web_query` in tool_loop — dort ohne Verlauf.
    """
    q = str(user_message or "").strip()
    low = q.lower()
    for word in ("preis", "kosten", "kostet", "welpenpreis"):
        if word in low:
            core = re.sub(
                r"^(was|wie|welche|welcher|welches|wieviel|wie viel)\s+",
                "", q, flags=re.I,
            )
            core = re.sub(r"[?.,]\s*$", "", core)
            if "preis" not in low and "kosten" not in low:
                core = core + " Preis"
            return core.strip()[:120] or q
    return q[:120] or ""


def clean_query(value):
    """Eine Suchanfrage aus der Modellantwort normalisieren."""
    q = str(value or "")
    q = _LEAD_RE.sub("", q.strip())
    q = q.strip().strip(_QUOTES).strip()
    q = re.sub(r"\s+", " ", q)
    for prefix in ("suchanfrage:", "suchbegriff:", "query:", "suche:"):
        if q.lower().startswith(prefix):
            q = q[len(prefix):].strip()
    q = q.strip().strip(_QUOTES).strip()
    return q.strip(" ?!.,;:").strip()[:QUERY_CHARS]


def key_terms(text):
    """Fachbegriffe/Zahlen aus einem Text — für den Regel-Fallback."""
    out, seen = [], set()
    for m in _TERM_RE.finditer(str(text or "")):
        w = m.group(0)
        low = w.lower()
        if low in _TERM_STOP or low in seen or low in _DEIXIS:
            continue
        seen.add(low)
        out.append(w)
    return out


def last_user_message(prior_history):
    for m in reversed(list(prior_history or [])):
        if str((m or {}).get("role") or "").strip().lower() == "user":
            return str((m or {}).get("content") or "")
    return ""


def slim_history(prior_history):
    """Letzte Turns, gekürzt: die Brücke braucht den Bezug, nicht den Aufsatz."""
    msgs = [
        m for m in (prior_history or [])
        if str((m or {}).get("role") or "").strip().lower() in ("user", "assistant")
    ][-HISTORY_MSGS:]
    out, total = [], 0
    for m in reversed(msgs):
        role = str(m.get("role")).strip().lower()
        cap = 400 if role == "user" else 700
        body = re.sub(r"\s+", " ", str(m.get("content") or "")).strip()
        if not body:
            continue
        if len(body) > cap:
            body = body[:cap] + " …"
        out.append({"role": role, "content": body})
        total += len(body)
        if total >= HISTORY_CHARS:
            break
    return list(reversed(out))


# --- Plan bauen -------------------------------------------------------------
def heuristic_plan(user_message, prior_history=None):
    """Regel-Fallback: Suchbegriffe des letzten Turns + aktuelle Frage.

    Kein Modell, keine Latenz. Deutlich besser als die nackte Rückfrage
    („Fachbegriffe … wie oft muss ich das tun?"), aber kein Ersatz für die
    Brücke — deshalb nur der Fallback.
    """
    q = plain_query(user_message)
    if not needs_context(user_message, prior_history):
        return {"standalone": "", "queries": [q] if q else [],
                "terms": [], "source": "passthrough"}
    terms = key_terms(last_user_message(prior_history))
    combo = (" ".join(terms[:5]) + " " + q).strip() if terms else q
    return {"standalone": "", "queries": [combo[:QUERY_CHARS] or q],
            "terms": terms[:8], "source": "heuristic"}


def parse_plan(text):
    """Modellantwort → {standalone, queries, terms} oder None."""
    data = _first_json(text)
    if data is None:
        return None
    standalone, terms = "", []
    if isinstance(data, dict):
        raw = data.get("queries") or data.get("search_queries") or data.get("query") or []
        standalone = str(data.get("standalone") or "").strip()
        terms = data.get("terms") or []
    elif isinstance(data, list):
        raw = data
    else:
        return None
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(terms, str):
        terms = [terms]

    queries, seen = [], set()
    for item in raw:
        if isinstance(item, dict):
            item = item.get("query") or item.get("q") or item.get("text") or ""
        q = clean_query(item)
        if len(q) < 3 or q.lower() in seen:
            continue
        seen.add(q.lower())
        queries.append(q)
        if len(queries) >= MAX_QUERIES:
            break
    if not queries:
        return None
    return {
        "standalone": clean_query(standalone),
        "queries": queries,
        "terms": [clean_query(t) for t in terms if clean_query(t)][:8],
    }


def _strip_fences(text):
    m = _FENCE_RE.search(str(text or ""))
    return m.group(1).strip() if m else str(text or "").strip()


# Marker, die eine Modellantwort als Prosa entlarven (keine Suchanfrage).
_PROSE_MARKERS = (
    "ich ", "leider", "gerne", "kann dir", "keine ahnung", "tut mir",
    "hier ist", "zusammenfassend", "antwort:",
)


def _looks_like_prose(text):
    """True: die Antwort ist Prosa, keine Liste von Suchanfragen.

    Ohne JSON darf nicht jede Modellantwort als Suchbegriff durchgehen — sonst
    sucht Exa nach „Ich habe leider keine Ahnung.".
    """
    s = str(text or "").strip()
    if not s:
        return True
    low = s.lower()
    if any(m in low for m in _PROSE_MARKERS):
        return True
    lines = [ln for ln in s.splitlines() if ln.strip()]
    if len(lines) == 1:
        # Ein Satz (Großbuchstabe, Satzende) ist Prosa; Kurzanfragen nicht.
        if re.search(r"[.!?:]\s*$", lines[0]):
            return True
        if len(lines[0].split()) > 16:
            return True
    return False


def _first_json(text):
    """JSON-Objekt/-Liste aus einer Modellantwort ziehen (auch mit Vorrede)."""
    s = _strip_fences(text)
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = s.find(opener), s.rfind(closer)
        if i >= 0 and j > i:
            try:
                return json.loads(s[i:j + 1])
            except Exception:
                continue
    # Kein JSON: zeilenweise Anfragen („1. Ersatzprüfung Gefahrstoffe Frist").
    if _looks_like_prose(s):
        return None
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    return lines or None


# --- Modellcall -------------------------------------------------------------
_SYSTEM = (
    "Du bist die Suchbrücke eines deutschen Fach-Chats (Arbeitsschutz, HSEQ, "
    "Technik). Du bekommst den bisherigen Chat-Verlauf und die letzte Rückfrage "
    "des Nutzers. Aufgabe: Formuliere daraus 1 bis 2 eigenständige Suchanfragen "
    "für eine Web-Suche (Exa/TinyFish).\n"
    "Regeln:\n"
    "1. Ersetze Verweise (das, es, dabei, damit, wie oft) durch den gemeinten "
    "Fachbegriff aus dem Verlauf.\n"
    "2. Übernimm Fachbegriffe, Regelwerke, Paragrafen, Behörden und Fristen aus "
    "dem Verlauf wörtlich.\n"
    "3. Höchstens 12 Wörter je Suchanfrage, keine Fragezeichen, keine Anführungs"
    "zeichen, keine Höflichkeit.\n"
    "4. Genau eine Anfrage, wenn die Rückfrage eindeutig ist. Zwei nur, wenn ein "
    "zweiter Blickwinkel gebraucht wird (z. B. Frist und Rechtsgrundlage).\n"
    "5. Keine erfundenen Fakten, keine Betriebs- oder Personendaten aus dem "
    "Verlauf, keine Dokumenttitel.\n"
    'Antworte NUR mit JSON: {"standalone": "<die Rückfrage als ein Satz, '
    'verlaufsfrei>", "queries": ["<Suchanfrage 1>"], "terms": ["<Fachbegriff>"]}'
)


def _user_block(user_message, prior_history):
    lines = ["VERLAUF:"]
    turns = slim_history(prior_history)
    if not turns:
        lines.append("(kein Verlauf)")
    for m in turns:
        label = "Nutzer" if m["role"] == "user" else "Assistent"
        lines.append(f"{label}: {m['content']}")
    lines += ["", f"RÜCKFRAGE: {str(user_message or '').strip()}", "", "JSON:"]
    return "\n".join(lines)


def _model_provider():
    """Optionales eigenes Brücken-Modell (SEARCH_BRIDGE_MODEL); sonst None."""
    if not MODEL:
        return None
    try:
        from .providers.direct import DirectProvider, uses_openrouter_slug
        from .providers.openrouter import OpenRouterProvider

        if uses_openrouter_slug(MODEL):
            return OpenRouterProvider(model=MODEL, fallback_model=None)
        return DirectProvider(model=MODEL, fallback_model=None)
    except Exception as e:
        log.log("search_bridge_model_error", model=MODEL, error=str(e)[:160])
        return None


def _is_local():
    try:
        return getattr(llm.get_provider("agent"), "provider_name", "") == "local"
    except Exception:
        return False


def _chat(system, user):
    provider = _model_provider()
    if provider is not None:
        return provider.chat(system, user, temperature=0.0, timeout=TIMEOUT)
    return llm.chat(system, user, temperature=0.0, kind="agent", timeout=TIMEOUT)


def _emit(cb, event):
    if cb is None:
        return
    cb(event)


def build_plan(user_message, prior_history=None, emit=None):
    """Suchplan für eine Nutzerfrage: {standalone, queries, terms, source}.

    source: "passthrough" (kein Bezug nötig), "llm" (Brücke), "heuristic".
    Immer mindestens eine Anfrage — die Suche fällt nie aus.
    """
    fallback = heuristic_plan(user_message, prior_history)
    if not ENABLED or not needs_context(user_message, prior_history):
        return fallback
    if _is_local():
        # Lokaler Kopf: ein Call kostet ~90 s Prefill — Regel-Fallback reicht.
        fallback = dict(fallback)
        fallback["source"] = "heuristic"
        return fallback

    _emit(emit, {"type": "step", "action": "SearchBridge", "status": "start",
                 "detail": "löse die Rückfrage mit dem Verlauf auf"})
    plan = None
    try:
        plan = parse_plan(_chat(_SYSTEM, _user_block(user_message, prior_history)))
    except Exception as e:
        log.log("search_bridge_error", error=str(e)[:200])
        plan = None

    if not plan or not plan.get("queries"):
        _emit(emit, {"type": "step", "action": "SearchBridge", "status": "done",
                     "detail": "ohne Ergebnis — Regel-Fallback"})
        log.log("search_bridge_fallback", chars=len(str(user_message or "")))
        out = dict(fallback)
        out["source"] = "heuristic"
        return out

    queries = plan["queries"][:MAX_QUERIES]
    _emit(emit, {"type": "step", "action": "SearchBridge", "status": "done",
                 "detail": " · ".join(f"„{q}“" for q in queries)[:160]})
    log.log("search_bridge", queries=queries, terms=plan.get("terms") or [],
            prior_msgs=len(prior_history or []),
            chars=len(str(user_message or "")))
    return {
        "standalone": plan.get("standalone") or "",
        "queries": queries,
        "terms": plan.get("terms") or [],
        "source": "llm",
    }
