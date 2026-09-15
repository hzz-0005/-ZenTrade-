from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pandas as pd
import pytest

from webapp.config import WebappSettings
from webapp.core.models import Decision
from webapp.core.portfolio import ExecutionModel, Portfolio
from webapp.engine.adaptive_agent import AdaptiveDecisionAgent
from webapp.engine.backtest_engine import BacktestEngine
from webapp.engine.context_builder import DailyContext
from webapp.engine.decision_agent import DecisionAgent
from webapp.skills.library import SkillLibrary


def _cn_context(*, previous_close: float, open_price: float, high: float, close: float) -> DailyContext:
    return DailyContext(
        symbol="600396.SS",
        market="cn",
        sim_date="2026-06-09",
        ohlcv_tail_csv=(
            "Date,Open,High,Low,Close,Volume\n"
            f"2026-06-08,{previous_close},{previous_close},{previous_close},{previous_close},1000\n"
            f"2026-06-09,{open_price},{high},{close * 0.98:.2f},{close},1000"
        ),
        indicators={},
        news=[],
        portfolio={"cash": 50_000, "shares": 500, "equity": 100_000},
    )


def test_cn_limit_up_session_blocks_new_buy_even_when_model_requests_one():
    """Removing the A-share chase guard would turn this executable buy back on."""
    ctx = _cn_context(previous_close=20.0, open_price=21.5, high=22.2, close=21.6)
    decision = Decision(action="buy", position_pct=0.5, reasoning="breakout")
    flags: list[str] = []

    result = DecisionAgent.reconcile(decision, ctx, flags)

    assert result.action == "hold"
    assert result.position_pct == 0.0
    assert any("禁止追涨" in flag for flag in flags)


def test_cn_limit_down_session_keeps_discretionary_partial_sell_from_panicking():
    """Removing the panic-sell guard would turn this partial exit back on."""
    ctx = _cn_context(previous_close=20.0, open_price=18.7, high=18.8, close=18.5)
    decision = Decision(action="sell", position_pct=0.5, reasoning="price is weak")
    flags: list[str] = []

    result = DecisionAgent.reconcile(decision, ctx, flags)

    assert result.action == "hold"
    assert result.position_pct == 0.0
    assert any("禁止杀跌" in flag for flag in flags)


class _DirectArchitectureAgent:
    """A selected architecture's normal ``decide`` result at the engine boundary."""

    def __init__(self, decision: Decision):
        self.decision = decision
        self._call_count = 0

    def reset_day(self) -> None:
        self._call_count = 0

    def decide(self, _ctx):
        return self.decision, "audit", "response", []


class _EngineGateway:
    ticker = "600396.SS"
    market = "cn"

    def __init__(self, *, previous_close: float, open_price: float, high: float,
                 low: float, close: float):
        self.previous_close = previous_close
        self.open_price = open_price
        self.high = high
        self.low = low
        self.close = close

    def ohlcv_tail(self, _date, _rows):
        return pd.DataFrame(
            {
                "Date": pd.to_datetime(["2026-06-08", "2026-06-09"]),
                "Open": [self.previous_close, self.open_price],
                "High": [self.previous_close, self.high],
                "Low": [self.previous_close, self.low],
                "Close": [self.previous_close, self.close],
                "Volume": [1000, 1000],
            }
        )

    @staticmethod
    def indicator_snapshot(_date):
        return {}

    @staticmethod
    def news(_date):
        return [], []

    @staticmethod
    def reports(_date):
        return [], []

    @staticmethod
    def fundamentals(_date):
        return {}, []

    @staticmethod
    def sentiment(_date):
        return "", []

    @staticmethod
    def benchmark_tail(_date):
        return "", []

    @staticmethod
    def news_signature(_date):
        return ()


class _NoSkills:
    @staticmethod
    def select_for_day(_date):
        return []


