"""Offline regressions for Checkpoint 1E-R1-CORRECTED."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from xrpl.core.binarycodec import decode

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
LIVE_EVIDENCE = ROOT / "evidence" / "checkpoint-1e-live-01"
sys.path.insert(0, str(SCRIPTS))

from e6_oracle import (  # noqa: E402
    LOAN_TERM_FIELDS,
    POSITIVE_ORACLE_REQUIRED_FIELDS,
    E6BindingError,
    E6ProjectionError,
    authorization_assertions,
    live_binding_assertions,
    positive_post_state_assertions,
    project_positive_oracle_terms,
    public_signing_evidence,
    require_live_bindings,
    require_positive_oracle_projection,
)


def _json(name: str) -> dict[str, Any]:
    value = json.loads((LIVE_EVIDENCE / name).read_text())
    assert isinstance(value, dict)
    return value


def _fixture() -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    bindings = _json("d12-live-bindings.json")["bindings"]
    observation = _json("positive-observation.json")
    before = _json("prerequisite-state.json")["state"]
    after = deepcopy(before)
    post = observation["post_state"]
    after.update(
        {
            "loan": post["loan"],
            "borrower_account": {
                "Account": bindings["borrower_account"],
                "Balance": post["borrower"]["xrp_balance"],
                "OwnerCount": post["borrower"]["OwnerCount"],
                "Sequence": post["borrower"]["Sequence"],
            },
            "borrower_balance": post["borrower"]["tRLUSD_balance"],
            "broker_owner_balance": post["broker_owner_tRLUSD_balance"],
            "vault_pseudo_balance": post["vault"]["pseudo_account_tRLUSD_balance"],
            "vault": {**before["vault"], **post["vault"]},
            "loan_broker": {**before["loan_broker"], **post["loan_broker"]},
        }
    )
    return bindings, observation, before, after


def _old_projection(envelope: dict[str, Any], vault_id: str) -> dict[str, Any]:
    projection = {field_name: envelope.get(field_name) for field_name in LOAN_TERM_FIELDS}
    projection["VaultID"] = vault_id
    return projection


def _corrected_projection(bindings: dict[str, Any]) -> dict[str, Any]:
    return project_positive_oracle_terms(
        bindings["exact_loan_set_body"],
        vault_id=bindings["vault_id"],
        intended_borrower=bindings["borrower_account"],
    )


def _all_pass(checks: list[Any]) -> bool:
    return all(check.passed for check in checks)


def test_r1_committed_fixture_reproduces_old_projection_keyerror() -> None:
    bindings, observation, before, after = _fixture()
    old = _old_projection(bindings["exact_loan_set_body"], bindings["vault_id"])
    with pytest.raises(KeyError, match="Account"):
        positive_post_state_assertions(
            validated_result="tesSUCCESS",
            before=before,
            after=after,
            terms=old,
            expected_start_date=observation["post_state"]["loan"]["StartDate"],
        )


def test_r2_committed_finalized_loanset_contains_account() -> None:
    bindings, _observation, _before, _after = _fixture()
    assert "Account" in bindings["exact_loan_set_body"]


def test_r3_committed_account_equals_intended_borrower() -> None:
    bindings, _observation, _before, _after = _fixture()
    assert bindings["exact_loan_set_body"]["Account"] == bindings["borrower_account"]


def test_r4_corrected_projection_contains_account() -> None:
    bindings, _observation, _before, _after = _fixture()
    projection = _corrected_projection(bindings)
    assert projection["Account"] == bindings["borrower_account"]


def test_r5_missing_account_fails_projection_preflight() -> None:
    bindings, _observation, _before, _after = _fixture()
    projection = _corrected_projection(bindings)
    projection.pop("Account")
    with pytest.raises(E6ProjectionError, match="Account"):
        require_positive_oracle_projection(
            projection,
            intended_borrower=bindings["borrower_account"],
        )


def test_r6_wrong_account_fails_semantic_assertion() -> None:
    bindings, observation, before, after = _fixture()
    projection = _corrected_projection(bindings)
    projection["Account"] = _json("actors-public.json")["credence_policy"]["account"]
    checks = positive_post_state_assertions(
        validated_result="tesSUCCESS",
        before=before,
        after=after,
        terms=projection,
        expected_start_date=observation["post_state"]["loan"]["StartDate"],
    )
    borrower = next(check for check in checks if check.name == "Loan Borrower")
    assert not borrower.passed


def test_r7_corrected_projection_has_every_consumed_field() -> None:
    bindings, _observation, _before, _after = _fixture()
    projection = _corrected_projection(bindings)
    assert set(POSITIVE_ORACLE_REQUIRED_FIELDS) == set(projection)


@pytest.mark.parametrize("field_name", POSITIVE_ORACLE_REQUIRED_FIELDS)
def test_r8_removing_any_consumed_field_fails_preflight(field_name: str) -> None:
    bindings, _observation, _before, _after = _fixture()
    projection = _corrected_projection(bindings)
    projection.pop(field_name)
    with pytest.raises(E6ProjectionError, match=field_name):
        require_positive_oracle_projection(
            projection,
            intended_borrower=bindings["borrower_account"],
        )


def test_r9_economic_terms_match_first_live_transaction() -> None:
    bindings, observation, _before, _after = _fixture()
    transaction = decode(observation["transaction"]["signed_transaction_blob"])
    assert {field_name: transaction.get(field_name) for field_name in LOAN_TERM_FIELDS} == {
        field_name: bindings["exact_loan_set_body"].get(field_name)
        for field_name in LOAN_TERM_FIELDS
    }


def test_r10_committed_authorization_domains_remain_valid() -> None:
    bindings, observation, _before, _after = _fixture()
    actors = _json("actors-public.json")
    transaction = decode(observation["transaction"]["signed_transaction_blob"])
    evidence = public_signing_evidence(transaction, observation["transaction"]["hash"])
    checks = authorization_assertions(
        evidence,
        borrower_account=bindings["borrower_account"],
        expected_borrower_signers={
            actors["research_bot"]["account"]: actors["research_bot"]["public_key_hex"],
            actors["credence_policy"]["account"]: actors["credence_policy"]["public_key_hex"],
        },
        broker_owner_account=bindings["broker_owner_account"],
        broker_owner_public_key=actors["broker_owner"]["public_key_hex"],
        expected_terms={field_name: transaction.get(field_name) for field_name in LOAN_TERM_FIELDS},
    )
    assert _all_pass(checks)


def test_r11_d12_rejects_original_incomplete_projection() -> None:
    bindings, _observation, _before, _after = _fixture()
    hardened = dict(bindings)
    hardened["positive_oracle_terms"] = _old_projection(
        bindings["exact_loan_set_body"], bindings["vault_id"]
    )
    assert not _all_pass(live_binding_assertions(hardened))
    with pytest.raises(E6BindingError, match="Account"):
        require_live_bindings(hardened)


def test_r12_d12_accepts_corrected_complete_projection() -> None:
    bindings, _observation, _before, _after = _fixture()
    hardened = dict(bindings)
    hardened["positive_oracle_terms"] = _corrected_projection(bindings)
    assert _all_pass(live_binding_assertions(hardened))
    require_live_bindings(hardened)
