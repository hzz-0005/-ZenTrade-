from __future__ import annotations

import asyncio
import json
import sqlite3

import pandas as pd
import pytest

from webapp.config import WebappSettings
from webapp.core.models import Decision
from webapp.core.portfolio import ExecutionModel, Portfolio
from webapp.engine.backtest_engine import BacktestEngine
from webapp.engine.clock import reset_sim_date, set_sim_date
from webapp.engine.data_gateway import DataGateway
from webapp.server.routers import sessions
from webapp.store import db


def _gateway() -> DataGateway:
    gateway = DataGateway("600000.SS", "cn")
    gateway._price_frame = pd.DataFrame(
        [
            {"Date": pd.Timestamp("2026-01-05"), "Open": 10.0, "High": 10.2,
             "Low": 9.8, "Close": 10.0, "Volume": 1000},
            {"Date": pd.Timestamp("2026-01-06"), "Open": 8.9, "High": 9.3,
             "Low": 8.8, "Close": 9.1, "Volume": 1200},
        ]
    )
    return gateway


def test_execution_open_is_available_without_exposing_next_day_to_signal_reads():
    """Removing the execution-only accessor must fail this no-lookahead contract."""
    gateway = _gateway()
    token = set_sim_date("2026-01-05")
    try:
        assert gateway.close_on("2026-01-06") == 10.0
        assert gateway.execution_open_on("2026-01-06") == 8.9
    finally:
        reset_sim_date(token)


def test_execution_open_rejects_a_date_that_has_no_trading_bar():
    """Replacing the exact-bar lookup with a tail lookup would silently fill on a wrong day."""
    gateway = _gateway()

    with pytest.raises(Exception, match="no execution bar"):
        gateway.execution_open_on("2026-01-07")


def test_cn_limit_up_open_leaves_a_new_buy_unfilled():
    """Changing the A-share limit-up branch to fill would recreate an impossible chase."""
    fill = BacktestEngine._execute_at_next_open(
        Decision(action="buy", position_pct=1.0),
        Portfolio(cash=10_000),
        ExecutionModel(min_order_shares=100, share_step=100),
        market="cn",
        prior_close=10.0,
        execution_open=11.0,
        execution_volume=1000,
    )

    assert fill.action == "rejected"
    assert "limit-up" in fill.reason


def test_next_open_fill_uses_the_execution_session_open_not_signal_close():
    """Changing the engine branch back to close_t would make this fill at 10.0."""
    decision = Decision(action="buy", position_pct=1.0)
    fill = BacktestEngine._execute_at_next_open(
        decision,
        Portfolio(cash=10_000),
        ExecutionModel(min_order_shares=100, share_step=100),
        market="cn",
        prior_close=10.0,
        execution_open=9.2,
        execution_volume=1000,
    )

    assert fill.action == "buy"
    assert fill.price == 9.2


def test_migration_keeps_legacy_rows_on_same_close_and_adds_audit_dates():
    """Removing a migration would make a pre-existing backtest database unreadable."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, use_full_graph INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE daily_records (session_id TEXT, day_index INTEGER, date TEXT);
        CREATE TABLE trades (id INTEGER PRIMARY KEY);
        """
    )

    db._migrate(conn)

    session_columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    daily_columns = {row[1] for row in conn.execute("PRAGMA table_info(daily_records)")}
    assert "execution_timing" in session_columns
    assert {"decision_date", "execution_date", "execution_status", "valuation_date"} <= daily_columns


@pytest.fixture()
def session_db(tmp_path, monkeypatch):
    """A real, isolated ledger for engine/API timing-contract tests."""
    previous = db._conn
    conn = db._connect(tmp_path / "backtest.db")
    monkeypatch.setattr(db, "_conn", conn)
    db.init_db()
    db.execute(
        "INSERT INTO sessions (id, ticker, canonical_ticker, market, start_date, end_date, "
        "initial_capital, commission_rate, min_commission, slippage_bps, status, cash, created_at) "
        "VALUES ('timing', '600000.SS', '600000.SS', 'cn', '2026-01-05', '2026-01-08', "
        "100000, .0003, 5, 0, 'done', 100000, '2026-01-01')"
    )
    yield
    conn.close()
    db._conn = previous


