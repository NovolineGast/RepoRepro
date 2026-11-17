"""Execution backend port shared by Docker and future stronger isolation backends."""

from pathlib import Path
from typing import Protocol

from repo_repro.adapters.contracts import (
    BundleBackend as BundleBackend,
)
from repo_repro.adapters.contracts import (
    SnapshotBundleBackend as SnapshotBundleBackend,
)
from repo_repro.core.models import CommandSpec, ExecutionResult, SandboxPolicy


class SandboxBackend(Protocol):
    def run(
        self,
        repository_path: Path,
        command: CommandSpec,
        *,
        timeout: int | None = None,
        resource_limits: SandboxPolicy,
    ) -> ExecutionResult: ...

    def run_install(
        self,
        repository_path: Path,
        command: CommandSpec,
        *,
        timeout: int | None = None,
        resource_limits: SandboxPolicy,
    ) -> ExecutionResult: ...
