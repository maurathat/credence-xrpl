# Checkpoint X1c — XRPL payment assurance remediation

## Outcome

X1c closes Salvador's five blocking X1b findings (B1–B5) and the three conditional defects (C1–C3) against X1a `607cc2a`.

It is offline:

- no XRPL ledger writes;
- no live provider calls;
- no Core accounting changes outside test fixtures.

The following are unchanged:

- frozen Core canonical objects and C3A definitions;
- the multisign digest and transaction identity;
- the authorization statement;
- the expiry proof;
- the outbox.

## Before and after

`.context/x1a/exploits.py` (local, not committed) runs the same exploit code against an archive of `607cc2a` and against the X1c tree. It asserts which tree it loaded. The committed regressions are in `tests/unit/test_xrpl_x1c_remediation.py`.

| Finding | Exploit | `607cc2a` | X1c |
| --- | --- | --- | --- |
| B1 | Binary `DeliveredAmount` 0.1, JSON `delivered_amount` 2.5 | `VALIDATED_SUCCESS`, `XRPL_SETTLED`, delivered 2,500,000, 1 outbox event | `UNKNOWN` (`EVIDENCE_INCONSISTENT`), no settlement, 0 events |
| B2 | Node reports network 1 at lookup | `VALIDATED_SUCCESS`, 1 event | `UNKNOWN` (`NETWORK_MISMATCH`), 0 events |
| B2 | Node history shrinks to `1-50` before lookup | `VALIDATED_SUCCESS`, 1 event | `UNKNOWN` (`LEDGER_OUTSIDE_COMPLETE_HISTORY`), 0 events |
| B3 | Caller passes a fabricated `FFFF…` ledger context | Signed; 1 signing-log row | Refused `LEDGER_CONTEXT_MISMATCH`; 0 rows, 0 signatures |
| B4 | RegularKey on the funded policy-signer account | Accepted | Refused `SIGNER_REGULAR_KEY_PRESENT` |
| B5 | `claim_xrpl_settlement` after the outbox event exists | Returned `True` (a second effect authority) | Refused `LEGACY_SETTLEMENT_CLAIM_RETIRED` |
| C2 | High-S secp256k1 signature (different transaction ID) | Accepted | Refused `SIGNATURE_NOT_CANONICAL` |
| C3 | 302 from the pinned endpoint to `http://0.0.0.0:<port>/` | Followed, and the target was hit (6 of 6 runs) | Refused (`RpcUnavailable`), target never hit (3 of 3) |

## Fixes

- **B1 — `rippled.py`**
  - The delivered amount comes from the binary metadata `DeliveredAmount`. When that field is absent, XRPL's rule for a successful Payment without `tfPartialPayment` applies: the delivered amount is the signed `Amount`. The X0 inspector refuses any Payment flags, so a partial payment never reaches this point.
  - The JSON `delivered_amount` must equal that amount in currency, issuer and exact decimal value. Otherwise the lookup is refused with `EVIDENCE_INCONSISTENT`.
  - An unsuccessful result must report no delivery at all.
  - Both observations are kept in the evidence envelope (`delivered_amount_observations`).
  - The intended amount is still checked by the X0t verifier, against the journal amount that is bound to the authorization.
- **B2 — `RippledTransport.lookup`**
  - Every lookup re-reads `server_state` and `ledger` and re-verifies:
    - the network ID;
    - the build version, which is now a required pin;
    - the node public key, when one is pinned.
  - The transaction's ledger must be:
    - validated;
    - at or below the validated ledger;
    - inside `complete_ledgers`;
    - hash-consistent with the fetched ledger, and with the identity's hash when it is the tip.
  - The identity read's raw responses are kept in the evidence.
- **B3 — `policy_signer.py`**
  - `PolicySigner` takes a `TrustedLedgerSource`, which `RippledTransport.ledger_context` implements, and reads ledger state itself on every signature.
  - A caller-supplied context is only compared with that read (`LEDGER_CONTEXT_MISMATCH`) and is never used as evidence.
  - A failed read is `LEDGER_UNAVAILABLE`.
  - Nothing is recorded or signed on any refusal.
  - A test enforces that no API, agent, inference, integration or Core module imports `credence.signers`.
- **B4 — `multisign.py`, `trlusd_setup.py`**
  - A funded signer account holding a RegularKey is refused with `SIGNER_REGULAR_KEY_PRESENT`, and the same condition is a setup-readiness finding.
  - The module documents the XRPL distinction. The agent account's SignerList names the signer accounts. Each signer account's own authority is its master key or its RegularKey, and rippled accepts either.
  - Nothing claims that a master key cannot act outside the routine two-signature flow.
- **B5 — `journal.py`, `service.py`**
  - `claim_settlement_effect` and `claim_xrpl_settlement` raise `LegacySettlementClaimRetired` whenever the operation has an outbox event. That is every verified success.
  - Stored claim history is never rewritten.
  - The outbox event, applied by the idempotent consumer keyed by `event_id`, is the one authoritative payment-event path.