def test_chart_uses_unique_valuation_dates_not_signal_dates(session_db):
    """Using daily_records.date for next-open equity would put a T+1 balance on T."""
    db.execute(
        "INSERT INTO daily_records (session_id, day_index, date, decision_date, execution_date, "
        "valuation_date, execution_status, decision_json, action, cash_after, shares_after, equity) "
        "VALUES ('timing', 0, '2026-01-05', '2026-01-05', '2026-01-06', '2026-01-06', "
        "'filled', '{}', 'buy', 0, 10000, 91000)"
    )
    # This is a no-order decision made on the execution session.  It must not
    # create a second point for 2026-01-06 in the equity chart.
    db.execute(
        "INSERT INTO daily_records (session_id, day_index, date, decision_date, execution_date, "
        "valuation_date, execution_status, decision_json, action, cash_after, shares_after, equity) "
        "VALUES ('timing', 1, '2026-01-06', '2026-01-06', NULL, '2026-01-06', "
        "'not_required', '{}', 'coast', 0, 10000, 91000)"
    )

    payload = sessions.chart("timing")

    assert payload["equity"] == [{"date": "2026-01-06", "equity": 91000.0, "cash": 0.0}]


def test_next_open_hold_never_reads_future_open_or_records_an_execution(session_db):
    """Treating a hold like an order leaks T+1 data into a no-order ledger row."""
    class HoldAgent:
        _call_count = 0

        def __init__(self):
            self.context = None

        def reset_day(self):
            self._call_count = 0

        def decide(self, context):
            self.context = context
            return Decision(action="hold"), "prompt", "response", []

    class NoFutureOpenGateway:
        ticker = "600000.SS"
        market = "cn"

        @staticmethod
        def ohlcv_tail(_date, _rows):
            return pd.DataFrame([
                {"Date": pd.Timestamp("2026-01-04"), "Open": 9.8, "High": 10, "Low": 9.7, "Close": 9.9, "Volume": 1000},
                {"Date": pd.Timestamp("2026-01-05"), "Open": 10, "High": 10.1, "Low": 9.8, "Close": 10, "Volume": 1000},
            ])

        indicator_snapshot = staticmethod(lambda _date: {})
        news = staticmethod(lambda _date: ([], []))
        reports = staticmethod(lambda _date: ([], []))
        fundamentals = staticmethod(lambda _date: ({}, []))
        sentiment = staticmethod(lambda _date: ("", []))
        benchmark_tail = staticmethod(lambda _date: ("", []))
        news_signature = staticmethod(lambda _date: ())

        @staticmethod
        def execution_bar_on(_date):
            raise AssertionError("a hold must not inspect the next session's bar")

    class NoSkills:
        select_for_day = staticmethod(lambda _date: [])

    agent = HoldAgent()
    engine = BacktestEngine("timing", WebappSettings(), agent, NoSkills())
    asyncio.run(engine._run_day(
        0, "2026-01-05", NoFutureOpenGateway(),
        Portfolio(cash=100000, shares=100, avg_cost=9.0), ExecutionModel(),
        execution_date="2026-01-06", execution_timing="next_open",
    ))

    row = db.query_one(
        "SELECT decision_date, execution_date, valuation_date, execution_status FROM daily_records "
        "WHERE session_id='timing' AND day_index=0"
    )
    assert dict(row) == {
        "decision_date": "2026-01-05", "execution_date": None,
        "valuation_date": "2026-01-05", "execution_status": "not_required",
    }
    # The decision observes the completed signal-day close (10.0), not the
    # previous close (9.9) and never the future execution session.  Reverting
    # the decision snapshot to prev_close makes this 100 currency units low.
    assert agent.context.portfolio["equity"] == 101000.0
    assert agent.context.portfolio["open_pnl"] == 100.0


def test_days_list_includes_decision_json_for_inline_reasoning(session_db):
    """Dropping the ledger JSON from the list query leaves every table reason blank."""
    decision_json = Decision(action="hold", reasoning="信号日收盘确认趋势").model_dump_json()
    db.execute(
        "INSERT INTO daily_records (session_id, day_index, date, decision_json, action, "
        "cash_after, shares_after, equity) VALUES ('timing', 0, '2026-01-05', ?, "
        "'hold', 100000, 0, 100000)",
        (decision_json,),
    )

    payload = sessions.list_days("timing", offset=0, limit=50)

    assert json.loads(payload["days"][0]["decision_json"])["reasoning"] == "信号日收盘确认趋势"


@pytest.mark.parametrize("kind", ["coast", "cooldown"])
def test_no_order_next_open_rows_keep_audit_date_without_execution_date(session_db, kind):
    """Dropping the no-order timing fields makes a skipped day look like legacy data."""
    engine = object.__new__(BacktestEngine)
    engine.session_id = "timing"
    trade_date = "2026-01-06"

    if kind == "coast":
        asyncio.run(engine._record_coast_day(
            1, trade_date, 9.5, _gateway(),
            {"decision_json": Decision(action="hold").model_dump_json(), "origin_date": "2026-01-05",
             "lower": 9.0, "upper": 10.0},
            execution_timing="next_open",
        ))
    else:
        asyncio.run(engine._record_cooldown_day(
            1, trade_date, 9.5, Portfolio(cash=100000), 3,
            execution_timing="next_open",
        ))

    row = db.query_one(
        "SELECT decision_date, execution_date, valuation_date, execution_status, context_json "
        "FROM daily_records WHERE session_id='timing' AND day_index=1"
    )
    assert {key: row[key] for key in ("decision_date", "execution_date", "valuation_date", "execution_status")} == {
        "decision_date": "2026-01-06",
        "execution_date": None,
        "valuation_date": "2026-01-06",
        "execution_status": "not_required",
    }
    assert json.loads(row["context_json"])["execution_timing"] == "next_open"


