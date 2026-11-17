"""Export captured execution data, never current working files or model credentials."""

import hashlib
import json
import os
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from repo_repro.adapters.contracts import AdapterSelection
from repo_repro.capsule.adapter_binding import v1_adapters
from repo_repro.capsule.archive import validate_capsule
from repo_repro.capsule.images import DockerImageArchive
from repo_repro.capsule.schema import (
    COMMAND,
    SCHEMA_VERSION,
    CapsuleError,
    CapsuleFile,
    CapsuleImage,
    CapsuleManifest,
    CapsuleSource,
    FileRole,
    ReplayRecipe,
)
from repo_repro.capsule.security import SecretRedactor, pack_source, source_files
from repo_repro.reproduction.models import DifferentialResult, ReproductionResult
from repo_repro.storage.artifacts import FileArtifactStore


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(131_072):
            digest.update(chunk)
    return digest.hexdigest()


class CapsuleExporter:
    def __init__(
        self,
        artifacts: FileArtifactStore,
        *,
        redactor: SecretRedactor | None = None,
        images: DockerImageArchive | None = None,
        adapters: AdapterSelection | None = None,
    ) -> None:
        self.artifacts = artifacts
        self.redactor = redactor or SecretRedactor()
        self.images = images or DockerImageArchive()
        self.adapters = v1_adapters(adapters)

    def export(
        self,
        result: ReproductionResult | DifferentialResult,
        destination: Path,
        *,
        diagnostic_only: bool = False,
    ) -> CapsuleManifest:
        destination = destination.absolute()
        if destination.exists() or destination.is_symlink():
            raise CapsuleError("capsule destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".capsule-export-", dir=destination.parent
        ) as temporary:
            stage = Path(temporary)
            if stage.resolve().parent != destination.parent.resolve():
                raise CapsuleError("unexpected staging directory")
            roles: dict[str, FileRole] = {}

            def put(name: str, data: bytes, role: FileRole) -> None:
                path = stage / name
                path.parent.mkdir(parents=True, exist_ok=True)
                if role is FileRole.REPLAY:
                    self.redactor.critical(data)
                else:
                    data = self.redactor.text(data.decode("utf-8")).encode()
                path.write_bytes(data)
                roles[name] = role

            groups: list[tuple[Literal["original", "fixed"], ReproductionResult]] = (
                [("original", result.original), ("fixed", result.fixed)]
                if isinstance(result, DifferentialResult)
                else [("original", result)]
            )
            original = groups[0][1]
            generated = original.generated_test
            recipe: ReplayRecipe | None = None
            image: CapsuleImage | None = None
            sources: list[CapsuleSource] = []
            traces: list[object] = []
            stdout: list[str] = []
            stderr: list[str] = []
            runtime = "unknown"
            actual_images: set[str] = set()
            captured_helpers: dict[str, str] | None = None
            log_bytes = 0
            for side, group in groups:
                snapshot_hashes = {item.revision.source.sha256 for item in group.executions}
                if not diagnostic_only and (len(snapshot_hashes) != 1 or not group.executions):
                    raise CapsuleError(
                        "offline export requires a consistent captured source for each version"
                    )
                for attempt in group.executions:
                    log_bytes += attempt.stdout.size_bytes + attempt.stderr.size_bytes
                    if log_bytes > 67_108_864:
                        raise CapsuleError("diagnostic logs exceed the 64 MiB export budget")
                    stdout.append(
                        f"=== {side} attempt {attempt.repetition} ===\n"
                        + self.artifacts.read_bytes(attempt.stdout).decode(
                            "utf-8", errors="replace"
                        )
                    )
                    stderr.append(
                        f"=== {side} attempt {attempt.repetition} ===\n"
                        + self.artifacts.read_bytes(attempt.stderr).decode(
                            "utf-8", errors="replace"
                        )
                    )
                    backend = json.loads(self.artifacts.read_bytes(attempt.backend_snapshot))
                    traces.append(
                        {
                            "side": side,
                            "execution": attempt.model_dump(mode="json"),
                            "backend": backend,
                            "pytest": json.loads(self.artifacts.read_bytes(attempt.pytest_report))
                            if attempt.pytest_report
                            else None,
                        }
                    )
                    if not diagnostic_only:
                        if not backend.get("cleanup_confirmed") or not backend.get("image_id"):
                            raise CapsuleError("captured runtime/cleanup evidence is incomplete")
                        actual_images.add(str(backend["image_id"]))
                        helpers = dict(backend.get("injected_files", []))
                        if captured_helpers is not None and captured_helpers != helpers:
                            raise CapsuleError("captured test/runtime helper versions differ")
                        captured_helpers = helpers
                        probe = backend.get("cgroup_probe", {})
                        if not isinstance(probe, dict):
                            raise CapsuleError("captured runtime inventory is invalid")
                        runtime = self.adapters.language.runtime_version(probe)
                if not diagnostic_only:
                    captured = source_files(
                        self.artifacts.read_bytes(group.executions[0].revision.source)
                    )
                    for name, digest in (captured_helpers or {}).items():
                        if (
                            name not in captured
                            or hashlib.sha256(captured[name]).hexdigest() != digest
                        ):
                            raise CapsuleError(
                                "source snapshot contradicts the captured helper hashes"
                            )
                    if (
                        captured.get(self.adapters.framework.bundle(generated.code).test_path)
                        != generated.code.encode()
                    ):
                        raise CapsuleError("captured test differs from the sealed test")
                    base = {
                        name: content
                        for name, content in captured.items()
                        if not name.startswith("repo_repro_generated/")
                    }
                    for content in base.values():
                        self.redactor.critical(content)
                    source_name = f"sources/{side}.tar"
                    put(source_name, pack_source(base), FileRole.REPLAY)
                    sources.append(CapsuleSource(side=side, commit=group.commit, path=source_name))
            environment = original.environment
            if not diagnostic_only:
                self.adapters.framework.validate_code(generated.code)
                if hashlib.sha256(generated.code.encode()).hexdigest() != generated.sha256:
                    raise CapsuleError("sealed test hash mismatch")
                if len(actual_images) != 1 or captured_helpers is None:
                    raise CapsuleError("offline capsule requires one frozen runtime image")
                image = CapsuleImage(image_id=next(iter(actual_images)))
                image_path = stage / image.path
                image_path.parent.mkdir(parents=True)
                self.images.capture(image.image_id, image_path, self.redactor)
                roles[image.path] = FileRole.REPLAY
                environment = environment.model_copy(update={"image": image.image_id})
                recipe = ReplayRecipe(
                    test_id=generated.id,
                    test_sha256=generated.sha256,
                    oracle=generated.oracle,
                    helper_hashes=tuple(sorted(captured_helpers.items())),
                )
                self.redactor.critical(recipe.model_dump_json().encode())
            role = FileRole.DIAGNOSTIC if diagnostic_only else FileRole.REPLAY
            put(
                "tests/test_reproduction.py",
                (
                    self.redactor.text(generated.code) if diagnostic_only else generated.code
                ).encode(),
                role,
            )
            put("environment.json", environment.model_dump_json(indent=2).encode(), role)
            put(
                "reference/result.json",
                json.dumps(self.redactor.value(result.model_dump(mode="json")), indent=2).encode(),
                FileRole.DIAGNOSTIC,
            )
            put(
                "evidence/trace.json",
                json.dumps(self.redactor.value(traces), indent=2).encode(),
                FileRole.DIAGNOSTIC,
            )
            put(
                "evidence/stdout.log",
                self.redactor.text("\n".join(stdout)).encode(),
                FileRole.DIAGNOSTIC,
            )
            put(
                "evidence/stderr.log",
                self.redactor.text("\n".join(stderr)).encode(),
                FileRole.DIAGNOSTIC,
            )
            put(
                "README.md",
                (
                    "# Repo Repro Capsule\n\n"
                    "Only explicit local replay with a local policy authorizes execution.\n"
                    "Do not run bundled scripts or build the Dockerfile.\n"
                    "Evidence is a redacted reference copy.\n"
                    "Commit IDs record provenance; hashes do not authenticate publishers.\n"
                    f"Mode: {'diagnostic (replay forbidden)' if diagnostic_only else 'offline'}.\n"
                ).encode(),
                FileRole.DIAGNOSTIC,
            )
            put(
                "Dockerfile",
                (
                    "# Diagnostic only; never built by Repo Repro.\n"
                    f"# Frozen image: {image.image_id if image else 'unavailable'}\nFROM scratch\n"
                ).encode(),
                FileRole.DIAGNOSTIC,
            )
            put(
                "reproduce.sh",
                b"#!/bin/sh\nprintf '%s\\n' 'Use local repro capsule replay with a local policy.'\n"
                b"exit 0\n",
                FileRole.DIAGNOSTIC,
            )
            put(
                "report.md",
                (
                    f"# Original execution reference\n\nStatus: {result.status.value}\n\n"
                    "This is not a new replay result. Diagnostics may have been redacted.\n"
                ).encode(),
                FileRole.DIAGNOSTIC,
            )
            repository = "local:git-snapshot"
            try:
                context = json.loads(self.artifacts.read_bytes(generated.input_record))
                repository = str(context.get("issue", {}).get("repository_url", repository))
            except (ValueError, UnicodeError):
                pass
            catalog = tuple(
                CapsuleFile(
                    path=name,
                    sha256=file_sha256(stage / name),
                    size_bytes=(stage / name).stat().st_size,
                    media_type="application/x-tar" if name.endswith(".tar") else "text/plain",
                    role=file_role,
                )
                for name, file_role in sorted(roles.items())
            )
            manifest = CapsuleManifest(
                schema_version=SCHEMA_VERSION,
                programming_language="python",
                test_framework="pytest",
                reproduction_command=COMMAND,
                capsule_id=uuid4(),
                mode="diagnostic" if diagnostic_only else "offline",
                repository_url=repository,
                original_commit=original.commit,
                fixed_commit=result.fixed_commit
                if isinstance(result, DifferentialResult)
                else None,
                runtime_version=runtime,
                timeout=environment.policy.timeout_seconds,
                expected_behavior=self.redactor.text(generated.oracle.behavior_contract),
                observed_behavior=self.redactor.text(result.reason),
                execution_status=result.status.value,
                generated_at=datetime.now(UTC),
                files=catalog,
                sources=tuple(sources),
                image=image,
                replay=recipe,
            )
            (stage / "repro.yaml").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
            names = [*roles, "repro.yaml"]
            (stage / "checksums.sha256").write_text(
                "".join(f"{file_sha256(stage / name)}  {name}\n" for name in sorted(names)),
                encoding="ascii",
            )
            packed = stage / "capsule.zip"
            with zipfile.ZipFile(packed, "w", compression=zipfile.ZIP_STORED) as archive:
                for name in sorted([*names, "checksums.sha256"]):
                    archive.write(stage / name, "reproduction/" + name)
            validate_capsule(packed)
            os.link(packed, destination)
            return manifest
