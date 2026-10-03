#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Vault-Index automatisch frisch halten (vault_index.json, bge-m3).

Der Vektorindex für die semantische Vault-Suche wurde bisher nur von Hand
gebaut (Einzeiler aus scripts/index_hygiene.py). Am 2026-10-02 stand er auf
dem 08.09. — 78 geänderte Notizen fehlten der semantischen Suche, alte
Vorlagen rangierten vor den echten Berichten. Dieses Skript schließt die Lücke:

1. **Stale-Erkennung:** jüngste .md-Änderung in den konfigurierten Vaults
   gegen den Indexstand (mtime) — nur bei Bedarf wird gebaut.
2. **Inkrementeller Aufbau** über core.retrieval.build_index_from_vault
   (Hash-Skip: unveränderte Dateien kosten nichts).
3. **Status** nach logs/vault_index.status.json (Stand, Alter, Umfang, Lauf) —
   Grundlage für Anzeige/Diagnose.
4. **Selbst-Drosselung:** höchstens alle VAULT_INDEX_REFRESH_H Stunden
   (Standard 6), `--force` hebt das auf.

Guard: Ohne erreichbares Ollama (bge-m3) wird NICHT gebaut — beim Aufbau ohne
Embeddings würden Chunks still verworfen und der Hash sie danach „erledigt"
markieren. Dann gilt: nächster Versuch im nächsten Slot.

Aufruf: über scripts/jobs-catchup.sh (alle 15 Min, hier gedrosselt) oder manuell:
  python3 scripts/vault_index_refresh.py            # frisch halten (gedrosselt)
  python3 scripts/vault_index_refresh.py --status   # nur anzeigen
  python3 scripts/vault_index_refresh.py --force    # drosselung übergehen

Copyright (c) 2026 Alexander Hubert — MIT License
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

LOGS = os.path.join(ROOT, "logs")
STATUS_PATH = os.path.join(LOGS, "vault_index.status.json")
LOCK_PATH = os.path.join(LOGS, "vault_index_refresh.lock")
THROTTLE_H = float(os.environ.get("VAULT_INDEX_REFRESH_H", "6") or 6)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _index_path() -> str:
    from core import retrieval

    return os.path.realpath(retrieval.INDEX_PATH)


def _ollama_ok() -> bool:
    """bge-m3 braucht Ollama — ohne Embeddings kein sinnvoller Aufbau."""
    from core import retrieval

    try:
        with urllib.request.urlopen(f"{retrieval.OLLAMA_URL}/api/tags", timeout=4) as r:
            return r.status == 200
    except Exception:
        return False


def _vault_roots():
    """Index-Roots wie der Aufbau: alle Vaults außer privaten."""
    from core import config, vaults_registry

    try:
        config.reload_vault_paths()
    except Exception:
        pass
    roots = [os.path.realpath(p) for p in (getattr(config, "VAULT_PATHS", None) or []) if p]
    try:
        priv = {os.path.realpath(p) for p in vaults_registry.private_paths()}
        roots = [r for r in roots if r not in priv]
    except Exception:
        pass
    return [r for r in roots if os.path.isdir(r)]


def _newest_md(roots) -> tuple:
    """(mtime, pfad) der jüngsten .md-Datei — grober, aber sicherer Stale-Maßstab."""
    best = (0.0, "")
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            relroot = os.path.relpath(dirpath, root)
            segs = relroot.split(os.sep)
            if segs != ["."] and (any(s.startswith(".") for s in segs) or "backups" in segs):
                dirnames[:] = []
                continue
            for fn in filenames:
                if not fn.endswith(".md"):
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    m = os.path.getmtime(full)
                except OSError:
                    continue
                if m > best[0]:
                    best = (m, full)
    return best


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_status(status: dict) -> None:
    os.makedirs(LOGS, exist_ok=True)
    tmp = STATUS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, STATUS_PATH)


