# Checkpoint 1D-H — SignerList response-shape repair

## Scope and retained result state

This is a harness-only repair based on the committed Checkpoint 1D evidence.
It submits no XRPL transaction and does not run E5 or E6. E5 remains
**BLOCKED**, and E5-A through E5-E remain **NOT ATTEMPTED**.

## Reproduced false block

The committed `05-borrower_set_2_of_2_signer_list.json` record contains both:

- an empty `after_state.signer_lists` value produced by reading the wrong
  top-level response path; and
- the installed list at
  `after_state.borrower_account.signer_lists`, preserving the original
  `account_data.signer_lists` value.

The legacy assertion therefore reports zero SignerLists. Reading the pinned
response location instead proves one list with quorum 2 and exactly these
weight-1 members:

- ResearchBot: `rE2TtYDybGhbKSUbcaeUpewHQrJPaJbeL1`
- Credence policy signer: `ra1BR57gxUrHsyE3ZEj7GPWmiaLBmnNzJp`

These are setup observations only. They are not completed E5 results.

## Pinned-source basis

The runtime reported rippled source commit
`4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17`.

At that exact commit:

- `include/xrpl/protocol/ApiVersion.h` lines 25-28 and 44 establish that a
  network request without an explicit API version uses API version 1.
- `src/xrpld/rpc/handlers/account/AccountInfo.cpp` lines 211-235 build the
  requested SignerList array and place it at
  `result.account_data.signer_lists` for API version 1. API version 2 uses the
  different top-level location.
- `src/test/rpc/AccountInfo_test.cpp` lines 128-160 exercise the API-version-1
  nested location for both an empty and an installed SignerList.

Pinned source links:

- <https://github.com/XRPLF/rippled/blob/4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17/include/xrpl/protocol/ApiVersion.h#L25-L45>
- <https://github.com/XRPLF/rippled/blob/4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17/src/xrpld/rpc/handlers/account/AccountInfo.cpp#L211-L235>
- <https://github.com/XRPLF/rippled/blob/4a4fded2eba11427c48ce3f24d9c1aea5e7a9d17/src/test/rpc/AccountInfo_test.cpp#L128-L160>

## Repair

`parse_account_info_signer_state` reads only
`result.account_data.signer_lists`. It rejects an absent or non-object
`account_data`, an absent `signer_lists`, and a non-array value. It does not
guess between API response versions.

`validate_expected_signer_list_response` combines that structural parser with
the existing semantic assertions. It fails closed unless there is exactly one
applicable list, quorum is 2, and the unique signer-to-weight mapping is exactly
the expected ResearchBot/Credence 1+1 set. Reordering remains immaterial;
malformed, missing, extra, duplicate, or incorrectly weighted entries fail.

## Signer-evidence and provenance audit

The future E5 runner already captures, before submission, the signed blob,
transaction hash, top-level `SigningPubKey`, top-level signature presence and
value, and every `Signers` member's account, public key, and public signature.
Each transaction record also retains the submission response, validated lookup
when available, and before/after protected state. Synthetic D5-D14 tests bind
the multisign, master-sign, blob, protected-state, and no-private-material
paths. No required E5 signer-evidence path is missing.

Future run provenance hashes the assertions, oracle, runner, and tests. It now
also identifies `scripts/e5_oracle.py` explicitly as the SignerList parser
binding and records its SHA-256.
