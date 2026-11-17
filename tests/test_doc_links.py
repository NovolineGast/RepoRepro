"""Offline Markdown link checks over temporary Git repositories."""

import subprocess
from pathlib import Path

import pytest
from scripts.check_doc_links import check, links


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text("# Example\n", encoding="utf-8")
    (tmp_path / "docs" / "guide.md").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "README.md", "docs/guide.md"], cwd=tmp_path, check=True)
    return tmp_path


def test_relative_links_images_and_anchors(repo: Path) -> None:
    (repo / "docs" / "image file.svg").write_text("<svg/>", encoding="utf-8")
    (repo / "docs" / "guide.md").write_text(
        '[home](../README.md#example) ![image](image%20file.svg) [self](guide.md "title")',
        encoding="utf-8",
    )
    assert check(repo) == []


def test_missing_link_reports_source_and_destination(repo: Path) -> None:
    (repo / "docs" / "guide.md").write_text("# Guide\n[missing](absent.md)", encoding="utf-8")
    assert check(repo) == ["docs/guide.md:2: missing target: absent.md"]


def test_untracked_files_and_examples_are_not_checked(repo: Path) -> None:
    (repo / "cache.md").write_text("[bad](missing.md)", encoding="utf-8")
    (repo / "docs" / "guide.md").write_text(
        "```md\n[bad](missing.md)\n```\n~~~\n![bad](missing.svg)\n~~~\n"
        "[web](https://example.org) [mail](mailto:a@example.org) [anchor](#here)",
        encoding="utf-8",
    )
    assert check(repo) == []


def test_target_cannot_escape_repository(repo: Path) -> None:
    (repo / "README.md").write_text("[outside](../outside.md)", encoding="utf-8")
    assert check(repo) == ["README.md:1: target outside repository: ../outside.md"]


def test_shorter_or_different_fence_does_not_close_block() -> None:
    assert links("````md\n```\n~~~\n[bad](missing)\n````\n[ok](present)") == [(6, "present")]


def test_command_exits_nonzero_for_missing_target(repo: Path) -> None:
    import sys

    (repo / "README.md").write_text("[bad](absent.md)", encoding="utf-8")
    script = Path(__file__).resolve().parents[1] / "scripts" / "check_doc_links.py"
    result = subprocess.run(
        [sys.executable, str(script), "--root", str(repo)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "README.md:1: missing target: absent.md" in result.stdout
