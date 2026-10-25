# INTERFACE CHANGE REQUEST 0010: reconciling payments on a public XRPL network

- **Contract:** `contracts/XRPL-ADAPTER-CONTRACT.md` (owner: XRPL Integration), covering the X0t
  journal, inspector and verifier and the X1c rules.
- **Requested by:** XRPL X1q (QuickNode read-only integration), Da Nang, authorized by Maura on
  2026-10-24.
- **Status:** open. No X0t or X1c code was changed. X1q returns a typed unsupported result instead
  (`QuickNodeReconciliationView`, see `contracts/QUICKNODE-ADAPTER-CONTRACT.md`).
- **Numbering:** 0005 to 0008 are used on `maurathat/credence-i2a-offline` and 0009 on this base,
  so this request is 0010.
- **Blocked:** reconciling any Credence payment on XRPL Testnet, or any other public network,
  through the X0t journal. Nothing in X1q is blocked.

## Why

X1q can observe a Testnet Payment by its original hash through QuickNode. The observation is
consistent and network-bound, with binary and JSON evidence in agreement. Three existing rules
prevent such a payment from ever reaching the X0t journal, and each is a deliberate X0t or X1c
decision this workstream does not own:

1. `XrplSignedPaymentInspector` requires a `NetworkID` field in the signed bytes. XRPL forbids that
   field on networks with ID 1024 or below, so a valid Testnet Payment cannot be persisted or
   verified.
2. The X1c B2 rule re-verifies one pinned node on every lookup: network, a required build pin and
   an optional node-key pin. A load-balanced hosted endpoint has no stable build or node identity
   to pin (QuickNode forwards `server_state` to varying rippled nodes behind Clio).
3. Expiry (`EXPIRED_NOT_INCLUDED`) needs a pre-broadcast anchor and complete history from the same
   `source_id`. Today the only accepted source is the pinned local node.

## Change requested (for the owner to decide)

- Allow the inspector and journal to bind a payment to its network explicitly, so that a network
  without `NetworkID` (for example Testnet, ID 1) is represented without that field. Payments on
  network 21337 must stay unchanged.
- Decide whether a hosted-source evidence class is ever acceptable for journal finality. If it is,
  define it explicitly, for example `credence.xrpl.hosted-payment-observation.v1` checked under
  the hosted trust policy and kept distinct from pinned-node evidence. If it is not, decide what
  replaces it on a public network (for example, an operator-run pinned node).
- Decide whether a hosted source may anchor submissions and prove non-inclusion. X1q assumes it
  may not.

Until the owner answers, X1q keeps every hosted lookup `UNAVAILABLE` with a typed `QUICKNODE_*`
detail. A journal entry stays `UNKNOWN` and is never finalized from hosted evidence.
