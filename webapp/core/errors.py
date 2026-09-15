"""Webapp-specific error taxonomy."""


class BacktestError(Exception):
    """Base class for webapp errors."""


class LLMBudgetExceeded(BacktestError):
    """Session hit its per-day or total LLM call budget."""


class DataUnavailable(BacktestError):
    """A required data source returned nothing usable."""


class InvalidDecision(BacktestError):
    """LLM decision cannot be executed against the current portfolio."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
