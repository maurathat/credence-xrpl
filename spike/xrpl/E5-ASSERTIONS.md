# Checkpoint 1D E5 frozen assertions

These assertions are fixed before the first Checkpoint 1D live transaction.
Checkpoint 1D exercises ordinary XRP `Payment` authorization only. It does not
submit a lending transaction or make a claim about `LoanSet`.

## Actor and signer-list state

- The borrower, ResearchBot signer, Credence policy signer, and payment recipient
  are fresh disposable accounts on the fresh standalone ledger.
- The borrower has one `SignerList`, with `SignerQuorum = 2`.
- Its exact entries are ResearchBot weight 1 and Credence policy weight 1.
- The borrower `AccountRoot` has no `RegularKey` field.
- The borrower master key is not disabled (`lsfDisableMaster` is clear).

## Public signing evidence

Every E5 transaction record must contain the signed transaction blob and an
equivalent decoded public authorization representation. The representation must
retain each participating account, public signing key, and transaction signature;
the top-level `SigningPubKey` behavior; the presence or absence of a top-level
signature; transaction hash; account sequence; and validated lookup result.

Seeds and private keys are never evidence.

For a multisigned transaction:

- top-level `SigningPubKey` is empty;
- no top-level `TxnSignature` is present; and
- the `Signers` participants and their public keys exactly match the expected set.

For the owner-master transaction:

- no `Signers` array is present;
- top-level `SigningPubKey` equals the borrower's public master key; and
- a top-level `TxnSignature` is present.

## E5-A — ResearchBot alone

- One multisign contribution: ResearchBot only.
- Expected authoritative result: `tefBAD_QUORUM`.
- Transaction is not validated.
- Borrower account, borrower signer list, and recipient account remain exactly
  unchanged.

## E5-B — Credence alone

- One multisign contribution: Credence policy signer only.
- Expected authoritative result: `tefBAD_QUORUM`.
- Transaction is not validated.
- Borrower account, borrower signer list, and recipient account remain exactly
  unchanged.

## E5-C — ResearchBot plus Credence

- Multisign contributions are exactly ResearchBot and Credence policy signer.
- Expected result: validated `tesSUCCESS`.
- No master-key authorization is present.
- Recipient balance increases by the exact Payment amount.
- Borrower balance decreases by the Payment amount plus the encoded fee.
- Borrower sequence increases by one.
- Borrower signer list remains unchanged.

## E5-D — owner master key

- The borrower master key is the only authorization.
- Expected result: validated `tesSUCCESS`.
- Recipient balance increases by the exact Payment amount.
- Borrower balance decreases by the Payment amount plus the encoded fee.
- Borrower sequence increases by one.
- Borrower signer list remains unchanged.

## E5-E — regular-key absence

- The final validated borrower `AccountRoot` has no `RegularKey` field.
- The final signer list still has quorum 2 and the exact two weight-1 entries.
- The master-key-disabled flag remains clear.

Checkpoint 1D stops on the first result, authorization, state, or evidence
contradiction. A failed live step is not repaired or retried.
