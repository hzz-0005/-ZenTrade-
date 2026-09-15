"""Request/response models for the REST API."""
from __future__ import annotations

from pydantic import BaseModel, Field

from webapp.core.models import DecisionArchitecture


class CreateSessionRequest(BaseModel):
    ticker: str
    start_date: str
    end_date: str
    initial_capital: float = Field(gt=0)
    commission_rate: float | None = None
    decision_architecture: DecisionArchitecture = "adaptive"
    use_full_graph: bool | None = None


class CreateSkillRequest(BaseModel):
    category: str
    statement: str = Field(min_length=1, max_length=240)


class UpdateSkillRequest(BaseModel):
    enabled: bool | None = None
    statement: str | None = Field(default=None, min_length=1, max_length=240)
    category: str | None = None
