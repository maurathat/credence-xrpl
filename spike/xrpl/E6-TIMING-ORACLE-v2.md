# Prospective E6 timing oracle v2

This oracle is prospective. It applies only to E6 experiments executed after
Checkpoint 1E-T. It does not alter the historical first-live E6 evidence or
its frozen `FAIL` verdict.

Pinned rippled 3.4.0 source commit:
`4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17`.

For a successful future LoanSet:

- `StartDate` equals the applying view header `closeTime` read by
  `LoanSet::doApply` (`LoanSet.cpp:225-229,649-666`).
- `NextPaymentDueDate == StartDate + PaymentInterval`
  (`LoanSet.cpp:690`).
- The frozen one-payment E6 terms use `PaymentInterval = 60` seconds and
  `GracePeriod = 60` seconds.
- `GracePeriod` is stored separately from `NextPaymentDueDate`; it is not
  included in the initial due date.
- A transaction lookup `date` is the accepted-ledger time and MUST NOT be used
  as the expected StartDate.

No negative case in Checkpoint 1E-N is expected to create a Loan. This timing
oracle is nevertheless frozen with the negative harness so N6 records the
applying-ledger construction time and accepted-ledger time without treating
either as a successful Loan StartDate.
