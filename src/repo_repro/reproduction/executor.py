"""Bounded repeated execution; every command is delegated to a sandbox backend."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from repo_repro.adapters.contracts import (
    AdapterSelection,
    BundleBackend,
    SnapshotBundleBackend,
    SnapshotTestBackend,
    TestBackend,
    TestRunRequest,
    TestSnapshotRequest,
)
from repo_repro.adapters.registry import default_selection
from repo_repro.analyzer.repository import GitRepository
from repo_repro.core.exceptions import ExecutionBackendError, PolicyViolation, RepoReproError
from repo_repro.core.models import ArtifactRef, ExecutionResult, Termination
from repo_repro.reproduction.models import (
    AttemptAssessment,
    ExecutionEnvironment,
    GeneratedTest,
    ReproductionResult,
    ReproductionStatus,
)
from repo_repro.storage.artifacts import FileArtifactStore
from repo_repro.verification.verifier import assess_execution


class ReproductionExecutor:
    def __init__(
        self,
        backend: TestBackend | BundleBackend,
        artifacts: FileArtifactStore,
        *,
        snapshot_backend: SnapshotTestBackend | SnapshotBundleBackend | None = None,
        adapters: AdapterSelection | None = None,
    ) -> None:
        self.backend = backend
        self.artifacts = artifacts
        self.snapshot_backend = snapshot_backend
        self.adapters = adapters or default_selection()

    def run_snapshot(
        self,
        source: ArtifactRef,
        commit: str,
        generated: GeneratedTest,
        environment: ExecutionEnvironment,
    ) -> ReproductionResult:
        if self.snapshot_backend is None:
            raise PolicyViolation("backend does not support archive snapshots")
        return self.run(Path("."), commit, generated, environment, _source=source)

    def _fingerprint(
        self,
        execution: ExecutionResult,
        environment: ExecutionEnvironment,
        generated: GeneratedTest,
    ) -> str:
        data = json.loads(self.artifacts.read_bytes(execution.backend_snapshot))
        bundle = self.adapters.framework.bundle(generated.code)
        expected = list(bundle.hashes)
        actual = sorted(tuple(item) for item in data.get("injected_files", []))
        if (
            data.get("cleanup_confirmed") is not True
            or data.get("image_reference") != environment.image
        ):
            raise PolicyViolation("cleanup or selected image evidence is inconsistent")
        if data.get("policy") != environment.policy.model_dump():
            raise PolicyViolation("actual policy differs from the requested environment")
        if data.get("test_sha256") != generated.sha256 or actual != expected:
            raise PolicyViolation("the test or collector differs from the sealed bundle")
        if execution.resource_usage is None or not execution.resource_usage.cgroup_verified:
            raise PolicyViolation("runtime enforcement evidence is missing")
        if execution.command != bundle.command:
            raise PolicyViolation("unexpected reproduction command")
        probe = data.get("cgroup_probe")
        if not isinstance(probe, dict) or not data.get("image_id"):
            raise PolicyViolation("runtime identity is incomplete")
        identity = {
            "test_bundle": actual,
            "image": data["image_id"],
            "policy": data.get("policy"),
            "limits": [probe.get(key) for key in ("memory", "swap", "cpu", "pids")],
        }
        runtime = self.adapters.language.fingerprint_identity(probe)
        if set(identity).intersection(runtime):
            raise PolicyViolation("runtime identity collides with execution enforcement evidence")
        identity.update(runtime)
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    def run(
        self,
        repository_path: Path,
        commit: str,
        generated: GeneratedTest,
        environment: ExecutionEnvironment,
        *,
        _source: ArtifactRef | None = None,
    ) -> ReproductionResult:
        self.adapters.framework.validate_code(generated.code)
        if hashlib.sha256(generated.code.encode()).hexdigest() != generated.sha256 or (
            generated.code != generated.proposal.test_code
        ):
            raise PolicyViolation("generated test content/hash mismatch")
        self.artifacts.read_bytes(generated.generation_record)
        self.artifacts.read_bytes(generated.input_record)
        if _source is None:
            with GitRepository().open(repository_path, ref=commit) as view:
                pinned_commit = view.commit
        else:
            pinned_commit = commit
        run_id = uuid4()
        self.artifacts.put_bytes(
            json.dumps(
                {
                    "run_id": str(run_id),
                    "commit": pinned_commit,
                    "test_sha256": generated.sha256,
                    "environment": environment.model_dump(),
                    "generation": generated.generation_record.model_dump(),
                    "stage": "planned",
                }
            ).encode(),
            media_type="application/json",
        )
        executions: list[ExecutionResult] = []
        assessments: list[AttemptAssessment] = []
        fingerprints: list[str] = []
        failed: str | None = None
        for repetition in range(1, environment.repetitions + 1):
            try:
                request = TestRunRequest(
                    repository_path=repository_path,
                    commit=pinned_commit,
                    test_code=generated.code,
                    policy=environment.policy,
                    run_id=run_id,
                    plan_id=generated.id,
                    repetition=repetition,
                )
                if _source is None:
                    if isinstance(self.backend, BundleBackend):
                        result = self.backend.run_bundle(
                            request,
                            self.adapters.framework.bundle(generated.code),
                        )
                    else:
                        result = self.backend.run_test(request)
                else:
                    assert self.snapshot_backend is not None
                    snapshot_request = TestSnapshotRequest(
                        source=_source,
                        **request.model_dump(exclude={"repository_path"}),
                    )
                    if isinstance(self.snapshot_backend, SnapshotBundleBackend):
                        result = self.snapshot_backend.run_snapshot_bundle(
                            snapshot_request,
                            self.adapters.framework.bundle(generated.code),
                        )
                    else:
                        result = self.snapshot_backend.run_snapshot(snapshot_request)
                executions.append(result)
                self.artifacts.put_bytes(
                    result.model_dump_json().encode(), media_type="application/json"
                )
                if result.termination is Termination.CANCELLED:
                    raise KeyboardInterrupt
                if (
                    result.revision.commit != pinned_commit
                    or result.run_id != run_id
                    or result.plan_id != generated.id
                    or result.repetition != repetition
                ):
                    raise PolicyViolation("backend returned inconsistent execution identity")
                evidence = (
                    self.artifacts.read_bytes(result.test_report) if result.test_report else None
                )
                assessment = assess_execution(
                    result,
                    generated.oracle,
                    evidence,
                    framework=self.adapters.framework,
                )
                assessments.append(assessment)
                if result.termination is Termination.EXITED:
                    fingerprints.append(self._fingerprint(result, environment, generated))
            except ExecutionBackendError as error:
                executions.append(error.result)
                self.artifacts.put_bytes(
                    error.result.model_dump_json().encode(), media_type="application/json"
                )
                self.artifacts.put_bytes(
                    json.dumps(
                        {
                            "run_id": str(run_id),
                            "stage": "aborted_backend_failure",
                            "execution_id": str(error.result.id),
                            "error": str(error),
                        }
                    ).encode(),
                    media_type="application/json",
                )
                raise
            except (RepoReproError, ValueError, RecursionError) as error:
                failed = str(error)
                break
        signatures = {(item.status, item.failure_signature) for item in assessments}
        flaky = len(signatures) > 1
        if failed or len(assessments) != environment.repetitions:
            status, reason = ReproductionStatus.ENVIRONMENT_ERROR, failed or "incomplete attempts"
        elif any(item.status is ReproductionStatus.ENVIRONMENT_ERROR for item in assessments):
            status, reason = ReproductionStatus.ENVIRONMENT_ERROR, "environment or fixture failure"
        elif len(set(fingerprints)) > 1:
            status, reason = ReproductionStatus.ENVIRONMENT_ERROR, "runtime environment drift"
        elif flaky:
            status, reason = (
                ReproductionStatus.INCONCLUSIVE,
                "FLAKY: repeated outcomes/failures differ",
            )
        else:
            status, reason = assessments[0].status, assessments[0].reason
        outcome = ReproductionResult(
            run_id=run_id,
            generated_test=generated,
            commit=pinned_commit,
            environment=environment,
            status=status,
            flaky=flaky,
            reason=reason,
            executions=tuple(executions),
            assessments=tuple(assessments),
            environment_fingerprints=tuple(fingerprints),
        )
        self.artifacts.put_bytes(outcome.model_dump_json().encode(), media_type="application/json")
        return outcome
