# Contributing

Use English for repository documentation, code comments, CLI text, commit messages,
and pull requests. Keep multilingual fixtures when their language matters to a test.
See [ROADMAP.md](ROADMAP.md) for current priorities, [Architecture](docs/architecture.md)
for dependency boundaries, and [completed specifications](docs/specs/README.md) for
the accepted M1/M2 contracts.

## Local setup and checks

Use Python 3.12 and [uv](https://docs.astral.sh/uv/) for the standard development
environment. The supported runtime range is Python 3.12–3.15 on Windows, macOS, and
Linux. From the project directory:

```sh
uv sync --locked --python 3.12
make check
```

`make check` runs Ruff formatting checks, Ruff lint, ty, and pytest. Tests are offline:
they do not require a Telegram account, login, or network requests to Telegram.
Dependency installation may require network access. Add tests for changed behavior
and keep existing MCP contracts compatible, or explain any compatibility changes.
For documentation-only changes, check links, examples, and formatting.

On Windows without Make, run the same checks directly:

```sh
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked ty check
uv run --locked pytest
```

CI checks formatting/types, builds wheel and source distributions, and runs tests
plus clean-install smoke checks. The matrix covers Python 3.12–3.15 on Linux and
3.12/3.15 on macOS and Windows. The required `checks` job succeeds only when its
dependencies succeed. No Telegram credentials or publishing secrets are needed.

When changing transport behavior, cover standalone STDIO, authenticated local HTTP,
and the STDIO proxy without a real Telegram account. Shared-client tests should
exercise lifecycle and cursor behavior, including a client disconnecting while the
session-owning server remains available. Preserve the recorded schemas of all existing tools
within a contract version. Intentional versioned changes require updated fixtures,
migration guidance, and transport parity tests. See the [0.3 migration](docs/migration-0.3.md)
and [bounded live validation](docs/live-validation.md).

## Branches, commits, and pull requests

1. Start from the current `main` and create a branch for one bounded change. Follow
   [Conventional Branch](https://conventionalbranch.org): use `feature/`, `bugfix/`,
   `hotfix/`, or `chore/` with a lowercase kebab-case description, such as
   `feature/public-channel-discovery` or `bugfix/preserve-retry-delay`.
   Documentation-only work uses a branch such as `chore/update-docs`.
   Release branches use `release/vX.Y.Z`, for example `release/v1.2.0`.
   Dependabot's generated `dependabot/` branches are the approved automation exception;
   human-created branches still use the Conventional Branch prefixes above.
2. Use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) for
   commit messages and the PR title: `<type>[optional scope]: <description>`.
   Examples: `feat(discovery): add public channel search`,
   `fix: preserve retry delays`, and `docs: explain discovery permissions`.
3. Run the relevant checks and open a pull request targeting `main`. Describe the
   problem, resulting behavior, and validation. Link a related issue or roadmap item
   when applicable. Update documentation for public behavior or configuration changes.
4. Address review feedback and wait for the GitHub `checks` job to pass. Use squash
   merge after review; the final PR title becomes the squash commit message. Do not push feature work
   directly to `main`.

Keep changes focused. Propose a new direction with a feature request before expanding
an agreed milestone. Add or update roadmap completion records only when their acceptance
criteria are met; distinguish offline checks from any separately performed live validation.

## Dependencies and releases

Commit `uv.lock` alongside dependency changes. Review release notes and the lockfile
diff, then run the appropriate compatibility checks. Dependabot opens update PRs for
review; it does not merge them automatically. Updates to pinned GitHub Actions follow
the same review and CI process as Python dependencies.

Changes intended for users should have a concise entry in [CHANGELOG.md](CHANGELOG.md).
Release preparation belongs in a PR; a version tag is pushed only after that PR has
been squash-merged and checked. Follow [the release procedure](docs/releasing.md) for
PyPI Trusted Publisher setup, artifact checks, tagging, and publication verification.
Do not claim a PyPI release from a successful source build alone.

## Reporting issues

Use the bug report form for a minimal reproduction, version information, and expected
versus actual behavior. Use the feature request form to explain the use case, proposed
scope, and relationship to the roadmap.

Never include phone numbers, login codes, API keys or hashes, session files, HTTP tokens,
or private Telegram conversation dumps in issues, pull requests, logs, or fixtures.
Use synthetic examples and remove sensitive data from diagnostics before sharing them.
