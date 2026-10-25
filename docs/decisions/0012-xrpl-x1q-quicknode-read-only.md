# 0012. XRPL X1q: QuickNode read-only Testnet transport

- Status: Proposed. Awaiting independent X1q review.
- Date: 2026-10-24
- Scope: XRPL X1q, read-only, offline-tested. Base `99ca646` (X1c). Amends nothing in 0010 or 0011.
- Source: Maura's X1q brief (ownership delegated to Da Nang for this checkpoint); QuickNode's
  published XRPL `server_info` and `server_state` documentation. Contract:
  `contracts/QUICKNODE-ADAPTER-CONTRACT.md`. Report: `spike/xrpl/X1Q-QUICKNODE.md`. Request:
  `contracts/requests/0010-xrpl-public-network-reconciliation.md`.

## Decisions

1. **A separate hosted transport.**
   - `QuickNodeXRPLTransport` and its HTTPS client are new and isolated. They are not a
     configuration of `RippledTransport`, and the loopback client is not reused.
   - Nothing in the local transport, journal, verifier, multisignature, signer or settlement code
     changes.
2. **Explicit network, Testnet only.**
   - The network comes from explicit configuration. Mainnet and local network 21337 are refused
     when the configuration is loaded.
   - Every identity read requires the configured network ID, and a contradiction latches the
     transport closed.
   - There is no fallback between hosted and local sources.
3. **No node pin for a hosted source.**
   - QuickNode answers through Clio, and `server_state` is forwarded to varying rippled nodes.
   - `pubkey_node` is sampled and recorded, never pinned.
   - Trust rests instead on the endpoint, verified TLS, the expected network, validated-ledger
     hash consistency across reads, monotonic validated progress, bounded ledger age and
     complete-history coverage.
4. **A trusted source, not consensus.** One QuickNode endpoint is a trusted source. It is not
   independent consensus verification.
5. **Hosted evidence never finalizes the X0t journal.**
   - The only seam binding returns `UNAVAILABLE` with typed `QUICKNODE_*` details.
   - Entries stay `UNKNOWN`, with no outbox event and no Core effect.
   - Changing this is X0t/X1c owner territory (request 0010).
6. **Credential isolation.**
   - The endpoint URL is read from the environment and treated as a secret throughout.
   - Only a one-way fingerprint identifies it in evidence and reports.

## Consequences

- Real QuickNode verification is PENDING until an endpoint is configured and the operator runs
  `python -m credence.xrpl_adapter.quicknode_probe` with a public account and an existing public
  Payment hash.
- Reconciling Credence payments on a public network needs the owner's answer to request 0010.
