"""Signature layer: message format, canonical determinism, key registry, the three signed object
types, the C2 fact adapter, and cross-object domain separation. Real PyNaCl, real Kessai digests."""

import ast
import json
from dataclasses import replace
from pathlib import Path

import pytest
from c2_world import ATTESTATION, FACILITY, RECEIPT_TIER2, REQUEST, VerifierWorld
from c3a_keys import (
    AGENT,
    AGENT_REVOKED,
    ATTACKER,
    GATEWAY,
    GATEWAY_REVOKED,
    OTHER_AGENT,
    PROVIDER_A,
    PROVIDER_B,
    PROVIDER_B_REVOKED,
    kid,
    make_test_key,
    registry,
)
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from credence.core import kessai, signatures
from credence.core.objects import (
    SCHEMA_VERSION,
    DeliveryReceiptBody,
    ProviderAttestationBody,
    PurchaseRequest,
    SignedObject,
)
from credence.core.primitives import (
    Ed25519Signature,
    KeyId,
    MicroTrlusd,
    Nonce,
    Purpose,
    QuoteId,
    Sha256Digest,
)
from credence.core.signatures import (
    SIGNATURE_DOMAIN,
    KeyRegistry,
    KeyRegistryError,
    KeyRole,
    KeyStatus,
    RegisteredKey,
    SignatureFailure,
    policy_signature_facts,
    sign,
    signature_message,
    statement_bytes,
    verifier_signature_facts,
    verify_signed,
)
from credence.core.verifier import ProviderSignatureFact as Fact

OTHER = Sha256Digest.from_hex("e" * 64)


def check(obj, signed, role, subject):  # type: ignore[no-untyped-def]
    return verify_signed(obj, signed, registry(), role=role, subject=subject)


# --- message construction ------------------------------------------------------------------


def test_message_is_the_length_prefixed_statement() -> None:
    digest = REQUEST.digest().value
    expected = b"".join(
        len(part).to_bytes(4, "big") + part
        for part in (
            b"credence/signature/v1",
            b"credence.purchase_request",
            b"1",
            digest.encode("ascii"),
        )
    )
    assert signature_message(REQUEST) == expected
    assert SIGNATURE_DOMAIN == "credence/signature/v1"


def test_length_prefixing_prevents_field_boundary_ambiguity() -> None:
    a = statement_bytes("credence.x", "1", "sha256:" + "0" * 64)
    b = statement_bytes("credence.x1", "", "sha256:" + "0" * 64)
    assert a != b


def test_equivalent_representations_sign_the_same_message() -> None:
    canonical = REQUEST.to_canonical()
    reordered = dict(reversed(list(canonical.items())))
    pretty = json.loads(json.dumps(canonical, indent=4, sort_keys=False))
    for value in (canonical, reordered, pretty):
        assert signature_message(PurchaseRequest.from_canonical(value)) == signature_message(
            REQUEST
        )
    raw = json.dumps(reordered, indent=2).encode()
    assert kessai.canonicalize_json_bytes(raw).sha256_hex == REQUEST.digest().hex


def test_signature_is_deterministic_and_independent_of_field_order() -> None:
    first = sign(REQUEST, AGENT)
    rebuilt = PurchaseRequest.from_canonical(dict(reversed(list(REQUEST.to_canonical().items()))))
    assert sign(rebuilt, AGENT) == first


CHANGES = {
    "facility_id": REQUEST.facility_id.__class__("facility-0002"),
    "provider_id": REQUEST.provider_id.__class__("provider-a"),
    "quote_id": QuoteId("quote-0002"),
    "max_total_cost_micro_trlusd": MicroTrlusd(1),
    "prompt_digest": OTHER,
    "nonce": Nonce("1" * 32),
    "expires_at": REQUEST.expires_at.plus_seconds(1),
}


