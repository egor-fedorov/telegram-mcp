# Releasing Telegram Context MCP

The distribution is `telegram-context-mcp`; its executable is `telegram-mcp`.
The commands below use **0.4.0**; substitute the version being prepared for later releases.
Publishing requires selected-source validation, including any changed download,
monitoring and retrieval paths. The [0.4 validation record](validation-0.4.md) and
[migration guide](migration-0.4.md) document this release's coverage. Source changes, a release
tag, a GitHub Release, and a published PyPI distribution are separate states. Do not
announce a PyPI release until the publishing job has succeeded and its files exist.

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

1. Start from current `main` on a Conventional Branch, such as `release/v0.4.0`.
2. Set the package version and refresh `uv.lock`. Keep `--version`, installation
   examples, and client pins consistent with the release.
3. Add a dated section to [CHANGELOG.md](../CHANGELOG.md), for example
   `## [0.4.0] - YYYY-MM-DD`, with the actual release date and migration steps.
4. Run `make check`, build wheel and source distributions, validate metadata, and
   smoke-test an installed wheel. Package checks must not need Telegram credentials.
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
git tag -a v0.4.0 -m "Release 0.4.0"
git push origin v0.4.0
```

The tag starts [release.yml](../.github/workflows/release.yml). The pipeline validates
the tag/version/changelog, builds the package, and publishes to PyPI through the
`pypi` Trusted Publisher. Only after successful package publication does it create
the GitHub Release. Follow the workflow run to completion; a pushed tag alone is
not confirmation that either publication succeeded.

Verify the release's wheel and source distribution on PyPI and test a clean install:

```sh
uvx --python 3.12 --from telegram-context-mcp==0.4.0 telegram-mcp --version
```

Update any publication-pending documentation only after this check succeeds. No live
Telegram login or user account content is part of the release pipeline.

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
