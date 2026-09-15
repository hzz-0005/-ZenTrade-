from unittest.mock import MagicMock

import pytest

from webapp.config import WebappSettings
from webapp.core.models import Decision
from webapp.engine.adaptive_agent import AdaptiveDecisionAgent
from webapp.engine.context_builder import DailyContext


def _ctx(*, previous=True, news_delta=None, close=100.0, lower=95.0, upper=105.0):
    rows = ["Date,Open,High,Low,Close,Volume"]
    rows.extend(
        f"2026-01-{day:02d},100,101,99,{close},1000" for day in range(1, 22)
    )
    prev = Decision(
        action="hold",
        confidence=0.7,
        reasoning="existing thesis",
        recheck_days=2,
        recheck_lower=lower,
        recheck_upper=upper,
        target_position_pct=0.5,
    ).model_dump()
    return DailyContext(
        symbol="TEST",
        market="us",
        sim_date="2026-01-22",
        ohlcv_tail_csv="\n".join(rows),
        indicators={},
        news=list(news_delta or []),
        news_delta=list(news_delta or []),
        portfolio={"cash": 50_000, "shares": 500, "equity": 100_000, "avg_cost": 95},
        prev_decision=prev if previous else None,
        coast={"lower": lower, "upper": upper, "origin_date": "2026-01-20"}
        if previous
        else None,
    )


def _delegate():
    delegate = MagicMock()
    delegate._call_count = 1
    delegate.decide.return_value = (
        Decision(action="hold", reasoning="reviewed"),
        "audit",
        "response",
        [],
    )
    return delegate


def test_existing_thesis_with_no_change_is_zero_call():
    fast, committee = _delegate(), _delegate()
    agent = AdaptiveDecisionAgent(WebappSettings(), fast_agent=fast, committee=committee)

    decision, _, _, flags = agent.decide(_ctx())

    assert decision.action == "hold"
    assert "adaptive:path=zero_call" in flags
    assert agent._call_count == 0
    fast.decide.assert_not_called()
    committee.decide.assert_not_called()


def test_first_day_uses_committee():
    committee = _delegate()
    agent = AdaptiveDecisionAgent(WebappSettings(), fast_agent=_delegate(), committee=committee)
    ctx = _ctx(previous=False)

    _, _, _, flags = agent.decide(ctx)

    committee.decide.assert_called_once_with(ctx)
    assert "adaptive:path=committee" in flags


def test_routine_new_information_uses_fast_path():
    fast = _delegate()
    agent = AdaptiveDecisionAgent(WebappSettings(), fast_agent=fast, committee=_delegate())
    ctx = _ctx(news_delta=[{"kind": "news", "title": "Routine product update"}])

    _, _, _, flags = agent.decide(ctx)

    fast.decide.assert_called_once_with(ctx)
    assert "adaptive:path=fast" in flags


def test_major_news_uses_committee():
    committee = _delegate()
    agent = AdaptiveDecisionAgent(WebappSettings(), fast_agent=_delegate(), committee=committee)
    ctx = _ctx(news_delta=[{"kind": "notice", "title": "公司业绩预增公告"}])

    agent.decide(ctx)

    committee.decide.assert_called_once_with(ctx)


def test_price_outside_standing_band_uses_committee():
    committee = _delegate()
    agent = AdaptiveDecisionAgent(WebappSettings(), fast_agent=_delegate(), committee=committee)
    ctx = _ctx(close=110.0)

    agent.decide(ctx)

    committee.decide.assert_called_once_with(ctx)


def test_delegate_failure_with_existing_thesis_degrades_to_stand_pat():
    fast = _delegate()
    fast.decide.side_effect = TimeoutError("slow provider")
    agent = AdaptiveDecisionAgent(WebappSettings(), fast_agent=fast, committee=_delegate())
    ctx = _ctx(news_delta=[{"kind": "news", "title": "Routine product update"}])

    decision, _, _, flags = agent.decide(ctx)

    assert decision.action == "hold"
    assert "adaptive:degraded=fast" in flags


def test_transient_connection_failure_degrades_to_stand_pat():
    fast = _delegate()
    fast.decide.side_effect = ConnectionError("connection reset by peer")
    agent = AdaptiveDecisionAgent(WebappSettings(), fast_agent=fast, committee=_delegate())
    ctx = _ctx(news_delta=[{"kind": "news", "title": "Routine product update"}])

    decision, _, _, flags = agent.decide(ctx)

    assert decision.action == "hold"
    assert "adaptive:degraded=fast" in flags


def test_budget_exceeded_degrades_to_stand_pat():
    from webapp.core.errors import LLMBudgetExceeded

    fast = _delegate()
    fast.decide.side_effect = LLMBudgetExceeded("adaptive committee exceeded 4 calls")
    agent = AdaptiveDecisionAgent(WebappSettings(), fast_agent=fast, committee=_delegate())
    ctx = _ctx(news_delta=[{"kind": "news", "title": "Routine product update"}])

    decision, _, _, flags = agent.decide(ctx)

    assert decision.action == "hold"
    assert "adaptive:degraded=fast" in flags


def test_structural_failure_is_not_disguised_as_stand_pat():
    """A persistent provider failure (bad API key / 400) must fail the session.

    Degrading structural errors to silent holds lets a misconfigured provider
    produce a "successful" backtest whose every day is a fabricated no-trade —
    the failure mode HANDOVER.md pitfall #4 calls worse than crashing.
    """
    fast = _delegate()
    fast.decide.side_effect = RuntimeError(
        "Invalid API key provided (error code: 401)"
    )
    agent = AdaptiveDecisionAgent(WebappSettings(), fast_agent=fast, committee=_delegate())
    ctx = _ctx(news_delta=[{"kind": "news", "title": "Routine product update"}])

    with pytest.raises(RuntimeError, match="401"):
        agent.decide(ctx)


def test_parse_failure_is_not_disguised_as_stand_pat():
    committee = _delegate()
    committee.decide.side_effect = ValueError("committee analysis returned no JSON object")
    agent = AdaptiveDecisionAgent(WebappSettings(), fast_agent=_delegate(), committee=committee)
    ctx = _ctx(close=110.0)  # price outside band -> committee path

    with pytest.raises(ValueError, match="no JSON object"):
        agent.decide(ctx)


def test_first_day_committee_failure_is_not_disguised_as_hold():
    committee = _delegate()
    committee.decide.side_effect = TimeoutError("slow provider")
    agent = AdaptiveDecisionAgent(WebappSettings(), fast_agent=_delegate(), committee=committee)

    with pytest.raises(TimeoutError, match="slow provider"):
        agent.decide(_ctx(previous=False))


def test_fast_streak_forces_committee_review():
    settings = WebappSettings(adaptive_fast_streak_limit=1)
    fast, committee = _delegate(), _delegate()
    agent = AdaptiveDecisionAgent(settings, fast_agent=fast, committee=committee)
    ctx = _ctx(news_delta=[{"kind": "news", "title": "Routine product update"}])

    agent.decide(ctx)
    agent.decide(ctx)

    assert fast.decide.call_count == 1
    assert committee.decide.call_count == 1