def _index_facts():
    """Stand/Umfang des Index — ohne das 24-MB-JSON zu laden (docs via Zeilenscan)."""
    ip = _index_path()
    try:
        st = os.stat(ip)
    except OSError:
        return {"vorhanden": False}
    # docs[] enthält pro Eintrag "path" — eindeutige Pfade = Doc-Anzahl.
    pfade = set()
    try:
        with open(ip, encoding="utf-8") as f:
            data = json.load(f)
        docs = data.get("docs") or []
        pfade = {d.get("path") for d in docs}
        chunks = len(docs)
    except (OSError, json.JSONDecodeError):
        chunks = None
    return {
        "vorhanden": True,
        "stand": datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds"),
        "alter_h": round((time.time() - st.st_mtime) / 3600, 1),
        "chunks": chunks,
        "dokumente": len(pfade) if pfade else None,
        "embed_model": "bge-m3",
    }


def status_anzeigen() -> None:
    facts = _index_facts()
    vor = _load_json(STATUS_PATH) or {}
    if not facts.get("vorhanden"):
        print("Vault-Index: KEIN Index vorhanden")
    else:
        print(
            "Vault-Index: Stand %(stand)s (%(alter_h)sh) · %(dokumente)s Dokumente · "
            "%(chunks)s Chunks · Modell %(embed_model)s" % facts
        )
    if vor.get("letzter_aufbau"):
        print(
            "letzter Aufbau: %s · neu %s · unverändert %s · entfernt %s · fehlgeschlagen %s · %ss"
            % (
                vor.get("letzter_aufbau"),
                vor.get("neu_indexiert"),
                vor.get("unchanged"),
                vor.get("entfernt"),
                vor.get("failed"),
                vor.get("dauer_s"),
            )
        )
    if vor.get("hygiene"):
        hinweis = vor.get("hygiene_befunde")
        print(
            "Index-Hygiene (letzte Prüfung): %s%s"
            % (vor.get("hygiene"), f" · {hinweis} Befund(e)" if hinweis else "")
        )
    if vor.get("hinweis"):
        print("Hinweis:", vor["hinweis"])


