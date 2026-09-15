"""Offline check of the hybrid escalation gate.

Covers the three escalation paths and the stand-pat fallback, with a fake
gate LLM and a fake full pipeline so nothing costs real tokens:

  1. deterministic rule hit (财报/重组/评级…) -> escalate, no LLM asked
  2. new news, no rule hit -> cheap LLM impact score decides
  3. no news at all -> escalate whenever the close leaves the band
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from webapp.config import WebappSettings  # noqa: E402
from webapp.core.models import Decision  # noqa: E402
from webapp.engine.context_builder import DailyContext  # noqa: E402
from webapp.engine.hybrid_agent import HybridDecisionAgent  # noqa: E402

TAIL = "\n".join(
    f"2026-06-{d:02d},100.0,102.0,99.0,{100 + i},1000" for i, d in enumerate(range(10, 30))
)
COAST = {
    "lower": 96.0, "upper": 130.0, "origin_date": "2026-06-20",
    "origin_index": 3, "until_index": 6,
    "decision_json": json.dumps(
        {"action": "buy", "position_pct": 0.2, "confidence": 0.7,
         "reasoning": "首次建仓", "key_signals": [], "used_skills": [],
         "recheck_days": 3, "recheck_upper": 130.0, "recheck_lower": 96.0}
    ),
}


class FakeGateLLM:
    def __init__(self, impact="low", reason="例行公告"):
        self.impact = impact
        self.reason = reason
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        self.last_prompt = "\n".join(m for _, m in messages)
        payload = {"impact": self.impact, "reason": self.reason}
        return type("R", (), {"content": json.dumps(payload, ensure_ascii=False)})()


class FakeGraph:
    """Stands in for the full multi-agent pipeline."""

    def __init__(self):
        self.calls = 0
        self._call_count = 0  # hybrid folds this into its own counter

    def decide(self, ctx):
        self.calls += 1
        self._call_count += 1
        return (
            Decision(action="buy", position_pct=0.25, confidence=0.8,
                     reasoning="完整流水线重新定价", recheck_days=3,
                     recheck_upper=140.0, recheck_lower=100.0),
            "<full pipeline audit>",
            "**Rating**: Buy",
            [],
        )


def make_agent(impact="low", reason="例行公告"):
    settings = WebappSettings(llm_provider="glm-cn", model="glm-5.3-flash",
                              graph_trigger="on_news", escalate_breach_ratio=0.25)
    agent = HybridDecisionAgent(settings, graph=FakeGraph())
    agent._gate_llm = FakeGateLLM(impact, reason)
    return agent


def ctx_with(news_delta, close_line=119.0):
    tail = "\n".join(
        f"2026-06-{d:02d},100.0,102.0,99.0,{100 + i},1000" for i, d in enumerate(range(10, 29))
    ) + f"\n2026-06-29,100.0,102.0,99.0,{close_line},1000"
    return DailyContext(
        symbol="600519.SS", market="cn", sim_date="2026-06-29",
        ohlcv_tail_csv=tail, indicators={}, news=[],
        news_delta=news_delta,
        prev_decision=json.loads(COAST["decision_json"]),
        coast=COAST,
    )


def ctx_first_day(close_line=100.0):
    """Exactly what the engine passes on a session's first decision day:
    no coast plan and no news delta (the signature only exists once a
    decision has been taken)."""
    tail = "\n".join(
        f"2026-06-{d:02d},100.0,102.0,99.0,{100 + i},1000" for i, d in enumerate(range(10, 29))
    ) + f"\n2026-06-29,100.0,102.0,99.0,{close_line},1000"
    return DailyContext(
        symbol="600519.SS", market="cn", sim_date="2026-06-29",
        ohlcv_tail_csv=tail, indicators={}, news=[],
        news_delta=[], prev_decision=None, coast=None,
    )


def run(name, agent, ctx, expect_escalate, expect_action=None):
    decision, audit, response, flags = agent.decide(ctx)
    escalated = agent.graph.calls == 1
    ok = escalated == expect_escalate
    if expect_action is not None:
        ok = ok and decision.action == expect_action
    print(f"{name:<34} escalate={escalated!s:<5} action={decision.action:<5} "
          f"pct={decision.position_pct:<5} band=[{decision.recheck_lower}, {decision.recheck_upper}]")
    print(f"{'':<34} flag: {flags[0][:90]}")
    print(f"{'':<34} {'OK' if ok else 'FAIL'}")
    return ok


def main() -> int:
    results = []

    # 1) deterministic rule hit — 财报 announcement
    agent = make_agent()
    results.append(run(
        "1 财报公告（规则命中）", agent,
        ctx_with([{"date": "2026-06-29", "title": "2026年半年度业绩预告", "kind": "notice"}]),
        expect_escalate=True, expect_action="buy",
    ))
    assert agent._gate_llm.calls == 0, "规则命中时不应调用 LLM 闸门"
    print(f"{'':<34} gate LLM 调用次数: 0（省掉了）\n")

    # 2) new news but no rule hit -> LLM says high
    agent = make_agent(impact="high", reason="机构大幅上调目标价")
    results.append(run(
        "2 新消息 + LLM 评估 high", agent,
        ctx_with([{"date": "2026-06-29", "title": "关于办公地址变更的公告", "kind": "notice"}]),
        expect_escalate=True, expect_action="buy",
    ))
    print(f"{'':<34} gate LLM 调用次数: {agent._gate_llm.calls}\n")

    # 3) new news, LLM says low -> stand pat
    agent = make_agent(impact="low", reason="例行公告，无实质影响")
    results.append(run(
        "3 新消息 + LLM 评估 low", agent,
        ctx_with([{"date": "2026-06-29", "title": "关于办公地址变更的公告", "kind": "notice"}]),
        expect_escalate=False, expect_action="hold",
    ))
    assert agent._gate_llm.calls == 1
    print()

    # 4) no news, price inside band -> stand pat
    agent = make_agent()
    results.append(run(
        "4 无消息 + 价格在区间内", agent,
        ctx_with([], close_line=119.0),
        expect_escalate=False, expect_action="hold",
    ))
    print()

    # 5) no news, price smashed through the top -> escalate
    agent = make_agent()
    results.append(run(
        "5 无消息 + 价格大幅破上沿", agent,
        ctx_with([], close_line=180.0),
        expect_escalate=True, expect_action="buy",
    ))
    print()

    # 6) price just outside the band (no news) must now escalate — re-centring
    #    the band used to swallow a slow drift and let a deep drawdown accumulate
    agent = make_agent()
    results.append(run(
        "6 无消息 + 价格刚出区间（升级）", agent,
        ctx_with([], close_line=131.0),
        expect_escalate=True, expect_action="buy",
    ))
    print()

    # 7) first decision day of a session: no standing decision to maintain,
    #    so the full pipeline MUST run (was the day-0 bug: silent hold forever)
    agent = make_agent()
    results.append(run(
        "7 会话首日（无在持判断）", agent,
        ctx_first_day(close_line=100.0),
        expect_escalate=True, expect_action="buy",
    ))
    print()

    # 8) stand-pat streak cap: N consecutive "nothing changed" verdicts must
    #    force a full-pipeline review — a "维持原判断" chain cannot renew itself
    #    forever while the market moves on
    settings = WebappSettings(llm_provider="glm-cn", model="glm-5.3-flash",
                              graph_trigger="on_news", max_standpat_streak=3)
    agent = HybridDecisionAgent(settings, graph=FakeGraph())
    agent._gate_llm = FakeGateLLM()
    streak_results = []
    for i in range(3):
        decision, _a, _r, flags = agent.decide(ctx_with([], close_line=119.0))
        escalated = agent.graph.calls > 0
        streak_results.append(escalated)
        print(f"8 连续 stand-pat 第{i + 1}日 escalate={escalated!s:<5} "
              f"action={decision.action:<5} flag: {flags[0][:70]}")
    ok8 = streak_results == [False, False, True]
    print(f"{'':<38} {'OK' if ok8 else 'FAIL'}（第3日必须强制复核）")
    results.append(ok8)

    print(f"\nRESULT: {'PASS' if all(results) else 'FAIL'} ({sum(results)}/{len(results)})")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
