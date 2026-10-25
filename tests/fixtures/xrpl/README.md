# Historical journal fixtures

These SQLite databases were created by the unmodified `PaymentJournal` at
commit `aa236e1fbe1db88038673c08826f62248d738e50`. They are immutable inputs to
the X0t fail-closed compatibility tests; they are not synthesized by changing
an X0s schema.

`x0_v1_journal.fixture` contains four distinct signed transactions: a validated
success whose settlement effect was claimed, a validated failure, an expired
payment and a pending payment. `x0_v1_empty_journal.fixture` contains the same
historical schema with no payment rows.

To reproduce them, archive the exact commit to a temporary directory, then run
`generate_x0_v1_journals.py` with that directory as both the working directory
and first `PYTHONPATH` entry. Use the project interpreter so xrpl-py is
available, and pass `--empty` for the empty fixture. The generator intentionally
uses the historical public journal methods rather than direct SQL updates.

The committed fixture SHA-256 values are:

- `x0_v1_journal.fixture`: `7eb66c223522ea9f6b65667711c4bff79475a09d722d1c77fd1eefd79717eb73`
- `x0_v1_empty_journal.fixture`: `815a97d9c8b0e35d2ef676bca4c4b3664f2845600fe539aeb1a0234e12248373`

## Version-2 journals (X0s/X0t/X1a)

`x1b_v2_journal.fixture` and `x1b_v2_empty_journal.fixture` were created by the
unmodified `PaymentJournal` at commit
`607cc2a40075aac28d354d748e01524784088e14` (journal schema version 2). They are
immutable inputs to the X1c tests that refuse version-2 journals without
changing them (`JOURNAL_VERSION_2_UNSUPPORTED`).

The populated fixture holds five payments: a validated success with its
undelivered outbox event and a legacy claim, a validated failure, a short
delivery in the version-2 representation (`VALIDATED_FAILURE` with
`DELIVERED_AMOUNT_MISMATCH`), an expired payment and a pending payment.

To reproduce them, archive that commit to a temporary directory, link
`third_party/custody`, then run `generate_x1b_v2_journals.py` with the archive
as both the working directory and first `PYTHONPATH` entry, using the project
interpreter. Pass `--empty` for the empty fixture. Rows carry wall-clock
timestamps, so a regeneration is equivalent but not byte-identical; the
committed bytes below are authoritative and are checked by the tests.

- `x1b_v2_journal.fixture`: `4638950db55e2baaca319cf046fc51e63b18d2535a2ebee201e0f3918acfb9aa`
- `x1b_v2_empty_journal.fixture`: `32490a0a52374d82e0ebf52582b86e0b04ccbfc4d6d41828d382ed2de45e6999`
