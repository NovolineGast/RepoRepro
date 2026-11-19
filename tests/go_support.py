"""Owned Go fixture with one exported pure function and a caller-owned oracle."""

from pathlib import Path

from git import Repo

from repo_repro.github.issue_parser import IssueParser
from repo_repro.reproduction.models import FailureOracle

CODE = """package repro
import (
    "testing"
    target "example.com/text"
)
func TestRepro(t *testing.T) {
    got := target.Normalize("_")
    if got != "_" {
        t.Errorf("connector preserved: got %v", got)
    }
}
"""


def oracle() -> FailureOracle:
    return FailureOracle(
        language="go",
        node_id="TestRepro",
        exception_type="GoAssertionFailure",
        message_contains="connector preserved",
        issue_quote="connector preserved",
        behavior_contract="Normalize preserves a connector-only input",
    )


def issue_payload() -> dict[str, object]:
    return {
        "html_url": "https://github.com/example/text/issues/1",
        "number": 1,
        "title": "Duplicate connector",
        "body": "Expected: connector preserved. Actual: doubled.",
    }


def issue() -> object:
    return IssueParser().parse_payload(issue_payload())


def repository_pair(path: Path) -> tuple[str, str]:
    path.mkdir()
    (path / "go.mod").write_text("module example.com/text\n\ngo 1.23\n", encoding="utf-8")
    source = path / "text.go"
    source.write_text(
        'package text\nfunc Normalize(s string) string { if s == "_" { return "__" }; return s }\n',
        encoding="utf-8",
    )
    with Repo.init(path, initial_branch="main") as repo:
        repo.index.add(["go.mod", "text.go"])
        original = repo.index.commit("duplicate connector", skip_hooks=True).hexsha
        source.write_text(
            "package text\nfunc Normalize(s string) string { return s }\n", encoding="utf-8"
        )
        repo.index.add(["text.go"])
        fixed = repo.index.commit("preserve connector", skip_hooks=True).hexsha
    return original, fixed
