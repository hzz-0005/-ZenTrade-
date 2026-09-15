"""Full TradingAgents multi-agent pipeline as the daily decision maker.

This is the "real" pipeline from the repo root, not the webapp's single-call
approximations:

    market / social / news / fundamentals analysts (tool-using)
        -> bull & bear researchers debate -> research manager
        -> trader (entry price, stop loss, sizing)
        -> aggressive / conservative / neutral risk debate -> risk manager
        -> portfolio manager (5-tier rating, price target, horizon)

The webapp's own vetted data (akshare OHLCV/indicators/fundamentals/news)
still feeds the prompt context and the chart, so the two data planes coexist:
the graph queries whatever its analysts ask for, the gateway guarantees the
execution price and the audit trail.
"""
from __future__ import annotations

import logging
import re
import time
from copy import deepcopy

from webapp.config import WebappSettings
from webapp.core.errors import LLMBudgetExceeded
from webapp.core.models import Decision
from webapp.engine.context_builder import DailyContext, band_regime, closes_from_csv

logger = logging.getLogger(__name__)

# Rating -> action direction only. Position magnitude is no longer hardcoded
# here: it is resolved by _size_position() from the PM's explicit target
# position plus risk factors (distance-to-stop, volatility, drawdown, conviction).
_RATING_ACTION = {
    "Buy": "buy",
    "Overweight": "buy",
    "Hold": "hold",
    "Underweight": "sell",
    "Sell": "sell",
}
_CONFIDENCE = {"Buy": 0.75, "Overweight": 0.60, "Hold": 0.45,
               "Underweight": 0.60, "Sell": 0.75}

_RE_RATING = re.compile(r"\*\*Rating\*\*\s*:\s*(\w+)")
_RE_SUMMARY = re.compile(r"\*\*Executive Summary\*\*\s*:\s*(.*?)(?=\n\*\*|\Z)", re.S)
_RE_THESIS = re.compile(r"\*\*Investment Thesis\*\*\s*:\s*(.*?)(?=\n\*\*|\Z)", re.S)
_RE_TARGET = re.compile(r"\*\*Price Target\*\*\s*:\s*(-?[\d.]+)")
_RE_STOP = re.compile(r"\*\*Stop Loss\*\*\s*:\s*(-?[\d.]+)")
_RE_TAKE_PROFIT = re.compile(r"\*\*Take Profit\*\*\s*:\s*(-?[\d.]+)")
_RE_TARGET_POS = re.compile(r"\*\*Target Position\*\*\s*:\s*(\d+(?:\.\d+)?)\s*%?")
_RE_RISK = re.compile(r"\*\*Risk Level\*\*\s*:\s*(\w+)")
_RE_HORIZON = re.compile(r"\*\*Time Horizon\*\*\s*:\s*(.+)")
_RE_ENTRY = re.compile(r"\*\*Entry Price\*\*\s*:\s*(-?[\d.]+)")
_RE_SIZING = re.compile(r"\*\*Position Sizing\*\*\s*:\s*(.+)")
_RE_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")


# A 4xx like this means the request itself is malformed — the graph handed the
# provider an inconsistent message list (e.g. tool_calls with no matching tool
# messages). Retrying sends the identical broken payload, so fail fast.
_FATAL_MARKERS = (
    "invalid_request_error", "invalid_api_key", "authentication_error",
    "permission_error", "context_length_exceeded", "unsupported",
    "'code': '401'", "'code': '403'", "error code: 401", "error code: 403",
    "error code: 400", "'code': '400'",
)
# Transient: the same request may well succeed a moment later.
_TRANSIENT_MARKERS = (
    "timeout", "timed out", "rate limit", "rate_limit", "429",
    "error code: 429", "connection", "temporarily", "overloaded",
    "error code: 500", "error code: 502", "error code: 503", "error code: 504",
    "server_error", "internal server error",
)


def _is_transient(exc: Exception) -> bool:
    """Whether retrying this exception has any chance of succeeding."""
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(marker in text for marker in _FATAL_MARKERS):
        return False
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def _first(pattern: re.Pattern, text: str) -> str | None:
    m = pattern.search(text or "")
    return m.group(1).strip() if m else None


