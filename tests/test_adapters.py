"""Adapter contracts, real dispatch and v1 compatibility; no extra language is shipped."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from repo_repro.adapters.contracts import (
    AdapterSelection,
    PhaseEvidence,
    SessionEvidence,
)
from repo_repro.adapters.contracts import (
    TestBundle as Bundle,
)
from repo_repro.adapters.contracts import (
    TestRunRequest as RunRequest,
)
from repo_repro.adapters.pytest import PytestAdapter
from repo_repro.adapters.python import PythonLanguageAdapter
from repo_repro.adapters.registry import AdapterRegistry, builtin_registry
from repo_repro.analyzer.base import RepositoryView
from repo_repro.analyzer.python_analyzer import PythonAnalyzer
from repo_repro.analyzer.service import RepositoryAnalysis
from repo_repro.capsule.adapter_binding import v1_adapters
from repo_repro.capsule.schema import CapsuleError
from repo_repro.core.context import EnvironmentInfo, RepositoryInfo
from repo_repro.core.exceptions import InputError, PolicyViolation
from repo_repro.core.models import CommandSpec, ExecutionResult, ResourceUsage, SandboxPolicy
from repo_repro.reproduction.executor import ReproductionExecutor
from repo_repro.reproduction.generator import TestGenerator
from repo_repro.reproduction.models import NODE_ID, ReproductionInput, ReproductionStatus
from repo_repro.reproduction.models import TestProposal as Proposal
from repo_repro.sandbox.docker_runner import DockerSandboxRunner
from repo_repro.storage.artifacts import FileArtifactStore
from repo_repro.verification.verifier import assess_execution
from tests.integration.test_reproduction import repository_pair
from tests.repro_support import CODE, context, execution
from tests.test_differential import IMAGE
from tests.test_sandbox import FakeDocker


def test_registry_has_only_python_pytest_and_rejects_unknown_identifiers() -> None:
    registry = builtin_registry()
    assert registry.describe() == (
        {"language": "python", "source_suffixes": (".py",), "frameworks": ("pytest",)},
    )
    assert registry.detect(("main.py",)).framework.id == "pytest"
    for language, framework in (("go", None), ("python", "vitest"), ("module:plugin", None)):
        with pytest.raises(InputError):
            registry.resolve(language, framework)
    with pytest.raises(InputError):
        registry.detect(("main.go",))


def test_registry_registration_is_explicit_and_duplicate_safe() -> None:
    registry = AdapterRegistry()
    with pytest.raises(InputError):
        registry.register_framework(PytestAdapter())
    registry.register_language(PythonLanguageAdapter())
    with pytest.raises(InputError):
        registry.register_language(PythonLanguageAdapter())
    registry.register_framework(PytestAdapter())
    with pytest.raises(InputError):
        registry.register_framework(PytestAdapter())


def test_bundle_files_are_immutable_and_constrained() -> None:
    bundle = PytestAdapter().bundle(CODE)
    with pytest.raises(TypeError):
        bundle.files[bundle.test_path] = b"changed"  # type: ignore[index]
    for changes in (
        {"report_path": "/etc/shadow"},
        {"files": {"../escape": b"x"}},
        {"test_path": "missing"},
        {"probe_argv": ()},
    ):
        with pytest.raises(ValueError):
            replace(bundle, **changes)


def test_v1_helper_bytes_and_command_are_unchanged() -> None:
    # Golden hashes from the committed pre-refactor v1 runner. Test code is intentionally excluded.
    expected = {
        "repo_repro_generated/launch.py": (
            "849886e106f8f39c26ebcc239e87e90db9fd3cbac55fbe367abc756e671e3fdd"
        ),
        "repo_repro_generated/plugin.py": (
            "33dbc865ba502313f14cff1719c632afd9175e941b15449c6cdead46b5ec39e9"
        ),
        "repo_repro_generated/pytest.ini": (
            "713f986ee48be06ad4bff2a700d39514a9f69e34b3547af52366c0a274e30e5b"
        ),
    }
    bundle = PytestAdapter().bundle(CODE)
    assert {path: digest for path, digest in bundle.hashes if path != bundle.test_path} == expected
    assert bundle.command == CommandSpec(
        argv=("python", "-I", "/workspace/repo_repro_generated/launch.py")
    )
    assert bundle.files[bundle.test_path] == CODE.encode()


class SpyAnalyzer(PythonAnalyzer):
    def inspect(
        self, view: RepositoryView, *, url: str, commit: str, default_branch: str | None, ref: str
    ) -> RepositoryInfo:
        info = super().inspect(view, url=url, commit=commit, default_branch=default_branch, ref=ref)
        return info.model_copy(update={"warnings": (*info.warnings, "selected analyzer invoked")})


def test_repository_analysis_uses_selected_analyzer(tmp_path: Path) -> None:
    source = tmp_path / "source"
    repository_pair(source)
    language = PythonLanguageAdapter()
    language.analyzer = SpyAnalyzer()
    info = RepositoryAnalysis(language).analyze(source, url="https://github.com/example/calculator")
    assert "selected analyzer invoked" in info.warnings


class ContractFramework(PytestAdapter):
    """Contract-only test double: not an implementation of another language/framework."""

    id = "contract"
    display_name = "contract"
    generation_prompt = "Return the contract test proposal as JSON."
    capsule_versions: tuple[str, ...] = ()

    def validate_context(self, context: ReproductionInput) -> None:
        if self.id not in context.environment.test_frameworks:
            raise InputError("contract test framework not selected")

    def validate_code(self, code: str) -> None:
        if code != "opaque candidate":
            raise PolicyViolation("invalid contract test")

    def validate_proposal(self, proposal: Proposal, context: ReproductionInput) -> None:
        self.validate_code(proposal.test_code)

    def canonical_test(self, code: str) -> str:
        return code.strip().lower()

    def bundle(self, code: str) -> Bundle:
        return Bundle(
            language_id="python",
            framework_id=self.id,
            command=CommandSpec(argv=("contract-runner", "repo_repro_generated/case.txt")),
            files={"repo_repro_generated/case.txt": code.encode()},
            test_path="repo_repro_generated/case.txt",
            report_path="/tmp/contract.json",
            probe_argv=("contract-probe",),
            version_key="contract_version",
        )

    def normalize_evidence(self, report: bytes) -> SessionEvidence:
        value = json.loads(report)
        if value.get("contract") is not True:
            raise ValueError("not contract evidence")
        return SessionEvidence(
            framework_id=self.id,
            collected=(NODE_ID,),
            collection_complete=True,
            collection_errors=(),
            phases=tuple(
                PhaseEvidence(
                    NODE_ID,
                    phase,
                    "failed" if phase == "call" else "passed",
                    False,
                    "AssertionError" if phase == "call" else None,
                    "returns zero" if phase == "call" else None,
                    False,
                )
                for phase in ("setup", "call", "teardown")
            ),
            session_complete=True,
            exit_code=1,
        )


class ContractBackend:
    def __init__(self, store: FileArtifactStore) -> None:
        self.store = store
        self.calls: list[Bundle] = []

    def run_bundle(self, request: RunRequest, bundle: Bundle) -> ExecutionResult:
        self.calls.append(bundle)
        item = execution()
        snapshot = {
            "cleanup_confirmed": True,
            "image_reference": IMAGE,
            "image_id": IMAGE,
            "policy": request.policy.model_dump(),
            "test_sha256": hashlib.sha256(request.test_code.encode()).hexdigest(),
            "injected_files": bundle.hashes,
            "cgroup_probe": {"python_version": "fixture", "packages": []},
        }
        return item.model_copy(
            update={
                "run_id": request.run_id,
                "plan_id": request.plan_id,
                "repetition": request.repetition,
                "revision": item.revision.model_copy(update={"commit": request.commit}),
                "command": bundle.command,
                "backend_snapshot": self.store.put_bytes(
                    json.dumps(snapshot).encode(), media_type="application/json"
                ),
                "pytest_report": self.store.put_bytes(
                    b'{"contract":true}', media_type="application/json"
                ),
                "resource_usage": ResourceUsage(
                    memory_limit_bytes=request.policy.memory_bytes,
                    cpu_limit=request.policy.cpus,
                    pids_limit=request.policy.pids,
                    cgroup_verified=True,
                ),
            }
        )


def test_generator_executor_and_verifier_dispatch_to_selected_framework(tmp_path: Path) -> None:
    source = tmp_path / "source"
    original, _ = repository_pair(source)
    store = FileArtifactStore(tmp_path / "evidence")
    registry = builtin_registry()
    registry.register_framework(ContractFramework())
    selected = registry.resolve("python", "contract")
    env = EnvironmentInfo(test_frameworks=("contract",))
    data = context().model_copy(update={"environment": env})
    generated = TestGenerator(store, adapters=selected).seal_code(data, "opaque candidate")
    backend = ContractBackend(store)
    from repo_repro.reproduction.models import ExecutionEnvironment

    environment = ExecutionEnvironment.model_validate(
        {"image": IMAGE, "policy": {"isolation": "reviewed_docker"}}
    )
    result = ReproductionExecutor(backend, store, adapters=selected).run(
        source, original, generated, environment
    )
    assert result.status is ReproductionStatus.REPRODUCED
    assert len(backend.calls) == 2 and all(
        bundle.framework_id == "contract" for bundle in backend.calls
    )
    assert result.executions[0].command.argv[0] == "contract-runner"
    assert (
        assess_execution(
            execution(), data.oracle, b'{"contract":true}', framework=selected.framework
        ).status
        is ReproductionStatus.REPRODUCED
    )


def test_v1_capsule_binding_rejects_other_frameworks() -> None:
    with pytest.raises(CapsuleError):
        v1_adapters(AdapterSelection(PythonLanguageAdapter(), ContractFramework()))


def test_tampered_bundle_is_rejected_before_docker_or_source_access(tmp_path: Path) -> None:
    from uuid import uuid4

    from repo_repro.reproduction.models import PytestRunRequest

    policy = SandboxPolicy(isolation="reviewed_docker")
    fake = FakeDocker(policy)
    store = FileArtifactStore(tmp_path / "evidence")
    backend = DockerSandboxRunner(image=IMAGE, artifacts=store, transport=fake)
    request = PytestRunRequest(
        repository_path=tmp_path / "missing",
        commit="a" * 40,
        test_code=CODE,
        policy=policy,
        run_id=uuid4(),
        plan_id=uuid4(),
        repetition=1,
    )
    bundle = PytestAdapter().bundle(CODE)
    with pytest.raises(PolicyViolation, match="registered framework"):
        backend.run_bundle(
            request, replace(bundle, command=CommandSpec(argv=("sh", "-c", "anything")))
        )
    assert fake.calls == []


def test_runtime_inventory_cannot_overwrite_kernel_evidence(tmp_path: Path) -> None:
    class InvalidInventory(PythonLanguageAdapter):
        def validate_inventory(self, value: Mapping[str, object]) -> dict[str, object]:
            return {"memory": "unlimited"}

    source = tmp_path / "source"
    repository_pair(source)
    policy = SandboxPolicy(isolation="reviewed_docker")
    fake = FakeDocker(policy)
    backend = DockerSandboxRunner(
        image=IMAGE,
        artifacts=FileArtifactStore(tmp_path / "evidence"),
        transport=fake,
        adapters=AdapterSelection(InvalidInventory(), PytestAdapter()),
    )
    result = backend.run(
        source, CommandSpec(argv=("python", "calculator.py")), resource_limits=policy
    )
    assert result.termination.value == "infra_error"
    assert result.resource_usage is not None and not result.resource_usage.cgroup_verified
    assert not fake.exists


def test_registry_rejects_invalid_and_ambiguous_language_ids() -> None:
    class InvalidLanguage(PythonLanguageAdapter):
        id = "../plugin"

    class AlsoPython(PythonLanguageAdapter):
        id = "second-python"

    registry = builtin_registry()
    with pytest.raises(InputError, match="invalid adapter"):
        registry.register_language(InvalidLanguage())
    registry.register_language(AlsoPython())
    with pytest.raises(InputError, match="no unique"):
        registry.detect(("main.py",))


def test_adapter_selection_rejects_a_framework_from_another_language() -> None:
    class Incompatible(ContractFramework):
        language_id = "other"

    with pytest.raises(ValueError, match="incompatible"):
        AdapterSelection(PythonLanguageAdapter(), Incompatible())


def test_normalized_evidence_must_identify_the_selected_framework() -> None:
    class WrongIdentity(ContractFramework):
        def normalize_evidence(self, report: bytes) -> SessionEvidence:
            return replace(super().normalize_evidence(report), framework_id="other")

    result = assess_execution(
        execution(), context().oracle, b'{"contract":true}', framework=WrongIdentity()
    )
    assert result.status is ReproductionStatus.INCONCLUSIVE


def test_model_generation_uses_the_selected_framework_prompt(tmp_path: Path) -> None:
    from tests.test_ai_investigation import SequenceProvider

    store = FileArtifactStore(tmp_path / "evidence")
    selected = AdapterSelection(PythonLanguageAdapter(), ContractFramework())
    data = context().model_copy(
        update={"environment": EnvironmentInfo(test_frameworks=("contract",))}
    )
    response = Proposal.model_validate(
        {
            "understanding": "contract fixture",
            "plan": "contract fixture",
            "closest_test": None,
            "test_code": "opaque candidate",
            "evidence": [{"source": "issue.body", "quote": "returns zero"}],
        }
    ).model_dump_json()
    provider = SequenceProvider(store, [response])
    result = TestGenerator(store, provider, adapters=selected).generate(data)
    assert result.code == "opaque candidate"
    assert str(provider.requests[0]["system"]).startswith(ContractFramework.generation_prompt)


def test_language_fingerprint_cannot_replace_core_identity_fields(tmp_path: Path) -> None:
    class InvalidIdentity(PythonLanguageAdapter):
        def fingerprint_identity(self, probe: Mapping[str, object]) -> dict[str, object]:
            return {"image": "override"}

    source = tmp_path / "source"
    original, _ = repository_pair(source)
    store = FileArtifactStore(tmp_path / "evidence")
    selected = AdapterSelection(InvalidIdentity(), ContractFramework())
    data = context().model_copy(
        update={"environment": EnvironmentInfo(test_frameworks=("contract",))}
    )
    generated = TestGenerator(store, adapters=selected).seal_code(data, "opaque candidate")
    from repo_repro.reproduction.models import ExecutionEnvironment

    env = ExecutionEnvironment.model_validate(
        {"image": IMAGE, "policy": {"isolation": "reviewed_docker"}}
    )
    result = ReproductionExecutor(ContractBackend(store), store, adapters=selected).run(
        source, original, generated, env
    )
    assert result.status is ReproductionStatus.ENVIRONMENT_ERROR
    assert "collides" in result.reason


def test_registered_bundle_must_contain_the_sealed_test_bytes(tmp_path: Path) -> None:
    from uuid import uuid4

    from repo_repro.reproduction.models import PytestRunRequest

    class WrongTest(PytestAdapter):
        id = "wrong-test"

        def bundle(self, code: str) -> Bundle:
            original = super().bundle(code)
            changed = dict(original.files)
            changed[original.test_path] = b"different test"
            return replace(original, files=changed)

    registry = builtin_registry()
    registry.register_framework(WrongTest())
    policy = SandboxPolicy(isolation="reviewed_docker")
    fake = FakeDocker(policy)
    backend = DockerSandboxRunner(
        image=IMAGE,
        artifacts=FileArtifactStore(tmp_path / "evidence"),
        transport=fake,
        registry=registry,
        adapters=registry.resolve("python", "wrong-test"),
    )
    request = PytestRunRequest(
        repository_path=tmp_path / "missing",
        commit="a" * 40,
        test_code=CODE,
        policy=policy,
        run_id=uuid4(),
        plan_id=uuid4(),
        repetition=1,
    )
    with pytest.raises(PolicyViolation, match="sealed candidate"):
        backend.run_test(request)
    assert not fake.calls


def test_bundle_copies_the_callers_mutable_file_mapping() -> None:
    original = PytestAdapter().bundle(CODE)
    files = dict(original.files)
    copied = replace(original, files=files)
    files[original.test_path] = b"mutated"
    assert copied.files[original.test_path] == CODE.encode()


def test_legacy_report_wire_key_is_preserved() -> None:
    from tests.repro_support import artifact

    report = artifact("report.json")
    result = execution().model_copy(update={"pytest_report": report})
    assert result.test_report == report
    assert "pytest_report" in result.model_dump() and "test_report" not in result.model_dump()


def test_adapter_discovery_cli_is_offline_and_has_no_workspace_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner

    from repo_repro.cli import app

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["adapters"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["frameworks"] == ["pytest"]
    assert not (tmp_path / ".repo-repro").exists()
