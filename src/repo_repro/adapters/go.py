"""Read-only Go Modules inspection and fixed-toolchain inventory."""

import re
from collections.abc import Mapping
from pathlib import PurePosixPath

from repo_repro.adapters.python import EXCLUDED
from repo_repro.analyzer.base import RepositoryView
from repo_repro.core.context import EnvironmentInfo, EvidenceItem, EvidenceOrigin, RepositoryInfo
from repo_repro.core.exceptions import PolicyViolation, RepositoryError, SandboxError
from repo_repro.core.models import ArtifactRef

GO = "/usr/local/go/bin/go"
INVENTORY = """import json,subprocess
v=subprocess.check_output(['/usr/local/go/bin/go','version'],env={'GOTOOLCHAIN':'local'})
print(json.dumps({'go_version':v.decode().strip()}))
"""


class GoAnalyzer:
    def inspect(
        self,
        view: RepositoryView,
        *,
        url: str,
        commit: str,
        default_branch: str | None,
        ref: str,
    ) -> RepositoryInfo:
        paths = view.paths
        modules = [
            p for p in paths if PurePosixPath(p).name == "go.mod" and "vendor" not in p.split("/")
        ]
        if modules != ["go.mod"] or any(PurePosixPath(p).name == "go.work" for p in paths):
            raise RepositoryError("Go requires one root go.mod and no workspace", stage="analyze")
        text = view.read("go.mod").decode("utf-8")
        match = re.search(r'^module\s+"?([A-Za-z0-9._~/-]+)"?\s*(?://[^\n]*)?$', text, re.M)
        if not match or any(part in {"", ".", ".."} for part in match[1].split("/")):
            raise RepositoryError("invalid Go module path", stage="analyze")
        sources = tuple(p for p in paths if GoLanguageAdapter.selectable_path(p))
        if not sources:
            raise RepositoryError("no selectable Go source", stage="analyze")
        for path in sources:
            source = view.read(path).decode("utf-8")
            imports = re.findall(r"\bimport\s+(?:\([^)]*\)|[^\n;]+)", source)
            if any(re.search(r'"C"', item) for item in imports):
                raise RepositoryError("CGO projects are not supported", stage="analyze")
        tests = tuple(p for p in sources if p.endswith("_test.go"))
        version = re.search(r"^go\s+(\S+)", text, re.M)
        evidence = (
            EvidenceItem(value=match[1], origin=EvidenceOrigin.STATIC_FACT, source="go.mod:module"),
        )
        return RepositoryInfo(
            repository_url=url,
            default_branch=default_branch,
            current_commit=commit,
            analyzed_ref=ref,
            files=paths,
            test_files=tests,
            test_directories=tuple(
                sorted(
                    {
                        str(PurePosixPath(p).parent)
                        for p in tests
                        if str(PurePosixPath(p).parent) != "."
                    }
                )
            ),
            environment=EnvironmentInfo(
                go_module=match[1],
                runtime_requirements=(version[1],) if version else (),
                dependency_files=tuple(p for p in ("go.mod", "go.sum") if p in paths),
                package_managers=("go-modules",),
                test_frameworks=("go-test",),
                evidence=evidence,
            ),
            evidence=evidence,
            warnings=(
                "Go support is limited to exported functions in a single pure-Go module; "
                "dependencies must already be available in the reviewed runtime image.",
            ),
        )


class GoLanguageAdapter:
    id = "go"
    source_suffixes = (".go",)
    default_framework = "go-test"
    planning_instructions = (
        "Investigate this Go module using its exported scalar-returning functions. "
        "Select .go source/test files. The test lives in a separate package and cannot access "
        "unexported identifiers. The go_module field identifies the module import prefix."
    )

    def __init__(self) -> None:
        self.analyzer = GoAnalyzer()

    def detect(self, paths: tuple[str, ...]) -> bool:
        return "go.mod" in paths

    @staticmethod
    def selectable_path(path: str) -> bool:
        return path.endswith(".go") and not any(
            p in EXCLUDED | {"vendor", "testdata"} or p.startswith(".env")
            for p in PurePosixPath(path).parts
        )

    def selectable(self, path: str) -> bool:
        return self.selectable_path(path)

    def validate_source_path(self, path: str) -> None:
        ArtifactRef.validate_path(path)
        if not self.selectable(path):
            raise PolicyViolation("Go context requires selectable .go files")

    def default_sources(self, paths: tuple[str, ...], tests: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(p for p in paths if self.selectable(p) and p not in tests)[:5]

    def inventory_command(self) -> tuple[str, ...]:
        return ("/usr/local/bin/python3", "-I", "-S", "-c", INVENTORY)

    def validate_inventory(self, value: Mapping[str, object]) -> dict[str, object]:
        version = value.get("go_version")
        if not isinstance(version, str) or not re.fullmatch(
            r"go version go\S+ linux/amd64", version
        ):
            raise SandboxError("Go toolchain inventory is invalid", stage="preflight")
        return {"go_version": version}

    def fingerprint_identity(self, probe: Mapping[str, object]) -> dict[str, object]:
        return {"go": self.validate_inventory(probe)["go_version"]}

    def runtime_version(self, probe: Mapping[str, object]) -> str:
        return str(self.validate_inventory(probe)["go_version"])
