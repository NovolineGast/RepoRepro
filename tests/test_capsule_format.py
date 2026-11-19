"""Version/schema, redaction and hostile ZIP handling without invoking Docker."""

import hashlib
import stat
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from repo_repro.capsule.archive import extract_capsule, validate_capsule
from repo_repro.capsule.schema import (
    COMMAND,
    REQUIRED,
    CapsuleError,
    CapsuleFile,
    CapsuleManifest,
    FileRole,
)
from repo_repro.capsule.security import SecretRedactor


def diagnostic_capsule(
    path: Path,
    *,
    version: str = "1.0.0",
    fixed_commit: str | None = None,
    runtime_version: str = "unknown",
) -> CapsuleManifest:
    files = {name: b"diagnostic data\n" for name in REQUIRED}
    manifest = CapsuleManifest(
        schema_version=version,
        programming_language="python",
        test_framework="pytest",
        reproduction_command=COMMAND,
        capsule_id=uuid4(),
        mode="diagnostic",
        repository_url="https://github.com/example/project",
        original_commit="a" * 40,
        fixed_commit=fixed_commit,
        runtime_version=runtime_version,
        timeout=120,
        expected_behavior="correct output",
        observed_behavior="unknown",
        execution_status="INCONCLUSIVE",
        generated_at=datetime.now(UTC),
        files=tuple(
            CapsuleFile(
                path=name,
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
                media_type="text/plain",
                role=FileRole.DIAGNOSTIC,
            )
            for name, data in sorted(files.items())
        ),
    )
    files["repro.yaml"] = manifest.model_dump_json(indent=2).encode()
    files["checksums.sha256"] = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(files.items())
    ).encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr("reproduction/" + name, data)
    return manifest


def test_diagnostic_package_is_valid_and_extraction_is_data_only(tmp_path: Path) -> None:
    capsule = tmp_path / "case.zip"
    manifest = diagnostic_capsule(capsule)
    assert validate_capsule(capsule) == manifest
    destination = tmp_path / "unpacked"
    assert extract_capsule(capsule, destination) == manifest
    assert (destination / "reproduce.sh").read_bytes() == b"diagnostic data\n"
    with pytest.raises(CapsuleError):
        extract_capsule(capsule, destination)


def test_patch_compatible_format_is_supported(tmp_path: Path) -> None:
    path = tmp_path / "v1.zip"
    diagnostic_capsule(path, version="1.0.2")
    assert validate_capsule(path).schema_version == "1.0.2"


@pytest.mark.parametrize(
    "field", ["schema_version", "programming_language", "test_framework", "reproduction_command"]
)
def test_required_runtime_contract_is_not_inferred(tmp_path: Path, field: str) -> None:
    manifest = diagnostic_capsule(tmp_path / "case.zip").model_dump()
    del manifest[field]
    with pytest.raises(ValidationError):
        CapsuleManifest.model_validate(manifest)


@pytest.mark.parametrize("version", ["2.0.0", "1.1.0", "1", "1.0.01"])
def test_incompatible_or_noncanonical_versions_are_rejected(tmp_path: Path, version: str) -> None:
    with pytest.raises(ValidationError):
        diagnostic_capsule(tmp_path / "invalid.zip", version=version)


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/tmp/escape",
        "reproduction/../../escape",
        "reproduction/C:/escape",
        "reproduction/a\\b",
        "reproduction/NUL",
        "other/file",
        "reproduction/data:stream",
        "reproduction/file.",
        "reproduction/file ",
        "reproduction//file",
    ],
)
def test_zip_slip_and_platform_path_tricks_are_rejected(tmp_path: Path, name: str) -> None:
    path = tmp_path / "hostile.zip"
    diagnostic_capsule(path)
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr(name, b"bad")
    with pytest.raises(CapsuleError):
        extract_capsule(path, tmp_path / "destination")
    assert not (tmp_path / "destination").exists()
    assert not (tmp_path / "escape").exists()


def test_links_and_case_aliases_are_rejected(tmp_path: Path) -> None:
    for member in ("reproduction/link", "reproduction/readme.md"):
        path = tmp_path / ("link.zip" if member.endswith("link") else "alias.zip")
        diagnostic_capsule(path)
        with zipfile.ZipFile(path, "a") as archive:
            item = zipfile.ZipInfo(member)
            if member.endswith("link"):
                item.create_system = 3
                item.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(item, b"../../outside")
        with pytest.raises(CapsuleError):
            validate_capsule(path)


def test_checksum_tampering_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "case.zip"
    diagnostic_capsule(path)
    changed = tmp_path / "changed.zip"
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(changed, "w") as destination:
        for entry in source.infolist():
            data = source.read(entry)
            destination.writestr(
                entry, b"tampered" if entry.filename.endswith("stdout.log") else data
            )
    with pytest.raises(CapsuleError, match=r"checksum|catalog"):
        validate_capsule(changed)


def test_secrets_are_redacted_and_critical_files_are_not_rewritten() -> None:
    redactor = SecretRedactor(("opaque-value-123",), include_environment=False)
    raw = 'api_key="opaque-value-123" Authorization: Bearer opaque-value-123 https://user:password@example.com'
    assert "opaque-value-123" not in redactor.text(raw)
    assert "password@example" not in redactor.text(raw)
    assert redactor.value({"environment": {"API_KEY": "something"}}) == {
        "environment": {"API_KEY": "[REDACTED]"}
    }
    with pytest.raises(CapsuleError):
        redactor.critical(raw.encode())


def test_reference_metadata_is_bounded_before_parsing(tmp_path: Path) -> None:
    original = tmp_path / "original.zip"
    diagnostic_capsule(original)
    hostile = tmp_path / "oversized.zip"
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(hostile, "w") as output:
        for member in source.infolist():
            data = (
                b" " * (8_388_608 + 1)
                if member.filename.endswith("reference/result.json")
                else source.read(member)
            )
            output.writestr(member, data)
    with pytest.raises(CapsuleError, match="slot limit"):
        validate_capsule(hostile)
