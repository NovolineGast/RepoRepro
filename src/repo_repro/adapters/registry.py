"""Deterministic adapter registration; never import implementations from untrusted data."""

import re

from repo_repro.adapters.contracts import AdapterSelection, LanguageAdapter, TestFrameworkAdapter
from repo_repro.core.exceptions import InputError


class AdapterRegistry:
    def __init__(self) -> None:
        self._languages: dict[str, LanguageAdapter] = {}
        self._frameworks: dict[tuple[str, str], TestFrameworkAdapter] = {}

    @staticmethod
    def _id(value: str) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", value):
            raise InputError("invalid adapter identifier")

    def register_language(self, adapter: LanguageAdapter) -> None:
        self._id(adapter.id)
        if adapter.id in self._languages:
            raise InputError("language adapter is already registered")
        self._languages[adapter.id] = adapter

    def register_framework(self, adapter: TestFrameworkAdapter) -> None:
        self._id(adapter.id)
        key = (adapter.language_id, adapter.id)
        if adapter.language_id not in self._languages or key in self._frameworks:
            raise InputError("framework requires a registered language and a unique identifier")
        self._frameworks[key] = adapter

    def resolve(self, language: str = "python", framework: str | None = None) -> AdapterSelection:
        if language not in self._languages:
            raise InputError(f"unsupported language adapter: {language}")
        selected = self._languages[language]
        key = (language, framework or selected.default_framework)
        if key not in self._frameworks:
            raise InputError(f"unsupported framework adapter: {key[1]} for {language}")
        return AdapterSelection(selected, self._frameworks[key])

    def detect(self, paths: tuple[str, ...]) -> AdapterSelection:
        matches = [name for name, adapter in self._languages.items() if adapter.detect(paths)]
        if len(matches) != 1:
            raise InputError("no unique supported language adapter was detected; select explicitly")
        return self.resolve(matches[0])

    def describe(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "language": name,
                "source_suffixes": adapter.source_suffixes,
                "frameworks": tuple(
                    framework
                    for language, framework in sorted(self._frameworks)
                    if language == name
                ),
            }
            for name, adapter in sorted(self._languages.items())
        )


def builtin_registry() -> AdapterRegistry:
    # Imports are local to keep package import free of Git/process probes.
    from repo_repro.adapters.pytest import PytestAdapter
    from repo_repro.adapters.python import PythonLanguageAdapter

    registry = AdapterRegistry()
    registry.register_language(PythonLanguageAdapter())
    registry.register_framework(PytestAdapter())
    return registry


def default_selection() -> AdapterSelection:
    return builtin_registry().resolve()
