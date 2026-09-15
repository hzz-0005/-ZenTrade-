"""A-share (China) data vendor backed by akshare.

akshare wraps domestic endpoints (EastMoney), so A-share data resolves without a
VPN — the exact gap that made yfinance-backed A-share sessions fail with
``YFRateLimitError``. The router (``interface.route_to_vendor``) prepends this
vendor for ``.SS``/``.SZ`` symbols and keeps the configured chain (yfinance, ...)
as fallback, so US/HK and non-A-share behaviour is unchanged.

Only the methods akshare can genuinely serve are implemented. The three-statement
methods (balance sheet / cash flow / income statement) return an honest
"not available via the domestic vendor" note instead of raising: the fundamentals
analyst already gets EPS/ROE/revenue-profit growth/debt-ratio from
``get_fundamentals``, and a hard raise here would abort the whole pipeline for a
statement the decision rarely needs.

All akshare imports are lazy (inside functions) so importing this module never
requires akshare to be installed.
"""
from __future__ import annotations

import logging
import os
from contextlib import suppress
from datetime import datetime

import pandas as pd

from .errors import NoMarketDataError

logger = logging.getLogger(__name__)

# akshare talks to domestic endpoints (EastMoney / Sina / CCTV). When the host
# runs a system proxy (Clash, v2rayN, ...) — which is exactly when yfinance NEEDS
# the proxy to reach Yahoo — ``requests`` inherits that proxy and the proxy
# refuses/stalls domestic connections (ProxyError). Bypass it for domestic hosts
# only, so akshare works whether the proxy is up or down.
_DOMESTIC_SUFFIXES = (
    "eastmoney.com", "sina.com.cn", "cctv.com",
    "10jqka.com.cn", "hexun.com", "eastmoney.com.cn",
)


def _bypass_proxy_for_domestic_hosts() -> None:
    for var in ("NO_PROXY", "no_proxy"):
        parts = [p.strip() for p in os.environ.get(var, "").split(",") if p.strip()]
        for suffix in _DOMESTIC_SUFFIXES:
            if suffix not in parts:
                parts.append(suffix)
        os.environ[var] = ",".join(parts)


_bypass_proxy_for_domestic_hosts()

# Numeric-code prefixes that belong to the Shanghai exchange (incl. STAR 688 and
# funds 51x). Everything else is Shenzhen. The suffix a caller passes is NOT
# trusted — users attach the wrong one often enough.
_SHANGHAI_PREFIXES = ("60", "68", "51")

_OHLCV_RENAME = {
    "日期": "Date", "开盘": "Open", "收盘": "Close",
    "最高": "High", "最低": "Low", "成交量": "Volume",
}

_SUPPORTED_INDICATORS = {
    "close_50_sma", "close_200_sma", "close_10_ema",
    "macd", "macds", "macdh", "rsi", "boll", "boll_ub", "boll_lb",
    "atr", "vwma", "mfi",
}


def a_share_code(ticker: str) -> str:
    """``'600519.SS'`` -> ``'600519'``; ``'000858.SZ'`` -> ``'000858'``."""
    return str(ticker).split(".")[0].strip().zfill(6)


def _exchange_prefix(ticker: str) -> str:
    """``'SH'`` for Shanghai listings, ``'SZ'`` otherwise, from the numeric code."""
    return "SH" if a_share_code(ticker).startswith(_SHANGHAI_PREFIXES) else "SZ"


def _compact_date(d: str) -> str:
    return d.replace("-", "")


def _load_ohlcv(symbol: str, curr_date: str, lookback_days: int = 365 * 5) -> pd.DataFrame:
    """qfq daily OHLCV up to ``curr_date`` (no look-ahead), English columns."""
    import akshare as ak

    code = a_share_code(symbol)
    start = (pd.Timestamp(curr_date) - pd.Timedelta(days=lookback_days)).strftime("%Y%m%d")
    df = ak.stock_zh_a_hist(
        symbol=code, period="daily",
        start_date=start, end_date=_compact_date(curr_date), adjust="qfq",
    )
    if df is None or df.empty:
        raise NoMarketDataError(symbol, detail="akshare returned no rows")
    df = df.rename(columns=_OHLCV_RENAME)
    if "Date" not in df.columns:
        raise NoMarketDataError(symbol, detail="akshare OHLCV missing date column")
    df["Date"] = pd.to_datetime(df["Date"])
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[["Date", "Open", "High", "Low", "Close", "Volume"]].sort_values("Date")
    return df[df["Date"] <= pd.Timestamp(curr_date)]


