"""Market-aware data gateway with a hard no-lookahead guarantee.

Routing: '.SS/.SZ' -> A-share vendor (akshare), anything else -> the
repo's yfinance-backed load_ohlcv. Independent of the path taken, every
frame leaves this module filtered to Date <= sim_date — the gateway is
the single choke point, so prompt content physically cannot contain a
post-T row.

Live-only sources (StockTwits, Reddit, Polymarket, ticker.info, insider
transactions) are never called in backtest mode; their absence is
reported honestly via data_flags.
"""
from __future__ import annotations

import logging

import pandas as pd

from webapp.core.errors import DataUnavailable
from webapp.engine.clock import get_sim_date
from webapp.engine.market_profile import enrich_fundamentals
from webapp.engine.vendors import cn_source, us_source

logger = logging.getLogger(__name__)

_INDICATORS = ["close_50_sma", "close_20_sma", "close_10_ema", "rsi", "macd", "boll_ub", "boll_lb"]


def detect_market(ticker: str) -> str:
    t = ticker.upper()
    if t.endswith((".SS", ".SZ", ".BJ")):
        return "cn"
    if t.endswith(".HK"):
        return "hk"
    return "us"


class DataGateway:
    """One instance per session; holds the (already end-clamped) price frame."""

    def __init__(self, ticker: str, market: str, macro_news_enabled: bool = False):
        self.ticker = ticker
        self.market = market
        # CCTV headlines are political: near-zero trading signal, and GLM's
        # content filter rejects the entire request when they are present.
        self.macro_news_enabled = macro_news_enabled
        self._price_frame: pd.DataFrame | None = None
        self._benchmark_frame: pd.DataFrame | None = None
        # Historical news archive (date -> items), prefetched once per session.
        self._notice_cache: dict[str, list[dict]] = {}
        self._macro_cache: dict[str, list[dict]] = {}
        self._archive_prefetched = False
        # Fundamentals snapshot (报告期 <= T); refreshed at most monthly.
        self._fundamentals: dict | None = None
        self._fundamentals_key: str | None = None
        # Broker research reports: full history loaded once, date-filtered per day.
        self._reports_raw: list[dict] | None = None

    # ---- one-time frame priming (call from asyncio.to_thread) ----

    def load_price_frame(self, start_date: str, end_date: str, lookback_calendar_days: int = 60) -> pd.DataFrame:
        """Full daily frame over [start-lookback, min(end, real today)].

        Never contains rows after end_date, so per-day slicing below is
        belt-and-braces on top of an already-safe frame.
        """
        fetch_start = (pd.Timestamp(start_date) - pd.Timedelta(days=lookback_calendar_days)).strftime("%Y-%m-%d")
        real_today = pd.Timestamp.today().strftime("%Y-%m-%d")
        fetch_end = min(end_date, real_today)

        frame = None
        akshare_error: str | None = None
        if self.market == "cn":
            try:
                frame = cn_source.fetch_daily_ohlcv(self.ticker, fetch_start, fetch_end)
            except DataUnavailable as exc:
                akshare_error = str(exc)
                logger.warning("A-share vendor failed (%s); falling back to yfinance", exc)
                frame = self._load_yf(fetch_start, fetch_end)
        else:
            frame = self._load_yf(fetch_start, fetch_end)

        if frame is None or frame.empty:
            reasons = []
            if akshare_error:
                reasons.append(f"akshare: {akshare_error}")
            reasons.append(f"yfinance fallback: no rows for {self.ticker}")
            raise DataUnavailable(
                f"no price data for {self.ticker} in [{fetch_start}, {fetch_end}]"
                f" ({'; '.join(reasons)})"
            )
        self._price_frame = frame.reset_index(drop=True)
        return self._price_frame

    def _load_yf(self, fetch_start: str, fetch_end: str) -> pd.DataFrame | None:
        # load_ohlcv clamps to its curr_date arg and pulls 5y of history;
        # pass fetch_end so the frame never contains rows past the session end.
        from tradingagents.dataflows.stockstats_utils import load_ohlcv

        # A-share codes are suffix-sensitive on Yahoo: 300308 only exists as
        # .SZ, so re-derive the exchange suffix from the numeric code instead
        # of trusting the suffix the session was created with.
        symbol = cn_source.yahoo_symbol(self.ticker) if self.market == "cn" else self.ticker
        try:
            df = load_ohlcv(symbol, fetch_end)
        except Exception as exc:
            logger.warning("yfinance load failed for %s: %s", self.ticker, exc)
            return None
        df = df.copy()
        df["Date"] = pd.to_datetime(df["Date"])
        return df[(df["Date"] >= fetch_start) & (df["Date"] <= fetch_end)][
            ["Date", "Open", "High", "Low", "Close", "Volume"]
        ].sort_values("Date")

    def trading_days(self, start_date: str, end_date: str) -> list[str]:
        if self._price_frame is None:
            raise RuntimeError("call load_price_frame() first")
        mask = (self._price_frame["Date"] >= start_date) & (self._price_frame["Date"] <= end_date)
        return [d.strftime("%Y-%m-%d") for d in self._price_frame.loc[mask, "Date"]]

    # ---- per-day accessors (all clamped to sim_date) ----

    def ohlcv_tail(self, curr_date: str, rows: int = 20) -> pd.DataFrame:
        frame = self._price_frame
        if frame is None:
            raise RuntimeError("call load_price_frame() first")
        # Defensive double clamp: sim_date AND session end (frame is already
        # end-clamped; this keeps the invariant local to every read).
        cutoff = min(curr_date, get_sim_date())
        tail = frame[frame["Date"] <= pd.Timestamp(cutoff)].tail(rows)
        if tail.empty:
            raise DataUnavailable(f"no price rows up to {cutoff}")
        return tail

    def close_on(self, curr_date: str) -> float:
        tail = self.ohlcv_tail(curr_date, rows=1)
        return float(tail.iloc[-1]["Close"])

    def execution_open_on(self, execution_date: str) -> float:
        """Return the exact next-session open for order filling only.

        This intentionally bypasses the simulation-clock clamp used by every
        signal/prompt accessor above.  The backtest engine may use it *after*
        a decision has been formed at the preceding close; callers must never
        feed this value into a decision context.  Exact matching (rather than
        a tail lookup) prevents a holiday or missing bar from being filled at
        a neighbouring day's price.
        """
        frame = self._price_frame
        if frame is None:
            raise RuntimeError("call load_price_frame() first")
        rows = frame[frame["Date"] == pd.Timestamp(execution_date)]
        if rows.empty:
            raise DataUnavailable(f"no execution bar for {execution_date}")
        value = rows.iloc[-1]["Open"]
        if pd.isna(value) or float(value) <= 0:
            raise DataUnavailable(f"no executable open for {execution_date}")
        return float(value)

    def execution_bar_on(self, execution_date: str) -> dict:
        """Exact execution-session bar, isolated from all signal accessors."""
        frame = self._price_frame
        if frame is None:
            raise RuntimeError("call load_price_frame() first")
        rows = frame[frame["Date"] == pd.Timestamp(execution_date)]
        if rows.empty:
            raise DataUnavailable(f"no execution bar for {execution_date}")
        row = rows.iloc[-1]
        return {
            "date": pd.Timestamp(row["Date"]).strftime("%Y-%m-%d"),
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
            "volume": float(row.get("Volume", 0) or 0),
        }

    def indicator_snapshot(self, curr_date: str) -> dict:
        """Latest indicator values computed from the clamped frame (stockstats)."""
        from stockstats import wrap

        tail = self.ohlcv_tail(curr_date, rows=120)
        if len(tail) < 55:  # SMA50 needs warmup
            return {}
        df = wrap(tail.copy())
        snap: dict[str, float | None] = {}
        for ind in _INDICATORS:
            try:
                val = df[ind].iloc[-1]
                snap[ind] = None if pd.isna(val) else round(float(val), 4)
            except Exception:
                snap[ind] = None
        return snap

    def prefetch_news_archive(self, start_date: str, end_date: str) -> None:
        """One-time priming of the dated news/research sources for the window.

        A-share: stock_news_em only serves the latest ~100 articles, so any
        backtest window comes back empty. Two date-addressable akshare
        endpoints cover the gap — per-stock announcements (stock_notice_report)
        and CCTV macro headlines (news_cctv). Failures are per-date and
        non-fatal; the day's news is degraded, never fabricated.

        US/HK: the dated source is broker rating changes (upgrades_downgrades),
        loaded once here so the escalation gate has material from day one.
        """
        if self._archive_prefetched:
            return
        if self.market != "cn":
            if self._reports_raw is None:
                try:
                    self._reports_raw = us_source.fetch_analyst_actions(self.ticker)
                except Exception as exc:
                    logger.warning("analyst actions prefetch failed for %s: %s", self.ticker, exc)
                    self._reports_raw = []
            # Bulk-fetch dated news once per calendar month and index it by day.
            # Per-day fetching would burn a request every decision day and the
            # free Alpha Vantage tier only allows ~25/day.
            for month_start, month_end in us_source.month_chunks(start_date, end_date):
                try:
                    for item in us_source.fetch_news(self.ticker, month_start, month_end):
                        self._notice_cache.setdefault(item["date"], []).append(item)
                except Exception as exc:
                    logger.warning(
                        "us news prefetch failed for %s %s~%s: %s",
                        self.ticker, month_start, month_end, exc,
                    )
            if self._notice_cache:
                logger.info(
                    "us news archive ready for %s: %d dated items",
                    self.ticker, sum(len(v) for v in self._notice_cache.values()),
                )
            self._archive_prefetched = True
            return
        from concurrent.futures import ThreadPoolExecutor

        dates = [d.strftime("%Y-%m-%d") for d in pd.date_range(start_date, end_date)]
        # CCTV headlines are only used as filler when macro filler is enabled
        # (default off) — skip the per-day fetch entirely otherwise, it is
        # half of all prefetch requests for data nothing ever reads.
        want_macro = self.macro_news_enabled
        logger.info("prefetching news archive for %s over %d calendar days", self.ticker, len(dates))

        def _one(d: str):
            notices: list[dict] = []
            macro: list[dict] = []
            try:
                notices = cn_source.fetch_notices_for_date(self.ticker, d)
            except DataUnavailable as exc:
                logger.debug("notice fetch failed for %s: %s", d, exc)
            if want_macro:
                try:
                    macro = cn_source.fetch_macro_news_for_date(d)
                except DataUnavailable as exc:
                    logger.debug("macro fetch failed for %s: %s", d, exc)
            return d, notices, macro

        fetched = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            for d, notices, macro in pool.map(_one, dates):
                self._notice_cache[d] = notices
                self._macro_cache[d] = macro
                fetched += 1
        self._archive_prefetched = True
        total_notices = sum(len(v) for v in self._notice_cache.values())
        logger.info(
            "news archive ready: %d/%d dates, %d announcements for %s",
            fetched, len(dates), total_notices, self.ticker,
        )

    def _archive_notices(self, curr_date: str, lookback_days: int) -> list[dict]:
        window_start = pd.Timestamp(curr_date) - pd.Timedelta(days=lookback_days)
        out = []
        for d, items in self._notice_cache.items():
            if window_start <= pd.Timestamp(d) <= pd.Timestamp(curr_date):
                out.extend(items)
        return sorted(out, key=lambda n: n.get("date", ""), reverse=True)

    def _archive_macro(self, curr_date: str, lookback_days: int) -> list[dict]:
        window_start = pd.Timestamp(curr_date) - pd.Timedelta(days=lookback_days)
        out = []
        for d, items in self._macro_cache.items():
            if window_start <= pd.Timestamp(d) <= pd.Timestamp(curr_date):
                out.extend(items)
        return sorted(out, key=lambda n: n.get("date", ""), reverse=True)

    def _load_reports(self) -> list[str]:
        """Lazy-load the broker research / analyst-actions list. Returns flags."""
        if self._reports_raw is not None:
            return []
        try:
            if self.market == "cn":
                self._reports_raw = cn_source.fetch_research_reports(self.ticker)
            else:
                # No A-share 研报 equivalent exists; broker rating changes
                # (firm, from/to grade, price target) play the same role.
                self._reports_raw = us_source.fetch_analyst_actions(self.ticker)
        except Exception as exc:
            self._reports_raw = []
            return [f"reports: unavailable ({exc.__class__.__name__})"]
        return []

    def fundamentals(self, curr_date: str) -> tuple[dict | None, list[str]]:
        """(snapshot, flags) — latest reported financials with 报告期 <= T."""
        flags: list[str] = []
        key = curr_date[:7]  # quarterly disclosures; monthly refresh is plenty
        if self._fundamentals is None or self._fundamentals_key != key:
            try:
                if self.market == "cn":
                    self._fundamentals = cn_source.fetch_financial_snapshot(self.ticker, curr_date)
                else:
                    self._fundamentals = us_source.fetch_financial_snapshot(self.ticker, curr_date)
            except Exception as exc:  # defensive: snapshots swallow their own errors
                self._fundamentals = None
                flags.append(f"fundamentals: unavailable ({type(exc).__name__})")
            self._fundamentals_key = key
        if self._fundamentals is None and not flags:
            flags.append("fundamentals: no reported period <= T")
        snap = dict(self._fundamentals) if self._fundamentals else None
        if snap:
            # Valuation anchor, recomputed per call (never cached — the frame's
            # close moves daily while the snapshot is monthly). EPS is
            # annualized first: A-share 摊薄EPS is cumulative within the year,
            # yfinance quarterly EPS is a single quarter.
            period = str(snap.get("报告期") or "")
            eps_fields = (
                (
                    ("摊薄每股收益(元)", "市盈率PE(年化估算)"),
                    ("扣除非经常性损益后的每股收益(元)", "扣非市盈率PE(年化估算)"),
                )
                if self.market == "cn"
                else (("每股收益", "市盈率PE(年化估算)"),)
            )
            for eps_key, pe_key in eps_fields:
                try:
                    eps = float(snap.get(eps_key))
                    if eps <= 0 or len(period) < 10:
                        continue
                    if self.market == "cn":
                        quarters = max(int(period[5:7]) // 3, 1)
                        annual_eps = eps * 4.0 / quarters
                    else:
                        annual_eps = eps * 4.0
                    close = self.close_on(curr_date)
                    if close:
                        snap[pe_key] = round(close / annual_eps, 1)
                except (TypeError, ValueError, ZeroDivisionError):
                    continue
            snap = enrich_fundamentals(self.ticker, self.market, snap)
        return snap, flags

    def reports(self, curr_date: str, lookback_days: int = 60,
                max_items: int = 5) -> tuple[list[dict], list[str]]:
        """(items, flags) — broker research published in the last `lookback_days`.

        60 days rather than 7: a rating change stays informative for weeks,
        whereas a 7-day window would almost always be empty for a single name.
        Still clamped to <= curr_date, so no lookahead.
        """
        flags: list[str] = []
        flags.extend(self._load_reports())
        if not self._reports_raw:
            if not flags:
                flags.append("reports: no broker coverage")
            return [], flags
        window_start = (pd.Timestamp(curr_date) - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        items = [r for r in self._reports_raw if window_start <= r["date"] <= curr_date]
        if items:
            flags.append(f"reports: {len(items)} 研报 in {lookback_days}d")
        return items[:max_items], flags

    def material_items(self, curr_date: str,
                       notice_days: int = 7,
                       research_days: int = 60) -> dict[tuple[str, str], dict]:
        """Material news in the window as {(date, title): item} — cached, no network.

        Only announcements and broker research count; macro headlines appear
        every day and would invalidate a coasting decision constantly.

        The two kinds get different windows on purpose: an announcement is
        actionable for days, a rating change stays informative for weeks — and
        a 7-day window on research would be empty for almost every name, which
        would silently starve the escalation gate.
        """
        if not (self._notice_cache or self._reports_raw):
            return {}
        items = list(self._archive_notices(curr_date, notice_days))
        if self._reports_raw:
            start = pd.Timestamp(curr_date) - pd.Timedelta(days=research_days)
            items += [r for r in self._reports_raw
                      if start <= pd.Timestamp(r["date"]) <= pd.Timestamp(curr_date)]
        return {(i.get("date", ""), i.get("title", "")): i for i in items}

    def news_signature(self, curr_date: str) -> tuple[tuple[str, str], ...]:
        """Identity of material news in the window — cached data only, no network.

        Used to detect "something new landed" while a decision is coasting, so
        the standing decision can be revisited on news rather than only on price.
        """
        return tuple(sorted(self.material_items(curr_date).keys()))

    def sentiment(self, curr_date: str, lookback_days: int = 60) -> tuple[str | None, list[str]]:
        """(summary, flags) — institutional mood from the rating mix in window.

        A real, computable sentiment proxy: the distribution of broker
        ratings (cn) or upgrade/downgrade actions (us) over the lookback
        window, plus the lean derived from it. Empty when there is no
        coverage at all — never fabricated.
        """
        flags = self._load_reports()
        if not self._reports_raw:
            return None, (flags or ["sentiment: no broker coverage"])
        window_start = (pd.Timestamp(curr_date) - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        recent = [r for r in self._reports_raw
                  if window_start <= str(r.get("date", "")) <= curr_date]
        if not recent:
            return None, ["sentiment: no ratings in window"]

        bullish = bearish = neutral = 0
        for r in recent:
            direction = str(r.get("rating") or "")
            if self.market == "cn":
                if any(w in direction for w in ("买入", "强烈推荐")):
                    bullish += 1
                elif any(w in direction for w in ("减持", "卖出", "回避")):
                    bearish += 1
                elif any(w in direction for w in ("增持", "中性", "持有", "推荐")):
                    neutral += 1
            else:
                if direction == "上调":
                    bullish += 1
                elif direction == "下调":
                    bearish += 1
                else:
                    neutral += 1
        total = len(recent)
        if bullish > bearish * 2 and bullish > neutral:
            lean = "机构情绪偏多"
        elif bearish > bullish * 2 and bearish > neutral:
            lean = "机构情绪偏空"
        elif bullish and bearish:
            lean = "机构情绪分歧"
        else:
            lean = "机构情绪中性"
        summary = (
            f"近{lookback_days}日 {total} 份研报/机构动作："
            f"看多 {bullish} · 中性 {neutral} · 看空 {bearish} → {lean}"
        )
        return summary, flags

    def news(self, curr_date: str, lookback_days: int = 7) -> tuple[list[dict], list[str]]:
        """(items, degradation_flags) — never fabricates; empty is honest.

        Composition for cn market, in priority order:
          1. per-stock announcements from the historical archive (notices)
          2. live per-stock news (stock_news_em — only hits for recent windows)
          3. CCTV macro headlines as contextual filler when 1+2 are thin
        """
        flags: list[str] = []
        sim = get_sim_date()
        if self.market == "cn":
            try:
                items = cn_source.fetch_news(self.ticker, curr_date, lookback_days)
            except DataUnavailable:
                items = []
            notices = self._archive_notices(curr_date, lookback_days)
            items = notices + items
            if len(items) < 4 and self.macro_news_enabled:
                # Opt-in only: low signal, and political content trips GLM's
                # content filter (1301), which aborts the whole session.
                items = items + self._archive_macro(curr_date, lookback_days)[:3]
            if notices:
                flags.append(f"news: archive backfill ({len(notices)} announcements)")
        else:
            # Prefer the session archive (bulk-fetched per month in
            # prefetch_news_archive). Falling back to a live window read keeps
            # this usable when the archive was never primed.
            items = self._archive_notices(curr_date, lookback_days)
            if not items:
                start = (pd.Timestamp(curr_date) - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")
                try:
                    items = us_source.fetch_news(self.ticker, start, curr_date)
                except Exception as exc:
                    # Never abort a session over flavour data: report and move on.
                    logger.warning("us news fetch failed for %s: %s", self.ticker, exc)
                    items = []
        # Gateway-level clamp on publish dates, regardless of vendor behavior.
        cutoff = pd.Timestamp(min(curr_date, sim)) + pd.Timedelta(days=1)
        safe = [n for n in items if n.get("date") and pd.Timestamp(n["date"]) < cutoff]
        if not safe:
            flags.append(f"news: none for window ({self.market})")
        return safe, flags

    @staticmethod
    def _normalize_yf_news(raw) -> list[dict]:
        out = []
        if isinstance(raw, str):
            return out  # sentinel strings from the data layer are not news
        for item in raw or []:
            if isinstance(item, dict):
                out.append({
                    "date": str(item.get("publish_time") or item.get("date") or "")[:10],
                    "title": str(item.get("title", ""))[:120],
                    "content": str(item.get("content", item.get("summary", "")))[:300],
                    "source": str(item.get("source", ""))[:40],
                })
        return out

    def benchmark_tail(self, curr_date: str, rows: int = 10) -> tuple[str | None, list[str]]:
        """Recent benchmark closes for regime context; None with a flag when absent."""
        flags: list[str] = []
        try:
            if self.market == "cn":
                if self._benchmark_frame is None:
                    end = min(curr_date, pd.Timestamp.today().strftime("%Y-%m-%d"))
                    start = (pd.Timestamp(curr_date) - pd.Timedelta(days=180)).strftime("%Y-%m-%d")
                    self._benchmark_frame = cn_source.fetch_index_daily(
                        cn_source.index_for_ticker(self.ticker), start, end
                    )
                bench = self._benchmark_frame
            else:
                from tradingagents.default_config import DEFAULT_CONFIG

                # NB: the key exists but defaults to None, so dict.get(key,
                # fallback) returns None rather than the fallback.
                idx = DEFAULT_CONFIG.get("benchmark_ticker") or "^GSPC"
                if self._benchmark_frame is None:
                    import yfinance as yf

                    start = (pd.Timestamp(curr_date) - pd.Timedelta(days=180)).strftime("%Y-%m-%d")
                    df = yf.download(idx, start=start, progress=False, auto_adjust=True)
                    if df is not None and not df.empty:
                        df = df.reset_index()
                        # yfinance returns MultiIndex columns ("Close", ticker)
                        # for recent versions; without flattening, df["Close"]
                        # is a DataFrame and formatting a row raises.
                        if isinstance(df.columns, pd.MultiIndex):
                            df.columns = df.columns.get_level_values(0)
                        df["Date"] = pd.to_datetime(df["Date"])
                        self._benchmark_frame = df[["Date", "Close"]].astype({"Close": "float64"})
                    else:
                        self._benchmark_frame = pd.DataFrame(columns=["Date", "Close"])
                bench = self._benchmark_frame
            if bench is None or bench.empty:
                flags.append("benchmark: unavailable")
                return None, flags
            cutoff = min(curr_date, get_sim_date())
            tail = bench[bench["Date"] <= pd.Timestamp(cutoff)].tail(rows)
            if tail.empty:
                flags.append("benchmark: unavailable")
                return None, flags
            return ", ".join(
                f"{d.strftime('%m-%d')}:{c:.2f}"
                for d, c in zip(tail["Date"], tail["Close"], strict=True)
            ), flags
        except Exception as exc:
            flags.append(f"benchmark: unavailable ({type(exc).__name__})")
            return None, flags
