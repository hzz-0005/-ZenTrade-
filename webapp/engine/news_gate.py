"""Gate: is today's news material enough to justify a full pipeline run?

The full multi-agent graph costs minutes per day. Most days nothing happens,
so instead of running it on a schedule we watch the news feed and escalate on
demand:

  tier 0 (free)   — deterministic keyword rules on announcement / research
                    titles. A 财报/重组/评级调整 hit escalates immediately;
                    no LLM call, no ambiguity.
  tier 1 (cheap)  — one small LLM call that scores the new items' likely
                    effect on the standing price band.
  tier 2 (dear)   — the full analyst -> debate -> trader -> risk pipeline.

Only tier 2 redefines the price band. Everything else keeps the standing
decision and just extends its horizon.
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

# (pattern, label) — ordered; the first hit wins for the reported reason.
MATERIAL_RULES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"财报|年报|半年报|季报|一季报|三季报|中报|业绩预告|业绩快报|"
                r"业绩预增|业绩预减|预亏|预盈|扭亏|营收|净利"), "财报/业绩"),
    (re.compile(r"重组|并购|收购|重大资产|资产注入|借壳|分立|分拆"), "资产重组"),
    (re.compile(r"增发|配股|可转债|定增|募资|再融资"), "再融资"),
    (re.compile(r"减持|增持|回购|股权激励|解禁|质押|冻结|举牌"), "股东行为"),
    (re.compile(r"停牌|复牌|退市|立案|处罚|诉讼|仲裁|违规|问询函|关注函|监管"), "监管/风险"),
    (re.compile(r"中标|重大合同|订单|战略合作|新产品|获批|专利|投产|扩产"), "经营动态"),
    (re.compile(r"评级|目标价|上调|下调|首次覆盖"), "券商评级"),
    (re.compile(r"分红|派息|送股|转增"), "分红送配"),
    # --- English titles (US/HK headlines and broker actions) ---
    (re.compile(r"earnings|revenue|guidance|profit|loss\b|beat\b|miss(es)?\b", re.I), "财报/业绩"),
    (re.compile(r"merger|acquisition|acquire|takeover|spin-?off", re.I), "资产重组"),
    (re.compile(r"buyback|repurchase|dividend|equity offering", re.I), "股东行为"),
    (re.compile(r"\bFDA\b|approval|lawsuit|antitrust|probe|investigation|subpoena|recall", re.I),
     "监管/风险"),
    # NB: deliberately no `overweight`/`underweight` here — those words appear
    # in every broker action's from/to grade field and would make routine
    # reaffirmations escalate. Analyst actions carry a Chinese label instead
    # (上调评级 / 下调评级 / 首次覆盖 / 维持 / 重申), which the rules above match.
    (re.compile(r"upgrade[sd]?|downgrade[sd]?|price target", re.I), "券商评级"),
)

# Only these kinds count as "new news". Macro headlines appear daily and would
# escalate every single day.
_MATERIAL_KINDS = ("notice", "research", "news")

_GATE_SYSTEM = (
    "你是一名消息面影响评估员，服务于量化交易回测。\n"
    "给定若干条新出现的消息，判断它们是否可能显著改变该股票未来数个交易日的价格运行区间。\n"
    "评级标准：\n"
    "- high：业绩预告/财报/重组/监管处罚/大幅调整评级这类能改变估值或趋势判断的重大信息\n"
    "- medium：可能影响短期情绪或成交，但不足以改变中期判断\n"
    "- low：例行公告、程序性事项、影响有限的常规信息\n"
    "- none：几乎无影响\n"
    "严格只输出一个 JSON 对象：{\"impact\": \"high|medium|low|none\", \"reason\": \"不超过50字\"}"
)


def filter_material(items: list[dict]) -> list[dict]:
    return [i for i in items if i.get("kind") in _MATERIAL_KINDS]


def rule_hits(items: list[dict]) -> list[tuple[str, dict]]:
    """Deterministic escalation: (label, item) for every title matching a rule."""
    hits: list[tuple[str, dict]] = []
    for item in filter_material(items):
        title = str(item.get("title", ""))
        for pattern, label in MATERIAL_RULES:
            if pattern.search(title):
                hits.append((label, item))
                break
    return hits


def _format_items(items: list[dict]) -> str:
    return "\n".join(
        f"- [{i.get('kind', '?')}][{i.get('date', '')}] {i.get('title', '')}"
        for i in items[:10]
    )


def llm_impact(llm, ticker: str, sim_date: str, items: list[dict],
               band: tuple[float, float] | None) -> tuple[str, str]:
    """Cheap single-call impact score. Returns (impact_level, reason)."""
    band_text = (
        f"当前生效的价格区间 [{band[0]:g}, {band[1]:g}]" if band else "当前无有效价格区间"
    )
    user = (
        f"交易日 {sim_date}，标的 {ticker}。{band_text}\n"
        f"新出现的消息：\n{_format_items(items)}\n\n"
        "请评估这些消息对该股未来数日价格区间的影响等级。"
    )
    try:
        response = llm.invoke([("system", _GATE_SYSTEM), ("human", user)])
        text = getattr(response, "content", response) or ""
        text = str(text).strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            payload = json.loads(text[start:end + 1])
            level = str(payload.get("impact", "low")).strip().lower()
            if level not in ("high", "medium", "low", "none"):
                level = "low"
            return level, str(payload.get("reason", ""))[:80]
        logger.debug("news gate returned no JSON: %s", text[:120])
    except Exception as exc:
        logger.warning("news gate LLM failed (%s); falling back to 'low'", exc)
    return "low", "评估调用失败，按低影响处理"


def band_breach_ratio(close: float | None, lower: float | None,
                      upper: float | None) -> float:
    """How far outside the band the close sits, in units of band width.

    0 means inside. 1.0 means the close is one full band-width beyond an edge.
    Used to decide whether a price break alone (without news) deserves a fresh
    full analysis.
    """
    if close is None or not lower or not upper or upper <= lower:
        return 0.0
    width = upper - lower
    if close < lower:
        return (lower - close) / width
    if close > upper:
        return (close - upper) / width
    return 0.0


def _wilder_rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder's RSI for the last close in `closes` (matches stockstats default).

    Used to derive the *previous* day's RSI so the oversold check can be a
    transition condition (crossed *into* oversold) rather than a level one
    (still oversold) — the latter re-triggers the full pipeline every day of a
    protracted oversold downtrend, exactly like the pre-fix "跌破 20 日线".
    """
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def detect_technical_breakdown(close: float | None, indicators: dict | None,
                               closes: list[float] | None) -> str | None:
    """Detect a technical breakdown (oversold flush / trend break).

    Returns a human-readable reason string, or None when nothing is broken.
    This is independent of news and of whether the close left the coast band:
    a sharp oversold flush or a trend break means the standing thesis may be
    invalid even while the price is still nominally inside its band — so it
    should force a fresh re-evaluation.

    The 20-day-MA / Bollinger checks are *transition* conditions, not level
    conditions: a name that merely *stays* below its 20-day line must not
    re-trigger a full pipeline every day (the standing decision already
    reflects the downtrend). Only a fresh cross below the line — prev close
    above it, today below — is a new signal. (Observed: 600396 in a downtrend
    re-ran the full 9-node pipeline daily on "跌破 20 日线" alone, defeating
    the coast.)
    """
    reasons: list[str] = []
    ind = indicators or {}
    closes = [c for c in (closes or []) if c]  # drop None/zero guards

    if len(closes) >= 2 and closes[-2]:
        d1 = closes[-1] / closes[-2] - 1
        if d1 <= -0.05:
            reasons.append(f"单日跌 {d1:.1%}")
    if len(closes) >= 3 and closes[-3]:
        d2 = closes[-1] / closes[-3] - 1
        if d2 <= -0.08:
            reasons.append(f"两日累计跌 {d2:.1%}")

    rsi = ind.get("rsi")
    if rsi is not None and rsi < 30:
        # Transition, not level: only a fresh cross *into* oversold (prev day
        # still >= 30, today < 30) is a new signal. A name that merely stays
        # oversold through a protracted downtrend must coast, not re-run the
        # full 9-node pipeline daily (observed: 600396 in its July slide sat
        # below RSI 30 for days and re-decided every day on "RSI 超卖" alone).
        prev_rsi = _wilder_rsi(closes[:-1]) if len(closes) >= 15 else None
        if prev_rsi is None or prev_rsi >= 30:
            reasons.append(f"RSI={rsi:.0f} 超卖")

    # Prev-day reference for the two trend-break checks below. When history is
    # too short to compute it we keep the old level check as a conservative
    # fallback (better to over-escalate than silently miss a break).
    prev_close = closes[-2] if len(closes) >= 2 else None
    prev_sma20 = prev_boll_lb = None
    if len(closes) >= 21:
        window = closes[-21:-1]  # the 20 closes ending at the previous day
        prev_sma20 = sum(window) / len(window)
        variance = sum((c - prev_sma20) ** 2 for c in window) / len(window)
        prev_boll_lb = prev_sma20 - 2 * (variance ** 0.5)

    sma20 = ind.get("close_20_sma")
    if sma20 and close is not None and close < sma20:
        if prev_close is None or prev_sma20 is None or prev_close >= prev_sma20:
            reasons.append(f"跌破 20 日线（{close:g} < {sma20:g}）")

    boll_lb = ind.get("boll_lb")
    if boll_lb and close is not None and close < boll_lb:
        if prev_close is None or prev_boll_lb is None or prev_close >= prev_boll_lb:
            reasons.append(f"跌破布林下轨（{boll_lb:g}）")

    return "；".join(reasons) if reasons else None


