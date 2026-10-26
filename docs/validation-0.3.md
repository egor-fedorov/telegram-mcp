# 0.3.0 validation record

This record describes checks for the 0.3.0 release candidate on 2026-10-22.
Implementation, local verification, and selected-source live validation are complete.
The implementation's full [GitHub CI run](https://github.com/egor-fedorov/telegram-mcp/actions/runs/39761842978)
passed, including the required `checks` job. This covers Linux on Python 3.12–3.15
and macOS/Windows on Python 3.12 and 3.15, with fresh distribution installations.
Merge status is recorded in [PR #13](https://github.com/egor-fedorov/telegram-mcp/pull/13).
Release availability is verified through [PyPI](https://pypi.org/project/telegram-context-mcp/)
and [GitHub Releases](https://github.com/egor-fedorov/telegram-mcp/releases), following
the [release procedure](releasing.md).

## Offline verification

Final `make check` passed formatting, lint, types, architecture boundaries, and
**891 tests, with one skipped**, without a Telegram account.

The final 0.3.0 wheel and source distribution built successfully. Both installed
in isolated, self-cleaning environments and passed metadata, CLI, STDIO, local HTTP,
and bridge smoke checks. The locked suite used MCP 2.2; fresh installation resolved
MCP 2.3 and passed the packaging smoke checks.

The first release workflow exposed nondeterministic exact-backoff assertions on
Windows/Python 3.12: floating-point subtraction could report 38 seconds for a
37-second wait when the system clock had not advanced. A single rerun reproduced
the same cause in another test. Both MCP tests now inject the existing fake clock,
retaining exact delay and no-premature-request assertions. Production retry logic
is unchanged. The failed attempts published neither PyPI files nor a GitHub Release;
the unpublished release tag is corrected only after the test fix passes CI.

The offline suite covers permission boundaries, compound search cursors, temporary
failures, authorization changes, catalog freshness and incremental loading, sparse
metadata, and all 14 typed MCP tool responses. The live harness has 20 offline tests
for selector bounds, selected-source isolation, redaction, failure handling, and
owned-client reconnect behavior.

## Response and schema sizes

The fixed regression fixture contains 30 ordinary posts of 342 characters each,
preserving all 10,260 text characters. Measurements use UTF-8 bytes of the complete
serialized MCP objects, including both text fallback and structured content.

| Serialized result | Frozen 0.2.0 baseline | 0.3.0 candidate, automatic MCP mode | Reduction |
| --- | ---: | ---: | ---: |
| `get_history` `CallToolResult` | 83,588 bytes | 32,075 bytes | 61.6% |
| `tools/list` | 110,590 bytes | 77,653 bytes | 29.8% |

These exceed the acceptance thresholds of 60% and 25%. The tests check both
automatic and legacy MCP negotiation modes; these modes share the new response
contract. They do not restore the 0.2 output shape. See
[test_wire.py](../tests/adapters/mcp/test_wire.py) for the fixture, frozen baselines,
serialization settings, and checks that every text character survives.

Reproduce the regression checks with:

```sh
uv run --locked pytest tests/adapters/mcp/test_wire.py
```

The percentages describe this fixture and serialized byte sizes, not a universal
compression ratio or an estimate of model tokens. See the
[migration guide](migration-0.3.md) for the output changes that produce the reduction.

## Selected-source live smoke check

The [bounded probe](live-validation.md) ran against two explicitly authorized
sources and one selected message. It exited with status `0`; all 24 emitted checks
had empty issue-code lists. No message content, source identifiers, titles, or
credentials are included in this record.

| Exercised path | Observed result |
| --- | --- |
| Source selection and initial status | Two selected sources; authorization confirmed |
| History continuation | Two pages per source; five messages on each page |
| Keyword search | Five messages per source |
| Link-only search | Empty initialization page followed by five results per source |
| Photo search | One result in one source and four in the other |
| Voice and audio search | Both filters completed with zero results in each source |
| Explicit message retrieval | One message |
| Context | Anchor plus four neighboring messages |
| Album | Two members found in the bounded neighborhood |
| Owned-client reconnect | Client disconnected while retaining its lease; subsequent status confirmed authorization and history returned five messages |

Zero-result media checks validate that the selected filter requests completed; they
do not verify normalization of a live voice or audio attachment. Album retrieval
does not establish album completeness. The reconnect test exercises recovery after
a controlled disconnect in the same process, not an external network outage or
Telegram server failure.

Global search was explicitly excluded. Dialog catalog scans, forum/topic reads,
authorization revocation, discovery, and unselected sources were not live-tested.
Their applicable behavior is covered by offline tests; no account-wide live coverage
is claimed. The probe did not send messages, join chats, mark messages read, or
download attachments.