def _to_float(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class GraphDecisionAgent:
    """Drives the full TradingAgents graph; maps its output to a webapp Decision."""

    def __init__(self, settings: WebappSettings, max_retries: int = 2,
                 retry_backoff: float = 10.0,
                 content_filter_retries: int = 2):
        self.settings = settings
        self._graph = None
        self._call_count = 0
        # Only transient failures are retried; see _propagate_with_retry.
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        # GLM (error 1301) sometimes trips its content filter on the model's
        # own generated summary of political-adjacent news. Sampling differs
        # per attempt, so these get their own small retry budget.
        self.content_filter_retries = content_filter_retries

    def reset_day(self) -> None:
        self._call_count = 0

    def _spend(self) -> None:
        """Charge one full-pipeline run against the daily graph budget.

        A graph run fans out ~9 LLM nodes, so it is budgeted separately from
        the cheap single/panel calls (max_llm_calls_per_day) via
        max_graph_runs_per_day. Previously _call_count was a bare counter —
        backtest_engine read it for the db but nothing enforced it, so a buggy
        loop could burn unbounded graph runs in a day.
        """
        self._call_count += 1
        if self._call_count > self.settings.max_graph_runs_per_day:
            raise LLMBudgetExceeded(
                f"exceeded {self.settings.max_graph_runs_per_day} graph runs "
                f"for this day ({self._call_count})"
            )

    # ---- graph lifecycle ----

    def _build_config(self) -> dict:
        from tradingagents.default_config import DEFAULT_CONFIG

        cfg = deepcopy(DEFAULT_CONFIG)
        webapp_data = self.settings.db_path.parent
        cfg.update({
            "llm_provider": self.settings.llm_provider,
            "deep_think_llm": self.settings.model,
            "quick_think_llm": self.settings.model,
            "backend_url": self.settings.backend_url,
            # Analysts reports and the final decision read better in Chinese;
            # the internal debate stays English for reasoning quality.
            "output_language": "Chinese",
            # One graph run per trading day; the webapp resumes at day level,
            # so LangGraph checkpoints would only add disk churn.
            "checkpoint_enabled": False,
            "data_cache_dir": str(webapp_data / "graph_cache"),
            "results_dir": str(webapp_data / "graph_results"),
            # Depth knobs: one debate round + one risk round keeps a full
            # pipeline affordable per day.
            "max_debate_rounds": 1,
            "max_risk_discuss_rounds": 1,
            "max_recur_limit": 100,
            # Parallel is now the default and safe: each analyst has its own
            # message channel and ToolNode (see AgentState + _create_tool_nodes),
            # so the four analysts run concurrently without orphaning each
            # other's tool_calls.
            "pipeline_mode": self.settings.graph_pipeline_mode,
            # GLM 的内容安全（1301）对政治敏感措辞极敏感——把宏观新闻查询里的
            # 地缘政治类主题换成中性经济主题，从源头减少审核触发面。
            "global_news_queries": [
                "Federal Reserve interest rates inflation",
                "S&P 500 earnings GDP economic outlook",
                "central bank policy energy prices",
                "oil commodities supply chain energy",
            ],
            # 回测里直接关闭流水线的宏观新闻工具（live 搜索结果=未来数据，
            # 且政治类头条会触发 GLM 1301）。新闻分析师会拿到诚实的
            # "已禁用"提示；消息面由 webapp 网关裁剪后的数据负责。
            "global_news_enabled": False,
            # 回测里同样关闭 live-only 数据源（内幕交易、预测市场）：它们
            # 只能取"现在"，无法按历史日期裁剪，等于前视。关闭后路由层返回
            # 诚实的"已禁用"提示，分析师降级到带日期裁剪的源。
            "live_only_sources_enabled": False,
            # 瘦身：四份分析师报告会被下游 9 个节点原样内嵌，不设上限时
            # prompt 滚雪球（600396 首跑在研究员节点 600s 读超时的主因之一）。
            # 每份报告截到 2000 字，截断有显式标记；新闻条数 20→10。
            "analyst_report_max_chars": 2000,
            "news_article_limit": 10,
            # Deep-thinking analysts legitimately take minutes per call; a 600s
            # ceiling (raised from 300 after GLM Bull-Researcher calls with
            # very large debate prompts timed out on 300) keeps a hung call
            # from stalling a session indefinitely while leaving headroom for
            # provider congestion on large inputs.
            "llm_timeout": 600.0,
        })
        return cfg

    def _ensure_graph(self):
        if self._graph is not None:
            return self._graph
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        graph = TradingAgentsGraph(
            selected_analysts=("market", "social", "news", "fundamentals"),
            debug=False,
            config=self._build_config(),
        )
        # Backtests don't need the self-reflection loop: it would burn one extra
        # LLM call per day resolving the previous day's outcome.
        graph._resolve_pending_entries = lambda *_a, **_k: None
        graph.memory_log.store_decision = lambda **_k: None
        self._graph = graph
        return graph

    # ---- decision ----

    def _propagate_once(self, graph, ctx: DailyContext, skills_text: str = "",
                        account_state: str = ""):
        """One pipeline run. Debug mode streams so a failure can be traced."""
        if not self.settings.graph_debug:
            return graph.propagate(
                ctx.symbol, ctx.sim_date,
                extra_context=skills_text, account_state=account_state,
            )
        return self._stream_debug(graph, ctx, skills_text, account_state)

    def _stream_debug(self, graph, ctx: DailyContext, skills_text: str = "",
                      account_state: str = ""):
        """Run via streaming so the message trace survives a mid-run failure.

        `propagate()` swallows nothing but exposes no state when a node raises;
        streaming lets us keep every intermediate state and, on failure, show
        exactly which tool_call_id never received a ToolMessage.
        """
        init_state = graph.propagator.create_initial_state(
            ctx.symbol, ctx.sim_date, asset_type="stock", past_context=skills_text,
            instrument_context=graph.resolve_instrument_context(ctx.symbol, "stock"),
            account_state=account_state,
        )
        args = graph.propagator.get_graph_args()
        trace: list[dict] = []
        try:
            for state in graph.graph.stream(init_state, stream_mode="values", **args):
                trace.append(state)
        except Exception:
            self._dump_message_trace(trace, ctx)
            raise
        final = trace[-1] if trace else {}
        return final, graph.process_signal(str(final.get("final_trade_decision") or ""))

    @staticmethod
    def _dump_message_trace(trace: list[dict], ctx: DailyContext) -> None:
        """Log the message sequence, flagging tool_calls with no ToolMessage."""
        logger.error(
            "graph debug: %s @ %s failed after %d completed steps",
            ctx.symbol, ctx.sim_date, len(trace),
        )
        if not trace:
            return
        pending: dict[str, str] = {}
        for i, msg in enumerate(trace[-1].get("messages") or []):
            role = type(msg).__name__
            if role == "AIMessage":
                calls = list(getattr(msg, "tool_calls", None) or [])
                if calls:
                    names = [c.get("name", "?") for c in calls]
                    ids = [c.get("id", "?") for c in calls]
                    logger.error("  [%d] AIMessage tool_calls=%s ids=%s", i, names, ids)
                    pending.update(dict.fromkeys(ids, "orphaned"))
                else:
                    logger.error("  [%d] AIMessage (no tool calls)", i)
            elif role == "ToolMessage":
                cid = getattr(msg, "tool_call_id", None)
                logger.error("  [%d] ToolMessage  tool_call_id=%s", i, cid)
                pending.pop(cid, None)
            else:
                logger.error("  [%d] %s", i, role)
        if pending:
            logger.error(
                "graph debug: ORPHANED tool_calls (no ToolMessage answered them): %s "
                "— this is the parallel-mode shared-messages bug; set "
                "WEBAPP_GRAPH_PIPELINE_MODE=sequential",
                sorted(pending),
            )

    def _propagate_with_retry(self, graph, ctx: DailyContext, skills_text: str = "",
                              account_state: str = ""):
        """Run the pipeline, retrying only errors that can plausibly succeed.

        Structural request errors (malformed messages, unsupported params, auth)
        are re-raised immediately — a retry sends the identical bad request.
        Timeouts / rate limits / 5xx get `max_retries` attempts with linear
        backoff. Content-filter rejections (GLM 1301, usually on the model's
        own generated text) get their own small budget: each attempt samples
        differently, so a retry can pass where the first attempt tripped.
        Everything else re-raises so the session stops loudly instead of
        inventing a decision.
        """
        from webapp.engine.decision_agent import _is_content_filter

        last_exc: Exception | None = None
        transient_used = 0
        filter_used = 0
        while True:
            try:
                return self._propagate_once(graph, ctx, skills_text, account_state)
            except Exception as exc:
                last_exc = exc
                if _is_content_filter(exc):
                    if filter_used < self.content_filter_retries:
                        filter_used += 1
                        logger.warning(
                            "content filter (1301) on %s @ %s, retry %d/%d "
                            "(re-sampling; trigger is often the model's own text)",
                            ctx.symbol, ctx.sim_date,
                            filter_used, self.content_filter_retries,
                        )
                        continue
                    self._log_failure_context(graph, ctx, exc, retryable=True)
                    raise
                if not _is_transient(exc):
                    self._log_failure_context(graph, ctx, exc, retryable=False)
                    raise
                if transient_used >= self.max_retries:
                    break
                transient_used += 1
                delay = self.retry_backoff * transient_used
                logger.warning(
                    "full graph transient failure for %s on %s (attempt %d/%d), "
                    "retrying in %.0fs: %s",
                    ctx.symbol, ctx.sim_date, transient_used, self.max_retries,
                    delay, exc,
                )
                time.sleep(delay)
        self._log_failure_context(graph, ctx, last_exc, retryable=True)
        raise last_exc  # type: ignore[misc]

    @staticmethod
    def _log_failure_context(graph, ctx: DailyContext, exc: Exception | None,
                             retryable: bool) -> None:
        """Log everything needed to reproduce a pipeline failure.

        Deliberately verbose: a graph failure aborts the session, so the log is
        the only post-mortem we get.
        """
        cfg = getattr(graph, "config", {}) or {}
        logger.error(
            "full graph failure | symbol=%s date=%s | %s | retryable=%s\n"
            "  pipeline_mode=%s model=%s/%s debate_rounds=%s risk_rounds=%s "
            "recur_limit=%s timeout=%s\n"
            "  error=%s: %s",
            ctx.symbol, ctx.sim_date,
            "retries exhausted" if retryable else "rejected, not retryable",
            retryable,
            cfg.get("pipeline_mode"), cfg.get("deep_think_llm"),
            cfg.get("quick_think_llm"), cfg.get("max_debate_rounds"),
            cfg.get("max_risk_discuss_rounds"), cfg.get("max_recur_limit"),
            cfg.get("llm_timeout"),
            type(exc).__name__ if exc else "?", exc,
        )
        if cfg.get("pipeline_mode") == "parallel" and "tool_call" in str(exc).lower():
            logger.error(
                "  hint: parallel mode is the likely cause — the four analysts "
                "share one messages list and LangGraph's ToolNode executes the "
                "tool_calls of whichever AIMessage is last, orphaning the "
                "others. Set WEBAPP_GRAPH_PIPELINE_MODE=sequential."
            )

    def decide(self, ctx: DailyContext) -> tuple[Decision, str, str, list[str]]:
        """Returns (decision, audit_text, final_decision_text, flags).

        Raises on failure. A broken pipeline must NOT be disguised as a valid
        "hold" — a backtest that silently records holds for days where no model
        ever ran is worse than one that stops: the results look plausible but
        are fiction. Transient errors (timeouts, rate limits) are retried;
        structural ones (400 invalid_request) fail immediately since retrying
        cannot help.
        """
        flags: list[str] = []
        graph = self._ensure_graph()
        self._spend()
        final_state, signal = self._propagate_with_retry(
            graph, ctx, _graph_context(ctx), _account_state(ctx)
        )

        final_text = str(final_state.get("final_trade_decision") or "")
        trader_text = str(final_state.get("trader_investment_plan") or "")

        rating = (_first(_RE_RATING, final_text) or signal or "Hold").capitalize()
        if rating not in _RATING_ACTION:
            flags.append(f"graph: unparsed rating '{rating}' -> hold")
            rating = "Hold"
        action = _RATING_ACTION[rating]

        entry = _to_float(_first(_RE_ENTRY, trader_text))
        # The PM's own stop/take-profit win; the trader's stop is the fallback.
        stop = _to_float(_first(_RE_STOP, final_text)) or _to_float(_first(_RE_STOP, trader_text))
        target = _to_float(_first(_RE_TARGET, final_text))
        take_profit = _to_float(_first(_RE_TAKE_PROFIT, final_text))

        # Target position: the PM's explicit target_position_pct wins; the
        # trader's free-text sizing is a fallback when the PM omits it.
        target_pos = _parse_target_position(_first(_RE_TARGET_POS, final_text))
        sizing_raw = _first(_RE_SIZING, trader_text)
        if target_pos is None and sizing_raw:
            m = _RE_PCT.search(sizing_raw)
            if m:
                target_pos = max(0.05, min(1.0, float(m.group(1)) / 100.0))
        # If the PM omitted the target weight entirely, fall back to a
        # rating-aligned default rather than _size_position's flat 50%-off /
        # 20%-in — a Sell with no target means "exit", not "sell half of
        # whatever tiny position is left".
        if target_pos is None:
            target_pos = _default_target_position(rating)

        close = _last_close(ctx)
        view = _position_view(ctx)
        current_weight = view["weight"]

        # --- rating vs target consistency (account = ground truth) ---
        # The PM's rating is the deliberate directional call; its
        # target_position_pct is frequently wrong (observed: Overweight with
        # target 6% while actually holding 25% — the model wrote a diversified
        # "portfolio slice" instead of the final single-stock exposure). When
        # the target contradicts the rating's direction, trust the RATING and
        # substitute a rating-aligned default target, flagging it loudly —
        # never silently degrade a decisive "Overweight" into a no-op hold.
        _eps = 0.005
        if current_weight > _eps:
            if action == "buy" and target_pos is not None and target_pos <= current_weight + _eps:
                flags.append(
                    f"graph: {rating} 评级但目标仓位 {target_pos:.0%} 不高于当前仓位 "
                    f"{current_weight:.0%}，方向矛盾，改用评级默认目标仓位"
                )
                target_pos = _default_target_position(rating)
            elif action == "sell" and target_pos is not None and target_pos >= current_weight - _eps:
                flags.append(
                    f"graph: {rating} 评级但目标仓位 {target_pos:.0%} 不低于当前仓位 "
                    f"{current_weight:.0%}，方向矛盾，改用评级默认目标仓位"
                )
                target_pos = _default_target_position(rating)

        # The coast band honours the model's own protective/target levels.
        band_anchor = target or take_profit
        lower, upper = _band(close, entry, stop, band_anchor, self.settings.recheck_band_pct)

        # Re-entry / add trigger: for a bullish stance the trader's entry price
        # is the level the model wants to buy at ("站稳X就买 / 回踩X加仓"). Anchor
        # the band edge to it so the engine re-decides when price reaches the
        # buy level instead of coasting straight past it. The protective stop
        # still fires every day via the hard risk-control layer, so it does not
        # need to double as the band floor here.
        if action == "buy" and entry and entry > 0 and close:
            if entry < close:
                lower = entry
            else:
                upper = entry
            if not (lower and upper and upper > lower > 0):
                lower = close * (1 - self.settings.recheck_band_pct)
                upper = close * (1 + self.settings.recheck_band_pct)
        elif action == "hold" and entry and entry > 0 and close and view["weight"] <= 0.005:
            # Flat + hold (watching for re-entry): the trader's entry is the
            # re-entry level. Anchor the band's lower edge to it so a pullback
            # down to it forces a re-decision — otherwise the engine coasts
            # through intermediate prices, the PM keeps saying "not there yet,
            # still watching", and the flat-book duty never fires.
            if entry < close:
                lower = entry
                if not (lower and upper and upper > lower > 0):
                    upper = close * (1 + self.settings.recheck_band_pct)

        # Position magnitude is derived from target position + risk factors,
        # not a hardcoded rating map.
        pct = _size_position(action=action, target_pos=target_pos, close=close,
                             portfolio=ctx.portfolio)

        # --- engine-side position reconciliation (account = ground truth) ---
        # The PM can hallucinate a holding it no longer has (observed on
        # 600396: after a full exit the pipeline kept emitting "trim the
        # existing position" for 10 straight days, with cash idle). Prompts
        # alone did not stop it — so the engine reconciles every decision
        # against the real account and says so in the record.
        degrade_note = ""
        if action == "sell" and view["weight"] <= 0.005:
            flags.append(
                f"graph: 当前空仓，{rating} 评级的减仓/止盈论述无持仓可执行，维持空仓"
            )
            action = "hold"
            degrade_note = (
                "【仓位核对】当前空仓（无持仓），模型关于减仓/止盈的论述无持仓可执行，"
                "实际执行=维持空仓；模型论述若提及“现有持仓”，与实际账户不符。\n"
            )
        elif action == "buy" and view["cash"] <= 0:
            flags.append(f"graph: {rating} 评级但可用现金为 0，买入无法执行，降级为 hold")
            action = "hold"
            degrade_note = "【仓位核对】当前无可用现金，买入无法执行，实际执行=维持仓位。\n"
        elif action in ("buy", "sell") and pct <= 0.0:
            # A buy/sell whose target is unreachable from the current position
            # (target already at/above the current weight for a sell, at/below
            # for a buy, or no position to act on) sizes to zero. Surfacing
            # that as "sell 0% rejected" wastes the day and hides the real
            # problem — degrade to a hold instead.
            flags.append(
                f"graph: {rating} 评级但目标仓位与当前仓位冲突（无可执行动作），降级为 hold"
            )
            action = "hold"
            degrade_note = "（目标仓位与当前仓位冲突，本日无实际动作，降级为 hold）\n"

        # 分批兑现上限：非清仓意图的单日卖出不超过当前持仓的一定比例——上涨
        # 趋势里"一下砍掉 50~70%"太激进（用户实测反馈）。目标仓位保持不变，
        # 剩余调仓留待后续再决策日继续。Sell 评级（明确离场）与硬风控层
        # （止损/止盈/回撤熔断，绕过本函数）不受此限。
        cap = self.settings.max_daily_sell_frac
        if action == "sell" and rating != "Sell" and 0 < cap < 1.0 and pct > cap:
            flags.append(
                f"graph: 分批兑现——{rating} 评级单日卖出上限 {cap:.0%}，"
                f"目标仓位不变，剩余调仓待后续再决策日继续"
            )
            pct = cap

        # Stance-aware level labels: the framework's Price Target / Stop Loss
        # carry long-entry semantics that read inverted on a bearish rating.
        level_parts, level_flags = _levels_view(rating, close, entry, stop, target, take_profit)
        flags.extend(level_flags)

        horizon = (_first(_RE_HORIZON, final_text) or "").lower()
        recheck_days = _horizon_to_days(horizon, action)

        thesis = (_first(_RE_THESIS, final_text) or "").strip()
        summary = (_first(_RE_SUMMARY, final_text) or "").strip()
        reasoning = _reasoning(rating, summary, thesis, level_parts, lower, upper, sizing_raw)
        # Stamp every decision with the actual position it was made from — a
        # "hold" must say *which* position is being held (用户核心诉求：决策
        # 必须基于真实仓位，而不是想象中的满仓）。
        reasoning = degrade_note + _position_basis(view, target_pos, action, pct) + "\n" + reasoning

        decision = Decision(
            action=action,
            position_pct=round(pct, 4),
            confidence=_CONFIDENCE[rating] if action != "hold" else _CONFIDENCE["Hold"],
            reasoning=reasoning,
            key_signals=_key_signals(final_state, rating, level_parts, lower, upper),
            used_skills=[],
            recheck_days=recheck_days,
            recheck_upper=upper,
            recheck_lower=lower,
            stop_loss=stop,
            take_profit=take_profit,
            target_position_pct=target_pos,
        )

        audit = _audit_text(ctx, final_state, signal, rating, close)
        return decision, audit, final_text, flags


# ---- helpers ----


def _skills_context(skills: list[dict]) -> str:
    """Render cross-session lessons for injection into the pipeline's PM prompt.

    The full graph ignores the webapp's skill library by default; this turns
    ``DailyContext.skills`` into the ``past_context`` block the Portfolio
    Manager already renders under "Lessons from prior decisions and outcomes".
    """
    if not skills:
        return ""
    return "\n".join(
        f"- [{s.get('category', '?')}] {s.get('statement', '')}" for s in skills
    )


def _graph_context(ctx: DailyContext) -> str:
    """One extra-context block shared by the full graph's downstream agents."""
    from webapp.engine.market_profile import market_guidance

    parts = [
        market_guidance(ctx.symbol, ctx.market, ctx.fundamentals),
        _skills_context(ctx.skills),
    ]
    return "\n\n".join(part for part in parts if part)


def _position_view(ctx: DailyContext) -> dict:
    """The account at decision time — engine-side ground truth.

    Independent of whatever the model *thinks* it holds. Used both for the
    PM's prompt and for reconciling the parsed decision against reality.
    """
    p = ctx.portfolio or {}
    cash = float(p.get("cash") or 0.0)
    shares = float(p.get("shares") or 0.0)
    close = _last_close(ctx)
    if close:
        pos_value = shares * close
        equity = cash + pos_value
    else:
        equity = float(p.get("equity") or 0.0)
        pos_value = max(0.0, equity - cash)
    weight = pos_value / equity if equity > 0 else 0.0
    return {"cash": cash, "shares": shares, "pos_value": pos_value,
            "equity": equity, "weight": weight}


def _position_basis(view: dict, target_pos: float | None, action: str, pct: float) -> str:
    """One auditable line: the real position this decision was made from."""
    target_txt = f"{target_pos:.0%}" if target_pos is not None else "维持现仓"
    exec_txt = {
        "buy": f"买入（动用现金的 {pct:.0%}）",
        "sell": f"卖出（当前持仓的 {pct:.0%}）",
        "hold": "不动",
    }[action]
    return (
        f"【仓位基准】决策前实际仓位 {view['weight']:.0%}"
        f"（市值 ¥{view['pos_value']:,.0f}），现金 ¥{view['cash']:,.0f}"
        f"（占 {view['cash'] / view['equity']:.0%}）"
        f"；目标仓位 {target_txt} → 执行：{exec_txt}"
        if view["equity"] > 0
        else f"【仓位基准】账户数据缺失；目标仓位 {target_txt} → 执行：{exec_txt}"
    )


def _account_state(ctx: DailyContext) -> str:
    """Render the caller's live position so the PM sizes against it.

    Without this the Portfolio Manager never sees the webapp account: it emits
    a *directional* target position (e.g. 0% on a Sell) with no anchor to the
    real holding, and the sizing layer can then produce a nonsensical "sell 0%"
    or "sell half of a 10% position" because the target never met the current
    weight. Feeding the actual position lets the PM move *from* the current
    exposure *to* a target, which is the whole point of position management.
    """
    p = ctx.portfolio or {}
    cash = float(p.get("cash") or 0.0)
    shares = float(p.get("shares") or 0.0)
    avg_cost = float(p.get("avg_cost") or 0.0)
    open_pnl = float(p.get("open_pnl") or 0.0)

    close = _last_close(ctx)
    if close:
        pos_value = shares * close
        equity = cash + pos_value
    else:
        equity = float(p.get("equity") or 0.0)
        pos_value = max(0.0, equity - cash)

    if equity <= 0:
        return ""
    weight = pos_value / equity

    lines = [
        f"- 当前持仓 {shares:g} 股，市值约 {pos_value:,.2f}，占总资产 {weight:.1%}",
        f"- 现金 {cash:,.2f}（占总资产 {cash / equity:.1%}），总资产 {equity:,.2f}",
    ]
    if shares > 0:
        lines.append(f"- 持仓成本 {avg_cost:.4f}，浮动盈亏 {open_pnl:+,.2f}")
        lines.append(
            "- 决策约束：target_position_pct 必须从上述实际仓位出发；卖出量以实际持仓为限，"
            "非清仓意图的减仓请分批（引擎对单日卖出有上限）。"
        )
    else:
        lines.append("- 当前空仓（持仓 0，现金即全部资产）")
        lines.append(
            "- 决策约束：空仓状态下你的可选动作只有两种——维持空仓（Hold），或回补建仓"
            "（Buy/Overweight + target_position_pct > 0）。禁止输出任何减仓、止盈、"
            "“对现有持仓……”类表述：你没有持仓可减，此类幻觉会被引擎直接判废。"
        )
    dd = p.get("drawdown")
    if dd is not None:
        lines.append(f"- 权益自峰值回撤 {float(dd):.1%}")

    # Standing plan continuity: hand the PM its own prior triggers so it can
    # follow through instead of re-deriving a fresh (usually "wait") opinion.
    prev = ctx.prev_decision or {}
    plan_bits = []
    if prev.get("stop_loss"):
        plan_bits.append(f"止损 {float(prev['stop_loss']):g}")
    if prev.get("take_profit"):
        plan_bits.append(f"止盈 {float(prev['take_profit']):g}")
    if prev.get("target_position_pct") is not None:
        plan_bits.append(f"目标仓位 {float(prev['target_position_pct']):.0%}")
    if plan_bits:
        origin = (ctx.coast or {}).get("origin_date", "前次决策")
        lines.append(
            f"- 在持计划（{origin} 制定）：{'，'.join(plan_bits)}。"
            "先检查今日数据是否已触发其中任一条件——触发则必须执行，"
            "不得用新的“再观望”替代自己定下的计划（同一买点“观望”最多用一次）。"
        )

    # Swing-position anchor: let the PM time against the recent trend (high-sell
    # / low-add) instead of reacting to today's move alone.
    try:
        regime = band_regime(closes_from_csv(ctx.ohlcv_tail_csv))
    except Exception:
        regime = ""
    if regime:
        lines.append("")
        lines.append("波段位置（结合近20日趋势择时，勿对单日涨跌做条件反射）：")
        lines.append(regime)

    # Raw market snapshot: the PM's only other market input is the analysts'
    # *paraphrases* of the tape, which drop the exact numbers (close, % move,
    # volume ratio, moving-average crossings) a buy/sell call actually turns
    # on. Inject the raw tape + indicators so it reads the same hard numbers a
    # human chart does — "07-20 +10% on 2x volume" — and can spot a
    # stabilization / reversal instead of re-deriving "wait for confirmation".
    try:
        snapshot = _market_snapshot(ctx)
    except Exception:
        snapshot = ""
    if snapshot:
        lines.append("")
        lines.append("市场快照（原始行情，非分析师转述——据此直接判断量价与均线位置）：")
        lines.append(snapshot)

    return "\n".join(lines)


def _market_snapshot(ctx: DailyContext) -> str:
    """Raw tape + indicators rendered as hard data for the Portfolio Manager.

    The PM otherwise reads the tape only through the analysts' summaries, which
    paraphrase away the exact numbers — the single-day % move, the volume ratio
    (放量/缩量), and the moving-average crossings — that a re-entry call turns
    on. This is the same information a human sees reading the chart, delivered
    as queried ground truth (like the account snapshot), so the model can spot
    a stabilization signal ("+10% 放量站上20日线") the analysts may have softened
    into "technically improving, but watch for overhead supply".
    """
    rows = [ln for ln in (ctx.ohlcv_tail_csv or "").strip().splitlines() if ln.strip()]
    parsed: list[tuple[str, float, float]] = []  # (date, close, volume)
    for line in rows[1:]:  # skip the CSV header (Date,Open,High,Low,Close,Volume)
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 6:
            try:
                parsed.append((parts[0], float(parts[4]), float(parts[5])))
            except ValueError:
                continue
    if not parsed:
        return ""

    last_date, last_close, last_vol = parsed[-1]
    out = [f"- 收盘 {last_close:.2f}（{last_date}）"]

    if len(parsed) >= 2:
        prev_close = parsed[-2][1]
        out.append(f"- 当日涨跌 {(last_close / prev_close - 1) * 100:+.2f}%")

    vols = [v for _, _, v in parsed]
    if len(vols) >= 6 and last_vol > 0:
        avg5 = sum(vols[-6:-1]) / 5.0
        if avg5 > 0:
            ratio = last_vol / avg5
            tag = "放量" if ratio >= 1.5 else ("缩量" if ratio <= 0.7 else "平量")
            out.append(f"- 成交量 {last_vol:,.0f}，量比 {ratio:.2f}（较近5日均量，{tag}）")

    ind = ctx.indicators or {}
    pos_bits = []
    for label, key in (("20日线", "close_20_sma"), ("50日线", "close_50_sma"),
                       ("10日EMA", "close_10_ema")):
        val = ind.get(key)
        if val:
            rel = "站上" if last_close >= float(val) else "跌破"
            pos_bits.append(f"{label} {float(val):.2f}（现价{rel}）")
    if pos_bits:
        out.append("- " + "，".join(pos_bits))

    misc = []
    rsi = ind.get("rsi")
    if rsi is not None:
        zone = "超买" if rsi >= 70 else ("超卖" if rsi <= 30 else "中性")
        misc.append(f"RSI {rsi:.0f}（{zone}）")
    boll_ub, boll_lb = ind.get("boll_ub"), ind.get("boll_lb")
    if boll_ub and boll_lb:
        misc.append(f"布林 [{float(boll_lb):.2f}, {float(boll_ub):.2f}]")
    if misc:
        out.append("- " + "，".join(misc))

    # Compact recent tape so the PM can SEE the reversal, not just be told it.
    recent = parsed[-8:]
    lines = [f"- 近{len(recent)}日量价（日期 收盘 涨跌幅 成交量）："]
    base = len(parsed) - len(recent)
    for i, (d, c, v) in enumerate(recent):
        idx = base + i
        prev_c = parsed[idx - 1][1] if idx >= 1 else c
        pct = (c / prev_c - 1) * 100 if idx >= 1 else 0.0
        lines.append(f"    {d}  {c:.2f}  {pct:+.1f}%  {_fmt_volume(v)}")
    out.append("\n".join(lines))
    return "\n".join(out)


def _fmt_volume(v: float) -> str:
    """Human volume label — 亿 for A-share scale, 万 otherwise, raw if tiny."""
    if v >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if v >= 1e4:
        return f"{v / 1e4:.0f}万"
    return f"{v:.0f}"


def _last_close(ctx: DailyContext) -> float | None:
    """Last close from the OHLCV tail CSV (Date,Open,High,Low,Close,Volume)."""
    lines = [ln for ln in (ctx.ohlcv_tail_csv or "").strip().splitlines() if ln.strip()]
    for line in reversed(lines):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 5:
            val = _to_float(parts[4])
            if val:
                return val
    return None


def _band(close, entry, stop, target, band_pct):
    """Coast band bracketing the current close.

    The trader's stop loss anchors the downside and the PM's target (falling
    back to the entry price) the upside; missing levels fall back to a
    symmetric band around the close. A band that does not bracket the close
    would trigger an immediate re-decision, which defeats the point.
    """
    if not close:
        return None, None
    pad = max(band_pct, 0.02)
    upper_anchor = target or entry

    # Honour the model's own levels only when they actually bracket the price.
    if stop and upper_anchor and stop < close < upper_anchor:
        lower, upper = stop, upper_anchor
    else:
        lower = stop if (stop and 0 < stop < close) else close * (1 - pad)
        upper = upper_anchor if (upper_anchor and upper_anchor > close) else close * (1 + pad)

    if not (upper > lower > 0):
        lower, upper = close * (1 - pad), close * (1 + pad)
    return round(lower, 4), round(upper, 4)


def _parse_target_position(raw: str | None) -> float | None:
    """Parse '80%' or '0.8' into a 0~1 target position fraction."""
    if not raw:
        return None
    m = re.match(r"(\d+(?:\.\d+)?)\s*%?", raw.strip())
    if not m:
        return None
    val = float(m.group(1))
    if val > 1.0:  # percent form
        val /= 100.0
    return max(0.0, min(1.0, val))


_DEFAULT_TARGET_POSITION = {
    "Buy": 1.0,
    "Overweight": 0.70,
    "Underweight": 0.30,
    "Sell": 0.0,
}


def _default_target_position(rating: str) -> float | None:
    """Rating-aligned target weight when the PM omits target_position_pct.

    A Sell means "exit", not "sell half of whatever tiny position is left";
    an Underweight means "trim toward a low exposure". Returns None for Hold
    (no movement — _size_position short-circuits on hold anyway).
    """
    return _DEFAULT_TARGET_POSITION.get(rating)


def _size_position(action: str, target_pos: float | None, close: float | None,
                   portfolio: dict | None = None) -> float:
    """Resolve the position magnitude from the target weight.

    Returns ``position_pct`` in the webapp's convention: for ``buy`` the
    fraction of available cash to spend, for ``sell`` the fraction of held
    shares to sell.

    Both sides honour the PM's target weight directly. The only temperer left
    is a drawdown guard on buys (deeper drawdown -> smaller adds, anti-chasing).
    The distance-to-stop risk budget, Bollinger volatility and conviction
    scaling were removed: they discounted the PM's own target — which already
    accounts for the stop distance and volatility — and are what turned a
    decisive "add to 80%" into a ~19% dribble, and a "clear out" into "sell
    75%". Actual risk control lives in the hard stop-loss / take-profit /
    drawdown circuit-breaker that runs every day, not in sizing discounts.
    """
    if action == "hold":
        return 0.0

    cash = float((portfolio or {}).get("cash") or 0.0)
    shares = float((portfolio or {}).get("shares") or 0.0)
    equity = float((portfolio or {}).get("equity") or 0.0)
    drawdown = float((portfolio or {}).get("drawdown") or 0.0)  # <= 0
    if not close or equity <= 0:
        return 0.0

    current_pos = shares * close / equity

    # The target is the exposure to *move to*, so the direction it implies must
    # agree with the action: a buy only makes sense when the target is above
    # the current weight, a sell only when it is below. When the target
    # contradicts the action (or there is nothing to act on) we size to 0 and
    # the caller degrades the decision to a hold — never a 0% buy/sell that the
    # execution layer then rejects.
    if action == "buy":
        target_pos = max(0.0, min(1.0, target_pos if target_pos is not None else 0.20))
        if target_pos <= current_pos:
            return 0.0
        need = (target_pos - current_pos) * equity
        pct = max(0.0, min(1.0, need / cash)) if cash > 0 else 0.0
        if drawdown < 0:
            dd_factor = min(1.0, max(0.3, 1.0 + drawdown * 2.0))
            pct = min(pct, dd_factor)
    else:  # sell
        target_pos = max(0.0, min(1.0, target_pos if target_pos is not None else 0.50))
        if current_pos <= 0 or target_pos >= current_pos:
            return 0.0
        pct = max(0.0, min(1.0, 1.0 - target_pos / current_pos))

    return round(max(0.0, min(1.0, pct)), 4)



def _horizon_to_days(horizon: str, action: str) -> int:
    """Map a free-text horizon onto the 1-10 recheck window."""
    if "day" in horizon:
        m = re.search(r"(\d+)", horizon)
        if m:
            return max(1, min(10, int(m.group(1))))
    if "week" in horizon:
        return 3
    if "month" in horizon:
        return 5
    # Buy/Sell decisions expire sooner than a standing Hold.
    return 2 if action in ("buy", "sell") else 4


def _reasoning(rating, summary, thesis, level_parts, lower, upper, sizing) -> str:
    parts = [f"【评级】{rating}"]
    if summary:
        parts.append(f"【执行摘要】{summary}")
    levels = list(level_parts)
    if sizing:
        levels.append(f"仓位 {sizing}")
    if lower and upper:
        levels.append(f"决策区间 [{lower:g}, {upper:g}]（破位即重新决策）")
    if levels:
        parts.append("【价位】" + "，".join(levels))
    if thesis:
        parts.append(f"【投资逻辑】{thesis}")
    text = "\n".join(parts)
    return text[:1200]


def _levels_view(rating: str, close: float | None, entry, stop, target, take_profit=None) -> tuple[list[str], list[str]]:
    """Stance-aware, close-relative labels for the model's price levels.

    The framework writes Price Target / Stop Loss in long-entry semantics.
    On a bearish rating the PM's target is a DOWNSIDE objective and a trader
    stop below the close is a further-reduce trigger — labeling them with
    bullish field names reads as an inverted pair (observed on 300308 d1:
    "目标价=535，止损价=570" for an Underweight at ~589). Labels are derived
    from where each level sits relative to the close, and levels that
    contradict the rating are dropped with a flag instead of surfaced raw.
    Returns (display_parts, flags).
    """
    flags: list[str] = []
    parts: list[str] = []
    if not close:
        return parts, flags
    bullish = rating in ("Buy", "Overweight")

    if target:
        if bullish and target <= close:
            flags.append(f"graph: PM 目标价 {target:g} 不高于现价，与{rating}评级矛盾，已忽略该价位")
        elif not bullish and target >= close:
            flags.append(f"graph: PM 目标价 {target:g} 不低于现价，与{rating}评级矛盾，已忽略该价位")
        else:
            parts.append(("上望目标" if bullish else "下探目标") + f" {target:g}")

    if take_profit and bullish and take_profit > close:
        parts.append(f"止盈位 {take_profit:g}")

    if stop and stop > 0:
        if stop < close:
            # Below the close: bullish = protect the position, bearish =
            # the trader's own "跌破则进一步减" trigger (300308: 跌破565-575).
            parts.append(("止损位" if bullish else "减仓触发位") + f" {stop:g}")
        elif bullish:
            flags.append(f"graph: 交易员止损 {stop:g} 不在现价下方，与{rating}评级矛盾，已忽略该价位")
        else:
            # Bearish stance with the invalidation level ABOVE the price:
            # the reduce-thesis is wrong once price reclaims this level.
            parts.append(f"观点失效位 {stop:g}")

    if entry and entry > 0:
        if bullish:
            if entry < close * 1.02:
                parts.append(f"入场参考 {entry:g}")
        else:
            parts.append(f"回补参考 {entry:g}")

    return parts, flags


def _key_signals(final_state: dict, rating: str, level_parts: list[str],
                 lower, upper) -> list[str]:
    # Price levels lead: the actionable output is the levels plus the coast
    # band; the list is truncated below.
    signals: list[str] = []
    price_bits = list(level_parts)
    if lower and upper:
        price_bits.append(f"决策区间 [{lower:g}, {upper:g}]")
    if price_bits:
        signals.append("价位：" + "，".join(price_bits))
    labels = [
        ("market_report", "技术面"),
        ("news_report", "消息面"),
        ("fundamentals_report", "基本面"),
        ("sentiment_report", "情绪面"),
    ]
    for key, label in labels:
        text = str(final_state.get(key) or "").strip()
        if not text:
            continue
        first = _first_meaningful_sentence(text)
        if first:
            signals.append(f"{label}：{first[:90]}")
    debate = (final_state.get("investment_debate_state") or {}).get("judge_decision") or ""
    if debate:
        first = _first_meaningful_sentence(str(debate))
        if first:
            signals.append(f"多空裁决：{first.strip()[:90]}")
    return signals[:6]


def _first_meaningful_sentence(text: str, min_len: int = 12) -> str:
    """First sentence worth showing.

    Splits on Chinese sentence enders (and newlines) only — splitting on the
    ASCII period cut tickers apart ("300308.SZ" -> "300308") and surfaced
    fragments like "技术面：300308." as a signal.
    """
    for sentence in re.split(r"[。！？!?\n]+", str(text or "")):
        cleaned = sentence.strip().strip("*-# ").strip()
        if len(cleaned) >= min_len:
            return cleaned
    return ""


def _audit_text(ctx: DailyContext, final_state: dict, signal: str,
                rating: str, close) -> str:
    """Human-readable trace of the whole pipeline, stored as prompt_text."""
    debate = final_state.get("investment_debate_state") or {}
    risk = final_state.get("risk_debate_state") or {}
    sections = [
        f"交易日: {ctx.sim_date}（{ctx.symbol}）　模式: 完整多智能体流水线",
        f"当日收盘（网关口径，用于成交）: {close}",
        f"流水线输出信号: {signal} → 组合经理评级: {rating}",
        # The graph's own data plane uses the original framework's tools, and
        # its social/news sources are live-only — the webapp's no-lookahead
        # gateway does not cover them. Say so where the reports are read.
        "⚠️ 数据口径提示：流水线内部取数沿用原框架（yfinance/StockTwits/Reddit 等）。",
        "其中社交媒体与部分新闻源只能取到运行当下的实盘内容，回测历史日期 T 时可能"
        "混入 T 之后的信息；请对【情绪面】与【消息面】两节相应打折，"
        "价格与基本面以网关裁剪后的数据为准。",
        "",
        "## 1. 市场技术分析师\n" + str(final_state.get("market_report") or "-"),
        "",
        "## 2. 社交媒体/情绪分析师\n" + str(final_state.get("sentiment_report") or "-"),
        "",
        "## 3. 新闻分析师\n" + str(final_state.get("news_report") or "-"),
        "",
        "## 4. 基本面分析师\n" + str(final_state.get("fundamentals_report") or "-"),
        "",
        "## 5. 多空研究员辩论\n"
        f"- 多头: {str(debate.get('bull_history') or '-')[:1200]}\n"
        f"- 空头: {str(debate.get('bear_history') or '-')[:1200]}\n"
        f"- 研究经理裁决: {str(debate.get('judge_decision') or '-')[:1200]}",
        "",
        "## 6. 研究经理投资计划\n" + str(final_state.get("investment_plan") or "-"),
        "",
        "## 7. 交易员方案\n" + str(final_state.get("trader_investment_plan") or "-"),
        "",
        "## 8. 风控辩论\n"
        f"- 激进: {str(risk.get('aggressive_history') or '-')[:800]}\n"
        f"- 保守: {str(risk.get('conservative_history') or '-')[:800]}\n"
        f"- 中性: {str(risk.get('neutral_history') or '-')[:800]}\n"
        f"- 风控经理裁决: {str(risk.get('judge_decision') or '-')[:800]}",
        "",
        "## 9. 组合经理最终决策\n" + str(final_state.get("final_trade_decision") or "-"),
    ]
    return "\n".join(sections)
