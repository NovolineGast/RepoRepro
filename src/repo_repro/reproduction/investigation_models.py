"""AI investigation records, separate from the immutable execution and Capsule formats."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator

from repo_repro.core.models import ArtifactRef, Record, Termination
from repo_repro.reproduction.models import (
    DifferentialResult,
    EvidenceQuote,
    GeneratedTest,
    ReproductionInput,
    ReproductionResult,
    ReproductionStatus,
)

InvestigationStop = Literal[
    "reproduced",
    "budget_exhausted",
    "duplicate_candidate",
    "model_error",
    "environment_error",
    "timeout",
    "flaky",
    "incomplete_evidence",
]


class InvestigationPlan(Record):
    origin: Literal["ai_inference"] = "ai_inference"
    summary: str = Field(min_length=1, max_length=4000)
    expected_behavior: str = Field(min_length=1, max_length=4000)
    actual_behavior: str = Field(min_length=1, max_length=4000)
    preconditions: tuple[Annotated[str, Field(min_length=1, max_length=2000)], ...] = Field(
        max_length=10
    )
    hypothesis: str = Field(min_length=1, max_length=4000)
    source_paths: tuple[str, ...] = Field(min_length=1, max_length=10)
    test_paths: tuple[str, ...] = Field(max_length=10)
    evidence: tuple[EvidenceQuote, ...] = Field(min_length=1, max_length=20)

    @field_validator("source_paths", "test_paths")
    @classmethod
    def source_paths_only(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        for path in paths:
            ArtifactRef.validate_path(path)
            if not path.endswith((".py", ".go")):
                raise ValueError("investigation selects supported source files only")
        if len(set(paths)) != len(paths):
            raise ValueError("duplicate investigation paths")
        return paths


class PlannedInvestigation(Record):
    plan: InvestigationPlan
    context: ReproductionInput
    record: ArtifactRef
    audit: ArtifactRef
    model_calls: int = Field(default=1, ge=1, le=2)


class ExecutionObservation(Record):
    exit_code: int | None
    termination: Termination
    stdout: str = Field(max_length=4000)
    stderr: str = Field(max_length=4000)
    pytest_evidence: str = Field(max_length=12000)


class CandidateFeedback(Record):
    attempt: int = Field(ge=1, le=5)
    previous_test: str | None = Field(default=None, max_length=32768)
    validation_error: str | None = Field(default=None, max_length=4000)
    status: ReproductionStatus | None = None
    reason: str = Field(max_length=4000)
    observations: tuple[ExecutionObservation, ...] = Field(default=(), max_length=2)


class InvestigationAttempt(Record):
    number: int = Field(ge=1, le=5)
    generated: GeneratedTest | None = None
    result: ReproductionResult | None = None
    rejection: str | None = None
    feedback_record: ArtifactRef | None = None
    generation_record: ArtifactRef | None = None


class InvestigationResult(Record):
    schema_version: Literal["1.0"] = "1.0"
    id: UUID
    plan: InvestigationPlan
    plan_record: ArtifactRef
    attempts: tuple[InvestigationAttempt, ...] = Field(max_length=5)
    selected_result: ReproductionResult | None
    verification: DifferentialResult | None = None
    model_calls: int = Field(ge=1, le=7)
    max_candidates: int = Field(ge=1, le=5)
    max_plan_attempts: int = Field(default=2, ge=1, le=2)
    stop_reason: InvestigationStop
