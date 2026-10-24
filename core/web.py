# -*- coding: utf-8 -*-
"""
Kontrollierte Web-Recherche (Ausbaustufe).

Wichtige Sicherheitsregel: Es gehen NUR bereinigte Suchanfragen an den
Webdienst (Exa, TinyFish). NIEMALS private Vault-Inhalte oder ungefilterte
Dokumente in die Suchanfrage einbetten. Der Aufrufer (Tool-Loop) bestätigt
die Anfrage, bevor sie rausgeht.

Zwei unabhängige Quellen — Suche default parallel (source=both):
  - Exa      -> EXA_API_KEY
  - TinyFish -> TINYFISH_API_KEY (Suche + Extract/Fetch)

Keys werden aus der Umgebung gelesen (core/dotenv.py lädt glyph-agent/.env) —
nicht fest im Code.
"""
import json
import os
import socket
import time
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- Exa --------------------------------------------------------------------
EXA_ENDPOINT = os.environ.get("EXA_ENDPOINT", "https://api.exa.ai/search")


def _exa_api_key():
    key = os.environ.get("EXA_API_KEY", "")
    if not key:
        raise RuntimeError(
            "EXA_API_KEY nicht gesetzt. Bitte in glyph-agent/.env bereitstellen."
        )
    return key


def search_exa(query, count=5, start_published_date=None, include_domains=None):
    """
    Führt eine Exa-Suche durch. query darf nur anonymisierte/öffentliche
    Suchbegriffe enthalten. Liefert Liste von {title, url, snippet}.
    """
    payload = {
        "query": query,
        "numResults": count,
        # Inhalte anfordern, damit Snippets/Highlights gefüllt werden (sonst liefert
        # Exa leere Snippet-Texte und selbst gute Modelle können nichts auswerten).
        "contents": {"text": True, "highlights": True},
        "highlight": {"num_sentences": 2},
    }
    domains = _domain_list(include_domains)
    if domains:
        payload["includeDomains"] = domains
    if start_published_date:
        payload["startPublishedDate"] = start_published_date
    req = urllib.request.Request(
        EXA_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": _exa_api_key(),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    results = []
    for r in data.get("results", []):
        snip = r.get("snippet") or ""
        # Exa liefert Highlights in r['highlights'] als Liste; fallback auf text.
        if not snip and r.get("highlights"):
            snip = " ".join(r["highlights"][:2])
        if not snip and r.get("text"):
            snip = r["text"][:300]
        results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": snip[:400],
        })
    return results


# --- TinyFish ---------------------------------------------------------------
# Zwei Endpoints: Suche + URL-Extraktion/Fetch. Key via TINYFISH_API_KEY.
TINYFISH_SEARCH = "https://api.search.tinyfish.ai"
TINYFISH_EXTRACT = "https://agent.tinyfish.ai/v1/automation/run-sse"
TINYFISH_FETCH = "https://api.fetch.tinyfish.ai"


def _tinyfish_api_key():
    key = os.environ.get("TINYFISH_API_KEY", "")
    if not key:
        raise RuntimeError(
            "TINYFISH_API_KEY nicht gesetzt. Bitte in glyph-agent/.env bereitstellen."
        )
    return key


def search_tinyfish(query, count=5, location="DE", language="de", include_domains=None):
    """
    Websuche über TinyFish. Liefert Liste von {title, url, snippet}.
    """
    q = urllib.parse.quote(query)
    url = f"{TINYFISH_SEARCH}?query={q}&location={location}&language={language}"
    domains = _domain_list(include_domains)
    if domains:
        url += "&include_domains=" + urllib.parse.quote(",".join(domains))
    req = urllib.request.Request(url, headers={"X-API-Key": _tinyfish_api_key()})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    # TinyFish-Suchen liefern je nach Antwortform entweder eine Liste oder
    # ein Objekt mit "results". Tolerant normalisieren.
    rows = data if isinstance(data, list) else data.get("results", [])
    out = []
    for r in rows[:count]:
        if isinstance(r, str):
            out.append({"title": "", "url": r, "snippet": ""})
            continue
        out.append({
            "title": r.get("title", ""),
            "url": r.get("url", r.get("link", "")),
            "snippet": r.get("snippet", r.get("description", "")) or "",
        })
    return out


