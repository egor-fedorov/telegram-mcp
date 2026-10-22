# Telegram Context MCP

## Rules

Hard rules; stop and report instead of working around them.

- No silent fallback: fail with an actionable error.
- Never commit or log phone numbers, login codes, API IDs or hashes, session files, HTTP tokens,
  or private Telegram content; use synthetic fixtures.
- Never report an unrun or skipped check as passed; run checks after the last change.
- When docs, config, code, and tests disagree, report the contradiction instead of choosing.
- Keep the MCP tool schemas recorded in `tests/fixtures/mcp_contract.json` compatible, or
  explain the compatibility change in the PR.

## Branches

- Start every branch from the updated `main` and open a pull request to `main`; never push to
  `main` directly. Squash-merge completed PRs.
- Name branches with a Conventional Branch prefix (`feature/`, `bugfix/`, `hotfix/`, `chore/`,
  `release/`) and a lowercase kebab-case description; release versions may contain dots.
  Dependabot's `dependabot/` branches are the only exception.
- Use Conventional Commits for commit messages and PR titles.

## Work

- Make the smallest correct change. Prefer, in order: no change, existing code, the standard
  library, an installed dependency, new code. Stay in scope.
- Read narrowly: focused search, bounded reads, `git diff --stat` before diffs, quiet test output.
- Use `uv sync --locked` and `uv run --locked` instead of pip or system Python; commit `uv.lock`
  with dependency changes.
- Never read or search `uv.lock`, `dist/`, or `.venv/`.
- Fastest checks: `uv run --locked pytest -q <test path>`, `uv run --locked ruff check <paths>`,
  `uv run --locked ty check`.
- Run `make check` on request and before a PR; review the diff and wait for the GitHub `checks`
  job before merging.
- Do not create plan, summary, or notes files unless asked.
- Update `ROADMAP.md` with completed work; see `CONTRIBUTING.md` for contributor guidance.
- Review dependency-update PRs manually; do not enable automatic merging.
- Keep public changes in `CHANGELOG.md`. Follow `docs/releasing.md` for version tags and PyPI
  publication; do not treat a merged PR as a published release.

## Language

- Use English for documentation, code comments and docstrings, CLI text, identifiers, test names,
  and commit messages. Conversation replies may follow the user's language.
- Preserve multilingual source content and test fixtures when their language is relevant to the
  behavior being tested.

## Report

One line per point: result, checks run with outcome, concrete risks. Write nothing else: no empty
categories, no actions not taken (such as "not committed"), nothing the user already sees.
