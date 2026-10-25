# Claw Street — Powered by Credence

Claw Street is Kessai Labs' AI-agent procurement demo, powered by Credence. The local release runs Core policy evaluation, agent-signed purchases, receipt verification, response access and facility accounting with offline mock providers.

**Start with the [working demo and judge walkthrough](release/claw-street/README.md), [submission description](release/claw-street/SUBMISSION.md), and [validation results](release/claw-street/SUBMISSION-VALIDATION.md).** The [public website](https://creds-for-ai.lovable.app) is a separate browser simulation with seeded data.

Amounts are integer micro-tRLUSD; tRLUSD is a synthetic test unit. No provider receives a payment. XRPL marketplace settlement, QuickNode, t54/x402 payments and Ledger hardware signing are not connected. Core/X2-0R remains outside this release. The normative design spec is [`docs/spec/credence-build-plan.md`](docs/spec/credence-build-plan.md); its planned capabilities are not current product claims.

## Quick start

```bash
make setup && make test
```

Requires `uv` (Python 3.12 is pinned in `.python-version`), Docker for `make secrets` and `make ledger-up`, and `bun` for `make web`.

| Target | Does |
| --- | --- |
| `setup` | Init the pinned custody submodule, `uv sync --locked` |
| `test` | Unit + conformance tests |
| `test-unit`, `test-conformance`, `test-integration` | One test layer |
| `lint` | ruff lint and format check, large-file guard |
| `typecheck` | mypy (strict) on `credence/` |
| `secrets` | gitleaks scan of the working tree |
| `ledger-up`, `ledger-down` | Local standalone rippled (needs `RIPPLED_IMAGE`; tag TODO) |
| `web` | Run the frontend against `CREDENCE_API_BASE_URL` |

## Layout and ownership

See [`docs/OWNERSHIP.md`](docs/OWNERSHIP.md). Agents follow [`CLAUDE.md`](CLAUDE.md) / [`AGENTS.md`](AGENTS.md).

## Submodules

- `third_party/custody`: the Kessai serializer source, pinned to `5dc15ff`. Interim until the general `kessai-canon` package exists.
- `web`: the local Claw Street frontend, pinned to the submission UI on `release/claw-street-ui`. The hosted Lovable simulation has separate source on that repository's `main` branch.
