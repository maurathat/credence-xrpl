# Checkpoint 1E-D E6 frozen LoanSet composition experiment

This specification is fixed before any E6 live transaction. It is derived from
rippled 3.4.0 source commit
`4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17`, not from the Credence plan.
Checkpoint 1E-D itself submits zero XRPL transactions.

## Exact authorization model

The frozen positive uses the unambiguous borrower-initiated form:

- `LoanSet.Account` is the borrower.
- `LoanSet.Counterparty` is explicitly the `LoanBroker.Owner`.
- `LoanSet.LoanBrokerID` identifies the Loan Broker ledger object.
- Because `Counterparty == LoanBroker.Owner`, pinned `LoanSet::preclaim` derives
  the borrower as `Account`.
- The borrower authorizes the ordinary transaction domain with the existing
  ResearchBot + Credence policy 2-of-2 SignerList. The borrower master key is
  not used and is not in the SignerList.
- The Loan Broker ledger object and its pseudo-account have no signing key. Its
  owner supplies the protocol counterparty authorization as a single signature
  in `CounterpartySignature`.
- Owner/Ledger facility approval remains separate, off-ledger future evidence.
  E6 does not exercise a Ledger hardware device.

Pinned sources: `LoanSet.cpp:156-180,282-304`,
`transactions.macro:959-985`, and `ledger_entries.macro:518-540`.

## Two signing domains over one body

After `fixCleanup3_4_0`:

- each borrower multisigner signs `SMT\0` / `0x534D5400`, followed by the
  canonical serialization of the transaction's signing fields, followed by
  that borrower's signer AccountID;
- the single Loan Broker-owner counterparty signer signs `CPT\0` /
  `0x43505400`, followed by the same canonical serialized signing body and no
  borrower-signer suffix;
- `Signers`, `TxnSignature`, `CounterpartySignature`, and `SponsorSignature`
  are excluded from that body; and
- the two signature containers are verified independently. Top-level
  `SigningPubKey` is empty for borrower multisigning. The counterparty object's
  `SigningPubKey` and `TxnSignature` select its single-sign path.

Therefore adding the borrower `Signers` array does not invalidate a correctly
formed counterparty signature, and adding `CounterpartySignature` does not alter
the borrower signing payload. The prefixes are nevertheless distinct, so a
signature cannot be moved between roles. The public evidence must reproduce the
payload for every signature and prove that the serialized body portion is
identical across both domains.

Pinned sources: `Sign.cpp:46-73,75-98,132-147`, `STTx.cpp:249-280,575-594`,
`HashPrefix.h:96-104`, `sfields.macro:411-418`, and
`LoanSecurity_test.cpp:1094-1154`.

Before both domains sign, the complete transaction body is frozen. It includes
the present common signing fields (`TransactionType`, `Account`, `Sequence`,
`Fee`, empty `SigningPubKey`, `LastLedgerSequence`, and `NetworkID`) and the
present LoanSet signing fields. Envelope values may be selected from the fresh
runtime, but neither domain may sign until all are final.

The exact positive economic fields are:

| Field | Value |
| --- | --- |
| `TransactionType` | `LoanSet` |
| `Account` | fresh borrower |
| `LoanBrokerID` | fresh Loan Broker ID |
| `Counterparty` | exact `LoanBroker.Owner` |
| `PrincipalRequested` | `1000` integer micro-tRLUSD |
| `LoanOriginationFee` | `0` |
| `LoanServiceFee` | `0` |
| `LatePaymentFee` | `0` |
| `ClosePaymentFee` | `0` |
| `OverpaymentFee` | `0` |
| `InterestRate` | `0` |
| `LateInterestRate` | `0` |
| `CloseInterestRate` | `0` |
| `OverpaymentInterestRate` | `0` |
| `PaymentTotal` | `1` |
| `PaymentInterval` | `60` seconds |
| `GracePeriod` | `60` seconds |

`Data`, sponsorship, delegation, tickets, and LoanSet flags are omitted. The
fee must be fixed before signing and must cover two ordinary multisign
contributions plus one counterparty signature. The exact resulting body,
serialization, payloads, and hashes are live evidence.

Changing `PrincipalRequested` from `1000` in body X to `1001` in body Y is the
frozen N5 mutation. `PrincipalRequested` is required by the LoanSet format,
used directly for disbursement, loan principal, Vault assets, and Loan Broker
debt, and is inside the counterparty-signed serialization.

## Required fresh prerequisite state

The eventual live runner must create fresh tRLUSD state, a closed-ended
cash-basis Vault (`LEVersion = 1`), a deposit during Subscription, a Loan Broker
owned by the Vault owner, first-loss cover, and the E5 borrower SignerList.
LoanSet must have adequate Vault assets, cover, debt headroom, holdings, reserve,
and a schedule that ends at least 60 seconds before Redemption.

The positive and N1-N5 execute during Investment. N6 executes during
Subscription against otherwise valid state and terms. N6 is orthogonal because
the same one-payment, 60-second schedule fits inside the minimum 180-second
Investment window, while the pinned phase check returns `tecTOO_SOON` before
LoanSet application.

