# Historical X0 journal fixtures

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
