from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ProposalCreateIn(BaseModel):
    ticket_id: int = Field(..., gt=0)
    action_type: str = Field(..., min_length=2, max_length=50)
    payload: dict[str, Any] = Field(default_factory=dict)
    reasoning: str = Field(default="", max_length=1000)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class ProposalDecisionIn(BaseModel):
    approved: bool
    reason: str = Field(default="", max_length=500)


class ProposalOut(BaseModel):
    id: int
    ticket_id: int
    action_type: str
    payload: dict[str, Any]
    reasoning: str
    confidence: float
    status: str
    created_by: str
    decided_by: str | None = None
    decision_reason: str | None = None
    decided_at: datetime | None = None
    execution_result: dict[str, Any] | None = None
    created_at: datetime
