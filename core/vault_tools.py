# -*- coding: utf-8 -*-
"""
Vault-Werkzeuge — die kontrollierte Tool-Schicht für Obsidian-Zugriff.

Prinzip (Architektur-Regel): Eine Datei im Vault ist DATENQUELLE, nicht
vertrauenswürdige Anweisung. Diese Schicht erzwingt:
  - Zugriff NUR innerhalb des konfigurierten Vaults (kein ../-Escape)
  - Lesen und Schreiben getrennt
  - KEINE Löschung / KEINE Umbenennung
  - Vor jeder Änderung: Backup + Revisionsnummer
  - Schreiben nur über zentrale Funktion (apply_edit) mit Diff-Bestätigung

Werkzeuge als einfache Python-Funktionen (persönlicher Sandkasten, kein Framework).
"""
import difflib
import json
import os
import re
import time

from . import config, log

# Einzige Nicht-Vault-Schreibdatei für °_Agent: Vorschläge, die den Chat überleben.
PENDING_CONTRACT = os.path.expanduser("~/.glyph/memory/pending-contract.md")


def pending_contract_abs():
    override = (os.environ.get("GLYPH_PENDING_CONTRACT") or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.abspath(os.path.expanduser(PENDING_CONTRACT))


def _is_pending_contract_ref(raw):
    s = (raw or "").strip().replace("\\", "/")
    if not s:
        return False
    aliases = {
        "pending-contract.md",
        "memory/pending-contract.md",
        "~/.glyph/memory/pending-contract.md",
        PENDING_CONTRACT.replace("\\", "/"),
        pending_contract_abs().replace("\\", "/"),
    }
    if s in aliases or os.path.basename(s) == "pending-contract.md" and (
        s.endswith("memory/pending-contract.md") or s.endswith(".glyph/memory/pending-contract.md")
    ):
        return True
    try:
        cand = os.path.realpath(os.path.expanduser(s))
    except OSError:
        return False
    return cand == os.path.realpath(pending_contract_abs())


def pending_contract_prompt_block(max_body=900):
    """Prompt-Block nur wenn echte Vorschlags-Bullets da sind."""
    path = pending_contract_abs()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            body = f.read().strip()
    except OSError:
        return None
    if not body:
        return None
    items = [
        ln for ln in body.splitlines()
        if ln.startswith("- ") and " · " in ln
    ]
    if not items:
        return None
    if len(body) > max_body:
        body = body[: max_body - 20] + "\n…[gekürzt]"
    return (
        "### Offene Vertragsvorschläge · ~/.glyph/memory/pending-contract.md\n"
        "Chat speichert nichts. Ja = Ziel schreiben + Bullet hier streichen.\n"
        + body
    )


# --- Pfad-Sicherheit ---

def _resolve_vault_path(relative_or_abs):
    """
    Löst einen Pfad relativ zu einem der konfigurierten Vaults auf und stellt sicher,
    dass er innerhalb EINES davon bleibt (Block gegen ../-Pfadmanipulation).
    Liefert absoluten, kanonischen Pfad oder None (unsicher).

    Akzeptiert u. a.:
      - absolut unter einem Vault-Root
      - relativ zum Vault-Root: "Vorlagen/Jobs/x.md"
      - mit Vault-Präfix (wie ListVaultDir/Treffer): "HSEQ Sync/Vorlagen/Jobs/x.md"
      - Index-Pfad mit führendem Slash: "/HSEQ Sync/Themen/PSA.md"
        (VaultFind speichert so — das ist kein Dateisystem-Root /HSEQ Sync)
    """
    vault_roots = [
        os.path.realpath(v)
        for v in getattr(config, "VAULT_PATHS", [config.VAULT_PATH])
        if v
    ]
    raw = (relative_or_abs or "").strip()
    if not raw:
        return None
    if _is_pending_contract_ref(raw):
        return pending_contract_abs()
    if os.path.isabs(raw):
        cand = os.path.realpath(raw)
        for v in vault_roots:
            if cand == v or cand.startswith(v + os.sep):
                return cand
        # "/HSEQ Sync/Themen" ist isabs, liegt aber nicht unter /HSEQ Sync auf Disk.
        # Nur wenn das erste Segment ein angebundener Vault-Name ist.
        stripped = raw.replace("\\", "/").lstrip("/")
        first = stripped.split("/", 1)[0] if stripped else ""
        names = {os.path.basename(v) for v in vault_roots}
        if first and first in names:
            raw = stripped
        else:
            return None

    # "HSEQ Sync/…"-Präfix → im passenden Vault-Root auflösen (erster Match)
    norm = raw.replace("\\", "/").lstrip("./")
    for v in vault_roots:
        vname = os.path.basename(v)
        if norm == vname or norm.startswith(vname + "/"):
            rest = "" if norm == vname else norm[len(vname) + 1 :]
            cand = os.path.realpath(os.path.join(v, rest)) if rest else v
            if cand == v or cand.startswith(v + os.sep):
                if os.path.lexists(cand) or rest:
                    # existierend ODER erlaubter Schreib-Zielpfad unter Root
                    if cand == v or cand.startswith(v + os.sep):
                        return cand if (os.path.exists(cand) or rest) else None
    # Relative Pfade: erster existierender Treffer unter den Roots; sonst erster Kandidat unter Root
    first_under = None
    for v in vault_roots:
        cand = os.path.realpath(os.path.join(v, norm))
        if cand == v or cand.startswith(v + os.sep):
            if first_under is None:
                first_under = cand
            if os.path.exists(cand):
                return cand
    return first_under


def _bound_vault_names():
    """Basenames der aktuell angebundenen Vault-Roots (für Fehlermeldungen)."""
    names = []
    seen = set()
    for v in getattr(config, "VAULT_PATHS", [config.VAULT_PATH]):
        if not v:
            continue
        try:
            n = os.path.basename(os.path.realpath(v))
        except OSError:
            n = os.path.basename(str(v).rstrip("/"))
        if n and n not in seen:
            seen.add(n)
            names.append(n)
    return names


def _outside_vault_error(raw):
    """Fehlertext, der angebundene Vaults nennt — sonst hält das Modell sie für ungebunden."""
    names = _bound_vault_names()
    if names:
        return (
            f"Pfad außerhalb der Vaults oder ungültig: {raw} "
            f"(angebunden: {', '.join(names)})"
        )
    return (
        f"Pfad außerhalb der Vaults oder ungültig: {raw} "
        f"(keine Vaults angebunden — Buch → Tab Vaults)"
    )


def _root_for_path(abs_path):
    """Liefert den Vault-Root, zu dem ein absoluter Pfad gehört, oder None."""
    abs_path = os.path.realpath(abs_path)
    for v in getattr(config, "VAULT_PATHS", [config.VAULT_PATH]):
        vr = os.path.realpath(v)
        if abs_path == vr or abs_path.startswith(vr + os.sep):
            return vr
    return None


def _fertig_report_frozen(rel: str) -> bool:
    """True: bestehender Fertig-Bericht (nicht Verarbeitungslog/README)."""
    r = str(rel or "").replace("\\", "/")
    if "/00 Arbeitsfluss/Fertig/" not in f"/{r}":
        return False
    base = os.path.basename(r).lower()
    return base not in ("verarbeitungslog.md", "readme.md")


def _rel_to_root(resolved):
    """Relativer Pfad eines absoluten Vault-Pfads zu seinem Vault-Root (mit Vault-Präfix)."""
    if _is_pending_contract_ref(resolved):
        return "~/.glyph/memory/pending-contract.md"
    root = _root_for_path(resolved)
    if root:
        rel = os.path.relpath(resolved, root)
        return os.path.join(os.path.basename(root), rel)
    return resolved


def _safe_md_name(path):
    """Erzwingt .md-Endung und erlaubt nur erlaubte Zeichen im Pfad."""
    if not path.endswith(".md"):
        path += ".md"
    # Erlaubt: Buchstaben/Ziffern, Leerzeichen, Bindestrich, Unterstrich, Schrägstrich, Punkt
    if re.search(r"[^A-Za-z0-9_\-./äöüÄÖÜß ]", os.path.basename(path)):
        return None
    return path


# Datei-/Pfad-Muster (heikle Privat-Spiegel im Wiki), zusätzlich zu BLOCKED_DIRS.
# HSEQ-fachliche unsafe-local-* (themen, schulung, …) bleiben indexierbar.
_HEIKLE_PATH_RE = re.compile(
    r"(behörden-recht|behoerden-recht|jugendamt|personenbezogen|"
    r"passwort|password|wiki-import|"
    r"unsafe-local-behörd|unsafe-local-behoerd|unsafe-local-familie|"
    r"unsafe-local-finanzen|"
    r"(^|/)familie[-_/]|"
    r"unterhalts?(v|vorschuss|rück|ruck|klage|urkunde))",
    re.I,
)


def _is_blocked(relpath):
    """True, wenn der Pfad in einen geschützten Ordner zeigt (case-insensitiv).
    Matching ist tolerant: Blocklist-Stichwort wird als Teilstring gegen den
    Ordnernamen geprüft (z. B. 'privat' trifft 'Privat', 'private', 'Privates').
    Zusätzlich: heikle Privat-/Behörden-Spiegel im Dateinamen (G3)."""
    low = relpath.replace(os.sep, "/").lower()
    parts = low.split("/")
    blocked = [b.lower().strip() for b in (getattr(config, "BLOCKED_DIRS", []) or []) if b.strip()]
    for p in parts:
        for b in blocked:
            if b and (b in p or p in b):
                return True
    if _HEIKLE_PATH_RE.search(low):
        return True
    return False


def _is_private(abs_path):
    """True, wenn der Pfad in einem Privat-Vault liegt (Registry mode=private).

    Gilt zusätzlich zu BLOCKED_DIRS: Privat-Vaults wie Peniel oder HSEQ Sync
    stehen dort nicht — Inhalt UND Dateinamen gehen nie an das Modell.
    """
    try:
        from . import vaults_registry as _vr

        return bool(_vr.is_private_path(abs_path))
    except Exception:
        return False


# --- Lesen / Suchen ---

# Stopwörter für Token-Suche (DE/EN-Rauschen). Datum/Zahlen werden nie gefiltert.
_SEARCH_STOP = frozenset({
    "die", "der", "das", "den", "dem", "des", "ein", "eine", "einer", "eines",
    "einen", "einem", "und", "oder", "im", "in", "am", "an", "auf", "bei",
    "mit", "von", "vom", "zu", "zum", "zur", "für", "fur", "ist", "sind",
    "war", "wie", "was", "welche", "welcher", "welches", "wo", "wer", "mir",
    "mein", "meine", "meinen", "meinem", "dir", "dein", "ich", "du", "wir",
    "uns", "auch", "noch", "nur", "nicht", "kein", "keine", "sich", "als",
    "aus", "nach", "über", "uber", "unter", "sollten", "sollte", "liegen",
    "liegt", "gibt", "habe", "hast", "hat", "haben", "sein", "seine", "ihrer",
    "ihren", "bitte", "mal", "etwa", "zb", "b", "z", "ob", "es", "dass",
    "daß", "hier", "dort", "diese", "dieser", "dieses", "doch", "schon",
    "ganz", "sehr", "mehr", "weniger", "alle", "alles", "jeder", "jede",
    "obsidian", "ordner", "sync", "dokkumente", "dokumente", "dateien",
    "datei", "notizen", "notiz", "rohnotizen", "weitere", "the", "a", "an",
    "of", "to", "for", "and", "or", "is", "are", "in", "on", "at", "by",
})


def _tokenize_query(query):
    """
    Zerlegt eine Nutzerfrage in Such-Tokens.
    Behält Daten/Zahlen (z. B. 2026-06-29); filtert Stopwörter und Minilänge.
    """
    raw = re.findall(r"[a-zA-Z0-9äöüÄÖÜß_\-./]+", (query or "").lower())
    tokens = []
    seen = set()
    for t in raw:
        t = t.strip("./-")
        if not t or t in seen:
            continue
        if any(c.isdigit() for c in t):
            tokens.append(t)
            seen.add(t)
            continue
        if len(t) < 3 or t in _SEARCH_STOP:
            continue
        tokens.append(t)
        seen.add(t)
    return tokens


def _is_archive_source_path(rel_path):
    """
    True bei OpenClaw-Wiki-Source-Kopien / Hash-Slug-Archiven — nicht die
    kanonischen Arbeitsdateien (z. B. HSEQ Sync Eingang/Fertig).
    """
    p = (rel_path or "").replace("\\", "/").lower().lstrip("/")
    if p == "sources" or p.startswith("sources/") or "/sources/" in f"/{p}/":
        return True
    if "unsafe-local" in p:
        return True
    # Hash-Slugs: …-70dc75d2-… oder …-d1978aae.md
    if re.search(r"-[0-9a-f]{8,}(?:-|\.md$)", p):
        return True
    return False


def _vault_priority_index(vault_name, vault_roots=None):
    """0 = primärer Vault (VAULT_PATHS[0]), höher = später / unbekannter."""
    roots = vault_roots or getattr(config, "VAULT_PATHS", [config.VAULT_PATH])
    name = (vault_name or "").strip()
    for i, v in enumerate(roots):
        if os.path.basename(os.path.realpath(v)) == name:
            return i
    return len(list(roots)) + 1


def path_source_rank(rel_path, vault_name=None, query=None):
    """
    Zusätzlicher Score für kanonische Arbeitsdateien vs. Archiv-Kopien.

    Ziel: Nutzer trifft gültige Live-Dateien (HSEQ Sync, Arbeitsfluss), nicht
    alte Wiki-Source-Nummern/Hash-Slugs aus memory-wiki/sources/.
    Positiv = bevorzugen, negativ = abwerten.
    """
    rel = (rel_path or "").replace("\\", "/").lstrip("/")
    rel_l = rel.lower()
    bonus = 0
    # Primär-Vault vor Neben-Vaults (Wiki, Archiv)
    pri = _vault_priority_index(vault_name)
    bonus += max(0, 24 - pri * 10)
    if _is_archive_source_path(rel):
        bonus -= 50
    # Arbeitsfluss-Roh-/Fertig-Notizen: kanonisch
    if "00 arbeitsfluss/eingang" in rel_l or "00 arbeitsfluss/fertig" in rel_l:
        bonus += 18
    q = (query or "").lower()
    if q:
        if "eingang" in q and "eingang" in rel_l:
            bonus += 12
        if "fertig" in q and "fertig" in rel_l:
            bonus += 12
    return bonus


def canon_vault_path(path):
    """Einheitlicher Trefferpfad: /VaultName/rel — ohne Disk-Home und ohne doppelte Schrägstriche."""
    p = str(path or "").replace("\\", "/").strip()
    if not p or p in (".", "/"):
        return p if p == "." else "/"
    while "//" in p:
        p = p.replace("//", "/")
    if len(p) > 1:
        p = p.rstrip("/")

    names = _bound_vault_names()
    names_sorted = sorted(names, key=len, reverse=True)

    def _from_vault_prefix(norm):
        n = norm.lstrip("/")
        for name in names_sorted:
            if n == name:
                return "/" + name
            if n.startswith(name + "/"):
                return "/" + n
        return None

    hit = _from_vault_prefix(p)
    if hit:
        return hit

    for name in names_sorted:
        marker = "/" + name
        if p == marker or p.endswith("/" + name):
            return marker
        idx = p.find(marker + "/")
        if idx >= 0:
            return p[idx:]

    abs_p = None
    if p.startswith("/"):
        try:
            abs_p = os.path.realpath(p)
        except OSError:
            abs_p = None
    if abs_p:
        for v in getattr(config, "VAULT_PATHS", [config.VAULT_PATH]) or []:
            if not v:
                continue
            try:
                vr = os.path.realpath(v)
            except OSError:
                continue
            if abs_p == vr or abs_p.startswith(vr + os.sep):
                rel = os.path.relpath(abs_p, vr).replace("\\", "/")
                name = os.path.basename(vr)
                if rel in (".", ""):
                    return "/" + name
                return "/" + name + "/" + rel

    if not p.startswith("/"):
        p = "/" + p
    return p


def _fold_name(s):
    """Klein, ß→ss, Umlaute, nur [a-z0-9], plus Form ohne Doppelbuchstaben."""
    t = (s or "").lower().replace("ß", "ss")
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue")):
        t = t.replace(a, b)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    compact = t.replace(" ", "")
    dedup = re.sub(r"(.)\1+", r"\1", compact)
    return t, compact, dedup


def _edit_distance(a, b, limit=2):
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > limit:
        return limit + 1
    if la > lb:
        a, b, la, lb = b, a, lb, la
    prev = list(range(lb + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        row_min = i
        for j, cb in enumerate(b, 1):
            val = min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + (ca != cb))
            cur.append(val)
            if val < row_min:
                row_min = val
        if row_min > limit:
            return limit + 1
        prev = cur
    return prev[-1]


# Dateiname-Füller: treffen viele DGUV-PDFs, dürfen 209-007 / Krane nicht überstimmen.
_GENERIC_NAME_TOKS = frozenset({
    "dguv", "information", "informationen", "vorschrift", "vorschriften",
    "handlungsleitfaden", "leitlinie", "leitfaden", "merkblatt",
    "regel", "regeln", "formular", "muster",
})


def _number_compacts(text):
    """Ziffernfolgen und benachbarte Paare (209-007 → 209, 007, 209007)."""
    parts = re.findall(r"\d+", text or "")
    out = []
    for p in parts:
        if len(p) >= 3:
            out.append(p)
    for i in range(len(parts) - 1):
        pair = parts[i] + parts[i + 1]
        if len(pair) >= 5:
            out.append(pair)
    return out


def _is_generic_name_tok(t):
    return bool(t) and t in _GENERIC_NAME_TOKS


def _distinctive_query_tokens(tokens):
    """Query-Tokens ohne DGUV/Information/Vorschrift — Zahlen und seltene Wörter."""
    out = []
    for t in tokens or []:
        _, t_c, _ = _fold_name(t)
        if not t_c or _is_generic_name_tok(t_c):
            continue
        out.append(t)
    return out


def _token_in_name(t, t_c, n_sp, n_c):
    if not t_c:
        return False
    if t_c in n_c or (t and t in n_sp):
        return True
    nums = re.sub(r"[^0-9]", "", t_c)
    if len(nums) >= 3 and nums in set(_number_compacts(n_sp)):
        return True
    return False


def _token_pair_score(a, b):
    """
    Treffer-Score für ein Query-Token gegen ein Dateiname-Token.
    kran/krane ja (kurze Flexion). betrieb/betriebssicherheit nein.
    vorliegen/vorlagen nein (kein Levenshtein).
    """
    if not a or not b:
        return 0
    if a == b:
        if a.isdigit() and len(a) >= 3:
            return 92
        if len(a) >= 5:
            return 88
        if len(a) >= 4:
            return 84
        if len(a) >= 3:
            return 72
        return 0
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 4 and longer.startswith(shorter) and (len(longer) - len(shorter)) <= 2:
        return 82
    return 0



def _distinctive_token_hit_compacts(dist_toks, n_sp, n_c, n_d, name_toks, n_nums):
    """Folded distinctive query tokens that match the name (token/pair/number)."""
    hits = []
    for t in dist_toks or []:
        _, t_c, t_d = _fold_name(t)
        if not t_c:
            continue
        matched = False
        if _token_in_name(t, t_c, n_sp, n_c) or t_c == n_c or t_d == n_d:
            matched = True
        else:
            for nt in name_toks:
                if _token_pair_score(t_c, nt):
                    matched = True
                    break
        if not matched:
            q_nums = set(_number_compacts(t)) | set(_number_compacts(t_c))
            if q_nums & n_nums:
                matched = True
        if matched:
            hits.append(t_c)
    return hits


def _name_match_coverage_ok(dist_toks, hit_compacts, primary):
    """
    Bei ≥2 Distinkt-Tokens: starker Score nur mit Coverage.
    - längstes Distinkt-Token trifft, oder
    - ≥2 Distinkt-Tokens treffen / strikte Majority, oder
    - Treffer = Primary-Dateiname-Token (Satzfrage: ein markantes Name-Token reicht).
    """
    if len(dist_toks or []) < 2:
        return True
    hits = list(hit_compacts or [])
    if not hits:
        return False
    hit_set = set(hits)

    def _clen(t):
        _, t_c, _ = _fold_name(t)
        return len(t_c or "")

    longest = max(dist_toks, key=_clen)
    _, long_c, _ = _fold_name(longest)
    if long_c and long_c in hit_set:
        return True
    if len(hits) >= 2:
        return True
    if len(hits) * 2 > len(dist_toks):
        return True
    if primary:
        for hc in hits:
            if hc == primary or _token_pair_score(hc, primary) >= 82:
                return True
    return False


def name_match_score(query, name):
    """
    Score 0–100: heißt der Ordner/die Datei so wie die Frage?
    Unabhängig vom Embedding-Index; Tippfehler ss/s (Arbeitssicherheit).
    Satzfragen: ein markantes Dateiname-Token reicht (nicht alle Query-Tokens).
    Zahlen (209-007) und seltene Wörter schlagen DGUV/Vorschrift/Information.
    Bei ≥2 Distinkt-Tokens: kein starker Treffer (≥70) nur durch ein einzelnes
    kurzes Neben-Token — Coverage (längstes Token / Mehrheit / Primary) nötig.
    """
    n_sp, n_c, n_d = _fold_name(name)
    if not n_c:
        return 0
    _q_sp, q_c, q_d = _fold_name(query)
    tokens = [t for t in _tokenize_query(query) if len(t) >= 3]
    if q_c and (q_c == n_c or q_d == n_d):
        return 100

    best = 0
    dist_toks = _distinctive_query_tokens(tokens)
    n_nums = set(_number_compacts(n_sp))
    q_nums = set()
    for t in tokens:
        q_nums.update(_number_compacts(t))
        _, t_c, _ = _fold_name(t)
        q_nums.update(_number_compacts(t_c))
    hit_nums = q_nums & n_nums
    if hit_nums:
        if any(len(x) >= 6 for x in hit_nums):
            best = max(best, 96)
        else:
            best = max(best, 92)

    compact_hit = bool(
        n_c and q_c and len(n_c) >= 6
        and (n_c in q_c or (len(q_c) >= 6 and q_c in n_c))
    )
    dedup_hit = bool(
        n_d and q_d and len(n_d) >= 6
        and (n_d in q_d or (len(q_d) >= 6 and q_d in n_d))
    )
    if compact_hit:
        best = max(best, 90 if dist_toks else 72)
    elif dedup_hit:
        best = max(best, 88 if dist_toks else 72)

    score_toks = dist_toks if dist_toks else tokens
    if score_toks:
        ok = True
        for t in score_toks:
            _, t_c, _t_d = _fold_name(t)
            if not _token_in_name(t, t_c, n_sp, n_c):
                ok = False
                break
        if ok:
            best = max(best, 94 if dist_toks else 80)

    if len(n_d) >= 8 and len(q_d) >= 8 and _edit_distance(q_d, n_d, 2) <= 2:
        best = max(best, 75)

    name_toks = [t for t in n_sp.split() if t]
    primary = ""
    for nt in reversed(name_toks):
        if len(nt) >= 4 and not nt.isdigit() and not _is_generic_name_tok(nt):
            primary = nt
            break
    pair_toks = dist_toks if dist_toks else tokens
    for t in pair_toks:
        _, t_c, t_d = _fold_name(t)
        if not t_c:
            continue
        if t_c == n_c or t_d == n_d:
            best = max(best, 85)
            continue
        for nt in name_toks:
            sc = _token_pair_score(t_c, nt)
            if sc and (_is_generic_name_tok(t_c) or _is_generic_name_tok(nt)):
                sc = min(sc, 68)
            if sc and nt == primary:
                sc = max(sc, 93)
            best = max(best, sc)
        if best < 70 and len(t_c) >= 8 and t_c in n_c:
            best = max(best, 60)

    # Coverage-Gate: Multi-Token-Query darf nicht nur über ein kurzes Neben-Token
    # auf ≥70 kommen (Stolperkate+Kanten → Kanten-PDF). Ein-Token-Queries unverändert.
    if best >= 70 and len(dist_toks) >= 2:
        strong_num = bool(hit_nums and any(len(x) >= 6 for x in hit_nums))
        whole_ok = bool(compact_hit or dedup_hit)
        if (
            not whole_ok
            and len(n_d) >= 8
            and len(q_d) >= 8
            and _edit_distance(q_d, n_d, 2) <= 2
        ):
            whole_ok = True
        all_dist_ok = bool(dist_toks) and all(
            _token_in_name(t, _fold_name(t)[1], n_sp, n_c) for t in dist_toks
        )
        if not (strong_num or whole_ok or all_dist_ok):
            hits = _distinctive_token_hit_compacts(
                dist_toks, n_sp, n_c, n_d, name_toks, n_nums
            )
            if not _name_match_coverage_ok(dist_toks, hits, primary):
                best = min(best, 68)
    return best


def _query_vault_hints(query):
    """Vault-Namen, die in der Frage vorkommen (auch Stopwort 'sync' in 'HSEQ Sync')."""
    _sp, q_c, _d = _fold_name(query)
    if not q_c:
        return []
    hints = []
    for v in getattr(config, "VAULT_PATHS", []) or []:
        name = os.path.basename(os.path.realpath(v) if v else "")
        if not name:
            continue
        _nsp, n_c, _nd = _fold_name(name)
        if n_c and n_c in q_c:
            hints.append(name)
    return hints


# CEO / ObsiBot hard-deny: these vault basenames are NEVER searched,
# even if registry mode is wrong or private_paths() misses them.
_SEARCH_DENY_VAULT_BASENAMES = frozenset({
    "privat",
    "peniel",
    "_recovery",
    "recovery",
})


def _is_search_denied_vault(vault_root_or_name) -> bool:
    """True if vault basename is on the hard-deny allowlist (case-insensitive)."""
    base = os.path.basename(os.path.normpath(str(vault_root_or_name or ""))).lower()
    return bool(base) and base in _SEARCH_DENY_VAULT_BASENAMES


def match_vault_entries(query, limit=16, min_score=70):
    """
    Ordner und .md/.pdf-Dateien, deren *Name* zur Frage passt (Disk, kein Index).\n    Ordner namens Vorlagen werden nicht als Treffer geführt, Dateien darin schon.

    Liefert Liste {kind, path, title, score, vault} — path kanonisch /Vault/rel.
    Private Vaults ausgelassen. Versteckte/.obsidian/backups/Blocklist wie search_vault.
    """
    q = (query or "").strip()
    if not q:
        return []
    vault_roots = [v for v in getattr(config, "VAULT_PATHS", [config.VAULT_PATH]) if v]
    try:
        from . import vaults_registry as _vr

        _priv = {os.path.realpath(p) for p in _vr.private_paths()}
    except Exception:
        _priv = set()
    hints = set(_query_vault_hints(q))
    folders = []
    files = []
    seen_f = set()
    seen_d = set()

    def _add_folder(vault_name, rel, score, name):
        leaf = (name or "").strip().lower()
        rel_leaf = (rel or "").rstrip("/").rsplit("/", 1)[-1].lower()
        if leaf == "vorlagen" or rel_leaf == "vorlagen":
            return
        path = canon_vault_path(f"{vault_name}/{rel}" if rel else vault_name)
        if path in seen_d:
            if score > 0:
                for row in folders:
                    if row["path"] == path:
                        row["score"] = max(row["score"], score)
                        break
            return
        seen_d.add(path)
        if hints and vault_name in hints:
            score += 8
        folders.append({
            "kind": "folder",
            "path": path,
            "title": name,
            "score": score,
            "vault": vault_name,
        })

    def _add_file(vault_name, rel, score, name):
        path = canon_vault_path(f"{vault_name}/{rel}")
        if path in seen_f:
            return
        seen_f.add(path)
        if hints and vault_name in hints:
            score += 8
        files.append({
            "kind": "file",
            "path": path,
            "title": name,
            "score": score,
            "vault": vault_name,
            "excerpt": "",
        })

    home_r = os.path.realpath(os.path.expanduser("~"))
    for vroot in vault_roots:
        vroot_r = os.path.realpath(vroot)
        if vroot_r in _priv or _is_search_denied_vault(vroot_r):
            continue
        if not os.path.isdir(vroot_r):
            continue
        # Home als Vault-Wurzel ist Arbeitsplatz, kein Notiz-Baum.
        # os.walk($HOME) macht die Ordner-Suche langsam und erzeugt Doppel-Pfade.
        if vroot_r == home_r:
            continue
        vault_name = os.path.basename(vroot_r)
        vscore = name_match_score(q, vault_name)
        if vscore >= 90:
            _add_folder(vault_name, "", vscore, vault_name)
        for dirpath, dirnames, filenames in os.walk(vroot_r):
            relroot = os.path.relpath(dirpath, vroot_r)
            segs = [] if relroot in (".", "") else relroot.split(os.sep)
            if segs and any(s.startswith(".") for s in segs):
                dirnames[:] = []
                continue
            if segs and "backups" in [s.lower() for s in segs]:
                dirnames[:] = []
                continue
            if segs:
                try:
                    if _is_blocked(relroot) or _is_archive_source_path(relroot):
                        dirnames[:] = []
                        continue
                except Exception:
                    pass
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d.lower() != "backups"
            ]
            if segs:
                dname = segs[-1]
                sc = name_match_score(q, dname)
                if sc >= min_score:
                    _add_folder(vault_name, relroot.replace("\\", "/"), sc, dname)
                    # Kein Dump aller Dateien in Vorlagen — Treffer nur per Dateiname.
                    if sc >= 85 and (dname or "").strip().lower() != "vorlagen":
                        for fn in filenames:
                            if fn.startswith(".") or not fn.lower().endswith((".md", ".pdf")):
                                continue
                            child_rel = os.path.join(relroot, fn).replace("\\", "/")
                            try:
                                if _is_blocked(child_rel) or _is_archive_source_path(child_rel):
                                    continue
                            except Exception:
                                pass
                            _add_file(vault_name, child_rel, max(sc - 10, min_score), fn)
            for fn in filenames:
                if fn.startswith(".") or not fn.lower().endswith((".md", ".pdf")):
                    continue
                stem, _ext = os.path.splitext(fn)
                sc = max(name_match_score(q, stem), name_match_score(q, fn))
                if sc < min_score:
                    continue
                rel = fn if relroot in (".", "") else os.path.join(relroot, fn)
                rel = rel.replace("\\", "/")
                try:
                    if _is_blocked(rel) or _is_archive_source_path(rel):
                        continue
                except Exception:
                    pass
                _add_file(vault_name, rel, sc, fn)
                parent = "" if relroot in (".", "") else relroot.replace("\\", "/")
                if parent:
                    _add_folder(
                        vault_name,
                        parent,
                        max(sc - 5, min_score),
                        os.path.basename(parent),
                    )

    folders.sort(key=lambda r: (-r["score"], r["path"]))
    files.sort(key=lambda r: (-r["score"], r["path"]))
    # Dateien zuerst — der Treffer soll die Notiz/PDF sein, nicht der Elternordner.
    max_files = min(10, limit)
    out = files[:max_files] + folders[: max(0, limit - min(len(files), max_files))]
    log.log("match_vault_entries", query=q[:80], n=len(out), folders=len(folders), files=len(files))
    return out[:limit]


def search_vault(query, limit=20, roots=None):
    """
    Durchsucht alle .md-Dateien im Vault (case-insensitive).

    Scoring (Hybrid Keyword):
      - Token-Treffer im Dateinamen/Pfad (stark gewichtet, Body-Cap)
      - Token-Treffer im Inhalt (gecappt — lange Wiki-Kopien nicht überstimmen)
      - Vault-Priorität + Abwertung von sources/unsafe-local-Hash-Archiven
      - optional exakter Phrasen-Treffer im Inhalt (falls Query kurz)

    Liefert Liste von {'path', 'abs_path', 'vault', 'hits'}. Reine Leseoperation.
    """
    query = (query or "").strip()
    if not query:
        return []
    query_l = query.lower()
    tokens = _tokenize_query(query)
    results = []
    vault_roots = [v for v in getattr(config, "VAULT_PATHS", [config.VAULT_PATH]) if v]
    if roots is not None:
        allow = set()
        for raw in roots:
            if not raw:
                continue
            try:
                allow.add(os.path.realpath(raw))
            except OSError:
                allow.add(str(raw))
        vault_roots = [v for v in vault_roots if os.path.realpath(v) in allow]
        if not vault_roots:
            return []
    try:
        from . import vaults_registry as _vr

        _priv = {os.path.realpath(p) for p in _vr.private_paths()}
    except Exception:
        _priv = set()
    for vault_i, vroot in enumerate(vault_roots):
        vroot_r = os.path.realpath(vroot)
        if vroot_r in _priv or _is_search_denied_vault(vroot_r):
            # Privat / hard-deny: kein Auto-Search (Cloud-Korpus) — nur explizites ReadNote
            continue
        vault_name = os.path.basename(vroot_r)
        for root, _dirs, files in os.walk(vroot_r):
            # Obsidian-interne Ordner + Backups ausschließen
            relroot = os.path.relpath(root, vroot_r)
            if any(seg.startswith(".") for seg in relroot.split(os.sep)):
                continue
            if "backups" in relroot.split(os.sep):
                continue
            if _is_blocked(relroot):
                continue
            for fn in files:
                if not fn.endswith(".md"):
                    continue
                fpath = os.path.join(root, fn)
                try:
                    with open(fpath, encoding="utf-8", errors="replace") as f:
                        content = f.read()
                except OSError:
                    continue
                content_l = content.lower()
                fn_l = fn.lower()
                rel = os.path.relpath(fpath, vroot_r)
                rel_l = rel.lower().replace("\\", "/")
                score = 0
                # Exakte Phrase (nur sinnvoll bei kurzen Queries, z. B. Fachbegriff)
                if len(query_l) <= 64 and " " not in query_l.strip():
                    # Body-Cap: lange Archiv-Kopien sollen reine Datums-Queries nicht dominieren
                    score += min(content_l.count(query_l), 4) * 3
                    if query_l in fn_l or query_l in rel_l:
                        score += 12
                if tokens:
                    for tok in tokens:
                        name_hit = tok in fn_l or tok in rel_l
                        c = content_l.count(tok)
                        if name_hit:
                            # Dateiname/Pfad: stark; Body nur leicht (Cap)
                            score += 12 + min(c, 3)
                        elif c:
                            score += min(c, 6)
                else:
                    # Keine Tokens (nur Stopwörter): alter Phrasen-Modus
                    score += min(content_l.count(query_l), 8)
                if not score:
                    continue
                score += path_source_rank(rel, vault_name=vault_name, query=query)
                # feiner Tie-Breaker: früherer Vault bei Gleichstand
                score += max(0, 3 - vault_i) * 0.01
                if score > 0:
                    results.append({
                        "path": rel,
                        "abs_path": fpath,
                        "vault": vault_name,
                        "hits": score,
                    })
    results.sort(key=lambda r: r["hits"], reverse=True)
    log.log("search_vault", query=query, results=len(results), tokens=tokens[:12])
    return results[:limit]


def list_vault_dir(path="", limit=200, extensions=None):
    """
    Listet Dateien und Unterordner unter einem Vault-Pfad (nur Lesen).

    path: relativ zu einem Vault-Root, optional mit Vault-Präfix
          (z. B. "HSEQ Sync/00 Arbeitsfluss/Eingang" oder "00 Arbeitsfluss/Eingang").
          Leer / "." = Top-Level aller konfigurierten Vaults.
    extensions: optional Iterable wie [".md", ".pdf"]; None = alle Dateien
                (außer versteckten/.obsidian/backups).
    limit: max. Einträge (Default 200).

    Liefert {
      status, path, vault, entries: [{name, path, type, size?, mtime?}],
      count, truncated, error?
    }.
    """
    limit = max(1, min(int(limit or 200), 1000))
    ext_set = None
    if extensions:
        ext_set = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions}

    raw = (path or "").strip()
    if raw in ("", ".", "/"):
        # Top-Level: je Vault-Root die direkten Kinder
        entries = []
        for vroot in getattr(config, "VAULT_PATHS", [config.VAULT_PATH]):
            vroot_r = os.path.realpath(vroot)
            if not os.path.isdir(vroot_r):
                continue
            if _is_private(vroot_r):
                continue
            vname = os.path.basename(vroot_r)
            try:
                names = sorted(os.listdir(vroot_r), key=lambda s: s.lower())
            except OSError:
                continue
            for name in names:
                if name.startswith("."):
                    continue
                if name.lower() == "backups":
                    continue
                full = os.path.join(vroot_r, name)
                rel_prefixed = f"{vname}/{name}"
                if _is_blocked(rel_prefixed):
                    continue
                is_dir = os.path.isdir(full)
                if not is_dir and ext_set is not None:
                    _, e = os.path.splitext(name)
                    if e.lower() not in ext_set:
                        continue
                entry = {
                    "name": name,
                    "path": rel_prefixed,
                    "type": "dir" if is_dir else "file",
                    "vault": vname,
                }
                if not is_dir:
                    try:
                        st = os.stat(full)
                        entry["size"] = st.st_size
                        entry["mtime"] = int(st.st_mtime)
                    except OSError:
                        pass
                entries.append(entry)
                if len(entries) >= limit:
                    break
            if len(entries) >= limit:
                break
        log.log("list_vault_dir", path=".", count=len(entries))
        return {
            "status": "success",
            "path": ".",
            "vault": None,
            "entries": entries[:limit],
            "count": min(len(entries), limit),
            "truncated": len(entries) > limit,
            "error": None,
        }

    resolved = _resolve_vault_path(raw)
    if not resolved:
        log.log("list_vault_dir_denied", path=raw, reason="outside_or_invalid")
        return {
            "status": "error",
            "path": raw,
            "vault": None,
            "entries": [],
            "count": 0,
            "truncated": False,
            "error": _outside_vault_error(raw),
        }
    rel = _rel_to_root(resolved)
    if _is_blocked(rel):
        return {
            "status": "error",
            "path": rel,
            "vault": None,
            "entries": [],
            "count": 0,
            "truncated": False,
            "error": f"Geschützter Ordner — Zugriff verweigert: {rel}",
        }
    if _is_private(resolved):
        log.log("list_vault_dir_denied", path=rel, reason="private")
        return {
            "status": "error",
            "path": rel,
            "vault": None,
            "entries": [],
            "count": 0,
            "truncated": False,
            "error": f"Privat-Vault — Auflisten gesperrt (Schloss): {rel}",
        }
    if not os.path.isdir(resolved):
        return {
            "status": "error",
            "path": rel,
            "vault": os.path.basename(_root_for_path(resolved) or ""),
            "entries": [],
            "count": 0,
            "truncated": False,
            "error": f"Kein Verzeichnis: {rel}",
        }

    vroot = _root_for_path(resolved)
    vname = os.path.basename(vroot) if vroot else ""
    entries = []
    try:
        names = sorted(os.listdir(resolved), key=lambda s: s.lower())
    except OSError as e:
        return {
            "status": "error",
            "path": rel,
            "vault": vname,
            "entries": [],
            "count": 0,
            "truncated": False,
            "error": str(e),
        }

    for name in names:
        if name.startswith("."):
            continue
        if name.lower() == "backups":
            continue
        full = os.path.join(resolved, name)
        child_rel = os.path.join(rel, name).replace("\\", "/")
        # blocked check auf Kind relativ zum Vault-Präfix
        if _is_blocked(child_rel):
            continue
        is_dir = os.path.isdir(full)
        # Privat-Vault, der unter einem offenen Ordner liegt: nicht mitlisten.
        if is_dir and _is_private(full):
            continue
        if not is_dir and ext_set is not None:
            _, e = os.path.splitext(name)
            if e.lower() not in ext_set:
                continue
        entry = {
            "name": name,
            "path": child_rel,
            "type": "dir" if is_dir else "file",
            "vault": vname,
        }
        if not is_dir:
            try:
                st = os.stat(full)
                entry["size"] = st.st_size
                entry["mtime"] = int(st.st_mtime)
            except OSError:
                pass
        entries.append(entry)
        if len(entries) >= limit:
            break

    truncated = len(entries) >= limit and len(names) > limit
    log.log("list_vault_dir", path=rel, count=len(entries), truncated=truncated)
    return {
        "status": "success",
        "path": rel,
        "vault": vname,
        "entries": entries,
        "count": len(entries),
        "truncated": truncated,
        "error": None,
    }


