"""Execution contracts shared by trusted adapters and the orchestration layer."""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from repo_repro.analyzer.base import RepositoryAnalyzer
from repo_repro.core.models import ArtifactRef, CommandSpec, ExecutionResult
from repo_repro.reproduction.models import (
    PytestRunRequest,
    PytestSnapshotRequest,
    ReproductionInput,
    TestProposal,
)

# These request layouts are neutral; the old names remain supported by the v1 API.
TestRunRequest = PytestRunRequest
TestSnapshotRequest = PytestSnapshotRequest


@dataclass(frozen=True)
class TestBundle:
    language_id: str
    framework_id: str
    command: CommandSpec
    files: Mapping[str, bytes]
    test_path: str
    report_path: str
    probe_argv: tuple[str, ...]
    version_key: str

    def __post_init__(self) -> None:
        for path, content in self.files.items():
            ArtifactRef.validate_path(path)
            if not path.startswith("repo_repro_generated/") or not isinstance(content, bytes):
                raise ValueError(
                    "test bundle files must be bytes in the reserved support directory"
                )
        if self.test_path not in self.files:
            raise ValueError("test bundle is missing its selected test")
        if not self.report_path.startswith("/tmp/"):
            raise ValueError("test reports must stay in container temporary storage")
        ArtifactRef.validate_path(self.report_path.removeprefix("/tmp/"))
        if not self.probe_argv or not self.version_key.isidentifier():
            raise ValueError("framework probe and metadata key must be explicit")
        object.__setattr__(self, "files", MappingProxyType(dict(self.files)))

    @property
    def hashes(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (path, hashlib.sha256(content).hexdigest())
            for path, content in sorted(self.files.items())
        )


@runtime_checkable
class BundleBackend(Protocol):
    def run_bundle(self, request: TestRunRequest, bundle: TestBundle) -> ExecutionResult: ...


@runtime_checkable
class SnapshotBundleBackend(Protocol):
    def run_snapshot_bundle(
        self, request: TestSnapshotRequest, bundle: TestBundle
    ) -> ExecutionResult: ...


class TestBackend(Protocol):
    def run_test(self, request: TestRunRequest) -> ExecutionResult: ...


class SnapshotTestBackend(Protocol):
    def run_snapshot(self, request: TestSnapshotRequest) -> ExecutionResult: ...


class LanguageAdapter(Protocol):
    @property
    def id(self) -> str: ...
    @property
    def source_suffixes(self) -> tuple[str, ...]: ...
    @property
    def default_framework(self) -> str: ...
    @property
    def analyzer(self) -> RepositoryAnalyzer: ...
    @property
    def planning_instructions(self) -> str: ...

    def detect(self, paths: tuple[str, ...]) -> bool: ...
    def selectable(self, path: str) -> bool: ...
    def validate_source_path(self, path: str) -> None: ...
    def default_sources(
        self, paths: tuple[str, ...], tests: tuple[str, ...]
    ) -> tuple[str, ...]: ...
    def inventory_command(self) -> tuple[str, ...]: ...
    def validate_inventory(self, value: Mapping[str, object]) -> dict[str, object]: ...
    def fingerprint_identity(self, probe: Mapping[str, object]) -> dict[str, object]: ...
    def runtime_version(self, probe: Mapping[str, object]) -> str: ...


class TestFrameworkAdapter(Protocol):
    @property
    def id(self) -> str: ...
    @property
    def language_id(self) -> str: ...
    @property
    def generation_prompt(self) -> str: ...
    @property
    def display_name(self) -> str: ...
    @property
    def capsule_versions(self) -> tuple[str, ...]: ...

    def validate_context(self, context: ReproductionInput) -> None: ...
    def validate_code(self, code: str) -> None: ...
    def validate_proposal(self, proposal: TestProposal, context: ReproductionInput) -> None: ...
    def bundle(self, code: str) -> TestBundle: ...
    def canonical_test(self, code: str) -> str: ...
    def normalize_evidence(self, report: bytes) -> "SessionEvidence": ...


@dataclass(frozen=True)
class PhaseEvidence:
    target_id: str
    phase: str
    outcome: str
    expected_failure: bool
    exception_type: str | None
    message: str | None
    truncated: bool


@dataclass(frozen=True)
class SessionEvidence:
    framework_id: str
    collected: tuple[str, ...]
    collection_complete: bool
    collection_errors: tuple[str, ...]
    phases: tuple[PhaseEvidence, ...]
    session_complete: bool
    exit_code: int


@dataclass(frozen=True)
class AdapterSelection:
    language: LanguageAdapter
    framework: TestFrameworkAdapter

    def __post_init__(self) -> None:
        if self.framework.language_id != self.language.id:
            raise ValueError("framework and language adapters are incompatible")
