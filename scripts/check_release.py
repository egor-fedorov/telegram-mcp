"""Validate a release tag and extract its published changelog entry."""

import argparse
import re
import subprocess
import tomllib
from datetime import date
from pathlib import Path


def release_notes(changelog: str, version: str) -> str:
    headings = list(re.finditer(r"^## (.+)$", changelog, re.MULTILINE))
    matches = [
        (index, heading)
        for index, heading in enumerate(headings)
        if heading.group(1).startswith(f"[{version}]")
    ]
    if len(matches) != 1:
        raise ValueError(f"CHANGELOG.md must contain exactly one [{version}] section.")
    index, heading = matches[0]
    label = re.fullmatch(rf"\[{re.escape(version)}\] - (\d{{4}}-\d{{2}}-\d{{2}})", heading.group(1))
    if label is None:
        raise ValueError("The release changelog heading must include an ISO date.")
    date.fromisoformat(label.group(1))
    end = headings[index + 1].start() if index + 1 < len(headings) else len(changelog)
    notes = changelog[heading.end() : end].strip()
    if not notes:
        raise ValueError("The release changelog entry must not be empty.")
    return notes + "\n"


def git(repo: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *arguments], text=True, encoding="utf-8"
    ).strip()


def check_release(repo: Path, tag: str) -> str:
    project = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = project["version"]
    if re.fullmatch(r"\d+\.\d+\.\d+", version) is None or tag != f"v{version}":
        raise ValueError("The tag must be v<project.version>, with a stable X.Y.Z version.")
    tagged_commit = git(repo, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}")
    if tagged_commit != git(repo, "rev-parse", "HEAD"):
        raise ValueError("The checked-out commit does not match the release tag.")
    subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", tagged_commit, "origin/main"],
        check=True,
    )
    return release_notes((repo / "CHANGELOG.md").read_text(encoding="utf-8"), version)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--notes-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        notes = check_release(args.repo, args.tag)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Release validation failed: {exc}\n")
    args.notes_file.write_text(notes, encoding="utf-8")
    print(f"Validated {args.tag} against project metadata, CHANGELOG.md, and origin/main.")


if __name__ == "__main__":
    main()
