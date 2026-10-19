"""Inference Checkpoint 3 offline: the Nebius Tier 1 adapter, its credential boundary, error
mapping and the Tier 1 attack cases, through the real gateway and Core. No network: the adapter
runs over a fake transport returning the documented response shape, and the HTTPS transport is
exercised with urlopen patched. No real credential is used anywhere."""

from __future__ import annotations

import io
import socket
import urllib.error
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path

import pytest
from inference_world import GATEWAY_KEY, Scenario, make_purchase
from market_world import (
    GENERAL,
    GENERAL_FACILITY,
    NEBIUS_MODEL,
    NEBIUS_PROFILE,
    NEBIUS_QUOTE,
    NEBIUS_SERVICE,
    market_world,
)

from credence.core.objects import ProviderAttestationBody
from credence.core.primitives import EvidenceTier, ReasonCode
from credence.core.signatures import sign
from credence.core.verifier import ProviderSignatureFact, VerifierInputError
from credence.inference import nebius
from credence.inference.gateway import (
    DeliveryRejected,
    DeliveryVerified,
    GatewayOutcome,
    ProviderFailed,
    ProviderFailureKind,
)
from credence.inference.nebius import (
    NEBIUS_PROVIDER_ID,
    HttpsNebiusTransport,
    NebiusExecutionError,
    NebiusFailure,
    NebiusUnavailableError,
)
from credence.inference.payload import payload_digest
from credence.inference.provider import ProviderAttestation, ProviderResult

FAKE_KEY = "not-a-real-key-offline-tests"
SCENARIO = Scenario(GENERAL_FACILITY, NEBIUS_QUOTE, NEBIUS_SERVICE, NEBIUS_PROFILE, GENERAL)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    (tmp_path / "a").mkdir()
    yield


def run(tmp_path: Path, reply: Mapping[str, object] | None = None) -> GatewayOutcome:
    world = market_world(reply)
    return world.gateway(tmp_path, world.nebius).purchase(make_purchase(SCENARIO))


def verified(tmp_path: Path) -> DeliveryVerified:
    outcome = run(tmp_path)
    assert isinstance(outcome, DeliveryVerified), outcome
    return outcome


def checker(tmp_path: Path):  # type: ignore[no-untyped-def]
    world = market_world()
    return world.gateway(tmp_path / "a", world.nebius)


# --- profile and adapter -------------------------------------------------------------------------


def test_profile_asserts_nothing_nebius_does_not_substantiate() -> None:
    profile = NEBIUS_PROFILE
    assert profile.model_identifier == NEBIUS_MODEL
    for field in (
        "model_artifact_digest",
        "tokenizer_digest",
        "adapter_digest",
        "chat_template_digest",
        "runtime",
        "precision",
        "quantization",
    ):
        assert getattr(profile, field) is None, field
    assert profile.sampling.temperature_milli == 0
    assert profile.sampling.seed is None


def test_request_body_and_usage_mapping(tmp_path: Path) -> None:
    world = market_world()
    outcome = world.gateway(tmp_path, world.nebius).purchase(make_purchase(SCENARIO))
    assert isinstance(outcome, DeliveryVerified)
    path, body = world.nebius_transport.posts[0]
    assert path == "/chat/completions"
    assert body == {
        "model": NEBIUS_MODEL,
        "messages": [{"role": "user", "content": make_purchase(SCENARIO).prompt.decode()}],
        "max_tokens": NEBIUS_QUOTE.max_output_tokens,
        "temperature": 0.0,
        "stream": False,
    }
    receipt = outcome.evidence.receipt
    assert (receipt.input_token_count, receipt.output_token_count) == (21, 6)
    assert world.nebius.last_finish_reason == "stop"


def test_tier1_flow_has_no_attestation_and_no_provider_signature(tmp_path: Path) -> None:
    outcome = verified(tmp_path)
    evidence = outcome.evidence
    assert evidence.receipt.evidence_tier is EvidenceTier.WITNESSED
    assert evidence.provider_attestation is None and evidence.provider_signature is None
    from credence.core.signatures import verifier_signature_facts

    facts = verifier_signature_facts(
        evidence.receipt, evidence.gateway_signature, None, None, market_world().registry()
    )
    assert facts.gateway_signature_valid
    assert facts.provider_signature is ProviderSignatureFact.NOT_PRESENT
    assert outcome.evidence.receipt.provider_id == NEBIUS_PROVIDER_ID


def _reply(**changes: object) -> dict[str, object]:
    from market_world import FakeNebiusTransport

    base = dict(FakeNebiusTransport().post("/chat/completions", {}))
    base.update(changes)
    return base