@pytest.mark.parametrize("architecture", ["fast", "classic_graph", "hybrid"])
@pytest.mark.parametrize(
    ("action", "position_pct", "gateway_kwargs", "portfolio_kwargs"),
    [
        (
            "buy",
            1.0,
            {"previous_close": 20.0, "open_price": 21.4, "high": 22.1, "low": 21.2, "close": 21.6},
            {"cash": 100_000},
        ),
        (
            "sell",
            0.5,
            {"previous_close": 20.0, "open_price": 18.7, "high": 18.8, "low": 18.2, "close": 18.5},
            {"cash": 90_000, "shares": 500, "avg_cost": 20.0},
        ),
    ],
)
def test_engine_choke_point_blocks_a_share_extreme_day_from_every_architecture(
    monkeypatch, architecture, action, position_pct, gateway_kwargs, portfolio_kwargs
):
    """Removing the engine guard lets a graph/hybrid output bypass fast-agent reconciliation."""
    agent = _DirectArchitectureAgent(
        Decision(action=action, position_pct=position_pct, reasoning=f"{architecture} request")
    )
    engine = BacktestEngine("session-1", WebappSettings(), agent, _NoSkills())
    gateway = _EngineGateway(**gateway_kwargs)
    portfolio = Portfolio(**portfolio_kwargs)
    execution = ExecutionModel()

    monkeypatch.setattr("webapp.engine.backtest_engine.db.execute", lambda *_args: None)
    monkeypatch.setattr("webapp.engine.backtest_engine.db.query", lambda *_args: [])
    monkeypatch.setattr("webapp.engine.backtest_engine.db.query_one", lambda *_args: None)

    coast = asyncio.run(engine._run_day(1, "2026-06-09", gateway, portfolio, execution))

    assert Decision.model_validate_json(coast["decision_json"]).action == "hold"
    if action == "buy":
        assert portfolio.cash == 100_000
        assert portfolio.shares == 0
    else:
        assert portfolio.cash == 90_000
        assert portfolio.shares == 500


def test_adaptive_agent_cooldown_is_zero_call_and_never_delegates():
    """Removing the cooldown branch would call the expensive committee again."""
    fast, committee = MagicMock(), MagicMock()
    agent = AdaptiveDecisionAgent(WebappSettings(), fast_agent=fast, committee=committee)
    ctx = _cn_context(previous_close=20.0, open_price=20.0, high=20.1, close=20.0)
    ctx.cooldown_days = 2

    decision, _audit, _response, flags = agent.decide(ctx)

    assert decision.action == "hold"
    assert "adaptive:path=zero_call" in flags
    assert any("冷静期" in flag for flag in flags)
    fast.decide.assert_not_called()
    committee.decide.assert_not_called()


def test_engine_resumes_two_days_of_cooldown_after_a_full_risk_exit(monkeypatch):
    """Dropping persisted risk-exit detection would let a resumed run re-enter early."""
    engine = object.__new__(BacktestEngine)
    engine.session_id = "session-1"

    monkeypatch.setattr(
        "webapp.engine.backtest_engine.db.query_one",
        lambda sql, params: {
            "day_index": 8,
            "context_json": '{"risk_control": "止损触发，清仓离场", "full_exit": true}',
        },
    )

    assert engine._cooldown_until_index(start_index=9) == 11


def test_cash_idle_timer_uses_the_latest_executed_trade_not_latest_buy(monkeypatch):
    """Changing the query back to buy-only would report the earlier buy day here."""
    engine = object.__new__(BacktestEngine)
    engine.session_id = "session-1"

    def latest_trade(sql, params):
        if "executed_shares" in sql:
            return {"day_index": 9}
        return {"day_index": 1}

    monkeypatch.setattr("webapp.engine.backtest_engine.db.query_one", latest_trade)

    assert engine._cash_idle_days(day_index=10) == 1


def test_skill_selection_keeps_only_the_best_rule_per_category(monkeypatch):
    """Removing category de-duplication would return both timing rules."""
    rows = [
        {"id": "timing-new", "category": "timing", "statement": "新择时", "created_at": "2026-01-10", "success_rate": 0.9},
        {"id": "timing-old", "category": "timing", "statement": "旧择时", "created_at": "2026-01-01", "success_rate": 0.7},
        {"id": "risk", "category": "risk_control", "statement": "风险规则", "created_at": "2026-01-05", "success_rate": 0.8},
    ]
    monkeypatch.setattr("webapp.skills.library.db.query", lambda *_args: rows)

    selected = SkillLibrary(WebappSettings(skill_top_n=3)).select_for_day("2026-01-20")

    assert [skill["id"] for skill in selected] == ["timing-new", "risk"]
    assert len({skill["category"] for skill in selected}) == len(selected)
