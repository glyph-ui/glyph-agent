# -*- coding: utf-8 -*-
"""Token aus einer Chat-Completion, pro Request-Thread.

Zählt, was der Anbieter in `usage` meldet. Keine Schätzung.
Cache-Treffer stecken in den Input-Token und werden nicht noch einmal addiert.
"""
import threading

_local = threading.local()


def reset():
    _local.input_tokens = 0
    _local.output_tokens = 0


def snapshot():
    inp = int(getattr(_local, "input_tokens", 0) or 0)
    out = int(getattr(_local, "output_tokens", 0) or 0)
    return {"input": inp, "output": out, "total": inp + out}


def _as_int(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def add_payload(data):
    """Addiert usage einer Completion. Liefert die Summe dieses Aufrufs."""
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return 0
    inp = _as_int(usage.get("prompt_tokens"))
    if inp == 0:
        inp = _as_int(usage.get("input_tokens"))
    out = _as_int(usage.get("completion_tokens"))
    if out == 0:
        out = _as_int(usage.get("output_tokens"))
    total = _as_int(usage.get("total_tokens"))
    if inp == 0 and out == 0 and total:
        inp = total
    elif total > inp + out:
        out += total - inp - out
    if inp == 0 and out == 0:
        return 0
    _local.input_tokens = int(getattr(_local, "input_tokens", 0) or 0) + inp
    _local.output_tokens = int(getattr(_local, "output_tokens", 0) or 0) + out
    return inp + out
