"""Create an owned, offline Go bug fixture; never execute its source on the host."""

import argparse
import json
from pathlib import Path

from git import Repo

from repo_repro.adapters.registry import builtin_registry
from repo_repro.github.issue_parser import IssueParser
from repo_repro.reproduction.models import ExecutionEnvironment, FailureOracle
from repo_repro.reproduction.planner import build_input

CODE = """package repro
import (
    "testing"
    target "example.com/connector"
)
func TestRepro(t *testing.T) {
    got := target.Normalize("_")
    if got != "_" {
        t.Errorf("connector preserved: got %v", got)
    }
}
"""


def create(output: Path, image: str) -> dict[str, str]:
    output.mkdir(parents=True, exist_ok=False)
    repository = output / "repository"
    repository.mkdir()
    (repository / "go.mod").write_text(
        "module example.com/connector\n\ngo 1.23\n", encoding="utf-8"
    )
    source = repository / "connector.go"
    source.write_text(
        "package connector\nfunc Normalize(s string) string {\n"
        '    if s == "_" { return "__" }; return s\n}\n',
        encoding="utf-8",
    )
    with Repo.init(repository, initial_branch="main") as repo:
        repo.index.add(["go.mod", "connector.go"])
        original = repo.index.commit("duplicate connector", skip_hooks=True).hexsha
        source.write_text(
            "package connector\nfunc Normalize(s string) string { return s }\n", encoding="utf-8"
        )
        repo.index.add(["connector.go"])
        fixed = repo.index.commit("preserve connector", skip_hooks=True).hexsha
    payload = {
        "html_url": "https://github.com/example/connector/issues/1",
        "number": 1,
        "title": "Connector duplicated",
        "body": "Expected: connector preserved. Actual: doubled.",
    }
    issue = IssueParser().parse_payload(payload)
    oracle = FailureOracle(
        language="go",
        node_id="TestRepro",
        exception_type="GoAssertionFailure",
        message_contains="connector preserved",
        issue_quote="connector preserved",
        behavior_contract="Normalize preserves a connector-only input",
    )
    environment = ExecutionEnvironment.model_validate(
        {
            "image": image,
            "policy": {"isolation": "reviewed_docker"},
        }
    )
    context = build_input(
        issue, repository, oracle, ref=original, adapters=builtin_registry().resolve("go")
    )
    for name, record in (
        ("context.json", context),
        ("oracle.json", oracle),
        ("environment.json", environment),
    ):
        (output / name).write_text(record.model_dump_json(indent=2), encoding="utf-8")
    (output / "issue.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output / "test.go").write_text(CODE, encoding="utf-8")
    commits = {"original": original, "fixed": fixed}
    (output / "commits.json").write_text(json.dumps(commits, indent=2), encoding="utf-8")
    return commits


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, default=Path(".repo-repro/go-demo"))
    args = parser.parse_args()
    print(json.dumps(create(args.output, args.image)))


if __name__ == "__main__":
    main()
