"""Capsule v1 is a Python/pytest codec, bound only to trusted local adapters."""

from repo_repro.adapters.contracts import AdapterSelection
from repo_repro.adapters.registry import default_selection
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
