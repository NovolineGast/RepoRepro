"""Check ordinary relative links in Git-tracked Markdown without network access."""

import argparse
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

_LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*(?:<([^>]+)>|([^\s)]+))(?:\s+[^)]*)?\)")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def links(text: str) -> list[tuple[int, str]]:
    """Return line numbers and destinations, excluding fenced examples."""
    found: list[tuple[int, str]] = []
    fence = ""
    for number, line in enumerate(text.splitlines(), start=1):
        marker = _FENCE.match(line)
        if fence:
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= len(fence)
                and not marker[2].strip()
            ):
                fence = ""
            continue
        if marker:
            fence = marker[1]
            continue
        found.extend((number, match[1] or match[2]) for match in _LINK.finditer(line))
    return found


def check(root: Path) -> list[str]:
    """Report missing files and targets that escape the repository root."""
    root = root.resolve()
    tracked = subprocess.check_output(["git", "ls-files", "-z", "--", "*.md"], cwd=root)
    errors = []
    for name in tracked.decode("utf-8").split("\0"):
        if not name:
            continue
        source = root / name
        for number, destination in links(source.read_text(encoding="utf-8")):
            parsed = urlsplit(destination)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            target = (source.parent / unquote(parsed.path)).resolve()
            if not target.is_relative_to(root):
                errors.append(f"{name}:{number}: target outside repository: {destination}")
            elif not target.exists():
                errors.append(f"{name}:{number}: missing target: {destination}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        errors = check(args.root)
    except (OSError, subprocess.CalledProcessError, UnicodeError) as exc:
        parser.exit(2, f"Could not check documentation links: {exc}\n")
    for error in errors:
        print(error)
    if not errors:
        print("Tracked Markdown links checked")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