def get_stock_data(symbol, start_date, end_date):
    """Daily qfq OHLCV for an A-share, both dates inclusive, CSV in/out."""
    import akshare as ak

    code = a_share_code(symbol)
    df = ak.stock_zh_a_hist(
        symbol=code, period="daily",
        start_date=_compact_date(start_date), end_date=_compact_date(end_date),
        adjust="qfq",
    )
    if df is None or df.empty:
        raise NoMarketDataError(symbol, detail="akshare returned no rows")
    df = df.rename(columns=_OHLCV_RENAME)
    if "Date" not in df.columns:
        raise NoMarketDataError(symbol, detail="akshare OHLCV missing date column")
    df["Date"] = pd.to_datetime(df["Date"])
    keep = ["Date", "Open", "High", "Low", "Close", "Volume"]
    df = df[[c for c in keep if c in df.columns]]
    header = f"# Stock data for {symbol} (akshare qfq) from {start_date} to {end_date}\n"
    header += f"# Total records: {len(df)}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + df.to_csv(index=False)


def get_indicators(symbol, indicator, curr_date, look_back_days=30):
    """A single stockstats indicator over akshare OHLCV (same output shape as yfinance)."""
    from stockstats import wrap

    if indicator not in _SUPPORTED_INDICATORS:
        raise ValueError(
            f"Indicator {indicator} is not supported. "
            f"Please choose from: {sorted(_SUPPORTED_INDICATORS)}"
        )

    data = _load_ohlcv(symbol, curr_date)
    wrapped = wrap(data)
    wrapped["Date"] = wrapped["Date"].dt.strftime("%Y-%m-%d")
    wrapped[indicator]  # trigger stockstats to compute the column

    value_by_date = {}
    for _, row in wrapped.iterrows():
        val = row[indicator]
        value_by_date[row["Date"]] = "N/A" if pd.isna(val) else str(val)

    before = pd.Timestamp(curr_date) - pd.Timedelta(days=int(look_back_days))
    day = pd.Timestamp(curr_date)
    lines = []
    while day >= before:
        date_str = day.strftime("%Y-%m-%d")
        value = value_by_date.get(date_str, "N/A: Not a trading day (weekend or holiday)")
        lines.append(f"{date_str}: {value}")
        day -= pd.Timedelta(days=1)

    return (
        f"## {indicator} values from {before.strftime('%Y-%m-%d')} to {curr_date} (akshare):\n\n"
        + "\n".join(lines)
    )


def get_fundamentals(ticker, curr_date=None):
    """Key A-share fundamentals (valuation, profitability, growth trend)."""
    import akshare as ak

    code = a_share_code(ticker)
    year = int((curr_date or datetime.now().strftime("%Y-%m-%d"))[:4]) - 1

    lines: list[str] = []

    # Basic identity + market cap (best-effort; the ratio fields matter more).
    try:
        info = ak.stock_individual_info_em(symbol=code)
        info_map = {}
        if isinstance(info, pd.DataFrame) and not info.empty:
            for _, row in info.iterrows():
                if len(row) >= 2:
                    info_map[str(row.iloc[0])] = row.iloc[1]
        for label, key in (
            ("股票简称", "股票简称"), ("行业", "行业"),
            ("总市值", "总市值"), ("流通市值", "流通市值"), ("上市时间", "上市时间"),
        ):
            if key in info_map and info_map[key] is not None:
                lines.append(f"{label}: {info_map[key]}")
    except Exception as exc:
        logger.debug("akshare individual_info failed for %s: %s", ticker, exc)

    # Financial analysis indicators (the decision-relevant part).
    try:
        fin = ak.stock_financial_analysis_indicator(symbol=code, start_year=str(year))
        if fin is None or fin.empty or "日期" not in fin.columns:
            raise NoMarketDataError(ticker, detail="akshare financial indicator empty")
        fin = fin.copy()
        fin["日期"] = pd.to_datetime(fin["日期"], errors="coerce")
        fin = fin[fin["日期"] <= pd.Timestamp(curr_date)] if curr_date else fin
        if fin.empty:
            raise NoMarketDataError(ticker, detail="akshare financial indicator stale")
        fin = fin.sort_values("日期")
        latest = fin.iloc[-1]

        def _num(v):
            try:
                return None if v is None or pd.isna(v) else float(v)
            except (TypeError, ValueError):
                return None

        for label, col in (
            ("报告期", "日期"),
            ("摊薄每股收益(元)", "摊薄每股收益(元)"),
            ("净资产收益率(%)", "净资产收益率(%)"),
            ("主营业务收入增长率(%)", "主营业务收入增长率(%)"),
            ("净利润增长率(%)", "净利润增长率(%)"),
            ("资产负债率(%)", "资产负债率(%)"),
        ):
            if col not in latest.index:
                continue
            v = latest[col]
            if label == "报告期":
                with suppress(Exception):
                    v = pd.to_datetime(v).date()
            else:
                v = _num(v)
                if v is not None:
                    v = f"{v:.2f}"
            if v is not None:
                lines.append(f"{label}: {v}")

        def _trend(col: str, n: int = 4) -> str:
            parts = []
            for v in fin[col].tail(n).tolist():
                num = _num(v)
                if num is not None:
                    parts.append(f"{num:.1f}")
            return "→".join(parts)

        rev = _trend("主营业务收入增长率(%)")
        profit = _trend("净利润增长率(%)")
        if rev:
            lines.append(f"营收增速趋势(近4期,由远及近): {rev}")
        if profit:
            lines.append(f"净利增速趋势(近4期,由远及近): {profit}")
    except NoMarketDataError:
        raise
    except Exception as exc:
        logger.warning("akshare financial indicator failed for %s: %s", ticker, exc)

    if not lines:
        raise NoMarketDataError(ticker, detail="no fundamental fields returned")

    header = f"# Company Fundamentals for {ticker} (akshare)\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + "\n".join(lines)


