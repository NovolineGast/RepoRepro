"""Export/import authorization, secret isolation and data-only CLI behavior."""

import hashlib
import io
import json
import tarfile
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from repo_repro.capsule.archive import extract_capsule, validate_capsule
from repo_repro.capsule.exporter import CapsuleExporter
from repo_repro.capsule.images import DockerImageArchive, scan_image
from repo_repro.capsule.replay import CapsuleReplay, compare_replay
from repo_repro.capsule.schema import CapsuleError
from repo_repro.capsule.security import SecretRedactor, pack_source
from repo_repro.cli import app
from repo_repro.core.models import SandboxPolicy
from repo_repro.reproduction.generator import TestGenerator
from repo_repro.reproduction.models import (
    ExecutionEnvironment,
    ReproductionResult,
    ReproductionStatus,
)
from repo_repro.reproduction.runtime import injected_files
from repo_repro.storage.artifacts import FileArtifactStore
from tests.repro_support import CODE, context, execution

IMAGE = "sha256:" + "d" * 64


class FakeImageStore(DockerImageArchive):
    def capture(self, image_id: str, destination: Path, redactor: SecretRedactor) -> None:
        destination.write_bytes(b"fixture image transport, never loaded")


def captured(
    store: FileArtifactStore, log: str = "api_key=opaque-capsule-secret"
) -> ReproductionResult:
    generated = TestGenerator(store).seal_code(context(), CODE)
    environment = ExecutionEnvironment.model_validate(
        {"image": IMAGE, "policy": {"isolation": "reviewed_docker"}}
    )
    files = {"calculator.py": b"def parse_value(value):\n    return -1\n", **injected_files(CODE)}
    archive = store.put_bytes(pack_source(files), media_type="application/x-tar")
    helpers = [
        (name, hashlib.sha256(data).hexdigest()) for name, data in injected_files(CODE).items()
    ]
    backend = store.put_bytes(
        json.dumps(
            {
                "image_id": IMAGE,
                "image_reference": IMAGE,
                "cleanup_confirmed": True,
                "injected_files": helpers,
                "cgroup_probe": {"python_version": "3.11 fixture"},
            }
        ).encode(),
        media_type="application/json",
    )
    item = execution().model_copy(
        update={
            "revision": execution().revision.model_copy(update={"source": archive}),
            "backend_snapshot": backend,
            "stdout": store.put_bytes(log.encode(), media_type="text/plain"),
            "stderr": store.put_bytes(b"password: opaque-error-secret", media_type="text/plain"),
        }
    )
    return ReproductionResult(
        run_id=uuid4(),
        generated_test=generated,
        commit="a" * 40,
        environment=environment,
        status=ReproductionStatus.INCONCLUSIVE,
        flaky=False,
        reason="fixture only",
        executions=(item,),
        assessments=(),
    )


def test_export_redacts_logs_and_keeps_only_replay_inputs(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "private")
    result = captured(store)
    destination = tmp_path / "case.zip"
    exporter = CapsuleExporter(
        store, images=FakeImageStore(), redactor=SecretRedactor(include_environment=False)
    )
    manifest = exporter.export(result, destination)
    assert manifest.mode == "offline" and validate_capsule(destination) == manifest
    root = tmp_path / "data"
    extract_capsule(destination, root)
    assert "opaque-capsule-secret" not in (root / "evidence/stdout.log").read_text()
    assert "opaque-error-secret" not in (root / "evidence/stderr.log").read_text()
    assert (root / "tests/test_reproduction.py").read_text() == CODE
    assert not any("model_audit" in entry.path for entry in manifest.files)
    assert "opaque-capsule-secret" in store.read_bytes(result.executions[0].stdout).decode()


def test_unknown_json_style_secret_values_are_redacted() -> None:
    redactor = SecretRedactor(include_environment=False)
    assert "opaque-json-value" not in redactor.text('{"api_key": "opaque-json-value"}')
    assert "opaque-token-value" not in redactor.text('{"token": "opaque-token-value"}')
    assert "opaque-prefix-value" not in redactor.text('OPENAI_API_KEY="opaque-prefix-value"')
    assert "opaque-client-value" not in redactor.text('client_secret="opaque-client-value"')
    assert "dXNlcjpwYXNz" not in redactor.text("Authorization: Basic dXNlcjpwYXNz")


def test_critical_test_credentials_reject_offline_export(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "private")
    result = captured(store)
    exporter = CapsuleExporter(
        store,
        images=FakeImageStore(),
        redactor=SecretRedactor(("returns zero",), include_environment=False),
    )
    with pytest.raises(CapsuleError):
        exporter.export(result, tmp_path / "unsafe.zip")
    assert not (tmp_path / "unsafe.zip").exists()


