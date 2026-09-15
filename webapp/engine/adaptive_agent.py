"""Three-tier decision scheduler for low-latency historical backtests."""
from __future__ import annotations

from webapp.config import WebappSettings
from webapp.core.errors import LLMBudgetExceeded
from webapp.core.models import Decision
from webapp.engine.context_builder import DailyContext
from webapp.engine.graph_agent import _is_transient, _last_close
from webapp.engine.hybrid_agent import (
    _technical_breakdown,
    _technical_improvement,
    coast_origin,
)
from webapp.engine.news_gate import filter_material, rule_hits


class AdaptiveDecisionAgent:
    """Use no model, one model, or a two-call committee as conditions require."""

    def __init__(self, settings: WebappSettings, fast_agent=None, committee=None):
        self.settings = settings
        self.fast_agent = fast_agent or self._make_fast_agent()
        self.committee = committee or self._make_committee()
        self._call_count = 0
        self._fast_streak = 0
        self._standpat_streak = 0

    def _make_fast_agent(self):
        from webapp.engine.decision_agent import DecisionAgent
        from webapp.llm import get_llm

        llm = get_llm(self.settings, timeout=180, thinking_level="low")
        return DecisionAgent(llm, self.settings)

    def _make_committee(self):
        from webapp.engine.committee_agent import CommitteeDecisionAgent

        return CommitteeDecisionAgent(self.settings)

    def reset_day(self) -> None:
        self._call_count = 0
        for delegate in (self.fast_agent, self.committee):
            reset = getattr(delegate, "reset_day", None)
            if reset:
                reset()

    def _classify(self, ctx: DailyContext) -> tuple[str, str]:
        if ctx.cooldown_days > 0:
            return "zero_call", f"风险清仓后的冷静期，剩余 {ctx.cooldown_days} 个交易日，禁止重新入场"
        if not ctx.prev_decision or not ctx.coast:
            return "committee", "会话首个决策日，需要建立基准观点"

        hits = rule_hits(list(ctx.news_delta or []))
        if hits:
            labels = "/".join(sorted({label for label, _ in hits}))
            return "committee", f"命中重大消息规则：{labels}"

        breakdown = _technical_breakdown(ctx)
        if breakdown:
            return "committee", f"技术面恶化：{breakdown}"
        improvement = _technical_improvement(ctx)
        if improvement:
            return "committee", f"技术面企稳/反转：{improvement}"

        close = _last_close(ctx)
        lower = (ctx.coast or {}).get("lower")
        upper = (ctx.coast or {}).get("upper")
        if lower and upper and close is not None and not (lower <= close <= upper):
            return "committee", f"价格 {close:g} 离开决策区间 [{lower:g}, {upper:g}]"

        if self._fast_streak >= self.settings.adaptive_fast_streak_limit:
            return "committee", "连续快速决策达到上限，进行全面复核"
        if self._standpat_streak + 1 >= self.settings.max_standpat_streak:
            return "committee", "连续维持原判断达到上限，进行全面复核"

        if filter_material(list(ctx.news_delta or [])):
            return "fast", "出现一般新增信息，进行一次快速复核"
        return "zero_call", "无重大变化且价格仍在原决策区间"

    def decide(self, ctx: DailyContext):
        path, reason = self._classify(ctx)
        if path == "zero_call":
            self._standpat_streak += 1
            decision = self._stand_pat(ctx, reason)
            audit = self._audit(ctx, path, reason)
            return decision, audit, decision.reasoning, [
                "adaptive:path=zero_call",
                f"adaptive:{reason}",
            ]

        delegate = self.fast_agent if path == "fast" else self.committee
        try:
            decision, audit, response, flags = delegate.decide(ctx)
        except Exception as exc:
            # Degrade to stand-pat only for failures that a re-decide can plausibly
            # recover from: a blown daily budget or a transient provider hiccup
            # (timeout / connection / 5xx). Structural errors (401/403/400, parse
            # failures) are re-raised so the session fails loudly — silently
            # holding every day while the LLM never runs is worse than crashing
            # (HANDOVER pitfall #4). A stand-pat also needs a standing thesis to
            # fall back on; without one there is nothing to maintain.
            if not ctx.prev_decision or not (_is_transient(exc) or isinstance(exc, LLMBudgetExceeded)):
                raise
            decision = self._stand_pat(ctx, f"{path} 路径失败：{type(exc).__name__}")
            audit = self._audit(ctx, "zero_call", decision.reasoning)
            return decision, audit, decision.reasoning, [
                f"adaptive:degraded={path}",
                "adaptive:path=zero_call",
            ]

        self._call_count += int(getattr(delegate, "_call_count", 0))
        self._standpat_streak = 0
        if path == "fast":
            self._fast_streak += 1
        else:
            self._fast_streak = 0
        return decision, audit, response, [f"adaptive:path={path}", f"adaptive:{reason}"] + list(flags)

    @staticmethod
    def _stand_pat(ctx: DailyContext, reason: str) -> Decision:
        prev = ctx.prev_decision or {}
        coast = ctx.coast or {}
        lower, upper = coast.get("lower"), coast.get("upper")
        return Decision(
            action="hold",
            confidence=round(float(prev.get("confidence", 0.4)) * 0.9, 3),
            reasoning=(
                f"【自适应维持原判断】{reason}。前次决策（{coast_origin(ctx)}）"
                f"为 {prev.get('action', 'hold')}，本日不新增交易。"
            ),
            key_signals=[
                "消息面：无需要重建观点的新增信息",
                f"沿用区间：[{lower:g}, {upper:g}]" if lower and upper else "沿用原观点",
            ],
            recheck_days=2,
            recheck_lower=round(lower, 4) if lower else None,
            recheck_upper=round(upper, 4) if upper else None,
            stop_loss=prev.get("stop_loss"),
            take_profit=prev.get("take_profit"),
            target_position_pct=prev.get("target_position_pct"),
        )

    @staticmethod
    def _audit(ctx: DailyContext, path: str, reason: str) -> str:
        return "\n".join(
            [
                f"交易日: {ctx.sim_date}（{ctx.symbol}）",
                "模式: 自适应精简架构",
                f"实际路径: {path}",
                f"判定依据: {reason}",
            ]
        )