Required enabled amendments are `MPTokensV1`, `SingleAssetVault`,
`LendingProtocol`, `LendingProtocolV1_1`, `fixCleanup3_1_3`,
`fixCleanup3_2_0`, `fixCleanup3_3_0`, and `fixCleanup3_4_0`. LoanSet's direct
dependency check requires SingleAssetVault and MPTokensV1; its transaction
format is gated by LendingProtocol; LendingProtocolV1_1 supplies the
closed-ended phase and cash-basis behavior; and fixCleanup3_4_0 supplies the
role-separated counterparty prefix used by this experiment.

Pinned sources: `LendingHelpers.cpp:69-81,251-271`,
`LoanSet.cpp:319-343,466-724`, and `Protocol.h:320-368`.

## E6-POSITIVE semantic oracle

Validated `tesSUCCESS` alone is insufficient. PASS requires all of these:

- the expected Loan index is SHA-512Half of namespace `L` as UINT16,
  `LoanBrokerID`, and the pre-transaction `LoanBroker.LoanSequence` as UINT32;
- exactly that object is a `Loan`, was absent before, and has the expected
  `LoanBrokerID`, pre-transaction `LoanSequence`, and borrower;
- its `PrincipalOutstanding`, fee/rate fields, `PaymentRemaining`,
  `PaymentInterval`, and `GracePeriod` match the frozen terms;
- `StartDate` equals the applying ledger close time,
  `PreviousPaymentDueDate = 0`, and
  `NextPaymentDueDate = StartDate + PaymentInterval`;
- for the zero-interest, one-payment terms, `PeriodicPayment` and
  `TotalValueOutstanding` both equal `1000`, and
  `ManagementFeeOutstanding = 0`;
- the Loan points to the expected Loan Broker, that broker still points to the
  expected Vault, and the exact Vault identity is unchanged;
- borrower tRLUSD increases by `1000`; Loan Broker-owner tRLUSD is unchanged;
  and the Vault pseudo-account tRLUSD decreases by `1000`;
- cash-basis `Vault.AssetsAvailable` decreases by `1000` while
  `Vault.AssetsTotal` is unchanged;
- `LoanBroker.DebtTotal` increases by `1000`, `CoverAvailable` is unchanged,
  `LoanSequence` increases by one, and its loan `OwnerCount` increases by one;
- borrower `OwnerCount` and transaction `Sequence` each increase by one; and
- the borrower 2-of-2 SignerList remains semantically unchanged.

No assertion uses a global object-count delta. Pinned identity and state sources:
`Indexes.cpp:71-105,593-597`, `LoanSet.cpp:546-724`, and
`ledger_entries.macro:542-626`.

## Bounded negatives

| Case | Frozen construction | Expected rejection |
| --- | --- | --- |
| N1 | Valid counterparty authorization; ResearchBot borrower contribution only | `tefBAD_QUORUM` |
| N2 | Valid counterparty authorization; Credence borrower contribution only | `tefBAD_QUORUM` |
| N3 | Valid borrower 2-of-2; `CounterpartySignature` absent | `temBAD_SIGNER` |
| N4 | Valid borrower 2-of-2; broker signature cryptographically invalid | local `invalidTransaction`, counterparty invalid-signature reason |
| N5 | Broker signs body X; `PrincipalRequested` alone changes to `1001` in Y; borrower 2-of-2 signs Y | local `invalidTransaction`, counterparty invalid-signature reason |
| N6 | Otherwise valid dual-domain body submitted during Subscription | validated `tecTOO_SOON` |

Every negative requires its exact rejection classification plus protected-state
assertions. A later lookup cannot overwrite submission or local-validation
results.

## Protected negative state

For N1-N6, the expected Loan index remains absent; the Vault and Loan Broker
semantic fields, debt, cover, loan sequence, Vault balances, borrower and broker
tRLUSD balances, borrower SignerList, and all relevant owner-directory
relationships remain unchanged.

N1-N5 are not applied, so the borrower AccountRoot balance, sequence, and owner
count also remain unchanged. N6 is a validated `tec` result: its XRP fee and
borrower sequence may change according to XRPL failure semantics and must be
asserted exactly, while they are excluded from the unchanged lending-state
projection. No negative can create a Loan.

## Public evidence and result handling

Each case retains the signed transaction blob/hash, complete decoded body,
borrower `Signers` identities/public keys/public signatures, empty top-level
`SigningPubKey`, Loan Broker-owner public identity/key/counterparty signature,
both prefix-tagged payloads and SHA-512Half digests, exact bound fields,
submission response, distinct validated lookup, ledger index, and semantic
before/after state. Seeds and private keys are never evidence.

Before a positive live submission, the runner must have nonempty bindings for
the git commit, assertion hash, runner hash, tests hash, build/source identity,
rippled version, network ID, amendments, evidence destination, actors, exact
body, Vault ID, Loan Broker ID, pre-loan sequence, and expected Loan index.

Any contradiction stops the run without repair or retry. E6 remains
`NOT_ATTEMPTED` until a separately authorized live checkpoint completes it.
