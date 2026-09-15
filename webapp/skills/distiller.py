"""End-of-session review -> categorized skills (the reflection loop).

Two LLM calls per session: one review (full trade log -> summary /
reflection / candidate skills), one dedupe pass against the existing
library. The session's date range stamps created_at so the temporal
clamp in SkillLibrary.select_for_day stays meaningful: a skill from a
session covering days after T cannot leak into a run at T.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from webapp.config import WebappSettings
from webapp.engine.decision_agent import _extract_text
from webapp.skills.library import SkillLibrary
from webapp.store import db

logger = logging.getLogger(__name__)


class CandidateSkill(BaseModel):
    category: Literal[
        "trend", "mean_reversion", "risk_control", "position_sizing",
        "sentiment", "timing", "regime", "execution",
    ]
    statement: str = Field(min_length=1, max_length=240)
    evidence: str = Field(default="", max_length=240)


def _operation_profile(session: dict, days: list, equity_days: list) -> str:
    """Quantified self-audit material for the review prompt.

    A trade log alone produced vague lessons ("注意风险"). With behavior
    stats — sell chunk sizes, re-entry count, response to crash days, gap vs
    buy&hold — the review can name concrete bad habits, which is the whole
    point of the reflection loop.
    """
    decisions: list[tuple[str, str, float | None]] = []
    for r in days:
        try:
            d = json.loads(r["decision_json"] or "{}")
        except Exception:
            d = {}
        decisions.append((r["date"], r["action"], d.get("position_pct")))

    sells = [p for _, a, p in decisions if a == "sell" and p]
    buys = [p for _, a, p in decisions if a == "buy" and p]
    first_sell_idx = next((i for i, (_, a, _) in enumerate(decisions) if a == "sell"), None)
    rebuys = sum(
        1 for i, (_, a, _) in enumerate(decisions)
        if a == "buy" and first_sell_idx is not None and i > first_sell_idx
    )

    lines = [
        f"- 交易日 {len(days)} 天：买入 {len(buys)} 次、卖出 {len(sells)} 次，"
        f"首次卖出后重新买入 {rebuys} 次"
    ]
    if sells:
        avg_sell = sum(sells) / len(sells)
        verdict = "碎步减仓模式——调仓不果断，风险敞口降不下去" if len(sells) >= 4 and avg_sell < 0.15 else "节奏正常"
        lines.append(f"- 卖出平均幅度 {avg_sell * 100:.1f}%/次（{verdict}）")

    cash_ratios = []
    for r in equity_days:
        try:
            if r["equity"] and r["cash_after"] is not None:
                cash_ratios.append(r["cash_after"] / r["equity"])
        except (KeyError, IndexError, TypeError):
            continue
    if cash_ratios:
        avg_cash = sum(cash_ratios) / len(cash_ratios)
        if avg_cash >= 0.3:
            lines.append(
                f"- 现金使用：日均现金占比 {avg_cash * 100:.0f}%——大额现金长期闲置，"
                "缺少部署计划（是等待触发的弹药，还是被遗忘？加仓决心与动作是否匹配？）"
            )

    eq = [(r["date"], float(r["equity"])) for r in equity_days]
    if len(eq) >= 3:
        def _worst(span: int):
            best = None
            for i in range(span, len(eq)):
                base = eq[i - span][1]
                if base:
                    ret = eq[i][1] / base - 1
                    if best is None or ret < best[1]:
                        best = (eq[i][0], ret)
            return best

        w1 = _worst(1)
        if w1 and w1[1] < -0.05:
            act = next((a for d, a, _ in decisions if d == w1[0]), "?")
            lines.append(f"- 最大单日跌幅 {w1[1] * 100:.1f}%（{w1[0]}），当日动作：{act}")
        w2 = _worst(2)
        if w2 and w2[1] < -0.10:
            act = next((a for d, a, _ in decisions if d == w2[0]), "?")
            nxt = next((a for d, a, _ in decisions if d > w2[0]), "?")
            lines.append(
                f"- 最大两日累计跌幅 {w2[1] * 100:.1f}%（截至 {w2[0]}），当日动作：{act}、次日：{nxt}"
                "——应急离场手法用上了吗？"
            )
        peak_val, peak_date, worst_dd, dd_peak, dd_trough = eq[0][1], eq[0][0], 0.0, eq[0][0], eq[0][0]
        for d, v in eq:
            if v > peak_val:
                peak_val, peak_date = v, d
            if peak_val > 0 and v / peak_val - 1 < worst_dd:
                worst_dd, dd_peak, dd_trough = v / peak_val - 1, peak_date, d
        if worst_dd < -0.05:
            lines.append(f"- 最大回撤 {worst_dd * 100:.1f}%（{dd_peak} → {dd_trough}）")

    try:
        # 买入持有基准必须锚定回测窗口的起始日，而不是 price_frames 表里
        # 历史最早一条。数据网关为了给指标预留预热窗口，会把 start_date 之前
        # 若干个月的历史 K 线也写入 price_frames；若直接用表首行，基准起点会被
        # 错误地拉到数月之前，导致「买入持有」收益率被严重高估（例如 600396 曾
        # 因起点错配到 3.04 而算出 +363.8% 的伪基准）。改用 sessions.start_date
        # 对齐到回测窗口第一天收盘价。
        start_date = session["start_date"]
        row_first = db.query_one(
            "SELECT close FROM price_frames WHERE session_id=? AND date>=? ORDER BY date LIMIT 1",
            (session["id"], start_date),
        )
        row_last = db.query_one(
            "SELECT close FROM price_frames WHERE session_id=? ORDER BY date DESC LIMIT 1",
            (session["id"],),
        )
        if row_first and row_last and row_first["close"]:
            bh = (row_last["close"] / row_first["close"] - 1) * 100
            strat = session["total_return_pct"] or 0.0
            verdict = "跑赢基准" if strat >= bh else "跑输基准——持有不动都比这套操作强，必须自省"
            lines.append(f"- 同期买入持有 {bh:+.1f}% vs 策略 {strat:+.1f}%（{verdict}）")
    except Exception:
        pass
    return "\n".join(lines)


class ReviewOutput(BaseModel):
    summary: str = ""
    reflection: str = ""
    trade_reviews: list[dict] = Field(default_factory=list)
    candidate_skills: list[CandidateSkill] = Field(default_factory=list)


class Distiller:
    def __init__(self, settings: WebappSettings, library: SkillLibrary):
        self.settings = settings
        self.library = library

    async def review_and_distill(self, session_id: str) -> None:
        session = db.query_one("SELECT * FROM sessions WHERE id=?", (session_id,))
        if session is None:
            return
        # A review failure must not fail the session: degrade to a placeholder
        # review so the ledger and metrics stay available in the UI.
        try:
            review = await self.review_session(session_id)
            skills_created = await self.distill(review, session)
        except Exception as exc:
            logger.warning("review/distill failed for %s: %s", session_id, exc)
            db.execute(
                "INSERT OR REPLACE INTO session_reviews "
                "(session_id, summary, reflection, trade_reviews_json, metrics_json, skills_created_json, created_at) "
                "VALUES (?,?,?,?,?,?,datetime('now'))",
                (
                    session_id,
                    "复盘调用失败（LLM 超时/异常），账务与指标不受影响。",
                    f"复盘异常: {type(exc).__name__}",
                    "[]",
                    json.dumps({
                        "total_return_pct": session["total_return_pct"],
                        "max_drawdown_pct": session["max_drawdown_pct"],
                        "win_rate": session["win_rate"],
                        "final_equity": session["final_equity"],
                    }, ensure_ascii=False),
                    "[]",
                ),
            )
            return

        db.execute(
            "INSERT OR REPLACE INTO session_reviews "
            "(session_id, summary, reflection, trade_reviews_json, metrics_json, skills_created_json, created_at) "
            "VALUES (?,?,?,?,?,?,datetime('now'))",
            (
                session_id,
                review.summary,
                review.reflection,
                json.dumps(review.trade_reviews, ensure_ascii=False),
                json.dumps({
                    "total_return_pct": session["total_return_pct"],
                    "max_drawdown_pct": session["max_drawdown_pct"],
                    "win_rate": session["win_rate"],
                    "final_equity": session["final_equity"],
                }, ensure_ascii=False),
                json.dumps(skills_created, ensure_ascii=False),
            ),
        )

    async def review_session(self, session_id: str) -> ReviewOutput:
        from webapp.prompts import loader

        session = db.query_one("SELECT * FROM sessions WHERE id=?", (session_id,))
        days = db.query(
            "SELECT date, action, decision_json, executed_shares, executed_price, fee, equity, cash_after, data_flags "
            "FROM daily_records WHERE session_id=? ORDER BY day_index",
            (session_id,),
        )
        trades = db.query(
            "SELECT * FROM trades WHERE session_id=? ORDER BY id", (session_id,)
        )

        # cap the prompt: at most 40 day lines and 15 trades
        day_lines = "\n".join(
            f"- {r['date']}: {r['action']}"
            + (f" {r['executed_shares']}股@{r['executed_price']} 费{r['fee']}" if r["action"] in ("buy", "sell") else "")
            + f" | 权益 {r['equity']}"
            for r in days[:40]
        )
        trade_lines = "\n".join(
            f"- #{t['id']} {t['entry_date']}→{t['exit_date'] or '持仓中'} "
            f"{t['shares']}股 @{t['entry_price']}→{t['exit_price'] or '-'} "
            f"盈亏 {t['realized_pnl']} ({t['return_pct']}%)"
            for t in trades[:15]
        ) or "- （无平仓交易）"
        metrics = (
            f"总收益 {session['total_return_pct']}%，最大回撤 {session['max_drawdown_pct']}%，"
            f"胜率 {session['win_rate'] if session['win_rate'] is not None else '无平仓'}%"
        )
        profile = _operation_profile(session, days, days)

        user_text = (
            f"标的 {session['canonical_ticker']}（{session['market']}），"
            f"区间 {session['start_date']} ~ {session['end_date']}，初始资金 {session['initial_capital']}。\n"
            f"指标：{metrics}\n\n## 操作画像（自我审视素材）\n{profile}\n"
            f"\n## 逐日记录\n{day_lines}\n\n## 交易（round-trips）\n{trade_lines}\n"
        )

        llm = _get_llm(self.settings, timeout=240)  # review prompt is long; think low but read slow
        system_text = loader.review_system()
        response = await asyncio.to_thread(
            llm.invoke, [("system", system_text), ("human", user_text)]
        )
        text = _extract_text(response)
        return _parse_review(text)

    async def distill(self, review: ReviewOutput, session: dict) -> list[dict]:
        candidates = review.candidate_skills[:6]
        if not candidates:
            return []
        from webapp.prompts import loader

        created: list[dict] = []
        # One cross-category dedupe batch. The previous per-category loop could
        # add up to six extra LLM calls despite the module promising two calls.
        existing: list[dict] = []
        for category in dict.fromkeys(c.category for c in candidates):
            existing.extend(
                self.library.list_skills(category=category, enabled=True)[:10]
            )
        verdicts = (
            await asyncio.to_thread(self._dedupe_call, candidates, existing)
            if existing else
            [{"index": i, "verdict": "new", "reason": "no existing skills"}
             for i in range(len(candidates))]
        )
        effective_date = (
            date.fromisoformat(session["end_date"]) + timedelta(days=1)
        ).isoformat()

        for c, v in zip(candidates, verdicts):
            verdict = str(v.get("verdict", "new"))
            if verdict.startswith("duplicate:"):
                continue
            if verdict.startswith("merge:"):
                target = verdict.split(":", 1)[1]
                self.library.apply_evidence(target, True, {
                    "merged_from": session["id"],
                    "evidence": c.evidence,
                })
                continue
            skill = self.library.create(
                category=c.category,
                statement=c.statement,
                source_session=session["id"],
                source_ticker=session["canonical_ticker"],
                source_market=session["market"],
                performance_evidence={"evidence": c.evidence, "session_return_pct": session["total_return_pct"]},
                effective_date=effective_date,
            )
            if skill:
                created.append(skill)
        return created

    def _dedupe_call(self, candidates: list[CandidateSkill], existing: list[dict]) -> list[dict]:
        from webapp.prompts import loader

        cand_lines = "\n".join(f"{i}. [{c.category}] {c.statement}" for i, c in enumerate(candidates))
        exist_lines = "\n".join(f"- id={s['id']} [{s['category']}] {s['statement']}" for s in existing)
        user_text = f"## 候选经验\n{cand_lines}\n\n## 已有经验（同分类）\n{exist_lines}"
        llm = _get_llm(self.settings)
        try:
            response = llm.invoke([("system", loader.dedupe_system()), ("human", user_text)])
            text = _extract_text(response)
            start, end = text.find("{"), text.rfind("}")
            payload = json.loads(text[start:end + 1])
            verdicts = payload.get("verdicts", [])
            # normalize: guarantee one entry per candidate index
            out = []
            for i in range(len(candidates)):
                match = next((v for v in verdicts if v.get("index") == i), {"index": i, "verdict": "new"})
                out.append(match)
            return out
        except Exception as exc:
            logger.warning("dedupe call failed (%s); rejecting candidates for safety", exc)
            return [
                {"index": i, "verdict": "duplicate:dedupe-unavailable",
                 "reason": "dedupe failed; fail closed"}
                for i in range(len(candidates))
            ]


def _parse_review(text: str) -> ReviewOutput:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return ReviewOutput.model_validate(json.loads(cleaned[start:end + 1]))
        except Exception as exc:
            logger.warning("review JSON invalid: %s", exc)
    return ReviewOutput(summary="复盘输出解析失败", reflection=text[:500])


def _get_llm(settings: WebappSettings, *, timeout: int = 60):
    from webapp.llm import get_llm

    return get_llm(settings, timeout=timeout)
