"""Contracts for bounded test generation and evidence-based reproduction."""

from enum import StrEnum
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from repo_repro.core.context import EnvironmentInfo, IssueContext, RepositoryInfo
from repo_repro.core.models import ArtifactRef, ExecutionResult, Record, SandboxPolicy

TEST_PATH = "repo_repro_generated/test_repro.py"
NODE_ID = TEST_PATH + "::test_repro"


class SourceFile(Record):
    path: str
    content: str = Field(max_length=65_536)

    @field_validator("path")
    @classmethod
    def safe_source_path(cls, value: str) -> str:
        ArtifactRef.validate_path(value)
        if not value.endswith((".py", ".go")):
            raise ValueError("only explicit supported source/test files are accepted")
        return value

    @classmethod
    def safe_python_path(cls, value: str) -> str:
        ArtifactRef.validate_path(value)
        if not value.endswith(".py"):
            raise ValueError("only explicit Python source/test files are accepted")
        return value


class FailureOracle(Record):
    """Caller-owned contract, sealed before the model call; the model cannot change it."""

    exception_type: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.]*$")
    message_contains: str = Field(min_length=1, max_length=2000)
    issue_quote: str = Field(min_length=1, max_length=4000)
    behavior_contract: str = Field(min_length=1, max_length=4000)
    language: Literal["python", "go"] = "python"
    node_id: str = "repo_repro_generated/test_repro.py::test_repro"

    @model_validator(mode="after")
    def target_identity(self) -> Self:
        expected = NODE_ID if self.language == "python" else "TestRepro"
        if self.node_id != expected:
            raise ValueError("oracle target does not match its language")
        if self.language == "go" and self.exception_type != "GoAssertionFailure":
            raise ValueError("Go requires the return-value assertion oracle")
        return self


class ReproductionInput(Record):
    issue: IssueContext
    repository: RepositoryInfo
    environment: EnvironmentInfo
    relevant_source_files: tuple[SourceFile, ...] = Field(min_length=1, max_length=20)
    existing_tests: tuple[SourceFile, ...] = Field(max_length=20)
    oracle: FailureOracle


class EvidenceQuote(Record):
    source: str = Field(
        description="Exactly 'issue.body' or a supplied file path; no section suffix."
    )
    quote: str = Field(
        min_length=1,
        max_length=4000,
        description="A short exact substring of that source after one JSON decoding.",
    )


class TestProposal(Record):
    understanding: str = Field(min_length=1, max_length=8000)
    plan: str = Field(min_length=1, max_length=8000)
    closest_test: str | None = Field(
        description="An existing test FILE PATH only, or null; never code or a node ID."
    )
    test_code: str = Field(
        min_length=1,
        max_length=32_768,
        description="Test source with decoded newlines in the selected framework's safe subset.",
    )
    evidence: tuple[EvidenceQuote, ...] = Field(min_length=1, max_length=20)


class GeneratedTest(Record):
    id: UUID
    code: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    oracle: FailureOracle
    proposal: TestProposal
    generation_record: ArtifactRef
    input_record: ArtifactRef


class ExecutionEnvironment(Record):
    image: str = Field(pattern=r"^(?:[A-Za-z0-9./:_-]+@)?sha256:[0-9a-f]{64}$")
    policy: SandboxPolicy
    repetitions: int = Field(default=2, ge=2, le=10)


class PytestPhase(Record):
    node_id: str
    phase: Literal["setup", "call", "teardown"]
    outcome: Literal["passed", "failed", "skipped"]
    was_xfail: bool
    exception_type: str | None
    message: str | None
    traceback: str | None
    truncated: bool = False


class PytestEvidence(Record):
    schema_version: Literal["1.0"]
    pytest_version: str
    collected: tuple[str, ...]
    collection_complete: bool
    collection_errors: tuple[str, ...]
    phases: tuple[PytestPhase, ...]
    session_complete: bool
    exit_code: int


class ReproductionStatus(StrEnum):
    REPRODUCED = "REPRODUCED"
    NOT_REPRODUCED = "NOT_REPRODUCED"
    INCONCLUSIVE = "INCONCLUSIVE"
    ENVIRONMENT_ERROR = "ENVIRONMENT_ERROR"
    TIMEOUT = "TIMEOUT"


class AttemptAssessment(Record):
    execution_id: UUID
    status: ReproductionStatus
    reason: str
    failure_signature: str | None = None


class ReproductionResult(Record):
    run_id: UUID
    generated_test: GeneratedTest
    commit: str
    environment: ExecutionEnvironment
    status: ReproductionStatus
    flaky: bool
    reason: str
    executions: tuple[ExecutionResult, ...]
    assessments: tuple[AttemptAssessment, ...]
    environment_fingerprints: tuple[str, ...] = ()


class DifferentialStatus(StrEnum):
    DIFFERENTIAL_VALIDATED = "DIFFERENTIAL_VALIDATED"
    REGRESSION_DETECTED = "REGRESSION_DETECTED"
    ORIGINAL_NOT_REPRODUCED = "ORIGINAL_NOT_REPRODUCED"
    FIXED_STILL_FAILS = "FIXED_STILL_FAILS"
    INCONCLUSIVE = "INCONCLUSIVE"
    ENVIRONMENT_ERROR = "ENVIRONMENT_ERROR"


class DifferentialResult(Record):
    id: UUID
    original_commit: str
    fixed_commit: str
    test_sha256: str
    status: DifferentialStatus
    reason: str
    original: ReproductionResult
    fixed: ReproductionResult
    environment_fingerprints: tuple[str, ...]


class PytestRunRequest(Record):
    repository_path: Path
    commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    test_code: str = Field(min_length=1, max_length=32_768)
    policy: SandboxPolicy
    run_id: UUID
    plan_id: UUID
    repetition: int = Field(ge=1)


class PytestSnapshotRequest(Record):
    source: ArtifactRef
    commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    test_code: str = Field(min_length=1, max_length=32_768)
    policy: SandboxPolicy
    run_id: UUID
    plan_id: UUID
    repetition: int = Field(ge=1)
