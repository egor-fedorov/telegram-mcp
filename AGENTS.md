# Repository conventions

## Language

- Use English for documentation, code comments and docstrings, CLI text,
  identifiers, test names, and commit messages.
- Preserve multilingual source content and test fixtures when their language
  is relevant to the behavior being tested.
- Conversation replies may follow the user's language.

## GitHub workflow

- Work on a focused branch and open a pull request to `main`; avoid direct pushes to `main`.
- Use Conventional Branch purpose prefixes: `feature/`, `bugfix/`, `hotfix/`,
  `chore/`, or `release/`. Use lowercase kebab-case descriptions; release versions
  may contain dots. Example: `feature/public-channel-discovery`.
- Dependabot-generated `dependabot/` branches are the approved automation exception;
  human-created branches must still use the Conventional Branch prefixes above.
- Use Conventional Commits for commit messages and PR titles.
- Run `make check`, review the diff, and wait for the GitHub `checks` job before merging.
- Squash-merge completed PRs. Start the next branch from the updated `main`.
- Update `ROADMAP.md` with completed work. See `CONTRIBUTING.md` for contributor guidance.
- Review dependency-update PRs manually; do not enable automatic merging.
- Keep public changes in `CHANGELOG.md`. Follow `docs/releasing.md` for version tags
  and PyPI publication; do not treat a merged PR as a published release.
