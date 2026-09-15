"""Disk-backed cache for dataflow vendor calls.

The multi-agent pipeline makes the *same* vendor call over and over: each of
the 4 analysts re-fetches the same ticker news, and a backtest that steps
day-by-day re-fetches the same historical window on every decision day. Each
hit is a network round-trip (yfinance/akshare/Alpha Vantage) and, on the
rate-limited free tiers, a quota unit.

This module gives ``route_to_vendor`` a deterministic on-disk cache keyed by
``(method, args, kwargs)``. The first call in a process (and across processes,
thanks to the on-disk store) does the real fetch; every later call for the
*same* inputs returns the cached payload with zero network. That is exactly
the "prefetch the whole window once, replay it per day" behaviour the user
wanted — without changing the pipeline topology.

Safety properties:
  * Only results that are deterministic for a fixed ``(method, args)`` are
    cached. Anything that depends on "now" (live search, live social feeds,
    prediction markets, ticker.info snapshots) is *never* cached, and is
    listed in ``UNCACHEABLE_METHODS``.
  * Date-bounded queries (``get_news``, ``get_stock_data``, fundamentals with
    ``curr_date``) carry their dates in the key, so a backtest day ``T`` and
    day ``T+1`` with different windows are distinct cache entries — no
    look-ahead bleed across days.
  * The store is a plain JSON file under ``data_cache_dir``, guarded by a
    process-wide lock so concurrent analysts don't clobber each other.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from .config import get_config

logger = logging.getLogger(__name__)

# Methods whose result depends on the current moment, not only on their
# arguments. Caching these would freeze "live" data and — worse — replay
# future news into historical backtest days (look-ahead). They are always
# bypassed. get_global_news is effectively live search and also excluded.
UNCACHEABLE_METHODS = frozenset({
    "get_global_news",
    "get_prediction_markets",
})

# Module-level state (one cache per process).
_store_path: Path | None = None
_store: dict[str, Any] = {}
_dirty: bool = False
_loaded: bool = False
_lock = threading.RLock()


def _stable_key(method: str, args: tuple, kwargs: dict, namespace: str = "") -> str:
    """Hash ``(method, args, kwargs)`` into a stable string key.

    ``kwargs`` is sorted so ``f(a=1, b=2)`` and ``f(b=2, a=1)`` hit the same
    entry. ``None`` and ``"None"`` are kept distinct by JSON-encoding them.
    """
    payload = json.dumps(
        {"method": method, "args": list(args), "kwargs": kwargs,
         "namespace": namespace},
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"{method}:{digest}"


def _cache_file() -> Path:
    global _store_path
    if _store_path is not None:
        return _store_path
    cfg = get_config()
    cache_dir = cfg.get("data_cache_dir")
    if not cache_dir:
        # No cache dir configured -> caching disabled (no-op path).
        _store_path = Path("")  # sentinel; get/set check truthiness
        return _store_path
    path = Path(cache_dir) / "dataflow_cache.json"
    _store_path = path
    return path


def _load() -> None:
    global _store, _dirty, _loaded
    path = _cache_file()
    if not path:
        return
    if _loaded:
        return
    try:
        if path.exists():
            _store = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Could not read dataflow cache %s: %s", path, exc)
        _store = {}
    _dirty = False
    _loaded = True


def _flush() -> None:
    global _dirty
    path = _cache_file()
    if not path or not _dirty:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_store, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        _dirty = False
    except OSError as exc:
        logger.warning("Could not write dataflow cache %s: %s", path, exc)


def is_cacheable(method: str, args: tuple) -> bool:
    """Whether ``method``'s result can be cached for these args."""
    # A ticker's fundamentals snapshot (Ticker.info) has no date arg and is a
    # "current" snapshot — caching it is fine within a run (it is cheap to
    # re-derive later), but it is *not* date-stable across a historical
    # backtest. We still cache it: same ticker, same process == same snapshot,
    # which avoids N repeated info() calls. Cross-day staleness is irrelevant
    # because the info snapshot is already "today" regardless of sim date.
    return method not in UNCACHEABLE_METHODS


def get(method: str, args: tuple, kwargs: dict, namespace: str = "") -> Any | None:
    """Return a cached payload, or ``None`` on a miss."""
    if not is_cacheable(method, args):
        return None
    with _lock:
        _load()
        key = _stable_key(method, args, kwargs, namespace)
        if key in _store:
            return _store[key]
        return None


def put(method: str, args: tuple, kwargs: dict, value: Any,
        namespace: str = "") -> None:
    """Store ``value`` under ``(method, args, kwargs)`` and flush to disk."""
    if not is_cacheable(method, args):
        return
    # Only cache plain strings — every vendor tool in this codebase returns a
    # formatted string (CSV/markdown). Reject anything non-serializable or a
    # sentinel "no data" string so we don't pin a transient empty result.
    if not isinstance(value, str):
        return
    if value.startswith(("NO_DATA_AVAILABLE", "DATA_UNAVAILABLE")):
        return
    with _lock:
        _load()
        key = _stable_key(method, args, kwargs, namespace)
        if _store.get(key) != value:
            global _dirty
            _store[key] = value
            _dirty = True
            _flush()


def clear() -> None:
    """Drop the in-memory cache (test/debug helper)."""
    global _store, _dirty, _loaded
    with _lock:
        _store = {}
        _dirty = False
        # A clear is expected to remain clear for this process; do not silently
        # hydrate the just-cleared entries from the disk file on the next get.
        _loaded = True
