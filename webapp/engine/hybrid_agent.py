"""Hybrid scheduling of the expensive full-pipeline graph.

Most trading days nothing happens. Running the analyst -> debate -> trader ->
risk pipeline on a fixed schedule burns minutes per day re-deriving the same
conclusion, so this agent escalates only when something changed:

  1. new material news hits a deterministic rule (财报/重组/评级/监管…) -> run it
  2. new news, but no rule hit -> one cheap LLM impact score; medium/high -> run it
  3. no news -> run it only if price broke the band by a meaningful margin

Otherwise it stands pat: no trade, keep the standing thesis and its price
band, just refresh the horizon. The band stays the model's own levels until
news actually invalidates them.
"""
from __future__ import annotations

import logging

from webapp.config import WebappSettings
from webapp.core.errors import LLMBudgetExceeded
from webapp.core.models import Decision
from webapp.engine.context_builder import DailyContext
from webapp.engine.graph_agent import GraphDecisionAgent, _last_close
from webapp.engine.news_gate import (
    filter_material,
    llm_impact,
    rule_hits,
)

logger = logging.getLogger(__name__)


class HybridDecisionAgent:
    """Cheap daily news triage; full multi-agent pipeline on escalation only."""

    def __init__(self, settings: WebappSettings, graph: GraphDecisionAgent | None = None):
        self.settings = settings
        self.graph = graph or GraphDecisionAgent(settings)
        self._gate_llm = None
        self._call_count = 0
        self.escalations = 0
        self.stand_pats = 0
        self._standpat_streak = 0  # consecutive stand-pats; reset on any escalation

    def reset_day(self) -> None:
        self._call_count = 0

    def _spend(self) -> None:
        """Charge one cheap triage LLM call against the daily budget.

        Only the news-impact triage (llm_impact) is a direct LLM call here; the
        escalation branch delegates to graph.decide, which enforces its own
        max_graph_runs_per_day budget. Previously _call_count was a bare
        counter with no enforcement.
        """
        self._call_count += 1
        if self._call_count > self.settings.max_llm_calls_per_day:
            raise LLMBudgetExceeded(
                f"exceeded {self.settings.max_llm_calls_per_day} LLM calls "
                f"for this day ({self._call_count})"
            )

    @property
    def gate_llm(self):
        """One small, low-thinking client for the impact triage."""
        if self._gate_llm is None:
            from webapp.llm import get_llm

            # The triage is a classification, not a thesis: force low thinking
            # so it stays cheap even when the global (decision) level is high.
            # get_llm builds the client at the requested depth directly, rather
            # than mutating a high-thinking client after the fact.
            llm = get_llm(self.settings, timeout=60, thinking_level="low")
            self._gate_llm = llm
        return self._gate_llm

    # ---- escalation policy ----

    def _should_escalate(self, ctx: DailyContext, delta: list[dict],
                         close: float | None, band) -> tuple[bool, str]:
        hits = rule_hits(delta)
        if hits:
            labels = sorted({label for label, _ in hits})
            sample = hits[0][1].get("title", "")[:40]
            return True, f"命中重大消息规则 [{'/'.join(labels)}]：{sample}"

        # A technical breakdown (sharp oversold flush / trend break) justifies a
        # fresh analysis on its own — even with no news and the close still
        # inside the band. Checked before the paid news-impact LLM so it costs
        # nothing and outranks a routine headline.
        tech = _technical_breakdown(ctx)
        if tech:
            return True, f"技术面恶化：{tech}"

        # The mirror image: a technical improvement (reversal / stabilization)
        # must also wake the pipeline, or the stand-pat coasts straight past the
        # buy point and the model only re-decides after the move is gone.
        improve = _technical_improvement(ctx)
        if improve:
            return True, f"技术面企稳/反转：{improve}"

        material = filter_material(delta)
        if material:
            level, why = llm_impact(self.gate_llm, ctx.symbol, ctx.sim_date,
                                    material, band)
            self._spend()
            if level in ("high", "medium"):
                return True, f"消息影响评估={level}：{why}"
            return False, f"消息影响评估={level}，维持原判断：{why}"

        # No news at all. The engine already re-decides whenever the close
        # leaves the band, so reaching this branch with the close outside the
        # band means the thesis may be invalidated — escalate regardless of how
        # small the breach is. (A "breach ratio" threshold here used to swallow
        # a slow drift: each day re-centred the band so a 40% drawdown could
        # accumulate without ever re-running the pipeline.) Reaching this branch
        # with the close still inside the band means only the horizon expired
        # with nothing changed — stand pat.
        lower, upper = band if band else (None, None)
        if lower and upper and close is not None and not (lower <= close <= upper):
            return True, f"无新消息，但收盘 {close:g} 已离开决策区间 [{lower:g}, {upper:g}]，重新评估"
        return False, "无新增重大消息且价格仍在区间内，仅有效期届满，维持原判断"

    # ---- decisions ----

    def decide(self, ctx: DailyContext) -> tuple[Decision, str, str, list[str]]:
        delta = list(ctx.news_delta or [])
        coast = ctx.coast or {}
        close = _last_close(ctx)
        band = (coast.get("lower"), coast.get("upper")) if coast else (None, None)

        if not ctx.coast or not ctx.prev_decision:
            # First decision day (or a resume that could not rebuild a standing
            # decision): there is no thesis to "maintain", so a stand-pat here
            # would fabricate a phantom prior judgment and — on a quiet stock —
            # coast the entire session without ever running the pipeline.
            # Always run the full pipeline once to seed a real decision.
            escalate, reason = True, "会话首个决策日（无在持判断），直接运行完整流水线建立基准"
        else:
            escalate, reason = self._should_escalate(ctx, delta, close, band)

        # Stand-pat 自我续期上限：闸门连续 N 天说"没事"之后，强制跑一次完整
        # 流水线做全面复核。否则"维持原判断"可以无限链式续期——实测 NVDA
        # 会话 20+ 天没有一次深度评估，600396 会话 +56% 浮盈没人复核坐电梯。
        if not escalate and self._standpat_streak + 1 >= self.settings.max_standpat_streak:
            escalate, reason = (
                True,
                f"已连续 {self._standpat_streak} 日维持原判断，强制全面复核"
                f"（stand-pat 上限 {self.settings.max_standpat_streak} 日，防止躺平）",
            )

        if escalate:
            # The graph agent enforces its own max_graph_runs_per_day budget in
            # its decide(); hybrid's _call_count tracks only the cheap triage
            # calls above, not the delegated full-pipeline run.
            self.escalations += 1
            self._standpat_streak = 0
            try:
                decision, audit, response, gflags = self.graph.decide(ctx)
            except LLMBudgetExceeded as exc:
                # A budget breach must NOT crash the session — it degrades to a
                # stand-pat for the day and re-evaluates tomorrow. The standing
                # thesis and its band are still valid; only the re-decision is
                # deferred. Without this, a single news-triggered re-decide on
                # a day that already ran the pipeline kills the whole backtest.
                self.stand_pats += 1
                decision = self._stand_pat(ctx, delta, close, band, reason)
                audit = self._audit(ctx, delta, close, reason, escalate=False)
                return decision, audit, decision.reasoning, [
                    f"hybrid: 升级到完整流水线（{reason}），但超出当日 graph 预算"
                    f"（{exc}），降级为维持原判断，明日再评估"
                ]
            flags = [f"hybrid: 升级到完整流水线（{reason}）"] + list(gflags)
            # Fold the graph's own call count back into the hybrid counter so
            # the engine (which reads self.agent._call_count) reports the real
            # number of full-pipeline runs. Without this, an escalated day shows
            # llm_calls=0 in the DB despite minutes of actual LLM work, because
            # the graph agent tracks its _call_count on a separate instance.
            self._call_count += self.graph._call_count
            return decision, audit, response, flags

        self.stand_pats += 1
        self._standpat_streak += 1
        decision = self._stand_pat(ctx, delta, close, band, reason)
        audit = self._audit(ctx, delta, close, reason, escalate=False)
        return decision, audit, decision.reasoning, [f"hybrid: {reason}"]

    def _stand_pat(self, ctx: DailyContext, delta: list[dict], close: float | None,
                   band, reason: str) -> Decision:
        """Keep the standing thesis; no trade; refresh the horizon."""
        prev = ctx.prev_decision or {}
        lower, upper = band
        # The escalation gate above already upgrades whenever the close leaves
        # the band, so a stand-pat here means the close is still inside the band
        # and only the horizon expired. Keep the band unchanged — re-centring it
        # on the current price used to swallow a slow drift and let a deep
        # drawdown accumulate without ever triggering a re-decision.
        note = ""

        signals: list[str] = []
        if delta:
            signals.append(
                "消息面：" + "；".join(
                    f"[{d.get('kind', '?')}]{str(d.get('title', ''))[:36]}" for d in delta[:3]
                )
            )
        else:
            signals.append("消息面：窗口内无新增重大信息")
        if lower and upper:
            signals.append(f"沿用区间：[{lower:g}, {upper:g}]")

        prev_action = prev.get("action", "hold")
        reasoning = (
            f"【维持原判断】{reason}{note}。"
            f"前次决策（{coast_origin(ctx)}）为 {prev_action}"
            f"，仓位 {prev.get('position_pct', 0)}，本日不新增交易。"
        )

        return Decision(
            action="hold",
            position_pct=0.0,
            confidence=round(float(prev.get("confidence", 0.4)) * 0.9, 3),
            reasoning=reasoning,
            key_signals=signals[:4],
            used_skills=[],
            recheck_days=2,
            recheck_upper=round(upper, 4) if upper else None,
            recheck_lower=round(lower, 4) if lower else None,
        )

    @staticmethod
    def _audit(ctx: DailyContext, delta: list[dict], close: float | None,
               reason: str, escalate: bool) -> str:
        lines = [
            f"交易日: {ctx.sim_date}（{ctx.symbol}）　模式: 分层决策（消息闸门）",
            f"当日收盘: {close}",
            f"闸门结论: {'升级 → 完整流水线' if escalate else '维持 → 不调用流水线'}",
            f"判定依据: {reason}",
            "",
            f"新增消息（{len(delta)} 条）:",
        ]
        if delta:
            lines += [
                f"- [{d.get('kind', '?')}][{d.get('date', '')}] {d.get('title', '')}"
                for d in delta[:15]
            ]
        else:
            lines.append("- （无）")
        return "\n".join(lines)


def coast_origin(ctx: DailyContext) -> str:
    return (ctx.coast or {}).get("origin_date", "前次")


def _tail_closes(ctx: DailyContext) -> list[float]:
    """Close prices from the OHLCV tail CSV (Date,Open,High,Low,Close,Volume)."""
    closes: list[float] = []
    for line in (ctx.ohlcv_tail_csv or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 5:
            try:
                closes.append(float(parts[4]))
            except ValueError:
                continue
    return closes


def _technical_breakdown(ctx: DailyContext) -> str | None:
    """Technical-breakdown trigger read from the day's context."""
    from webapp.engine.news_gate import detect_technical_breakdown

    return detect_technical_breakdown(_last_close(ctx), ctx.indicators, _tail_closes(ctx))


def _technical_improvement(ctx: DailyContext) -> str | None:
    """Technical-improvement trigger read from the day's context."""
    from webapp.engine.news_gate import detect_technical_improvement

    return detect_technical_improvement(_last_close(ctx), ctx.indicators, _tail_closes(ctx))