@pytest.mark.parametrize("member", sorted(CHANGES))
def test_changing_any_field_breaks_the_old_signature(member: str) -> None:
    signed = sign(REQUEST, AGENT)
    changed = replace(REQUEST, **{member: CHANGES[member]})
    assert changed.digest() != REQUEST.digest()
    result = check(changed, signed, KeyRole.AGENT, FACILITY.borrower_account.value)
    assert result.failure is SignatureFailure.DIGEST_MISMATCH
    forged_digest = replace(signed, object_digest=changed.digest())
    result = check(changed, forged_digest, KeyRole.AGENT, FACILITY.borrower_account.value)
    assert result.failure is SignatureFailure.BAD_SIGNATURE


def test_purpose_is_bound_by_the_digest() -> None:
    """Purpose has one value under C1A typing, so changing it means changing its bytes."""
    canonical = REQUEST.to_canonical()
    assert canonical["purpose"] == Purpose.INFERENCE.value
    other_bytes = json.dumps({**canonical, "purpose": "training"}).encode()
    other = kessai.canonicalize_json_bytes(other_bytes)
    assert "sha256:" + other.sha256_hex != REQUEST.digest().value


# --- key registry --------------------------------------------------------------------------


def test_key_id_is_the_public_key() -> None:
    key_id = kid(AGENT)
    assert bytes(AGENT.verify_key) == signatures.public_key_bytes(key_id)
    assert key_id.value == "ed25519:" + bytes(AGENT.verify_key).hex()


def test_registry_refuses_duplicates_and_invalid_points() -> None:
    reg = KeyRegistry()
    entry = RegisteredKey(kid(AGENT), KeyRole.AGENT, "x", KeyStatus.CURRENT)
    reg.register(entry)
    with pytest.raises(KeyRegistryError):
        reg.register(entry)
    small_order = KeyId("ed25519:" + "00" * 32)
    with pytest.raises(KeyRegistryError):
        reg.register(RegisteredKey(small_order, KeyRole.AGENT, "x", KeyStatus.CURRENT))
    with pytest.raises(KeyRegistryError):
        reg.register(RegisteredKey(kid(GATEWAY), KeyRole.GATEWAY, "", KeyStatus.CURRENT))


def test_revocation_keeps_the_signature_valid_but_not_current() -> None:
    reg = registry()
    signed = sign(REQUEST, AGENT)
    subject = FACILITY.borrower_account.value
    assert verify_signed(REQUEST, signed, reg, role=KeyRole.AGENT, subject=subject).accepted
    reg.revoke(kid(AGENT))
    result = verify_signed(REQUEST, signed, reg, role=KeyRole.AGENT, subject=subject)
    assert (result.signature_valid, result.key_current) == (True, False)
    assert result.failure is SignatureFailure.REVOKED_KEY


# --- purchase request ----------------------------------------------------------------------

BORROWER = FACILITY.borrower_account.value


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        (AGENT, (True, True)),
        (AGENT_REVOKED, (True, False)),
        (OTHER_AGENT, (False, False)),  # registered agent key for another borrower
        (GATEWAY, (False, False)),  # registered, but not an agent key
        (ATTACKER, (False, False)),  # unknown key
    ],
    ids=["current", "revoked", "other-borrower", "wrong-role", "unknown"],
)
def test_request_facts(key: object, expected: tuple[bool, bool]) -> None:
    facts = policy_signature_facts(REQUEST, sign(REQUEST, key), FACILITY, registry())  # type: ignore[arg-type]
    assert (facts.request_signature_valid, facts.working_key_current) == expected


def test_request_facts_without_a_signature() -> None:
    facts = policy_signature_facts(REQUEST, None, FACILITY, registry())
    assert (facts.request_signature_valid, facts.working_key_current) == (False, False)


@pytest.mark.parametrize("member", ["nonce", "quote_id", "max_total_cost_micro_trlusd"])
def test_request_changed_after_signing(member: str) -> None:
    signed = sign(REQUEST, AGENT)
    changed = replace(REQUEST, **{member: CHANGES[member]})
    facts = policy_signature_facts(changed, signed, FACILITY, registry())
    assert facts.request_signature_valid is False


