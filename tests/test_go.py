"""Fail-closed Go syntax, analysis, language selection and native evidence."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from repo_repro.adapters.go import GoLanguageAdapter
from repo_repro.adapters.go_test import GoTestAdapter
from repo_repro.adapters.registry import builtin_registry
from repo_repro.capsule.security import SecretRedactor
from repo_repro.cli import app
from repo_repro.core.exceptions import PolicyViolation, RepositoryError
from repo_repro.github.issue_parser import IssueParser
from repo_repro.reproduction.generator import TestGenerator as Generator
from repo_repro.reproduction.models import FailureOracle, ReproductionStatus
from repo_repro.reproduction.planner import build_input
from repo_repro.storage.artifacts import FileArtifactStore
from repo_repro.verification.verifier import assess_execution
from tests.go_support import CODE, issue_payload, oracle, repository_pair
from tests.repro_support import execution


def events(outcome: str = "fail") -> dict[str, object]:
    package = "example.com/text/repo_repro_generated"
    values: list[dict[str, object]] = [
        {"Action": "start", "Package": package},
        {"Action": "run", "Package": package, "Test": "TestRepro"},
        {
            "Action": "output",
            "Package": package,
            "Test": "TestRepro",
            "Output": "    repro_test.go:9: connector preserved: got __\n",
        },
        {"Action": outcome, "Package": package, "Test": "TestRepro"},
        {"Action": outcome, "Package": package},
    ]
    return {
        "schema_version": "2.0",
        "framework": "go-test",
        "truncated": False,
        "exit_code": 0 if outcome == "pass" else 1,
        "events": values,
    }


def test_go_analysis_and_generation_cli_use_committed_sources(tmp_path: Path) -> None:
    original, _ = repository_pair(tmp_path / "repo")
    adapters = builtin_registry().resolve("go")
    context = build_input(
        IssueParser().parse_payload(issue_payload()),
        tmp_path / "repo",
        oracle(),
        ref=original,
        adapters=adapters,
    )
    assert context.environment.go_module == "example.com/text"
    assert 'return "__"' in context.relevant_source_files[0].content
    (tmp_path / "context.json").write_text(context.model_dump_json(), encoding="utf-8")
    (tmp_path / "test.go").write_text(CODE, encoding="utf-8")
    result = CliRunner().invoke(
        app,
        [
            "generate",
            str(tmp_path / "context.json"),
            "--test-file",
            str(tmp_path / "test.go"),
            "--artifacts",
            str(tmp_path / "artifacts"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["oracle"]["language"] == "go"
    Generator(FileArtifactStore(tmp_path / "store"), adapters=adapters).seal_code(context, CODE)
    with pytest.raises(PolicyViolation):
        Generator(FileArtifactStore(tmp_path / "store"), adapters=adapters).seal_code(
            context, CODE.replace("example.com/text", "os")
        )


@pytest.mark.parametrize(
    "before,after",
    [
        ('got := target.Normalize("_")', 'got := "__"'),
        ('if got != "_"', "if true"),
        ('if got != "_"', 'if got == "_"'),
        ('if got != "_"', 'got = "__"\n if got != "_"'),
        ("t.Errorf(", "t.Fatalf("),
        ("t.Errorf(", "t.Skipf("),
        ('target.Normalize("_")', 'target.Normalize(os.Getenv("X"))'),
        ('target.Normalize("_")', 'target.Normalize(func() string { panic("X") }())'),
        ('"testing"', '"testing"\n "os"'),
        ("package repro", "//go:build ignore\npackage repro"),
        ("package repro", 'package repro\nfunc init() { panic("X") }'),
        ('"example.com/text"', '"example.com/text/../os"'),
        ('"example.com/text"', '"example.com/text/internal/foo"'),
        ("got %v", "got %s %v"),
        ("got %v", "got"),
        ("TestRepro(t", "TestRepro2(t"),
        ("target.Normalize", "target.normalize"),
        ("target.Normalize", "target . Normalize"),
    ],
)
def test_go_candidates_cannot_fabricate_or_bypass_assertions(before: str, after: str) -> None:
    with pytest.raises(PolicyViolation):
        GoTestAdapter().validate_code(CODE.replace(before, after))


def test_go_oracle_cannot_impersonate_python_target() -> None:
    with pytest.raises(ValidationError):
        FailureOracle(**{**oracle().model_dump(), "node_id": "test_repro"})
    with pytest.raises(ValidationError):
        FailureOracle(**{**oracle().model_dump(), "exception_type": "AssertionError"})
    assert builtin_registry().detect(("go.mod", "text.go")).language.id == "go"
    assert not GoLanguageAdapter().selectable("vendor/a.go")
    assert GoTestAdapter().canonical_test(CODE) == GoTestAdapter().canonical_test(
        CODE.replace("    ", "\t").replace("got :=", "got  :=")
    )


def test_github_secret_references_are_data_but_literal_credentials_still_block_export() -> None:
    redactor = SecretRedactor(include_environment=False)
    for value in (
        "COVERALLS_TOKEN: ${{ secrets.COVERALLS_TOKEN }}\n",
        'TOKEN: "${{secrets.TEST_TOKEN}}"\n',
    ):
        redactor.critical(value.encode())
        assert redactor.text(value) == value
    for value in (
        'TOKEN: "real-credential"',
        "TOKEN: ${{secrets.TOKEN}}literal",
        'TOKEN: "${{secrets.TOKEN}}literal"',
        'TOKEN: "${{vars.TOKEN}}"',
    ):
        assert redactor.text(value) != value
        from repo_repro.capsule.schema import CapsuleError

        with pytest.raises(CapsuleError):
            redactor.critical(value.encode())


def test_go_event_stream_requires_real_target_assertion() -> None:
    framework = GoTestAdapter()
    assessment = assess_execution(
        execution(), oracle(), json.dumps(events()).encode(), framework=framework
    )
    assert assessment.status is ReproductionStatus.REPRODUCED
    passed = assess_execution(
        execution(0), oracle(), json.dumps(events("pass")).encode(), framework=framework
    )
    assert passed.status is ReproductionStatus.NOT_REPRODUCED
    skipped = assess_execution(
        execution(), oracle(), json.dumps(events("skip")).encode(), framework=framework
    )
    assert skipped.status is ReproductionStatus.INCONCLUSIVE


@pytest.mark.parametrize(
    "mutation",
    [
        "empty",
        "truncated",
        "no_target",
        "duplicate",
        "wrong_package",
        "panic",
        "cached",
        "wrong_exit",
        "wrong_message",
        "missing_end",
    ],
)
def test_incomplete_or_unrelated_go_failures_are_never_reproduced(mutation: str) -> None:
    data = events()
    values = data["events"]
    assert isinstance(values, list)
    if mutation == "empty":
        values.clear()
    elif mutation == "truncated":
        data["truncated"] = True
    elif mutation == "no_target":
        data["events"] = [e for e in values if "Test" not in e]
    elif mutation == "duplicate":
        values.insert(2, dict(values[1]))
    elif mutation == "wrong_package":
        values[1]["Package"] = "another/repo_repro_generated"
    elif mutation == "panic":
        values[2]["Output"] += "panic: connector preserved\n"
    elif mutation == "cached":
        data["events"] = [values[0], values[-1]]
    elif mutation == "wrong_exit":
        data["exit_code"] = 0
    elif mutation == "wrong_message":
        values[2]["Output"] = "    repro_test.go:9: unrelated\n"
    else:
        values.pop()
    result = assess_execution(
        execution(), oracle(), json.dumps(data).encode(), framework=GoTestAdapter()
    )
    assert result.status is not ReproductionStatus.REPRODUCED


@pytest.mark.parametrize(
    "extra,content",
    [
        ("go.work", "go 1.23"),
        ("nested/go.mod", "module nested"),
        ("c.go", 'package text\nimport "C"'),
    ],
)
def test_unsupported_go_layouts_fail_before_execution(
    tmp_path: Path, extra: str, content: str
) -> None:
    from git import Repo

    repository_pair(tmp_path / "repo")
    path = tmp_path / "repo" / extra
    path.parent.mkdir(exist_ok=True)
    path.write_text(content, encoding="utf-8")
    with Repo(tmp_path / "repo") as repo:
        repo.index.add([extra])
        repo.index.commit("unsupported layout", skip_hooks=True)
    with pytest.raises(RepositoryError):
        build_input(
            IssueParser().parse_payload(issue_payload()),
            tmp_path / "repo",
            oracle(),
            adapters=builtin_registry().resolve("go"),
        )


def test_go_flaky_outcomes_cannot_be_verified(tmp_path: Path) -> None:
    import hashlib

    from repo_repro.adapters.contracts import TestBundle as Bundle
    from repo_repro.core.models import ExecutionResult, ResourceUsage
    from repo_repro.reproduction.executor import ReproductionExecutor
    from repo_repro.reproduction.models import ExecutionEnvironment, PytestRunRequest

    original, _ = repository_pair(tmp_path / "repo")
    store = FileArtifactStore(tmp_path / "artifacts")
    adapters = builtin_registry().resolve("go")
    context = build_input(
        IssueParser().parse_payload(issue_payload()),
        tmp_path / "repo",
        oracle(),
        ref=original,
        adapters=adapters,
    )
    generated = Generator(store, adapters=adapters).seal_code(context, CODE)
    image = "sha256:" + "c" * 64

    class AlternatingBackend:
        def run_bundle(self, request: PytestRunRequest, bundle: Bundle) -> ExecutionResult:
            passed = request.repetition == 2
            result = execution(0 if passed else 1)
            data = {
                "cleanup_confirmed": True,
                "image_reference": image,
                "image_id": image,
                "policy": request.policy.model_dump(),
                "test_sha256": hashlib.sha256(request.test_code.encode()).hexdigest(),
                "injected_files": bundle.hashes,
                "cgroup_probe": {"go_version": "go version go1.27.0 linux/amd64"},
            }
            return result.model_copy(
                update={
                    "run_id": request.run_id,
                    "plan_id": request.plan_id,
                    "repetition": request.repetition,
                    "command": bundle.command,
                    "revision": result.revision.model_copy(update={"commit": request.commit}),
                    "backend_snapshot": store.put_bytes(
                        json.dumps(data).encode(), media_type="application/json"
                    ),
                    "pytest_report": store.put_bytes(
                        json.dumps(events("pass" if passed else "fail")).encode(),
                        media_type="application/json",
                    ),
                    "resource_usage": ResourceUsage(
                        memory_limit_bytes=2147483648,
                        cpu_limit=1,
                        pids_limit=128,
                        cgroup_verified=True,
                    ),
                }
            )

    environment = ExecutionEnvironment.model_validate(
        {"image": image, "policy": {"isolation": "reviewed_docker"}}
    )
    result = ReproductionExecutor(AlternatingBackend(), store, adapters=adapters).run(
        tmp_path / "repo", original, generated, environment
    )
    assert result.flaky and result.status is ReproductionStatus.INCONCLUSIVE
    assert len(result.executions) == 2 and len(result.environment_fingerprints) == 2


def test_capsule_versions_bind_their_exact_language_framework_and_command(tmp_path: Path) -> None:
    from repo_repro.adapters.go_test import GO_COMMAND
    from repo_repro.capsule.schema import CapsuleManifest
    from tests.test_capsule_format import diagnostic_capsule

    original = diagnostic_capsule(tmp_path / "v1.zip").model_dump(mode="json")
    data = {
        **original,
        "schema_version": "2.0.0",
        "programming_language": "go",
        "test_framework": "go-test",
        "reproduction_command": list(GO_COMMAND),
    }
    data["files"] = [
        dict(item, path="tests/test_reproduction.go")
        if item["path"] == "tests/test_reproduction.py"
        else item
        for item in original["files"]
    ]
    assert CapsuleManifest.model_validate(data).schema_version == "2.0.0"
    for change in (
        {"schema_version": "1.0.0"},
        {"schema_version": "2.0.1"},
        {"programming_language": "python"},
        {"test_framework": "pytest"},
        {"reproduction_command": original["reproduction_command"]},
        {"files": original["files"]},
    ):
        with pytest.raises(ValidationError):
            CapsuleManifest.model_validate({**data, **change})
