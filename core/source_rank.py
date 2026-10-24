# -*- coding: utf-8 -*-
"""Rang von Web-Treffern nach Rechtsverbindlichkeit.

Kein harter Filter: amtliche Hosts und Rechtsprechung nach vorn, bekannte
Ratgeber nach hinten, unbekannte Seiten bleiben dazwischen.

Relevanz schlägt Stufe: Wer nicht zum Suchbegriff passt (relevance 0), rutscht
nach unten — auch wenn die Quelle amtlich ist. Die Stufe entscheidet nur bei
ähnlicher Relevanz (Gleichstand). `web.web_search` sortiert deshalb nach
`(-relevance, band)`.
"""

import re
import urllib.parse

# host → (stufe, rolle). Längere Hosts vor Suffix-Regeln prüfen.
_EXACT = {
    "gesetze-im-internet.de": (1, "gesetz"),
    "bgbl.de": (1, "gesetz"),
    "eur-lex.europa.eu": (1, "gesetz"),
    "rechtsprechung-im-internet.de": (1, "rechtsprechung"),
    "bundesarbeitsgericht.de": (1, "rechtsprechung"),
    "bundessozialgericht.de": (1, "rechtsprechung"),
    "bundesgerichtshof.de": (1, "rechtsprechung"),
    "bundesverfassungsgericht.de": (1, "rechtsprechung"),
    "bundesfinanzhof.de": (1, "rechtsprechung"),
    "bverwg.de": (1, "rechtsprechung"),
    "dejure.org": (1, "rechtsprechung"),
    "openjur.de": (1, "rechtsprechung"),
    "baua.de": (2, "behoerde"),
    "gmbl.de": (2, "behoerde"),
    "umweltbundesamt.de": (2, "behoerde"),
    "komnet.nrw.de": (2, "behoerde"),
    "dakks.de": (2, "behoerde"),
    "laga-online.de": (2, "behoerde"),
    "lawa.de": (2, "behoerde"),
    "echa.europa.eu": (2, "behoerde"),
    "recht.nrw.de": (2, "behoerde"),
    "gesetze-bayern.de": (2, "behoerde"),
    "landesrecht-bw.de": (2, "behoerde"),
    "gesetze.berlin.de": (2, "behoerde"),
    "din.de": (3, "norm"),
    "beuth.de": (3, "norm"),
    "iso.org": (3, "norm"),
    "vfdb.de": (3, "norm"),
    "vds.de": (3, "norm"),
    "verbraucherzentrale.de": (5, "ratgeber"),
    "wohnschutz.com": (5, "ratgeber"),
}

# Landes- und Bundesportale, wenn sie in der offenen Suche auftauchen.
_LAND_SUFFIXES = (
    "niedersachsen.de",
    "nrw.de",
    "bayern.de",
    "sachsen.de",
    "sachsen-anhalt.de",
    "thueringen.de",
    "hessen.de",
    "rlp.de",
    "saarland.de",
    "berlin.de",
    "hamburg.de",
    "bremen.de",
    "schleswig-holstein.de",
    "mecklenburg-vorpommern.de",
    "baden-wuerttemberg.de",
    "brandenburg.de",
    "bund.de",
)

def preferred_domains():
    """Hosts für die zusätzliche Amtssuche. Apex zieht Unterdomains mit."""
    hosts = [h for h, (stufe, _rolle) in _EXACT.items() if stufe <= 2]
    hosts.append("dguv.de")
    return hosts


def classify(url):
    """None, wenn der Host keine Stufe hat. Sonst {stufe, rolle}."""
    host = _host(url)
    if not host:
        return None
    path = _path(url)
    exact = _match_exact(host)
    if exact:
        return {"stufe": exact[0], "rolle": exact[1]}
    if host == "dguv.de" or host.endswith(".dguv.de"):
        return _dguv(path)
    for suffix in _LAND_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            return {"stufe": 2, "rolle": "behoerde"}
    return None


def annotate(row):
    """Kopie der Zeile mit stufe/rolle, wenn bekannt."""
    if not isinstance(row, dict):
        return row
    info = classify(row.get("url"))
    if not info:
        return row
    item = dict(row)
    item["stufe"] = info["stufe"]
    item["rolle"] = info["rolle"]
    return item


