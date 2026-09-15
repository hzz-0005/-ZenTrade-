"""Offline check of the full-pipeline adapter: graph output -> webapp Decision.

Swaps in a fake TradingAgentsGraph so the mapping (rating -> action, PM levels
-> stop-loss/take-profit/target position + coast band, reports -> key signals,
audit text) is exercised without spending a real multi-agent run. Also
unit-checks the multi-factor position sizing in isolation.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from webapp.config import WebappSettings  # noqa: E402
from webapp.engine.context_builder import DailyContext  # noqa: E402
from webapp.engine.graph_agent import GraphDecisionAgent, _size_position  # noqa: E402
import tradingagents.graph.trading_graph as tg  # noqa: E402

TAIL = "\n".join(
    f"2026-06-{d:02d},100.0,102.0,99.0,{100 + i},1000" for i, d in enumerate(range(10, 30))
)


class FakeGraph:
    """Stands in for TradingAgentsGraph.propagate()."""

    FINAL = (
        "**Rating**: Buy\n\n"
        "**Executive Summary**: 分批建仓，跌破止损离场。\n\n"
        "**Investment Thesis**: 盈利预测上修，估值仍有空间。\n\n"
        "**Price Target**: 130.0\n\n"
        "**Stop Loss**: 98.0\n\n"
        "**Take Profit**: 128.0\n\n"
        "**Target Position**: 80%\n\n"
        "**Risk Level**: medium\n\n"
        "**Time Horizon**: 2-4 weeks"
    )
    SIGNAL = "Buy"

    def __init__(self, **_kwargs):
        self.memory_log = type("M", (), {"store_decision": lambda **_k: None})()

    def _resolve_pending_entries(self, *_a, **_k):
        return None

    def propagate(self, ticker, trade_date, extra_context="", account_state=""):
        trader = (
            "**Action**: Buy\n\n**Reasoning**: 技术面站上均线，消息面研报上调评级。\n\n"
            "**Entry Price**: 105.0\n\n**Stop Loss**: 96.0\n\n"
            "**Position Sizing**: 12% of portfolio\n\n"
            "FINAL TRANSACTION PROPOSAL: **BUY**"
        )
        state = {
            "company_of_interest": ticker,
            "trade_date": trade_date,
            "market_report": "价格站上 60 日均线，MACD 金叉。成交量温和放大。",
            "sentiment_report": "散户情绪偏乐观但未见极端。",
            "news_report": "券商上调评级至买入；公司发布季度业绩预增公告。",
            "fundamentals_report": "ROE 维持高位，营收增速回升。",
            "investment_plan": "建议分批建仓。",
            "trader_investment_plan": trader,
            "final_trade_decision": self.FINAL,
            "investment_debate_state": {
                "bull_history": "多头：盈利预测上修。",
                "bear_history": "空头：估值已反映预期。",
                "judge_decision": "多头论据更扎实，支持建仓。",
            },
            "risk_debate_state": {
                "aggressive_history": "激进：可加仓。",
                "conservative_history": "保守：控制仓位。",
                "neutral_history": "中性：分批。",
                "judge_decision": "采用中性方案。",
            },
        }
        return state, self.SIGNAL


class FakeFlatSellGraph(FakeGraph):
    """The 600396 failure mode: already flat, PM still says 'trim the holding'."""

    FINAL = (
        "**Rating**: Underweight\n\n"
        "**Executive Summary**: 对现有持仓分批止盈，先减掉 1/3。\n\n"
        "**Investment Thesis**: 涨幅过大，风险收益比不对称。\n\n"
        "**Price Target**: 90.0\n\n"
        "**Stop Loss**: 130.0\n\n"
        "**Target Position**: 30%\n\n"
        "**Risk Level**: high\n\n"
        "**Time Horizon**: 1-2 weeks"
    )
    SIGNAL = "SELL"


class FakeTrimGraph(FakeGraph):
    """Full position, PM wants to trim toward 25% — the 70%-dump failure mode."""

    FINAL = (
        "**Rating**: Underweight\n\n"
        "**Executive Summary**: 连涨后兑现利润，目标仓位降至 25%。\n\n"
        "**Investment Thesis**: 短线超买，波段顶部。\n\n"
        "**Price Target**: 140.0\n\n"
        "**Stop Loss**: 100.0\n\n"
        "**Target Position**: 25%\n\n"
        "**Risk Level**: medium\n\n"
        "**Time Horizon**: 1-2 weeks"
    )
    SIGNAL = "SELL"


class FakeExitGraph(FakeGraph):
    """Explicit Sell rating = full exit; the graduated-sell cap must NOT apply."""

    FINAL = (
        "**Rating**: Sell\n\n"
        "**Executive Summary**: 持有逻辑破坏，清仓离场。\n\n"
        "**Investment Thesis**: 盈利趋势恶化。\n\n"
        "**Price Target**: 80.0\n\n"
        "**Stop Loss**: 130.0\n\n"
        "**Target Position**: 0%\n\n"
        "**Risk Level**: high\n\n"
        "**Time Horizon**: 1 week"
    )
    SIGNAL = "SELL"


class FakeOverweightSmallTargetGraph(FakeGraph):
    """Holding 25%, PM says Overweight but writes target 6% (< current) — the
    rating/target direction mismatch that used to degrade to a no-op hold."""

    FINAL = (
        "**Rating**: Overweight\n\n"
        "**Executive Summary**: 趋势完好，继续增持。\n\n"
        "**Investment Thesis**: 站上均线，回踩即加仓。\n\n"
        "**Price Target**: 150.0\n\n"
        "**Stop Loss**: 120.0\n\n"
        "**Target Position**: 6%\n\n"
        "**Risk Level**: medium\n\n"
        "**Time Horizon**: 2-4 weeks"
    )
    SIGNAL = "BUY"


def _ctx(portfolio: dict) -> DailyContext:
    return DailyContext(
        symbol="600519.SS", market="cn", sim_date="2026-06-29",
        ohlcv_tail_csv=TAIL, indicators={}, news=[], portfolio=portfolio,
    )


def _decide_with(graph_cls, portfolio: dict):
    tg.TradingAgentsGraph = graph_cls
    settings = WebappSettings(llm_provider="glm-cn", model="glm-5.3-flash")
    agent = GraphDecisionAgent(settings)
    return agent.decide(_ctx(portfolio))


def main() -> int:
    tg.TradingAgentsGraph = FakeGraph

    settings = WebappSettings(llm_provider="glm-cn", model="glm-5.3-flash")
    agent = GraphDecisionAgent(settings)

    ctx = DailyContext(
        symbol="600519.SS", market="cn", sim_date="2026-06-29",
        ohlcv_tail_csv=TAIL, indicators={}, news=[],
        portfolio={"cash": 100000.0, "shares": 0.0, "equity": 100000.0, "drawdown": 0.0},
    )
    decision, audit, response, flags = agent.decide(ctx)

    print("\n=== mapped Decision ===")
    print(json.dumps(json.loads(decision.model_dump_json()), ensure_ascii=False, indent=2))
    print("\nflags:", flags)

    ok = (
        decision.action == "buy"
        and decision.stop_loss == 98.0            # PM's own stop, not the trader's 96
        and decision.take_profit == 128.0
        and abs(decision.target_position_pct - 0.80) < 1e-6
        and decision.recheck_lower == 105.0       # re-entry trigger (trader entry) anchors the band floor
        and decision.recheck_upper == 130.0       # band anchored to PM price target
        and decision.recheck_days == 3            # "2-4 weeks"
        and 0.0 < decision.position_pct <= 1.0    # multi-factor sizing produced a positive buy
        and "止盈位 128" in " ".join(decision.key_signals)
        and "【仓位基准】" in decision.reasoning   # every decision states its position basis
        and "实际仓位 0%" in decision.reasoning    # ...and this one is from flat
    )
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    if not ok:
        return 1

    # --- scenario: flat account, PM hallucinates a holding ("trim the position") ---
    flat_port = {"cash": 100000.0, "shares": 0.0, "equity": 100000.0, "drawdown": 0.0}
    d_flat, _a, _r, flags_flat = _decide_with(FakeFlatSellGraph, flat_port)
    flat_ok = (
        d_flat.action == "hold"                                # sell from flat -> hold
        and d_flat.position_pct == 0.0
        and "【仓位核对】" in d_flat.reasoning                  # engine calls out the hallucination
        and any("空仓" in f for f in flags_flat)
        and "实际仓位 0%" in d_flat.reasoning
    )
    print(f"\nflat-hallucination: action={d_flat.action} flags={flags_flat}")
    print(f"FLAT RESULT: {'PASS' if flat_ok else 'FAIL'}")

    # --- scenario: full position, Underweight target 25% -> graduated sell cap ---
    full_port = {"cash": 0.0, "shares": 100000.0 / 119.0, "equity": 100000.0, "drawdown": 0.0}
    d_trim, _a, _r, flags_trim = _decide_with(FakeTrimGraph, full_port)
    trim_ok = (
        d_trim.action == "sell"
        and abs(d_trim.position_pct - 0.5) < 1e-6              # capped (uncapped would be 0.75)
        and any("分批兑现" in f for f in flags_trim)
        and abs(d_trim.target_position_pct - 0.25) < 1e-6      # PM target preserved
    )
    print(f"\ngraduated-sell: action={d_trim.action} pct={d_trim.position_pct} flags={flags_trim}")
    print(f"TRIM RESULT: {'PASS' if trim_ok else 'FAIL'}")

    # --- scenario: explicit Sell rating -> full exit, cap exempt ---
    d_exit, _a, _r, flags_exit = _decide_with(FakeExitGraph, full_port)
    exit_ok = (
        d_exit.action == "sell"
        and d_exit.position_pct >= 0.99                        # clear-out NOT capped
        and not any("分批兑现" in f for f in flags_exit)
    )
    print(f"\nfull-exit: action={d_exit.action} pct={d_exit.position_pct} flags={flags_exit}")
    print(f"EXIT RESULT: {'PASS' if exit_ok else 'FAIL'}")

    # --- scenario: holding 25%, Overweight but target 6% (< current) ---
    # The rating says "increase" while the target says "decrease to 6%" — the
    # model wrote a diversified portfolio slice instead of the final account
    # exposure. Trust the rating, fall back to the Overweight default target,
    # and buy — never silently hold.
    overweight_port = {"cash": 75000.0, "shares": 25000.0 / 119.0,
                       "equity": 100000.0, "drawdown": 0.0}
    d_over, _a, _r, flags_over = _decide_with(FakeOverweightSmallTargetGraph, overweight_port)
    over_ok = (
        d_over.action == "buy"
        and d_over.position_pct > 0.5                      # decisive add, not a hold
        and abs(d_over.target_position_pct - 0.70) < 1e-6  # fallback to Overweight default
        and any("方向矛盾" in f for f in flags_over)
    )
    print(f"\noverweight-small-target: action={d_over.action} pct={d_over.position_pct} "
          f"target={d_over.target_position_pct} flags={flags_over}")
    print(f"OVERWEIGHT RESULT: {'PASS' if over_ok else 'FAIL'}")

    # unit-check the sizing in isolation
    pct_no_portfolio = _size_position("buy", 0.5, 119.0, {})
    pct_drawdown = _size_position("buy", 0.5, 119.0,
                                  {"cash": 50000.0, "shares": 0.0, "equity": 100000.0, "drawdown": -0.3})
    pct_full = _size_position("buy", 0.5, 119.0,
                              {"cash": 0.0, "shares": 840.0, "equity": 100000.0, "drawdown": 0.0})
    # Decisive-add fix: the PM's target weight must be honoured, not dribbled
    # away by the old risk-budget/conviction temperers. A Buy targeting 80%
    # from a ~9% position should spend most of the cash (was ~0.19).
    pct_buy_decisive = _size_position("buy", 0.8, 100.0,
                                      {"cash": 90000.0, "shares": 90.0, "equity": 100000.0, "drawdown": 0.0})
    # Position-state awareness (the "sell 0% rejected" fix): a sell whose target
    # is at/above the current weight, or on an empty account, must size to 0 so
    # the caller degrades it to a hold — never a 0% sell that gets rejected.
    pct_sell_target_above = _size_position("sell", 0.30, 119.0,
                                           {"cash": 90000.0, "shares": 84.0, "equity": 100000.0, "drawdown": 0.0})
    pct_sell_flat = _size_position("sell", 0.0, 119.0,
                                   {"cash": 100000.0, "shares": 0.0, "equity": 100000.0, "drawdown": 0.0})
    pct_sell_real = _size_position("sell", 0.0, 119.0,
                                   {"cash": 90000.0, "shares": 84.0, "equity": 100000.0, "drawdown": 0.0})
    unit_ok = (
        pct_no_portfolio == 0.0       # no equity info -> cannot size -> 0
        and 0.0 < pct_drawdown < 1.0  # drawdown guard still trims, but not to zero
        and pct_full == 0.0           # no cash -> cannot add
        and pct_buy_decisive > 0.7    # decisive add honours the PM target (was ~0.19)
        and pct_sell_target_above == 0.0   # target above current -> no trim (degrade to hold)
        and pct_sell_flat == 0.0           # no position -> no sell
        and pct_sell_real >= 0.99          # a tiny remaining position clears out, not "sells half"
    )
    print(f"unit sizing: no_portfolio={pct_no_portfolio} drawdown={pct_drawdown} full={pct_full} buy_decisive={pct_buy_decisive}")
    print(f"unit sell-state: target_above={pct_sell_target_above} flat={pct_sell_flat} clear_out={pct_sell_real}")
    print(f"UNIT RESULT: {'PASS' if unit_ok else 'FAIL'}")
    return 0 if (unit_ok and flat_ok and trim_ok and exit_ok and over_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