def file_hash(path=""):
    """SHA-256 einer Vault-Datei — Nur-Lesen, Ersatz für `shasum -a 256`.

    Warum es das als Werkzeug gibt: der HSEQ-Tageslauf gleicht Rohnotizen über
    ihre Prüfsumme gegen das Verarbeitungslog ab, weil Änderungsdaten von
    Sync-Diensten verfälscht werden. Die Anweisung nennt dafür `shasum -a 256` —
    ein Shell-Befehl, den der Agent-Modus nicht ausführen kann (er hat kein
    Shell-Werkzeug). Ohne Prüfsumme bleibt der Lauf beim Prüfen stehen und
    schreibt keine Berichte.

    Liefert {status, path, vault, sha256, size, mtime, error}.
    """
    import hashlib

    raw = (path or "").strip()
    resolved = _resolve_vault_path(raw)
    if not resolved:
        log.log("file_hash_denied", path=raw, reason="outside_or_invalid")
        return {
            "status": "error",
            "path": raw,
            "vault": None,
            "sha256": None,
            "size": None,
            "mtime": None,
            "error": "Pfad liegt außerhalb der erlaubten Vaults oder existiert nicht.",
        }
    if not os.path.isfile(resolved):
        log.log("file_hash_denied", path=raw, reason="not_a_file")
        return {
            "status": "error",
            "path": raw,
            "vault": None,
            "sha256": None,
            "size": None,
            "mtime": None,
            "error": "Kein reguläre Datei (Ordner?).",
        }

    h = hashlib.sha256()
    try:
        with open(resolved, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        st = os.stat(resolved)
    except OSError as e:
        log.log("file_hash_fail", path=raw, error=str(e)[:120])
        return {
            "status": "error",
            "path": raw,
            "vault": None,
            "sha256": None,
            "size": None,
            "mtime": None,
            "error": f"Datei nicht lesbar: {e}",
        }

    digest = h.hexdigest()
    rel = _rel_to_root(resolved)
    log.log("file_hash", path=rel, sha12=digest[:12], size=st.st_size)
    return {
        "status": "success",
        "path": rel,
        "vault": (rel.lstrip("/").split("/", 1)[0] if "/" in rel.lstrip("/") else None),
        "sha256": digest,
        "size": st.st_size,
        "mtime": int(st.st_mtime),
        "error": None,
    }


def read_note(path, head="agent"):
    """Liest eine Notiz (relativ zum Vault) und gibt {path, content, chars} zurück.

    head: "agent" sperrt Privat-Vaults (Graph: °_Agent private). ^_Code ruft mit
    head="code" und behält seine eigene Graph-Freigabe.
    """
    resolved = _resolve_vault_path(path)
    if not resolved or not resolved.endswith(".md"):
        raise ValueError(f"Ungültiger oder unsicherer Pfad: {path}")
    rel = _rel_to_root(resolved)
    if _is_blocked(rel):
        raise PermissionError(f"Geschützter Ordner — Zugriff verweigert: {rel}")
    if head == "agent" and _is_private(resolved):
        raise PermissionError(f"Privat-Vault — Lesen gesperrt (Schloss): {rel}")
    if not os.path.isfile(resolved):
        raise FileNotFoundError(f"Notiz nicht gefunden: {path}")
    with open(resolved, encoding="utf-8", errors="replace") as f:
        content = f.read()
    log.log("read_note", path=rel, chars=len(content))
    return {"path": rel, "content": content, "chars": len(content)}


# --- Erstellen ---

def create_note(path, content):
    """
    Legt eine neue Notiz an. Weigert sich, wenn die Datei bereits existiert
    (kein Überschreiben!). Liefert {path, created: True} oder {path, exists: True}.
    """
    if not str(content or "").strip():
        raise ValueError("Leere Notiz verboten — Wachstum, kein Leeren")
    name = _safe_md_name(path)
    if not name:
        raise ValueError(f"Ungültiger Notizname: {path}")
    resolved = _resolve_vault_path(name)
    if not resolved:
        raise ValueError(f"Pfad außerhalb des Vaults: {path}")
    if os.path.exists(resolved):
        log.log("create_note_skipped", path=path, reason="exists")
        return {"path": path, "created": False, "exists": True}
    os.makedirs(os.path.dirname(resolved), exist_ok=True)
    with open(resolved, "w", encoding="utf-8") as f:
        f.write(content)
    rel = _rel_to_root(resolved)
    log.log("create_note", path=rel, chars=len(content))
    return {"path": rel, "created": True, "exists": False}


# --- Änderungen: Diff-Vorschau + gesichertes Anwenden ---

def _revision_path(resolved):
    """Ermittelt den nächsten Revisions-Pfad für eine Datei."""
    rel = _rel_to_root(resolved)
    stem = rel.replace("/", "__").replace(".md", "")
    return os.path.join(config.BACKUP_DIR, f"{stem}.R{{n}}.md")


def propose_edit(path, new_content):
    """
    Erzeugt nur eine DIFF-VORSCHAU (ändert nichts!).
    Liefert {path, diff (Unified-Diff), changed: bool, old_len, new_len}.
    Der Nutzer entscheidet dann über apply_edit.
    """
    current = read_note(path)  # loggt lesen
    old = current["content"].splitlines(keepends=True)
    new = new_content.splitlines(keepends=True)
    diff = "".join(difflib.unified_diff(
        old, new, fromfile=f"a/{path}", tofile=f"b/{path}", lineterm=""
    ))
    log.log("propose_edit", path=current["path"], changed=(old != new),
            diff_len=len(diff))
    return {
        "path": current["path"],
        "diff": diff,
        "changed": old != new,
        "old_chars": len(current["content"]),
        "new_chars": len(new_content),
    }


def apply_edit(path, new_content):
    """
    Wendet eine Änderung NUR nach Backup + Revisionsnummer an.
    1) liest den aktuellen Inhalt  2) legt Backup an (R<n>)
    3) schreibt atomar (Temp-Datei + rename)
    Weigert sich bei gleichem Inhalt. KEIN Löschen/Umbenennen/Leeren.
    """
    if not str(new_content or "").strip():
        raise ValueError("Notiz leeren verboten — Wachstum, kein Löschen")
    try:
        current = read_note(path)
    except FileNotFoundError:
        # Neue Datei: ApplyEdit legt sie an, statt abzubrechen.
        #
        # Warum: das Modell waehlt zum Schreiben eines neuen Berichts regelmaessig
        # ApplyEdit statt CreateNote. Der FileNotFoundError liess den Aufruf
        # scheitern — und weil die Schleife bei EINEM Tool-Fehler abbricht
        # (tool_loop: "Bei Tool-Fehler abbrechen"), starb damit der ganze Job,
        # ohne dass je eine Datei entstand. Datei anlegen ist hier ungefaehrlich:
        # es gibt nichts zu ueberschreiben, also auch kein Backup.
        current = None
    resolved = _resolve_vault_path(path)
    if resolved is None:
        raise ValueError(f"Unsicherer Pfad: {path}")
    try:
        from . import vaults_registry as _vr

        if _vr.is_private_path(resolved):
            raise ValueError("Privat-Vault: Schreiben gesperrt (Schloss)")
        if _is_pending_contract_ref(resolved):
            pass
        # mode r: block writes unless under classic HSEQ job prefixes (handled by jobs)
        elif not _vr.is_writable_path(resolved):
            # still allow if vault mode is rw only
            modes_ok = False
            for v in _vr.list_vaults():
                if not v.get("enabled", True):
                    continue
                root = os.path.realpath(v["path"])
                if (resolved == root or resolved.startswith(root + os.sep)) and v.get(
                    "mode"
                ) == "rw":
                    modes_ok = True
                    break
            if not modes_ok:
                raise ValueError("Vault ist nur-lesen (r) — Schreiben gesperrt")
    except ValueError:
        raise
    except Exception:
        pass

    rel = current["path"] if current is not None else _rel_to_root(resolved)
    old_content = current["content"] if current is not None else ""
    if current is not None and _fertig_report_frozen(rel):
        # Job-Schreibschritt: ApplyEdit auf einen bestehenden Fertig-Bericht
        # hat einen echten Auditbericht (SoT-Beispiel) überschrieben. Anlegen bleibt
        # erlaubt (current is None); Log darf weiter ApplyEdit.
        log.log("apply_edit_skipped", path=rel, reason="exists")
        return {
            "path": rel,
            "applied": False,
            "created": False,
            "reason": "exists",
            "hint": "Fertig-Bericht existiert — CreateNote mit neuem Namen oder nur Log fortschreiben.",
        }
    if current is not None and old_content == new_content:
        log.log("apply_edit_skipped", path=rel, reason="no_change")
        return {"path": rel, "applied": False, "reason": "no_change"}

    # Revisionsnummer bestimmen (Sidecar-Index nötig)
    rev = _next_revision(rel)
    backup_file = ""
    if current is not None:
        backup_file = os.path.join(config.BACKUP_DIR, _backup_filename(rel, rev))
        with open(backup_file, "w", encoding="utf-8") as f:
            f.write(old_content)

    # Atomar schreiben: zuerst Temp, dann rename (kein halber Zustand)
    tmp = resolved + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_content)
    os.replace(tmp, resolved)

    log.log("apply_edit", path=rel, rev=rev, created=current is None,
            backup=_rel_to_root(backup_file) if backup_file else "",
            old_chars=len(old_content), new_chars=len(new_content))
    return {"path": rel, "applied": True, "rev": rev, "created": current is None,
            "backup": _rel_to_root(backup_file) if backup_file else ""}


