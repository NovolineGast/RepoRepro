"""Python project inspection, context selection and runtime inventory."""

from collections.abc import Mapping
from pathlib import PurePosixPath

from repo_repro.analyzer.python_analyzer import PythonAnalyzer
from repo_repro.core.exceptions import PolicyViolation, SandboxError
from repo_repro.reproduction.models import SourceFile

EXCLUDED = {
    ".git",
    ".ssh",
    ".aws",
    ".azure",
    ".docker",
    ".config",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "repo_repro_generated",
}

PYTHON_INVENTORY = """import importlib.metadata, json, sys, sysconfig
paths = list({sysconfig.get_path('purelib'), sysconfig.get_path('platlib')})
print(json.dumps({'python_version': sys.version,
    'packages': sorted((dist.metadata.get('Name', ''), dist.version)
                      for dist in importlib.metadata.distributions(path=paths))}))
"""


class PythonLanguageAdapter:
    id = "python"
    source_suffixes = (".py",)
    default_framework = "pytest"
    planning_instructions = (
        "Investigate the supplied Python project; select Python source and test files."
    )

    def __init__(self) -> None:
        self.analyzer = PythonAnalyzer()

    def detect(self, paths: tuple[str, ...]) -> bool:
        return any(path.endswith(".py") or path == "pyproject.toml" for path in paths)

    def selectable(self, path: str) -> bool:
        return path.endswith(".py") and not any(
            part in EXCLUDED or part.startswith(".env") for part in PurePosixPath(path).parts
        )

    def validate_source_path(self, path: str) -> None:
        SourceFile.safe_python_path(path)

    def default_sources(self, paths: tuple[str, ...], tests: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            path
            for path in paths
            if path.endswith(".py")
            and path not in tests
            and PurePosixPath(path).name not in {"setup.py", "conftest.py"}
        )[:5]

    def inventory_command(self) -> tuple[str, ...]:
        return ("/usr/local/bin/python3", "-I", "-S", "-c", PYTHON_INVENTORY)

    def validate_inventory(self, value: Mapping[str, object]) -> dict[str, object]:
        packages = value.get("packages")
        if not isinstance(value.get("python_version"), str) or not isinstance(packages, list):
            raise SandboxError("runtime inventory is incomplete", stage="preflight")
        if len(packages) > 2000 or any(
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
            for item in packages
        ):
            raise SandboxError("runtime package inventory is invalid", stage="preflight")
        return {"python_version": value["python_version"], "packages": packages}

    def fingerprint_identity(self, probe: Mapping[str, object]) -> dict[str, object]:
        if not probe.get("python_version"):
            raise PolicyViolation("runtime identity is incomplete")
        return {"python": probe.get("python_version"), "packages": probe.get("packages")}

    def runtime_version(self, probe: Mapping[str, object]) -> str:
        return str(probe.get("python_version", "unknown"))
