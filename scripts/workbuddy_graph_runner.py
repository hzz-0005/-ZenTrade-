"""WorkBuddy-LLM channel: run the FULL TradingAgents pipeline with WorkBuddy as the LLM.

Everything deterministic is the project's real code:
  - BacktestEngine loop semantics (forced day-0 buy, hard risk stops, coast,
    fresh-news delta, tech break/improve triggers, stand-pat cap)
  - HybridDecisionAgent.decide() (the real gate ordering, incl. rule_hits)
  - GraphDecisionAgent.decide() (the REAL parser/reconciler/sizer/band code,
    fed a final_state produced by WorkBuddy acting as each LLM node)
  - ExecutionModel / Portfolio fills

WorkBuddy plays: the 4 analysts, bull/bear, research manager, trader, the 3
risk debators, the PM, and the news-gate impact triage (gate_policy below).

Data plane (no lookahead): project graph_cache CSV (prices), the announcement
archive reconstructed from what the previous API sessions actually saw, and
the per-day context dumped by the gateway (workbuddy_run_0508_0731_ctx.json).

Usage:
    python scripts/workbuddy_graph_runner.py            # runs until a decision day, then pauses
    (WorkBuddy writes webapp/data/workbuddy_day_response.json, re-runs)
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from webapp.config import WebappSettings  # noqa: E402
from webapp.core.models import Decision, Fill  # noqa: E402
from webapp.core.portfolio import ExecutionModel, Portfolio  # noqa: E402
from webapp.engine.context_builder import DailyContext  # noqa: E402
from webapp.engine.news_gate import detect_technical_breakdown, detect_technical_improvement  # noqa: E402
from webapp.engine.graph_agent import GraphDecisionAgent, _last_close  # noqa: E402
from webapp.skills.library import SkillLibrary  # noqa: E402

TICKER = "600396.SS"
START, END = "2026-05-08", "2026-07-31"
CAPITAL = 1_000_000.0
COMMISSION = 0.0005
STANDPAT_CAP = 10          # run script for 600396 windows raised this from 5
MAX_DAILY_SELL_FRAC = 0.5
FALLBACK_STOP_PCT = 0.08
MAX_DD_STOP = 0.20
DD_REDUCE_TO = 0.30
BAND_PCT = 0.03

DATA = ROOT / "webapp" / "data"
CTX_PATH = DATA / "workbuddy_run_0508_0731_ctx.json"
STATE_PATH = DATA / "workbuddy_run_state.json"
REQ_PATH = DATA / "workbuddy_day_request.json"
RESP_PATH = DATA / "workbuddy_day_response.json"

# Announcement archive as the previous API sessions actually saw it (titles
# recovered from their recorded data_flags; dates pinned by the day each one
# first appeared as a fresh-news delta).
NOTICE_ARCHIVE: dict[str, list[dict]] = {
    "2026-05-12": [{"date": "2026-05-12", "title": "华电辽能:关于股票交易风险提示的公告", "kind": "notice"}],
    "2026-07-11": [{"date": "2026-07-11", "title": "华电辽能:持股5%以上股东集中竞价减持股份计划公告", "kind": "notice"}],
    "2026-07-22": [{"date": "2026-07-22", "title": "华电辽能:股票交易异常波动公告", "kind": "notice"}],
}

# WorkBuddy acting as the cheap news-gate impact triage LLM (llm_impact).
# Only reached for material news that hits NO deterministic rule.
GATE_POLICY = {
    "2026-05-12": ("low", "连续涨停后交易所程序性风险提示，无基本面变化"),
    "2026-07-22": ("low", "异动例行程序性披露，无实质基本面变化"),
}


def archive_notices(curr_date: str, lookback_days: int = 7) -> list[dict]:
    import pandas as pd
    start = pd.Timestamp(curr_date) - pd.Timedelta(days=lookback_days)
    out = []
    for d, items in NOTICE_ARCHIVE.items():
        if start <= pd.Timestamp(d) <= pd.Timestamp(curr_date):
            out.extend(items)
    return sorted(out, key=lambda n: n.get("date", ""), reverse=True)


def material_items(curr_date: str) -> dict[tuple[str, str], dict]:
    return {(i.get("date", ""), i.get("title", "")): i for i in archive_notices(curr_date, 7)}


def news_signature(curr_date: str) -> tuple:
    return tuple(sorted(material_items(curr_date).keys()))


def load_ctx() -> list[dict]:
    return json.loads(CTX_PATH.read_text(encoding="utf-8"))


def new_state() -> dict:
    return {
        "day_index": 0,
        "cash": CAPITAL,
        "shares": 0.0,
        "avg_cost": 0.0,
        "coast": None,          # {"until_index","lower","upper","origin_index","origin_date","decision_json","news_sig"}
        "peak_equity": None,
        "standpat_streak": 0,
        "records": [],          # ledger rows
        "last_buy_index": None,
    }


def save_state(st: dict) -> None:
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return new_state()


# ---------------------------------------------------------------------------
# Deterministic execution (the project's real ExecutionModel / Portfolio)
# ---------------------------------------------------------------------------
def make_exec_model() -> ExecutionModel:
    return ExecutionModel(commission_rate=COMMISSION, min_commission=0.0,
                          slippage_bps=0.0, min_lot_shares=0.0)


def execute(decision: Decision, portfolio: Portfolio, em: ExecutionModel, close: float) -> Fill:
    if decision.action == "sell" and portfolio.shares <= 0:
        return Fill(action="hold", requested_pct=decision.position_pct,
                    reason="sell ignored: no open position (long-only)")
    from webapp.core.errors import InvalidDecision
    try:
        fill = em.validate_and_fill(decision, portfolio, close)
    except InvalidDecision as exc:
        return Fill(action="rejected", requested_pct=decision.position_pct, reason=exc.reason)
    if fill.action == "buy":
        portfolio.apply_fill("buy", fill.price, fill.shares, fill.fee)
    elif fill.action == "sell":
        portfolio.apply_fill("sell", fill.price, fill.shares, fill.fee)
    return fill


def make_coast(decision: Decision, close_t: float, day_index: int, trade_date: str) -> dict:
    lower, upper = decision.recheck_lower, decision.recheck_upper
    if not (lower and upper and 0 < lower < upper):
        lower, upper = close_t * 0.97, close_t * 1.03
    return {
        "until_index": day_index + max(1, min(decision.recheck_days, 10)),
        "lower": float(lower), "upper": float(upper),
        "origin_index": day_index, "origin_date": trade_date,
        "decision_json": decision.model_dump_json(),
        "news_sig": [list(x) for x in news_signature(trade_date)],
    }


def current_drawdown(st: dict, equity_now: float) -> float:
    if st["peak_equity"] and st["peak_equity"] > 0:
        return equity_now / st["peak_equity"] - 1
    return 0.0


def sell_to_target(close_t: float, portfolio: Portfolio, target_pos: float) -> float:
    equity = portfolio.equity(close_t)
    if equity <= 0:
        return 1.0
    current = portfolio.shares * close_t / equity
    if current <= target_pos:
        return 0.0
    return max(0.0, min(1.0, 1.0 - target_pos / current))


def exec_risk_sell(st: dict, day_index: int, trade_date: str, close_t: float,
                   portfolio: Portfolio, em: ExecutionModel, reason: str, sell_pct: float) -> bool:
    if sell_pct <= 0 or portfolio.shares <= 0:
        return False
    decision = Decision(action="sell", position_pct=round(sell_pct, 4), confidence=1.0,
                        reasoning=reason, key_signals=[f"risk-control: {reason}"],
                        used_skills=[], recheck_days=1)
    fill = execute(decision, portfolio, em, close_t)
    if fill.action in ("rejected", "hold"):
        return False
    # persist the fill into the runner state (same as run_decision_day)
    st["cash"], st["shares"], st["avg_cost"] = portfolio.cash, portfolio.shares, portfolio.avg_cost
    equity = portfolio.equity(close_t)
    st["records"].append({
        "day": day_index, "date": trade_date, "action": fill.action,
        "requested_pct": fill.requested_pct, "shares": fill.shares, "price": fill.price,
        "fee": fill.fee, "cash": round(portfolio.cash, 2), "shares_after": round(portfolio.shares, 4),
        "equity": round(equity, 2), "llm_calls": 0, "flags": [f"risk-control: {reason}"],
        "reasoning": reason,
    })
    return True


def check_risk_stops(st: dict, day_index: int, trade_date: str, close_t: float,
                     portfolio: Portfolio, em: ExecutionModel) -> bool:
    if portfolio.shares <= 0:
        return False
    equity_now = portfolio.equity(close_t)
    if st["peak_equity"] is None or equity_now > st["peak_equity"]:
        st["peak_equity"] = equity_now

    stop_loss = take_profit = None
    if st["coast"] and st["coast"].get("decision_json"):
        try:
            d = Decision.model_validate_json(st["coast"]["decision_json"])
            stop_loss, take_profit = d.stop_loss, d.take_profit
        except Exception:
            pass
    stop_is_fallback = False
    if (stop_loss is None or stop_loss <= 0) and FALLBACK_STOP_PCT and portfolio.avg_cost > 0:
        stop_loss = portfolio.avg_cost * (1 - FALLBACK_STOP_PCT)
        stop_is_fallback = True

    if st["peak_equity"] and st["peak_equity"] > 0:
        dd = equity_now / st["peak_equity"] - 1
        if dd <= -MAX_DD_STOP:
            return exec_risk_sell(st, day_index, trade_date, close_t, portfolio, em,
                reason=f"回撤熔断：权益自峰值回撤 {dd:.1%}（超过上限 {MAX_DD_STOP:.0%}），"
                       f"强制降至目标仓位 {DD_REDUCE_TO:.0%}",
                sell_pct=sell_to_target(close_t, portfolio, DD_REDUCE_TO))
    if stop_loss and 0 < stop_loss and close_t <= stop_loss:
        source = "兜底" if stop_is_fallback else ""
        return exec_risk_sell(st, day_index, trade_date, close_t, portfolio, em,
            reason=f"止损触发（{source or '模型'}）：收盘 {close_t:g} 跌破止损位 {stop_loss:g}，清仓离场",
            sell_pct=1.0)
    if take_profit and 0 < take_profit and close_t >= take_profit:
        return exec_risk_sell(st, day_index, trade_date, close_t, portfolio, em,
            reason=f"止盈触发：收盘 {close_t:g} 触及止盈位 {take_profit:g}，兑现一半锁定利润",
            sell_pct=0.5)
    return False


# ---------------------------------------------------------------------------
# The decision day: hybrid gate (real code) + graph decide (real code) with
# WorkBuddy's pipeline outputs injected as the final_state.
# ---------------------------------------------------------------------------
def build_ctx(day: dict, st: dict, days: list[dict], news_delta: list[dict],
              prev_coast: dict | None, extra_flags: list[str]) -> DailyContext:
    import pandas as pd
    tail = day["tail"]
    lines = ["Date,Open,High,Low,Close,Volume"]
    for d, c, v in tail:
        lines.append(f"{d},{c:.4f},{c:.4f},{c:.4f},{c:.4f},{v:.0f}")
    ohlcv_csv = "\n".join(lines)

    close = day["close"]
    prev_close = tail[-2][1] if len(tail) > 1 else close
    pos_value = st["shares"] * prev_close
    equity = st["cash"] + pos_value
    portfolio_now = {
        "cash": round(st["cash"], 2), "shares": round(st["shares"], 4),
        "avg_cost": round(st["avg_cost"], 4) if st["shares"] else 0.0,
        "equity": round(equity, 2),
        "open_pnl": round((prev_close - st["avg_cost"]) * st["shares"], 2) if st["shares"] else 0.0,
        "drawdown": round(current_drawdown(st, equity), 4),
    }

    recent = []
    for r in st["records"][-5:]:
        plan_bits = []
        try:
            dj = json.loads(r.get("decision_json") or "{}")
            if dj.get("stop_loss"):
                plan_bits.append(f"止损{float(dj['stop_loss']):g}")
            if dj.get("take_profit"):
                plan_bits.append(f"止盈{float(dj['take_profit']):g}")
            if dj.get("target_position_pct") is not None:
                plan_bits.append(f"目标仓{float(dj['target_position_pct']):.0%}")
        except Exception:
            pass
        detail = f"{r['shares']}股@{r['price']}" if r["action"] in ("buy", "sell") and r.get("shares") else ""
        recent.append({"date": r["date"], "action": r["action"], "detail": detail,
                       "plan": ("计划: " + "/".join(plan_bits)) if plan_bits else ""})

    # skills: the real library, real DB, real temporal clamp
    try:
        skills = SkillLibrary(WebappSettings()).select_for_day(day["date"])
    except Exception:
        skills = []

    notices = archive_notices(day["date"], 7)
    news_flags = [f"news: archive backfill ({len(notices)} announcements)"] if notices else \
                 ["news: none for window (cn)"]

    return DailyContext(
        symbol=TICKER, market="cn", sim_date=day["date"],
        ohlcv_tail_csv=ohlcv_csv,
        indicators=day["ind"],
        news=[{**n, "kind": n.get("kind", "notice")} for n in notices],
        fundamentals=day.get("fundamentals"),
        sentiment=day.get("sentiment"),
        benchmark=None,
        portfolio=portfolio_now,
        recent_days=recent,
        data_flags=list(extra_flags) + news_flags + day.get("fund_flags", [])
                    + day.get("sent_flags", []),
        skills=skills,
        news_delta=list(news_delta or []),
        prev_decision=(json.loads(prev_coast["decision_json"])
                       if prev_coast and prev_coast.get("decision_json") else None),
        coast=prev_coast,
    )


def _signal_of(text: str) -> str:
    import re
    m = re.search(r"\*\*Rating\*\*\s*:\s*(\w+)", text or "")
    return m.group(1) if m else ""


def run_decision_day(day: dict, st: dict, days: list[dict], recheck_reasons: list[str],
                     news_delta: list[dict], prev_coast: dict | None):
    settings = WebappSettings()
    settings.max_standpat_streak = STANDPAT_CAP
    settings.max_daily_sell_frac = MAX_DAILY_SELL_FRAC
    settings.recheck_band_pct = BAND_PCT

    flags = list(recheck_reasons)

    # idle-cash nudge (engine rule)
    day_index = st["day_index"]
    if st["last_buy_index"] is not None or True:
        last_buy = st["last_buy_index"] if st["last_buy_index"] is not None else -1
        equity = st["cash"] + st["shares"] * day["tail"][-2][1]
        cash_ratio = st["cash"] / equity if equity > 0 else 0.0
        idle = day_index - last_buy
        if cash_ratio >= 0.3 and idle >= 5:
            flags.append(f"cash: 现金占比 {cash_ratio:.0%} 已闲置 {idle} 个交易日——请给出这笔现金的部署计划"
                         "（在等什么触发条件、计划加多少），或说明为何当前必须保留")

    ctx = build_ctx(day, st, days, news_delta, prev_coast, flags)

    # ---- WorkBuddy as the gate LLM (llm_impact) via policy map ----
    import webapp.engine.hybrid_agent as ha
    from webapp.engine.news_gate import filter_material

    def fake_llm_impact(llm, ticker, sim_date, items, band):
        policy = GATE_POLICY.get(sim_date)
        if policy:
            return policy
        return ("low", "无预置评估，按低影响处理")

    ha.llm_impact = fake_llm_impact

    # ---- decide escalate? (real HybridDecisionAgent.decide) ----
    graph_agent = GraphDecisionAgent(settings)
    pending = {"resp": None}

    def fake_propagate(graph, ctx2, skills_text="", account_state=""):
        resp = pending["resp"]
        if resp is None:
            raise RuntimeError("NEEDS_PIPELINE_OUTPUT")
        final_state = {
            "market_report": resp.get("market_report", ""),
            "sentiment_report": resp.get("sentiment_report", ""),
            "news_report": resp.get("news_report", ""),
            "fundamentals_report": resp.get("fundamentals_report", ""),
            "investment_debate_state": {
                "bull_history": resp.get("debate", {}).get("bull", ""),
                "bear_history": resp.get("debate", {}).get("bear", ""),
                "judge_decision": resp.get("debate", {}).get("judge", ""),
            },
            "investment_plan": resp.get("investment_plan", ""),
            "trader_investment_plan": resp.get("trader_investment_plan", ""),
            "risk_debate_state": {
                "aggressive_history": resp.get("risk", {}).get("aggressive", ""),
                "conservative_history": resp.get("risk", {}).get("conservative", ""),
                "neutral_history": resp.get("risk", {}).get("neutral", ""),
                "judge_decision": resp.get("final_trade_decision", ""),
                "history": resp.get("risk", {}).get("history", ""),
            },
            "final_trade_decision": resp.get("final_trade_decision", ""),
        }
        return final_state, _signal_of(final_state["final_trade_decision"])

    # Pre-set _graph so _ensure_graph() short-circuits (its `if self._graph is
    # not None` guard) and never instantiates the real TradingAgentsGraph
    # (which needs an API key). The patched _propagate_with_retry ignores it.
    graph_agent._graph = object()
    graph_agent._propagate_with_retry = fake_propagate
    hybrid = ha.HybridDecisionAgent(settings, graph=graph_agent)

    # Pre-check whether this day escalates, to know if we need WorkBuddy's output
    delta = list(ctx.news_delta or [])
    coast = ctx.coast or {}
    close = _last_close(ctx)
    band = (coast.get("lower"), coast.get("upper")) if coast else (None, None)
    if not ctx.coast or not ctx.prev_decision:
        escalate, reason = True, "会话首个决策日（无在持判断），直接运行完整流水线建立基准"
    else:
        from webapp.engine.news_gate import rule_hits
        hits = rule_hits(delta)
        if hits:
            labels = sorted({label for label, _ in hits})
            sample = hits[0][1].get("title", "")[:40]
            escalate, reason = True, f"命中重大消息规则 [{'/'.join(labels)}]：{sample}"
        else:
            tech = ha._technical_breakdown(ctx)
            if tech:
                escalate, reason = True, f"技术面恶化：{tech}"
            else:
                improve = ha._technical_improvement(ctx)
                if improve:
                    escalate, reason = True, f"技术面企稳/反转：{improve}"
                else:
                    material = filter_material(delta)
                    if material:
                        level, why = fake_llm_impact(None, ctx.symbol, ctx.sim_date, material, band)
                        if level in ("high", "medium"):
                            escalate, reason = True, f"消息影响评估={level}：{why}"
                        else:
                            escalate, reason = False, f"消息影响评估={level}，维持原判断：{why}"
                    else:
                        lower, upper = band
                        if lower and upper and close is not None and not (lower <= close <= upper):
                            escalate, reason = True, f"无新消息，但收盘 {close:g} 已离开决策区间 [{lower:g}, {upper:g}]，重新评估"
                        else:
                            escalate, reason = False, "无新增重大消息且价格仍在区间内，仅有效期届满，维持原判断"
        if not escalate and st["standpat_streak"] + 1 >= STANDPAT_CAP:
            escalate, reason = True, (f"已连续 {st['standpat_streak']} 日维持原判断，强制全面复核"
                                      f"（stand-pat 上限 {STANDPAT_CAP} 日，防止躺平）")

    if escalate:
        # Need WorkBuddy's pipeline output
        if RESP_PATH.exists():
            resp = json.loads(RESP_PATH.read_text(encoding="utf-8"))
            if resp.get("date") == day["date"]:
                pending["resp"] = resp
        if pending["resp"] is None:
            write_request(day, ctx, reason, recheck_reasons, st)
            return None  # pause
    # else: stand-pat path needs no LLM

    decision, audit, response_text, dec_flags = hybrid.decide(ctx)
    flags.extend(dec_flags)
    st["standpat_streak"] = 0 if escalate else st["standpat_streak"] + 1

    # ---- execution ----
    em = make_exec_model()
    portfolio = Portfolio(cash=st["cash"], shares=st["shares"], avg_cost=st["avg_cost"])
    close_t = day["close"]
    fill = execute(decision, portfolio, em, close_t)
    if fill.action == "rejected" or fill.reason.startswith("sell ignored"):
        flags.append(f"execution: {fill.reason}")
    if fill.action == "buy":
        st["last_buy_index"] = day_index

    equity = portfolio.equity(close_t)
    st["cash"], st["shares"], st["avg_cost"] = portfolio.cash, portfolio.shares, portfolio.avg_cost
    st["records"].append({
        "day": day_index, "date": day["date"], "action": fill.action,
        "requested_pct": fill.requested_pct if fill.action in ("buy", "sell", "rejected") else 0.0,
        "shares": fill.shares, "price": fill.price, "fee": fill.fee,
        "cash": round(portfolio.cash, 2), "shares_after": round(portfolio.shares, 4),
        "equity": round(equity, 2), "llm_calls": 1 if escalate else 0,
        "flags": flags, "decision_json": decision.model_dump_json(),
        "reasoning": decision.reasoning[:400],
    })
    st["day_index"] = day_index + 1
    return make_coast(decision, close_t, day_index, day["date"])


def write_request(day: dict, ctx: DailyContext, reason: str, recheck_reasons: list[str], st: dict):
    from webapp.engine.graph_agent import _account_state
    req = {
        "date": day["date"], "close": day["close"],
        "gate_reason": reason, "recheck_reasons": recheck_reasons,
        "indicators": day["ind"],
        "tail_25": day["tail"],
        "account_state_for_pm": _account_state(ctx),
        "portfolio": ctx.portfolio,
        "recent_days": ctx.recent_days,
        "news_delta": ctx.news_delta,
        "skills_injected": ctx.skills,
        "data_flags": ctx.data_flags,
        "coast": st["coast"],
    }
    REQ_PATH.write_text(json.dumps(req, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------
def main() -> int:
    days = load_ctx()
    st = load_state()
    em = make_exec_model()

    # day 0: forced full buy (engine policy)
    if st["day_index"] == 0 and not st["records"]:
        d0 = days[0]
        portfolio = Portfolio(cash=st["cash"], shares=0.0, avg_cost=0.0)
        dec = Decision(action="buy", position_pct=1.0, confidence=1.0,
                       reasoning="策略规则：首个交易日强制满仓建仓（引擎执行，非模型决策）",
                       key_signals=["policy: initial full position"], used_skills=[], recheck_days=1)
        close_t = d0["close"]
        fill = execute(dec, portfolio, em, close_t)
        equity = portfolio.equity(close_t)
        st.update(cash=portfolio.cash, shares=portfolio.shares, avg_cost=portfolio.avg_cost,
                  last_buy_index=0)
        st["records"].append({"day": 0, "date": d0["date"], "action": "buy",
                              "requested_pct": 1.0, "shares": fill.shares, "price": fill.price,
                              "fee": fill.fee, "cash": round(portfolio.cash, 2),
                              "shares_after": round(portfolio.shares, 4), "equity": round(equity, 2),
                              "llm_calls": 0, "flags": [f"policy: 首日强制满仓建仓 @ {close_t}"],
                              "reasoning": "首日强制满仓（引擎规则）"})
        st["day_index"] = 1
        save_state(st)
        print(f"[day0] forced buy {fill.shares:.1f} shares @ {fill.price}, fee {fill.fee:.2f}")

    while st["day_index"] < len(days):
        i = st["day_index"]
        day = days[i]
        portfolio = Portfolio(cash=st["cash"], shares=st["shares"], avg_cost=st["avg_cost"])
        close_t = day["close"]

        # 1) hard risk stops
        if check_risk_stops(st, i, day["date"], close_t, portfolio, em):
            st["coast"] = None
            st["day_index"] = i + 1
            save_state(st)
            print(f"[day{i}] {day['date']} risk-stop executed")
            continue

        # 2) coast checks
        recheck_reasons: list[str] = []
        fresh_news: list[dict] = []
        prev_coast = st["coast"]
        if st["coast"] is not None:
            coast = st["coast"]
            in_band = coast["lower"] <= close_t <= coast["upper"]
            items = material_items(day["date"])
            prev_sig = {tuple(x) for x in coast["news_sig"]}
            fresh = [items[k] for k in sorted(set(items) - prev_sig, key=str)]
            fresh_news = fresh
            tech_break = tech_improve = None
            if i < coast["until_index"] and in_band and not fresh:
                tech_break = day["tech_break"]
                tech_improve = day["tech_improve"]
            if (i < coast["until_index"] and in_band and not fresh
                    and not tech_break and not tech_improve):
                equity = portfolio.equity(close_t)
                st["records"].append({
                    "day": i, "date": day["date"], "action": "coast", "requested_pct": 0.0,
                    "shares": None, "price": None, "fee": 0.0,
                    "cash": round(portfolio.cash, 2), "shares_after": round(portfolio.shares, 4),
                    "equity": round(equity, 2), "llm_calls": 0,
                    "flags": [f"coast: 沿用 {coast['origin_date']} 的决策，有效区间 "
                              f"{coast['lower']:.2f}~{coast['upper']:.2f}，未触发重新决策"],
                    "reasoning": ""})
                st["day_index"] = i + 1
                save_state(st)
                continue
            if fresh:
                titles = "；".join(str(d.get("title", ""))[:40] for d in fresh[:3])
                recheck_reasons.append(f"re-decide: 消息面新增 {len(fresh)} 条重大信息"
                                       f"（{coast['origin_date']} 的决策作废）：{titles}")
            elif not in_band:
                recheck_reasons.append(f"re-decide: 收盘 {close_t} 突破区间 "
                                       f"[{coast['lower']}, {coast['upper']}]")
            elif tech_break:
                recheck_reasons.append(f"re-decide: 技术面恶化（{tech_break}）")
            elif tech_improve:
                recheck_reasons.append(f"re-decide: 技术面企稳/反转（{tech_improve}）")
            st["coast"] = None

        # 3) decision day
        new_coast = run_decision_day(day, st, days, recheck_reasons, fresh_news, prev_coast)
        if new_coast is None:
            save_state(st)
            req = json.loads(REQ_PATH.read_text(encoding="utf-8"))
            print(f"NEEDS_PIPELINE_OUTPUT day{i} {day['date']} close={day['close']} "
                  f"gate={req['gate_reason']}")
            return 42
        st["coast"] = new_coast
        save_state(st)
        last = st["records"][-1]
        print(f"[day{i}] {day['date']} {last['action']} pct={last['requested_pct']} "
              f"equity={last['equity']}")

    # ---- finalize ----
    eqs = [r["equity"] for r in st["records"] if r["equity"]]
    peak, max_dd = eqs[0], 0.0
    for e in eqs:
        peak = max(peak, e)
        if peak > 0:
            max_dd = min(max_dd, (e / peak - 1) * 100)
    final = eqs[-1]
    print("\n=== LEDGER ===")
    for r in st["records"]:
        print(f"{r['day']:>2} {r['date']} {r['action']:<6} pct={r['requested_pct']!s:<6} "
              f"sh={r['shares']} px={r['price']} fee={r['fee']} cash={r['cash']:.0f} "
              f"shares={r['shares_after']} eq={r['equity']:.0f} llm={r['llm_calls']}")
    print(f"\nfinal_equity={final:,.0f} return={(final/CAPITAL-1)*100:+.2f}% "
          f"max_dd={max_dd:.2f}% llm_days={sum(1 for r in st['records'] if r['llm_calls'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