def band(row):
    """Sortierband: 1–4 amtlich, dann offen, Ratgeber zuletzt."""
    if not isinstance(row, dict):
        return 4
    rolle = row.get("rolle")
    stufe = row.get("stufe")
    if rolle == "ratgeber" or stufe == 5:
        return 5
    if isinstance(stufe, int) and 1 <= stufe <= 4:
        return stufe - 1
    return 4


# --- Relevanz: passt der Treffer zum Suchbegriff? ---------------------------
# Füllwörter und Frage-/Bitte-Formen, die im Inhalt nichts sagen.
_STOPWORDS = frozenset(
    """
    der die das dem den des ein eine einen einer einem eines ist sind war waren
    und oder aber wie was wer wann wo welche welcher welches nicht ja nein auch
    nur noch dann wenn weil damit daher sowie gemäß laut mit ohne von vom zum zur
    im am an auf aus bei bis durch gegen um zu über unter nach vor für fuer
    the and or of for to in on at a an is are was were
    bitte such suche suchen ich mir mich dir dich uns sie ihr gerne mal
    zeig zeige zeigen kannst du einmal
    """.split()
)


# Deutsche Wortstämme: typische Endungen abtrennen („pruefen“/„pruefung“ →
# „pruf“), damit Formen desselben Stamms zusammenfinden. Nie unter 4 Zeichen.
_STEM_SUFFIXES = (
    "ungen", "ungen", "heit", "keit", "lich", "isch", "ung", "end", "ern",
    "ten", "em", "en", "er", "es", "st", "e", "n", "s", "t",
)


def _stem(word):
    for suf in _STEM_SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 4:
            return word[: -len(suf)]
    return word


def _fold(text):
    return (
        str(text or "")
        .lower()
        .replace("ä", "a")
        .replace("ö", "o")
        .replace("ü", "u")
        .replace("ß", "ss")
    )


def query_terms(query):
    """Bedeutsame Suchbegriffe (Stämme, Stoppwörter raus, ohne Duplikate)."""
    words = re.findall(r"[a-z0-9§]+", _fold(query))
    out = []
    seen = set()
    for w in words:
        if len(w) < 3 or w in _STOPWORDS:
            continue
        stem = _stem(w)
        if len(stem) < 3 or stem in _STOPWORDS or stem in seen:
            continue
        seen.add(stem)
        out.append(stem)
    return out


def _is_word_char(ch):
    # Bindestrich zählt mit: „coronavirus-testverordnung“ ist KEIN eigenes „test“.
    return ch.isalnum() or ch == "-"


def _word_hit(term, hay):
    """2 = eigenes Wort, 1 = Teil eines zusammengesetzten Worts, 0 = fehlt."""
    if not term or not hay:
        return 0
    for m in re.finditer(re.escape(term), hay):
        before = hay[m.start() - 1] if m.start() > 0 else " "
        after = hay[m.end()] if m.end() < len(hay) else " "
        if not _is_word_char(before) and not _is_word_char(after):
            return 2
    if term in hay:
        return 1
    return 0


def relevance(query, row):
    """0.0–1.0: wie sehr passt der Treffer zur Anfrage.

    Titel zählt doppelt, Snippet/URL einfach. Unbekanntes (0.0) wird nicht
    verworfen, aber von jedem relevanten Treffer überholt.
    """
    if not isinstance(row, dict):
        return 0.0
    terms = query_terms(query)
    if not terms:
        return 0.0
    title = _fold(row.get("title"))
    rest = _fold(f"{row.get('snippet') or ''} {row.get('url') or ''}")
    hits = 0
    for t in terms:
        in_title = _word_hit(t, title)
        if in_title:
            hits += in_title * 2
        else:
            hits += _word_hit(t, rest)
    return round(hits / (4.0 * len(terms)), 3)


def rank_key(query, row):
    """Sortierschlüssel: Relevanz zuerst, Stufe bricht den Gleichstand."""
    return (-relevance(query, row), band(row))



def _dguv(path):
    p = (path or "").lower()
    if "regel" in p or "information" in p or "publikation" in p:
        return {"stufe": 3, "rolle": "erlaeuterung"}
    return {"stufe": 4, "rolle": "uvv"}


def _match_exact(host):
    if host in _EXACT:
        return _EXACT[host]
    for known, info in _EXACT.items():
        if host.endswith("." + known):
            return info
    return None


def _host(url):
    try:
        host = (urllib.parse.urlparse(str(url or "")).hostname or "").lower()
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _path(url):
    try:
        return urllib.parse.urlparse(str(url or "")).path or ""
    except Exception:
        return ""
