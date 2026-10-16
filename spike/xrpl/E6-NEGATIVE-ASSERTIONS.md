# Checkpoint 1E-N frozen E6 negative assertions

This prospective specification is frozen before the Checkpoint 1E-N ledger is
started. It executes N1-N6 only. It MUST NOT submit another positive LoanSet,
LoanPay, or any E7-E9 transaction.

## Common LoanSet body

Every case starts from the Checkpoint 1E-D one-payment terms: principal 1000
integer micro-tRLUSD, PaymentTotal 1, PaymentInterval 60 seconds, GracePeriod
60 seconds, and all specified fees and rates zero. Account is the borrower,
Counterparty is the Loan Broker owner, and LoanBrokerID identifies the fresh
case fixture. Autofilled Fee, Sequence, LastLedgerSequence, and NetworkID are
fixed before signing. Top-level SigningPubKey is empty.

The live preflight must bind all six exact finalized constructions before N1
is submitted. N1-N5 use one Investment-phase fixture. N6 uses an independent
Subscription-phase fixture. No case depends on an existing Loan.

## Expected results and authorization

| Case | Borrower authorization | Broker authorization | Mutation | Expected result |
| --- | --- | --- | --- | --- |
| N1 | ResearchBot only | valid CPT-NUL signature | none | `tefBAD_QUORUM` |
| N2 | Credence policy only | valid CPT-NUL signature | none | `tefBAD_QUORUM` |
| N3 | ResearchBot + Credence 2-of-2 | absent | none | `temBAD_SIGNER` |
| N4 | ResearchBot + Credence 2-of-2 | present, fixed invalid signature `00` | none | local `invalidTransaction` |
| N5 | ResearchBot + Credence 2-of-2 over Y | valid over X, invalid over Y | only PrincipalRequested `1000` in X to `1001` in Y | local `invalidTransaction` |
| N6 | ResearchBot + Credence 2-of-2 | valid CPT-NUL signature | Subscription phase | validated `tecTOO_SOON` |

Every borrower contribution must validate over the SMT-NUL domain and the
submitted body. Valid broker signatures must validate over the CPT-NUL domain.
The borrower master key is absent. Public evidence must retain identities,
public keys, public signatures, payloads, digests, exact bodies, and results.

## Protected state

For N1-N5, the complete semantic projection is unchanged:

- expected Loan identity remains absent;
- Vault and Loan Broker objects and relationships;
- Vault and broker pseudo-account tRLUSD holdings;
- borrower and broker-owner tRLUSD holdings;
- Loan Broker debt, cover, LoanSequence, and owner count;
- borrower AccountRoot sequence and owner count;
- borrower SignerList and regular-key absence.

For N6, the same lending, authorization, and holding projection is unchanged.
Only borrower AccountRoot XRP balance and Sequence are excluded from that
equality projection. Sequence must increase by exactly one and XRP balance
must decrease by exactly the transaction Fee. The expected Loan remains
absent.

## Timing capture

N6 records the configured phase dates, parent validated-ledger close time,
source-derived applying-ledger header closeTime, final accepted-ledger close
time, and close-time resolution. This does not use transaction lookup `date`
as a Loan StartDate and does not claim a Loan was created.

Any unexpected result, authorization proof failure, protected-state mutation,
missing binding, or dependency on a positive LoanSet stops the run without a
repair or retry.
