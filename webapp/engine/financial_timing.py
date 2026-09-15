"""Conservative financial-statement availability rules for backtests.

Vendor statement tables are current snapshots indexed by period end, not
point-in-time archives indexed by filing time.  A period ending on day T was
not public on T.  These functions deliberately use conservative publication
deadlines so a backtest may see one fewer report, but never gains an impossible
early look at a report merely because its period end is in the past.
"""
from __future__ import annotations

from pandas import DateOffset, Timestamp


def assumed_publication_date(period, market: str) -> Timestamp:
    """Earliest date a period is treated as public when filing time is absent."""
    p = Timestamp(period).normalize()
    if market == "cn":
        month_day = (p.month, p.day)
        if month_day == (3, 31):
            return Timestamp(year=p.year, month=4, day=30)
        if month_day == (6, 30):
            return Timestamp(year=p.year, month=8, day=31)
        if month_day == (9, 30):
            return Timestamp(year=p.year, month=10, day=31)
        if month_day == (12, 31):
            return Timestamp(year=p.year + 1, month=4, day=30)
        # Non-standard periods: avoid same-day visibility.
        return p + DateOffset(days=120)

    # yfinance does not expose a dependable filing timestamp alongside every
    # quarterly column.  45 days is a conservative large-accelerated-filer
    # assumption for quarterly data; annual tables are handled with 90 days by
    # ``is_period_available(..., annual=True)`` below.
    return p + DateOffset(days=45)


def is_period_available(period, as_of, market: str, *, annual: bool = False) -> bool:
    p = Timestamp(period).normalize()
    if annual and market != "cn":
        available = p + DateOffset(days=90)
    else:
        available = assumed_publication_date(p, market)
    return available <= Timestamp(as_of).normalize()


def available_periods(periods, as_of, market: str, *, annual: bool = False) -> list:
    """Return vendor column labels that were plausibly public by ``as_of``."""
    return [p for p in periods if is_period_available(p, as_of, market, annual=annual)]
