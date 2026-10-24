# -*- coding: utf-8 -*-
"""
Recherche-Policy (OpenClaw RECHERCHE.md → glyph-agent).

  Suche = Exa + TinyFish parallel (WebSearch, source=both)
  Fein  = TinyFish ExtractUrl / FetchUrl — konkrete URL

Keine privaten Vault-Inhalte in Suchqueries. Reine Policy-Helfer, kein Netzwerk.
"""
import re

# Signale für „Fein“: Nutzer hat schon eine URL oder will explizit extrahieren.
FINE_SIGNALS = (
    "http://", "https://", "www.",
    "extrahier", "zieh mir", "von der seite", "von dieser url",
    "zielseite", "tabelleninhalt", "fetch", "scrape",
)

# Signale für grobe Websuche (zusätzlich zu routing.CURRENCY_SIGNALS).
COARSE_OK = True  # Default: grobe Suche wenn need_web

# Signale für „Rechts-/HSEQ-Frage“: nur dann läuft zusätzlich die Amtssuche
# (gesetze-im-internet, Gerichte, Behörden, DIN, dguv.de). Ohne Signal bleibt
# es bei der offenen Suche — sonst landen zu „test“ die Testverordnung und
# DGUV-Testseiten oben, nur weil sie amtlich sind.
# Lange Signale als Substring (auch „gesetzlich“, „Prüfung“), kurze/mehrdeutige
# nur als eigenes Wort („din“ nicht in „finden“, „iso“ nicht in „isoliert“).
LEGAL_SUBSTRINGS = (
    "gesetz", "verordnung", "vorschrift", "regel", "pflicht", "haftung",
    "bussgeld", "bußgeld", "strafe", "gericht", "urteil", "rechtsprechung",
    "klage", "anspruch", "zulassung", "genehmig", "zertifi", "pruf", "prüf",
    "nachweis", "dokumentation", "arbeitsschutz", "unfallverhut",
    "unfallverhüt", "gefahrstoff", "betriebssicherheit", "arbschg", "betrsv",
    "gefstoff", "satzung", "behoerde", "behörde", "auflage", "inspektion",
    "audit", "gepruf", "geprüf", "sicherheitsdatenblatt", "gefahrdung",
    "gefährdung", "arbeitssicherheit", "konformität", "konformitat",
    "compliance",
)
LEGAL_WORDS = (
    "din", "iso", "norm", "normen", "dguv", "asr", "trgs", "trbs", "bgr",
    "vdi",
)


def is_legal_query(query: str) -> bool:
    """True, wenn die Anfrage nach Recht/Regelwerk klingt → Amtssuche zulassen."""
    q = (query or "").lower()
    if any(sig in q for sig in LEGAL_SUBSTRINGS):
        return True
    return any(
        re.search(r"(?<![a-z0-9äöüß])" + re.escape(w) + r"(?![a-z0-9äöüß])", q)
        for w in LEGAL_WORDS
    )


def classify_web_depth(query: str) -> str:
    """
    Liefert 'fine' | 'coarse'.
    fine  → TinyFish extract/fetch bei konkreter URL
    coarse → WebSearch (Exa + TinyFish)
    """
    q = (query or "").lower()
    for sig in FINE_SIGNALS:
        if sig in q:
            return "fine"
    # URL-Muster
    if re.search(r"https?://\S+", query or ""):
        return "fine"
    return "coarse"


def extract_urls(query: str):
    """Alle http(s)-URLs aus der Frage."""
    return re.findall(r"https?://[^\s<>\"']+", query or "")


def default_web_source(query: str) -> str:
    """source= für web_search: immer beide (Exa + TinyFish)."""
    return "both"


def policy_prompt_snippet(web=None) -> str:
    """Kurzer Text für System-Prompt.

    web: None = Jobs/Default; 'open' = ohne Apfel; 'apple' = Ordner-Suche an.
    TinyFish + Exa immer.
    """
    base = (
        "RECHERCHE: WebSearch = Exa und TinyFish parallel. "
        "TinyFish ExtractUrl/FetchUrl = feine Zielseite (konkrete URL). "
        "Suchanfrage = eigenständige Begriffe (Fachbegriff, Regelwerk, Behörde), "
        "nie die nackte Rückfrage: „das“, „es“, „wie oft“ allein liefern fremde "
        "Themen. Bei Nachfragen den gemeinten Fachbegriff aus dem Verlauf nennen "
        "(die Suchbrücke tut das automatisch, wenn du es vergisst). "
        "Keine privaten Vault-Texte in Suchanfragen. "
        "Relevanz vor Quellenrang: Nur Treffer als Beleg nutzen, die zum "
        "Suchbegriff passen (Feld relevance > 0) — keine Quelle nur, weil sie "
        "amtlich ist. Nicht passende Treffer nicht aufzählen. "
        "Rechtsfragen: Feld stufe nutzen. Stufe 1 Gesetz/Verordnung und Rechtsprechung, "
        "Stufe 2 Technische Regeln und Behörden (baua.de, gmbl.de, umweltbundesamt.de, "
        "komnet.nrw.de, Landesportale). DGUV-Vorschrift ist Satzungsrecht, "
        "DGUV-Regel nur Erläuterung. Nur bei Rechtsfragen mindestens eine Stufe 1 "
        "oder 2 zitieren, sonst als Meinung kennzeichnen — bei Sachfragen zählt "
        "der Inhalt, nicht die Quellen-Stufe. "
        "Ratgeber nicht als Recht. Normtitel ohne Text im Treffer nicht als Beleg."
    )
    if web == "open":
        return (
            base
            + " Ordner-Suche aus: allgemeine Suche, Internet, soziale Netze."
        )
    if web == "apple":
        return (
            base
            + " Ordner-Suche an: KomNet und DGUV. "
            "Keine allgemeine Websuche, keine sozialen Netze."
        )
    return base
