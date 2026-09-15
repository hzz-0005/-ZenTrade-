"""US / HK data vendor (yfinance).

Mirrors ``cn_source`` for non-A-share markets so the pipeline has the same
three inputs everywhere:

  fundamentals — quarterly statements, keyed by period end, filtered to <= T
  research     — broker rating changes (upgrades/downgrades), dated
  news         — per-symbol headlines over an explicit window

Every read is point-in-time: statements carry their period-end date and rating
actions carry their grade date, so a backtest at date T only ever sees what was
public by T. Nothing here falls back to ``Ticker.info`` — that snapshot is
always "now" and would leak the future into historical decisions.
"""
from __future__ import annotations

import json
import logging

import pandas as pd

from webapp.engine.financial_timing import available_periods

logger = logging.getLogger(__name__)

# yfinance row labels -> the key surfaced in the prompt.
_INCOME_ROWS = {
    "营业收入": "Total Revenue",
    "净利润": "Net Income",
    "基本每股收益": "Basic EPS",
}
_BALANCE_ROWS = {
    "股东权益": "Stockholders Equity",
    "总资产": "Total Assets",
    "总负债": "Total Liabilities Net Minority Interest",
}


def _pick(df: pd.DataFrame, label: str, col):
    if df is None or df.empty or label not in df.index:
        return None
    try:
        val = float(df.loc[label, col])
    except (TypeError, ValueError):
        return None
    return None if pd.isna(val) else val


def _jsonable(v):
    """numpy/pandas scalars are not JSON-serializable; normalize for storage."""
    if v is None:
        return None
    if hasattr(v, "item"):
        return v.item()
    return float(v)


# yfinance action codes -> Chinese labels the news-gate rules can match.
_ACTION_CN = {
    "up": "上调评级",
    "down": "下调评级",
    "init": "首次覆盖",
    "main": "维持",
    "reit": "重申",
}


_NAME_CACHE: dict[str, str] = {}


def _company_name(symbol: str) -> str:
    """Short company name for headline matching (NVDA -> "NVIDIA"), cached."""
    if symbol in _NAME_CACHE:
        return _NAME_CACHE[symbol]
    name = ""
    try:
        import yfinance as yf

        name = str(yf.Ticker(symbol).info.get("shortName") or "")
    except Exception:
        name = ""
    # "NVIDIA Corporation" -> "NVIDIA"; strip a trailing comma if present.
    first = name.split()[0].strip(",") if name else ""
    _NAME_CACHE[symbol] = first
    return first