def _next_revision(relpath):
    """Liest den Revisionsstand aus einem Sidecar-Index (SQLite-frei: JSON)."""
    idx_file = os.path.join(config.BACKUP_DIR, "revisions.json")
    data = {}
    if os.path.exists(idx_file):
        try:
            with open(idx_file, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}
    n = data.get(relpath, 0) + 1
    data[relpath] = n
    with open(idx_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return n


def _backup_filename(relpath, rev):
    stem = relpath.replace("/", "__").replace(".md", "")
    return f"{stem}.R{rev}.md"


def list_backups():
    """Listet gesicherte Revisionen auf (für Wiederherstellung/Transparenz)."""
    if not os.path.isdir(config.BACKUP_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(config.BACKUP_DIR)):
        if fn.endswith(".md"):
            out.append(fn)
    return out


# --- Wiki-Status (agent-digest, read-only) ------------------------------------

def _wiki_digest_path():
    """Pfad zu agent-digest.json unter memory-wiki, oder None."""
    vault_roots = getattr(config, "VAULT_PATHS", [config.VAULT_PATH])
    for v in vault_roots:
        # Konvention: memory-wiki/.openclaw-wiki/cache/agent-digest.json
        cand = os.path.join(
            v, ".openclaw-wiki", "cache", "agent-digest.json"
        )
        if os.path.isfile(cand):
            return cand
        # Basename-Match falls Vault-Root anders heißt
        if "memory-wiki" in os.path.basename(os.path.realpath(v)).lower() or \
           "openclaw" in os.path.basename(os.path.realpath(v)).lower():
            cand2 = os.path.join(
                os.path.realpath(v), ".openclaw-wiki", "cache", "agent-digest.json"
            )
            if os.path.isfile(cand2):
                return cand2
    # Fallback: bekannter Default-Pfad
    home = os.path.expanduser("~")
    fallback = os.path.join(
        home, "ObsidianVaults", "memory-wiki",
        ".openclaw-wiki", "cache", "agent-digest.json",
    )
    if os.path.isfile(fallback):
        return fallback
    return None


def wiki_status():
    """
    Read-only Stats aus agent-digest.json (memory-wiki).
    Liefert pageCounts, claimCount, claimHealth (gekürzt), page_sample_n.
    """
    path = _wiki_digest_path()
    if not path:
        log.log("wiki_status_missing")
        return {
            "ok": False,
            "available": False,
            "error": (
                "agent-digest.json nicht gefunden "
                "(erwartet unter memory-wiki/.openclaw-wiki/cache/)."
            ),
        }
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return {"ok": False, "available": False, "error": f"digest unlesbar: {e}"}

    pages = data.get("pages") or []
    # Keine vollen pages — nur Stats + kurze Stichprobe
    sample = []
    for p in pages[:8]:
        if not isinstance(p, dict):
            continue
        sample.append({
            "id": p.get("id"),
            "title": p.get("title"),
            "kind": p.get("kind") or p.get("pageType"),
            "path": p.get("path"),
        })
    claim_health = data.get("claimHealth") or {}
    # claimHealth kann verschachtelt sein — flach halten
    health_summary = {}
    if isinstance(claim_health, dict):
        for k, v in list(claim_health.items())[:12]:
            if isinstance(v, (int, float, str, bool)) or v is None:
                health_summary[k] = v
            elif isinstance(v, list):
                health_summary[k] = f"{len(v)} Einträge"
            elif isinstance(v, dict):
                health_summary[k] = {sk: sv for sk, sv in list(v.items())[:8]
                                     if isinstance(sv, (int, float, str, bool, type(None)))}
            else:
                health_summary[k] = str(type(v).__name__)

    log.log("wiki_status", path=path, pages=len(pages))
    return {
        "ok": True,
        "available": True,
        "digest_path": path,
        "pageCounts": data.get("pageCounts") or {},
        "claimCount": data.get("claimCount"),
        "claimHealth": health_summary,
        "contradictionClusters": len(data.get("contradictionClusters") or []),
        "pages_total": len(pages),
        "page_sample": sample,
        "error": None,
    }


# --- Optional: Obsidian CLI (kepano) unter Sicherheitsdach --------------------

def _obsidian_bin():
    """Pfad zur obsidian-CLI (Homebrew oder PATH), oder None."""
    import shutil
    for cand in (
        os.environ.get("OBSIDIAN_CLI"),
        "/opt/homebrew/bin/obsidian",
        "/usr/local/bin/obsidian",
        shutil.which("obsidian"),
    ):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def obsidian_open(path):
    """
    Öffnet eine Notiz in der Obsidian-App über die offizielle CLI (kepano).

    Sicherheit:
      - Pfad muss innerhalb eines erlaubten Vaults auflösbar sein (_resolve_vault_path)
      - BLOCKED_DIRS greifen wie bei read_note
      - Kein freier Shell-String aus dem Modell — nur fester CLI-Aufruf
      - Wenn CLI fehlt: klarer Fehler, kein Crash

    Liefert {ok, path, vault, opened, message}.
    """
    import subprocess
    if not path:
        raise ValueError("Pfad fehlt.")
    resolved = _resolve_vault_path(path)
    if not resolved or not resolved.endswith(".md"):
        raise ValueError(f"Ungültiger oder unsicherer Pfad: {path}")
    rel = _rel_to_root(resolved)
    if _is_blocked(rel):
        raise PermissionError(f"Geschützter Ordner — Obsidian-Open verweigert: {rel}")
    if not os.path.isfile(resolved):
        raise FileNotFoundError(f"Notiz nicht gefunden: {path}")

    root = _root_for_path(resolved)
    vault_name = os.path.basename(root) if root else ""
    # Relativ zum Vault-Root (Obsidian will vault-interne Pfade)
    note_in_vault = os.path.relpath(resolved, root) if root else path
    note_in_vault = note_in_vault.replace("\\", "/")

    bin_path = _obsidian_bin()
    if not bin_path:
        log.log("obsidian_open_skipped", path=rel, reason="cli_missing")
        return {
            "ok": False,
            "opened": False,
            "path": rel,
            "vault": vault_name,
            "message": "Obsidian-CLI nicht gefunden (obsidian binary). "
                       "In Obsidian: Settings → Advanced → Command line interface aktivieren.",
        }

    # CLI: obsidian open <file>  bzw. mit vault — Versionen variieren; try open path
    try:
        # Bevorzugt: URI-Schema open (funktioniert auch ohne CLI-Subcommands)
        # obsidian "obsidian://open?vault=...&file=..."
        from urllib.parse import quote
        uri = f"obsidian://open?vault={quote(vault_name)}&file={quote(note_in_vault)}"
        subprocess.run(
            ["open", uri],
            check=False,
            capture_output=True,
            timeout=10,
        )
        log.log("obsidian_open", path=rel, vault=vault_name, via="uri")
        return {
            "ok": True,
            "opened": True,
            "path": rel,
            "vault": vault_name,
            "message": f"Obsidian geöffnet: {vault_name} / {note_in_vault}",
        }
    except Exception as e:
        log.log("obsidian_open_error", path=rel, error=str(e))
        return {
            "ok": False,
            "opened": False,
            "path": rel,
            "vault": vault_name,
            "message": f"Obsidian-Open fehlgeschlagen: {e}",
        }
