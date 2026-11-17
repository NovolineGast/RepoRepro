"""Read-only Git acquisition and analysis dispatch through a selected language adapter."""

from pathlib import Path

from repo_repro.adapters.contracts import LanguageAdapter
from repo_repro.adapters.registry import default_selection
from repo_repro.analyzer.repository import GitRepository
from repo_repro.core.context import RepositoryInfo
from repo_repro.core.urls import repository_url


class RepositoryAnalysis:
    def __init__(
        self, language: LanguageAdapter | None = None, source: GitRepository | None = None
    ) -> None:
        self.language = language or default_selection().language
        self.source = source or GitRepository()

    def analyze(self, path: Path, *, url: str, ref: str = "HEAD") -> RepositoryInfo:
        with self.source.open(path, ref=ref) as view:
            info = self.language.analyzer.inspect(
                view,
                url=repository_url(url),
                commit=view.commit,
                default_branch=view.default_branch,
                ref=ref,
            )
            return info.model_copy(
                update={
                    "warnings": info.warnings + tuple(view.warnings),
                    "origin_url": view.origin_url,
                    "repository_identity_verified": False,
                }
            )

    def clone_and_analyze(
        self, url: str, destination: Path, *, ref: str | None = None
    ) -> RepositoryInfo:
        self.source.clone(url, destination, ref=ref)
        info = self.analyze(destination, url=url, ref="FETCH_HEAD" if ref else "HEAD")
        return info.model_copy(
            update={"repository_identity_verified": info.origin_url == repository_url(url)}
        )