def _to_float_or(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _annual_pair(ticker, cutoff) -> tuple[float | None, float | None, float | None, float | None]:
    """(rev_now, ni_now, rev_prev, ni_prev) from the annual income statement.

    Quarterly history is capped at five periods, which often puts the year-ago
    quarter out of reach; the annual statements carry four years and back up
    the growth rates. Both sides always come from the SAME statement — mixing a
    quarterly figure with an annual one produces nonsense like "-48% growth".
    """
    try:
        annual = ticker.income_stmt
    except Exception:
        return None, None, None, None
    if annual is None or annual.empty:
        return None, None, None, None
    cols = sorted(available_periods(annual.columns, cutoff, "us", annual=True),
                  key=pd.Timestamp, reverse=True)
    if len(cols) < 2:
        return None, None, None, None
    return (
        _pick(annual, _INCOME_ROWS["营业收入"], cols[0]),
        _pick(annual, _INCOME_ROWS["净利润"], cols[0]),
        _pick(annual, _INCOME_ROWS["营业收入"], cols[1]),
        _pick(annual, _INCOME_ROWS["净利润"], cols[1]),
    )


def _fmt_big(v: float) -> str:
    """Human-scale money formatting for trend strings ('150.1B', '3.4M')."""
    v = float(v)
    for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(v) >= div:
            return f"{v / div:.1f}{suffix}"
    return f"{v:.1f}"


def fetch_financial_snapshot(symbol: str, curr_date: str) -> dict | None:
    """Latest quarterly fundamentals whose period end is <= curr_date.

    Growth rates compare against the same quarter a year earlier (four columns
    back); ROE uses trailing four quarters. Both stay inside periods already
    reported by T.
    """
    import yfinance as yf

    cutoff = pd.Timestamp(curr_date)
    try:
        ticker = yf.Ticker(symbol)
        income = ticker.quarterly_financials
        balance = ticker.quarterly_balance_sheet
    except Exception as exc:
        logger.warning("yfinance statements failed for %s: %s", symbol, exc)
        return None
    if income is None or income.empty:
        return None

    cols = sorted(available_periods(income.columns, cutoff, "us"),
                  key=pd.Timestamp, reverse=True)
    if not cols:
        return None
    col = cols[0]

    revenue = _pick(income, _INCOME_ROWS["营业收入"], col)
    net_income = _pick(income, _INCOME_ROWS["净利润"], col)
    eps = _pick(income, _INCOME_ROWS["基本每股收益"], col)
    equity = _pick(balance, _BALANCE_ROWS["股东权益"], col) if balance is not None else None
    assets = _pick(balance, _BALANCE_ROWS["总资产"], col) if balance is not None else None
    liabilities = (
        _pick(balance, _BALANCE_ROWS["总负债"], col) if balance is not None else None
    )

    # Trailing twelve months: the four reported quarters up to T.
    ttm_income = 0.0
    ttm_quarters = 0
    for c in cols[:4]:
        val = _pick(income, _INCOME_ROWS["净利润"], c)
        if val is not None:
            ttm_income += val
            ttm_quarters += 1

    def growth(now, before):
        if now is None or before in (None, 0):
            return None
        return round((now - before) / abs(before) * 100, 2)

    # Quarterly YoY needs the same quarter a year back (four periods); yfinance
    # caps quarterly history at five, so this is often out of reach.
    yoy_col = cols[4] if len(cols) > 4 else None
    growth_revenue = (
        growth(revenue, _pick(income, _INCOME_ROWS["营业收入"], yoy_col)) if yoy_col else None
    )
    growth_income = (
        growth(net_income, _pick(income, _INCOME_ROWS["净利润"], yoy_col)) if yoy_col else None
    )
    # Fall back to annual-vs-annual (never quarterly-vs-annual).
    if growth_revenue is None or growth_income is None:
        a_rev, a_ni, a_rev_prev, a_ni_prev = _annual_pair(ticker, cutoff)
        if growth_revenue is None:
            growth_revenue = growth(a_rev, a_rev_prev)
        if growth_income is None:
            growth_income = growth(a_ni, a_ni_prev)

    roe = round(ttm_income / equity * 100, 2) if equity else None
    debt_ratio = (
        round(liabilities / assets * 100, 2) if (liabilities is not None and assets) else None
    )

    # Quarterly earnings trajectory (oldest -> newest) so the prompt shows a
    # trend, not one static column.
    def _q_trend(row_label: str, n: int = 4) -> str:
        vals = [_pick(income, row_label, c) for c in reversed(cols[:n])]
        vals = [v for v in vals if v is not None]
        return "→".join(_fmt_big(v) for v in vals)

    trend_parts = []
    revenue_trend = _q_trend(_INCOME_ROWS["营业收入"])
    if revenue_trend:
        trend_parts.append(f"营收 {revenue_trend}")
    income_trend = _q_trend(_INCOME_ROWS["净利润"])
    if income_trend:
        trend_parts.append(f"净利 {income_trend}")

    return {
        "报告期": str(pd.Timestamp(col).date()),
        "数据时序说明": "按报告期后45天估算保守披露日；数据源为当前修订快照，非逐日 point-in-time 版本",
        "每股收益": _jsonable(eps),
        "净资产收益率(%)": _jsonable(roe),
        "营业收入同比增长(%)": _jsonable(growth_revenue),
        "净利润同比增长(%)": _jsonable(growth_income),
        "资产负债率(%)": _jsonable(debt_ratio),
        "盈利趋势(近4季,由远及近)": "；".join(trend_parts) or None,
        "ttm_quarters": ttm_quarters,
    }


def fetch_analyst_actions(symbol: str) -> list[dict]:
    """Broker rating changes, newest first — the US analogue of A-share 研报.

    Carries firm, from/to grade and price-target moves, keyed by the grade date
    so the caller can filter to <= T.
    """
    import yfinance as yf

    try:
        raw = yf.Ticker(symbol).upgrades_downgrades
    except Exception as exc:
        logger.warning("yfinance upgrades_downgrades failed for %s: %s", symbol, exc)
        return []
    if raw is None or raw.empty:
        return []

    out: list[dict] = []
    for ts, row in raw.iterrows():
        try:
            day = str(pd.Timestamp(ts).date())
        except Exception:
            continue
        firm = str(row.get("Firm") or "").strip()
        to_grade = str(row.get("ToGrade") or "").strip()
        from_grade = str(row.get("FromGrade") or "").strip()
        action = str(row.get("Action") or "").strip()
        target = row.get("currentPriceTarget")

        move = f"{from_grade} → {to_grade}" if (from_grade and to_grade) else (to_grade or from_grade)
        # Label the action in Chinese so the news gate's keyword rules hit it.
        # "维持"/"重申" deliberately omit 评级 so routine reaffirmations do not
        # escalate — only real changes (上调/下调/首次覆盖) do.
        action_cn = _ACTION_CN.get(action.lower(), "")
        title = f"{firm}：{move}".strip(" ：")
        if action_cn:
            title += f"（{action_cn}）"
        elif action:
            title += f"（{action}）"
        bits = []
        if action:
            bits.append(f"动作：{action}")
        if target is not None and not pd.isna(target):
            bits.append(f"目标价 {float(target):.2f}")
        out.append({
            "date": day,
            "title": title[:120],
            "content": "；".join(bits)[:200],
            "source": firm or "券商评级",
            "kind": "research",
            # Structured direction for the sentiment mix: up/init -> 上调,
            # down -> 下调, reaffirmations -> 维持.
            "rating": {"up": "上调", "init": "上调", "down": "下调"}.get(
                action.lower(), "维持"
            ) if action else "维持",
        })
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


def _normalize(raw, default_kind: str = "news") -> list[dict]:
    if isinstance(raw, str) or not raw:
        return []  # sentinel strings ("No news found for ...") are not news
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append({
            "date": str(item.get("publish_time") or item.get("date") or "")[:10],
            "title": str(item.get("title", ""))[:120],
            "content": str(item.get("content", item.get("summary", "")))[:300],
            "source": str(item.get("source", ""))[:40],
            "kind": item.get("kind", default_kind),
        })
    return [n for n in out if n["date"]]


def _ensure_env_loaded() -> None:
    """Make sure the project's .env has been merged into os.environ.

    ``tradingagents/__init__`` calls ``load_dotenv(find_dotenv(...))`` — that
    import is the project's single source of env truth. Reading a key before it
    happens silently yields None, so trigger it explicitly instead of relying on
    whatever else happened to be imported first.
    """
    import tradingagents  # noqa: F401  (import side effect: loads .env)


def month_chunks(start_date: str, end_date: str) -> list[tuple[str, str]]:
    """Split a range into calendar-month windows for bulk fetching.

    NEWS_SENTIMENT returns at most ~50 items per request, so asking for a whole
    backtest window at once yields only the most recent weeks. Chunking by month
    keeps each request inside that cap — and because each chunk is cached
    separately, a long session costs one call per month instead of one per
    trading day (the free tier is ~25/day).
    """
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    chunks: list[tuple[str, str]] = []
    cur = start
    while cur <= end:
        month_end = cur + pd.offsets.MonthEnd(0)
        chunk_end = min(month_end, end)
        chunks.append((cur.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")))
        cur = chunk_end + pd.Timedelta(days=1)
    return chunks


def _alpha_vantage_feed(symbol: str, start_date: str, end_date: str) -> list[dict]:
    """Raw NEWS_SENTIMENT feed for a window, fetched live (no disk cache).

    yfinance only serves recent articles, so any historical backtest window
    comes back empty. Alpha Vantage covers the past with a free key
    (ALPHA_VANTAGE_API_KEY); without one we stay silent rather than erroring.
    """
    import os

    _ensure_env_loaded()
    if not os.environ.get("ALPHA_VANTAGE_API_KEY"):
        return []

    try:
        from tradingagents.dataflows.alpha_vantage import get_news as av_get_news

        raw = av_get_news(symbol, start_date, end_date)
    except Exception as exc:
        logger.warning("alpha vantage news failed for %s: %s", symbol, exc)
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    return (raw or {}).get("feed") or []


def _alpha_vantage_news(symbol: str, start_date: str, end_date: str) -> list[dict]:
    """Historical headlines via Alpha Vantage, filtered to the subject company."""
    feed = _alpha_vantage_feed(symbol, start_date, end_date)
    wanted = symbol.upper()
    out: list[dict] = []
    for item in feed:
        published = str(item.get("time_published") or "")
        if len(published) < 8:
            continue
        day = f"{published[:4]}-{published[4:6]}-{published[6:8]}"

        # NEWS_SENTIMENT returns every article that merely *mentions* the
        # ticker — most are about other companies ("3 Value Stocks with Warning
        # Signs" mentions NVDA in passing). Feeding those to the news analyst
        # is worse than feeding nothing. Keep an article when either:
        #   - its relevance to our symbol is high (>= 0.8), or
        #   - the company name appears in the headline
        # The second rule matters: genuinely important stories sometimes carry
        # a low relevance score ("Why OpenAI Is Unhappy With Some Nvidia
        # Chips" scores 0.30) and would otherwise be dropped.
        sentiments = item.get("ticker_sentiment") or []
        ours = next(
            (s for s in sentiments if str(s.get("ticker", "")).upper() == wanted), None
        )
        if ours is None:
            continue
        relevance = _to_float_or(ours.get("relevance_score"), 0.0)
        title = str(item.get("title", ""))
        name = _company_name(symbol)
        named = bool(name) and name.lower() in title.lower()
        if not (relevance >= 0.8 or named):
            continue

        # Sentiment first: the content field is truncated, and a one-line
        # verdict is worth more than the tail of a long summary.
        bits: list[str] = []
        label = str(ours.get("ticker_sentiment_label") or "")
        score = _to_float_or(ours.get("ticker_sentiment_score"), None)
        if label:
            bits.append(f"情绪：{label}" + (f"({score:+.2f})" if score is not None else ""))
        bits.append(str(item.get("summary", ""))[:240])
        out.append({
            "date": day,
            "title": title[:120],
            "content": "；".join(b for b in bits if b)[:300],
            "source": str(item.get("source", ""))[:40],
            "kind": "news",
        })
    out.sort(key=lambda n: n["date"], reverse=True)
    return out[:20]


def fetch_news(symbol: str, start_date: str, end_date: str) -> list[dict]:
    """Per-symbol headlines over [start_date, end_date], structured.

    Deliberately does NOT use ``get_news_yfinance``: that helper renders its
    result into a markdown *string* for LLM prompts, so there is nothing
    structured to filter or persist (which is why the old US path always
    produced zero items). We read the Yahoo payload directly and reuse the
    project's article parser, then window-filter here for no-lookahead.

    Yahoo only carries recent articles, so historical backtest windows usually
    come back empty; Alpha Vantage covers those when a key is configured.
    """
    import yfinance as yf

    from tradingagents.dataflows.symbol_utils import normalize_symbol
    from tradingagents.dataflows.yfinance_news import _extract_article_data

    try:
        canonical = normalize_symbol(symbol)
        raw = yf.Ticker(canonical).news or []
    except Exception as exc:
        logger.warning("yfinance news failed for %s: %s", symbol, exc)
        raw = []

    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date) + pd.Timedelta(days=1)  # end date inclusive
    out: list[dict] = []
    for article in raw:
        data = _extract_article_data(article)
        pub = data.get("pub_date")
        if pub is None:
            continue
        day = pd.Timestamp(pub.date())
        if not (start <= day < end):
            continue
        out.append({
            "date": str(day.date()),
            "title": str(data.get("title", ""))[:120],
            "content": str(data.get("summary", ""))[:300],
            "source": str(data.get("publisher", ""))[:40],
            "kind": "news",
        })
    if not out:
        out = _alpha_vantage_news(symbol, start_date, end_date)
    return out