class _RiskRetryGateway:
    """Two opening auctions: first is limit-down, then the retry can fill."""

    ticker = "600000.SS"
    market = "cn"

    def __init__(self):
        self.opens = {
            "2026-01-06": {"open": 8.1, "close": 8.1, "volume": 1000},
            "2026-01-07": {"open": 9.1, "close": 9.2, "volume": 1000},
        }

    @staticmethod
    def close_on(_date):
        return 9.0

    def execution_bar_on(self, date):
        return self.opens[date]


def test_unfilled_next_open_risk_exit_is_audited_and_retried_without_mutating_the_book(session_db):
    """Returning false after a blocked risk order would call the LLM and lose the retry."""
    engine = BacktestEngine(
        "timing", WebappSettings(), decision_agent=object(), skill_library=object()
    )
    gateway = _RiskRetryGateway()
    portfolio = Portfolio(cash=0, shares=1000, avg_cost=10.0)
    execution = ExecutionModel(min_order_shares=100, share_step=100)
    coast = {"decision_json": Decision(action="hold", stop_loss=9.5).model_dump_json()}

    blocked = asyncio.run(engine._check_risk_stops(
        0, "2026-01-05", "2026-01-06", "next_open", gateway, portfolio, execution, coast,
    ))

    assert blocked is True
    assert (portfolio.cash, portfolio.shares) == (0, 1000)
    first = db.query_one(
        "SELECT action, execution_status, cash_after, shares_after, context_json "
        "FROM daily_records WHERE session_id='timing' AND day_index=0"
    )
    assert {key: first[key] for key in ("action", "execution_status", "cash_after", "shares_after")} == {
        "action": "rejected", "execution_status": "unfilled", "cash_after": 0.0,
        "shares_after": 1000.0,
    }
    first_context = json.loads(first["context_json"])
    assert first_context["risk_control"].startswith("止损触发")
    assert first_context["risk_retry"] is False

    retried = asyncio.run(engine._check_risk_stops(
        1, "2026-01-06", "2026-01-07", "next_open", gateway, portfolio, execution, None,
    ))

    assert retried is True
    assert portfolio.shares == 0
    second = db.query_one(
        "SELECT action, execution_status, execution_date, context_json "
        "FROM daily_records WHERE session_id='timing' AND day_index=1"
    )
    assert {key: second[key] for key in ("action", "execution_status", "execution_date")} == {
        "action": "sell", "execution_status": "filled", "execution_date": "2026-01-07",
    }
    assert json.loads(second["context_json"])["risk_retry"] is True


def test_cash_idle_clock_starts_on_next_open_fill_not_its_prior_signal_day(monkeypatch):
    """Using only daily_records.day_index overstates idle time immediately after a next-open fill."""
    engine = object.__new__(BacktestEngine)
    engine.session_id = "timing"

    monkeypatch.setattr(
        "webapp.engine.backtest_engine.db.query_one",
        lambda *_args: {"day_index": 4, "date": "2026-01-09", "execution_date": "2026-01-12"},
    )

    assert engine._cash_idle_days(day_index=5) == 0


def test_final_next_open_valuation_is_recorded_once_and_drives_metrics_and_chart(session_db):
    """Omitting the terminal valuation makes next-open returns and the chart stop a day early."""
    engine = object.__new__(BacktestEngine)
    engine.session_id = "timing"
    portfolio = Portfolio(cash=0, shares=10_000, avg_cost=10.0)

    engine._record_final_valuation(2, "2026-01-08", 9.5, portfolio, execution_timing="next_open")
    engine._record_final_valuation(2, "2026-01-08", 9.5, portfolio, execution_timing="next_open")
    engine._finalize_metrics(pd.DataFrame(), ["2026-01-05", "2026-01-06", "2026-01-08"])

    rows = db.query("SELECT action, valuation_date, equity FROM daily_records WHERE session_id='timing'")
    assert [dict(row) for row in rows] == [{"action": "final_mark", "valuation_date": "2026-01-08", "equity": 95_000.0}]
    session = db.query_one("SELECT final_equity, total_return_pct FROM sessions WHERE id='timing'")
    assert dict(session) == {"final_equity": 95_000.0, "total_return_pct": -5.0}
    assert sessions.chart("timing")["equity"] == [
        {"date": "2026-01-08", "equity": 95_000.0, "cash": 0.0}
    ]
