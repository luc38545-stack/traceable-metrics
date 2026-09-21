"""traceable-analysis Skill 环境自检的 Git 完整性回归测试。"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
CHECK_ENV_PATH = (
    ROOT / "skills" / "traceable-analysis" / "scripts" / "check_env.py"
)
SPEC = importlib.util.spec_from_file_location("traceable_skill_check_env", CHECK_ENV_PATH)
assert SPEC is not None and SPEC.loader is not None
CHECK_ENV = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK_ENV)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def clean_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "skill-test@example.invalid")
    _git(repo, "config", "user.name", "Skill Test")

    (repo / ".gitignore").write_text("_review*/\n", encoding="utf-8")
    (repo / "core").mkdir()
    (repo / "core" / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", ".gitignore", "core/tracked.py")
    _git(repo, "commit", "--quiet", "-m", "baseline")
    return repo


@pytest.mark.parametrize(
    ("relative_path", "expected_status"),
    [
        ("core/untracked.py", "?? core/untracked.py"),
        (
            "schemas/metrics/commerce/fake.yml",
            "?? schemas/metrics/commerce/fake.yml",
        ),
    ],
)
def test_untracked_product_file_is_dirty(
    clean_git_repo: Path,
    relative_path: str,
    expected_status: str,
) -> None:
    target = clean_git_repo / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("test\n", encoding="utf-8")

    status_ok, changes = CHECK_ENV._git_worktree_changes(clean_git_repo)

    assert status_ok is True
    assert expected_status in changes


def test_explicitly_ignored_review_directory_is_clean(clean_git_repo: Path) -> None:
    target = clean_git_repo / "_review_bt_now" / "result.txt"
    target.parent.mkdir()
    target.write_text("temporary\n", encoding="utf-8")

    status_ok, changes = CHECK_ENV._git_worktree_changes(clean_git_repo)

    assert status_ok is True
    assert changes == []


def test_tracked_file_modification_is_dirty(clean_git_repo: Path) -> None:
    (clean_git_repo / "core" / "tracked.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )

    status_ok, changes = CHECK_ENV._git_worktree_changes(clean_git_repo)

    assert status_ok is True
    assert " M core/tracked.py" in changes
