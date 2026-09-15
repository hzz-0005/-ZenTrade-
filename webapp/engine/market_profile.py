"""Market-specific interpretation layered over shared financial/technical data.

Indicator mathematics remain market-agnostic.  This module supplies the
interpretation that must not be shared blindly across US equities and A-shares:
board microstructure, earnings stage, and whether PE is meaningful at all.
"""
from __future__ import annotations

from typing import Any


def _number(values: dict[str, Any] | None, *keys: str) -> float | None:
    for key in keys:
        value = (values or {}).get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def cn_board_name(symbol: str) -> str:
    """Return the normal board regime from an A-share numeric ticker."""
    code = str(symbol).split(".")[0].zfill(6)
    if code.startswith(("300", "301")):
        return "创业板（常规20%涨跌幅）"
    if code.startswith(("688", "689")):
        return "科创板（常规20%涨跌幅）"
    if code.startswith(("4", "8", "92")):
        return "北交所（常规30%涨跌幅）"
    return "沪深主板（常规10%涨跌幅）"


def _earnings_stage(fundamentals: dict[str, Any]) -> tuple[str, str]:
    eps = _number(fundamentals, "摊薄每股收益(元)")
    prior_eps = _number(fundamentals, "上年同期摊薄每股收益(元)")
    adjusted_eps = _number(
        fundamentals,
        "扣除非经常性损益后的每股收益(元)",
        "扣非每股收益(元)",
    )
    prior_adjusted = _number(fundamentals, "上年同期扣非每股收益(元)")
    operating_cash = _number(
        fundamentals,
        "每股经营性现金流(元)",
        "每股经营活动产生的现金流量净额(元)",
    )

    if eps is None:
        return "盈利数据不足", "PE可靠性未知：不得把缺失数据解释为便宜或昂贵"
    if eps <= 0:
        return (
            "仍在亏损（PE不适用）",
            "禁用PE：改看亏损收窄、营收、毛利率、现金消耗与资产负债表",
        )

    if prior_eps is None or adjusted_eps is None:
        return (
            "账面盈利（缺少同比或扣非数据）",
            "PE仅作弱参考：盈利质量和可比基准不足，不得据此确认便宜或昂贵",
        )

    is_turnaround = prior_eps is not None and prior_eps <= 0
    if adjusted_eps is not None and adjusted_eps <= 0:
        label = "账面扭亏" if is_turnaround else "账面盈利"
        return (
            f"{label}（扣非未转正，质量待验证）",
            "禁用绝对PE：微利分母会放大倍数，改看扣非、现金流、营收与利润率",
        )

    adjusted_turnaround = (
        adjusted_eps is not None
        and adjusted_eps > 0
        and prior_adjusted is not None
        and prior_adjusted <= 0
    )
    if is_turnaround or adjusted_turnaround:
        if operating_cash is not None and operating_cash <= 0:
            stage = "利润扭亏（经营现金流未转正，质量待验证）"
        else:
            stage = "经营性扭亏（催化剂，仍需后续报告期确认）"
        return (
            stage,
            "禁用绝对PE：扭亏初期分母不稳定，优先看扣非、现金流和后续持续性",
        )

    if operating_cash is not None and operating_cash < 0:
        return (
            "盈利但经营现金流为负（质量待验证）",
            "PE仅作弱参考：先核验利润含金量，再做行业和自身历史比较",
        )

    return (
        "稳定盈利",
        "仅作行业/公司自身历史的相对比较；禁止套用海外固定倍数阈值",
    )


def enrich_fundamentals(
    symbol: str,
    market: str,
    fundamentals: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Add A-share interpretation fields without mutating vendor data."""
    if fundamentals is None:
        return None
    result = dict(fundamentals)
    if market != "cn":
        return result
    stage, pe_policy = _earnings_stage(result)
    result["A股板块"] = cn_board_name(symbol)
    result["盈利阶段"] = stage
    result["PE适用规则"] = pe_policy
    return result


def market_guidance(
    symbol: str,
    market: str,
    fundamentals: dict[str, Any] | None,
) -> str:
    """Render one shared CN rule block for every decision architecture."""
    if market != "cn":
        return ""
    enriched = enrich_fundamentals(symbol, market, fundamentals or {}) or {}
    return "\n".join(
        [
            "## A股市场适配规则",
            f"- 板块制度: {enriched.get('A股板块', cn_board_name(symbol))}",
            f"- 盈利阶段: {enriched.get('盈利阶段', '盈利数据不足')}",
            f"- 估值口径: {enriched.get('PE适用规则', '不得使用固定PE阈值')}",
            "- 仓位规则: 高PE只能降低置信度或目标仓位，不能单独否决买入；"
            "盈利拐点可先建立小仓位，再由后续报告验证。",
            "- 择时规则: MA/MACD/RSI等公式继续共用，但按所属板块波动和涨跌幅制度解释；"
            "一个可观察的企稳信号即可建立试探仓，不得无限叠加确认条件。",
        ]
    )
