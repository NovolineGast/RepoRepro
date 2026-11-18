"""Prepare inputs for huandu/xstrings #64 from an existing reviewed clone."""

import argparse
import json
from pathlib import Path

from examples.create_go_demo import CODE
from repo_repro.adapters.registry import builtin_registry
from repo_repro.github.issue_parser import IssueParser
from repo_repro.reproduction.models import ExecutionEnvironment, FailureOracle
from repo_repro.reproduction.planner import build_input

ORIGINAL = "82cd3c3cf9e0c01f474e3f88a9c65ab60eb39e79"
FIXED = "a9fcd95229fb6b76828e4a6882474767da2bfb20"


def create(repository: Path, output: Path, image: str) -> None:
    output.mkdir(parents=True, exist_ok=False)
    quote = "Every connector of the input is preserved by design"
    payload = {
        "html_url": "https://github.com/huandu/xstrings/issues/64",
        "number": 64,
        "title": "Connector-only case conversion duplicates the final rune",
        "body": "Excerpt from upstream issue #64: " + quote,
    }
    oracle = FailureOracle(
        language="go",
        node_id="TestRepro",
        exception_type="GoAssertionFailure",
        message_contains="connector preserved",
        issue_quote=quote,
        behavior_contract="ToCamelCase preserves a connector-only string such as underscore",
    )
    context = build_input(
        IssueParser().parse_payload(payload),
        repository,
        oracle,
        ref=ORIGINAL,
        source_paths=("convert.go",),
        infer_tests=False,
        adapters=builtin_registry().resolve("go"),
    )
    environment = ExecutionEnvironment.model_validate(
        {"image": image, "policy": {"isolation": "reviewed_docker"}}
    )
    for name, record in (
        ("context.json", context),
        ("oracle.json", oracle),
        ("environment.json", environment),
    ):
        (output / name).write_text(record.model_dump_json(indent=2), encoding="utf-8")
    (output / "issue.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output / "test.go").write_text(
        CODE.replace("example.com/connector", "github.com/huandu/xstrings").replace(
            "target.Normalize", "target.ToCamelCase"
        ),
        encoding="utf-8",
    )
    (output / "commits.json").write_text(
        json.dumps({"original": ORIGINAL, "fixed": FIXED}), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(".repo-repro/go-public"))
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    create(args.repository, args.output, args.image)


if __name__ == "__main__":
    main()
