"""Explicit generation/run/verification commands; the model cannot select these options."""

import json
import os
from pathlib import Path
from typing import Annotated

import typer
from pydantic import SecretStr, ValidationError

from repo_repro.adapters.registry import builtin_registry
from repo_repro.ai.openai_compatible import OpenAICompatibleProvider, OpenAIConfig
from repo_repro.core.exceptions import InputError, RepoReproError
from repo_repro.core.models import Record
from repo_repro.github.issue_parser import IssueParser
from repo_repro.reproduction.executor import ReproductionExecutor
from repo_repro.reproduction.generator import TestGenerator
from repo_repro.reproduction.investigator import AIInvestigator
from repo_repro.reproduction.models import (
    DifferentialStatus,
    ExecutionEnvironment,
    FailureOracle,
    GeneratedTest,
    ReproductionInput,
    ReproductionStatus,
)
from repo_repro.sandbox.docker_runner import DockerSandboxRunner
from repo_repro.storage.artifacts import FileArtifactStore
from repo_repro.verification.differential import DifferentialVerifier


def read_json(path: Path) -> bytes:
    with path.open("rb") as stream:
        content = stream.read(524_289)
    if len(content) > 524_288:
        raise InputError("input exceeds 512 KiB")
    return content


def echo_record(record: Record) -> None:
    """Keep structured output lossless on UTF-8 and legacy console encodings alike."""
    typer.echo(json.dumps(record.model_dump(mode="json"), ensure_ascii=True, indent=2))


def write_result(record: Record, output: Path | None) -> None:
    text = record.model_dump_json(indent=2)
    if output:
        output.write_text(text + "\n", encoding="utf-8")
        typer.echo(f"Saved to {output}")
    else:
        echo_record(record)


def failure(error: Exception) -> None:
    message = (
        str(error)
        if isinstance(error, RepoReproError)
        else f"Invalid input or I/O ({type(error).__name__})"
    )
    typer.echo(message, err=True)
    raise typer.Exit(3) from None


def generate(
    context: Path,
    output: Path | None = None,
    artifacts: Path = Path(".repo-repro/evidence"),
    model: str | None = None,
    base_url: str = "https://api.openai.com/v1",
    local_http: bool = False,
    legacy_max_tokens: bool = False,
    test_file: Path | None = None,
    reasoning_effort: str | None = None,
) -> None:
    """Generate a candidate, or seal an explicit user test without AI."""
    try:
        data = ReproductionInput.model_validate_json(read_json(context))
        adapters = builtin_registry().resolve(data.oracle.language)
        store = FileArtifactStore(artifacts)
        if test_file:
            candidate = TestGenerator(store, adapters=adapters).seal_code(
                data, read_json(test_file).decode("utf-8")
            )
        else:
            if model is None:
                raise InputError("provide --model or --test-file")
            key = os.environ.get("OPENAI_API_KEY")
            provider = OpenAICompatibleProvider(
                OpenAIConfig(
                    model=model,
                    base_url=base_url,
                    api_key=SecretStr(key) if key else None,
                    allow_local_http=local_http,
                    token_parameter="max_tokens" if legacy_max_tokens else "max_completion_tokens",
                    reasoning_effort=reasoning_effort,
                ),
                store,
            )
            candidate = TestGenerator(store, provider, adapters=adapters).generate(data)
        write_result(candidate, output)
    except (RepoReproError, ValidationError, ValueError, OSError) as error:
        failure(error)


def reproduce(
    repository: Path,
    test: Path,
    environment: Path,
    commit: str = "HEAD",
    output: Path | None = None,
    artifacts: Path = Path(".repo-repro/evidence"),
) -> None:
    """Run a sealed test repeatedly in fresh sandbox containers."""
    try:
        generated = GeneratedTest.model_validate_json(read_json(test))
        adapters = builtin_registry().resolve(generated.oracle.language)
        settings = ExecutionEnvironment.model_validate_json(read_json(environment))
        store = FileArtifactStore(artifacts)
        backend = DockerSandboxRunner(image=settings.image, artifacts=store, adapters=adapters)
        result = ReproductionExecutor(backend, store, adapters=adapters).run(
            repository, commit, generated, settings
        )
        write_result(result, output)
        if result.status is not ReproductionStatus.REPRODUCED:
            raise typer.Exit(1)
    except (RepoReproError, ValidationError, ValueError, OSError) as error:
        failure(error)


