# Releasing Telegram Context MCP

The distribution is `telegram-context-mcp`; its executable is `telegram-mcp`, with a
`telegram-context-mcp` alias starting with the Registry integration.
The commands below use **0.5.1 as an example**, not a published or scheduled release;
substitute the version being prepared. Published 0.4.0 predates Registry metadata.
Publishing requires selected-source validation, including any changed download,
monitoring and retrieval paths. The [0.5 validation record](validation-0.5.md) covers
the current release; the [0.4 migration guide](migration-0.4.md) applies to upgrades
from 0.3. Source changes, a release
tag, a GitHub Release, a published PyPI distribution, and an MCP Registry entry are
separate states. Verify each publication before announcing it.

## One-time PyPI setup

A maintainer with the relevant PyPI account must configure a Trusted Publisher.
Repository code and GitHub authentication cannot grant ownership of a PyPI name.
For a new project, [create a pending publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/);
for an existing project, use its publishing settings. Verify the name is available
or already owned by the maintainer. A pending publisher does not reserve the name.

| PyPI field | Required value |
| --- | --- |
| Project name | `telegram-context-mcp` |
| Owner | `egor-fedorov` |
| Repository | `telegram-mcp` |
| Workflow filename | `release.yml` |
| Environment name | `pypi` |

The owner and repository fields identify GitHub, not a PyPI username. Configure the
GitHub `pypi` environment consistently with that publisher. If environment protection
requires a reviewer, its normal approval applies to the publishing job.
Publishing uses GitHub OIDC with narrowly scoped `id-token: write`; no long-lived
PyPI token belongs in repository secrets or configuration.

See PyPI's [Trusted Publisher setup](https://docs.pypi.org/trusted-publishers/adding-a-publisher/)
and [publishing guide](https://docs.pypi.org/trusted-publishers/using-a-publisher/).

## Prepare the release PR

1. Start from current `main` on a Conventional Branch, such as `release/v0.5.1`.
2. Set `project.version` in `pyproject.toml` and refresh `uv.lock`. Run
   `uv run --no-project python scripts/check_registry.py --sync-version` to update
   both versions in `server.json` from the package metadata. Keep `--version`,
   installation examples, and client pins consistent with the release.
3. Add a dated section to [CHANGELOG.md](../CHANGELOG.md), for example
   `## [0.5.1] - YYYY-MM-DD`, with the actual release date and migration steps.
4. Run `make check`, build wheel and source distributions, validate metadata, and
   run `uv run --locked python scripts/smoke_distribution.py`. This installs both
   artifacts outside the checkout and checks the declared Registry executable and
   STDIO arguments, ownership marker, CLI and transports without Telegram credentials.
5. Record release-specific validation, following the previous
   [0.3 validation record](validation-0.3.md): response/schema size checks and a bounded
   [selected-source live probe](live-validation.md). Live validation requires explicit
   source authorization; an offline check does not authorize account-wide reads.
6. Review the artifacts and diff, open the release PR, and wait for the required
   GitHub `checks` job. Squash-merge into `main` when the checks pass.

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the regular check commands and branch rules.
Build outputs under `dist/` are generated artifacts; do not commit them.

## Tag and publish

After merging, update the local `main`, verify the exact commit, then create and push
an annotated version tag:

```sh
git switch main
git pull --ff-only origin main
git tag -a v0.5.1 -m "Release 0.5.1"
git push origin v0.5.1
```

The tag starts [release.yml](../.github/workflows/release.yml). The pipeline validates
the tag/version/changelog, builds the package, and publishes to PyPI through the
`pypi` Trusted Publisher. Only after successful package publication does it start
the GitHub Release and MCP Registry jobs. Those jobs are independent, so a Registry
outage does not prevent GitHub Release creation. Follow each job to completion;
a pushed tag alone does not confirm publication.

Verify the release's wheel and source distribution on PyPI and test a clean install:

```sh
uvx --python 3.12 --from telegram-context-mcp==0.5.1 telegram-mcp --version
```

Update any publication-pending documentation only after this check succeeds. No live
Telegram login or user account content is part of the release pipeline.

## MCP Registry publication

The Registry identity is `io.github.egor-fedorov/telegram-mcp`. The reviewed
[server.json](../server.json) describes a version-pinned PyPI installation, equivalent
to `uvx --python 3.12 telegram-context-mcp@0.5.1 serve` for the example release.
`telegram-context-mcp` and `telegram-mcp` invoke the same CLI. Clients still need uv
and explicit local `login` and `configure`; the manifest contains no credentials,
session paths or machine-specific HTTP URL. Shared-session clients should follow
the [client guide](clients.md) instead of starting another session-owning process.

The README's `mcp-name:` marker must reach the **published PyPI description** before
Registry publication. The already published 0.4.0 cannot be registered by this
workflow: its tag and package lack this integration. Publish a new package version
through the regular release process; do not move the old tag or re-upload its files.
The first Registry publication succeeded with 0.5.0; see its
[validation record](validation-0.5.md#publication-verification). Update publication
status in README and ROADMAP only after the corresponding remote checks succeed.

[registry.yml](../.github/workflows/registry.yml) reads metadata from the specified
release tag and verifies that it belongs to `origin/main`. It checks the exact PyPI
version, ownership marker, and non-yanked wheel and source distribution. The pinned,
checksum-verified `mcp-publisher` authenticates through GitHub OIDC with
`id-token: write`; no additional repository secret or interactive account setup is
required. See the official [GitHub Actions guide](https://modelcontextprotocol.io/registry/github-actions)
and [PyPI ownership requirements](https://modelcontextprotocol.io/registry/package-types).
Review publisher updates manually, changing its version and SHA-256 together.

Local metadata checks are offline by default:

```sh
uv run --no-project python scripts/check_registry.py
```

After publication, fetch tags and verify both remote records against the tagged
manifest (these checks use the network):

```sh
git fetch origin main --tags
uv run --no-project python scripts/check_registry.py --tag v0.5.1 --check-pypi --require-published
```

The version endpoint must contain the same server/package versions, PyPI identifier,
repository and STDIO launch metadata, with active status. Discovery clients can find
the entry by its Registry identity; clients without Registry discovery can use the
manual [installation and client instructions](../README.md#install-and-log-in).

If only Registry publication fails, dispatch its workflow from `main`:

```sh
gh workflow run registry.yml --ref main -f tag=v0.5.1
```

This uses the tag's manifest, even if `main` has since advanced. It does not build,
upload to PyPI or move the tag. An identical active Registry entry makes the retry a
successful no-op. Network and authorization failures are errors, not evidence that
the entry is missing. A conflicting entry fails because
[Registry versions are immutable](https://modelcontextprotocol.io/registry/versioning);
correct the metadata in a new release instead. After a failed verification, retry
this workflow to check whether the original publication completed.

## Failed or partial publication

If Trusted Publisher setup or project ownership is missing, finish that external setup
before retrying publication. Never paste a PyPI token into workflow code to work around
an OIDC failure. Keep the release documented as pending until it succeeds.

For a workflow failure, inspect which jobs and files completed before retrying. PyPI
versions cannot be overwritten. If files were already uploaded, verify their contents;
do not move the version tag or try to replace them with a different build. A correction
to an already published package needs a new version. If PyPI succeeded but GitHub
Release creation failed, repair only the remaining release-publication step.

Dependency-update PRs are reviewed and checked manually. They do not authorize a
release, bypass CI, or change publication credentials.
