# infra/rippled

Owner: XRPL Integration (Codex).

Local standalone `rippled` in Docker: a pinned release build with the vault and lending amendments (including LendingProtocolV1_1) enabled in a new genesis ledger.

Checkpoint 0 placeholder. The image tag is a TODO: `make ledger-up` refuses to start until `RIPPLED_IMAGE` is set to a pinned release image. `rippled.cfg` is a placeholder for the XRPL spike to fill in. Local ledger data goes in `infra/rippled/data/`, which git ignores.
