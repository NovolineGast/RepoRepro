"""Capsule v1 is a Python/pytest codec, bound only to trusted local adapters."""

from repo_repro.adapters.contracts import AdapterSelection
from repo_repro.adapters.registry import builtin_registry, default_selection
from repo_repro.capsule.schema import COMMAND, CapsuleError
from repo_repro.core.models import CommandSpec


def v1_adapters(selection: AdapterSelection | None = None) -> AdapterSelection:
    selected = selection or default_selection()
    if (selected.language.id, selected.framework.id) != (
        "python",
        "pytest",
    ) or "1.0" not in selected.framework.capsule_versions:
        raise CapsuleError("Capsule v1 requires the Python/pytest adapter binding")
    bundle = selected.framework.bundle("")
    if (
        bundle.command != CommandSpec(argv=COMMAND)
        or bundle.test_path != "repo_repro_generated/test_repro.py"
    ):
        raise CapsuleError("adapter runtime layout is incompatible with Capsule v1")
    return selected


def capsule_adapters(language: str, selection: AdapterSelection | None = None) -> AdapterSelection:
    if language == "python":
        return v1_adapters(selection)
    from repo_repro.adapters.go_test import GO_COMMAND, GO_TEST_PATH

    selected = selection or builtin_registry().resolve(language)
    if (selected.language.id, selected.framework.id) != (
        "go",
        "go-test",
    ) or "2.0" not in selected.framework.capsule_versions:
        raise CapsuleError("Capsule v2 requires the Go/go-test adapter binding")
    bundle = selected.framework.bundle("")
    if bundle.command != CommandSpec(argv=GO_COMMAND) or bundle.test_path != GO_TEST_PATH:
        raise CapsuleError("adapter runtime layout is incompatible with Capsule v2")
    return selected
