"""Tests for the technical-improvement trigger (news_gate.detect_technical_improvement).

This is the mirror of test_technical_breakdown. The regression it guards: a
standing "hold / observe" decision taken during a downtrend used to coast
straight through the reversal, because the gate only ever re-escalated on
*deterioration*. A fresh stabilization signal — big up day, cross back above
the 20-day line, recovery out of oversold — must wake the pipeline for a
re-evaluation. And, symmetric to the breakdown detector, every check must be a
*transition* condition so a name that merely *stays* strong does not re-run the
pipeline daily.
"""

import pytest

from webapp.engine.news_gate import detect_technical_improvement


def _closes(n, start=20.0, step=0.0):
    return [start - step * i for i in range(n)]


@pytest.mark.unit
class TestDetectTechnicalImprovement:
    def test_single_day_rally_fires(self):
        closes = _closes(24, start=20.0) + [21.4]  # +7% today
        ind = {"close_20_sma": 99.0, "rsi": 50}
        assert "单日涨" in detect_technical_improvement(21.4, ind, closes)

    def test_two_day_cumulative_rally_fires(self):
        # Two mild up days that sum to >= +8%.
        closes = _closes(23, start=20.0) + [20.0, 20.0]  # 2 flat days at 20
        # replace last two with 20.0 -> 20.8 (+4%) -> 21.6 (+3.85%); two-day +8%
        closes = _closes(23, start=20.0) + [20.8, 21.6]
        ind = {"close_20_sma": 99.0, "rsi": 50}
        assert "两日累计涨" in detect_technical_improvement(21.6, ind, closes)

    def test_cross_back_above_sma20_fires(self):
        # 20 flat days at 20.0, a dip to 19.7 (now just below the 20-day line),
        # then a modest bounce back to 20.0 — a fresh cross back above the line.
        # The single-day move (+1.5%) stays under the +5% reversal threshold so
        # the *crossing* is what fires, not the sharp-reversal rule.
        closes = [20.0] * 20 + [19.7, 20.0]
        today = closes[-1]
        ind = {"close_20_sma": sum(closes[-20:]) / 20.0, "rsi": 50}
        assert "站回 20 日线" in detect_technical_improvement(today, ind, closes)

    def test_persistent_above_sma20_does_not_fire(self):
        # A steady rally that crossed back above the 20-day line days ago and is
        # still above it today -> no fresh signal, so the day coasts.
        closes = _closes(25, start=12.0, step=-0.32)  # 12.0 .. 19.68 rising
        today = closes[-1]
        ind = {"close_20_sma": today - 1.0, "rsi": 55}
        assert detect_technical_improvement(today, ind, closes) is None

    def test_rsi_recovery_out_of_oversold_fires(self):
        # Previous day RSI < 30 (steady decline), today RSI >= 30.
        closes = _closes(25, start=20.0, step=0.3)  # 20.0 .. 12.8 -> prev RSI low
        closes = closes[:-1] + [13.6]  # today bounces up
        ind = {"close_20_sma": 99.0, "rsi": 31}
        assert "脱离超卖" in detect_technical_improvement(13.6, ind, closes)

    def test_still_above_rsi_30_does_not_fire(self):
        # A name already recovered (prev RSI >= 30) and still >= 30 today coasts.
        closes = _closes(25, start=12.0, step=-0.32)  # rising -> prev RSI high
        today = closes[-1]
        ind = {"close_20_sma": today - 1.0, "rsi": 55}
        assert detect_technical_improvement(today, ind, closes) is None

    def test_no_signal_returns_none(self):
        closes = _closes(25, start=20.0)
        ind = {"close_20_sma": 99.0, "rsi": 50}
        assert detect_technical_improvement(19.9, ind, closes) is None
