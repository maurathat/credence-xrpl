"""X1a: tRLUSD test-token topology on the local standalone ledger, prepared but not executed.

tRLUSD, a test token, is issued by a local issuer account on the local standalone rippled. This
module:

- lists the unsigned setup transactions, in order, and who must sign each;
- computes the XRP reserve each account needs from the reserves the validated ledger reports;
- checks, from validated ledger snapshots, whether the topology is ready for one provider Payment.

Nothing here signs or submits. Fee, Sequence and LastLedgerSequence are filled in only when a
separately authorized setup run executes these steps. X1a authorizes no ledger write.

Topology:

| Account | Holds | Owned objects |
| --- | --- | --- |
| Issuer | Issues tRLUSD; DefaultRipple set; no TransferRate, RequireAuth or GlobalFreeze | none |
| Agent payment account | tRLUSD trust line; SignerList of ResearchBot + policy signer, quorum 2 | 2 |
| Provider settlement account (one per provider) | tRLUSD trust line | 1 |
| ResearchBot and policy signer | Signer accounts; may be unfunded; sign with their master keys; a funded signer account must hold no RegularKey | none |

Owner: XRPL Integration (X1a, authorized by Maura).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from xrpl.core.addresscodec import is_valid_classic_address

from credence.xrpl_adapter.multisign import (
    REQUIRED_QUORUM,
    SIGNER_WEIGHT,
    AccountSnapshot,
    MasterKeyPolicy,
    MultisignPolicy,
    MultisignRejected,
    multisign_fee_drops,
    trlusd_value,
    verify_account_keys,
    verify_signer_list,
)
from credence.xrpl_adapter.preparation import TRLUSD_WIRE_CODE
from credence.xrpl_adapter.rippled import TrustLineSnapshot, ValidatedLedgerIdentity

ASF_DISABLE_MASTER = 4
ASF_DEFAULT_RIPPLE = 8
TF_SET_NO_RIPPLE = 0x00020000
LSF_REQUIRE_AUTH = 0x00040000
LSF_GLOBAL_FREEZE = 0x00400000
LSF_DEFAULT_RIPPLE = 0x00800000
TRANSFER_RATE_NONE = (0, 1_000_000_000)

AGENT_OWNED_OBJECTS = 2  # the tRLUSD trust line and the SignerList
PROVIDER_OWNED_OBJECTS = 1  # the tRLUSD trust line


@dataclass(frozen=True)
class TrlusdTopology:
    issuer: str
    agent_account: str
    researchbot_signer: str
    policy_signer: str
    provider_accounts: Mapping[str, str]
    master_key: MasterKeyPolicy
    network_id: int
    trust_limit_micro_units: int
    agent_funding_micro_units: int

    def __post_init__(self) -> None:
        accounts = [
            self.issuer,
            self.agent_account,
            self.researchbot_signer,
            self.policy_signer,
            *self.provider_accounts.values(),
        ]
        if not all(is_valid_classic_address(account) for account in accounts):
            raise ValueError("every topology account must be a valid XRPL classic address")
        if len(set(accounts)) != len(accounts):
            raise ValueError("topology accounts must be distinct")
        if not self.provider_accounts:
            raise ValueError("at least one provider settlement account is required")
        if not 0 < self.agent_funding_micro_units <= self.trust_limit_micro_units:
            raise ValueError("funding must be positive and within the trust limit")


@dataclass(frozen=True)
class SetupStep:
    order: int
    signer: str
    purpose: str
    transaction: Mapping[str, Any]


def _trlusd(topology: TrlusdTopology, amount_micro_units: int) -> dict[str, str]:
    return {
        "currency": TRLUSD_WIRE_CODE,
        "issuer": topology.issuer,
        "value": trlusd_value(amount_micro_units),
    }


def setup_transactions(topology: TrlusdTopology) -> tuple[SetupStep, ...]:
    """The unsigned setup steps, in order. Fee, Sequence and LastLedgerSequence are omitted."""
    network = {"NetworkID": topology.network_id}
    steps: list[SetupStep] = [
        SetupStep(
            1,
            "issuer",
            "Let tRLUSD ripple between holders through the issuer",
            {
                "TransactionType": "AccountSet",
                "Account": topology.issuer,
                "SetFlag": ASF_DEFAULT_RIPPLE,
                **network,
            },
        ),
        SetupStep(
            2,
            "owner (agent account master key)",
            "Agent payment account trusts the issuer for tRLUSD",
            {
                "TransactionType": "TrustSet",
                "Account": topology.agent_account,
                "LimitAmount": _trlusd(topology, topology.trust_limit_micro_units),
                "Flags": TF_SET_NO_RIPPLE,
                **network,
            },
        ),
    ]
    for provider_id, account in sorted(topology.provider_accounts.items()):
        steps.append(
            SetupStep(
                len(steps) + 1,
                f"provider settlement account ({provider_id})",
                f"Provider settlement account for {provider_id} trusts the issuer for tRLUSD",
                {
                    "TransactionType": "TrustSet",
                    "Account": account,
                    "LimitAmount": _trlusd(topology, topology.trust_limit_micro_units),
                    "Flags": TF_SET_NO_RIPPLE,
                    **network,
                },
            )
        )
    steps.append(
        SetupStep(
            len(steps) + 1,
            "issuer",
            "Issue the tRLUSD test balance to the agent payment account",
            {
                "TransactionType": "Payment",
                "Account": topology.issuer,
                "Destination": topology.agent_account,
                "Amount": _trlusd(topology, topology.agent_funding_micro_units),
                "Flags": 0,
                **network,
            },
        )
    )
    steps.append(
        SetupStep(
            len(steps) + 1,
            "owner (agent account master key)",
            "SignerList: ResearchBot and the Credence policy signer, weight 1 each, quorum 2",
            {
                "TransactionType": "SignerListSet",
                "Account": topology.agent_account,
                "SignerQuorum": REQUIRED_QUORUM,
                "SignerEntries": [
                    {"SignerEntry": {"Account": signer, "SignerWeight": SIGNER_WEIGHT}}
                    for signer in (topology.researchbot_signer, topology.policy_signer)
                ],
                **network,
            },
        )
    )
    if topology.master_key is MasterKeyPolicy.DISABLED:
        steps.append(
            SetupStep(
                len(steps) + 1,
                "owner (agent account master key)",
                "Disable the master key so only the SignerList can authorize",
                {
                    "TransactionType": "AccountSet",
                    "Account": topology.agent_account,
                    "SetFlag": ASF_DISABLE_MASTER,
                    **network,
                },
            )
        )
    return tuple(steps)


def reserve_drops(reserve_base_drops: int, reserve_increment_drops: int, owned: int) -> int:
    """The XRP an account must keep: base reserve plus one increment per owned object."""
    if min(reserve_base_drops, reserve_increment_drops, owned) < 0:
        raise ValueError("reserves and owned-object counts are non-negative")
    return reserve_base_drops + reserve_increment_drops * owned


@dataclass(frozen=True)
class TrustLineState:
    """A holder's tRLUSD trust line to the issuer, from the same validated ledger."""

    exists: bool
    ledger_hash: str
    balance_micro_units: int
    limit_micro_units: int


