# Contributing

Use English for repository documentation, code comments, CLI text, commit messages,
and pull requests. Keep multilingual fixtures when their language matters to a test.
See [ROADMAP.md](ROADMAP.md) for priorities and milestone acceptance criteria.

## Local setup and checks

Use Python 3.12 and [uv](https://docs.astral.sh/uv/). From the project directory:

```sh
uv sync --locked
make check
```

`make check` runs Ruff formatting checks, Ruff lint, ty, and pytest. Tests are offline:
they do not require a Telegram account, login, or network requests to Telegram.
Dependency installation may require network access. Add tests for changed behavior
and keep existing MCP contracts compatible, or explain any compatibility changes.
For documentation-only changes, check links, examples, and formatting.

## Branches, commits, and pull requests

1. Start from the current `main` and create a branch for one bounded change. Follow
   [Conventional Branch](https://conventionalbranch.org): use `feature/`, `bugfix/`,
   `hotfix/`, or `chore/` with a lowercase kebab-case description, such as
   `feature/public-channel-discovery` or `bugfix/preserve-retry-delay`.
   Documentation-only work uses a branch such as `chore/update-docs`.
   Release branches use `release/vX.Y.Z`, for example `release/v1.2.0`.
2. Use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) for
   commit messages and the PR title: `<type>[optional scope]: <description>`.
   Examples: `feat(discovery): add public channel search`,
   `fix: preserve retry delays`, and `docs: explain discovery permissions`.
3. Run the relevant checks and open a pull request targeting `main`. Describe the
   problem, resulting behavior, and validation. Link a related issue or roadmap item
   when applicable. Update documentation for public behavior or configuration changes.
4. Address review feedback and wait for CI to pass. Use squash merge after review;
   the final PR title becomes the squash commit message. Do not push feature work
   directly to `main`.

Keep changes focused. Propose a new direction with a feature request before expanding
an agreed milestone. Add or update roadmap completion records only when their acceptance
criteria are met; distinguish offline checks from any separately performed live validation.

## Reporting issues

Use the bug report form for a minimal reproduction, version information, and expected
versus actual behavior. Use the feature request form to explain the use case, proposed
scope, and relationship to the roadmap.

Never include phone numbers, login codes, API keys or hashes, session files, credentials,
or private Telegram conversation dumps in issues, pull requests, logs, or fixtures.
Use synthetic examples and remove sensitive data from diagnostics before sharing them.