def test_wrong_key_claimed_for_a_valid_signature() -> None:
    signed = replace(sign(REQUEST, AGENT), key_id=kid(AGENT_REVOKED))
    assert check(REQUEST, signed, KeyRole.AGENT, BORROWER).failure is SignatureFailure.BAD_SIGNATURE


def test_malformed_signature_and_public_key() -> None:
    signed = sign(REQUEST, AGENT)
    garbage = replace(signed, signature=Ed25519Signature("ab" * 64))
    assert check(REQUEST, garbage, KeyRole.AGENT, BORROWER).failure is (
        SignatureFailure.BAD_SIGNATURE
    )
    with pytest.raises(ValueError):
        SignedObject.from_canonical({**signed.to_canonical(), "signature": "zz" * 64})
    with pytest.raises(ValueError):
        SignedObject.from_canonical({**signed.to_canonical(), "key_id": "ed25519:abc"})
    # A registry entry for an invalid point cannot exist; verification also refuses it directly.
    reg = registry()
    reg._keys[KeyId("ed25519:" + "00" * 32)] = RegisteredKey(
        KeyId("ed25519:" + "00" * 32), KeyRole.AGENT, BORROWER, KeyStatus.CURRENT
    )
    bad_key = replace(signed, key_id=KeyId("ed25519:" + "00" * 32))
    result = verify_signed(REQUEST, bad_key, reg, role=KeyRole.AGENT, subject=BORROWER)
    assert result.failure is SignatureFailure.INVALID_PUBLIC_KEY


# --- gateway receipt -----------------------------------------------------------------------

RECEIPT = VerifierWorld().wired().receipt
RECEIPT_CHANGES = {
    "purchase_request_digest": OTHER,
    "execution_profile_digest": OTHER,
    "response_digest": OTHER,
    "input_token_count": RECEIPT.input_token_count + 1,
    "output_token_count": RECEIPT.output_token_count + 1,
    "price_micro_trlusd": RECEIPT.price_micro_trlusd + MicroTrlusd(1),
    "nonce": Nonce("2" * 32),
    "previous_receipt_digest": OTHER,
}


def gateway_fact(receipt: DeliveryReceiptBody, signed: SignedObject | None) -> bool:
    facts = verifier_signature_facts(receipt, signed, None, None, registry())
    return facts.gateway_signature_valid


def test_valid_gateway_receipt() -> None:
    assert gateway_fact(RECEIPT, sign(RECEIPT, GATEWAY)) is True


@pytest.mark.parametrize("member", sorted(RECEIPT_CHANGES))
def test_receipt_altered_after_gateway_signing(member: str) -> None:
    signed = sign(RECEIPT, GATEWAY)
    altered = replace(RECEIPT, **{member: RECEIPT_CHANGES[member]})
    assert gateway_fact(altered, signed) is False
    assert gateway_fact(altered, replace(signed, object_digest=altered.digest())) is False


@pytest.mark.parametrize("key", [PROVIDER_B, AGENT, GATEWAY_REVOKED, ATTACKER])
def test_receipt_signed_by_a_non_current_gateway_key(key: object) -> None:
    assert gateway_fact(RECEIPT, sign(RECEIPT, key)) is False  # type: ignore[arg-type]


def test_missing_gateway_signature() -> None:
    assert gateway_fact(RECEIPT, None) is False


# --- provider attestation ------------------------------------------------------------------


def provider_fact(attestation: ProviderAttestationBody | None, signed: SignedObject | None) -> Fact:
    facts = verifier_signature_facts(RECEIPT, None, attestation, signed, registry())
    return facts.provider_signature


def test_attestation_binds_exactly_the_frozen_five_members() -> None:
    assert ProviderAttestationBody.MEMBERS == (
        "purchase_request_digest",
        "execution_profile_digest",
        "response_digest",
        "input_token_count",
        "output_token_count",
    )


