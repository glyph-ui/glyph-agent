#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI für wiederkehrende To-dos / Legacy-Job-Namen."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

PORT = int(os.environ.get("GLYPH_AGENT_PORT", "18899"))
HOST = os.environ.get("GLYPH_AGENT_HOST", "127.0.0.1")
BASE = f"http://{HOST}:{PORT}"

ALIAS = {
    "hseq-eingang": "td-eingang",
    "hseq-handover": "td-handover",
    "hseq-aus-fertig-lernen": "td-lernen",
    "memory-hygiene": "td-memory",
    "memory": "td-memory",
    "wiki-hygiene": "td-wiki-hygiene",
    "wiki": "td-wiki-hygiene",
    "session-cleanup": "td-cleanup",
    "cleanup": "td-cleanup",
    "inbox": "td-inbox-sync",
    "vault-inbox": "td-inbox-sync",
}


def http_json(method, path, body=None, timeout=1900):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def main(argv=None):
    p = argparse.ArgumentParser(description="glyph-agent recurring todo")
    p.add_argument("job", nargs="?", help="todo id oder legacy hseq-*")
    p.add_argument("--list", action="store_true")
    p.add_argument("--force", action="store_true", default=True)
    p.add_argument("--due", action="store_true", help="alle fälligen laufen lassen")
    args = p.parse_args(argv)

    if args.list:
        print(json.dumps(http_json("GET", "/recurring", timeout=30), ensure_ascii=False, indent=2)[:8000])
        return 0
    if args.due:
        print(json.dumps(http_json("POST", "/recurring/run-due", {}, timeout=1900), ensure_ascii=False, indent=2)[:8000])
        return 0
    if not args.job:
        p.error("job/id fehlt")
    rid = ALIAS.get(args.job, args.job)
    r = http_json("POST", f"/recurring/{rid}/run", {"force": bool(args.force)}, timeout=1900)
    print(json.dumps(r, ensure_ascii=False, indent=2)[:4000])
    return 0 if r.get("ok") or r.get("skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