def main() -> int:
    args = set(sys.argv[1:])
    if "--status" in args:
        status_anzeigen()
        return 0

    force = "--force" in args

    # Nur ein Aufbau gleichzeitig (15-Min-Hook + manuelle Läufe). Der Lock
    # löst sich beim Prozessende von selbst — kein verwaister Sperreintrag.
    os.makedirs(LOGS, exist_ok=True)
    lock_fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Vault-Index: Aufbau läuft bereits — überspringe")
        return 0

    vor = _load_json(STATUS_PATH) or {}
    ip = _index_path()
    index_mtime = os.path.getmtime(ip) if os.path.isfile(ip) else 0.0

    roots = _vault_roots()
    if not roots:
        print("Vault-Index: keine Vault-Pfade konfiguriert — nichts zu tun")
        return 0

    newest_m, newest_f = _newest_md(roots)
    # Maßstab ist der letzte ERFOLGREICHE Aufbau (Status), nicht die Index-mtime:
    # ein abgebrochener Aufbau schreibt den Index bereits — er würde sonst als
    # „frisch" gelten, obwohl Dateien fehlen (2026-10-02 genau so passiert).
    letzter = vor.get("letzter_aufbau_ts") or 0.0
    stale = (not os.path.isfile(ip)) or newest_m > letzter + 1
    alter_h = (time.time() - index_mtime) / 3600 if index_mtime else None

    zu_fruh = (time.time() - letzter) < THROTTLE_H * 3600

    # --force baut auch bei frischem Index (Handgriff „jetzt erneuern/prüfen").
    if not stale and not force:
        _write_status(
            {
                **(_load_json(STATUS_PATH) or {}),
                "geprueft_am": _now_iso(),
                "stale": False,
                "jüngste_änderung": datetime.fromtimestamp(newest_m).astimezone().isoformat(timespec="seconds"),
            }
        )
        return 0

    if zu_fruh and not force:
        print(
            "Vault-Index: veraltet (jüngste Änderung %s), aber gedrosselt — nächster Aufbau in spätestens %.0f h"
            % (
                datetime.fromtimestamp(newest_m).strftime("%Y-%m-%d %H:%M"),
                THROTTLE_H - (time.time() - letzter) / 3600,
            )
        )
        return 0

    if not _ollama_ok():
        print("Vault-Index: Ollama (bge-m3) nicht erreichbar — KEIN Aufbau (Chunks würden still fallen). Nächster Versuch im nächsten Slot.")
        _write_status(
            {
                **(_load_json(STATUS_PATH) or {}),
                "geprueft_am": _now_iso(),
                "stale": True,
                "ollama": False,
                "hinweis": "Ollama nicht erreichbar — Aufbau verschoben",
            }
        )
        return 2

    from core import retrieval

    print(
        "Vault-Index: baue inkrementell auf (veraltet seit Indexstand %.0fh, jüngste Änderung %s) …"
        % (alter_h or 0, os.path.basename(newest_f))
    )
    t0 = time.time()
    try:
        res = retrieval.build_index_from_vault(quiet=True)
    except Exception as e:
        print("Vault-Index: Aufbau FEHLGESCHLAGEN — %s" % e)
        _write_status(
            {
                **(_load_json(STATUS_PATH) or {}),
                "geprueft_am": _now_iso(),
                "stale": True,
                "ollama": True,
                "hinweis": f"Aufbau fehlgeschlagen: {e}",
            }
        )
        return 1
    dauer = round(time.time() - t0, 1)

    if res.get("error"):
        print("Vault-Index: abgebrochen — %s" % res["error"])
        return 1

    facts = _index_facts()

    # Datenschutz-Nachweis mitlaufen lassen (Index-Hygiene, s. index_hygiene.py):
    # nach jedem Aufbau ist der beste Moment zu belegen, dass nur erlaubtes im
    # Index liegt. Befund wird laut gemeldet, gelöscht wird nichts.
    try:
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import index_hygiene

        hyg = index_hygiene.run_check()
    except Exception as e:
        hyg = {"status": "fehler", "befunde": None, "fehler": str(e)}

    _write_status(
        {
            "geprueft_am": _now_iso(),
            "letzter_aufbau": _now_iso(),
            "letzter_aufbau_ts": time.time(),
            "stale": False,
            "ollama": True,
            "dauer_s": dauer,
            "neu_indexiert": res.get("indexed"),
            "chunks_neu": res.get("chunks"),
            "unchanged": res.get("unchanged"),
            "entfernt": res.get("removed"),
            "failed": res.get("failed"),
            "hygiene": hyg.get("status"),
            "hygiene_befunde": hyg.get("befunde"),
            "hinweis": "" if not res.get("failed") else f"{res['failed']} Datei(en) fehlgeschlagen",
            **facts,
        }
    )
    print(
        "Vault-Index: fertig in %.0fs · neu %s · unverändert %s · leer %s · entfernt %s · fehlgeschlagen %s · jetzt %s Chunks / %s Dokumente (Stand %s)"
        % (
            dauer,
            res.get("indexed"),
            res.get("unchanged"),
            res.get("leer"),
            res.get("removed"),
            res.get("failed"),
            facts.get("chunks"),
            facts.get("dokumente"),
            facts.get("stand"),
        )
    )
    if hyg.get("status") == "befund":
        print(
            "‼ Index-Hygiene: BEFUND (%s) — siehe logs/index_hygiene_report.json"
            % hyg.get("befunde")
        )
    elif hyg.get("status") == "fehler":
        print("Index-Hygiene: Prüfung fehlgeschlagen — %s" % hyg.get("fehler"))
    else:
        print("Index-Hygiene: ok — keine heiklen Pfade, keine Privat-Leaks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
