"""Foundational value models; these records never execute commands or access files."""

from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"{prefix}{suffix}" for prefix in ("COM", "LPT") for suffix in "123456789¹²³"}
)


class Record(BaseModel):
    """Reject unknown fields and reassignment on shared records."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactRef(Record):
    """A content reference with a portable, syntactically safe relative path."""

    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        """Reject traversal and platform-specific path ambiguities without I/O."""
        parts = value.split("/")
        if (
            PurePosixPath(value).is_absolute()
            or any(part in {"", ".", ".."} for part in parts)
            or any(char in value for char in '\\<>:"|?*')
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or any(part.endswith((".", " ")) for part in parts)
            or any(part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES for part in parts)
        ):
            raise ValueError("artifact path must be a portable relative POSIX path")
        return value


class RepositoryRevision(Record):
    """A source snapshot identified by a complete Git commit ID."""

    repository_url: str = Field(min_length=1)
    commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    source: ArtifactRef


class CommandSpec(Record):
    """Recorded argv, work directory, and explicit environment; not execution approval."""

    argv: tuple[str, ...] = Field(min_length=1)
    cwd: str = "/workspace"
    env: tuple[tuple[str, str], ...] = ()

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Require a program name and arguments representable by a process API."""
        if not value[0] or any("\0" in argument for argument in value):
            raise ValueError("argv must name a program and cannot contain NUL")
        return value

    @field_validator("cwd")
    @classmethod
    def validate_cwd(cls, value: str) -> str:
        """Keep the recorded directory within the container workspace."""
        if value != "/workspace":
            if not value.startswith("/workspace/"):
                raise ValueError("cwd must be inside /workspace")
            ArtifactRef.validate_path(value.removeprefix("/workspace/"))
        return value

    @field_validator("env")
    @classmethod
    def validate_env(cls, value: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        """Reject duplicate keys and environment values invalid for process APIs."""
        names: set[str] = set()
        for name, content in value:
            if not name or "=" in name or "\0" in name or "\0" in content or name in names:
                raise ValueError("environment names must be unique and process-safe")
            names.add(name)
        return value


class SandboxPolicy(Record):
    """Requested test limits, subject to a future independent local PolicyGate."""

    isolation: Literal["reviewed_docker", "disposable_vm_docker"]
    network: Literal["none"] = "none"
    cpus: float = Field(default=1.0, gt=0, le=4, allow_inf_nan=False)
    memory_bytes: int = Field(default=2_147_483_648, gt=0, le=8_589_934_592)
    pids: int = Field(default=128, ge=1, le=512)
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    output_bytes_limit: int = Field(default=10_485_760, ge=1, le=67_108_864)
    workspace_bytes_limit: int = Field(default=536_870_912, ge=1, le=1_073_741_824)


class Termination(StrEnum):
    """Externally observed termination, separate from the process exit code."""

    EXITED = "exited"
    TIMEOUT = "timeout"
    OOM = "oom"
    OUTPUT_LIMIT = "output_limit"
    WORKSPACE_LIMIT = "workspace_limit"
    CANCELLED = "cancelled"
    INFRA_ERROR = "infra_error"


class ResourceUsage(Record):
    """Observed enforcement and optional metrics; absent measurements stay null."""

    memory_limit_bytes: int = Field(gt=0)
    cpu_limit: float = Field(gt=0)
    pids_limit: int = Field(gt=0)
    cgroup_verified: bool
    oom_killed: bool = False
    memory_sample_bytes: int | None = Field(default=None, ge=0)
    cpu_sample_percent: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    pids_sample: int | None = Field(default=None, ge=0)
    metric_samples: int = Field(default=0, ge=0)
    metrics_source: Literal["docker_stats", "unavailable"] = "unavailable"


class ExecutionResult(Record):
    """Structured evidence for one command attempt, including failed starts."""

    schema_version: Literal["1.0"] = "1.0"
    id: UUID
    run_id: UUID
    plan_id: UUID
    phase: Literal["resolve", "build", "install", "inventory", "test"]
    repetition: int = Field(ge=1)
    revision: RepositoryRevision
    environment_id: UUID | None = None
    command: CommandSpec
    backend_snapshot: ArtifactRef
    exit_code: int | None = None
    termination: Termination
    stdout: ArtifactRef
    stderr: ArtifactRef
    started_at: AwareDatetime
    finished_at: AwareDatetime
    duration_seconds: float = Field(ge=0, allow_inf_nan=False)
    logs_complete: bool
    pytest_report: ArtifactRef | None = None
    resource_usage: ResourceUsage | None = None

    @property
    def test_report(self) -> ArtifactRef | None:
        """Framework-neutral access to the v1 wire field named pytest_report."""
        return self.pytest_report

    @property
    def timed_out(self) -> bool:
        return self.termination is Termination.TIMEOUT

    @model_validator(mode="after")
    def validate_completion(self) -> Self:
        """Require coherent timestamps and an exit code for normal exits."""
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        if self.termination is Termination.EXITED and self.exit_code is None:
            raise ValueError("a normally exited process must have an exit code")
        return self


class Verdict(StrEnum):
    """Verification states; no verifier is implemented by this module."""

    VERIFIED = "verified"
    BASELINE_OBSERVED = "baseline_observed"
    NOT_REPRODUCED = "not_reproduced"
    INCONCLUSIVE = "inconclusive"