def verify(
    repository: Path,
    original_commit: str,
    fixed_commit: str,
    test: Path,
    environment: Path,
    output: Path | None = None,
    artifacts: Path = Path(".repo-repro/evidence"),
) -> None:
    """Compare commits using the exact same test and runtime."""
    try:
        generated = GeneratedTest.model_validate_json(read_json(test))
        adapters = builtin_registry().resolve(generated.oracle.language)
        settings = ExecutionEnvironment.model_validate_json(read_json(environment))
        store = FileArtifactStore(artifacts)
        backend = DockerSandboxRunner(image=settings.image, artifacts=store, adapters=adapters)
        result = DifferentialVerifier(
            ReproductionExecutor(backend, store, adapters=adapters), store
        ).verify(
            repository,
            original_commit,
            fixed_commit,
            generated,
            settings,
        )
        write_result(result, output)
        if result.status is not DifferentialStatus.DIFFERENTIAL_VALIDATED:
            raise typer.Exit(1)
    except (RepoReproError, ValidationError, ValueError, OSError) as error:
        failure(error)


def register(app: typer.Typer) -> None:
    app.command("generate")(generate)
    app.command("reproduce")(reproduce)
    app.command("verify")(verify)
    app.command("investigate")(investigate)


def investigate(
    repository: Path,
    issue_json: Path,
    oracle: Path,
    environment: Path,
    model: Annotated[str, typer.Option(help="Explicit model ID for planning and test generation.")],
    ref: str = "HEAD",
    fixed_commit: str | None = None,
    max_candidates: int = 3,
    max_plan_attempts: int = 2,
    max_model_tokens: int = 4096,
    output: Path | None = None,
    result_output: Path | None = None,
    artifacts: Path = Path(".repo-repro/evidence"),
    base_url: str = "https://api.openai.com/v1",
    local_http: bool = False,
    legacy_max_tokens: bool = False,
    reasoning_effort: str | None = None,
    initial_test: Path | None = None,
) -> None:
    """Use AI to localize a bug and revise candidates from bounded original-version feedback."""
    try:
        if not 1 <= max_candidates <= 5:
            raise InputError("--max-candidates must be between 1 and 5")
        contract = FailureOracle.model_validate_json(read_json(oracle))
        adapters = builtin_registry().resolve(contract.language)
        settings = ExecutionEnvironment.model_validate_json(read_json(environment))
        issue = IssueParser().parse(issue_json=issue_json)
        store = FileArtifactStore(artifacts)
        key = os.environ.get("OPENAI_API_KEY")
        provider = OpenAICompatibleProvider(
            OpenAIConfig(
                model=model,
                base_url=base_url,
                api_key=SecretStr(key) if key else None,
                allow_local_http=local_http,
                max_completion_tokens=max_model_tokens,
                token_parameter="max_tokens" if legacy_max_tokens else "max_completion_tokens",
                reasoning_effort=reasoning_effort,
            ),
            store,
        )
        backend = DockerSandboxRunner(image=settings.image, artifacts=store, adapters=adapters)
        session = AIInvestigator(
            provider, ReproductionExecutor(backend, store, adapters=adapters), store
        ).run(
            issue,
            repository,
            contract,
            settings,
            ref=ref,
            max_candidates=max_candidates,
            fixed_commit=fixed_commit,
            max_plan_attempts=max_plan_attempts,
            initial_code=read_json(initial_test).decode("utf-8")
            if initial_test is not None
            else None,
        )
        write_result(session, output)
        selected = session.verification or session.selected_result
        if result_output is not None and selected is not None:
            write_result(selected, result_output)
        if session.verification is not None:
            if session.verification.status is not DifferentialStatus.DIFFERENTIAL_VALIDATED:
                raise typer.Exit(1)
        elif session.stop_reason != "reproduced":
            raise typer.Exit(1)
    except (RepoReproError, ValidationError, ValueError, OSError) as error:
        failure(error)
