"""A-share data vendor (akshare / 东方财富).

Chosen because akshare endpoints take explicit date ranges natively and
its per-stock news (stock_news_em) carries publish timestamps — both far
friendlier to a no-lookahead backtest than Yahoo's "latest N articles".

Column names of every frame returned here are normalized to English
(Date/Open/High/Low/Close/Volume) so the gateway and prompt builder are
market-agnostic. Every fetch is defensive: failures raise DataUnavailable
and the gateway decides whether to fall back to yfinance.
"""
from __future__ import annotations

import logging
import os

import pandas as pd

from webapp.core.errors import DataUnavailable
from webapp.engine.financial_timing import available_periods

logger = logging.getLogger(__name__)

# akshare talks to domestic endpoints (eastmoney / sina / cctv), and the GLM
# API (open.bigmodel.cn) is domestic too. When the host runs a system proxy
# (Clash, v2rayN, ...), requests inherits it from the Windows registry and the
# proxy frequently refuses or stalls connections to these domestic hosts
# (ProxyError / read timeouts) — while the yfinance fallback still NEEDS the
# proxy to reach Yahoo. Adding them to NO_PROXY bypasses the proxy for
# domestic vendors only.
_DOMESTIC_SUFFIXES = ("eastmoney.com", "sina.com.cn", "cctv.com", "bigmodel.cn",
                      "tencent.com", "tencentcloudapi.com", "aliyuncs.com")


def _bypass_proxy_for_domestic_hosts() -> None:
    for var in ("NO_PROXY", "no_proxy"):
        parts = [p.strip() for p in os.environ.get(var, "").split(",") if p.strip()]
        for suffix in _DOMESTIC_SUFFIXES:
            if suffix not in parts:
                parts.append(suffix)
        os.environ[var] = ",".join(parts)


_bypass_proxy_for_domestic_hosts()

_OHLCV_RENAME = {
    "日期": "Date", "开盘": "Open", "收盘": "Close",
    "最高": "High", "最低": "Low", "成交量": "Volume",
}
_NEWS_RENAME = {"发布时间": "publish_time", "新闻标题": "title", "新闻内容": "content"}


def a_share_code(ticker: str) -> str:
    """'600519.SS' -> '600519'; '000858.SZ' -> '000858'."""
    return ticker.split(".")[0]


def yahoo_symbol(ticker: str) -> str:
    """A-share ticker -> Yahoo symbol with the correct exchange suffix.

    The suffix the session was created with cannot be trusted: 300308 is a
    Shenzhen ChiNext stock, so Yahoo only knows ``300308.SZ`` — a session
    created as ``300308.SS`` 404s on the yfinance fallback. The numeric code
    prefix decides the exchange, not the suffix: 60x/68x (and funds 51x)
    are Shanghai, everything else Shenzhen.
    """
    code = a_share_code(ticker)
    if code.startswith(("60", "68", "51")):
        return f"{code}.SS"
    return f"{code}.SZ"


