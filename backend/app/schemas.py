from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


RunStatus = Literal["pending", "running", "completed", "failed", "canceled"]
StageStatus = Literal["pending", "running", "completed", "failed"]
NodeStatus = Literal["pending", "running", "completed", "failed", "skipped"]
NodeType = Literal["expert", "review", "synthesis"]
StageName = Literal["experts", "peer_review", "synthesis"]


class ExpertSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    model: str = Field(min_length=1)
    persona: str = Field(min_length=1)
    system_prompt: str = Field(min_length=1)
    enabled: bool = True
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1)
    timeout_seconds: int = Field(default=120, ge=1, le=600)


class CouncilConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(default="")
    experts: list[ExpertSpec] = Field(default_factory=list)
    review_prompt_template: str = Field(min_length=1)
    synthesis_model: str = Field(min_length=1)
    synthesis_prompt_template: str = Field(min_length=1)
    synthesis_temperature: float | None = Field(default=None, ge=0, le=2)
    synthesis_max_tokens: int | None = Field(default=None, ge=1)
    synthesis_timeout_seconds: int = Field(default=180, ge=1, le=900)
    version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_experts(self) -> "CouncilConfig":
        ids = [expert.id for expert in self.experts]
        if len(ids) != len(set(ids)):
            raise ValueError("Expert ids must be unique.")
        if not any(expert.enabled for expert in self.experts):
            raise ValueError("At least one expert must be enabled.")
        return self


class ExpertOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expert_id: str
    expert_label: str
    model: str
    persona: str
    answer: str
    claims: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)


class AnonymizedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    expert_id: str
    expert_label: str
    answer: str
    claims: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)


class ReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer_id: str
    reviewer_label: str
    model: str
    persona: str
    ranking_labels: list[str] = Field(default_factory=list)
    ranking_expert_ids: list[str] = Field(default_factory=list)
    best_overall_expert_id: str | None = None
    best_for_architecture_expert_id: str | None = None
    best_for_execution_expert_id: str | None = None
    best_for_clarity_expert_id: str | None = None
    summary: str
    merge_recommendations: list[str] = Field(default_factory=list)
    critical_disagreements: list[str] = Field(default_factory=list)
    per_response_feedback: dict[str, str] = Field(default_factory=dict)


class AggregateReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ranking_expert_ids: list[str] = Field(default_factory=list)
    scores: dict[str, int] = Field(default_factory=dict)
    best_overall_expert_id: str | None = None
    best_for_architecture_expert_id: str | None = None
    best_for_execution_expert_id: str | None = None
    best_for_clarity_expert_id: str | None = None
    merge_recommendations: list[str] = Field(default_factory=list)
    critical_disagreements: list[str] = Field(default_factory=list)
    summary: str = ""


class StageSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: StageName
    label: str
    status: StageStatus = "pending"
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    total_nodes: int = 0
    completed_nodes: int = 0
    failed_nodes: int = 0


class NodeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    stage: StageName
    node_type: NodeType
    label: str
    model: str
    persona: str | None = None
    status: NodeStatus = "pending"
    display_order: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    output_preview: str = ""
    output: Any | None = None
    error: str | None = None


class RunEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    run_id: str
    type: str
    stage: StageName | None = None
    node_id: str | None = None
    timestamp: datetime = Field(default_factory=utcnow)
    payload: dict[str, Any] = Field(default_factory=dict)


class RunSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    query: str
    status: RunStatus = "pending"
    latest_event_id: int = 0
    config_snapshot: CouncilConfig
    stage_snapshots: dict[str, StageSnapshot] = Field(default_factory=dict)
    node_snapshots: dict[str, NodeSnapshot] = Field(default_factory=dict)
    expert_outputs: list[ExpertOutput] = Field(default_factory=list)
    anonymized_responses: list[AnonymizedResponse] = Field(default_factory=list)
    review_outputs: list[ReviewOutput] = Field(default_factory=list)
    aggregate_review: AggregateReview | None = None
    final_answer: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class RunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    query: str
    status: RunStatus
    config_id: str
    config_name: str
    created_at: datetime
    updated_at: datetime
    final_answer_preview: str | None = None


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    config_id: str | None = None
    config: CouncilConfig | None = None


class CancelRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: RunStatus


class ConfigListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CouncilConfig]


class RunListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RunSummary]
