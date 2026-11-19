"""Typed, provenance-aware Issue and static repository analysis records."""

from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator

from repo_repro.core.models import ArtifactRef, Record


class EvidenceOrigin(StrEnum):
    API_FACT = "api_fact"
    STATIC_FACT = "static_fact"
    REPORTER_STATEMENT = "reporter_statement"
    REPORTER_SPECULATION = "reporter_speculation"
    HEURISTIC = "heuristic"
    AI_INFERENCE = "ai_inference"


class EvidenceItem(Record):
    value: str
    origin: EvidenceOrigin
    source: str


class ReproductionStep(Record):
    order: int = Field(ge=1)
    description: str
    origin: Literal["reporter_statement"] = "reporter_statement"


class EnvironmentInfo(Record):
    go_module: str | None = None
    runtime_requirements: tuple[str, ...] = ()
    python_requirements: tuple[str, ...] = ()
    dependency_files: tuple[str, ...] = ()
    package_managers: tuple[str, ...] = ()
    test_frameworks: tuple[str, ...] = ()
    reported_details: tuple[EvidenceItem, ...] = ()
    evidence: tuple[EvidenceItem, ...] = ()

    @field_validator("dependency_files")
    @classmethod
    def validate_dependency_paths(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        for path in paths:
            ArtifactRef.validate_path(path)
        return paths


class BugDescription(Record):
    summary: str
    expected_behavior: str | None = None
    actual_behavior: str | None = None
    reproduction_steps: tuple[ReproductionStep, ...] = ()
    error_logs: tuple[EvidenceItem, ...] = ()


class RepositoryInfo(Record):
    schema_version: Literal["1.0"] = "1.0"
    repository_url: str
    origin_url: str | None = None
    repository_identity_verified: bool = False
    default_branch: str | None
    current_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    analyzed_ref: str
    files: tuple[str, ...]
    environment: EnvironmentInfo
    test_directories: tuple[str, ...] = ()
    test_files: tuple[str, ...] = ()
    entry_points: tuple[EvidenceItem, ...] = ()
    evidence: tuple[EvidenceItem, ...] = ()
    warnings: tuple[str, ...] = ()

    @field_validator("files", "test_files", "test_directories")
    @classmethod
    def validate_paths(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        for path in paths:
            ArtifactRef.validate_path(path)
        return paths


class IssueContext(Record):
    schema_version: Literal["1.0"] = "1.0"
    issue_url: str
    repository_url: str
    number: int = Field(ge=1)
    title: str
    body: str
    labels: tuple[str, ...]
    source: Literal["github", "local_json"]
    body_trusted: Literal[False] = False
    bug: BugDescription
    environment: EnvironmentInfo
    api_facts: tuple[EvidenceItem, ...] = ()
    reporter_statements: tuple[EvidenceItem, ...] = ()
    reporter_speculations: tuple[EvidenceItem, ...] = ()
    ai_inferences: tuple[EvidenceItem, ...] = ()
    related_code_files: tuple[EvidenceItem, ...] = ()
    possible_test_files: tuple[EvidenceItem, ...] = ()
    rejected_paths: tuple[str, ...] = ()
    repository: RepositoryInfo | None = None