def extract_tinyfish(url, goal, timeout=None):
    """
    Besucht eine konkrete URL und extrahiert strukturierte Daten (JSON) nach
    `goal`. Hauptnutzen von TinyFish: Navigation + Extraktion auf Zielseite.

    time-bounded: Ein hänger/zu-langsamer PDF- oder Seiten-Abruf darf die gesamte
    Agenten-Antwort nicht blockieren. `timeout` (Sekunden, Default 12) erzwingt
    einen harten Abbruch: nach Ablauf wird KEIN weiteres COMPLETE abgewartet,
    sondern sofort ein schnelles {"error": "timeout"} geliefert, damit der
    Tool-Loop weiterarbeiten kann statt im ReAct-Nachlauf 40+ s festzuhängen.
    """
    if not url or not goal:
        raise RuntimeError("extract_tinyfish braucht url und goal.")
    timeout = timeout if timeout is not None else 12
    payload = json.dumps({"url": url, "goal": goal}).encode("utf-8")
    req = urllib.request.Request(
        TINYFISH_EXTRACT,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": _tinyfish_api_key(),
        },
        method="POST",
    )
    try:
        # SSE wird hier konsequent als Ganzes eingelesen, aber NUR bis `timeout`.
        # PDFs/JS-Seiten, die nicht in der Zeit ein COMPLETE liefern, werden so
        # schnell abgebrochen statt den Request unnötig am Leben zu halten.
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except (socket.timeout, TimeoutError):
        return {"error": f"timeout nach {timeout}s", "url": url}
    except Exception as e:
        return {"error": f"fetch fehlgeschlagen: {e}", "url": url}
    found = None
    deadline = time.monotonic() + timeout
    for line in raw.splitlines():
        if time.monotonic() > deadline:
            return {"error": f"timeout nach {timeout}s (Parsing)", "url": url}
        line = line.strip()
        if not line.startswith("data: "):
            continue
        try:
            d = json.loads(line[6:])
        except Exception:
            continue
        if d.get("type") == "COMPLETE":
            found = d.get("result")
        elif d.get("type") == "ERROR" or d.get("status") == "FAILED":
            return {"error": line[6:]}
    return found if found is not None else {"error": "kein COMPLETE", "url": url}