@pytest.mark.parametrize(
    "reply,failure",
    [
        (_reply(model="other/model"), NebiusFailure.MALFORMED_RESPONSE),
        (_reply(choices=[]), NebiusFailure.MALFORMED_RESPONSE),
        (_reply(choices=[{"message": {}}]), NebiusFailure.MALFORMED_RESPONSE),
        (_reply(usage=None), NebiusFailure.MALFORMED_RESPONSE),
        (_reply(usage={"prompt_tokens": 1}), NebiusFailure.MALFORMED_RESPONSE),
        (
            _reply(usage={"prompt_tokens": -1, "completion_tokens": 1}),
            NebiusFailure.MALFORMED_RESPONSE,
        ),
        (
            _reply(
                usage={
                    "prompt_tokens": 9,
                    "completion_tokens": 1,
                    "prompt_tokens_details": {"cached_tokens": 4},
                }
            ),
            NebiusFailure.UNSUPPORTED_USAGE,
        ),
        (
            _reply(
                usage={
                    "prompt_tokens": 9,
                    "completion_tokens": 5,
                    "completion_tokens_details": {"reasoning_tokens": 2},
                }
            ),
            NebiusFailure.UNSUPPORTED_USAGE,
        ),
    ],
    ids=[
        "other-model",
        "no-choices",
        "no-content",
        "no-usage",
        "no-output-count",
        "negative",
        "cached",
        "reasoning",
    ],
)
def test_unusable_replies_are_execution_errors(
    tmp_path: Path, reply: Mapping[str, object], failure: NebiusFailure
) -> None:
    world = market_world(reply)
    purchase = make_purchase(SCENARIO)
    from credence.inference.provider import ProviderRequest

    with pytest.raises(NebiusExecutionError) as exc:
        world.nebius.execute(
            ProviderRequest(
                purchase.request, purchase.execution_profile, purchase.quote, purchase.prompt
            )
        )
    assert exc.value.failure is failure
    outcome = run(tmp_path, reply)
    assert isinstance(outcome, ProviderFailed)
    assert outcome.kind is ProviderFailureKind.PROVIDER_EXECUTION_ERROR


