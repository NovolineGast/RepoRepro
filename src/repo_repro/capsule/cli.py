"""Data-only capsule operations and a separately explicit replay command."""

from pathlib import Path
from typing import Annotated, NoReturn

import typer
from pydantic import ValidationError

from repo_repro.capsule.archive import extract_capsule, validate_capsule
from repo_repro.capsule.exporter import CapsuleExporter
from repo_repro.capsule.replay import CapsuleReplay
from repo_repro.capsule.schema import CapsuleManifest, json_object
from repo_repro.core.exceptions import RepoReproError
from repo_repro.core.models import SandboxPolicy
from repo_repro.reproduction.cli import echo_record, read_json
from repo_repro.reproduction.models import DifferentialResult, ReproductionResult
from repo_repro.storage.artifacts import FileArtifactStore

app = typer.Typer(
    help="Versioned, verified capsules. Inspect/extract never execute code.", no_args_is_help=True
)


def fail(error: Exception) -> NoReturn:
    typer.echo(
        str(error)
        if isinstance(error, RepoReproError)
        else f"Invalid capsule input ({type(error).__name__})",
        err=True,
    )
    raise typer.Exit(3) from None


@app.command("schema")
def schema() -> None:
    import json

    typer.echo(json.dumps(CapsuleManifest.model_json_schema(), indent=2))


@app.command("export")
def export(
    result: Path,
    destination: Path,
    artifacts: Path = Path(".repo-repro/evidence"),
    diagnostic_only: bool = False,
) -> None:
    """Export captured sources/runtime or explicitly non-replayable diagnostics."""
    try:
        data = json_object(read_json(result))
        record = (
            DifferentialResult.model_validate(data)
            if isinstance(data, dict) and "original" in data
            else ReproductionResult.model_validate(data)
        )
        manifest = CapsuleExporter(FileArtifactStore(artifacts)).export(
            record, destination, diagnostic_only=diagnostic_only
        )
        echo_record(manifest)
    except (RepoReproError, ValidationError, ValueError, OSError) as error:
        fail(error)


def _validated_manifest(capsule: Path) -> CapsuleManifest:
    try:
        return validate_capsule(capsule)
    except (RepoReproError, ValueError, OSError) as error:
        fail(error)


def _console_value(value: str) -> str:
    """Escape non-ASCII and non-printable characters in free-form manifest fields."""
    return value if value.isascii() and value.isprintable() else ascii(value)


@app.command("validate")
def validate(capsule: Path) -> None:
    """Validate schema, complete file catalog and hashes; never load images or execute."""
    echo_record(_validated_manifest(capsule))


@app.command("inspect")
def inspect(
    capsule: Path,
    summary: Annotated[
        bool, typer.Option("--summary", help="Show runtime, recorded outcome and commits as text.")
    ] = False,
) -> None:
    """Inspect a validated capsule as JSON or a short summary without executing it."""
    manifest = _validated_manifest(capsule)
    if not summary:
        echo_record(manifest)
        return
    typer.echo(
        "\n".join(
            [
                f"Schema version: {manifest.schema_version}",
                f"Mode: {manifest.mode}",
                f"Language/framework: {manifest.programming_language} / {manifest.test_framework}",
                f"Runtime version: {_console_value(manifest.runtime_version)}",
                f"Execution status: {_console_value(manifest.execution_status)}",
                f"Original commit: {manifest.original_commit}",
                f"Fixed commit: {manifest.fixed_commit or 'unavailable'}",
            ]
        )
    )


@app.command("unpack")
def unpack(capsule: Path, destination: Path) -> None:
    """Validate and safely extract into a new directory; scripts remain data."""
    try:
        manifest = extract_capsule(capsule, destination)
        typer.echo(f"Extracted {manifest.capsule_id} into {destination}; nothing executed")
    except (RepoReproError, ValueError, OSError) as error:
        fail(error)


@app.command("replay")
def replay(
    capsule: Path, workspace: Path, policy: Annotated[Path, typer.Option("--policy")]
) -> None:
    """Authorize sandbox replay under a LOCAL policy, without bundled scripts."""
    try:
        selected = SandboxPolicy.model_validate(json_object(read_json(policy)))
        report = CapsuleReplay().replay(capsule, workspace, local_policy=selected, authorized=True)
        echo_record(report)
        if report.status != "MATCHED":
            raise typer.Exit(1)
    except (RepoReproError, ValidationError, ValueError, OSError) as error:
        fail(error)
