"""Distribution identity and release-gate behavior without network access."""

import subprocess
import tomllib
from pathlib import Path

import pytest

from scripts.check_release import check_release, git, release_notes
from telegram_mcp.adapters.version import get_version


def test_installed_distribution_version_matches_manifest() -> None:
    manifest = Path(__file__).resolve().parents[1] / "pyproject.toml"
    project = tomllib.loads(manifest.read_text(encoding="utf-8"))["project"]
    assert get_version() == project["version"]


def test_release_notes_select_only_the_tagged_release() -> None:
    notes = release_notes(
        "# Changelog\n\n## [Unreleased]\n\nFuture work.\n\n"
        "## [0.2.0] - 2026-10-21\n\n### Added\n\n- Published package.\n\n"
        "## [0.1.0] - 2026-10-01\n\nInitial implementation.\n",
        "0.2.0",
    )
    assert notes == "### Added\n\n- Published package.\n"


@pytest.mark.parametrize(
    "changelog",
    [
        "## [Unreleased]\n\nNot released.\n",
        "## [0.2.0] - 2026-10-21\n\n",
        "## [0.2.0]\n\nMissing date.\n",
        "## [0.2.0] - 2026-02-30\n\nInvalid date.\n",
        "## [0.2.0] - 2026-10-21\n\nOne.\n## [0.2.0] - 2026-10-22\n\nTwo.\n",
    ],
)
def test_invalid_release_notes_are_rejected(changelog: str) -> None:
    with pytest.raises(ValueError):
        release_notes(changelog, "0.2.0")


@pytest.fixture
def release_repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Package Test")
    git(tmp_path, "config", "commit.gpgsign", "false")
    git(tmp_path, "config", "tag.gpgsign", "false")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.2.0"\n', encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [0.2.0] - 2026-10-21\n\nPublished package.\n", encoding="utf-8"
    )
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "test: prepare release")
    git(tmp_path, "tag", "v0.2.0")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    return tmp_path


def test_release_tag_matches_manifest_changelog_and_main(release_repo: Path) -> None:
    assert check_release(release_repo, "v0.2.0") == "Published package.\n"


def test_release_version_mismatch_is_rejected(release_repo: Path) -> None:
    with pytest.raises(ValueError, match="tag must"):
        check_release(release_repo, "v0.3.0")


def test_release_tag_cannot_point_to_another_checkout(release_repo: Path) -> None:
    git(release_repo, "commit", "--allow-empty", "-m", "test: another commit")
    with pytest.raises(ValueError, match="checked-out commit"):
        check_release(release_repo, "v0.2.0")


def test_release_commit_must_be_merged_to_main(release_repo: Path) -> None:
    git(release_repo, "commit", "--allow-empty", "-m", "test: unmerged release")
    git(release_repo, "tag", "-f", "v0.2.0")
    with pytest.raises(subprocess.CalledProcessError):
        check_release(release_repo, "v0.2.0")