def detect_technical_improvement(close: float | None, indicators: dict | None,
                                 closes: list[float] | None) -> str | None:
    """Detect a technical improvement (sharp reversal / stabilization).

    The mirror image of :func:`detect_technical_breakdown`. A standing
    "hold / observe" decision taken during a downtrend must not coast straight
    through the reversal: a fresh stabilization signal — a big up day, a cross
    back above the 20-day line, a recovery out of oversold — should wake the
    pipeline for a re-evaluation. Otherwise the coast slips past the buy point
    and the model only re-decides once the price has already run far, then
    refuses to chase. (Observed: 600396's +35% rebound over 07-20~07-22 was
    coasted over because the gate only ever re-escalated on *deterioration*.)

    Every check is a *transition* condition (a fresh cross), never a level one,
    so a name that merely *stays* strong does not re-run the pipeline daily —
    the same discipline as the breakdown detector.
    """
    reasons: list[str] = []
    ind = indicators or {}
    closes = [c for c in (closes or []) if c]  # drop None/zero guards

    # Sharp reversal: single-day and two-day cumulative up moves (mirrors the
    # -5% / -8% flush thresholds in the breakdown detector).
    if len(closes) >= 2 and closes[-2]:
        d1 = closes[-1] / closes[-2] - 1
        if d1 >= 0.05:
            reasons.append(f"单日涨 {d1:.1%}")
    if len(closes) >= 3 and closes[-3]:
        d2 = closes[-1] / closes[-3] - 1
        if d2 >= 0.08:
            reasons.append(f"两日累计涨 {d2:.1%}")

    # Recovery out of oversold: yesterday's RSI < 30, today's >= 30. A fresh
    # cross, not a "still above 30" level check.
    rsi = ind.get("rsi")
    if rsi is not None and rsi >= 30:
        prev_rsi = _wilder_rsi(closes[:-1]) if len(closes) >= 15 else None
        if prev_rsi is not None and prev_rsi < 30:
            reasons.append(f"RSI={rsi:.0f} 脱离超卖")

    # Fresh cross back above the 20-day line: prev close below its 20-day MA,
    # today's close at/above today's 20-day MA. This is the classic right-side
    # confirmation the model says it waits for, so it must not be coasted over.
    prev_close = closes[-2] if len(closes) >= 2 else None
    prev_sma20 = None
    if len(closes) >= 21:
        window = closes[-21:-1]  # the 20 closes ending at the previous day
        prev_sma20 = sum(window) / len(window)
    sma20 = ind.get("close_20_sma")
    if sma20 and close is not None and close >= sma20:
        if prev_close is not None and prev_sma20 is not None and prev_close < prev_sma20:
            reasons.append(f"站回 20 日线（{close:g} ≥ {sma20:g}）")

    return "；".join(reasons) if reasons else None
