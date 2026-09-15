"""Shared 5-tier rating vocabulary and a deterministic heuristic parser.

The same five-tier scale (Buy, Overweight, Hold, Underweight, Sell) is used by:
- The Research Manager (investment plan recommendation)
- The Portfolio Manager (final position decision)
- The signal processor (rating extracted for downstream consumers)
- The memory log (rating tag stored alongside each decision entry)

Centralising it here avoids drift between those call sites.
"""

from __future__ import annotations

import re
import unicodedata

# Canonical, ordered 5-tier scale (most bullish to most bearish).
RATINGS_5_TIER: tuple[str, ...] = (
    "Buy", "Overweight", "Hold", "Underweight", "Sell",
)

# Sentinel returned when a decision cannot be parsed into a 5-tier rating.
# Unlike a silent fallback to "Hold", this makes a parsing failure *visible*
# so downstream consumers can flag it for review instead of treating it as a
# tradeable neutral signal (#1170).
RATING_REVIEW = "REVIEW"

_RATING_SET = {r.lower() for r in RATINGS_5_TIER}

# Matches "Rating: X" / "rating - X" / "Rating: **X**" — tolerates markdown
# bold wrappers and either a colon or hyphen separator.
_RATING_LABEL_RE = re.compile(r"rating.*?[:\-][\s*]*(\w+)", re.IGNORECASE)


def extract_rating(text: str) -> str | None:
    """Extract a 5-tier rating, returning ``None`` on failure.

    Unlike :func:`parse_rating`, this never fabricates a default: an
    unrecognizable decision yields ``None`` so the caller can decide how to
    handle a genuine parsing failure (e.g. map it onto :data:`RATING_REVIEW`).
    """
    if not text:
        return None
    norm = unicodedata.normalize("NFKC", text)

    for line in norm.splitlines():
        m = _RATING_LABEL_RE.search(line)
        if m and m.group(1).lower() in _RATING_SET:
            return m.group(1).capitalize()

    for line in norm.splitlines():
        for word in line.lower().split():
            clean = word.strip("*:.,")
            if clean in _RATING_SET:
                return clean.capitalize()

    return None


def is_review(signal: str) -> bool:
    """True if ``signal`` is the :data:`RATING_REVIEW` sentinel."""
    return signal == RATING_REVIEW


def parse_rating(text: str, default: str = "Hold") -> str:
    """Heuristically extract a 5-tier rating from prose text.

    Two-pass strategy:
    1. Look for an explicit "Rating: X" label (tolerant of markdown bold).
    2. Fall back to the first 5-tier rating word found anywhere in the text.

    Returns a Title-cased rating string, or ``default`` if no rating word appears.

    Backwards-compatible wrapper over :func:`extract_rating`; callers that need
    to distinguish a parsing failure from a real ``Hold`` should use
    :func:`extract_rating` / :func:`is_review` instead.
    """
    return extract_rating(text) or default
