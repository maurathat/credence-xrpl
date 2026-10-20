# Ownership

One owner per top-level area. Agents modify only the areas their workstream owns (see `CLAUDE.md` / `AGENTS.md`).
Merge authority for every area is Maura; no agent merges to main.
Changes to Core or Inference canonical contracts made on the integration branch need explicit review by their owners (see `contracts/requests/`).

| Area | Workstream | Agent |
| --- | --- | --- |
| `credence/core/` (incl. `core/store/`) | Credence Core | Claude Code |
| `credence/signers/` | Credence Core (separate from the engine to isolate keys) | Claude Code |
| `credence/xrpl_adapter/` | XRPL Integration | Codex |
| `credence/inference/` | Inference | Claude Code |
| `credence/agent/` (ResearchBot) | Inference | Claude Code |
| `credence/api/` | API/UI | Claude Code |
| `credence/integration/` | Integration (San Antonio) | Claude Code |
| `web/` (submodule: Lovable frontend) | API/UI | Claude Code |
| `ledger-bridge/` | API/UI | Claude Code |
| `contracts/XRPL-ADAPTER-CONTRACT.md` | XRPL Integration | Codex |
| `contracts/CORE-OBJECTS.md` | Credence Core | Claude Code |
| `contracts/INFERENCE-GATEWAY-CONTRACT.md` | Inference | Claude Code |
| `contracts/API-CONTRACT.md` | API/UI | Claude Code |
| `contracts/INTEGRATION-FLOW.md` | Integration (San Antonio) | Claude Code |
| `contracts/fixtures/` | Owner of the contract each fixture belongs to | — |
| `contracts/requests/` | Any workstream may add a request; the contract's owner answers | — |
| `spike/xrpl/` | XRPL spike | Codex |
| `review/xrpl/` | XRPL review (independent) | Claude Code |
| `vectors/` | Credence Core | Claude Code |
| `tests/unit/`, `tests/conformance/`, `tests/integration/`, `tests/e2e/` | Each workstream for tests of its own area | — |
| `tests/integration/test_i1_*`, `tests/integration/i1_world.py` | Integration (San Antonio) | Claude Code |
| `tests/attack/` | Red Team (read-only on all other code) | Codex |
| `infra/rippled/` | XRPL Integration | Codex |
| `infra/speculos/` | Ledger Integration | **TBD** |
| `third_party/custody/` (submodule, pinned) | Kessai (external; not edited here) | — |
| `docs/`, root tooling (`Makefile`, `pyproject.toml`, CI, agent rules) | Maura | — |

## Workstreams

| Workstream | Agent |
| --- | --- |
| Credence Core | Claude Code |
| XRPL Integration | Codex |
| Inference (incl. ResearchBot) | Claude Code |
| Ledger Integration | **TBD** |
| API/UI | Claude Code |
| Integration (San Antonio): orchestration, serialization, journal, response recovery, API boundary between Inference and Core | Claude Code |
| Red Team | Codex, read-only |
| XRPL spike | Codex |
| XRPL review | Claude Code |