def test_zero_cached_and_reasoning_details_are_accepted(tmp_path: Path) -> None:
    usage = {
        "prompt_tokens": 21,
        "completion_tokens": 6,
        "total_tokens": 27,
        "prompt_tokens_details": {"cached_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 0},
    }
    assert isinstance(run(tmp_path, _reply(usage=usage)), DeliveryVerified)


# --- credential boundary and transport --------------------------------------------------------------


def test_missing_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(nebius.API_KEY_ENV, raising=False)
    with pytest.raises(NebiusUnavailableError) as exc:
        HttpsNebiusTransport.from_environment()
    assert exc.value.failure is NebiusFailure.CREDENTIAL_MISSING


def test_key_is_never_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(nebius.API_KEY_ENV, FAKE_KEY)
    transport = HttpsNebiusTransport.from_environment()
    assert FAKE_KEY not in repr(transport) and FAKE_KEY not in str(vars(transport).keys())


@pytest.mark.parametrize(
    "url",
    [
        "http://api.tokenfactory.nebius.com/v1",
        "https://example.com/v1",
        "https://api.studio.nebius.com/v1",
    ],
)
def test_transport_only_talks_to_the_official_https_host(url: str) -> None:
    with pytest.raises(ValueError, match="only"):
        HttpsNebiusTransport(FAKE_KEY, base_url=url)


@pytest.mark.parametrize(
    "status,failure,unavailable",
    [
        (401, NebiusFailure.AUTHENTICATION, True),
        (403, NebiusFailure.AUTHENTICATION, True),
        (402, NebiusFailure.PAYMENT_REQUIRED, True),
        (404, NebiusFailure.MODEL_UNAVAILABLE, True),
        (429, NebiusFailure.RATE_LIMITED, True),
        (503, NebiusFailure.SERVICE_UNAVAILABLE, True),
        (529, NebiusFailure.SERVICE_UNAVAILABLE, True),
        (500, NebiusFailure.SERVER_ERROR, False),
        (502, NebiusFailure.SERVER_ERROR, False),
        (400, NebiusFailure.REQUEST_REJECTED, False),
        (422, NebiusFailure.REQUEST_REJECTED, False),
    ],
)
def test_http_status_mapping_without_leaking_the_key(
    monkeypatch: pytest.MonkeyPatch, status: int, failure: NebiusFailure, unavailable: bool
) -> None:
    def fail(request: object, timeout: float) -> object:
        body = io.BytesIO(f'{{"detail": "echo {FAKE_KEY}"}}'.encode())
        raise urllib.error.HTTPError(
            "https://api.tokenfactory.nebius.com/v1/x", status, "x", {}, body
        )  # type: ignore[arg-type]

    monkeypatch.setattr(nebius, "_open", fail)
    transport = HttpsNebiusTransport(FAKE_KEY)
    expected = NebiusUnavailableError if unavailable else NebiusExecutionError
    with pytest.raises(expected) as exc:
        transport.post("/chat/completions", {})
    assert exc.value.failure is failure
    assert FAKE_KEY not in str(exc.value) and exc.value.__cause__ is None


@pytest.mark.parametrize(
    "reason,failure",
    [
        (TimeoutError(), NebiusFailure.TIMEOUT),
        (ConnectionRefusedError(), NebiusFailure.UNREACHABLE),
    ],
)
def test_network_failures(
    monkeypatch: pytest.MonkeyPatch, reason: Exception, failure: NebiusFailure
) -> None:
    def fail(request: object, timeout: float) -> object:
        raise urllib.error.URLError(reason)

    monkeypatch.setattr(nebius, "_open", fail)
    with pytest.raises((NebiusUnavailableError, NebiusExecutionError)) as exc:
        HttpsNebiusTransport(FAKE_KEY).get("/models")
    assert exc.value.failure is failure  # type: ignore[union-attr]


def test_redirects_are_refused() -> None:
    handler = nebius._RefuseRedirects()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://example.com/") is None


def test_authorization_header_is_sent_only_in_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    class Reply(io.BytesIO):
        status = 200

        def __enter__(self) -> Reply:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def capture(request: nebius.urllib.request.Request, timeout: float) -> Reply:
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        return Reply(b'{"object": "list", "data": []}')

    monkeypatch.setattr(nebius, "_open", capture)
    transport = HttpsNebiusTransport(FAKE_KEY)
    assert transport.get("/models") == {"object": "list", "data": []}
    assert transport.last_status == 200
    assert seen == {
        "url": "https://api.tokenfactory.nebius.com/v1/models",
        "auth": f"Bearer {FAKE_KEY}",
    }


# --- Tier 1 attack cases (A-G) through the real Core -------------------------------------------------


def test_a_attestation_inserted_into_a_tier1_result(tmp_path: Path) -> None:
    """At the provider boundary the gateway refuses it; given to the verifier, Core refuses the
    inconsistent input."""
    body = ProviderAttestationBody(
        payload_digest(b"x"), NEBIUS_PROFILE.digest(), payload_digest(b"y"), 1, 1
    )

    class Inserting:
        attestation_capability = market_world().nebius.attestation_capability
        provider_id = NEBIUS_PROVIDER_ID

        def execute(self, request):  # type: ignore[no-untyped-def]
            result: ProviderResult = market_world().nebius.execute(request)
            return replace(result, attestation=ProviderAttestation(body, None))

    world = market_world()
    outcome = world.gateway(tmp_path, Inserting()).purchase(make_purchase(SCENARIO))  # type: ignore[arg-type]
    assert isinstance(outcome, ProviderFailed)
    assert outcome.kind is ProviderFailureKind.PROVIDER_MALFORMED_RESPONSE

    evidence = verified(tmp_path / "a").evidence
    with pytest.raises(VerifierInputError):
        checker(tmp_path).verify_delivery(
            replace(evidence, provider_attestation=body), b"Synthetic Nebius-shaped reply."
        )


def test_b_tier1_receipt_presented_as_tier2(tmp_path: Path) -> None:
    outcome = verified(tmp_path)
    evidence = outcome.evidence
    body = ProviderAttestationBody(
        evidence.receipt.purchase_request_digest,
        evidence.receipt.execution_profile_digest,
        evidence.receipt.response_digest,
        evidence.receipt.input_token_count,
        evidence.receipt.output_token_count,
    )
    as_tier2 = replace(
        evidence.receipt,
        evidence_tier=EvidenceTier.BOUND_AND_SIGNED,
        provider_attestation_digest=body.digest(),
    )
    forged = replace(
        evidence,
        receipt=as_tier2,
        gateway_signature=sign(as_tier2, GATEWAY_KEY),
        provider_attestation=body,
        provider_signature=None,
    )
    decision = checker(tmp_path).verify_delivery(forged, outcome.response)
    assert decision.record.reason_code is ReasonCode.RECEIPT_SIG_INVALID


def test_c_swapped_response_bytes(tmp_path: Path) -> None:
    outcome = verified(tmp_path)
    decision = checker(tmp_path).verify_delivery(outcome.evidence, b"Synthetic swapped bytes.")
    assert decision.record.reason_code is ReasonCode.RESPONSE_DIGEST_MISMATCH


def test_d_wrong_profile_digest(tmp_path: Path) -> None:
    other = replace(NEBIUS_PROFILE, context_min_tokens=4096).digest()

    class WrongProfile:
        attestation_capability = market_world().nebius.attestation_capability
        provider_id = NEBIUS_PROVIDER_ID

        def execute(self, request):  # type: ignore[no-untyped-def]
            return replace(market_world().nebius.execute(request), execution_profile_digest=other)

    outcome = market_world().gateway(tmp_path, WrongProfile()).purchase(make_purchase(SCENARIO))  # type: ignore[arg-type]
    assert isinstance(outcome, DeliveryRejected)
    assert outcome.decision.record.reason_code is ReasonCode.BINDING_MISMATCH


def test_e_token_counts_above_quote_caps(tmp_path: Path) -> None:
    usage = {"prompt_tokens": 21, "completion_tokens": NEBIUS_QUOTE.max_output_tokens + 1}
    outcome = run(tmp_path, _reply(usage=usage))
    assert isinstance(outcome, DeliveryRejected)
    assert outcome.decision.record.reason_code is ReasonCode.TOKEN_COUNT_EXCEEDS_QUOTE


def test_f_altered_billed_amount(tmp_path: Path) -> None:
    outcome = verified(tmp_path)
    evidence = outcome.evidence
    altered = replace(
        evidence.receipt,
        price_micro_trlusd=evidence.receipt.price_micro_trlusd
        + evidence.receipt.price_micro_trlusd.__class__(1),
    )
    resigned = replace(evidence, receipt=altered, gateway_signature=sign(altered, GATEWAY_KEY))
    decision = checker(tmp_path).verify_delivery(resigned, outcome.response)
    assert decision.record.reason_code is ReasonCode.BILLED_AMOUNT_MISMATCH


def test_g_invalid_gateway_signature(tmp_path: Path) -> None:
    from inference_world import ATTACKER_KEY

    outcome = verified(tmp_path)
    evidence = outcome.evidence
    bad = replace(evidence, gateway_signature=sign(evidence.receipt, ATTACKER_KEY))
    decision = checker(tmp_path).verify_delivery(bad, outcome.response)
    assert decision.record.reason_code is ReasonCode.RECEIPT_SIG_INVALID


def test_usage_is_kept_as_numbers_only(tmp_path: Path) -> None:
    world = market_world(
        _reply(
            usage={
                "prompt_tokens": 21,
                "completion_tokens": 6,
                "total_tokens": 27,
                "prompt_tokens_details": {"cached_tokens": 0},
                "note": "dropped",
            }
        )
    )
    assert isinstance(
        world.gateway(tmp_path, world.nebius).purchase(make_purchase(SCENARIO)), DeliveryVerified
    )
    assert world.nebius.last_usage == {
        "prompt_tokens": 21,
        "completion_tokens": 6,
        "total_tokens": 27,
        "prompt_tokens_details": {"cached_tokens": 0},
    }


# --- credential hardening: malformed keys are refused, never stripped ------------------------


@pytest.mark.parametrize(
    "bad",
    [
        FAKE_KEY + " ",
        " " + FAKE_KEY,
        FAKE_KEY + "\n",
        FAKE_KEY + "\r",
        "\t" + FAKE_KEY,
        FAKE_KEY[:5] + " " + FAKE_KEY[5:],
        FAKE_KEY + "\x00",
        FAKE_KEY + "\x7f",
        FAKE_KEY + "\u00a0",
    ],
    ids=[
        "trailing-space",
        "leading-space",
        "newline",
        "cr",
        "tab",
        "inner-space",
        "nul",
        "del",
        "nbsp",
    ],
)
def test_malformed_credential_is_refused_before_any_network(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    def no_network(request: object, timeout: float) -> object:
        raise AssertionError("network attempted with a malformed credential")

    monkeypatch.setattr(nebius, "_open", no_network)
    with pytest.raises(NebiusUnavailableError) as exc:
        HttpsNebiusTransport(bad)  # from_environment delegates here; env vars cannot hold NUL
    assert exc.value.failure is NebiusFailure.CREDENTIAL_MALFORMED
    if "\x00" not in bad:
        monkeypatch.setenv(nebius.API_KEY_ENV, bad)
        with pytest.raises(NebiusUnavailableError):
            HttpsNebiusTransport.from_environment()
    assert bad.strip() not in str(exc.value) and FAKE_KEY not in str(exc.value)


def test_well_formed_credential_is_used_verbatim() -> None:
    assert nebius.credential_well_formed(FAKE_KEY)
    transport = HttpsNebiusTransport(FAKE_KEY)
    assert transport._HttpsNebiusTransport__api_key == FAKE_KEY  # type: ignore[attr-defined]