def fetch_daily_ohlcv(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Daily qfq OHLCV for an A-share, both dates inclusive, YYYY-MM-DD in."""
    import akshare as ak

    code = a_share_code(symbol)
    fmt = lambda d: d.replace("-", "")  # noqa: E731
    try:
        df = ak.stock_zh_a_hist(
            symbol=code, period="daily",
            start_date=fmt(start_date), end_date=fmt(end_date), adjust="qfq",
        )
    except Exception as exc:
        raise DataUnavailable(f"akshare stock_zh_a_hist failed for {symbol}: {exc}") from exc
    if df is None or df.empty:
        raise DataUnavailable(f"akshare returned no rows for {symbol}")
    df = df.rename(columns=_OHLCV_RENAME)
    if "Date" not in df.columns:
        raise DataUnavailable(f"akshare OHLCV missing 日期 column for {symbol}")
    df["Date"] = pd.to_datetime(df["Date"])
    df["Volume"] = pd.to_numeric(df.get("Volume"), errors="coerce").fillna(0)
    return df[["Date", "Open", "High", "Low", "Close", "Volume"]].sort_values("Date")


def fetch_news(symbol: str, curr_date: str, lookback_days: int = 7) -> list[dict]:
    """Per-stock news with publish timestamps, filtered to (T-lookback, T].

    stock_news_em only serves the most recent ~100 articles, so older
    windows legitimately come back empty — the caller treats that as a
    degradation flag, never as an error.
    """
    import akshare as ak

    code = a_share_code(symbol)
    try:
        df = ak.stock_news_em(symbol=code)
    except Exception as exc:
        raise DataUnavailable(f"akshare stock_news_em failed for {symbol}: {exc}") from exc
    if df is None or df.empty:
        return []
    df = df.rename(columns=_NEWS_RENAME)
    if "publish_time" not in df.columns:
        return []
    ts = pd.to_datetime(df["publish_time"], errors="coerce")
    cutoff = pd.Timestamp(curr_date) + pd.Timedelta(days=1)  # inclusive of T's day
    window_start = pd.Timestamp(curr_date) - pd.Timedelta(days=lookback_days)
    mask = (ts >= window_start) & (ts < cutoff)
    out = []
    for _, row in df[mask].iterrows():
        out.append({
            "date": str(ts.loc[_].date()) if pd.notna(ts.loc[_]) else "",
            "title": str(row.get("title", ""))[:120],
            "content": str(row.get("content", ""))[:300],
            "source": str(row.get("文章来源", row.get("source", "")))[:40],
            "kind": "news",
        })
    return out


def _first_value(row: pd.Series | None, *names: str):
    if row is None:
        return None
    for name in names:
        value = row.get(name)
        if value is not None and not pd.isna(value):
            return value
    return None


def _snapshot_from_indicator_frame(df: pd.DataFrame, curr_date: str) -> dict | None:
    """Build a no-lookahead snapshot from an already-fetched indicator frame."""
    if df is None or df.empty or "日期" not in df.columns:
        return None
    df = df.copy()
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
    public_periods = available_periods(df["日期"].dropna(), curr_date, "cn")
    df = df[df["日期"].isin(public_periods)]
    if df.empty:
        return None
    df = df.sort_values("日期")
    latest = df.iloc[-1]
    latest_date = latest["日期"]
    comparable = df[
        (df["日期"].dt.year < latest_date.year)
        & (df["日期"].dt.month == latest_date.month)
        & (df["日期"].dt.day == latest_date.day)
    ]
    prior = comparable.iloc[-1] if not comparable.empty else None
    keep = {
        "报告期": str(latest["日期"].date()),
        "数据时序说明": "按法定期限估算保守披露日；数据源为当前修订快照，非逐日 point-in-time 版本",
        "摊薄每股收益(元)": _first_value(latest, "摊薄每股收益(元)", "摊薄每股收益（元）"),
        "上年同期摊薄每股收益(元)": _first_value(prior, "摊薄每股收益(元)", "摊薄每股收益（元）"),
        "扣除非经常性损益后的每股收益(元)": _first_value(
            latest,
            "扣除非经常性损益后的每股收益(元)",
            "扣除非经常性损益后的每股收益（元）",
        ),
        "上年同期扣非每股收益(元)": _first_value(
            prior,
            "扣除非经常性损益后的每股收益(元)",
            "扣除非经常性损益后的每股收益（元）",
        ),
        "每股经营性现金流(元)": _first_value(
            latest, "每股经营性现金流(元)", "每股经营性现金流（元）"
        ),
        "销售毛利率(%)": latest.get("销售毛利率(%)"),
        "销售净利率(%)": latest.get("销售净利率(%)"),
        "扣除非经常性损益后的净利润(元)": _first_value(
            latest,
            "扣除非经常性损益后的净利润(元)",
            "扣除非经常性损益后的净利润（元）",
        ),
        "经营现金净流量与净利润的比率(%)": latest.get("经营现金净流量与净利润的比率(%)"),
        "净资产收益率(%)": latest.get("净资产收益率(%)"),
        "主营业务收入增长率(%)": latest.get("主营业务收入增长率(%)"),
        "净利润增长率(%)": latest.get("净利润增长率(%)"),
        "资产负债率(%)": latest.get("资产负债率(%)"),
    }

    def _trend(col: str, n: int = 4) -> str:
        vals = [row.get(col) for _, row in df.tail(n).iterrows()]
        parts = []
        for v in vals:
            if v is None or pd.isna(v):
                continue
            try:
                parts.append(f"{float(v):.1f}")
            except (TypeError, ValueError):
                continue
        return "→".join(parts)

    # 营收/净利增速 are YoY and comparable across periods; ROE is cumulative
    # within the year (Q1 vs Q3 are different bases), so it stays a
    # latest-period-only field — trending it would fabricate a collapse.
    trend = "；".join(
        f"{name} {_trend(col)}"
        for name, col in (
            ("营收增速", "主营业务收入增长率(%)"),
            ("净利增速", "净利润增长率(%)"),
        )
        if _trend(col)
    )
    keep["盈利趋势(近4期,由远及近)"] = trend or None

    # np.float64 is not JSON-serializable — normalize to plain Python scalars.
    return {
        k: (None if v is None or (not isinstance(v, str) and pd.isna(v))
            else (v.item() if hasattr(v, "item") else v))
        for k, v in keep.items()
    }


def fetch_financial_snapshot(symbol: str, curr_date: str) -> dict | None:
    """Latest indicators conservatively estimated as public by ``curr_date``."""
    import akshare as ak

    code = a_share_code(symbol)
    year = int(curr_date[:4]) - 2
    try:
        df = ak.stock_financial_analysis_indicator(symbol=code, start_year=str(year))
    except Exception as exc:
        logger.warning("akshare financial indicator failed for %s: %s", symbol, exc)
        return None
    return _snapshot_from_indicator_frame(df, curr_date)


def fetch_notices_for_date(symbol: str, date: str) -> list[dict]:
    """Company announcements published on one specific date (eastmoney).

    Unlike stock_news_em this endpoint is date-addressable, so it covers
    historical backtest windows. Raises DataUnavailable on transport errors;
    an empty list means the company genuinely filed nothing that day.
    """
    import akshare as ak

    code = a_share_code(symbol)
    try:
        df = ak.stock_notice_report(symbol="全部", date=date.replace("-", ""))
    except Exception as exc:
        raise DataUnavailable(f"akshare stock_notice_report failed for {date}: {exc}") from exc
    if df is None or df.empty or "代码" not in df.columns:
        return []
    hit = df[df["代码"].astype(str).str.zfill(6) == code]
    out = []
    for _, row in hit.iterrows():
        out.append({
            "date": str(row.get("公告日期", date))[:10] or date,
            "title": str(row.get("公告标题", ""))[:120],
            "content": f"公告类型：{row.get('公告类型', '未分类')}",
            "source": "公司公告",
            "kind": "notice",
        })
    return out


_REPORT_CACHE: dict[str, list[dict]] = {}


def fetch_research_reports(symbol: str) -> list[dict]:
    """Full history of broker research reports (eastmoney) for one A-share.

    Returns everything, newest first; the caller filters by date so the
    no-lookahead rule stays with the caller. Cached per code — a session
    scans the same table every day and the endpoint is a full-history dump.

    Carries far more tradable signal than macro headlines: rating (买入/增持),
    house name, and forward EPS/PE estimates.
    """
    import akshare as ak

    code = a_share_code(symbol)
    if code in _REPORT_CACHE:
        return _REPORT_CACHE[code]
    try:
        df = ak.stock_research_report_em(symbol=code)
    except Exception as exc:
        raise DataUnavailable(f"akshare stock_research_report_em failed for {symbol}: {exc}") from exc
    if df is None or df.empty or "日期" not in df.columns:
        _REPORT_CACHE[code] = []
        return []

    eps_cols = sorted(c for c in df.columns if c.endswith("-盈利预测-收益"))
    pe_cols = sorted(c for c in df.columns if c.endswith("-盈利预测-市盈率"))

    out: list[dict] = []
    for _, row in df.iterrows():
        day = str(row.get("日期", ""))[:10]
        if not day:
            continue
        rating = str(row.get("东财评级", "")).strip()
        org = str(row.get("机构", "")).strip()
        forecast_parts = []
        for col in eps_cols[:2]:
            val = row.get(col)
            if val is not None and not pd.isna(val):
                forecast_parts.append(f"{col[:4]}E EPS {float(val):.2f}")
        for col in pe_cols[:2]:
            val = row.get(col)
            if val is not None and not pd.isna(val):
                forecast_parts.append(f"{col[:4]}E PE {float(val):.1f}")
        content = f"{org}（评级：{rating or '未评级'}）"
        if forecast_parts:
            content += "；" + "，".join(forecast_parts)
        out.append({
            "date": day,
            "title": str(row.get("报告名称", ""))[:120],
            "content": content,
            "source": org or "券商研报",
            "kind": "research",
            "rating": rating or "未评级",  # structured: sentiment mix, not just free text
        })
    out.sort(key=lambda r: r["date"], reverse=True)
    _REPORT_CACHE[code] = out
    return out


def fetch_macro_news_for_date(date: str, max_items: int = 12) -> list[dict]:
    """CCTV 新闻联播 headlines for one date (national macro news).

    Date-addressable, so it backfills historical windows. Titles carry most
    of the signal; content is truncated hard to keep the prompt compact.
    """
    import akshare as ak

    try:
        df = ak.news_cctv(date=date.replace("-", ""))
    except Exception as exc:
        raise DataUnavailable(f"akshare news_cctv failed for {date}: {exc}") from exc
    if df is None or df.empty:
        return []
    out = []
    for _, row in df.head(max_items).iterrows():
        raw_date = str(row.get("date", ""))
        norm = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}" if len(raw_date) == 8 else date
        out.append({
            "date": norm,
            "title": str(row.get("title", ""))[:120],
            "content": str(row.get("content", ""))[:200],
            "source": "新闻联播",
            "kind": "macro",
        })
    return out


def fetch_index_daily(index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Benchmark index daily closes, e.g. 'sh000001' (上证指数)."""
    import akshare as ak

    try:
        df = ak.stock_zh_index_daily(symbol=index_code)
    except Exception as exc:
        raise DataUnavailable(f"akshare index {index_code} failed: {exc}") from exc
    if df is None or df.empty:
        raise DataUnavailable(f"akshare index {index_code} returned no rows")
    df = df.rename(columns={"date": "Date", "close": "Close"})
    df["Date"] = pd.to_datetime(df["Date"])
    df = df[(df["Date"] >= start_date) & (df["Date"] <= end_date)]
    return df[["Date", "Close"]].sort_values("Date")


def index_for_ticker(ticker: str) -> str:
    """Map an A-share ticker to its home benchmark index.

    Derived from the numeric code prefix, not the ticker suffix — users
    attach the wrong suffix often enough (300308.SS) that the suffix is
    not a reliable exchange signal.
    """
    return "sh000001" if yahoo_symbol(ticker).endswith(".SS") else "sz399001"
