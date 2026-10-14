"""Executable bindings for the frozen Checkpoint 1C A-K oracle.

The normative meanings live in STATE-ASSERTIONS.md.  This module binds those
meanings to captured runner observations; it does not define new protocol
semantics.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from harness import (
    StateAssertion,
    classify_transaction,
    cover_transition_assertions,
    loan_broker_assertions,
    loan_broker_create_transition_assertions,
    mptoken_assertions,
    predicate_assertion,
    resolve_transaction_result,
    semantic_objects,
    state_assertion,
    vault_assertions,
    vault_create_transition_assertions,
)

ORACLE_ORDER = tuple("ABCDEFGHIJK")

CAPTURE_PATHS = frozenset(
    {
        "pre_state",
        "submission_response",
        "submission_hash_if_any",
        "client_exception",
        "validated_lookup",
        "transport_or_server_failure",
        "post_state",
        "ledger_before",
        "ledger_after",
        "context",
        "synthetic",
    }
)

REQUIRED_OUTPUT_FIELDS = frozenset(
    {
        "step",
        "name",
        "expected_result",
        "submission_result",
        "submission_hash_if_any",
        "client_exception",
        "validated_lookup_status",
        "validated_result_if_any",
        "pre_state",
        "post_state",
        "state_assertion",
        "state_assertions",
        "protected_state_unchanged",
        "ledger_index",
        "close_time",
        "ledger_before",
        "ledger_after",
        "classification",
        "result_resolution",
        "synthetic",
    }
)

FROZEN_PREDICATES: dict[str, tuple[str, ...]] = {
    "A": (
        "unique_created_vault",
        "vault_and_owner_mptoken",
        "created_objects_absent_before",
        "owner_trustline_protected",
        "auxiliary_objects_captured",
    ),
    "B": (
        "subscription_phase",
        "vault_relationships_stable",
        "asset_and_balance_delta",
        "share_delta",
        "unrelated_state_protected",
    ),
    "C": (
        "subscription_phase",
        "vault_relationships_stable",
        "asset_and_balance_delta",
        "share_delta",
        "unrelated_state_protected",
    ),
    "D": (
        "investment_phase",
        "complete_protected_state_unchanged",
    ),
    "E": (
        "investment_phase",
        "complete_protected_state_unchanged",
    ),
    "F": (
        "redemption_phase",
        "vault_relationships_stable",
        "asset_and_balance_delta",
        "share_delta",
        "unrelated_state_protected",
    ),
    "G": (
        "redemption_phase",
        "complete_protected_state_unchanged",
    ),
    "H": (
        "unique_created_loan_broker",
        "loan_broker_semantics",
        "created_broker_absent_before",
        "vault_and_owner_asset_protected",
        "owner_reserve_effects",
        "pseudo_account_captured",
    ),
    "I": (
        "vault_broker_relationship",
        "pseudo_accounts_present_and_distinct",
        "vault_configuration_protected",
    ),
    "J": (
        "loan_broker_configuration_stable",
        "cover_and_owner_balance_delta",
        "broker_holding_delta",
        "debt_and_vault_protected",
    ),
    "K": (
        "matching_loan_broker_absent",
        "complete_protected_state_unchanged",
    ),
}

EXPECTED_RESULTS = {
    "A": "tesSUCCESS",
    "B": "tesSUCCESS",
    "C": "tesSUCCESS",
    "D": "tecEXPIRED",
    "E": "tecTOO_SOON",
    "F": "tesSUCCESS",
    "G": "tecEXPIRED",
    "H": "tesSUCCESS",
    "I": "tesSUCCESS",
    "J": "tesSUCCESS",
    "K": "tecNO_PERMISSION",
}

STEP_NAMES = {
    "A": "E3 VaultCreate",
    "B": "E3 Subscription deposit",
    "C": "E3 Subscription withdrawal",
    "D": "E3 Investment deposit rejection",
    "E": "E3 Investment withdrawal rejection",
    "F": "E3 Redemption withdrawal",
    "G": "E3 Redemption deposit rejection",
    "H": "E4 Loan Broker creation",
    "I": "E4 Vault/Broker relationship validation",
    "J": "E4 cover establishment",
    "K": "E4 negative broker operation",
}

PredicateEvaluator = Callable[[dict[str, Any]], dict[str, StateAssertion]]


class OraclePreflightError(ValueError):
    """The frozen oracle is not completely or correctly bound."""


class OracleExecutionError(RuntimeError):
    """The fixture or live runner violated frozen-oracle control flow."""


@dataclass(frozen=True)
class OracleBinding:
    step: str
    name: str
    expected_result: str
    predicate_ids: tuple[str, ...]
    evaluator: PredicateEvaluator
    capture_paths: frozenset[str]
    requires_validated_post_state: bool
    requires_protected_state: bool


def _group(predicate_id: str, checks: Iterable[StateAssertion]) -> StateAssertion:
    items = list(checks)
    return predicate_assertion(
        predicate_id,
        bool(items) and all(item.passed for item in items),
        True,
        [asdict(item) for item in items],
    )


def _field_checks(
    label: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    fields: Iterable[str],
) -> list[StateAssertion]:
    return [
        state_assertion(
            f"{label} {field_name}",
            None if after is None else after.get(field_name),
            None if before is None else before.get(field_name),
        )
        for field_name in fields
    ]


def _numeric(node: Mapping[str, Any] | None, field_name: str) -> int:
    return int((node or {}).get(field_name, "0"))


def _created_indices(observation: Mapping[str, Any], ledger_type: str) -> list[str]:
    lookup = observation.get("validated_lookup") or {}
    meta = lookup.get("meta") or {}
    indices: list[str] = []
    for wrapper in meta.get("AffectedNodes", []):
        created = wrapper.get("CreatedNode")
        if isinstance(created, dict) and created.get("LedgerEntryType") == ledger_type:
            index = created.get("LedgerIndex")
            if isinstance(index, str):
                indices.append(index)
    return indices


def _phase(close_time: int, subscription: int, redemption: int) -> str:
    if close_time <= subscription:
        return "Subscription"
    if close_time < redemption:
        return "Investment"
    return "Redemption"


VAULT_STABLE_FIELDS = (
    "index",
    "Owner",
    "Asset",
    "VaultKind",
    "SubscriptionDate",
    "RedemptionDate",
    "WithdrawalPolicy",
    "Flags",
    "ShareMPTID",
)

BROKER_STABLE_FIELDS = (
    "index",
    "Owner",
    "VaultID",
    "Account",
    "DebtMaximum",
    "CoverRateMinimum",
    "CoverRateLiquidation",
    "ManagementFeeRate",
)


def _lifecycle_evaluator(
    observation: dict[str, Any],
    *,
    phase_name: str,
) -> dict[str, StateAssertion]:
    before = observation["pre_state"]
    after = observation["post_state"]
    context = observation["context"]
    header = observation["ledger_before"]
    actual_phase = _phase(
        int(header["close_time"]),
        int(context["subscription_date"]),
        int(context["redemption_date"]),
    )
    return {
        f"{phase_name.lower()}_phase": _group(
            f"{phase_name.lower()}_phase",
            [state_assertion("vault phase", actual_phase, phase_name)],
        ),
        "vault_relationships_stable": _group(
            "vault_relationships_stable",
            [
                *_field_checks(
                    "Vault",
                    before.get("vault"),
                    after.get("vault"),
                    VAULT_STABLE_FIELDS,
                ),
                state_assertion(
                    "share issuance relationship",
                    (after.get("share_issuance") or {}).get("mpt_issuance_id"),
                    (after.get("vault") or {}).get("ShareMPTID"),
                ),
                *mptoken_assertions(
                    after.get("lender_share"),
                    index=None,
                    account=context["lender"],
                    issuance_id=str((after.get("vault") or {}).get("ShareMPTID", "")),
                ),
            ],
        ),
        "asset_and_balance_delta": _group(
            "asset_and_balance_delta",
            [
                state_assertion(
                    "Vault AssetsTotal delta",
                    _numeric(after.get("vault"), "AssetsTotal")
                    - _numeric(before.get("vault"), "AssetsTotal"),
                    context["asset_delta"],
                ),
                state_assertion(
                    "Vault AssetsAvailable delta",
                    _numeric(after.get("vault"), "AssetsAvailable")
                    - _numeric(before.get("vault"), "AssetsAvailable"),
                    context["asset_delta"],
                ),
                state_assertion(
                    "lender tRLUSD balance delta",
                    int(after["lender_balance"]) - int(before["lender_balance"]),
                    context["balance_delta"],
                ),
            ],
        ),
        "share_delta": _group(
            "share_delta",
            [
                state_assertion(
                    "issuance OutstandingAmount delta",
                    _numeric(after.get("share_issuance"), "OutstandingAmount")
                    - _numeric(before.get("share_issuance"), "OutstandingAmount"),
                    context["share_delta"],
                ),
                state_assertion(
                    "holder MPTAmount delta",
                    _numeric(after.get("lender_share"), "MPTAmount")
                    - _numeric(before.get("lender_share"), "MPTAmount"),
                    context["share_delta"],
                ),
            ],
        ),
        "unrelated_state_protected": _group(
            "unrelated_state_protected",
            [
                state_assertion(
                    "unrelated state",
                    after.get("unrelated_state"),
                    before.get("unrelated_state"),
                )
            ],
        ),
    }


def _negative_phase_evaluator(
    observation: dict[str, Any],
    *,
    phase_name: str,
) -> dict[str, StateAssertion]:
    context = observation["context"]
    header = observation["ledger_before"]
    actual_phase = _phase(
        int(header["close_time"]),
        int(context["subscription_date"]),
        int(context["redemption_date"]),
    )
    phase_id = f"{phase_name.lower()}_phase"
    return {
        phase_id: _group(
            phase_id,
            [state_assertion("vault phase", actual_phase, phase_name)],
        ),
        "complete_protected_state_unchanged": _group(
            "complete_protected_state_unchanged",
            [
                state_assertion(
                    "complete protected state",
                    observation["post_state"].get("protected"),
                    observation["pre_state"].get("protected"),
                )
            ],
        ),
    }


def _evaluate_a(observation: dict[str, Any]) -> dict[str, StateAssertion]:
    before = observation["pre_state"]
    after = observation["post_state"]
    context = observation["context"]
    vault_index = context["vault_index"]
    vault = after.get("vault")
    share_id = None if vault is None else vault.get("ShareMPTID")
    return {
        "unique_created_vault": _group(
            "unique_created_vault",
            [
                state_assertion(
                    "created Vault indices",
                    _created_indices(observation, "Vault"),
                    [vault_index],
                )
            ],
        ),
        "vault_and_owner_mptoken": _group(
            "vault_and_owner_mptoken",
            [
                *vault_create_transition_assertions(
                    before_objects=before["owner_objects"],
                    after_objects=after["owner_objects"],
                    vault_index=vault_index,
                    owner=context["owner"],
                    asset=context["asset"],
                    vault_kind=context["vault_kind"],
                    subscription_date=context["subscription_date"],
                    redemption_date=context["redemption_date"],
                    withdrawal_policy=context["withdrawal_policy"],
                ),
                *vault_assertions(
                    vault,
                    index=vault_index,
                    owner=context["owner"],
                    asset=context["asset"],
                    vault_kind=context["vault_kind"],
                    subscription_date=context["subscription_date"],
                    redemption_date=context["redemption_date"],
                    withdrawal_policy=context["withdrawal_policy"],
                    flags=context["flags"],
                ),
                *mptoken_assertions(
                    after.get("owner_mptoken"),
                    index=None,
                    account=context["owner"],
                    issuance_id="" if share_id is None else str(share_id),
                    flags=0,
                ),
            ],
        ),
        "created_objects_absent_before": _group(
            "created_objects_absent_before",
            [
                state_assertion(
                    "Vault absent before",
                    len(
                        semantic_objects(
                            before["owner_objects"],
                            ledger_entry_type="Vault",
                            index=vault_index,
                        )
                    ),
                    0,
                ),
                state_assertion(
                    "owner MPToken absent before",
                    len(
                        semantic_objects(
                            before["owner_objects"],
                            ledger_entry_type="MPToken",
                            identity={
                                "Account": context["owner"],
                                "MPTokenIssuanceID": share_id,
                            },
                        )
                    ),
                    0,
                ),
            ],
        ),
        "owner_trustline_protected": _group(
            "owner_trustline_protected",
            [
                state_assertion(
                    "owner tRLUSD trust line",
                    after.get("owner_trustline"),
                    before.get("owner_trustline"),
                )
            ],
        ),
        "auxiliary_objects_captured": _group(
            "auxiliary_objects_captured",
            [
                state_assertion(
                    "share issuance type",
                    (after.get("share_issuance") or {}).get("LedgerEntryType"),
                    "MPTokenIssuance",
                ),
                state_assertion(
                    "Vault pseudo AccountRoot type",
                    (after.get("vault_pseudo_account") or {}).get("LedgerEntryType"),
                    "AccountRoot",
                ),
                state_assertion(
                    "Vault pseudo account identity",
                    (after.get("vault_pseudo_account") or {}).get("Account"),
                    (vault or {}).get("Account"),
                ),
            ],
        ),
    }


def _evaluate_b(observation: dict[str, Any]) -> dict[str, StateAssertion]:
    return _lifecycle_evaluator(observation, phase_name="Subscription")


def _evaluate_c(observation: dict[str, Any]) -> dict[str, StateAssertion]:
    return _lifecycle_evaluator(observation, phase_name="Subscription")


def _evaluate_d(observation: dict[str, Any]) -> dict[str, StateAssertion]:
    return _negative_phase_evaluator(observation, phase_name="Investment")


def _evaluate_e(observation: dict[str, Any]) -> dict[str, StateAssertion]:
    return _negative_phase_evaluator(observation, phase_name="Investment")


def _evaluate_f(observation: dict[str, Any]) -> dict[str, StateAssertion]:
    return _lifecycle_evaluator(observation, phase_name="Redemption")


def _evaluate_g(observation: dict[str, Any]) -> dict[str, StateAssertion]:
    return _negative_phase_evaluator(observation, phase_name="Redemption")


def _evaluate_h(observation: dict[str, Any]) -> dict[str, StateAssertion]:
    before = observation["pre_state"]
    after = observation["post_state"]
    context = observation["context"]
    broker_index = context["loan_broker_index"]
    broker = after.get("loan_broker")
    return {
        "unique_created_loan_broker": _group(
            "unique_created_loan_broker",
            [
                state_assertion(
                    "created LoanBroker indices",
                    _created_indices(observation, "LoanBroker"),
                    [broker_index],
                )
            ],
        ),
        "loan_broker_semantics": _group(
            "loan_broker_semantics",
            [
                *loan_broker_create_transition_assertions(
                    before_objects=before["owner_objects"],
                    after_objects=after["owner_objects"],
                    broker_index=broker_index,
                    owner=context["owner"],
                    vault_id=context["vault_id"],
                    debt_maximum=context["debt_maximum"],
                    cover_rate_minimum=context["cover_rate_minimum"],
                    cover_rate_liquidation=context["cover_rate_liquidation"],
                ),
                *loan_broker_assertions(
                    broker,
                    index=broker_index,
                    owner=context["owner"],
                    vault_id=context["vault_id"],
                    debt_maximum=context["debt_maximum"],
                    cover_rate_minimum=context["cover_rate_minimum"],
                    cover_rate_liquidation=context["cover_rate_liquidation"],
                ),
            ],
        ),
        "created_broker_absent_before": _group(
            "created_broker_absent_before",
            [
                state_assertion(
                    "LoanBroker absent before",
                    len(
                        semantic_objects(
                            before["owner_objects"],
                            ledger_entry_type="LoanBroker",
                            index=broker_index,
                        )
                    ),
                    0,
                )
            ],
        ),
        "vault_and_owner_asset_protected": _group(
            "vault_and_owner_asset_protected",
            [
                *_field_checks(
                    "Vault",
                    before.get("vault"),
                    after.get("vault"),
                    VAULT_STABLE_FIELDS,
                ),
                state_assertion(
                    "owner tRLUSD balance",
                    after.get("owner_balance"),
                    before.get("owner_balance"),
                ),
            ],
        ),
        "owner_reserve_effects": _group(
            "owner_reserve_effects",
            [
                state_assertion(
                    "owner OwnerCount delta",
                    int((after.get("owner_account") or {}).get("OwnerCount", 0))
                    - int((before.get("owner_account") or {}).get("OwnerCount", 0)),
                    context["owner_count_delta"],
                )
            ],
        ),
        "pseudo_account_captured": _group(
            "pseudo_account_captured",
            [
                state_assertion(
                    "LoanBroker pseudo AccountRoot type",
                    (after.get("broker_pseudo_account") or {}).get("LedgerEntryType"),
                    "AccountRoot",
                ),
                state_assertion(
                    "LoanBroker pseudo account identity",
                    (after.get("broker_pseudo_account") or {}).get("Account"),
                    (broker or {}).get("Account"),
                ),
            ],
        ),
    }


def _evaluate_i(observation: dict[str, Any]) -> dict[str, StateAssertion]:
    before = observation["pre_state"]
    after = observation["post_state"]
    vault = after.get("vault")
    broker = after.get("loan_broker")
    return {
        "vault_broker_relationship": _group(
            "vault_broker_relationship",
            [
                state_assertion(
                    "LoanBroker VaultID",
                    (broker or {}).get("VaultID"),
                    (vault or {}).get("index"),
                ),
                state_assertion(
                    "LoanBroker Owner",
                    (broker or {}).get("Owner"),
                    (vault or {}).get("Owner"),
                ),
            ],
        ),
        "pseudo_accounts_present_and_distinct": _group(
            "pseudo_accounts_present_and_distinct",
            [
                predicate_assertion(
                    "Vault pseudo account present",
                    isinstance((vault or {}).get("Account"), str)
                    and bool((vault or {}).get("Account")),
                    True,
                    (vault or {}).get("Account"),
                ),
                predicate_assertion(
                    "LoanBroker pseudo account present",
                    isinstance((broker or {}).get("Account"), str)
                    and bool((broker or {}).get("Account")),
                    True,
                    (broker or {}).get("Account"),
                ),
                predicate_assertion(
                    "pseudo accounts distinct",
                    bool((vault or {}).get("Account"))
                    and (vault or {}).get("Account") != (broker or {}).get("Account"),
                    True,
                    {
                        "vault": (vault or {}).get("Account"),
                        "loan_broker": (broker or {}).get("Account"),
                    },
                ),
            ],
        ),
        "vault_configuration_protected": _group(
            "vault_configuration_protected",
            _field_checks(
                "Vault",
                before.get("vault"),
                after.get("vault"),
                VAULT_STABLE_FIELDS,
            ),
        ),
    }


def _evaluate_j(observation: dict[str, Any]) -> dict[str, StateAssertion]:
    before = observation["pre_state"]
    after = observation["post_state"]
    context = observation["context"]
    return {
        "loan_broker_configuration_stable": _group(
            "loan_broker_configuration_stable",
            [
                *_field_checks(
                    "LoanBroker",
                    before.get("loan_broker"),
                    after.get("loan_broker"),
                    BROKER_STABLE_FIELDS,
                ),
                *loan_broker_assertions(
                    after.get("loan_broker"),
                    index=context["loan_broker_index"],
                    owner=context["owner"],
                    vault_id=context["vault_id"],
                    debt_maximum=context["debt_maximum"],
                    cover_rate_minimum=context["cover_rate_minimum"],
                    cover_rate_liquidation=context["cover_rate_liquidation"],
                    cover_available=str(context["cover_delta"]),
                    debt_total="0",
                ),
            ],
        ),
        "cover_and_owner_balance_delta": _group(
            "cover_and_owner_balance_delta",
            cover_transition_assertions(
                before_broker=before.get("loan_broker"),
                after_broker=after.get("loan_broker"),
                broker_index=context["loan_broker_index"],
                owner=context["owner"],
                vault_id=context["vault_id"],
                before_owner_balance=before["owner_balance"],
                after_owner_balance=after["owner_balance"],
                cover_delta=context["cover_delta"],
            ),
        ),
        "broker_holding_delta": _group(
            "broker_holding_delta",
            [
                state_assertion(
                    "broker pseudo-account holding delta",
                    int(after["broker_holding"]) - int(before["broker_holding"]),
                    context["cover_delta"],
                )
            ],
        ),
        "debt_and_vault_protected": _group(
            "debt_and_vault_protected",
            [
                state_assertion(
                    "DebtTotal remains zero",
                    (after.get("loan_broker") or {}).get("DebtTotal", "0"),
                    "0",
                ),
                *_field_checks(
                    "Vault",
                    before.get("vault"),
                    after.get("vault"),
                    VAULT_STABLE_FIELDS,
                ),
                state_assertion(
                    "unrelated state",
                    after.get("unrelated_state"),
                    before.get("unrelated_state"),
                ),
            ],
        ),
    }


def _evaluate_k(observation: dict[str, Any]) -> dict[str, StateAssertion]:
    after = observation["post_state"]
    context = observation["context"]
    matches = semantic_objects(
        after["non_owner_objects"],
        ledger_entry_type="LoanBroker",
        identity={"Owner": context["non_owner"], "VaultID": context["vault_id"]},
    )
    return {
        "matching_loan_broker_absent": _group(
            "matching_loan_broker_absent",
            [state_assertion("matching non-owner LoanBroker count", len(matches), 0)],
        ),
        "complete_protected_state_unchanged": _group(
            "complete_protected_state_unchanged",
            [
                state_assertion(
                    "complete protected state",
                    after.get("protected"),
                    observation["pre_state"].get("protected"),
                )
            ],
        ),
    }


EVALUATORS: dict[str, PredicateEvaluator] = {
    "A": _evaluate_a,
    "B": _evaluate_b,
    "C": _evaluate_c,
    "D": _evaluate_d,
    "E": _evaluate_e,
    "F": _evaluate_f,
    "G": _evaluate_g,
    "H": _evaluate_h,
    "I": _evaluate_i,
    "J": _evaluate_j,
    "K": _evaluate_k,
}


def default_bindings() -> tuple[OracleBinding, ...]:
    return tuple(
        OracleBinding(
            step=step,
            name=STEP_NAMES[step],
            expected_result=EXPECTED_RESULTS[step],
            predicate_ids=FROZEN_PREDICATES[step],
            evaluator=EVALUATORS[step],
            capture_paths=CAPTURE_PATHS,
            requires_validated_post_state=EXPECTED_RESULTS[step].startswith("tes"),
            requires_protected_state=not EXPECTED_RESULTS[step].startswith("tes"),
        )
        for step in ORACLE_ORDER
    )


def preflight(bindings: Iterable[OracleBinding]) -> tuple[OracleBinding, ...]:
    items = tuple(bindings)
    steps = tuple(item.step for item in items)
    errors: list[str] = []
    if steps != ORACLE_ORDER:
        errors.append(f"A-K order mismatch: expected {ORACLE_ORDER}, observed {steps}")
    for item in items:
        if item.step not in FROZEN_PREDICATES:
            errors.append(f"unknown step {item.step}")
            continue
        if item.name != STEP_NAMES[item.step]:
            errors.append(f"{item.step}: name binding mismatch")
        if item.expected_result != EXPECTED_RESULTS[item.step]:
            errors.append(f"{item.step}: expected-result binding mismatch")
        if item.predicate_ids != FROZEN_PREDICATES[item.step]:
            errors.append(f"{item.step}: semantic predicate binding mismatch")
        if not callable(item.evaluator):
            errors.append(f"{item.step}: semantic evaluator missing")
        missing_capture = CAPTURE_PATHS - item.capture_paths
        if missing_capture:
            errors.append(f"{item.step}: missing capture paths {sorted(missing_capture)}")
        positive = item.expected_result.startswith("tes")
        if positive and not item.requires_validated_post_state:
            errors.append(f"{item.step}: positive step lacks validated post-state checking")
        if not positive and not item.requires_protected_state:
            errors.append(f"{item.step}: negative step lacks protected-state checking")
    if errors:
        raise OraclePreflightError("; ".join(errors))
    return items


def evaluate_observation(
    binding: OracleBinding,
    observation: dict[str, Any],
) -> dict[str, Any]:
    missing = CAPTURE_PATHS - observation.keys()
    if missing:
        raise OracleExecutionError(f"{binding.step}: missing observation fields {sorted(missing)}")
    predicates = binding.evaluator(observation)
    observed_predicate_ids = tuple(predicates)
    if observed_predicate_ids != binding.predicate_ids:
        raise OracleExecutionError(
            f"{binding.step}: predicate invocation mismatch; expected "
            f"{binding.predicate_ids}, observed {observed_predicate_ids}"
        )
    assertions = list(predicates.values())
    resolution = resolve_transaction_result(
        submission_response=observation["submission_response"],
        submission_hash_if_any=observation["submission_hash_if_any"],
        validated_lookup=observation["validated_lookup"],
        client_exception=observation["client_exception"],
        transport_or_server_failure=observation["transport_or_server_failure"],
    )
    protected_unchanged = None
    if binding.requires_protected_state:
        protected_present = (
            "protected" in observation["pre_state"] and "protected" in observation["post_state"]
        )
        protected_unchanged = (
            protected_present
            and observation["pre_state"]["protected"] == observation["post_state"]["protected"]
        )
    classification = classify_transaction(
        expected_result=binding.expected_result,
        resolution=resolution,
        before_state=observation["pre_state"],
        after_state=observation["post_state"],
        assertions=assertions,
        state_unchanged=protected_unchanged,
    )
    ledger_after = observation["ledger_after"]
    result = {
        "step": binding.step,
        "name": binding.name,
        "expected_result": binding.expected_result,
        "submission_result": resolution["submission_result"],
        "submission_hash_if_any": resolution["submission_hash_if_any"],
        "client_exception": resolution["client_exception"],
        "validated_lookup_status": resolution["validated_lookup_status"],
        "validated_result_if_any": resolution["validated_result_if_any"],
        "pre_state": observation["pre_state"],
        "post_state": observation["post_state"],
        "state_assertion": classification["state_assertion"],
        "state_assertions": assertions,
        "protected_state_unchanged": protected_unchanged,
        "ledger_index": ledger_after.get("ledger_index"),
        "close_time": ledger_after.get("close_time"),
        "ledger_before": observation["ledger_before"],
        "ledger_after": ledger_after,
        "classification": classification,
        "result_resolution": resolution,
        "synthetic": bool(observation["synthetic"]),
    }
    if not REQUIRED_OUTPUT_FIELDS.issubset(result):
        raise OracleExecutionError(f"{binding.step}: required output metadata missing")
    return result


class OracleSession:
    """Preflighted, ordered, fail-closed execution of A-K observations."""

    def __init__(self, bindings: Iterable[OracleBinding] | None = None) -> None:
        self.bindings = preflight(default_bindings() if bindings is None else bindings)
        self.records: list[dict[str, Any]] = []
        self.stopped = False

    @property
    def next_step(self) -> str | None:
        if self.stopped or len(self.records) == len(self.bindings):
            return None
        return self.bindings[len(self.records)].step

    def record(self, step: str, observation: dict[str, Any]) -> dict[str, Any]:
        if self.stopped:
            raise OracleExecutionError("oracle session already stopped")
        expected_step = self.next_step
        if step != expected_step:
            raise OracleExecutionError(
                f"oracle step order mismatch: expected {expected_step}, observed {step}"
            )
        binding = self.bindings[len(self.records)]
        result = evaluate_observation(binding, observation)
        self.records.append(result)
        if result["classification"]["status"] != "PASS":
            self.stopped = True
        return result

    def run(
        self,
        observations: Mapping[str, Callable[[], dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        for binding in self.bindings:
            if self.stopped:
                break
            provider = observations.get(binding.step)
            if provider is None:
                raise OracleExecutionError(f"{binding.step}: observation provider missing")
            self.record(binding.step, provider())
        return self.records

    def require_complete(self) -> None:
        observed = tuple(record["step"] for record in self.records)
        if observed != ORACLE_ORDER or self.stopped:
            raise OracleExecutionError(
                f"oracle session incomplete: expected {ORACLE_ORDER}, observed {observed}"
            )