def fetch_tinyfish(url, fmt="markdown", timeout=30):
    """
    Holt den Inhalt einer URL (markdown|text|html). Liefert Text/JSON.
    """
    payload = json.dumps({"urls": [url], "format": fmt}).encode("utf-8")
    req = urllib.request.Request(
        TINYFISH_FETCH,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": _tinyfish_api_key(),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def browse_url(url, goal=None, timeout=None):
    """
    Wrapper um TinyFish Extract mit Ziel „Zusammenfassung“.
    Für Überblick ohne eigenes JSON-Schema (Tool BrowseUrl).
    """
    if not url:
        raise RuntimeError("browse_url braucht url.")
    g = (goal or "").strip() or (
        "Fasse die Seite knapp zusammen: Titel, Kernaussagen (3–8 Bulletpoints), "
        "wichtige Zahlen/Daten, und nenne die Quelle (URL). Antworte als JSON "
        "mit keys: title, summary, bullets (array of strings), key_facts (array)."
    )
    result = extract_tinyfish(url, g, timeout=timeout)
    return {"url": url, "goal": g, "result": result}


# --- Dispatch (für Tool-Registry) ------------------------------------------
# So viele Zeichen Volltext werden an die Top-Treffer gehängt (wie Vault-Top-2).
TOP_TEXT_CHARS = 8000
# Aufgefüllt werden nur die relevantesten Treffer einer Suche.
TOP_TEXT_ROWS = 2
# Ein hänger Fetch darf die Suche nicht blockieren.
TOP_TEXT_TIMEOUT = 12


def web_search(query, count=5, source="both", include_domains=None, attach_top=TOP_TEXT_ROWS):
    """
    Kontrollierte Websuche. Default: Exa und TinyFish parallel, URLs mergen.
    source: "both" (Standard) | "exa" | "tinyfish".
    Ohne include_domains: offene Suche; zusätzlich Amtssuche NUR bei
    Rechts-/HSEQ-Fragen (research.is_legal_query). Sortierung nach Relevanz
    zur Anfrage, die Stufe bricht nur den Gleichstand — amtliche Treffer ohne
    Bezug zum Suchbegriff rutschen nach unten, nichts wird verworfen.
    Die `attach_top` relevantesten Treffer werden zusätzlich als Volltext
    (`text`, max. TOP_TEXT_CHARS Zeichen) angehängt, damit aus Inhalten
    geantwortet wird statt aus Snippets.
    include_domains bleibt ein harter Filter (Apfel-Fallback) ohne Ranking.
    query darf nur anonymisierte Suchbegriffe enthalten.
    """
    src = (source or "both").strip().lower()
    if src not in ("exa", "tinyfish", "both"):
        src = "both"
    if include_domains:
        return _search_sources(query, count, src, include_domains)
    ranked = _search_ranked(query, count, src)
    return _attach_contents(ranked, attach_top)


def _search_sources(query, count, src, include_domains):
    if src == "tinyfish":
        return search_tinyfish(query, count=count, include_domains=include_domains)
    if src == "exa":
        return search_exa(query, count=count, include_domains=include_domains)
    return _search_both(query, count=count, include_domains=include_domains)


def _search_both(query, count=5, include_domains=None):
    """Exa + TinyFish gleichzeitig. Ein Ausfall lässt die andere Quelle stehen."""
    buckets = _gather(query, count, ("exa", "tinyfish"), [include_domains])
    return _merge_search_rows(buckets[("exa", 0)], buckets[("tinyfish", 0)], count)


def _search_ranked(query, count, src):
    """Offene Suche, bei Rechts-/HSEQ-Fragen zusätzlich Amtssuche.

    Sortierung: Relevanz zur Anfrage zuerst (`source_rank.rank_key`), die
    Quellen-Stufe entscheidet nur bei Gleichstand. Treffer ohne Bezug bleiben
    erhalten, landen aber hinter jedem passenden Treffer.
    """
    from .research import is_legal_query
    from .source_rank import annotate, preferred_domains, rank_key, relevance

    names = ("exa", "tinyfish") if src == "both" else (src,)
    passes = [None]
    if is_legal_query(query):
        passes.append(preferred_domains())
    buckets = _gather(query, count, names, passes)
    flat = []
    for name in names:
        for idx in range(len(passes)):
            flat.extend(buckets.get((name, idx)) or [])
    deduped = _dedupe_rows(flat)
    ranked = []
    for row in deduped:
        item = annotate(row)
        if isinstance(item, dict):
            item["relevance"] = relevance(query, item)
        ranked.append(item)
    ranked.sort(key=lambda r: rank_key(query, r))
    return ranked[:count]


def _attach_contents(rows, top):
    """Volltext der `top` relevantesten Treffer nachladen (TinyFish Fetch)."""
    try:
        top = int(top)
    except (TypeError, ValueError):
        top = 0
    targets = [r for r in (rows or [])[:top] if isinstance(r, dict) and r.get("url")]
    if not targets:
        return rows

    def _fetch(row):
        try:
            raw = fetch_tinyfish(row["url"], "markdown", timeout=TOP_TEXT_TIMEOUT)
        except Exception:
            return ""
        return _as_text(raw)

    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
        texts = list(pool.map(_fetch, targets))
    text_by_row = {id(r): t for r, t in zip(targets, texts)}
    out = []
    for row in rows:
        item = dict(row)
        text = text_by_row.get(id(row))
        if text:
            item["text"] = text[:TOP_TEXT_CHARS]
        out.append(item)
    return out


def _as_text(raw):
    """Beliebige Fetch-Antwort (Text/JSON/Listen) tolerant zu reinem Text."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                return _as_text(json.loads(s))
            except Exception:
                return s
        return s
    if isinstance(raw, dict):
        for key in ("content", "text", "markdown", "body", "result", "data"):
            v = raw.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for key in ("results", "pages", "documents"):
            v = raw.get(key)
            if isinstance(v, list):
                return "\n".join(t for t in (_as_text(x) for x in v) if t)
        return ""
    if isinstance(raw, list):
        return "\n".join(t for t in (_as_text(x) for x in raw) if t)
    return str(raw)


def _gather(query, count, names, domain_passes):
    """(name, index) → Zeilen. Index 0 ist der erste Durchgang."""
    tasks = [(name, idx, dom) for name in names for idx, dom in enumerate(domain_passes)]

    def _run(name, domains):
        if name == "exa":
            return search_exa(query, count=count, include_domains=domains)
        return search_tinyfish(query, count=count, include_domains=domains)

    out = {(name, idx): [] for name, idx, _dom in tasks}
    with ThreadPoolExecutor(max_workers=max(1, len(tasks))) as pool:
        futs = {pool.submit(_run, name, dom): (name, idx) for name, idx, dom in tasks}
        for fut in as_completed(futs):
            key = futs[fut]
            try:
                out[key] = fut.result() or []
            except Exception:
                out[key] = []
    return out


def _merge_search_rows(exa_rows, tinyfish_rows, count=5):
    return _dedupe_rows(list(exa_rows or []) + list(tinyfish_rows or []))[:count]


def _dedupe_rows(rows):
    seen = set()
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        url = _norm_url(row.get("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        item = dict(row)
        item["url"] = url
        out.append(item)
    return out


def _norm_url(url):
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        p = urllib.parse.urlparse(raw)
    except Exception:
        return raw
    if not p.scheme or not p.netloc:
        return raw
    host = p.netloc.lower()
    path = p.path.rstrip("/") or ""
    query = ("?" + p.query) if p.query else ""
    return f"{p.scheme.lower()}://{host}{path}{query}"


def _domain_list(include_domains):
    if not include_domains:
        return []
    if isinstance(include_domains, str):
        parts = include_domains.split(",")
    else:
        parts = list(include_domains)
    out = []
    seen = set()
    for p in parts:
        d = str(p or "").strip().lower()
        if d.startswith("www."):
            d = d[4:]
        if not d or d in seen:
            continue
        seen.add(d)
        out.append(d)
    return out