@dataclass(frozen=True)
class SetupObservation:
    ledger_index: int
    ledger_hash: str
    base_fee_drops: int
    reserve_base_drops: int
    reserve_increment_drops: int
    accounts: Mapping[str, AccountSnapshot]
    trust_lines: Mapping[str, TrustLineState]


def setup_findings(
    topology: TrlusdTopology,
    observation: SetupObservation,
    *,
    payment_amount_micro_units: int,
    maximum_fee_drops: int,
) -> tuple[str, ...]:
    """Problems that block one provider Payment of ``payment_amount_micro_units``. Empty = ready."""
    findings: list[str] = []

    def account(name: str) -> AccountSnapshot | None:
        snapshot = observation.accounts.get(name)
        if snapshot is None or not snapshot.exists:
            findings.append(f"ACCOUNT_MISSING:{name}")
            return None
        if not snapshot.validated or snapshot.ledger_hash != observation.ledger_hash:
            findings.append(f"LEDGER_STATE_INCONSISTENT:{name}")
            return None
        return snapshot

    def line(name: str) -> TrustLineState | None:
        state = observation.trust_lines.get(name)
        if state is None or not state.exists:
            findings.append(f"TRUST_LINE_MISSING:{name}")
            return None
        if state.ledger_hash != observation.ledger_hash:
            findings.append(f"LEDGER_STATE_INCONSISTENT:trust-line:{name}")
            return None
        return state

    issuer = account(topology.issuer)
    if issuer is not None:
        if not issuer.flags & LSF_DEFAULT_RIPPLE:
            findings.append("ISSUER_DEFAULT_RIPPLE_NOT_SET")
        if issuer.flags & LSF_REQUIRE_AUTH:
            findings.append("ISSUER_REQUIRES_AUTHORIZATION")
        if issuer.flags & LSF_GLOBAL_FREEZE:
            findings.append("ISSUER_GLOBAL_FREEZE")
        if issuer.transfer_rate is not None and issuer.transfer_rate not in TRANSFER_RATE_NONE:
            findings.append("ISSUER_TRANSFER_RATE_SET")

    fee = multisign_fee_drops(observation.base_fee_drops, REQUIRED_QUORUM)
    if fee > maximum_fee_drops:
        findings.append("MULTISIGN_FEE_ABOVE_CEILING")

    agent = account(topology.agent_account)
    if agent is not None:
        policy = MultisignPolicy(
            account=topology.agent_account,
            researchbot_account=topology.researchbot_signer,
            policy_signer_account=topology.policy_signer,
            network_id=topology.network_id,
            master_key=topology.master_key,
            maximum_fee_drops=maximum_fee_drops,
            maximum_ledger_window=1,
        )
        for check in (verify_signer_list, verify_account_keys):
            try:
                check(policy, agent)
            except MultisignRejected as exc:
                findings.append(f"AGENT_{exc.code}")
        required = reserve_drops(
            observation.reserve_base_drops,
            observation.reserve_increment_drops,
            max(AGENT_OWNED_OBJECTS, agent.owner_count or 0),
        )
        if (agent.balance_drops or 0) < required + fee:
            findings.append("AGENT_XRP_BELOW_RESERVE_PLUS_FEE")
    agent_line = line(topology.agent_account)
    if agent_line is not None and agent_line.balance_micro_units < payment_amount_micro_units:
        findings.append("AGENT_TRLUSD_INSUFFICIENT")

    for provider_id, provider_account in sorted(topology.provider_accounts.items()):
        provider = account(provider_account)
        if provider is not None:
            required = reserve_drops(
                observation.reserve_base_drops,
                observation.reserve_increment_drops,
                max(PROVIDER_OWNED_OBJECTS, provider.owner_count or 0),
            )
            if (provider.balance_drops or 0) < required:
                findings.append(f"PROVIDER_XRP_BELOW_RESERVE:{provider_id}")
        provider_line = line(provider_account)
        if provider_line is not None and (
            provider_line.limit_micro_units - provider_line.balance_micro_units
            < payment_amount_micro_units
        ):
            findings.append(f"PROVIDER_TRUST_LIMIT_TOO_LOW:{provider_id}")

    for signer in (topology.researchbot_signer, topology.policy_signer):
        snapshot = observation.accounts.get(signer)
        if snapshot is not None and snapshot.exists and snapshot.regular_key is not None:
            findings.append(f"SIGNER_REGULAR_KEY_PRESENT:{signer}")
        if snapshot is not None and snapshot.exists and snapshot.master_key_disabled:
            findings.append(f"SIGNER_MASTER_DISABLED:{signer}")
    return tuple(findings)


