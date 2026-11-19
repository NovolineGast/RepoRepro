"""Check release archives for required public files and accidental local artifacts."""

import argparse
import tarfile
import zipfile
from pathlib import Path

LOCAL = {".tools", ".repo-repro", "reports", ".venv", "__pycache__"}
DRAFTS = {"docs/qa.md", "docs/development.md", "docs/architecture.md", "docs/adr.md"}
REQUIRED = {
    "README.md",
    "LICENSE",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "CHANGELOG.md",
    ".github/workflows/ci.yml",
    "docs/assets/banner.svg",
    "docs/assets/demo.gif",
    "docs/go.md",
    "docs/cases/xstrings-64.md",
    "examples/run_demo.py",
    "examples/issue.json",
}


def check(directory: Path) -> None:
    wheels = list(directory.glob("*.whl"))
    sources = list(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise ValueError("supply a directory with exactly one wheel and one source distribution")
    with tarfile.open(sources[0]) as archive:
        names = {name.partition("/")[2] for name in archive.getnames()}
    if any(LOCAL.intersection(Path(name).parts) for name in names) or DRAFTS.intersection(names):
        raise ValueError("source distribution includes local artifacts or private design drafts")
    missing = REQUIRED - names
    if missing:
        raise ValueError("source distribution is missing: " + ", ".join(sorted(missing)))
    with zipfile.ZipFile(wheels[0]) as archive:
        if "repo_repro/reproduction/pytest_plugin.txt" not in archive.namelist():
            raise ValueError("wheel is missing the trusted pytest collector resource")
    print("Release archive contents checked")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    check(parser.parse_args().directory)


if __name__ == "__main__":
    main()
