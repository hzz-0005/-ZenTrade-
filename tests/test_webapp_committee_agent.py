import json
from types import SimpleNamespace

import pytest

from webapp.config import WebappSettings
from webapp.engine.committee_agent import CommitteeDecisionAgent
from webapp.engine.context_builder import DailyContext


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False))


def _ctx(*, cash=50_000, shares=500, market="us", symbol="TEST", fundamentals=None):
    return DailyContext(
        symbol=symbol,
        market=market,
        sim_date="2026-01-22",
        ohlcv_tail_csv=(
            "Date,Open,High,Low,Close,Volume\n"
            "2026-01-21,99,101,98,100,1000\n"
            "2026-01-22,100,102,99,101,1200"
        ),
        indicators={"rsi": 52, "close_20_sma": 98},
        news=[],
        fundamentals=fundamentals,
        portfolio={
            "cash": cash,
            "shares": shares,
            "equity": cash + shares * 101,
            "avg_cost": 95,
            "open_pnl": shares * 6,
        },
    )


def test_committee_normal_path_uses_exactly_two_calls():
    analyst = FakeLLM(
        {
            "bull_case": ["trend"],
            "bear_case": ["valuation"],
            "risks": ["gap"],
            "uncertainties": ["news"],
            "key_levels": {"support": 100, "resistance": 120},
        }
    )
    decider = FakeLLM(
        {
            "action": "hold",
            "position_pct": 0,
            "confidence": 0.7,
            "reasoning": "balanced",
            "key_signals": [],
            "used_skills": [],
        }
    )
    agent = CommitteeDecisionAgent(WebappSettings(), analyst, decider)

    decision, audit, _, flags = agent.decide(_ctx())

    assert decision.action == "hold"
    assert len(analyst.calls) == 1
    assert len(decider.calls) == 1
    assert agent._call_count == 2
    assert "多头证据" in audit
    assert "adaptive:path=committee" in flags


def test_decision_prompt_contains_analysis_and_real_account():
    analyst = FakeLLM(
        {
            "bull_case": ["trend"],
            "bear_case": [],
            "risks": [],
            "uncertainties": [],
            "key_levels": {},
        }
    )
    decider = FakeLLM({"action": "hold", "reasoning": "wait"})
    agent = CommitteeDecisionAgent(WebappSettings(), analyst, decider)

    agent.decide(_ctx(cash=12_345, shares=67))

    prompt = str(decider.calls[0])
    assert "trend" in prompt
    assert "12345" in prompt
    assert "67" in prompt


def test_committee_reconciles_sell_when_account_is_empty():
    analyst = FakeLLM(
        {
            "bull_case": [],
            "bear_case": ["weak"],
            "risks": [],
            "uncertainties": [],
            "key_levels": {},
        }
    )
    decider = FakeLLM(
        {"action": "sell", "position_pct": 1, "confidence": 0.8, "reasoning": "exit"}
    )
    agent = CommitteeDecisionAgent(WebappSettings(), analyst, decider)

    decision, _, _, flags = agent.decide(_ctx(cash=100_000, shares=0))

    assert decision.action == "hold"
    assert any("当前空仓" in flag for flag in flags)


def test_committee_enforces_daily_call_budget():
    settings = WebappSettings(adaptive_max_calls_per_day=1)
    analyst = FakeLLM(
        {
            "bull_case": [],
            "bear_case": [],
            "risks": [],
            "uncertainties": [],
            "key_levels": {},
        }
    )
    agent = CommitteeDecisionAgent(settings, analyst, FakeLLM({"action": "hold"}))

    with pytest.raises(Exception, match="exceeded 1 calls"):
        agent.decide(_ctx())


def test_committee_system_policy_treats_high_cn_pe_as_sizing_not_veto():
    analyst = FakeLLM(
        {
            "bull_case": [],
            "bear_case": [],
            "risks": [],
            "uncertainties": [],
            "key_levels": {},
        }
    )
    decider = FakeLLM({"action": "hold", "reasoning": "reviewed"})
    agent = CommitteeDecisionAgent(WebappSettings(), analyst, decider)

    agent.decide(
        _ctx(
            market="cn",
            symbol="300308.SZ",
            fundamentals={
                "摊薄每股收益(元)": 0.05,
                "上年同期摊薄每股收益(元)": -0.10,
                "扣除非经常性损益后的每股收益(元)": -0.02,
                "市盈率PE(年化估算)": 180.0,
            },
        )
    )

    assert "高PE只能影响仓位与置信度，不能单独否决买入" in analyst.calls[0][0][1]
    assert "高PE只能影响仓位与置信度，不能单独否决买入" in decider.calls[0][0][1]
