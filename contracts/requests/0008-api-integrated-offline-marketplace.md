# INTERFACE CHANGE REQUEST 0008: integrated offline product API (CR-9)

- **Contract:** `contracts/API-CONTRACT.md` (owner: API/UI); the agent client in `credence/agent/` (owner: Inference).
- **Requested by:** I2a offline integration (`maurathat/credence-i2a-offline`).
- **Status:** implemented provisionally on the I2a branch as authorized by Maura's I2a brief (2026-10-20). Security review required; Inference review of `credence/agent/client.py` and `cli.py`.

## Change

`/v0/live/*` routes served by an `IntegrationService` embedded in the API process (`credence.api.offline_demo serve`), with offline mock providers, bearer agent sessions (token digests only on the server), agent-scoped authorization, typed error mapping and an XRPL payment-status boundary that answers `not_submitted`. The P0 routes are unchanged. Details are in `contracts/API-CONTRACT.md`, section "I2a".

Not enabled: live provider execution, the buyer price cap (CR-2), XRPL preparation, signing or submission (X0 is not merged), lending.
