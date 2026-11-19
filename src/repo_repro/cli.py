"""Command-line entry points for Repo Repro."""

import os
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer
from rich.console import Console

from repo_repro import __version__
from repo_repro.adapters.registry import builtin_registry
from repo_repro.analyzer.service import RepositoryAnalysis
from repo_repro.capsule.cli import app as capsule_app
from repo_repro.config import AppConfig
from repo_repro.core.context import IssueContext
from repo_repro.core.exceptions import InputError, RepoReproError
from repo_repro.github.client import GitHubClient
from repo_repro.github.issue_parser import IssueParser
from repo_repro.reproduction.cli import echo_record, register

app = typer.Typer(
    name="repro",
    help="Repo Repro: bug reproduction, fix verification, and Capsule replay.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
    pretty_exceptions_show_locals=False,
)
console = Console()
register(app)
app.add_typer(capsule_app, name="capsule")


@app.callback()
def main() -> None:
    """Access Repo Repro commands."""


@app.command()
def version() -> None:
    """Show the installed Repo Repro version."""
    config = AppConfig()
    console.print(f"[bold cyan]{config.app_name}[/bold cyan] {__version__}")


def _write_context(context: IssueContext, output: Path | None) -> None:
    serialized = context.model_dump_json(indent=2)
    if output is None:
        echo_record(context)
    else:
        output.write_text(serialized + "\n", encoding="utf-8")
        console.print("Analysis saved to " + str(output), markup=False)


@app.command("issue")
def issue(
    url: Annotated[str | None, typer.Argument(help="Canonical GitHub Issue URL.")] = None,
    issue_json: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
) -> None:
    """Extract Issue data from GitHub or an explicitly supplied local JSON file."""
    try:
        parser = IssueParser(GitHubClient(token=os.environ.get("GITHUB_TOKEN")))
        _write_context(parser.parse(url, issue_json=issue_json), output)
    except (RepoReproError, OSError) as error:
        console.print(str(error), style="red", markup=False)
        raise typer.Exit(2 if isinstance(error, InputError) else 3) from None


@app.command("analyze")
def analyze(
    url: Annotated[str | None, typer.Argument(help="Canonical GitHub Issue URL.")] = None,
    issue_json: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
    repository_path: Annotated[Path | None, typer.Option(exists=True, file_okay=False)] = None,
    cache_dir: Annotated[Path, typer.Option()] = Path(".repo-repro/repositories"),
    ref: Annotated[str | None, typer.Option(help="Explicit Git branch, tag or commit.")] = None,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
    language: str = "python",
) -> None:
    """Fetch an Issue and statically analyze committed repository files."""
    try:
        parser = IssueParser(GitHubClient(token=os.environ.get("GITHUB_TOKEN")))
        context = parser.parse(url, issue_json=issue_json)
        analyzer = RepositoryAnalysis(builtin_registry().resolve(language).language)
        if repository_path is None:
            information = analyzer.clone_and_analyze(
                context.repository_url,
                cache_dir / (uuid4().hex + ".git"),
                ref=ref,
            )
        else:
            information = analyzer.analyze(
                repository_path,
                url=context.repository_url,
                ref=ref or "HEAD",
            )
        _write_context(parser.relate(context, information), output)
    except (RepoReproError, OSError) as error:
        console.print(str(error), style="red", markup=False)
        raise typer.Exit(2 if isinstance(error, InputError) else 3) from None


@app.command("adapters")
def adapters() -> None:
    """List explicitly registered built-in language and test-framework adapters."""
    import json

    typer.echo(json.dumps(builtin_registry().describe(), indent=2))
