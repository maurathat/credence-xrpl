# Test summary for the release candidate

| Item | Value |
| --- | --- |
| Release branch | `maurathat/claw-street-release` |
| Tested code commit | `9844530f7832d3f920a2219f27f26cbdc2a9f0fc` (tree `bed8db4`); this documentation commit changes only `release/claw-street/` |
| Merge parents | `ab1bd50b2ac5e131d00c81bd2f3dd963fca49cf2` (UI baseline) and `4c57b912a484a1b2cc129ffa0d1cc9a301fb1fcf` (x402 adapter) |
| Web submodule | `4a9a812167f015f98cb4d0458fb3b93981b1d48d`, resolvable from GitHub `creds-for-ai` branch `release/claw-street-ui` |
| Custody submodule | `5dc15ff6fb097dc5f4a47e72969fae4a296620a5` (Kessai serializer, unchanged) |
| Run | 2026-10-25 UTC, macOS, Python 3.12, bun 1.4.2, in a fresh worktree whose submodules came from GitHub only |

## Python

| Command | Result |
| --- | --- |
| `make test-unit` | 1245 passed |
| `make test-conformance` | 148 passed |
| `make test-integration-offline` | 239 passed, 28 skipped. All skips are opt-in local-model tests (15 Llama, 13 Qwen; `CREDENCE_LOCAL_LLAMA=1` / `CREDENCE_LOCAL_QWEN=1`) |
| `uv run pytest tests/unit -k x402` plus `tests/conformance/test_x402_p0_fixtures.py` | 358 + 6 = 364 passed |
| x402 suite under a socket guard that refuses non-loopback connections | 364 passed; 0 non-loopback connection attempts; 7 HTTP requests, all loopback; no `/verify` or `/settle` request |
| `make typecheck` (strict mypy) | no issues in 64 source files |
| `make lint` | ruff clean; 205 files formatted; large-file and decision-number checks pass |
| Fixture regeneration (all 7 builders in `contracts/fixtures/` and `vectors/`) | byte-identical; no file changed |
| Contract and number references | every cited decision (0001–0009) and request (0001–0010) exists; every cited repo path exists |
| gitleaks v8.21.2 (repo config) | no leaks: committed tree (317 files) and history (31 non-merge commits) |

An export-based run reports 2 fewer passes and 2 more skips (1630 passed / 30 skipped). Two tests skip when
`third_party/custody` is not a submodule checkout, as in a `git archive` export: `tests/unit/test_core_kessai.py:35`
and `tests/conformance/test_kessai_serializer.py:42`.

## Web (`web/`, at `4a9a812`)

| Command | Result |
| --- | --- |
| `bun install --frozen-lockfile` | ok, no lockfile changes |
| `bun run typecheck` | ok |
| `bun run build` | ok |
| `bun test` | 55 passed, 0 failed |

## Browser (Playwright, Chromium, API and web on loopback)

Three runs on one fresh dataset. Before run 3 the API was stopped and restarted on the same database.

| Run | Scenario A (Nebius mock, Tier 1): drawn | Scenario B (local Qwen mock, Tier 2): drawn | Each |
| --- | --- | --- | --- |
| 1 (fresh) | 0 → 2 | 0 → 8 | accounting_settled → retrieved → acknowledged; 0 failed receipt checks; XRPL "not submitted" |
| 2 (same server) | 2 → 4 | 8 → 16 | same |
| 3 (after restart) | 4 → 6 | 16 → 24 | same |

On every run:
- A forged session token is refused.
- A buyer price cap disables Prepare (preview only).
- 0 console errors; the agent key and session token appear in no request.
- All six Claw Street screens pass: logo lockup, "Powered by Credence", XRPL not submitted, not-live panel, no bare
  RLUSD.
- All 21 routes: 0 bare "RLUSD", 0 "Forwards", 0 "written to XRPL".
- The API process opened no non-loopback socket during runs 1–2.
- Run 1 is identical to the earlier `ab1bd50` UI-only run once per-run IDs are set aside. Adding x402 changed nothing
  the browser does.

A CLI-only purchase (`agent.cli … purchase --preset scenario-b-controlled`) also reached `accounting_settled`.

## Screenshots

The six main views from run 1 are kept outside Git as evidence (this repo keeps evidence dumps out of version
control): `screen-overview.png`, `screen-marketplace.png`, `screen-agent-purchase.png`, `screen-credit-payments.png`,
`screen-evidence.png`, `screen-activity.png`. Per-step scenario captures (`scenario-a-1…5`, `scenario-b-1…5`) are
alongside them.

## Reproduce

```bash
make setup && make test && make typecheck && make lint
(cd web && bun install --frozen-lockfile && bun run typecheck && bun run build && bun test)
```
