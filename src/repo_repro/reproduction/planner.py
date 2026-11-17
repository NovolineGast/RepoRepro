"""Build bounded, commit-bound generation inputs from read-only Git objects."""

from pathlib import Path

from repo_repro.adapters.contracts import AdapterSelection
from repo_repro.adapters.registry import default_selection
from repo_repro.analyzer.repository import GitRepository
from repo_repro.analyzer.service import RepositoryAnalysis
from repo_repro.core.context import IssueContext
from repo_repro.core.exceptions import InputError
from repo_repro.github.issue_parser import IssueParser
from repo_repro.reproduction.models import FailureOracle, ReproductionInput, SourceFile


def validate_input(context: ReproductionInput, adapters: AdapterSelection | None = None) -> None:
    selected = adapters or default_selection()
    if context.oracle.issue_quote not in context.issue.body:
        raise InputError("oracle must be anchored in the supplied Issue body")
    if context.issue.repository_url != context.repository.repository_url:
        raise InputError("Issue and repository identities differ")
    selected.framework.validate_context(context)
    files = (*context.relevant_source_files, *context.existing_tests)
    if len({item.path for item in files}) != len(files):
        raise InputError("duplicate context files")
    if any(item.path not in context.repository.files for item in files):
        raise InputError("context file is absent from the analyzed Git tree")
    for item in files:
        selected.language.validate_source_path(item.path)
    if sum(len(item.content.encode()) for item in files) > 262_144:
        raise InputError("source/test context exceeds the 256 KiB budget")


def build_input(
    issue: IssueContext,
    repository_path: Path,
    oracle: FailureOracle,
    *,
    ref: str = "HEAD",
    source_paths: tuple[str, ...] = (),
    test_paths: tuple[str, ...] = (),
    infer_tests: bool = True,
    adapters: AdapterSelection | None = None,
) -> ReproductionInput:
    selected = adapters or default_selection()
    repository = RepositoryAnalysis(selected.language).analyze(
        repository_path,
        url=issue.repository_url,
        ref=ref,
    )
    related = IssueParser().relate(issue, repository)
    sources = source_paths or tuple(
        item.value
        for item in related.related_code_files
        if item.value in repository.files and item.value not in repository.test_files
    )
    if not sources:
        sources = selected.language.default_sources(repository.files, repository.test_files)
    tests = (
        (
            test_paths
            or tuple(item.value for item in related.possible_test_files)
            or repository.test_files[:3]
        )
        if infer_tests
        else test_paths
    )
    with GitRepository().open(repository_path, ref=repository.current_commit) as view:

        def load(paths: tuple[str, ...]) -> tuple[SourceFile, ...]:
            return tuple(
                SourceFile(path=path, content=view.read(path).decode("utf-8")) for path in paths
            )

        context = ReproductionInput(
            issue=issue,
            repository=repository,
            environment=repository.environment,
            relevant_source_files=load(sources),
            existing_tests=load(tests),
            oracle=oracle,
        )
    validate_input(context, selected)
    return context
