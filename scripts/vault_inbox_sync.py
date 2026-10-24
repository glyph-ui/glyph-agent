#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Täglicher Vault-Inbox: Scanordner + Downloads + Beruf-Nachzug.

Kopiert (nie löschen). Klassifiziert gemischte Drops. HSEQ → ASI + Index;
Privat → Privat-Vault (kein Index); Beruf-intern ohne Glyph-Suche.
Passwörter-Ordner: skip. Kein Status-Log im Vault — eine Zeile stdout.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import config, dotenv, vault_inbox  # noqa: E402

TZ = ZoneInfo("Europe/Berlin")
ICLOUD_DOCS = os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/Documents"
)
DOWNLOADS = os.path.expanduser("~/Downloads")
ASI = os.path.expanduser("~/ObsidianVaults/ASI, BS. UWS, QM, EM")
PRIVAT = os.path.expanduser("~/ObsidianVaults/Privat")
LIMIT = int(os.environ.get("VAULT_INBOX_LIMIT", "80"))


def main():
    dotenv.load_dotenv()
    try:
        config.reload_vault_paths()
    except Exception:
        pass
    day = datetime.now(TZ).strftime("%Y-%m-%d")
    asi = ASI
    privat = PRIVAT
    sources = [
        os.path.join(ICLOUD_DOCS, "Scanordner"),
        DOWNLOADS,
        os.path.join(ICLOUD_DOCS, "01 Beruf"),
    ]
    cache = os.path.join(ROOT, "logs", "vault_inbox_hashcache.json")
    res = vault_inbox.sync(
        sources=sources,
        asi_root=asi,
        privat_root=privat,
        day=day,
        limit=LIMIT,
        cache_path=cache,
    )
    # Index: Nightly (pdf-index-nightly) zieht neue ASI-Dateien inkrementell.
    # Hier nicht einbetten — der OCR-Backlog hält denselben Lock.
    print(
        f"inbox hseq={res['copied_hseq']} intern={res['copied_intern']} "
        f"privat={res['copied_privat']} dup={res['skipped_dup']} "
        f"secret={res['skipped_secret']} index_queue={len(res['index_files'])}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
