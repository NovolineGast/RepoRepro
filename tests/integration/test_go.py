"""Go feedback, differential verification, Capsule v2 and failure classification in real Docker."""

import json
import os
from pathlib import Path

import pytest
from git import Repo
from tests.go_support import CODE, issue_payload, oracle, repository_pair
from tests.test_ai_investigation import SequenceProvider

from repo_repro.adapters.registry import builtin_registry
from repo_repro.capsule.exporter import CapsuleExporter
from repo_repro.capsule.replay import CapsuleReplay
from repo_repro.github.issue_parser import IssueParser
from repo_repro.reproduction.executor import ReproductionExecutor
from repo_repro.reproduction.generator import TestGenerator as Generator
from repo_repro.reproduction.investigation_models import InvestigationPlan
from repo_repro.reproduction.investigator import AIInvestigator
from repo_repro.reproduction.models import (
    DifferentialStatus,
    ExecutionEnvironment,
    ReproductionStatus,
)
from repo_repro.reproduction.planner import build_input
from repo_repro.sandbox.docker_runner import DockerSandboxRunner
from repo_repro.storage.artifacts import FileArtifactStore

pytestmark = pytest.mark.docker_integration


@pytest.fixture
def go_image() -> str:
    value = os.environ.get("REPRO_GO_IMAGE")
    if not value:
        if os.environ.get("REPRO_REQUIRE_GO") == "1":
            pytest.fail("REPRO_GO_IMAGE is required")
        pytest.skip("set REPRO_GO_IMAGE to a reviewed Python supervisor + Go toolchain image")
    return value


def test_go_ai_feedback_verify_export_and_replay(tmp_path: Path, go_image: str) -> None:
    repository = tmp_path / "repository"
    original, fixed = repository_pair(repository)
    store = FileArtifactStore(tmp_path / "artifacts")
    adapters = builtin_registry().resolve("go")
    issue = IssueParser().parse_payload(issue_payload())
    context = build_input(issue, repository, oracle(), ref=original, adapters=adapters)
    generated = Generator(store, adapters=adapters).seal_code(context, CODE)
    plan = InvestigationPlan(
        summary="Connector doubled",
        expected_behavior="connector preserved",
        actual_behavior="doubled",
        preconditions=("connector-only input",),
        hypothesis="extra write",
        source_paths=("text.go",),
        test_paths=(),
        evidence=generated.proposal.evidence,
    )
    provider = SequenceProvider(
        store, [plan.model_dump_json(), generated.proposal.model_dump_json()]
    )
    backend = DockerSandboxRunner(image=go_image, artifacts=store, adapters=adapters)
    executor = ReproductionExecutor(backend, store, adapters=adapters)
    environment = ExecutionEnvironment.model_validate(
        {"image": go_image, "policy": {"isolation": "reviewed_docker"}}
    )
    session = AIInvestigator(provider, executor, store).run(
        issue,
        repository,
        oracle(),
        environment,
        ref=original,
        fixed_commit=fixed,
        initial_code=CODE.replace('Normalize("_")', 'Normalize("ok")').replace(
            'got != "_"', 'got != "ok"'
        ),
    )
    assert session.model_calls == 2 and session.stop_reason == "reproduced"
    assert session.attempts[0].result is not None
    assert session.attempts[0].result.status is ReproductionStatus.NOT_REPRODUCED
    result = session.verification
    assert result is not None and result.status is DifferentialStatus.DIFFERENTIAL_VALIDATED
    assert all(
        e.command == adapters.framework.bundle(CODE).command
        for group in (result.original, result.fixed)
        for e in group.executions
    )
    for execution in result.original.executions:
        data = json.loads(store.read_bytes(execution.test_report)) if execution.test_report else {}
        assert data["truncated"] is False
        assert any(
            e.get("Action") == "run" and e.get("Test") == "TestRepro" for e in data["events"]
        )
    capsule = tmp_path / "case.zip"
    manifest = CapsuleExporter(store).export(result, capsule)
    assert manifest.schema_version == "2.0.0" and manifest.test_framework == "go-test"
    assert manifest.replay is not None and manifest.replay.test_path.endswith(".go")
    replay = CapsuleReplay().replay(
        capsule, tmp_path / "replay", local_policy=environment.policy, authorized=True
    )
    assert replay.status == "MATCHED" and replay.behavior_match and replay.environment_match


@pytest.mark.parametrize("kind", ["compile", "missing_dependency", "panic", "timeout"])
def test_real_go_failures_are_not_bug_reproductions(
    tmp_path: Path, go_image: str, kind: str
) -> None:
    repository = tmp_path / "repository"
    original, _ = repository_pair(repository)
    if kind in {"panic", "timeout", "missing_dependency"}:
        source = repository / "text.go"
        code = {
            "panic": (
                'package text\nfunc Normalize(s string) string {\n panic("connector preserved") }\n'
            ),
            "timeout": "package text\nfunc Normalize(s string) string { for {} }\n",
            "missing_dependency": (
                'package text\nimport _ "example.com/missing"\n'
                "func Normalize(s string) string { return s }\n"
            ),
        }[kind]
        source.write_text(code, encoding="utf-8")
        with Repo(repository) as repo:
            repo.index.add(["text.go"])
            original = repo.index.commit(kind, skip_hooks=True).hexsha
    store = FileArtifactStore(tmp_path / "artifacts")
    adapters = builtin_registry().resolve("go")
    context = build_input(
        IssueParser().parse_payload(issue_payload()),
        repository,
        oracle(),
        ref=original,
        adapters=adapters,
    )
    code = CODE.replace("Normalize", "Missing") if kind == "compile" else CODE
    generated = Generator(store, adapters=adapters).seal_code(context, code)
    backend = DockerSandboxRunner(image=go_image, artifacts=store, adapters=adapters)
    environment = ExecutionEnvironment.model_validate(
        {
            "image": go_image,
            "policy": {
                "isolation": "reviewed_docker",
                "timeout_seconds": 2 if kind == "timeout" else 120,
            },
        }
    )
    result = ReproductionExecutor(backend, store, adapters=adapters).run(
        repository, original, generated, environment
    )
    assert result.status is (
        ReproductionStatus.TIMEOUT
        if kind == "timeout"
        else ReproductionStatus.INCONCLUSIVE
        if kind == "panic"
        else ReproductionStatus.ENVIRONMENT_ERROR
    )
    assert len(result.executions) == 2
    assert all(
        json.loads(store.read_bytes(e.backend_snapshot))["cleanup_confirmed"]
        for e in result.executions
    )
