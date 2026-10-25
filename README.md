# Claw Street — Powered by Credence

Claw Street is Kessai Labs' AI-agent procurement demo, powered by Credence. The local release runs Core policy evaluation, agent-signed purchases, receipt verification, response access and facility accounting with offline mock providers.

**Start with the [working demo and judge walkthrough](release/claw-street/README.md), [submission description](release/claw-street/SUBMISSION.md), and [validation results](release/claw-street/SUBMISSION-VALIDATION.md).** The [public website](https://creds-for-ai.lovable.app) is a separate browser simulation with seeded data.

Amounts are integer micro-tRLUSD; tRLUSD is a synthetic test unit. No provider receives a payment. XRPL marketplace settlement, QuickNode, t54/x402 payments and Ledger hardware signing are not connected. Core/X2-0R remains outside this release. The normative design spec is [`docs/spec/credence-build-plan.md`](docs/spec/credence-build-plan.md); its planned capabilities are not current product claims.

## What we built

- A local, end-to-end procurement demo: Core policy evaluation, agent-signed purchase requests, reservations, delivery verification, response retrieval and acknowledgement, and facility accounting.
- Two offline demonstration scenarios, backed by clearly labelled recorded evidence from real Nebius inference and a real local Qwen run. The demo does not make those live calls again.
- An x402 v2 discovery, compatibility and offline payment-proposal adapter using recorded real merchant challenges and t54 facilitator capability responses.
- Durable purchase recovery, agent-scoped access and revocation protections, and independent review checkpoints. Accounting settlement is not an on-chain provider payout.
- A local release UI, a judge walkthrough, a guided/recorded demo, a six-slide pitch, presenter notes and submission assets. See the [release guide](release/claw-street/README.md) and [submission assets](release/claw-street/SUBMISSION-ASSETS.md).

## Sponsor and ecosystem integrations

This table distinguishes **what the release demonstrates** from **engineering completed on separate branches**. Publishing a development branch does not merge it into this release, accept its open review gates, or connect it to the marketplace.

| Technology | What we accomplished | Current boundary and evidence |
| --- | --- | --- |
| **XRP Ledger / Ripple** | Separate workstreams implement standalone-ledger lending/authorization experiments, software 2-of-2 payment verification, a durable payment journal, restart reconciliation and adversarial late-completion protections. Public-Testnet tooling includes bounded-history checks and fail-closed unknown outcomes. | These are separate from the marketplace release. No purchase-linked XRPL payout is connected here; native-XRP X2 authority remains gated. See the [XRPL adapter branch](https://github.com/maurathat/credence-xrpl/tree/maurathat/credence-multi-sig-ledger) and [standalone/Testnet research branch](https://github.com/maurathat/credence-xrpl/tree/maurathat/xrpl-credence-codex). Local lending evidence is not a production lending or mainnet claim. |
| **Nebius Token Factory** | Built the inference adapter and retained recorded evidence from a real public-API inference run, including the witnessed-response evidence used in the demo. | Scenario A runs an offline mock. Tier 1 means Credence witnessed the response; Nebius did not sign or attest it. Normal Nebius billing is separate from XRPL. No partnership is implied. See [inference evidence](contracts/fixtures/inference_c3/) and the [inference branch](https://github.com/maurathat/credence-xrpl/tree/maurathat/credence-inference-claude). |
| **x402 / t54** | Recorded real unpaid x402 v2 challenges from Heurist and Monid, plus t54 documentation and `/supported` responses; implemented parsing, compatibility assessment and immutable offline payment-proposal preparation. | Included as read-only/offline functionality, not paid inference. No facilitator `/verify` or `/settle` call, payment signing or submission. Observed merchant RLUSD/Mainnet terms are external challenge data, not our micro-tRLUSD accounting or an executed payment. See [disclosures](release/claw-street/DISCLOSURES.md) and the [x402 contract](contracts/X402-ADAPTER-CONTRACT.md). |
| **QuickNode** | Built a separate read-only XRPL adapter and hardened endpoint handling, response/deadline bounds, credential isolation and reconciliation failure behavior with offline tests. | A real QuickNode Testnet endpoint has not been validated in the recorded branch evidence. Not connected to the release and not authoritative payment finality. See the [QuickNode branch](https://github.com/maurathat/credence-xrpl/tree/maurathat/quicknode-xrpl-integration). |
| **Ledger** | Defined the intended owner/agent/policy authority split; separate XRPL work implements a software two-signer baseline. | Ledger hardware, WebHID and Speculos are not connected. Software multisigning is not hardware-signing evidence. See [disclosures](release/claw-street/DISCLOSURES.md). |
| **Lovable** | Built the Claw Street frontend and a separate local-release UI integrated with the demo backend. | [`creds-for-ai`](https://github.com/maurathat/creds-for-ai) is the frontend repository. Its `release/claw-street-ui` branch is the parent repository's pinned release UI; its `main` serves the separate seeded browser simulation. The hosted site does not execute this backend's signature verification. |
| **Kessai** | Uses the pinned Kessai serializer for canonical bytes and digests: JCS (RFC 8785) + NFC. | Consumed as the `third_party/custody` submodule, not reimplemented here. |
| **Local Qwen provider** | Recorded a real local inference run and provider-signed evidence; Scenario B demonstrates the corresponding verification/accounting flow offline. | Tier 2 means the provider signed what it says it served, not proof that exact model weights ran. No model runs during the default demo. |
| **Verifiable Intent** | Identified in the integration roadmap. | Not connected: no credential issuance or verification is claimed. |

The [full integration disclosures](release/claw-street/DISCLOSURES.md), [validation record](release/claw-street/SUBMISSION-VALIDATION.md) and branch-specific reports are the detailed evidence. No sponsor endorsement, trustless settlement, live paid t54 inference, mainnet/RLUSD settlement or production-ready lending is claimed. Credence is a Kessai product; Maura's UOR Foundation contributions do not imply Foundation endorsement.

## Which repository and branch?

- **This repository, `maurathat/credence-xrpl`:** Credence backend, contracts, tests, integration work and the Claw Street release/submission package. `main` and `maurathat/claw-street-release` identify the promoted release; other workstreams remain separate branches.
- **[`maurathat/creds-for-ai`](https://github.com/maurathat/creds-for-ai):** the separate frontend/Lovable repository referenced by the `web` submodule. Updating this backend does not automatically change the hosted frontend's `main`.
- **X2-0R:** a review-only native-XRP purchase-authority design, not an implemented or connected feature. Micro-tRLUSD is never reinterpreted as XRP drops. A future one-drop Testnet demonstration payout would require independent authority and a separate execution grant, and would not economically discharge the inference charge.

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
