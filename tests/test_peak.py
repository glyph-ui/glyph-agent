#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for peak/off-peak window logic."""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import peak


def _utc(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def test_default_windows():
    assert peak.peak_windows() == [(1, 4), (6, 10)]
    assert peak.peak_weekdays() == [0, 1, 2, 3, 4]


def test_weekday_peak():
    # Mo 2026-08-31 02:00 UTC = Peak
    assert peak.is_peak(_utc(2026, 8, 31, 2)) is True
    # Mo 08:00 UTC = Peak
    assert peak.is_peak(_utc(2026, 8, 31, 8)) is True
    # Mo 00:00 UTC = off-peak
    assert peak.is_peak(_utc(2026, 8, 31, 0)) is False
    # Mo 05:00 UTC = off-peak (Lücke zwischen Fenstern)
    assert peak.is_peak(_utc(2026, 8, 31, 5)) is False
    # Mo 11:00 UTC = off-peak
    assert peak.is_peak(_utc(2026, 8, 31, 11)) is False


def test_weekend_off_peak():
    # So 2026-08-30 02:00 UTC = Wochenende → off-peak
    assert peak.is_peak(_utc(2026, 8, 30, 2)) is False
    # Sa 2026-08-29 08:00 UTC = Wochenende → off-peak
    assert peak.is_peak(_utc(2026, 8, 29, 8)) is False


def test_custom_windows():
    os.environ["HYBRID_PEAK_WINDOWS"] = '{"utc": [[0, 6]], "weekdays": [0, 1, 2, 3, 4, 5, 6]}'
    try:
        assert peak.peak_windows() == [(0, 6)]
        assert peak.peak_weekdays() == [0, 1, 2, 3, 4, 5, 6]
        # So 03:00 UTC → jetzt Peak (custom: alle Tage)
        assert peak.is_peak(_utc(2026, 8, 30, 3)) is True
        # So 12:00 UTC → off-peak
        assert peak.is_peak(_utc(2026, 8, 30, 12)) is False
    finally:
        os.environ.pop("HYBRID_PEAK_WINDOWS", None)


def test_describe():
    s = peak.describe()
    assert "01:00" in s and "10:00" in s and "Mo" in s


if __name__ == "__main__":
    test_default_windows()
    test_weekday_peak()
    test_weekend_off_peak()
    test_custom_windows()
    test_describe()
    print("ok")
