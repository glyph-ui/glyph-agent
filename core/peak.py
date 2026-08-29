# -*- coding: utf-8 -*-
"""
Peak/Off-Peak-Fenster für zeitbewusstes Hybrid-Routing.

DeepSeek bepreist seit 2026-08-16 nach Tageszeit (nur Werktage, Beijing-Zeit):
  Peak-Hours: 01:00–04:00 und 06:00–10:00 UTC, Mo–Fr
  Off-Peak:   alle anderen Stunden (Wochenenden komplett off-peak)

Hybrid-Modus: während Peak → OpenRouter (Reserve), off-peak → Direct (Primary).
Konfigurierbar über HYBRID_PEAK_WINDOWS (JSON, Standard DeepSeek-Fenster):

  HYBRID_PEAK_WINDOWS='{"utc": [[1, 4], [6, 10]], "weekdays": [0,1,2,3,4]}'

weekdays: 0=Montag … 6=Sonntag. Fenster: [start, end) in UTC-Stunden.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import List, Tuple

DEFAULT_WINDOWS: List[Tuple[int, int]] = [(1, 4), (6, 10)]
DEFAULT_WEEKDAYS: List[int] = [0, 1, 2, 3, 4]  # Mo–Fr


def _parse_windows(raw: str) -> List[Tuple[int, int]]:
    try:
        data = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return list(DEFAULT_WINDOWS)
    utc = data.get("utc") if isinstance(data, dict) else None
    if not isinstance(utc, list):
        return list(DEFAULT_WINDOWS)
    out = []
    for pair in utc:
        if (
            isinstance(pair, list)
            and len(pair) == 2
            and all(isinstance(h, int) for h in pair)
        ):
            out.append((pair[0], pair[1]))
    return out or list(DEFAULT_WINDOWS)


def _parse_weekdays(raw: str) -> List[int]:
    try:
        data = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return list(DEFAULT_WEEKDAYS)
    days = data.get("weekdays") if isinstance(data, dict) else None
    if not isinstance(days, list) or not days:
        return list(DEFAULT_WEEKDAYS)
    out = []
    for d in days:
        if isinstance(d, int) and 0 <= d <= 6:
            out.append(d)
    return out or list(DEFAULT_WEEKDAYS)


def peak_windows() -> List[Tuple[int, int]]:
    return _parse_windows(os.environ.get("HYBRID_PEAK_WINDOWS", ""))


def peak_weekdays() -> List[int]:
    return _parse_weekdays(os.environ.get("HYBRID_PEAK_WINDOWS", ""))


def is_peak(now=None) -> bool:
    """True, wenn jetzt (UTC) ein Peak-Fenster läuft (Werktag + Uhrzeit)."""
    dt = now or datetime.now(timezone.utc)
    if dt.weekday() not in peak_weekdays():
        return False
    hour = dt.hour
    return any(start <= hour < end for start, end in peak_windows())


def describe() -> str:
    """Kurzbeschreibung der aktuell konfigurierten Fenster (für Status/Logs)."""
    w = peak_windows()
    days = peak_weekdays()
    day_names = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    day_str = ",".join(day_names[d] for d in days)
    win_str = " & ".join(
        f"{start:02d}:00–{end:02d}:00 UTC" for start, end in w
    )
    return f"{win_str} ({day_str})"
