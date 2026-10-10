# 0001. Foundational architecture decisions

- Status: Accepted
- Date: 2026-10-10
- Source: `docs/spec/credence-build-plan.md` (build plan of Oct 9, 2026)

## Decisions

1. **Borrower account is 2-of-2, master key on the owner's Ledger.** The ResearchBot credit account has a signer list of the ResearchBot key and the Credence policy key, quorum 2, with the regular key disabled. Its master key is the owner's Ledger device, which can act alone, change the signer list, or remove the agent. (Plan: XRPL integration > Accounts and keys.)
2. **Settlement is pay-after-verification; escrow is on the roadmap.** Providers are paid by a co-signed direct payment after the verifier passes. Credence is a trusted settlement party; escrow-backed settlement (ideally Smart Escrows, XLS-100) is roadmap only. (Plan: Settlement and dispute policy; Roadmap.)
3. **Closed-ended vaults per LendingProtocolV1_1.** All loans come from closed-ended Single Asset Vaults whose subscription, investment and redemption phase dates are fixed at creation. (Plan: Vault lifecycle.)
4. **Kessai serializer: JCS (RFC 8785) + NFC.** It is the only serializer in Credence; it is a dependency, never reimplemented here. Full name, once per document: "Kessai serializer: JCS (RFC 8785) with NFC normalization, bound using the SCITT Canonical Payload Binding `jcs` construction." NFC is not part of RFC 8785: it is applied to every string value and key before JCS serialization. (Plan: Serialization and receipts.)
5. **Integer micro-units.** Every amount is an integer in the smallest unit (1,000,000 micro-units per token). The local-ledger currency is the test token tRLUSD. (Plan: Domain model > Money rule.)

## Consequences

- If Credence is offline the agent cannot spend; the owner's master key is the escape hatch.
- Until the general serializer package exists, Credence pins the custody repository as a submodule and only `dataset.v1` digests are testable (see Open blocker below).

## Open blocker

The serializer exists today only as the dataset-specific `canon.py` inside custody. Before Credence Core starts, Kessai extracts a general package (working name `kessai-canon`) exposing canonical bytes and a digest for any object, with `dataset_id` built on top and all existing vectors passing. Credence then depends on that package instead of the custody submodule.
