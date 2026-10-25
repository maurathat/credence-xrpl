"""X1a tRLUSD test-token topology: setup plan, reserves and readiness. Nothing is submitted."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from x1a_rippled import NODE_KEY, FakeRippled, standard_ledger
from x1a_world import (
    AMOUNT,
    ISSUER,
    NETWORK_ID,
    OWNER,
    POLICY,
    PROVIDER_A,
    PROVIDER_B,
    RESEARCHBOT,
)
from xrpl.core.binarycodec import encode

from credence.xrpl_adapter.multisign import LSF_DISABLE_MASTER, MasterKeyPolicy
from credence.xrpl_adapter.preparation import TRLUSD_WIRE_CODE
from credence.xrpl_adapter.rippled import RippledTransport, RippledTransportConfig
from credence.xrpl_adapter.trlusd_setup import (
    ASF_DEFAULT_RIPPLE,
    ASF_DISABLE_MASTER,
    LSF_DEFAULT_RIPPLE,
    LSF_REQUIRE_AUTH,
    TrlusdTopology,
    observe_setup,
    reserve_drops,
    setup_findings,
    setup_transactions,
)

TOPOLOGY = TrlusdTopology(
    issuer=ISSUER.account,
    agent_account=OWNER.account,
    researchbot_signer=RESEARCHBOT.account,
    policy_signer=POLICY.account,
    provider_accounts={"provider-a": PROVIDER_A.account, "provider-b": PROVIDER_B.account},
    master_key=MasterKeyPolicy.OWNER_RETAINED,
    network_id=NETWORK_ID,
    trust_limit_micro_units=1_000_000_000,
    agent_funding_micro_units=100_000_000,
)
MAX_FEE = 100


def test_setup_plan_order_and_signers() -> None:
    steps = setup_transactions(TOPOLOGY)
    kinds = [(step.transaction["TransactionType"], step.transaction["Account"]) for step in steps]
    assert kinds == [
        ("AccountSet", ISSUER.account),
        ("TrustSet", OWNER.account),
        ("TrustSet", PROVIDER_A.account),
        ("TrustSet", PROVIDER_B.account),
        ("Payment", ISSUER.account),
        ("SignerListSet", OWNER.account),
    ]
    assert [step.order for step in steps] == list(range(1, 7))
    assert steps[0].transaction["SetFlag"] == ASF_DEFAULT_RIPPLE
    signer_list = steps[-1].transaction
    assert signer_list["SignerQuorum"] == 2
    assert {e["SignerEntry"]["Account"] for e in signer_list["SignerEntries"]} == {
        RESEARCHBOT.account,
        POLICY.account,
    }
    assert all(e["SignerEntry"]["SignerWeight"] == 1 for e in signer_list["SignerEntries"])
    funding = steps[4].transaction
    assert funding["Amount"] == {
        "currency": TRLUSD_WIRE_CODE,
        "issuer": ISSUER.account,
        "value": "100",
    }
    for step in steps:
        assert step.transaction["NetworkID"] == NETWORK_ID
        assert not {"Fee", "Sequence", "LastLedgerSequence", "TxnSignature"} & set(step.transaction)
        # Every step is a well-formed XRPL transaction once the executor fills in fee and sequence.
        encode({**step.transaction, "Fee": "12", "Sequence": 1, "SigningPubKey": ""})


def test_master_key_disabled_topology_adds_the_disable_step() -> None:
    steps = setup_transactions(replace(TOPOLOGY, master_key=MasterKeyPolicy.DISABLED))
    assert steps[-1].transaction == {
        "TransactionType": "AccountSet",
        "Account": OWNER.account,
        "SetFlag": ASF_DISABLE_MASTER,
        "NetworkID": NETWORK_ID,
    }


def test_topology_refuses_bad_accounts_and_amounts() -> None:
    with pytest.raises(ValueError):
        replace(TOPOLOGY, provider_accounts={"provider-a": OWNER.account})
    with pytest.raises(ValueError):
        replace(TOPOLOGY, provider_accounts={})
    with pytest.raises(ValueError):
        replace(TOPOLOGY, agent_funding_micro_units=TOPOLOGY.trust_limit_micro_units + 1)


def test_reserve_arithmetic() -> None:
    assert reserve_drops(1_000_000, 200_000, 2) == 1_400_000
    assert reserve_drops(10_000_000, 2_000_000, 0) == 10_000_000
    with pytest.raises(ValueError):
        reserve_drops(1, 1, -1)


def ready_ledger(**changes: Any) -> FakeRippled:
    rippled = standard_ledger(**changes)
    rippled.accounts[ISSUER.account] = {
        "Balance": "100000000",
        "Flags": LSF_DEFAULT_RIPPLE,
        "OwnerCount": 0,
        "Sequence": 1,
    }
    for provider in (PROVIDER_A, PROVIDER_B):
        rippled.accounts[provider.account] = {
            "Balance": "20000000",
            "Flags": 0,
            "OwnerCount": 1,
            "Sequence": 1,
        }
        rippled.lines[provider.account] = [
            {
                "account": ISSUER.account,
                "currency": TRLUSD_WIRE_CODE,
                "balance": "0",
                "limit": "1000",
            }
        ]
    rippled.lines[OWNER.account] = [
        {"account": ISSUER.account, "currency": TRLUSD_WIRE_CODE, "balance": "100", "limit": "1000"}
    ]
    return rippled


def findings(rippled: FakeRippled, topology: TrlusdTopology = TOPOLOGY) -> tuple[str, ...]:
    transport = RippledTransport(
        rippled,
        RippledTransportConfig(
            source_id="local-rippled",
            expected_network_id=NETWORK_ID,
            expected_build_version="3.4.0",
            expected_node_public_key=NODE_KEY,
        ),
    )
    observation = observe_setup(transport, topology)
    return setup_findings(
        topology, observation, payment_amount_micro_units=AMOUNT, maximum_fee_drops=MAX_FEE
    )


def test_ready_topology_has_no_findings() -> None:
    assert findings(ready_ledger()) == ()


def test_issuer_configuration_is_checked() -> None:
    rippled = ready_ledger()
    rippled.accounts[ISSUER.account].update(
        {"Flags": LSF_REQUIRE_AUTH, "TransferRate": 1_002_000_000}
    )
    assert set(findings(rippled)) == {
        "ISSUER_DEFAULT_RIPPLE_NOT_SET",
        "ISSUER_REQUIRES_AUTHORIZATION",
        "ISSUER_TRANSFER_RATE_SET",
    }


def test_agent_account_is_checked() -> None:
    rippled = ready_ledger()
    rippled.accounts[OWNER.account].update({"Balance": "1400029", "RegularKey": PROVIDER_A.account})
    rippled.signer_lists[OWNER.account][0]["SignerQuorum"] = 1
    rippled.lines[OWNER.account][0]["balance"] = "1.499999"
    assert set(findings(rippled)) == {
        "AGENT_QUORUM_MISMATCH",
        "AGENT_REGULAR_KEY_PRESENT",
        "AGENT_XRP_BELOW_RESERVE_PLUS_FEE",
        "AGENT_TRLUSD_INSUFFICIENT",
    }
    rippled = ready_ledger()
    rippled.accounts[OWNER.account]["Balance"] = "1400030"
    assert findings(rippled) == ()


def test_master_key_policy_is_checked_in_setup() -> None:
    rippled = ready_ledger()
    rippled.accounts[OWNER.account]["Flags"] = LSF_DISABLE_MASTER
    assert findings(rippled) == ("AGENT_MASTER_KEY_STATE_MISMATCH",)
    disabled = replace(TOPOLOGY, master_key=MasterKeyPolicy.DISABLED)
    assert findings(rippled, disabled) == ()


def test_provider_accounts_and_trust_lines_are_checked() -> None:
    rippled = ready_ledger()
    del rippled.lines[PROVIDER_B.account]
    rippled.lines[PROVIDER_A.account][0].update({"balance": "999", "limit": "1000.4"})
    rippled.accounts[PROVIDER_A.account]["Balance"] = "1199999"
    assert set(findings(rippled)) == {
        "TRUST_LINE_MISSING:" + PROVIDER_B.account,
        "PROVIDER_TRUST_LIMIT_TOO_LOW:provider-a",
        "PROVIDER_XRP_BELOW_RESERVE:provider-a",
    }


def test_missing_accounts_and_fee_ceiling_are_reported() -> None:
    rippled = ready_ledger(base_fee=40)
    del rippled.accounts[ISSUER.account]
    assert set(findings(rippled)) == {
        "ACCOUNT_MISSING:" + ISSUER.account,
        "MULTISIGN_FEE_ABOVE_CEILING",
    }


def test_signer_with_disabled_master_is_reported() -> None:
    rippled = ready_ledger()
    rippled.accounts[POLICY.account] = {
        "Balance": "1000000",
        "Flags": LSF_DISABLE_MASTER,
        "OwnerCount": 0,
        "Sequence": 1,
    }
    assert findings(rippled) == ("SIGNER_MASTER_DISABLED:" + POLICY.account,)


def test_observation_is_read_only_and_pinned() -> None:
    rippled = ready_ledger()
    findings(rippled)
    methods = {method for method, _ in rippled.calls}
    assert methods <= {"server_state", "ledger", "account_info", "account_objects", "account_lines"}


@pytest.mark.parametrize("signer", [RESEARCHBOT, POLICY], ids=["researchbot", "policy-signer"])
def test_signer_regular_key_is_a_readiness_finding(signer: Any) -> None:
    rippled = ready_ledger()
    rippled.accounts[signer.account] = {
        "Balance": "1000000",
        "Flags": 0,
        "OwnerCount": 0,
        "RegularKey": PROVIDER_B.account,
        "Sequence": 1,
    }
    assert findings(rippled) == ("SIGNER_REGULAR_KEY_PRESENT:" + signer.account,)
