"""Tests for the technical-breakdown trigger (news_gate.detect_technical_breakdown).

The key regression guard here: the 20-day-MA / Bollinger checks must be
*transition* conditions. Before #1066, a name that stayed below its 20-day MA
re-triggered the full pipeline every day ("跌破 20 日线"), defeating the coast
and re-running ~9 LLM nodes daily for a thesis that hadn't changed.
"""

import pytest

from webapp.engine.news_gate import detect_technical_breakdown


def _closes(n, start=20.0, step=0.0):
    return [start - step * i for i in range(n)]


@pytest.mark.unit
class TestDetectTechnicalBreakdown:
    def test_fresh_cross_below_sma20_fires(self):
        # 24 days flat at 20, today closes below the 20-day line.
        closes = _closes(24, start=20.0) + [12.0]
        ind = {"close_20_sma": 19.6, "boll_lb": 15.0, "rsi": 38}
        assert "跌破 20 日线" in detect_technical_breakdown(12.0, ind, closes)

    def test_persistent_below_sma20_does_not_fire(self):
        # A steady, mild decline that crossed below the 20-day line days ago and
        # is still below it today (daily moves ~-3%, two-day ~-6%, both under
        # their thresholds) -> no fresh signal, so the day coasts.
        closes = _closes(25, start=20.0, step=0.32)  # 20.0 .. 12.32
        today = closes[-1]
        ind = {"close_20_sma": today + 1.0, "boll_lb": today - 5.0, "rsi": 45}
        assert detect_technical_breakdown(today, ind, closes) is None

    def test_single_day_crash_still_fires(self):
        closes = _closes(24, start=20.0) + [18.6]  # -7% today
        ind = {"close_20_sma": 99.0, "boll_lb": -99.0, "rsi": 50}
        assert "单日跌" in detect_technical_breakdown(18.6, ind, closes)

    def test_rsi_oversold_still_fires(self):
        closes = _closes(25, start=20.0)
        ind = {"close_20_sma": 99.0, "boll_lb": -99.0, "rsi": 22}
        assert "RSI" in detect_technical_breakdown(19.9, ind, closes)

    def test_persistent_oversold_does_not_fire(self):
        # A name already oversold for days (prev RSI < 30) that stays oversold
        # today must coast — otherwise a protracted oversold downtrend re-runs
        # the full pipeline daily on "RSI 超卖" alone (600396's July slide).
        closes = _closes(25, start=20.0, step=0.32)  # steady decline, prev RSI ~0
        today = closes[-1]
        ind = {"close_20_sma": today + 1.0, "boll_lb": today - 5.0, "rsi": 22}
        assert detect_technical_breakdown(today, ind, closes) is None

    def test_short_history_falls_back_to_level_check(self):
        # Fewer than 21 closes: can't compute the prev-day reference, so keep
        # the old level check (over-escalate rather than silently miss a break).
        closes = _closes(5, start=20.0, step=1.0)  # ends at 16.0
        ind = {"close_20_sma": 17.0, "boll_lb": -99.0, "rsi": 50}
        assert "跌破 20 日线" in detect_technical_breakdown(16.0, ind, closes)
