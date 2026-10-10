# Credence

Credence is a Kessai product: working capital for AI agents. An agent borrows from an XRPL lending vault, spends only on verified inference, and repays from what it earns. The normative spec is [`docs/spec/credence-build-plan.md`](docs/spec/credence-build-plan.md).

This is the Checkpoint 0 skeleton: structure, tooling and rules only. Every component is a stub that raises `NotImplementedError`.

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
- `web`: the Credence Markets frontend (Lovable), pinned and unchanged.