def test_provider_fact_outcomes() -> None:
    assert provider_fact(ATTESTATION, sign(ATTESTATION, PROVIDER_B)) is Fact.VALID
    assert provider_fact(None, None) is Fact.NOT_PRESENT
    assert provider_fact(ATTESTATION, None) is Fact.NOT_PRESENT
    assert provider_fact(None, sign(ATTESTATION, PROVIDER_B)) is Fact.INVALID
    for key in (PROVIDER_A, PROVIDER_B_REVOKED, GATEWAY, ATTACKER):
        assert provider_fact(ATTESTATION, sign(ATTESTATION, key)) is Fact.INVALID


@pytest.mark.parametrize(
    "member",
    [
        "purchase_request_digest",
        "execution_profile_digest",
        "response_digest",
        "input_token_count",
        "output_token_count",
    ],
)
def test_attestation_altered_after_provider_signing(member: str) -> None:
    signed = sign(ATTESTATION, PROVIDER_B)
    value = OTHER if member.endswith("digest") else getattr(ATTESTATION, member) + 1
    altered = replace(ATTESTATION, **{member: value})
    assert provider_fact(altered, signed) is Fact.INVALID


# --- domain separation ---------------------------------------------------------------------


def test_signature_for_one_type_never_verifies_as_another_type() -> None:
    """Even for an attacker who reuses a digest string under another object type."""
    digest = ATTESTATION.digest().value
    statement = statement_bytes(ProviderAttestationBody.OBJECT_TYPE, SCHEMA_VERSION, digest)
    signature = PROVIDER_B.sign(statement).signature
    confused = statement_bytes(DeliveryReceiptBody.OBJECT_TYPE, SCHEMA_VERSION, digest)
    with pytest.raises(BadSignatureError):
        VerifyKey(bytes(PROVIDER_B.verify_key)).verify(confused, signature)


def test_attestation_signature_is_not_a_gateway_receipt_signature() -> None:
    attestation_signed = sign(ATTESTATION, PROVIDER_B)
    as_is = verify_signed(
        RECEIPT, attestation_signed, registry(), role=KeyRole.GATEWAY, subject=None
    )
    assert as_is.failure is SignatureFailure.DIGEST_MISMATCH
    retargeted = replace(attestation_signed, object_digest=RECEIPT.digest())
    result = verify_signed(RECEIPT, retargeted, registry(), role=KeyRole.GATEWAY, subject=None)
    assert not result.accepted  # wrong role, and the bytes signed are a different statement
    assert gateway_fact(RECEIPT, retargeted) is False
    # Same key registered as a gateway still cannot replay it: the statement differs by type.
    reg = KeyRegistry([RegisteredKey(kid(PROVIDER_B), KeyRole.GATEWAY, "g", KeyStatus.CURRENT)])
    result = verify_signed(RECEIPT, retargeted, reg, role=KeyRole.GATEWAY, subject=None)
    assert result.failure is SignatureFailure.BAD_SIGNATURE


def test_request_signature_is_not_a_receipt_signature() -> None:
    signed = replace(sign(REQUEST, AGENT), object_digest=RECEIPT_TIER2.digest())
    reg = KeyRegistry([RegisteredKey(kid(AGENT), KeyRole.GATEWAY, "g", KeyStatus.CURRENT)])
    result = verify_signed(RECEIPT_TIER2, signed, reg, role=KeyRole.GATEWAY, subject=None)
    assert result.failure is SignatureFailure.BAD_SIGNATURE


def test_role_and_object_type_must_agree() -> None:
    signed = sign(REQUEST, AGENT)
    result = verify_signed(REQUEST, signed, registry(), role=KeyRole.GATEWAY, subject=None)
    assert result.failure is SignatureFailure.WRONG_OBJECT_TYPE


# --- no XRPL conflation --------------------------------------------------------------------


def test_signature_module_has_no_xrpl_or_storage_dependency() -> None:
    path = Path(signatures.__file__)
    imported = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"xrpl", "sqlite3", "socket", "urllib", "http", "time", "datetime"}
    assert "authorized_transaction_digest" not in path.read_text()


def test_test_keys_are_distinct_per_label() -> None:
    assert make_test_key("x") != make_test_key("y")
