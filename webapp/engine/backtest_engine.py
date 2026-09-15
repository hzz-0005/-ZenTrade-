"""The daily backtest loop / session state machine.

Decision-day T invariants:
  - sim_date ContextVar is set to T for the whole iteration
  - every signal / prompt data read goes through DataGateway (clamped to T)
  - a T-close decision fills only at the next trading session's opening price
  - the full prompt is persisted for audit (verify no post-T dates)
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import pandas as pd

from webapp.config import WebappSettings
from webapp.core.errors import DataUnavailable, InvalidDecision, LLMBudgetExceeded
from webapp.core.models import Decision, Fill
from webapp.core.portfolio import ExecutionModel, Portfolio
from webapp.engine.clock import reset_sim_date, set_sim_date
from webapp.engine.context_builder import DailyContext
from webapp.engine.data_gateway import DataGateway
from webapp.engine.decision_agent import DecisionAgent, apply_market_trade_guard
from webapp.skills.library import SkillLibrary
from webapp.store import db

logger = logging.getLogger(__name__)


class SessionState:
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    REVIEWING = "reviewing"
    DONE = "done"
    FAILED = "failed"
    STOPPED = "stopped"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _cash_idle_note(cash_ratio: float, idle_days: int) -> str | None:
    """Soft nudge: cash is a position too — it must have a plan or a reason.

    Observed pathology: the model sells half, then lets ~50% cash sit dead for
    the rest of the session without ever re-deploying. This flag goes into the
    daily prompt's data-coverage section; it demands a deployment plan (what
    trigger, what size) or an explicit justification — it does not force an
    action. Deliberately non-prescriptive.
    """
    if cash_ratio >= 0.3 and idle_days >= 5:
        return (f"cash: 现金占比 {cash_ratio:.0%} 已闲置 {idle_days} 个交易日——"
                "仅在有可验证入场条件时才考虑部署；否则说明继续保留的条件。"
                "这不是买入指令。")
    return None


class BacktestEngine:
    def __init__(self, session_id: str, settings: WebappSettings,
                 decision_agent: DecisionAgent, skill_library: SkillLibrary):
        self.session_id = session_id
        self.settings = settings
        self.agent = decision_agent
        self.skills = skill_library
        self.pause_event = asyncio.Event()
        self.pause_event.set()
        self._stop_requested = False
        # Running equity peak for the drawdown circuit-breaker (rebuilt on resume).
        self._peak_equity: float | None = None
        self._last_risk_was_full_exit = False

    # ---- lifecycle control (called from the API layer) ----

    def pause(self) -> None:
        self.pause_event.clear()

    def resume(self) -> None:
        self.pause_event.set()

    def stop(self) -> None:
        self._stop_requested = True
        self.pause_event.set()  # unblock a paused loop so it can exit

    # ---- main loop ----

    async def run(self) -> None:
        session = db.query_one("SELECT * FROM sessions WHERE id=?", (self.session_id,))
        if session is None:
            raise RuntimeError(f"session {self.session_id} not found")

        db.execute(
            "UPDATE sessions SET status=?, started_at=COALESCE(started_at, ?) WHERE id=?",
            (SessionState.RUNNING, _now(), self.session_id),
        )
        try:
            await self._run_loop(dict(session))
        except asyncio.CancelledError:
            self._set_status(SessionState.STOPPED)
            raise
        except Exception as exc:
            logger.exception("session %s failed", self.session_id)
            db.execute(
                "UPDATE sessions SET status=?, error=?, finished_at=? WHERE id=?",
                (SessionState.FAILED, f"{type(exc).__name__}: {exc}", _now(), self.session_id),
            )

    def _set_status(self, status: str) -> None:
        finished = ", finished_at='" + _now() + "'" if status in (SessionState.STOPPED,) else ""
        db.execute(f"UPDATE sessions SET status=?{finished} WHERE id=?", (status, self.session_id))

    async def _run_loop(self, session: dict) -> None:
        ticker = session["canonical_ticker"]
        market = session["market"]
        gw = DataGateway(
            ticker, market,
            macro_news_enabled=getattr(self.settings, "macro_news_enabled", False),
        )

        exec_model = ExecutionModel(
            commission_rate=session["commission_rate"],
            min_commission=session["min_commission"],
            slippage_bps=session["slippage_bps"],
            min_order_shares=(
                200.0 if market == "cn" and ticker.split(".")[0].startswith("688")
                else 100.0 if market == "cn" else 0.0
            ),
            share_step=(
                1.0 if market == "cn" and ticker.split(".")[0].startswith("688")
                else 100.0 if market == "cn" else 0.0001
            ),
            sell_tax_rate=0.0005 if market == "cn" else 0.0,
        )
        # Restore portfolio state so pause/resume (or a server restart) continues
        # the ledger instead of resetting to initial capital.
        portfolio = Portfolio(
            cash=session["cash"], shares=session["shares"], avg_cost=session["avg_cost"]
        )

        # ---- one-time data priming (blocking, in a thread) ----
        frame = await asyncio.to_thread(
            gw.load_price_frame, session["start_date"], session["end_date"],
            self.settings.lookback_calendar_days,
        )
        days = gw.trading_days(session["start_date"], session["end_date"])
        if not days:
            raise DataUnavailable(f"no trading days for {ticker} in the requested range")
        if len(days) > self.settings.max_session_days:
            raise DataUnavailable(
                f"range covers {len(days)} trading days > max {self.settings.max_session_days}"
            )
        db.execute("UPDATE sessions SET trading_days=? WHERE id=?",
                   (json.dumps(days), self.session_id))
        # persist the frame once for the chart endpoint (kline overlay)
        db.executemany(
            "INSERT OR REPLACE INTO price_frames (session_id, date, open, high, low, close, volume) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (self.session_id, r["Date"].strftime("%Y-%m-%d"),
                 float(r["Open"]), float(r["High"]), float(r["Low"]),
                 float(r["Close"]), float(r.get("Volume", 0) or 0))
                for _, r in frame.iterrows()
            ],
        )

        # Historical news backfill (announcements + macro headlines) — one-time,
        # best-effort: per-date failures degrade that day's news, never the session.
        try:
            news_start = (pd.Timestamp(session["start_date"]) - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
            await asyncio.to_thread(gw.prefetch_news_archive, news_start, session["end_date"])
        except Exception as exc:
            logger.warning("news archive prefetch failed (%s); continuing without it", exc)

        # Resume support: skip already-recorded days. A restart loses the
        # in-memory coast plan, so rebuild it from the last real decision —
        # otherwise the first-day rule (no standing decision -> escalate)
        # would burn one full pipeline run on every resume.
        start_index = session["current_day_index"] or 0
        coast: dict | None = None  # standing decision: {"until_index","lower","upper","origin_index","origin_date","decision_json"}
        cooldown_until_index = self._cooldown_until_index(start_index)
        if start_index > 0:
            coast = await self._restore_coast(gw, start_index)

        # Rebuild the running equity peak from prior days so the drawdown
        # circuit-breaker survives a pause/resume or a server restart.
        self._peak_equity = None
        for r in db.query(
            "SELECT equity FROM daily_records WHERE session_id=? AND day_index < ? ORDER BY day_index",
            (self.session_id, start_index),
        ):
            if r["equity"] is not None:
                self._peak_equity = max(self._peak_equity or 0.0, float(r["equity"]))

        execution_timing = session.get("execution_timing") or "same_close"
        use_next_open = execution_timing == "next_open"

        # Position-management policy (sessions.initial_position='full'): buy
        # a full position deterministically, so
        # from day 1 the agent manages a holding instead of answering
        # "enter or stay flat?" — which LLMs answer with "stay flat" forever.
        # No coast is planted, so day 1 always gets a real LLM decision.
        if (start_index == 0
                and (session.get("initial_position") or "full") == "full"
                and not db.query_one(
                    "SELECT 1 FROM daily_records WHERE session_id=? LIMIT 1",
                    (self.session_id,),
                ) and (not use_next_open or len(days) > 1)):
            token = set_sim_date(days[0])  # close_on() is clock-guarded
            try:
                self._force_initial_buy(
                    0, days[0], gw, portfolio, exec_model,
                    execution_date=days[1] if use_next_open else days[0],
                    execution_timing=execution_timing,
                )
            finally:
                reset_sim_date(token)
            start_index = 1

        day_index = start_index
        # A next-open session has no fill available after its final close.
        # Its final-day close is still used by _finalize_metrics below, but no
        # LLM request is made for an order that cannot be executed in-range.
        last_signal_index = len(days) - 1 if use_next_open else len(days)
        while day_index < last_signal_index:
            trade_date = days[day_index]
            execution_date = days[day_index + 1] if use_next_open else trade_date

            await self.pause_event.wait()
            if self._stop_requested:
                self._set_status(SessionState.STOPPED)
                return

            token = set_sim_date(trade_date)
            try:
                recheck_reasons: list[str] = []
                fresh_news: list[dict] = []
                prev_coast = coast

                # Hard risk-control layer: stop-loss / take-profit / drawdown
                # circuit-breaker, enforced every day (including coast days),
                # independent of the LLM. This is what holds a drawdown in check.
                if await self._check_risk_stops(
                    day_index, trade_date, execution_date, execution_timing,
                    gw, portfolio, exec_model, coast,
                ):
                    coast = None  # invalidate the standing decision; next day re-decides
                    if self._last_risk_was_full_exit:
                        cooldown_until_index = day_index + 3
                    day_index += 1
                    continue

                if day_index < cooldown_until_index:
                    close_now = await asyncio.to_thread(gw.close_on, trade_date)
                    await self._record_cooldown_day(
                        day_index, trade_date, close_now, portfolio, cooldown_until_index,
                        execution_timing=execution_timing,
                    )
                    day_index += 1
                    continue

                if coast is not None:
                    close_now = await asyncio.to_thread(gw.close_on, trade_date)
                    in_band = coast["lower"] <= close_now <= coast["upper"]
                    # Material news invalidates a standing decision even while
                    # the price is still inside its band.
                    fresh_news = await asyncio.to_thread(
                        self._news_delta, gw, trade_date, coast
                    )
                    tech_break = None
                    tech_improve = None
                    if day_index < coast["until_index"] and in_band and not fresh_news:
                        # Price still inside the band with no news — but a sharp
                        # oversold flush / trend break (or, on the other side, a
                        # stabilization / reversal) should still force a fresh
                        # re-evaluation rather than coasting straight through it.
                        indicators = await asyncio.to_thread(
                            gw.indicator_snapshot, trade_date
                        )
                        tech_break = await asyncio.to_thread(
                            self._technical_breakdown, gw, trade_date, close_now, indicators
                        )
                        tech_improve = await asyncio.to_thread(
                            self._technical_improvement, gw, trade_date, close_now, indicators
                        )
                    if (day_index < coast["until_index"] and in_band
                            and not fresh_news and not tech_break and not tech_improve):
                        # coast: keep the standing decision, no LLM call today
                        await self._record_coast_day(
                            day_index, trade_date, close_now, gw, coast,
                            execution_timing=execution_timing,
                        )
                        day_index += 1
                        continue
                    if fresh_news:
                        titles = "；".join(str(d.get("title", ""))[:40] for d in fresh_news[:3])
                        recheck_reasons.append(
                            f"re-decide: 消息面新增 {len(fresh_news)} 条重大信息"
                            f"（{coast['origin_date']} 的决策作废）：{titles}"
                        )
                    elif not in_band:
                        recheck_reasons.append(
                            f"re-decide: 收盘 {close_now} 突破区间 "
                            f"[{coast['lower']}, {coast['upper']}]"
                        )
                    elif tech_break:
                        recheck_reasons.append(f"re-decide: 技术面恶化（{tech_break}）")
                    elif tech_improve:
                        recheck_reasons.append(f"re-decide: 技术面企稳/反转（{tech_improve}）")
                    coast = None

                coast = await self._run_day(
                    day_index, trade_date, gw, portfolio, exec_model, recheck_reasons,
                    news_delta=fresh_news, prev_coast=prev_coast,
                    execution_date=execution_date, execution_timing=execution_timing,
                )
            finally:
                reset_sim_date(token)
            day_index += 1

        if use_next_open:
            # A T+1 order cannot be placed after the final close, but the
            # final close is still part of the requested backtest interval.
            # Keep an explicit valuation row so metrics and the equity chart
            # do not silently stop one market day early.
            final_token = set_sim_date(days[-1])
            try:
                final_close = await asyncio.to_thread(gw.close_on, days[-1])
                self._record_final_valuation(
                    len(days) - 1, days[-1], final_close, portfolio,
                    execution_timing=execution_timing,
                )
            finally:
                reset_sim_date(final_token)
            db.execute("UPDATE sessions SET current_day_index=? WHERE id=?", (len(days), self.session_id))

        # ---- review phase ----
        db.execute("UPDATE sessions SET status=? WHERE id=?", (SessionState.COMPLETED, self.session_id))
        self._finalize_metrics(frame, days)
        db.execute("UPDATE sessions SET status=?, finished_at=? WHERE id=?",
                   (SessionState.REVIEWING, _now(), self.session_id))
        await self._run_review(gw, days)
        db.execute("UPDATE sessions SET status=?, finished_at=? WHERE id=?",
                   (SessionState.DONE, _now(), self.session_id))

    @staticmethod
    def _news_delta(gw: DataGateway, trade_date: str, coast: dict) -> list[dict]:
        """Material items that appeared after the coasting decision was taken."""
        items = gw.material_items(trade_date)
        previous = set(coast.get("news_sig") or ())
        return [items[key] for key in sorted(set(items) - previous)]

    @staticmethod
    def _technical_breakdown(gw: DataGateway, trade_date: str, close: float | None,
                             indicators: dict | None) -> str | None:
        """Technical-breakdown trigger that forces a re-decide during a coast."""
        from webapp.engine.news_gate import detect_technical_breakdown

        try:
            # 21+ closes let detect_technical_breakdown distinguish a *fresh*
            # cross below the 20-day MA / Bollinger band from a persistent
            # below-state (the latter must coast, not re-run the pipeline daily).
            tail = gw.ohlcv_tail(trade_date, rows=25)
            closes = [float(r) for r in tail["Close"].tolist()]
        except Exception:
            closes = []
        return detect_technical_breakdown(close, indicators, closes)

    @staticmethod
    def _technical_improvement(gw: DataGateway, trade_date: str, close: float | None,
                               indicators: dict | None) -> str | None:
        """Technical-improvement trigger (reversal / stabilization) that forces
        a re-decide during a coast — the mirror of the breakdown trigger."""
        from webapp.engine.news_gate import detect_technical_improvement

        try:
            # Same 25-row tail so the detector can distinguish a *fresh* cross
            # back above the 20-day line from a persistent above-state.
            tail = gw.ohlcv_tail(trade_date, rows=25)
            closes = [float(r) for r in tail["Close"].tolist()]
        except Exception:
            closes = []
        return detect_technical_improvement(close, indicators, closes)

    async def _restore_coast(self, gw: DataGateway, start_index: int) -> dict | None:
        """Rebuild the standing-decision plan after a server restart / resume.

        The last non-coast daily record holds everything the coast needs: the
        decision JSON (band + recheck window) and its day index. The news
        signature is recomputed deterministically from the gateway caches for
        the origin date, so "new news" detection keeps working. Returns None
        when nothing can be safely rebuilt — the caller then re-decides.
        """
        row = db.query_one(
            "SELECT day_index, date, action, decision_json, context_json FROM daily_records "
            "WHERE session_id=? AND day_index < ? AND action != 'coast' "
            "ORDER BY day_index DESC LIMIT 1",
            (self.session_id, start_index),
        )
        if row is None or not row["decision_json"]:
            return None
        # A risk exit or a persisted cooldown must always resume with a fresh
        # thesis after the cooldown, never revive the pre-stop coast plan.
        try:
            context = json.loads(row["context_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            context = {}
        if row["action"] == "cooldown" or context.get("risk_control"):
            return None
        try:
            decision = Decision.model_validate_json(row["decision_json"])
        except Exception:
            return None
        lower, upper = decision.recheck_lower, decision.recheck_upper
        if not (lower and upper and 0 < lower < upper):
            return None
        return {
            "until_index": row["day_index"] + max(1, min(decision.recheck_days, 10)),
            "lower": float(lower),
            "upper": float(upper),
            "origin_index": row["day_index"],
            "origin_date": row["date"],
            "decision_json": row["decision_json"],
            "news_sig": await asyncio.to_thread(gw.news_signature, row["date"]),
        }

    def _cooldown_until_index(self, start_index: int) -> int:
        """Rebuild a full-risk-exit cooldown after pause/restart.

        The risk row itself is the durable source of truth.  This leaves all
        historic sessions untouched and only changes the execution policy for
        the two trading days immediately following their last full risk exit.
        """
        row = db.query_one(
            "SELECT day_index, context_json FROM daily_records WHERE session_id=? "
            "AND action='sell' ORDER BY day_index DESC LIMIT 1",
            (self.session_id,),
        )
        if row is None:
            return start_index
        try:
            context = json.loads(row["context_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            return start_index
        if not context.get("risk_control") or not context.get("full_exit"):
            return start_index
        return max(start_index, int(row["day_index"]) + 3)

    def _cash_idle_days(self, day_index: int) -> int:
        """Trading days since the most recent *executed* order of either side."""
        last_trade = db.query_one(
            "SELECT day_index, date, execution_date FROM daily_records WHERE session_id=? "
            "AND executed_shares IS NOT NULL AND executed_shares > 0 "
            "AND action IN ('buy', 'sell') ORDER BY day_index DESC LIMIT 1",
            (self.session_id,),
        )
        if not last_trade:
            return max(0, day_index + 1)
        # next_open rows are indexed by their signal day, but cash only
        # changes on the following session.  Starting the idle clock from the
        # signal index creates false "idle cash" pressure on that fill day.
        record_index = int(last_trade["day_index"])
        keys = last_trade.keys()
        execution_date = last_trade["execution_date"] if "execution_date" in keys else None
        record_date = last_trade["date"] if "date" in keys else None
        execution_index = record_index + int(bool(execution_date and record_date and execution_date != record_date))
        return max(0, day_index - execution_index)

    def _force_initial_buy(self, day_index: int, trade_date: str, gw: DataGateway,
                           portfolio: Portfolio, exec_model: ExecutionModel, *,
                           execution_date: str, execution_timing: str) -> None:
        """Policy day: submit a full-position order without asking the LLM.

        Evidence from real sessions: an LLM asked "enter or stay flat?" from
        cash answers hold (49 consecutive holds with position 0.0 in one
        session), and the full pipeline rates Underweight/Sell on a flat book
        (recorded as rejected). Seeding the book deterministically reframes
        every later decision as *position management* — trim / add / exit —
        which is the behavior the framework is actually good at.
        """
        self.agent.reset_day()
        close_t = gw.close_on(trade_date)
        decision = Decision(
            action="buy", position_pct=1.0, confidence=1.0,
            reasoning="策略规则：首个交易日强制满仓建仓（引擎执行，非模型决策）",
            key_signals=["policy: initial full position"],
            used_skills=[],
            recheck_days=1,
        )
        if execution_timing == "next_open":
            bar = gw.execution_bar_on(execution_date)
            fill = self._execute_at_next_open(
                decision, portfolio, exec_model, market=gw.market,
                prior_close=close_t, execution_open=bar["open"],
                execution_volume=bar["volume"], ticker=gw.ticker,
            )
            mark_close = bar["close"]
        else:
            fill = self._execute(decision, portfolio, exec_model, close_t)
            mark_close = close_t
        self._update_trades(execution_date, fill, portfolio)
        equity = portfolio.equity(mark_close)
        db.execute(
            "INSERT OR REPLACE INTO daily_records "
            "(session_id, day_index, date, decision_date, execution_date, valuation_date, execution_status, "
            " decision_json, action, requested_pct, "
            " executed_shares, executed_price, fee, cash_after, shares_after, equity, "
            " data_flags, skills_injected, context_json, prompt_text, response_text, "
            " llm_calls, latency_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.session_id, day_index, trade_date,
                trade_date, execution_date,
                execution_date,
                "filled" if fill.action in ("buy", "sell") else (
                    "unfilled" if fill.action == "rejected" else "not_required"
                ),
                decision.model_dump_json(),
                fill.action, fill.requested_pct,
                fill.shares, fill.price, fill.fee,
                round(portfolio.cash, 2), round(portfolio.shares, 4), round(equity, 2),
                json.dumps([f"policy: 首日强制满仓；信号 {trade_date}，执行 {execution_date}"], ensure_ascii=False),
                "[]",
                json.dumps({"close": close_t, "mark_close": mark_close,
                             "policy": "force_initial_full", "decision_date": trade_date,
                             "execution_date": execution_date,
                             "valuation_date": execution_date},
                           ensure_ascii=False),
                None, None, 0, 0,
            ),
        )
        db.execute(
            "UPDATE sessions SET current_day_index=?, cash=?, shares=?, avg_cost=? WHERE id=?",
            (day_index + 1, round(portfolio.cash, 2), round(portfolio.shares, 4),
             round(portfolio.avg_cost, 4), self.session_id),
        )
        logger.info("session %s day %s (%s): policy full-position buy %.0f @ %s",
                    self.session_id, day_index, trade_date, fill.shares, fill.price)

    async def _run_day(self, day_index: int, trade_date: str, gw: DataGateway,
                       portfolio: Portfolio, exec_model: ExecutionModel,
                       extra_flags: list[str] | None = None,
                       news_delta: list[dict] | None = None,
                       prev_coast: dict | None = None,
                       *, execution_date: str | None = None,
                       execution_timing: str = "same_close") -> dict | None:
        """Run one full LLM-decision day. Returns the coast plan for the days ahead."""
        execution_date = execution_date or trade_date
        self.agent.reset_day()
        flags: list[str] = list(extra_flags or [])
        t0 = asyncio.get_event_loop().time()

        # 1) data (clamped to T; blocking calls in threads)
        # 25 rows (not 20) so the hybrid scheduler's technical-breakdown check
        # has 21+ closes to detect a fresh trend cross vs. a persistent state.
        tail = await asyncio.to_thread(gw.ohlcv_tail, trade_date, 25)
        indicators = await asyncio.to_thread(gw.indicator_snapshot, trade_date)
        news, news_flags = await asyncio.to_thread(gw.news, trade_date)
        flags.extend(news_flags)
        # Broker research is the highest-signal text we get, so it leads the
        # message section; a 60-day window keeps it populated where a 7-day one
        # would almost always be empty for a single name.
        research, research_flags = await asyncio.to_thread(gw.reports, trade_date)
        flags.extend(research_flags)
        news = research + news
        fundamentals, fund_flags = await asyncio.to_thread(gw.fundamentals, trade_date)
        flags.extend(fund_flags)
        sentiment, sent_flags = await asyncio.to_thread(gw.sentiment, trade_date)
        flags.extend(sent_flags)
        benchmark, bench_flags = await asyncio.to_thread(gw.benchmark_tail, trade_date)
        flags.extend(bench_flags)

        close_t = float(tail.iloc[-1]["Close"])
        # The decision is made only after this signal session has closed, so
        # the portfolio state must be marked at T's close.  The order (if any)
        # is still filled at T+1's open below; keeping the snapshot here avoids
        # both stale T-1 equity and any T+1 price leakage in the prompt.
        portfolio_now = portfolio.snapshot(close_t)
        portfolio_now["drawdown"] = round(self._current_drawdown(portfolio.equity(close_t)), 4)

        # Idle-cash nudge into the prompt's flags section.
        cash_note = _cash_idle_note(
            portfolio.cash / portfolio_now["equity"] if portfolio_now["equity"] else 0.0,
            self._cash_idle_days(day_index),
        )
        if cash_note:
            flags.append(cash_note)

        # recent days for coherence — including each decision's own plan levels
        # (stop / take-profit / target weight) so the model can FOLLOW THROUGH
        # on its stated triggers instead of re-deriving a fresh "wait" opinion.
        recent_rows = db.query(
            "SELECT date, action, executed_shares, executed_price, decision_json "
            "FROM daily_records WHERE session_id=? ORDER BY day_index DESC LIMIT 5",
            (self.session_id,),
        )
        recent_days = []
        for r in reversed(recent_rows):
            plan = ""
            try:
                dj = json.loads(r["decision_json"] or "{}")
                bits = []
                if dj.get("stop_loss"):
                    bits.append(f"止损{float(dj['stop_loss']):g}")
                if dj.get("take_profit"):
                    bits.append(f"止盈{float(dj['take_profit']):g}")
                if dj.get("target_position_pct") is not None:
                    bits.append(f"目标仓{float(dj['target_position_pct']):.0%}")
                if bits:
                    plan = "计划: " + "/".join(bits)
            except Exception:
                pass
            recent_days.append(
                {
                    "date": r["date"],
                    "action": r["action"],
                    "detail": (
                        f"{r['executed_shares']}股@{r['executed_price']}"
                        if r["action"] in ("buy", "sell") and r["executed_shares"] else ""
                    ),
                    "plan": plan,
                }
            )

        # 2) skill injection (temporal clamp inside select_for_day)
        skills_today = self.skills.select_for_day(trade_date)

        # 3) decision (1 LLM call)
        ctx = DailyContext(
            symbol=gw.ticker, market=gw.market, sim_date=trade_date,
            ohlcv_tail_csv=self._tail_csv(tail),
            indicators=indicators,
            news=news,
            fundamentals=fundamentals,
            sentiment=sentiment,
            benchmark=benchmark,
            portfolio=portfolio_now,
            recent_days=recent_days,
            data_flags=flags,
            skills=skills_today,
            # escalation inputs for the hybrid scheduler
            news_delta=list(news_delta or []),
            prev_decision=(
                json.loads(prev_coast["decision_json"])
                if prev_coast and prev_coast.get("decision_json") else None
            ),
            coast=prev_coast,
        )
        decision, prompt_text, response_text, dec_flags = await asyncio.to_thread(
            self.agent.decide, ctx
        )
        flags.extend(dec_flags)

        # All configured architectures (fast, adaptive, full graph, and the
        # graph-backed hybrid) converge here.  Do not rely on a particular
        # agent's reconciliation path for an execution safety invariant.
        # Re-running this guard after the fast agent is harmless: it is
        # idempotent once the decision has already been converted to ``hold``.
        apply_market_trade_guard(decision, ctx, flags)

        # 4) The signal is made with T-close data.  New sessions fill only at
        # T+1's opening auction; old persisted sessions keep their recorded
        # same-close convention so resume never mixes timing models.
        # A hold (or a sell from a flat long-only book) is not an order.  Do
        # not inspect a future opening auction merely to confirm a no-op: that
        # would leak T+1 data and falsely label the row as executed.
        requires_execution = (
            (decision.action == "buy" and decision.position_pct > 0)
            or (decision.action == "sell" and decision.position_pct > 0 and portfolio.shares > 0)
        )
        if execution_timing == "next_open" and requires_execution:
            execution_bar = await asyncio.to_thread(gw.execution_bar_on, execution_date)
            fill = self._execute_at_next_open(
                decision, portfolio, exec_model, market=gw.market,
                prior_close=close_t, execution_open=execution_bar["open"],
                execution_volume=execution_bar["volume"], ticker=gw.ticker,
            )
            mark_close = float(execution_bar["close"])
            record_execution_date = execution_date
            valuation_date = execution_date
        else:
            fill = self._execute(decision, portfolio, exec_model, close_t)
            mark_close = close_t
            record_execution_date = trade_date if requires_execution else None
            valuation_date = trade_date
        if fill.action == "rejected" or fill.reason.startswith("sell ignored"):
            flags.append(f"execution: {fill.reason}")

        # 5) round-trip bookkeeping (skill attribution happens on position close)
        self._update_trades(record_execution_date or trade_date, fill, portfolio)

        # 6) record day
        equity = portfolio.equity(mark_close)
        latency_ms = int((asyncio.get_event_loop().time() - t0) * 1000)
        db.execute(
            "INSERT OR REPLACE INTO daily_records "
            "(session_id, day_index, date, decision_date, execution_date, valuation_date, execution_status, "
            " decision_json, action, requested_pct, "
            " executed_shares, executed_price, fee, cash_after, shares_after, equity, "
            " data_flags, skills_injected, context_json, prompt_text, response_text, "
            " llm_calls, latency_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.session_id, day_index, trade_date,
                trade_date, record_execution_date,
                valuation_date,
                "filled" if fill.action in ("buy", "sell") else (
                    "unfilled" if fill.action == "rejected" else "not_required"
                ),
                decision.model_dump_json(),
                fill.action, fill.requested_pct if fill.action in ("buy", "sell", "rejected") else 0.0,
                fill.shares, fill.price, fill.fee,
                round(portfolio.cash, 2), round(portfolio.shares, 4), round(equity, 2),
                json.dumps(flags, ensure_ascii=False),
                json.dumps(skills_today, ensure_ascii=False),
                json.dumps({
                    "ohlcv_tail_last_date": str(tail.iloc[-1]["Date"].date()),
                    "decision_date": trade_date,
                    "execution_date": record_execution_date,
                    "valuation_date": valuation_date,
                    "execution_timing": execution_timing,
                    "mark_close": mark_close,
                    "indicators": indicators,
                    "news_count": len(news),
                    "news_kinds": {
                        k: sum(1 for n in news if n.get("kind") == k)
                        for k in ("research", "news", "notice", "macro")
                    },
                    "fundamentals": fundamentals,
                    "sentiment": sentiment,
                    "portfolio_at_decision": portfolio_now,
                }, ensure_ascii=False),
                prompt_text, response_text,
                self.agent._call_count, latency_ms,
            ),
        )
        db.execute(
            "UPDATE sessions SET current_day_index=?, cash=?, shares=?, avg_cost=?, "
            "llm_call_count=llm_call_count+? WHERE id=?",
            (day_index + 1, round(portfolio.cash, 2), round(portfolio.shares, 4),
             round(portfolio.avg_cost, 4), self.agent._call_count, self.session_id),
        )
        # Bookkeeping rows: a hybrid stand-pat day may make zero LLM calls —
        # don't record a fake call row for it.
        if self.agent._call_count > 0:
            db.execute(
                "INSERT INTO llm_calls (session_id, day_index, purpose, model, latency_ms, ok, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (self.session_id, day_index, "decide", self.settings.model, latency_ms, 1, _now()),
            )
        logger.info("session %s day %s (%s): %s", self.session_id, day_index, trade_date, fill.action)
        return self._make_coast(decision, close_t, day_index, trade_date,
                                news_sig=await asyncio.to_thread(gw.news_signature, trade_date))

    @staticmethod
    def _make_coast(decision: Decision, close_t: float, day_index: int,
                    trade_date: str, news_sig: tuple = ()) -> dict:
        """Build the standing-decision plan from the model's band + horizon.

        An order generated by the decision is filled on the next session open
        for new sessions; coasting only means "don't re-query the LLM" while
        the close stays inside the band, before the horizon, and no material
        news has landed since the decision was taken.
        """
        lower, upper = decision.recheck_lower, decision.recheck_upper
        if not (lower and upper and 0 < lower < upper):
            lower = close_t * 0.97
            upper = close_t * 1.03
        return {
            "until_index": day_index + max(1, min(decision.recheck_days, 10)),
            "lower": float(lower),
            "upper": float(upper),
            "origin_index": day_index,
            "origin_date": trade_date,
            "decision_json": decision.model_dump_json(),
            "news_sig": tuple(news_sig or ()),
        }

    async def _record_coast_day(self, day_index: int, trade_date: str, close_t: float,
                                gw: DataGateway, coast: dict, *,
                                execution_timing: str = "same_close") -> None:
        """A day where the standing decision still holds: no LLM call, mark-to-market only."""
        db.execute(
            "INSERT OR REPLACE INTO daily_records "
            "(session_id, day_index, date, decision_date, execution_date, valuation_date, execution_status, "
            " decision_json, action, requested_pct, "
            " executed_shares, executed_price, fee, cash_after, shares_after, equity, "
            " data_flags, skills_injected, context_json, prompt_text, response_text, "
            " llm_calls, latency_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.session_id, day_index, trade_date,
                trade_date, None, trade_date, "not_required",
                coast["decision_json"],
                "coast", 0.0, None, None, 0.0,
                round(self._last_cash(), 2), round(self._last_shares(), 4),
                round(self._last_cash() + self._last_shares() * close_t, 2),
                json.dumps([f"coast: 沿用 {coast['origin_date']} 的决策，"
                            f"有效区间 {coast['lower']:.2f}~{coast['upper']:.2f}，未触发重新决策"],
                           ensure_ascii=False),
                "[]",
                json.dumps({"close": close_t, "band": [coast["lower"], coast["upper"]],
                             "origin_date": coast["origin_date"], "decision_date": trade_date,
                             "execution_date": None, "valuation_date": trade_date,
                             "execution_timing": execution_timing}, ensure_ascii=False),
                None, None, 0, 0,
            ),
        )
        db.execute(
            "UPDATE sessions SET current_day_index=? WHERE id=?",
            (day_index + 1, self.session_id),
        )
        logger.info("session %s day %s (%s): coast (no LLM call)", self.session_id, day_index, trade_date)

    async def _record_cooldown_day(self, day_index: int, trade_date: str, close_t: float,
                                   portfolio: Portfolio, cooldown_until_index: int, *,
                                   execution_timing: str = "same_close") -> None:
        """Mark-to-market a mandatory post-stop no-trade day without any LLM call."""
        remaining = cooldown_until_index - day_index
        decision = Decision(
            action="hold", position_pct=0.0, confidence=1.0,
            reasoning=f"风控清仓后的冷静期（剩余 {remaining} 个交易日）：不调用模型、不允许重新入场。",
            key_signals=["risk-control: full-exit cooldown"],
            used_skills=[], recheck_days=1,
        )
        db.execute(
            "INSERT OR REPLACE INTO daily_records "
            "(session_id, day_index, date, decision_date, execution_date, valuation_date, execution_status, "
            " decision_json, action, requested_pct, "
            " executed_shares, executed_price, fee, cash_after, shares_after, equity, "
            " data_flags, skills_injected, context_json, prompt_text, response_text, "
            " llm_calls, latency_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.session_id, day_index, trade_date,
                trade_date, None, trade_date, "not_required", decision.model_dump_json(),
                "cooldown", 0.0, None, None, 0.0,
                round(portfolio.cash, 2), round(portfolio.shares, 4),
                round(portfolio.equity(close_t), 2),
                json.dumps([f"risk-control: 冷静期第 {3 - remaining} 日，禁止重新入场"], ensure_ascii=False),
                "[]",
                json.dumps({"close": close_t, "cooldown_until_index": cooldown_until_index,
                             "decision_date": trade_date, "execution_date": None,
                             "valuation_date": trade_date,
                             "execution_timing": execution_timing}, ensure_ascii=False),
                None, None, 0, 0,
            ),
        )
        db.execute(
            "UPDATE sessions SET current_day_index=?, cash=?, shares=?, avg_cost=? WHERE id=?",
            (day_index + 1, round(portfolio.cash, 2), round(portfolio.shares, 4),
             round(portfolio.avg_cost, 4), self.session_id),
        )
        logger.info("session %s day %s (%s): full-risk-exit cooldown", self.session_id, day_index, trade_date)

    def _record_final_valuation(self, day_index: int, trade_date: str, close_t: float,
                                portfolio: Portfolio, *, execution_timing: str) -> None:
        """Persist the terminal mark of a next-open session without an order/LLM call.

        ``day_index`` is the final trading-day index, which the next-open
        signal loop intentionally never consumes.  ``INSERT OR REPLACE``
        makes recovery/re-run idempotent while the unique primary key prevents
        duplicate chart points.
        """
        decision = Decision(
            action="hold", position_pct=0.0, confidence=1.0,
            reasoning="回测区间最后一个交易日收盘估值；无后续交易日，不生成订单。",
            key_signals=["valuation: terminal close"], used_skills=[], recheck_days=1,
        )
        db.execute(
            "INSERT OR REPLACE INTO daily_records "
            "(session_id, day_index, date, decision_date, execution_date, valuation_date, execution_status, "
            " decision_json, action, requested_pct, "
            " executed_shares, executed_price, fee, cash_after, shares_after, equity, "
            " data_flags, skills_injected, context_json, prompt_text, response_text, "
            " llm_calls, latency_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.session_id, day_index, trade_date,
                None, None, trade_date, "not_required", decision.model_dump_json(),
                "final_mark", 0.0, None, None, 0.0,
                round(portfolio.cash, 2), round(portfolio.shares, 4),
                round(portfolio.equity(close_t), 2),
                json.dumps(["valuation: 最后交易日收盘，仅估值不下单"], ensure_ascii=False), "[]",
                json.dumps({"close": close_t, "valuation_date": trade_date,
                            "execution_timing": execution_timing,
                            "terminal_valuation": True}, ensure_ascii=False),
                None, None, 0, 0,
            ),
        )
        db.execute(
            "UPDATE sessions SET current_day_index=?, cash=?, shares=?, avg_cost=? WHERE id=?",
            (day_index + 1, round(portfolio.cash, 2), round(portfolio.shares, 4),
             round(portfolio.avg_cost, 4), self.session_id),
        )
        logger.info("session %s day %s (%s): final valuation", self.session_id, day_index, trade_date)

    # ---- hard risk control (stop-loss / take-profit / drawdown) ----

    async def _check_risk_stops(self, day_index: int, trade_date: str,
                                execution_date: str, execution_timing: str,
                                gw: DataGateway, portfolio: Portfolio, exec_model: ExecutionModel,
                                coast: dict | None) -> bool:
        """Enforce stop-loss / take-profit / drawdown circuit-breaker.

        Runs every day (including coast days), before the coast/LLM decision,
        and is fully deterministic — it does not depend on whether the model
        decides to re-evaluate. Returns True if a risk action was executed.
        """
        self._last_risk_was_full_exit = False
        if portfolio.shares <= 0:
            return False

        close_t = await asyncio.to_thread(gw.close_on, trade_date)
        equity_now = portfolio.equity(close_t)
        if self._peak_equity is None or equity_now > self._peak_equity:
            self._peak_equity = equity_now

        # A stop that was triggered earlier but could not leave the book at a
        # limit-down opening is an outstanding deterministic risk order, not
        # a fresh LLM decision.  Retry it before looking at a new stop level
        # or a standing thesis; otherwise a one-word limit would make the
        # forced exit disappear from the audit trail on the following day.
        pending_risk = self._pending_unfilled_risk()
        if pending_risk is not None:
            return self._exec_risk_sell(
                day_index, trade_date, execution_date, execution_timing,
                close_t, gw, portfolio, exec_model,
                reason=pending_risk.reasoning,
                sell_pct=pending_risk.position_pct,
                risk_retry=True,
            )

        stop_loss = take_profit = None
        if coast and coast.get("decision_json"):
            try:
                d = Decision.model_validate_json(coast["decision_json"])
                stop_loss = d.stop_loss
                take_profit = d.take_profit
            except Exception:
                pass

        # Engine-side fallback stop-loss. The model's stop is free-text and
        # frequently absent (the PM/trader quote an entry and target but no
        # stop). Without one, a long book rides all the way down to the
        # -20% drawdown breaker with no earlier exit. Anchor to average cost
        # (NOT today's close, which would ratchet the stop every day) so the
        # stop is stable while the position is open.
        stop_is_fallback = False
        fallback = self.settings.fallback_stop_loss_pct
        if (stop_loss is None or stop_loss <= 0) and fallback and portfolio.avg_cost > 0:
            stop_loss = portfolio.avg_cost * (1 - float(fallback))
            stop_is_fallback = True

        # 1) Drawdown circuit-breaker (highest priority).
        if self._peak_equity and self._peak_equity > 0:
            dd = equity_now / self._peak_equity - 1
            if dd <= -self.settings.max_drawdown_stop_pct:
                return self._exec_risk_sell(
                    day_index, trade_date, execution_date, execution_timing,
                    close_t, gw, portfolio, exec_model,
                    reason=(
                        f"回撤熔断：权益自峰值回撤 {dd:.1%}（超过上限 "
                        f"{self.settings.max_drawdown_stop_pct:.0%}），"
                        f"强制降至目标仓位 {self.settings.drawdown_reduce_to_pct:.0%}"
                    ),
                    sell_pct=self._sell_to_target(close_t, portfolio, self.settings.drawdown_reduce_to_pct),
                )

        # 2) Stop-loss (full exit for a long book).
        if stop_loss and 0 < stop_loss and close_t <= stop_loss:
            source = "兜底" if stop_is_fallback else ""
            return self._exec_risk_sell(
                day_index, trade_date, execution_date, execution_timing,
                close_t, gw, portfolio, exec_model,
                reason=f"止损触发（{source or '模型'}）：收盘 {close_t:g} 跌破止损位 {stop_loss:g}，清仓离场",
                sell_pct=1.0,
            )

        # 3) Take-profit (bank half on reaching the first target).
        if take_profit and 0 < take_profit and close_t >= take_profit:
            return self._exec_risk_sell(
                day_index, trade_date, execution_date, execution_timing,
                close_t, gw, portfolio, exec_model,
                reason=f"止盈触发：收盘 {close_t:g} 触及止盈位 {take_profit:g}，兑现一半锁定利润",
                sell_pct=0.5,
            )

        return False

    @staticmethod
    def _sell_to_target(close_t: float, portfolio: Portfolio, target_pos: float) -> float:
        """Fraction of shares to sell to bring position down to target_pos."""
        equity = portfolio.equity(close_t)
        if equity <= 0:
            return 1.0
        current = portfolio.shares * close_t / equity
        if current <= target_pos:
            return 0.0
        return max(0.0, min(1.0, 1.0 - target_pos / current))

    def _pending_unfilled_risk(self) -> Decision | None:
        """Return the newest blocked deterministic risk order, if any."""
        row = db.query_one(
            "SELECT decision_json FROM daily_records "
            "WHERE session_id=? AND execution_status='unfilled' "
            "AND data_flags LIKE '%risk-control:%' "
            "ORDER BY day_index DESC LIMIT 1",
            (self.session_id,),
        )
        if not row or not row["decision_json"]:
            return None
        try:
            decision = Decision.model_validate_json(row["decision_json"])
        except Exception:
            return None
        return decision if decision.action == "sell" and decision.position_pct > 0 else None

    def _exec_risk_sell(self, day_index: int, trade_date: str, execution_date: str,
                        execution_timing: str, close_t: float, gw: DataGateway,
                        portfolio: Portfolio, exec_model: ExecutionModel,
                        reason: str, sell_pct: float, *, risk_retry: bool = False) -> bool:
        """Execute and audit a deterministic risk sell, including blocked orders."""
        if sell_pct <= 0 or portfolio.shares <= 0:
            return False
        decision = Decision(
            action="sell", position_pct=round(sell_pct, 4), confidence=1.0,
            reasoning=reason, key_signals=[f"risk-control: {reason}"], used_skills=[],
            recheck_days=1,
        )
        if execution_timing == "next_open":
            bar = gw.execution_bar_on(execution_date)
            fill = self._execute_at_next_open(
                decision, portfolio, exec_model, market=gw.market,
                prior_close=close_t, execution_open=bar["open"],
                execution_volume=bar["volume"], ticker=gw.ticker,
            )
            mark_close = float(bar["close"])
        else:
            fill = self._execute(decision, portfolio, exec_model, close_t)
            mark_close = close_t
        filled = fill.action == "sell"
        if filled:
            self._update_trades(execution_date, fill, portfolio)
        self._last_risk_was_full_exit = fill.action == "sell" and portfolio.shares <= 0
        equity = portfolio.equity(mark_close)
        db.execute(
            "INSERT OR REPLACE INTO daily_records "
            "(session_id, day_index, date, decision_date, execution_date, valuation_date, execution_status, "
            " decision_json, action, requested_pct, "
            " executed_shares, executed_price, fee, cash_after, shares_after, equity, "
            " data_flags, skills_injected, context_json, prompt_text, response_text, "
            " llm_calls, latency_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.session_id, day_index, trade_date,
                trade_date, execution_date,
                execution_date,
                "filled" if fill.action in ("buy", "sell") else (
                    "unfilled" if fill.action == "rejected" else "not_required"
                ),
                decision.model_dump_json(),
                fill.action, fill.requested_pct,
                fill.shares, fill.price, fill.fee,
                round(portfolio.cash, 2), round(portfolio.shares, 4), round(equity, 2),
                json.dumps([f"risk-control: {reason}"], ensure_ascii=False),
                "[]",
                json.dumps({
                    "close": close_t,
                    "mark_close": mark_close,
                    "decision_date": trade_date,
                    "execution_date": execution_date,
                    "valuation_date": execution_date,
                    "execution_timing": execution_timing,
                    "risk_control": reason,
                    "risk_retry": risk_retry,
                    "full_exit": self._last_risk_was_full_exit,
                }, ensure_ascii=False),
                None, None, 0, 0,
            ),
        )
        db.execute(
            "UPDATE sessions SET current_day_index=?, cash=?, shares=?, avg_cost=? WHERE id=?",
            (day_index + 1, round(portfolio.cash, 2), round(portfolio.shares, 4),
             round(portfolio.avg_cost, 4), self.session_id),
        )
        logger.info("session %s day %s (%s): risk-control %s%s",
                    self.session_id, day_index, trade_date, fill.action,
                    " (retry)" if risk_retry else "")
        # A rejected risk order is still handled: it has been persisted and
        # the loop must not fall through to an LLM decision on this day.
        return True

    def _current_drawdown(self, equity_now: float) -> float:
        """Current drawdown vs the running peak (<= 0)."""
        if self._peak_equity and self._peak_equity > 0:
            return equity_now / self._peak_equity - 1
        return 0.0

    def _last_cash(self) -> float:
        row = db.query_one("SELECT cash FROM sessions WHERE id=?", (self.session_id,))
        return row["cash"] if row else 0.0

    def _last_shares(self) -> float:
        row = db.query_one("SELECT shares FROM sessions WHERE id=?", (self.session_id,))
        return row["shares"] if row else 0.0

    # ---- helpers ----

    @staticmethod
    def _tail_csv(tail: pd.DataFrame) -> str:
        lines = ["Date,Open,High,Low,Close,Volume"]
        for _, r in tail.iterrows():
            lines.append(
                f"{r['Date'].strftime('%Y-%m-%d')},{r['Open']:.4f},{r['High']:.4f},"
                f"{r['Low']:.4f},{r['Close']:.4f},{r['Volume']:.0f}"
            )
        return "\n".join(lines)

    @staticmethod
    def _execute(decision: Decision, portfolio: Portfolio,
                 exec_model: ExecutionModel, close_t: float) -> Fill:
        # A long-only book cannot honor a sell from flat — and with the full
        # pipeline rating Underweight/Sell on a flat book this used to waste
        # whole decision days as "rejected". Record a no-op hold instead.
        if decision.action == "sell" and portfolio.shares <= 0:
            return Fill(action="hold", requested_pct=decision.position_pct,
                        reason="sell ignored: no open position (long-only)")
        try:
            fill = exec_model.validate_and_fill(decision, portfolio, close_t)
        except InvalidDecision as exc:
            return Fill(action="rejected", requested_pct=decision.position_pct,
                        reason=exc.reason)
        if fill.action == "buy":
            portfolio.apply_fill("buy", fill.price, fill.shares, fill.fee)
        elif fill.action == "sell":
            portfolio.apply_fill("sell", fill.price, fill.shares, fill.fee)
        return fill

    @staticmethod
    def _execute_at_next_open(decision: Decision, portfolio: Portfolio,
                              exec_model: ExecutionModel, *, market: str,
                              prior_close: float, execution_open: float,
                              execution_volume: float = 0.0,
                              ticker: str = "") -> Fill:
        """Fill an order at the following session's opening auction.

        The decision has already been made with ``prior_close`` data.  This
        method deliberately receives only the opening price needed to fill it;
        execution-day high/low/close are never decision inputs.  Daily OHLCV
        does not expose an order book, so a Chinese stock opening exactly at
        the board's daily limit is conservatively marked unfilled.
        """
        if execution_open <= 0 or execution_volume <= 0:
            return Fill(action="rejected", requested_pct=decision.position_pct,
                        reason="no executable opening auction")
        if market == "cn" and prior_close > 0:
            code = ticker.split(".", 1)[0]
            # ChiNext/STAR have ±20% normal limits; BJ normal limit is ±30%.
            # ST and special resumption days require instrument metadata that
            # daily OHLCV does not carry, so they are intentionally not guessed.
            limit_pct = 0.30 if ticker.upper().endswith(".BJ") else (
                0.20 if code.startswith(("30", "68")) else 0.10
            )
            limit_up = round(prior_close * (1 + limit_pct), 2)
            limit_down = round(prior_close * (1 - limit_pct), 2)
            if decision.action == "buy" and execution_open >= limit_up:
                return Fill(action="rejected", requested_pct=decision.position_pct,
                            reason="A-share limit-up at open; buy left unfilled")
            if decision.action == "sell" and execution_open <= limit_down:
                return Fill(action="rejected", requested_pct=decision.position_pct,
                            reason="A-share limit-down at open; sell left unfilled")
        return BacktestEngine._execute(decision, portfolio, exec_model, execution_open)

    def _open_trade(self, trade_date: str, fill: Fill) -> None:
        db.execute(
            "INSERT INTO trades (session_id, symbol, side, entry_date, entry_price, shares, fees, "
            "status, opened_day_index, entry_fee, sold_shares, partial_realized_pnl) "
            "SELECT ?, canonical_ticker, 'open long', ?, ?, ?, ?, 'open', current_day_index, ?, 0, 0 "
            "FROM sessions WHERE id=?",
            (self.session_id, trade_date, fill.price, fill.shares, fill.fee, fill.fee, self.session_id),
        )

    def _update_trades(self, trade_date: str, fill: Fill, portfolio: Portfolio) -> None:
        open_trade = db.query_one(
            "SELECT * FROM trades WHERE session_id=? AND status='open' ORDER BY id DESC LIMIT 1",
            (self.session_id,),
        )
        if fill.action == "buy" and open_trade is None:
            self._open_trade(trade_date, fill)
        elif fill.action == "sell" and open_trade is not None:
            remaining = portfolio.shares
            closed = open_trade if remaining <= 0 else None
            if closed is not None:
                # Full close. The revenue side only counts the final slice;
                # every earlier partial slice was booked into
                # partial_realized_pnl as it happened, so a scaled-out trade
                # reports its real total PnL (entry fee charged once).
                invested = open_trade["entry_price"] * open_trade["shares"] + open_trade["entry_fee"]
                pnl = (
                    (fill.price - open_trade["entry_price"]) * fill.shares
                    - fill.fee
                    - open_trade["entry_fee"]
                    + (open_trade["partial_realized_pnl"] or 0.0)
                )
                ret = (pnl / invested * 100) if invested > 0 else 0.0
                db.execute(
                    "UPDATE trades SET side='close long', exit_date=?, exit_price=?, realized_pnl=?, "
                    "return_pct=?, status='closed', closed_day_index=(SELECT current_day_index FROM sessions WHERE id=?) "
                    "WHERE id=?",
                    (trade_date, fill.price, round(pnl, 2), round(ret, 3), self.session_id, open_trade["id"]),
                )
                self._attribute_skills(open_trade["opened_day_index"], pnl > 0)
            elif open_trade is not None:
                # Partial sell: book the realized slice immediately, then keep
                # the trade open for what is still held.
                slice_pnl = (fill.price - open_trade["entry_price"]) * fill.shares - fill.fee
                db.execute(
                    "UPDATE trades SET sold_shares=?, partial_realized_pnl=?, fees=? WHERE id=?",
                    (
                        round(open_trade["sold_shares"] + fill.shares, 4),
                        round((open_trade["partial_realized_pnl"] or 0.0) + slice_pnl, 4),
                        round(open_trade["fees"] + fill.fee, 4),
                        open_trade["id"],
                    ),
                )
        elif fill.action == "buy" and open_trade is not None:
            # add to position: average entry price; the cost basis resets, so
            # any partial-slice bookkeeping from before no longer applies
            total_shares = open_trade["shares"] + fill.shares
            new_price = (open_trade["entry_price"] * open_trade["shares"] + fill.price * fill.shares) / total_shares
            db.execute(
                "UPDATE trades SET entry_price=?, shares=?, fees=?, entry_fee=?, "
                "sold_shares=0, partial_realized_pnl=0 WHERE id=?",
                (round(new_price, 4), total_shares, round(open_trade["fees"] + fill.fee, 4),
                 fill.fee, open_trade["id"]),
            )

    def _attribute_skills(self, opened_day_index: int, profitable: bool) -> None:
        """Credit/blame the skills claimed by the decision that opened this trade."""
        row = db.query_one(
            "SELECT decision_json FROM daily_records WHERE session_id=? AND day_index=?",
            (self.session_id, opened_day_index),
        )
        if not row:
            return
        try:
            used = Decision.model_validate_json(row["decision_json"]).used_skills
        except Exception:
            return
        evidence = {
            "session": self.session_id,
            "opened_day_index": opened_day_index,
            "outcome": "profit" if profitable else "loss",
        }
        for skill_id in used:
            self.skills.apply_evidence(skill_id, profitable, evidence)

    def _finalize_metrics(self, frame: pd.DataFrame, days: list[str]) -> None:
        rows = db.query(
            "SELECT date, equity FROM daily_records WHERE session_id=? ORDER BY day_index",
            (self.session_id,),
        )
        equities = [r["equity"] for r in rows]
        if not equities:
            return
        initial = db.query_one("SELECT initial_capital FROM sessions WHERE id=?", (self.session_id,))["initial_capital"]
        final = equities[-1]
        peak = equities[0]
        max_dd = 0.0
        for e in equities:
            peak = max(peak, e)
            if peak > 0:
                max_dd = min(max_dd, (e / peak - 1) * 100)
        trades = db.query(
            "SELECT return_pct FROM trades WHERE session_id=? AND status='closed'",
            (self.session_id,),
        )
        wins = [t for t in trades if (t["return_pct"] or 0) > 0]
        win_rate = (len(wins) / len(trades) * 100) if trades else None
        db.execute(
            "UPDATE sessions SET final_equity=?, total_return_pct=?, max_drawdown_pct=?, win_rate=? WHERE id=?",
            (round(final, 2), round((final / initial - 1) * 100, 3), round(max_dd, 3),
             round(win_rate, 2) if win_rate is not None else None, self.session_id),
        )

    async def _run_review(self, gw: DataGateway, days: list[str]) -> None:
        """After the last day: LLM review of the session + skill distillation."""
        from webapp.skills.distiller import Distiller

        distiller = Distiller(self.settings, self.skills)
        await distiller.review_and_distill(self.session_id)