def get_news(ticker, start_date, end_date):
    """Per-stock news with publish timestamps, filtered to the window.

    Best-effort: ``stock_news_em`` only serves recent articles, so historical
    windows legitimately come back empty — that degrades to a note, never an
    exception (news is context, not a hard dependency of the pipeline).
    """
    import akshare as ak

    code = a_share_code(ticker)
    try:
        df = ak.stock_news_em(symbol=code)
    except Exception as exc:
        logger.warning("akshare stock_news_em failed for %s: %s", ticker, exc)
        return f"# News for {ticker}\n（国内数据源个股新闻暂时不可用：{type(exc).__name__}）"

    if df is None or df.empty:
        return f"# News for {ticker}\n（窗口内无个股新闻）"

    df = df.rename(columns={"发布时间": "publish_time", "新闻标题": "title", "新闻内容": "content"})
    if "publish_time" not in df.columns:
        return f"# News for {ticker}\n（个股新闻接口无时间字段，无法按窗口过滤）"

    ts = pd.to_datetime(df["publish_time"], errors="coerce")
    start = pd.Timestamp(start_date)
    cutoff = pd.Timestamp(end_date) + pd.Timedelta(days=1)  # inclusive of end_date
    mask = (ts >= start) & (ts < cutoff)

    rows = []
    for _, row in df[mask].iterrows():
        t = ts.loc[_]
        rows.append(
            f"[{str(t.date()) if pd.notna(t) else ''}] {str(row.get('title', ''))[:120]}"
            + (f"\n    {str(row.get('content', ''))[:200]}" if row.get("content") else "")
        )

    if not rows:
        return f"# News for {ticker}\n（窗口 {start_date} ~ {end_date} 内无个股新闻）"

    header = f"# News for {ticker} from {start_date} to {end_date} (akshare)\n\n"
    return header + "\n\n".join(rows)


def _statement_unavailable(ticker, stmt_name):
    """Honest degradation for the three financial statements."""
    return (
        f"# {stmt_name} for {ticker}\n"
        "国内数据源（akshare）暂不逐表提供该报表。关键财务指标（EPS/ROE/营收·净利增速/"
        "资产负债率）已由 get_fundamentals 提供，请基于这些指标分析，不要编造具体科目数值。"
    )


def get_balance_sheet(ticker, freq="quarterly", curr_date=None):
    return _statement_unavailable(ticker, "资产负债表")


def get_cashflow(ticker, freq="quarterly", curr_date=None):
    return _statement_unavailable(ticker, "现金流量表")


def get_income_statement(ticker, freq="quarterly", curr_date=None):
    return _statement_unavailable(ticker, "利润表")
