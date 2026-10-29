# Contributing

Use English for repository documentation, code comments, CLI text, commit messages,
and pull requests. Keep multilingual fixtures when their language matters to a test.
See [ROADMAP.md](ROADMAP.md) for current priorities, [Architecture](docs/architecture.md)
for dependency boundaries, and [milestone specifications](docs/specs/README.md) for
proposed and completed contracts, clearly marked by status.

## Local setup and checks

Use Python 3.12 and [uv](https://docs.astral.sh/uv/) for the standard development
environment. The supported runtime range is Python 3.12–3.15 on Windows, macOS, and
Linux. From the project directory:

```sh
uv sync --locked --python 3.12 --extra extraction
make check
```

`make check` runs Ruff formatting checks, Ruff lint, ty, and pytest in parallel through
pytest-xdist. Tests are offline: they do not require a Telegram account, login, or
network requests to Telegram.
Dependency installation may require network access. Add tests for changed behavior
and keep existing MCP contracts compatible, or explain any compatibility changes.
For documentation-only changes, check links, examples, and formatting.

On Windows without Make, run the same checks directly:

```sh
uv run --locked --extra extraction ruff format --check .
uv run --locked --extra extraction ruff check .
uv run --locked --extra extraction ty check
uv run --locked --extra extraction pytest -n auto
```

Ruff enforces a maximum McCabe complexity of 15 with no exceptions. Split a function
into named steps instead of raising the limit or adding `# noqa: C901`.

CI checks formatting/types, builds wheel and source distributions, and runs tests
with the extraction extra. Clean base-package probes also verify operation without
the optional document parsers. CI includes clean-install smoke checks.
The matrix covers Python 3.12–3.15 on Linux and
3.12/3.15 on macOS and Windows. The required `checks` job succeeds only when its
dependencies succeed. No Telegram credentials or publishing secrets are needed.

When changing transport behavior, cover standalone STDIO, authenticated local HTTP,
and the STDIO proxy without a real Telegram account. Shared-client tests should
exercise lifecycle and cursor behavior, including a client disconnecting while the
session-owning server remains available. Preserve the recorded schemas of all existing tools
within a contract version. Intentional versioned changes require updated fixtures,
migration guidance, and transport parity tests. See the [0.4 migration](docs/migration-0.4.md)
and [bounded live validation](docs/live-validation.md).

## Test layout

Place tests beside others covering the same layer; reserve integration suites for
behavior that crosses adapter boundaries or starts real transports and processes.

| Directory | Coverage |
| --- | --- |
| `tests/domain/` | Pure domain rules |
| `tests/application/` | Use cases, access policy, pagination and recovery with fake ports |
| `tests/adapters/` | Configuration, CLI, storage and other local adapters |
| `tests/adapters/telegram/` | Telegram requests, raw payload mapping and runtime lifecycle |
| `tests/adapters/mcp/` | MCP tool behavior, compact output and schema-size checks |
| `tests/integration/` | Shared transports, resource delivery, subprocesses and packaging |
| `tests/contracts/` | Versioned MCP contract and architectural boundaries |
| `tests/support/` | Explicitly imported fakes, builders and reusable test contexts |

Test modules must not import other test modules. Put genuinely shared helpers in
`tests/support/`; keep local helpers in their own suite. Subprocesses import support
modules directly, without collecting a test module. Avoid an implicit fixture registry
in `conftest.py`. The shared `paths` module locates the project and fixtures independently
of test depth; keep the versioned schema fixture at `tests/fixtures/mcp_contract.json`.
`tests/fixtures/mcp_listing.json` pins tool order, descriptions, server instructions,
prompts and resource templates. `tests/fixtures/state-0.5/` holds state written by
0.5.0; it must keep loading and serializing byte for byte.

For quick iteration, `make test-fast` skips tests marked `slow` (real transports,
subprocesses and very large fixtures). For a focused run, use the appropriate
directory or file:

```sh
make test-fast
uv run --locked pytest tests/adapters/telegram tests/application
uv run --locked pytest tests/integration/test_attachment_resources.py
```

Run the complete `make check` before opening or merging a PR.

## Adding an MCP tool

Tools are organized by feature: watches, changes, attachments, status, discovery,
catalog and messages. A new tool in an existing feature touches only that feature:

1. Implement the use case in `application/`, adding a port method, its Telegram gateway
   and the matching `tests/support` fake only when it needs new Telegram data.
2. Add one `Service` method that delegates to the use case.
3. Add response DTOs to `adapters/wire/<feature>.py` and export them from
   `adapters/wire/__init__.py`; the wire parity test lists any intended projection.
4. Register the tool in `adapters/mcp/tools/<feature>.py`. Only a new feature module
   needs one registration line in `adapters/mcp/server.py`, which fixes the tool order.
5. Update `tests/fixtures/mcp_contract.json` and `tests/fixtures/mcp_listing.json` for
   the intended contract change, and document the tool in the usage guide.

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
review; it does not merge them automatically. Weekly version updates separate the
MCP packages, development tools (`pytest`, `pytest-asyncio`, `pytest-xdist`, Ruff and
ty), and other dependencies. Telethon updates receive their own PRs. Development-tool
and other dependency groups combine only minor/patch updates; major updates stay
individual.
GitHub Actions have their own weekly minor/patch group and follow the same review
and CI process. These groups keep a type-checker or linter change from blocking a
library update.

Keep Dependabot alerts and security updates enabled in the repository settings.
Security fixes open separate PRs as needed, independently of the weekly version-update
groups, and require manual review and passing CI too. When an update fails, fix the
compatibility issue or narrow that update; do not disable checks or enable auto-merge.

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