def test_diagnostic_export_never_calls_docker_and_cannot_be_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileArtifactStore(tmp_path / "private")
    result = captured(store)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Docker must not be called")

    monkeypatch.setattr(DockerImageArchive, "capture", forbidden)
    archive = tmp_path / "diagnostic.zip"
    CapsuleExporter(store).export(result, archive, diagnostic_only=True)
    assert validate_capsule(archive).mode == "diagnostic"
    with pytest.raises(CapsuleError, match="diagnostic"):
        CapsuleReplay().replay(
            archive,
            tmp_path / "replay",
            local_policy=SandboxPolicy(isolation="reviewed_docker"),
            authorized=True,
        )
    assert not (tmp_path / "replay").exists()


def test_replay_is_never_authorized_by_import_or_manifest(tmp_path: Path) -> None:
    with pytest.raises(CapsuleError, match="authorization"):
        CapsuleReplay().replay(
            tmp_path / "not-even-read.zip",
            tmp_path / "unused",
            local_policy=SandboxPolicy(isolation="reviewed_docker"),
        )
    assert not (tmp_path / "unused").exists()


def test_validate_and_unpack_cli_do_not_run_bundled_scripts(tmp_path: Path) -> None:
    from tests.test_capsule_format import diagnostic_capsule

    path = tmp_path / "case.zip"
    diagnostic_capsule(path)
    checked = CliRunner().invoke(app, ["capsule", "validate", str(path)])
    assert checked.exit_code == 0, checked.output
    unpacked = CliRunner().invoke(app, ["capsule", "unpack", str(path), str(tmp_path / "unpacked")])
    assert unpacked.exit_code == 0, unpacked.output


@pytest.mark.parametrize("diagnostic_only", [True, False])
def test_inspect_can_summarize_a_validated_capsule(tmp_path: Path, diagnostic_only: bool) -> None:
    store = FileArtifactStore(tmp_path / "private")
    path = tmp_path / "case.zip"
    expected = CapsuleExporter(store, images=FakeImageStore()).export(
        captured(store), path, diagnostic_only=diagnostic_only
    )
    runner = CliRunner()

    summary = runner.invoke(app, ["capsule", "inspect", str(path), "--summary"])
    assert summary.exit_code == 0, summary.output
    assert summary.stdout.splitlines() == [
        f"Schema version: {expected.schema_version}",
        f"Mode: {expected.mode}",
        "Language/framework: python / pytest",
        f"Runtime version: {expected.runtime_version}",
        f"Execution status: {expected.execution_status}",
        f"Original commit: {expected.original_commit}",
        "Fixed commit: unavailable",
    ]

    default = runner.invoke(app, ["capsule", "inspect", str(path)])
    checked = runner.invoke(app, ["capsule", "validate", str(path)])
    assert default.exit_code == checked.exit_code == 0
    manifest_json = expected.model_dump(mode="json")
    assert json.loads(default.stdout) == json.loads(checked.stdout) == manifest_json


def test_inspect_summary_keeps_both_commits_and_escapes_free_form_metadata(tmp_path: Path) -> None:
    from tests.test_capsule_format import diagnostic_capsule

    path = tmp_path / "differential.zip"
    fixed = "b" * 40
    runtime = "Python 3.12\n测试"
    manifest = diagnostic_capsule(path, fixed_commit=fixed, runtime_version=runtime)

    summary = CliRunner().invoke(app, ["capsule", "inspect", str(path), "--summary"])

    assert summary.exit_code == 0, summary.output
    assert f"Original commit: {manifest.original_commit}" in summary.stdout
    assert f"Fixed commit: {fixed}" in summary.stdout
    assert f"Runtime version: {runtime!a}" in summary.stdout.splitlines()


def test_inspect_summary_reports_archive_errors_like_default_json(tmp_path: Path) -> None:
    path = tmp_path / "damaged.zip"
    path.write_bytes(b"not a valid capsule")
    runner = CliRunner()
    args = ["capsule", "inspect", str(path)]

    summary = runner.invoke(app, [*args, "--summary"])
    default = runner.invoke(app, args)

    assert summary.exit_code == default.exit_code == 3
    assert summary.stdout == ""
    assert summary.stderr == default.stderr


def test_replay_comparison_marks_runtime_drift_separately(tmp_path: Path) -> None:
    baseline = captured(FileArtifactStore(tmp_path)).model_copy(
        update={"environment_fingerprints": ("old", "old")}
    )
    drift = baseline.model_copy(update={"environment_fingerprints": ("new", "new")})
    assert compare_replay(baseline, drift) == (True, False)


@pytest.mark.parametrize(
    "payload",
    [
        [{"RepoTags": ["unrelated:latest"]}],
        {"layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+zstd"}]},
        {"config": {"Env": ["OPENAI_API_KEY=opaque-image-key"]}},
    ],
)
def test_image_transport_rejects_tags_unscannable_layers_and_credentials(
    tmp_path: Path, payload: object
) -> None:
    path = tmp_path / "image.tar"
    content = json.dumps(payload).encode()
    with tarfile.open(path, "w") as output:
        info = tarfile.TarInfo(
            "manifest.json" if isinstance(payload, list) else "blobs/config.json"
        )
        info.size = len(content)
        output.addfile(info, io.BytesIO(content))
    with pytest.raises(CapsuleError):
        scan_image(path, SecretRedactor(include_environment=False))
