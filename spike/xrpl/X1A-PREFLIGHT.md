# Checkpoint X1a — XRPL multisignature and live-ledger preflight

## Outcome

X1a prepares one independently authorized tRLUSD provider Payment linked to a verified Credence inference purchase. tRLUSD is a test token on a local standalone XRPL ledger.

It is an offline implementation and preflight checkpoint:

- no live XRPL calls;
- no ledger writes;
- no real token transfer;
- no provider execution.

Base: `ada4d6d6eaf2282829fc6eeae290c7ff43572eaf` (X0t). The X0t journal, service, preparation, types and mock are unchanged. No Core code, canonical object or C3A definition changed.

## 1. Multisignature (`credence/xrpl_adapter/multisign.py`)

Two signers authorize the agent payment account's routine spends:

- ResearchBot, weight 1;
- the Credence policy signer, weight 1;
- quorum 2.

Building blocks:

- `build_unsigned_payment` builds the multisign-ready Payment: empty `SigningPubKey`, no signatures, `Flags` 0, `NetworkID`, `LastLedgerSequence`, and either Sequence or TicketSequence with Sequence 0.
- `unsigned_transaction_digest` (scheme `xrpl-stx-sha512half-v1`) is the SHA-512Half of the XRPL single-signing serialization (`STX\0` prefix) of that unsigned form. It is the transaction identity that Core authorizes and both signers sign.
- `sign_as_signer` produces one master-key signature over the XRPL multisigning message for that signer (`SMT\0` + signing fields + the signer's account ID). The key is used once and not retained.
- `assemble_multisigned` checks every signature first, then sorts `Signers` by numeric account ID.

`verify_multisigned_payment` refuses with a stable code unless all of the following hold:

| Check | Refusal codes |
| --- | --- |
| Multisigned Payment, no single-signature fields | `NOT_MULTISIGNED`, `WRONG_TRANSACTION_TYPE`, `MALFORMED_TRANSACTION` |
| Unsigned form equals the authorized digest | `TRANSACTION_DIGEST_MISMATCH` |
| Account, destination, currency, issuer, amount, network | `ACCOUNT_MISMATCH`, `DESTINATION_MISMATCH`, `CURRENCY_MISMATCH`, `ISSUER_MISMATCH`, `AMOUNT_MISMATCH`, `NETWORK_MISMATCH` |
| State from one validated ledger | `LEDGER_NOT_VALIDATED`, `LEDGER_STATE_INCONSISTENT` |
| SignerList exactly {ResearchBot: 1, policy signer: 1}, quorum 2 | `SIGNER_LIST_MISSING`, `SIGNER_LIST_MISMATCH`, `SIGNER_WEIGHT_MISMATCH`, `QUORUM_MISMATCH` |
| No RegularKey; master key in the configured state | `REGULAR_KEY_PRESENT`, `MASTER_KEY_STATE_MISMATCH` |
| Exactly the two signers, canonical order, master keys, valid signatures | `SIGNER_MISSING`, `UNEXPECTED_SIGNER`, `DUPLICATE_SIGNER`, `SIGNER_ORDER`, `SIGNER_KEY_NOT_MASTER`, `SIGNER_MASTER_DISABLED`, `SIGNATURE_INVALID`, `QUORUM_NOT_MET` |
| Fee ≥ base fee × (1 + 2) and ≤ ceiling | `FEE_TOO_LOW`, `FEE_ABOVE_CEILING` |
| Sequence is the account's next, or the ticket exists | `SEQUENCE_MISMATCH`, `TICKET_UNAVAILABLE` |
| Validated index < LastLedgerSequence ≤ validated index + window | `LAST_LEDGER_SEQUENCE_EXPIRED`, `LAST_LEDGER_SEQUENCE_TOO_FAR` |

### Master-key and regular-key limitations

- **No RegularKey.** A RegularKey on the agent payment account is always refused.
- **The master key is not restricted by multisigning.** `MasterKeyPolicy` makes Credence's expectation explicit, and verification fails if the ledger disagrees.
  - `OWNER_RETAINED` is the specified configuration. The build plan's accounts table (XRPL integration, "Accounts and keys") gives the borrower account's key as "Owner's Ledger device", with "regular key disabled". The owner signs setup, revocation and the emergency sweep. While that master key is enabled, the owner can authorize any transaction alone. ResearchBot alone still cannot.
  - `DISABLED` (only the SignerList can authorize) is supported and tested, but it is not the specified setup: the owner could then not revoke or sweep without both signers.
- **Signer keys.** Signers must sign with the master key whose address is the signer account. Signer regular keys are not supported. A signer account whose master key is disabled on ledger is refused.

## 2. Core payment-authorization binding

`credence/signers/payment_authorization.py` defines the `PaymentAuthorization` statement. It is immutable, Ed25519-signed by a Credence settlement-authority key, and domain-separated (`credence/xrpl/payment-authorization/v1`, then the Kessai canonical form, JCS (RFC 8785) + NFC).

It binds:

- the operation, facility, reservation and provider;
- the receipt digest, receipt-chain head and verifier decision digest;
- the accounting settlement time;
- the amount (integer micro-tRLUSD), currency and issuer;
- the source and destination accounts;
- the authorized unsigned-transaction digest and its scheme;
- the network ID and an expiry (at most 300 seconds ahead).

Its `authorization_id`, the SHA-256 of the signed bytes, is the X0t journal's `payment_authorization_id`. The outbox event therefore points back to exactly one statement.

It is operational, not canonical. No C1/C3A object, C3A key role or signature definition changed. `credence.decision_record.authorized_transaction_digest` stays null.

> **Open decision for Maura.** The build plan says the policy service checks the digest "in the engine's decision record". That field is frozen null, so X1a carries the digest in this statement, as the X1a brief directs. Populating the decision record instead would be a Core change that needs separate approval.

### Policy signer checks (`credence/signers/policy_signer.py`)

The policy signer checks each of the following independently before it signs:

1. **Statement.** The signature is valid under a current authority key (`BAD_AUTHORIZATION_SIGNATURE`, `UNKNOWN_AUTHORITY_KEY`, `AUTHORITY_KEY_REVOKED`), and the statement has not expired and is not overlong (`AUTHORIZATION_EXPIRED`, `AUTHORIZATION_TOO_LONG`).
2. **Network.** The statement's network ID matches the configured one.
3. **Core's own accounting settlement**, read through `AccountingSettlementSource`. It must exist and be `accounting_settled` (`SETTLEMENT_NOT_FOUND`, `SETTLEMENT_NOT_ACCOUNTING_SETTLED`). Every bound field must equal Core's record (`SETTLEMENT_MISMATCH` with the field name). The chain head must be the bound one (`CHAIN_HEAD_MISMATCH`).
4. **Parties.** The source account and the tRLUSD issuer and currency are checked. The destination must be that provider's configured payout account (`DESTINATION_NOT_PROVIDER_PAYOUT`).
5. **Transaction.** The digest must equal the authorized digest (`TRANSACTION_DIGEST_MISMATCH`). The signer then rebuilds the canonical Payment from the statement and compares digests (`TRANSACTION_FIELDS_MISMATCH`), so memos, paths, `SendMax` or flags cannot ride along even inside an authorized digest.
6. **Ledger.** The policy signer itself must be in the validated SignerList (`POLICY_SIGNER_NOT_AUTHORIZED`). Signer list, keys, fee, sequence and expiry are checked as in section 1.
7. **Write-ahead record.** Before signing, a durable record binds the receipt, operation and authorization to one unsigned digest. A different transaction for the same receipt or operation is refused (`ALREADY_AUTHORIZED`), even after a restart or under concurrency. Re-signing the identical transaction is idempotent.

**Core dependency.** Core has no read that returns the settled row. This is filed as `contracts/requests/0009-core-accounting-settlement-read.md`. Tests use a fake source.

## 3. Trusted ledger transport (`credence/xrpl_adapter/rippled.py`)

`RippledTransport` uses JSON-RPC with `api_version` 2.

**Readiness.** Every operation in the brief is implemented and tested against a deterministic fake node (`tests/unit/x1a_rippled.py`). None has been run against the pinned rippled.

| Operation | Implementation |
| --- | --- |
| Validated ledger identity and network | `server_state` (network ID, optional build pin, validated index, hash, base fee, reserves, `complete_ledgers`), then `ledger` to confirm the same hash is validated |
| Account info and SignerList | `account_info` with `signer_lists`, pinned by `ledger_hash` |
| Fee and sequence or ticket | Base fee from the validated ledger. Sequence from `account_info`. Tickets from paged `account_objects` |
| Transaction submission | `submit` with `tx_blob` only. Refused unless `submission_enabled`, which X1a never sets. `offline_only = False`, so the X0t `PaymentService` refuses this transport for submission |
| Transaction-hash lookup | `tx` binary and JSON forms plus `ledger`. The hash is derived from the blob, the two forms must agree on result, index and validation, and the ledger hash must match. The result is the X0s evidence envelope |
| Complete-ledger-history coverage | `complete_ledgers` parsed into ranges for the X0t observer. Gaps prevent proof of non-inclusion |
| Raw validated evidence capture | Every request and its exact response bytes are kept. The lookup envelope embeds them |

The client accepts only loopback `http(s)` endpoints, bypasses proxies, and refuses any `secret`, `seed`, `passphrase` or key parameter.

The ledger source remains a trust boundary. It is not independent consensus proof.

## 4. tRLUSD local-ledger setup (`credence/xrpl_adapter/trlusd_setup.py`)

### Accounts

| Account | Configuration | Owned objects | XRP needed |
| --- | --- | --- | --- |
| Issuer | `DefaultRipple`; no `TransferRate`, `RequireAuth` or `GlobalFreeze` | 0 | base reserve + setup fees |
| Agent payment account (owner holds master key) | tRLUSD trust line; SignerList; no RegularKey | 2 | base + 2 × increment + 3 × base fee per payment |
| Provider settlement account (one per provider) | tRLUSD trust line | 1 | base + 1 × increment |
| ResearchBot, policy signer | Signer accounts only; may be unfunded; master keys | 0 | none |

Reserves come from the validated ledger's `reserve_base` and `reserve_inc`. They are not assumed.

### Steps

Prepared by `setup_transactions` as unsigned transactions. None was signed or submitted.

1. Issuer `AccountSet` `asfDefaultRipple`.
2. Agent account `TrustSet` for tRLUSD (`tfSetNoRipple`), signed by the owner's master key.
3. Each provider settlement account `TrustSet` for tRLUSD.
4. Issuer `Payment` of the tRLUSD test balance to the agent account.
5. Agent account `SignerListSet`: ResearchBot 1, policy signer 1, quorum 2, signed by the owner's master key.
6. Only for `MasterKeyPolicy.DISABLED`: agent account `AccountSet` `asfDisableMaster`.

**Readiness check.** `observe_setup` reads every account and trust line from one validated ledger; it is read-only. `setup_findings` then lists blockers:

- issuer flags;
- signer list, keys and master-key state;
- XRP reserve plus multisign fee;
- tRLUSD balance and provider trust-limit headroom;
- signer master keys;
- fee ceiling.

### Local instructions, for the separately authorized run

1. Pin the rippled image and fill in `infra/rippled/rippled.cfg`. Both are still placeholders.
   - Use the build and network ID recorded in X0 (rippled 3.4.0, `4a4fded2…`, network ID 21337).
   - Enable the eight amendments listed in `X0-READINESS.md`.
2. Run `make ledger-up` (`infra/rippled/` is owned by XRPL Integration).
3. Read-only preflight:
   - `RippledTransport(LoopbackJsonRpcClient("http://127.0.0.1:5005"), RippledTransportConfig(source_id=..., expected_network_id=21337, expected_build_version=...))`;
   - `validated_ledger_identity()`;
   - compare each response shape with the fake node and record any difference.
4. Fund and configure the accounts using steps 1 to 6, with keys generated outside Git and outside the browser.
5. Run `observe_setup` and then `setup_findings` until no findings remain.

Steps 2 to 5 are ledger writes or need a running node. They are not authorized in X1a.

## 5. Payment reliability (`credence/xrpl_adapter/settlement.py`)

Built on the accepted X0t journal, with no second transaction identity, retry path or effect channel.

`classify_payment` names exactly one outcome per journal record:

| Outcome | Meaning |
| --- | --- |
| `NOT_SUBMITTED` | Signed bytes persisted, never broadcast |
| `SUBMISSION_PENDING` | Submitted or awaiting validation |
| `VALIDATED_SUCCESS` | Validated success with `XRPL_SETTLED` |
| `VALIDATED_FAILURE` | Validated, and the ledger result is not `tesSUCCESS` |
| `VALIDATED_UNEXPECTED_DELIVERY` | Validated `tesSUCCESS`, but the delivered asset or amount was wrong. A transferred-funds exception, decided from the stored evidence's ledger result |
| `EXPIRED_NOT_INCLUDED` | Anchored complete history proves non-inclusion |
| `UNKNOWN_RECONCILIATION_REQUIRED` | X0t `UNKNOWN`, or any record whose evidence does not support its state |

`SqliteSettlementEventConsumer` is the production idempotent consumer contract:

- it applies an outbox event in its own `BEGIN IMMEDIATE` transaction, keyed by the outbox `event_id`, and runs the destination effect in the same transaction;
- a replay is a no-op;
- a contradictory event, or a second event for the same operation, authorization or hash, is refused;
- an effect failure rolls back, so the event is retried.

It is the single live settlement-effect authority path. The X0t `claim_xrpl_settlement` bit is not a live authority and must not be used alongside it. The X0t code still exposes that method; X1a did not remove it.

## 6. Test evidence

| Suite | Tests |
| --- | --- |
| `tests/unit/test_xrpl_x1a_multisign.py` | 45 |
| `tests/unit/test_xrpl_x1a_policy_signer.py` | 35 |
| `tests/unit/test_xrpl_x1a_transport.py` | 34 |
| `tests/unit/test_xrpl_x1a_flow.py` | 14 |
| `tests/unit/test_xrpl_x1a_setup.py` | 12 |

Brief adversarial cases, and where they are covered:

| Case | Coverage |
| --- | --- |
| One missing signature | `SIGNER_MISSING` |
| Invalid signatures or signer quorum | `SIGNATURE_INVALID`, `QUORUM_MISMATCH`, `SIGNER_WEIGHT_MISMATCH`, `SIGNER_LIST_MISMATCH` |
| Transaction altered after signing | Six fields; digest and signature both fail |
| Wrong issuer, amount or destination | Multisign verification and the policy signer |
| Revoked or unauthorized policy signer | Removed from the SignerList; revoked or unknown authority key; wrong key; unexpected signer |
| Submission timeout | Transport mapping; flow from `UNKNOWN` to resolution by hash |
| Duplicate submission | Node `tefPAST_SEQ` is not final; duplicate journal entry; second transaction per receipt |
| Incorrect validated ledger evidence | Ledger-hash and validation faults; binary and JSON disagreement; hash and blob mismatch; unusable `delivered_amount` |
| Missing history | Gapped `complete_ledgers` leaves expiry `UNKNOWN` |
| Concurrent settlement-event consumption | Eight consumers, two delivering services, and eight concurrent signers |

The end-to-end flow runs offline: Core binding → policy signer and ResearchBot → verification → X0t journal → submission (offline mock) → reconciliation through `RippledTransport.lookup` → outbox → idempotent consumer. Exactly one effect is applied.

## Remaining dependencies for one live test-token payment

1. **Maura's authorization for X1b** (ledger writes).
2. **Pinned local rippled.** The image tag, digest and `rippled.cfg` in `infra/rippled/` are still Checkpoint 0 placeholders. Owner: XRPL Integration.
3. **Read-only shape check** of every transport call against that node, recording any API v2 shape difference before any write.
4. **Core request 0009**, the read-only accounting-settlement fact. The policy signer has no live Core source until it lands.
5. **Key custody.**
   - The settlement-authority key: issuance, custody and its key ring. The build plan does not name this key; it is new in X1a.
   - The policy-signer key: the build plan's key table puts it in 1Password, used only by the policy service.
   - Neither key may be in Git or the browser.
6. **ResearchBot signing integration** (Inference workstream). ResearchBot must sign the same unsigned transaction with its own key; X1a tests use a test key.
7. **Enforced live transport boundary.** The X0t `PaymentService` accepts only `offline_only` transports. A live checkpoint needs a reviewed, explicit submission authority in place of that marker. Direct journal methods must stay out of live settlement authority.
8. **Core-side destination effect** for the consumer: recording `xrpl_settled` or a `credence.settlement_record`. This is a Core change and needs approval.
9. **Execution of the setup steps** in section 4, then a clean `setup_findings`.
10. **Replacement after proven expiry.** The policy signer's log refuses any second transaction for a receipt. That includes a replacement after the first is `EXPIRED_NOT_INCLUDED`, so such a receipt cannot be paid yet. A reviewed procedure must allow exactly one replacement, and only on the journal's proven non-inclusion of the prior hash.

## Subsequent X1 adapters (documented, not implemented)

- **Ledger hardware signing.** The owner's Ledger signs `SignerListSet` (setup and revocation), facility approval and the emergency sweep through the `ledger-bridge/` WebHID bridge.
  - The device would supply the setup-step signatures in section 4. It never sees the agent's or policy signer's keys, and is never asked to sign routine provider Payments.
  - Clear-signing support for `SignerListSet` and `LoanSet` is the Ledger device spike's question and is not answered here.
- **QuickNode transport.** A second `JsonRpcClient` and `RippledTransport` source for a hosted public XRPL node, for example to show a public explorer link if Devnet carries the lending amendments.
  - It is a different trust boundary from a local node. It must keep the same pinned-ledger, history and evidence checks.
  - The loopback-only client is deliberately not reused for it.
  - Today Credence runs on a local XRPL ledger with the lending amendments enabled.

## Ownership note

`credence/xrpl_adapter/`, `spike/xrpl/` and `contracts/XRPL-ADAPTER-CONTRACT.md` are listed under XRPL Integration (Codex) in `docs/OWNERSHIP.md`, and `credence/signers/` under Credence Core. Maura assigned X1a to this workspace. `docs/OWNERSHIP.md` was not changed.