- **C1 — option A, approved by Maura**
  - New terminal journal state `VALIDATED_UNEXPECTED_DELIVERY`, used for a validated `tesSUCCESS` with the wrong delivered asset or amount. It:
    - keeps the raw evidence and the actual delivered amount;
    - sets no `XRPL_SETTLED` and creates no outbox event;
    - is never retried or replaced automatically;
    - is reported by `requires_operator_reconciliation` and `operator_reconciliation_queue`.
  - Journal schema version 3. New journals start at version 3, and the classifier also requires the state constraint to admit every state.
  - Complete version-2 journals are refused unchanged with `JOURNAL_VERSION_2_UNSUPPORTED`. A v2 file relabelled as version 3 is `UNSUPPORTED_JOURNAL_SCHEMA`.
  - No automatic migration.
- **C2 — `multisign.py`** applies rippled's fully-canonical rule before cryptographic verification:
  - secp256k1: strict minimal DER, `0 < r, s < n`, and `s <= n/2`;
  - Ed25519: 64 bytes and `S < L`.

  xrpl-py's secp256k1 verifier accepts high-S signatures, and its signer emits low-S ones.
- **C3 — `rippled.py`.** The loopback client installs a redirect handler that refuses every redirect.

## Changed files

**Code**

- `credence/xrpl_adapter/rippled.py` (B1, B2, C3)
- `credence/xrpl_adapter/multisign.py` (B4, C2)
- `credence/xrpl_adapter/trlusd_setup.py` (B4)
- `credence/signers/policy_signer.py` (B3)
- `credence/xrpl_adapter/journal.py` (B5, C1)
- `credence/xrpl_adapter/service.py` (B5, C1)
- `credence/xrpl_adapter/types.py` (B5, C1)
- `credence/xrpl_adapter/settlement.py` (C1, B5 docs)
- `credence/xrpl_adapter/__init__.py` (export)

**Tests**

- `tests/unit/test_xrpl_x1c_remediation.py` (new, 40 tests)
- `tests/unit/test_xrpl_x1a_policy_signer.py` (+6)
- `tests/unit/test_xrpl_x1a_setup.py` (+2)
- `tests/unit/test_xrpl_x1a_transport.py`
- `tests/unit/test_xrpl_x1a_flow.py`
- `tests/unit/test_xrpl_payment_readiness.py` (authorized B5/C1 expectation changes)
- `tests/unit/x1a_world.py`
- `tests/unit/x1a_rippled.py`

**Fixtures**

- `tests/fixtures/xrpl/generate_x1b_v2_journals.py`
- `tests/fixtures/xrpl/x1b_v2_journal.fixture`
- `tests/fixtures/xrpl/x1b_v2_empty_journal.fixture`
- `tests/fixtures/xrpl/README.md`

The v2 fixtures were generated by the unmodified `607cc2a` code. Their SHA-256 values are pinned, and a test now checks every fixture against its recorded hash.

**Documents**

- `contracts/XRPL-ADAPTER-CONTRACT.md`
- `spike/xrpl/X1A-PREFLIGHT.md` (supersession notes)
- this report
- `docs/decisions/0011-xrpl-x1c-payment-assurance.md`

## Accepted X0t behaviour changed only as authorized

These X0t tests changed, each as the X1c brief or Maura's C1 approval directs:

- Three claim tests now assert one outbox event and a refused legacy claim (B5).
- The delivered-amount case expects `VALIDATED_UNEXPECTED_DELIVERY` (C1).
- The fresh-journal tests expect version 3 (C1).

Every other X0t test passes unchanged, including:

- the X0 legacy-journal refusals, with byte-for-byte preservation (X0 fixture hashes unchanged);
- expiry proof;
- exact-byte resubmission;
- competing reconciliation;
- outbox crash and replay windows.

## Newly discovered limitations

1. **Unconfirmed API v2 shapes.** The response shapes (including `pubkey_node` and `meta_blob`) are still exercised only against fakes. Read-only verification against the pinned rippled remains a prerequisite.
2. **Delivered-amount fallback.** The fallback to the signed `Amount` is correct only because Payment flags are refused. Allowing `tfPartialPayment` later would require reading `DeliveredAmount` alone.
3. **No fixture regeneration test.** The v2 fixture regeneration is equivalent but not byte-identical, because rows carry timestamps. The committed bytes are pinned by hash, as for X0. Re-running the generator is not part of the test suite.
4. **No live caller can construct the policy signer yet.** The signer is not yet wired to a real `RippledTransport` with custody-held keys. It now needs a trusted ledger source.

## Remaining X1 prerequisites (deferred, unchanged scope)

- **C4.** XRP funding operations and full trust-line and freeze readiness.
- **C5.** Re-issuing an identical transaction after statement expiry, or replacing it after proven non-inclusion. The one-payment-per-receipt rule is unchanged.
- Pinned rippled configuration and read-only shape verification.
- Core request 0009, the accounting-settlement read.
- Key custody, and ResearchBot signing integration.
- An enforced live submission authority.
- The Core-side settlement effect.
- Live submission itself.
- Optional Ledger hardware signing, and QuickNode or x402 adapters.
- Native LoanSet, LoanPay and vault integration.

Passing local tests does not authorize ledger submission.
