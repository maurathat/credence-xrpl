# Agent rules for Credence

Standing rules for every agent working in this repository (Claude Code, Codex, or any other).
`CLAUDE.md` and `AGENTS.md` have identical content; change both or neither.

The normative spec is [`docs/spec/credence-build-plan.md`](docs/spec/credence-build-plan.md).
Ownership of each area is in [`docs/OWNERSHIP.md`](docs/OWNERSHIP.md).

## Rules

1. **Own your worktree and your top-level area only.** Do not modify another workstream's code or contract.
2. **Need an interface change?** Write an INTERFACE CHANGE REQUEST in `contracts/requests/` and stop. Do not change it yourself.
3. **Do not resolve ambiguity in the spec by assumption.** Report the contradiction and stop.
4. **If a ledger or provider call returns an unexpected result, record it and stop that branch.** Do not switch builds or redesign to make it pass.
5. **Commit only after your checkpoint's tests pass.** Stop after the requested checkpoint and report.
6. **Claims discipline:** the Say / Don't say table below is copied verbatim from the build plan. Every sentence in code, docs, UI and commit messages must respect it.
7. **The merge authority is Maura.** No agent merges to main.

## Hard rules carried from Checkpoint 0

- All money is integer micro-units. The test token is called tRLUSD everywhere, never RLUSD.
- No private keys, secrets or key-shaped strings anywhere in the repo. CI fails the build on one.
- Do not reimplement the Kessai serializer. Credence depends on it; it is never copied or rewritten here.

## Claims discipline (verbatim from the build plan)

### Say / don't say

| Say | Don't say |
| --- | --- |
| Kessai serializer: JCS (RFC 8785) + NFC | UOR-ADDR, or "UOR-compatible" |
| Tier 2: the provider signed what it says it served | "Proves the exact weights ran" |
| Tier 1: Credence witnessed Nebius's response | Anything implying Nebius signed or attested it |
| Runs on a local XRPL ledger with the lending amendments enabled | "Live on mainnet" |
| tRLUSD, a test token | RLUSD |
| Uses Nebius Token Factory's public API; Credence pays Nebius through normal billing | Any partnership with Nebius, or that Nebius accepts XRPL payment |
| Providers are paid after verification; Credence is a trusted settlement party | "Trustless settlement" |
| One qualifying provider per class today; the mechanism handles many | A busy market |
| Inference reservations, physically delivered (preview) | Futures, derivatives, an exchange, "trading" |
| Price index from Credence's own purchases plus dated public prices (illustrative) | A market price or benchmark |
| Credence is a Kessai product; Maura also contributes to the UOR Foundation | Anything implying the Foundation endorses Credence |

