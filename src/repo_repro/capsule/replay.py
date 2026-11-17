"""Explicitly authorized replay using frozen data and the LOCAL trusted sandbox runner."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime

from repo_repro.adapters.contracts import AdapterSelection
from repo_repro.capsule.adapter_binding import v1_adapters
from repo_repro.capsule.archive import extract_capsule, validate_capsule
from repo_repro.capsule.images import DockerImageArchive, scan_image
from repo_repro.capsule.schema import CapsuleError, json_object
from repo_repro.capsule.security import SecretRedactor, source_files
from repo_repro.core.models import Record, SandboxPolicy
from repo_repro.reproduction.executor import ReproductionExecutor
from repo_repro.reproduction.models import (
    DifferentialResult,
    ExecutionEnvironment,
    GeneratedTest,
    ReproductionResult,
    ReproductionStatus,
    TestProposal,
)
from repo_repro.sandbox.docker_runner import DockerSandboxRunner
from repo_repro.storage.artifacts import FileArtifactStore
from repo_repro.verification.differential import compare


class ReplayReport(Record):
    schema_version: Literal["1.0.0"] = "1.0.0"
    capsule_id: UUID
    replay_id: UUID
    generated_at: AwareDatetime
    status: Literal["MATCHED", "DRIFTED", "INCONCLUSIVE", "ENVIRONMENT_ERROR"]
    behavior_match: bool
    environment_match: bool
    reference_status: str
    replay_status: str
    reason: str
    result: ReproductionResult | DifferentialResult


def _groups(result: ReproductionResult | DifferentialResult) -> tuple[ReproductionResult, ...]:
    return (result.original, result.fixed) if isinstance(result, DifferentialResult) else (result,)


def compare_replay(
    reference: ReproductionResult | DifferentialResult,
    current: ReproductionResult | DifferentialResult,
) -> tuple[bool, bool]:
    left, right = _groups(reference), _groups(current)
    behavior = (
        type(reference) is type(current)
        and reference.status == current.status
        and len(left) == len(right)
    )
    for old, new in zip(left, right, strict=False):
        behavior &= (
            old.status == new.status
            and old.flaky == new.flaky
            and (
                [(item.status, item.failure_signature) for item in old.assessments]
                == [(item.status, item.failure_signature) for item in new.assessments]
            )
        )
    environment = len(left) == len(right) and all(
        bool(old.environment_fingerprints)
        and old.environment_fingerprints == new.environment_fingerprints
        for old, new in zip(left, right, strict=False)
    )
    return behavior, environment


class CapsuleReplay:
    def __init__(
        self, *, images: DockerImageArchive | None = None, adapters: AdapterSelection | None = None
    ) -> None:
        self.images = images or DockerImageArchive()
        self.adapters = v1_adapters(adapters)

    def replay(
        self,
        capsule: Path,
        workspace: Path,
        *,
        local_policy: SandboxPolicy,
        authorized: bool = False,
    ) -> ReplayReport:
        if not authorized:
            raise CapsuleError(
                "replay requires explicit caller authorization; inspect/extract never execute"
            )
        manifest = validate_capsule(capsule)
        if manifest.mode != "offline" or manifest.image is None or manifest.replay is None:
            raise CapsuleError("diagnostic-only capsules cannot be replayed")
        if workspace.exists() or workspace.is_symlink():
            raise CapsuleError("use a new replay workspace")
        workspace.mkdir(parents=True)
        root = workspace / "capsule"
        manifest = extract_capsule(capsule, root)
        assert manifest.image is not None and manifest.replay is not None
        reference_data = json_object((root / "reference/result.json").read_bytes())
        reference: ReproductionResult | DifferentialResult
        if isinstance(reference_data, dict) and "original" in reference_data:
            reference = DifferentialResult.model_validate(reference_data)
        else:
            reference = ReproductionResult.model_validate(reference_data)
        if reference.status.value != manifest.execution_status or (
            _groups(reference)[0].commit != manifest.original_commit
            or (reference.fixed_commit if isinstance(reference, DifferentialResult) else None)
            != manifest.fixed_commit
        ):
            raise CapsuleError("reference result contradicts the manifest")
        environment = ExecutionEnvironment.model_validate(
            json_object((root / "environment.json").read_bytes())
        )
        if (
            environment.image != manifest.image.image_id
            or environment.policy.timeout_seconds != manifest.timeout
        ):
            raise CapsuleError("runtime configuration contradicts the manifest")
        for field in (
            "cpus",
            "memory_bytes",
            "pids",
            "timeout_seconds",
            "output_bytes_limit",
            "workspace_bytes_limit",
        ):
            if getattr(local_policy, field) > getattr(environment.policy, field):
                raise CapsuleError("local replay policy cannot widen the captured resource limits")
        if (
            environment.policy.isolation == "disposable_vm_docker"
            and local_policy.isolation != "disposable_vm_docker"
        ):
            raise CapsuleError(
                "local policy cannot downgrade the captured VM deployment requirement"
            )
        code_bytes = (root / manifest.replay.test_path).read_bytes()
        if hashlib.sha256(code_bytes).hexdigest() != manifest.replay.test_sha256:
            raise CapsuleError("test payload differs from the sealed recipe")
        code = code_bytes.decode("utf-8")
        self.adapters.framework.validate_code(code)
        helpers = tuple(
            sorted(
                (name, hashlib.sha256(content).hexdigest())
                for name, content in self.adapters.framework.bundle(code).files.items()
            )
        )
        if helpers != manifest.replay.helper_hashes:
            raise CapsuleError("local trusted runtime is incompatible with captured helper hashes")
        redactor = SecretRedactor()
        redactor.critical(manifest.replay.model_dump_json().encode())
        snapshots: dict[str, bytes] = {}
        for source in manifest.sources:
            content = (root / source.path).read_bytes()
            files = source_files(content)
            for name, data in files.items():
                if name.startswith("repo_repro_generated/"):
                    raise CapsuleError("capsule source must not provide tool scripts")
                redactor.critical(name.encode())
                redactor.critical(data)
            snapshots[source.side] = content
        scan_image(root / manifest.image.path, redactor)
        self.images.ensure(manifest.image.image_id, root / manifest.image.path)
        store = FileArtifactStore(workspace / "artifacts")
        provenance = store.put_bytes(
            json.dumps(
                {
                    "origin": "capsule_replay",
                    "parent_capsule_id": str(manifest.capsule_id),
                    "test_sha256": manifest.replay.test_sha256,
                }
            ).encode(),
            media_type="application/json",
        )
        proposal = TestProposal.model_validate(
            {
                "understanding": "Replay a captured sealed test without model generation.",
                "plan": manifest.replay.oracle.behavior_contract,
                "closest_test": None,
                "test_code": code,
                "evidence": [{"source": "issue.body", "quote": manifest.replay.oracle.issue_quote}],
            }
        )
        generated = GeneratedTest(
            id=manifest.replay.test_id,
            code=code,
            sha256=manifest.replay.test_sha256,
            oracle=manifest.replay.oracle,
            proposal=proposal,
            generation_record=provenance,
            input_record=provenance,
        )
        environment = environment.model_copy(update={"policy": local_policy})
        backend = DockerSandboxRunner(
            image=manifest.image.image_id, artifacts=store, adapters=self.adapters
        )
        executor = ReproductionExecutor(
            backend, store, snapshot_backend=backend, adapters=self.adapters
        )
        outcomes: list[ReproductionResult] = []
        for source in manifest.sources:
            snapshot = store.put_bytes(snapshots[source.side], media_type="application/x-tar")
            outcomes.append(executor.run_snapshot(snapshot, source.commit, generated, environment))
        current: ReproductionResult | DifferentialResult = outcomes[0]
        if manifest.fixed_commit:
            status, reason = compare(outcomes[0], outcomes[1])
            current = DifferentialResult(
                id=uuid4(),
                original_commit=manifest.original_commit,
                fixed_commit=manifest.fixed_commit,
                test_sha256=generated.sha256,
                status=status,
                reason=reason,
                original=outcomes[0],
                fixed=outcomes[1],
                environment_fingerprints=outcomes[0].environment_fingerprints
                + outcomes[1].environment_fingerprints,
            )
        behavior, same_environment = compare_replay(reference, current)
        replay_status: Literal["MATCHED", "DRIFTED", "INCONCLUSIVE", "ENVIRONMENT_ERROR"]
        if any(item.status is ReproductionStatus.ENVIRONMENT_ERROR for item in outcomes):
            replay_status = "ENVIRONMENT_ERROR"
        elif any(
            item.flaky
            or item.status in {ReproductionStatus.INCONCLUSIVE, ReproductionStatus.TIMEOUT}
            for item in outcomes
        ):
            replay_status = "INCONCLUSIVE"
        else:
            replay_status = "MATCHED" if behavior and same_environment else "DRIFTED"
        report = ReplayReport(
            capsule_id=manifest.capsule_id,
            replay_id=uuid4(),
            generated_at=datetime.now(UTC),
            status=replay_status,
            behavior_match=behavior,
            environment_match=same_environment,
            reference_status=reference.status.value,
            replay_status=current.status.value,
            reason="Compare target behavior and runtime fingerprints; diagnostics are reference.",
            result=current,
        )
        (workspace / "replay-report.json").write_text(
            report.model_dump_json(indent=2), encoding="utf-8"
        )
        (workspace / "replay-report.md").write_text(
            f"# Capsule replay\n\nStatus: {report.status}\n\n"
            f"Reference: {report.reference_status}\n\nNew result: {report.replay_status}\n\n"
            f"Behavior match: {behavior}\n\nEnvironment match: {same_environment}\n",
            encoding="utf-8",
        )
        return report