class SetupLedgerSource(Protocol):
    """The read-only parts of ``RippledTransport`` used to observe the setup."""

    def validated_ledger_identity(self) -> ValidatedLedgerIdentity: ...

    def account_snapshot(
        self, account: str, identity: ValidatedLedgerIdentity
    ) -> AccountSnapshot: ...

    def trust_line(
        self, account: str, issuer: str, currency_code: str, identity: ValidatedLedgerIdentity
    ) -> TrustLineSnapshot: ...


def observe_setup(source: SetupLedgerSource, topology: TrlusdTopology) -> SetupObservation:
    """Read every topology account and trust line from one validated ledger. Read-only."""
    identity = source.validated_ledger_identity()
    names = [
        topology.issuer,
        topology.agent_account,
        topology.researchbot_signer,
        topology.policy_signer,
        *sorted(topology.provider_accounts.values()),
    ]
    accounts = {name: source.account_snapshot(name, identity) for name in names}
    holders = [topology.agent_account, *sorted(topology.provider_accounts.values())]
    trust_lines: dict[str, TrustLineState] = {}
    for holder in holders:
        snapshot = source.trust_line(holder, topology.issuer, TRLUSD_WIRE_CODE, identity)
        trust_lines[holder] = TrustLineState(
            exists=snapshot.exists,
            ledger_hash=snapshot.ledger_hash,
            balance_micro_units=snapshot.balance_micro_units,
            limit_micro_units=snapshot.limit_micro_units,
        )
    return SetupObservation(
        ledger_index=identity.ledger_index,
        ledger_hash=identity.ledger_hash,
        base_fee_drops=identity.base_fee_drops,
        reserve_base_drops=identity.reserve_base_drops,
        reserve_increment_drops=identity.reserve_increment_drops,
        accounts=accounts,
        trust_lines=trust_lines,
    )
