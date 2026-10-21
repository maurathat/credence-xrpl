"""Product P0 read model: the offline adapter behind the Credence Markets UI.

Read-only. It owns no money, no keys that sign anything outside a dry run, and no state:

- Providers and procurement presets come from the committed offline market configuration
  (contracts/fixtures/inference_c3/market-selection-deterministic.json), the configuration the
  Integration service runs with. Quotes are configured DEMO prices in micro-tRLUSD (tRLUSD is a
  test token), never Nebius's billed price.
- `evaluate_procurement` is a real Core dry run: Core's `qualify_and_select` evaluates every offer
  with the real policy engine against a temporary, empty reservation ledger. Each candidate is
  signed in-process by an ephemeral sandbox key that is generated for that one evaluation, is
  registered only in that evaluation's registry and is never stored or returned. Nothing is
  reserved and no provider is called.
- Purchase lifecycles, accounting exposure and the offline receipts come from recorded offline
  `IntegrationService` runs (contracts/fixtures/api_p0/offline-operations.json). Purchases are not
  submitted to any backend: the accepted integration (I1g/I1h) is not connected.
- Receipt evidence is re-checked on every request with Core's own digest and signature functions,
  against the registry recorded with that evidence.
- XRPL: no settlement is implemented. Every purchase's real payment state is `not_submitted`; the
  other settlement states exist only as labelled UI fixtures.

Accounting settlement is not XRPL settlement.

Owner: API/UI.
"""

from __future__ import annotations

import json
import secrets
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nacl.signing import SigningKey

from credence.core.billing import max_total_cost_value
from credence.core.objects import (
    DeliveryReceiptBody,
    ExecutionProfile,
    Facility,
    ProcurementClass,
    ProviderAttestationBody,
    PurchaseRequest,
    Quote,
    ServiceClass,
    SignedObject,
)
from credence.core.primitives import (
    CountryCode,
    DeliveryMode,
    EvidenceTier,
    FacilityId,
    KeyId,
    MicroTrlusd,
    Nonce,
    ProviderId,
    Purpose,
    ReasonCode,
    Timestamp,
)
from credence.core.procurement import meets_tier, required_tier, structural_failures
from credence.core.reservation_ledger import ReservationLedger
from credence.core.signatures import (
    KeyRegistry,
    KeyRole,
    KeyStatus,
    RegisteredKey,
    key_id_for,
    sign,
    verify_signed,
)
from credence.inference.gateway import Purchase
from credence.inference.market import Qualification, qualify_and_select
from credence.inference.payload import payload_digest
from credence.integration.journal import OperationState

from . import explanations as text

JSON = dict[str, Any]

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "contracts" / "fixtures"
MARKET_CONFIGURATION = FIXTURES / "inference_c3" / "market-selection-deterministic.json"
LIVE_MARKET = FIXTURES / "inference_c3" / "qwen-market-live-observation.json"
NEBIUS_CATALOG = FIXTURES / "inference_c3" / "nebius-model-Qwen_Qwen3-30B-A3B-Instruct-2507.json"
OFFLINE_OPERATIONS = FIXTURES / "api_p0" / "offline-operations.json"
XRPL_UI_STATES = FIXTURES / "api_p0" / "xrpl-settlement-ui-states.json"

BASELINE = "a3ec0a31f262e325061761e72c3b11246e2a400d"
NEBIUS = "nebius-token-factory"
LOCAL = "local-qwen-provider"
DRY_RUN_FACILITY = "facility-procurement-dry-run"
DRY_RUN_TTL_SECONDS = 600
ROUTING_RULE = (
    "Lowest maximum cost among offers Core accepted, then provider ID, then quote ID. "
    "Configured demo quotes; there is no auction or bidding."
)

PROVIDER_PROFILES: dict[str, JSON] = {
    NEBIUS: {
        "display_name": "Nebius Token Factory",
        "role": "Provider A",
        "relationship": "Credence calls Nebius Token Factory's public API and pays Nebius "
        "through normal billing. No partnership is implied; Nebius does not sign or attest "
        "anything Credence records.",
        "evidence_capability": "Tier 1 (Witnessed): Credence's gateway witnesses the response "
        "and signs the receipt.",
    },
    LOCAL: {
        "display_name": "Local Qwen (operator machine)",
        "role": "Provider B",
        "relationship": "A local model run by the Credence operator behind a signing provider. "
        "Its execution profile binds the GGUF artifact digest.",
        "evidence_capability": "Tier 2 (Bound and signed): the provider signs what it says it "
        "served; the gateway also signs the receipt.",
    },
}


class ProductError(ValueError):
    """A request the read model cannot answer. `code` is stable; `status` is the HTTP status."""

    def __init__(self, code: str, detail: str, status: int = 422) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.status = status


def utc_now() -> Timestamp:
    return Timestamp(datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))


def _load(path: Path) -> JSON:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _tier(tier: EvidenceTier) -> JSON:
    name, description = text.TIER_TEXT[tier]
    return {"tier": tier.value, "name": name, "description": description}


def _registry(entries: Sequence[Mapping[str, str]]) -> KeyRegistry:
    return KeyRegistry(
        RegisteredKey(
            KeyId(e["key_id"]),
            KeyRole(e["role"]),
            e["subject"],
            KeyStatus(e.get("status", "current")),
        )
        for e in entries
    )


@dataclass(frozen=True)
class Offer:
    quote: Quote
    service_class: ServiceClass
    execution_profile: ExecutionProfile


@dataclass(frozen=True)
class ProcurementForm:
    """What an agent or operator asks for. Every field becomes a Core object field."""

    model_family: str
    prompt: str
    max_total_cost_micro_trlusd: int | None  # None: each offer's own quote maximum
    minimum_evidence_tier: int
    allowed_providers: tuple[str, ...]
    allowed_precisions: tuple[str, ...] | None
    allowed_quantizations: tuple[str, ...] | None
    allowed_jurisdictions: tuple[str, ...]
    allowed_data_residencies: tuple[str, ...]
    facility_limit_micro_trlusd: int
    facility_drawn_micro_trlusd: int


def _form_record(form: ProcurementForm) -> JSON:
    return {
        "model_family": form.model_family,
        "prompt": form.prompt,
        "max_total_cost_micro_trlusd": form.max_total_cost_micro_trlusd,
        "minimum_evidence_tier": form.minimum_evidence_tier,
        "allowed_providers": list(form.allowed_providers),
        "allowed_precisions": None
        if form.allowed_precisions is None
        else list(form.allowed_precisions),
        "allowed_quantizations": None
        if form.allowed_quantizations is None
        else list(form.allowed_quantizations),
        "allowed_jurisdictions": list(form.allowed_jurisdictions),
        "allowed_data_residencies": list(form.allowed_data_residencies),
        "facility_limit_micro_trlusd": form.facility_limit_micro_trlusd,
        "facility_drawn_micro_trlusd": form.facility_drawn_micro_trlusd,
    }


def _sorted(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


def procurement_class_for(form: ProcurementForm) -> ProcurementClass:
    """The Core ProcurementClass a form describes (the dry run and the I2a purchase path use this
    one mapping). Raises ValueError or TypeError on an invalid combination."""
    return ProcurementClass(
        model_family=form.model_family,
        allowed_precisions=None
        if form.allowed_precisions is None
        else _sorted(form.allowed_precisions),
        allowed_quantizations=None
        if form.allowed_quantizations is None
        else _sorted(form.allowed_quantizations),
        minimum_evidence_tier=EvidenceTier(form.minimum_evidence_tier),
        delivery_mode=DeliveryMode.INTERACTIVE,
        allowed_jurisdictions=tuple(CountryCode(c) for c in _sorted(form.allowed_jurisdictions)),
        allowed_data_residencies=tuple(
            CountryCode(c) for c in _sorted(form.allowed_data_residencies)
        ),
        allowed_providers=tuple(ProviderId(p) for p in _sorted(form.allowed_providers)),
    )


def receipt_checks(
    receipt: DeliveryReceiptBody,
    gateway: SignedObject,
    attestation: ProviderAttestationBody | None,
    provider_signed: SignedObject | None,
    request: PurchaseRequest,
    profile: ExecutionProfile,
    registry: KeyRegistry,
    recorded_digest: str,
    response: bytes | None,
    response_note: str,
) -> list[JSON]:
    """Recompute a receipt's evidence checks with Core's digest and signature functions."""
    checks: list[JSON] = []

    def check(name: str, passed: bool | None, detail: str) -> None:
        checks.append(
            {
                "check": name,
                "result": "not_applicable" if passed is None else "pass" if passed else "fail",
                "detail": detail,
            }
        )

    digest = receipt.digest().value
    check(
        "receipt_digest",
        digest == recorded_digest,
        f"recomputed {digest} (Kessai serializer: JCS (RFC 8785) + NFC)",
    )
    gateway_result = verify_signed(receipt, gateway, registry, role=KeyRole.GATEWAY, subject=None)
    check(
        "gateway_signature",
        gateway_result.accepted,
        f"Ed25519 by {gateway.key_id.value}"
        + ("" if gateway_result.failure is None else f": {gateway_result.failure.value}"),
    )
    tier = receipt.evidence_tier
    if tier is EvidenceTier.BOUND_AND_SIGNED:
        if attestation is None or provider_signed is None:
            check("provider_signature", False, "Tier 2 receipt without a provider attestation")
        else:
            result = verify_signed(
                attestation,
                provider_signed,
                registry,
                role=KeyRole.PROVIDER,
                subject=receipt.provider_id.value,
            )
            check(
                "provider_signature",
                result.accepted,
                f"Ed25519 by {provider_signed.key_id.value} for {receipt.provider_id.value}"
                + ("" if result.failure is None else f": {result.failure.value}"),
            )
            check(
                "attestation_binding",
                receipt.provider_attestation_digest == attestation.digest()
                and attestation.purchase_request_digest == receipt.purchase_request_digest
                and attestation.execution_profile_digest == receipt.execution_profile_digest
                and attestation.response_digest == receipt.response_digest
                and attestation.input_token_count == receipt.input_token_count
                and attestation.output_token_count == receipt.output_token_count,
                "attestation digest, request, profile, response and token counts equal "
                "the receipt's",
            )
    else:
        check(
            "provider_signature",
            None,
            "Tier 1: the provider signs nothing; the gateway witnessed the response",
        )
    check(
        "request_binding",
        receipt.purchase_request_digest == request.digest(),
        "receipt names the digest of the approved purchase request",
    )
    check(
        "profile_binding",
        receipt.execution_profile_digest == profile.digest(),
        "receipt names the digest of the quoted execution profile",
    )
    check(
        "response_digest",
        None if response is None else payload_digest(response) == receipt.response_digest,
        response_note,
    )
    return checks


class ProductReadModel:
    def __init__(self, clock: Callable[[], Timestamp] = utc_now) -> None:
        self._clock = clock
        self._market = _load(MARKET_CONFIGURATION)
        self._live = _load(LIVE_MARKET)
        self._catalog = _load(NEBIUS_CATALOG)
        self._offline = _load(OFFLINE_OPERATIONS)
        self._xrpl_states = _load(XRPL_UI_STATES)
        objects = self._market["objects"]
        self._offers: dict[str, Offer] = {}
        for prefix, provider in (("nebius", NEBIUS), ("local", LOCAL)):
            offer = Offer(
                Quote.from_canonical(objects[f"{prefix}_quote"]),
                ServiceClass.from_canonical(objects[f"{prefix}_service_class"]),
                ExecutionProfile.from_canonical(objects[f"{prefix}_execution_profile"]),
            )
            assert offer.quote.provider_id.value == provider
            self._offers[provider] = offer
        self._presets: dict[str, tuple[ProcurementClass, Facility]] = {
            "scenario-a-general": (
                ProcurementClass.from_canonical(objects["general_procurement_class"]),
                Facility.from_canonical(objects["general_facility"]),
            ),
            "scenario-b-controlled": (
                ProcurementClass.from_canonical(objects["controlled_procurement_class"]),
                Facility.from_canonical(objects["controlled_facility"]),
            ),
        }
        self._runs: dict[str, JSON] = {
            run["operation"]["operation_id"]: run for run in self._offline["runs"]
        }

    # --- health ----------------------------------------------------------------------------

    def health(self) -> JSON:
        return {
            "mode": "offline",
            "baseline": BASELINE,
            "core_dry_run": True,
            "live_purchases": False,
            "ledger": {
                "connected": False,
                "network": None,
                "note": "No ledger connection. XRPL settlement is not implemented.",
            },
            "currency": {
                "code": "tRLUSD",
                "unit": "micro-tRLUSD",
                "micro_units_per_token": 1_000_000,
                "note": "tRLUSD is a test token.",
            },
        }

    # --- listProviders ---------------------------------------------------------------------

    def list_providers(self) -> list[JSON]:
        return [self._provider(provider) for provider in (NEBIUS, LOCAL)]

    def _provider(self, provider: str) -> JSON:
        offer = self._offers[provider]
        profile, service, quote = offer.execution_profile, offer.service_class, offer.quote
        profile_fields = profile.to_canonical()
        asserted = {
            name: profile_fields[name]
            for name in (
                "model_identifier",
                "model_artifact_digest",
                "tokenizer_digest",
                "adapter_digest",
                "chat_template_digest",
                "runtime",
                "precision",
                "quantization",
            )
        }
        record: JSON = {
            "provider_id": provider,
            **PROVIDER_PROFILES[provider],
            "model_family": service.model_family,
            "model_identifier": profile.model_identifier,
            "execution_profile": {
                "digest": profile.digest().value,
                "fields": asserted,
                "not_asserted": sorted(k for k, v in asserted.items() if v is None),
                "context_min_tokens": profile.context_min_tokens,
                "object": profile_fields,
            },
            "service_class": {
                "digest": service.digest().value,
                "delivery_mode": service.delivery_mode.value,
                "service_tier": service.service_tier.value,
                "jurisdiction": None
                if service.jurisdiction is None
                else service.jurisdiction.value,
                "data_residency": [c.value for c in service.data_residency],
                "object": service.to_canonical(),
            },
            "evidence": _tier(service.evidence_tier),
            "quote": {
                "price_kind": "configured_demo",
                "price_label": "Configured demo price in micro-tRLUSD (tRLUSD is a test token)"
                + ("; not Nebius's billed price" if provider == NEBIUS else ""),
                "quote_id": quote.quote_id.value,
                "digest": quote.digest().value,
                "input_price_per_million_micro_trlusd": (
                    quote.input_price_per_million_micro_trlusd.value
                ),
                "output_price_per_million_micro_trlusd": (
                    quote.output_price_per_million_micro_trlusd.value
                ),
                "max_input_tokens": quote.max_input_tokens,
                "max_output_tokens": quote.max_output_tokens,
                "max_total_cost_micro_trlusd": max_total_cost_value(quote),
                "valid_until": quote.valid_until.value,
                "object": quote.to_canonical(),
            },
            "availability": self._availability(provider),
        }
        if provider == NEBIUS:
            entry = self._catalog["entry"]
            record["declared_metadata"] = {
                "source": self._catalog["endpoint"],
                "observed_at": self._catalog["observed_at"],
                "note": "Nebius catalog declarations, not Credence evidence. They do not "
                "populate the execution profile; pricing strings are not converted.",
                "quantization": entry["quantization"],
                "context_length": entry["context_length"],
                "regions": entry["regions"],
                "pricing": {
                    "prompt": entry["pricing"]["prompt"],
                    "completion": entry["pricing"]["completion"],
                },
            }
        return record

    def _availability(self, provider: str) -> JSON:
        scenario = "scenario_a_general" if provider == NEBIUS else "scenario_b_controlled"
        delivery = self._live[scenario]["delivery"]
        return {
            "status": "not_monitored",
            "note": "No live availability check in P0. Shown: the last recorded delivery.",
            "last_recorded_delivery": {
                "completed_at": delivery["receipt"]["completed_at"],
                "receipt_digest": delivery["receipt_digest"],
                "source": self._live["id"],
                "latency_ms": self._live.get("nebius_latency_ms") if provider == NEBIUS else None,
            },
        }

    # --- evaluateProcurement ---------------------------------------------------------------

    def procurement_presets(self) -> list[JSON]:
        return [
            {
                "preset_id": preset,
                "title": "Scenario A: general (Tier 1 floor)"
                if preset == "scenario-a-general"
                else "Scenario B: controlled (Q8_0, Tier 2 floor)",
                "form": _form_record(self.preset_form(preset)),
                "recorded_operation_id": self._preset_operation(preset),
            }
            for preset in self._presets
        ]

    def preset_form(self, preset: str) -> ProcurementForm:
        try:
            procurement, facility = self._presets[preset]
        except KeyError:
            raise ProductError("UNKNOWN_PRESET", preset, 404) from None
        return ProcurementForm(
            model_family=procurement.model_family,
            prompt="Synthetic Credence test prompt.",
            max_total_cost_micro_trlusd=None,
            minimum_evidence_tier=max(
                procurement.minimum_evidence_tier.value, facility.minimum_evidence_tier.value
            ),
            allowed_providers=tuple(p.value for p in facility.allowed_providers),
            allowed_precisions=procurement.allowed_precisions,
            allowed_quantizations=procurement.allowed_quantizations,
            allowed_jurisdictions=tuple(c.value for c in procurement.allowed_jurisdictions),
            allowed_data_residencies=tuple(c.value for c in procurement.allowed_data_residencies),
            facility_limit_micro_trlusd=facility.limit_micro_trlusd.value,
            facility_drawn_micro_trlusd=facility.drawn_micro_trlusd.value,
        )

    def _preset_operation(self, preset: str) -> str | None:
        for operation_id, run in self._runs.items():
            if run["procurement_preset"] == preset:
                return operation_id
        return None

    def _matching_preset(self, form: ProcurementForm) -> str | None:
        for preset in self._presets:
            expected = self.preset_form(preset)
            if replace(form, prompt=expected.prompt) == expected:
                return preset
        return None

    def _core_objects(self, form: ProcurementForm) -> tuple[ProcurementClass, Facility]:
        if not form.allowed_providers:
            raise ProductError("NO_PROVIDERS", "allow at least one provider")
        try:
            tier = EvidenceTier(form.minimum_evidence_tier)
            procurement = procurement_class_for(form)
            template = self._presets["scenario-a-general"][1]
            facility = replace(
                template,
                facility_id=FacilityId(DRY_RUN_FACILITY),
                limit_micro_trlusd=MicroTrlusd(form.facility_limit_micro_trlusd),
                drawn_micro_trlusd=MicroTrlusd(form.facility_drawn_micro_trlusd),
                allowed_providers=procurement.allowed_providers,
                allowed_procurement_classes=(procurement.digest(),),
                minimum_evidence_tier=tier,
            )
            # Re-validate the facility as a canonical object (replace() skips nothing, but the
            # round trip makes any invalid combination fail here rather than in the engine).
            Facility.from_canonical(facility.to_canonical())
            if form.max_total_cost_micro_trlusd is not None:
                MicroTrlusd(form.max_total_cost_micro_trlusd)
        except ProductError:
            raise
        except (ValueError, TypeError) as exc:
            raise ProductError("INVALID_PROCUREMENT", str(exc)) from exc
        return procurement, facility

    def evaluate_procurement(self, form: ProcurementForm) -> JSON:
        procurement, facility = self._core_objects(form)
        now = self._clock()
        prompt = form.prompt.encode("utf-8")
        sandbox_key = SigningKey.generate()
        registry = KeyRegistry(
            [
                RegisteredKey(
                    key_id_for(sandbox_key.verify_key),
                    KeyRole.AGENT,
                    facility.borrower_account.value,
                    KeyStatus.CURRENT,
                )
            ]
        )
        candidates = []
        for provider in (NEBIUS, LOCAL):
            offer = self._offers[provider]
            request = PurchaseRequest(
                facility_id=facility.facility_id,
                provider_id=offer.quote.provider_id,
                quote_id=offer.quote.quote_id,
                purpose=Purpose.INFERENCE,
                max_total_cost_micro_trlusd=MicroTrlusd(
                    max_total_cost_value(offer.quote)
                    if form.max_total_cost_micro_trlusd is None
                    else form.max_total_cost_micro_trlusd
                ),
                prompt_digest=payload_digest(prompt),
                nonce=Nonce(secrets.token_hex(16)),
                expires_at=now.plus_seconds(DRY_RUN_TTL_SECONDS),
            )
            candidates.append(
                Purchase(
                    request=request,
                    request_signature=sign(request, sandbox_key),
                    prompt=prompt,
                    facility=facility,
                    quote=offer.quote,
                    service_class=offer.service_class,
                    execution_profile=offer.execution_profile,
                    procurement_classes=(procurement,),
                )
            )
        del sandbox_key
        with tempfile.TemporaryDirectory(prefix="credence-dry-run-") as tmp:
            ledger = ReservationLedger(Path(tmp) / "dry-run.sqlite3")
            selection = qualify_and_select(candidates, registry=registry, ledger=ledger, now=now)
        chosen = None if selection.chosen is None else selection.chosen.purchase.request
        count = selection.qualifying_count
        preset = self._matching_preset(form)
        return {
            "mode": "dry_run",
            "notice": "Core policy dry run. Nothing was reserved and no provider was called. "
            "Each offer was signed in-process by an ephemeral sandbox key created for this "
            "evaluation only.",
            "evaluated_at": now.value,
            "form": _form_record(form),
            "matching_preset": preset,
            "procurement_class": {
                "digest": procurement.digest().value,
                "object": procurement.to_canonical(),
            },
            "facility": {
                "facility_id": facility.facility_id.value,
                "limit_micro_trlusd": facility.limit_micro_trlusd.value,
                "drawn_micro_trlusd": facility.drawn_micro_trlusd.value,
                "held_micro_trlusd": 0,
                "note": "Dry-run facility: held is 0 because the dry-run ledger is empty.",
            },
            "routing_rule": ROUTING_RULE,
            "offers": [
                self._qualification(q, procurement, facility) for q in selection.qualifications
            ],
            "qualifying_count": count,
            "competition": "No qualifying provider"
            if count == 0
            else "1 qualifying provider"
            if count == 1
            else f"{count} qualifying providers; the lowest maximum cost is chosen",
            "chosen_provider_id": None if chosen is None else chosen.provider_id.value,
            "chosen_quote_id": None if chosen is None else chosen.quote_id.value,
        }

    def _qualification(
        self, q: Qualification, procurement: ProcurementClass, facility: Facility
    ) -> JSON:
        request = q.purchase.request
        service = q.purchase.service_class
        reasons: list[str] = []
        code = q.reason_code
        if code is ReasonCode.PROFILE_NOT_ALLOWED:
            reasons = [
                text.REQUIREMENT_TEXT[name]
                for name in structural_failures(
                    procurement, service, q.purchase.execution_profile, request.provider_id
                )
            ]
        elif code is ReasonCode.TIER_TOO_LOW:
            need = required_tier(facility.minimum_evidence_tier, procurement)
            assert not meets_tier(service, need)
            reasons = [
                f"offers Tier {service.evidence_tier.value} "
                f"({text.TIER_TEXT[service.evidence_tier][0]}); "
                f"Tier {need.value} ({text.TIER_TEXT[need][0]}) is required"
            ]
        elif code is ReasonCode.PRICE_ABOVE_MAX:
            reasons = [
                f"maximum cost {q.max_total_cost} micro-tRLUSD is above the maximum acceptable "
                f"price {request.max_total_cost_micro_trlusd.value} micro-tRLUSD"
            ]
        elif code is ReasonCode.OVER_LIMIT:
            reasons = [
                f"drawn {facility.drawn_micro_trlusd.value} + held "
                f"{q.currently_reserved.value} + maximum cost {q.max_total_cost} exceeds the "
                f"limit {facility.limit_micro_trlusd.value} (micro-tRLUSD)"
            ]
        record = q.decision.record
        return {
            "provider_id": request.provider_id.value,
            "display_name": PROVIDER_PROFILES[request.provider_id.value]["display_name"],
            "quote_id": request.quote_id.value,
            "evidence": _tier(service.evidence_tier),
            "max_total_cost_micro_trlusd": q.max_total_cost,
            "price_kind": "configured_demo",
            "qualified": q.qualified,
            "reason_code": None if code is None else code.value,
            "reason": None if code is None else text.REASON_TEXT[code],
            "reason_details": reasons,
            "policy_decision": {"digest": record.digest().value, "object": record.to_canonical()},
        }

    # --- submitPurchase --------------------------------------------------------------------

    def submit_purchase(self, form: ProcurementForm) -> JSON:
        self._core_objects(form)
        preset = self._matching_preset(form)
        operation_id = None if preset is None else self._preset_operation(preset)
        if operation_id is None:
            return {
                "mode": "not_available",
                "code": "LIVE_PURCHASE_NOT_CONNECTED",
                "detail": "Live purchases need the accepted integration backend (I1g/I1h), which "
                "is not connected. Only the recorded offline runs for Scenario A and Scenario B "
                "can be replayed.",
                "operation_id": None,
            }
        return {
            "mode": "offline_replay",
            "code": "RECORDED_OFFLINE_RUN",
            "detail": "No purchase was made. This opens the recorded offline IntegrationService "
            "run for this preset (fake providers, TEST keys).",
            "operation_id": operation_id,
        }

    # --- getOperation ----------------------------------------------------------------------

    def list_operations(self) -> list[JSON]:
        return [self._operation_summary(run) for run in self._runs.values()]

    def _run(self, operation_id: str) -> JSON:
        try:
            return self._runs[operation_id]
        except KeyError:
            raise ProductError("UNKNOWN_OPERATION", operation_id, 404) from None

    def _operation_summary(self, run: JSON) -> JSON:
        op = run["operation"]
        state = OperationState(op["state"])
        last = run["steps"][-1]["core"]["exposure"]
        return {
            "operation_id": op["operation_id"],
            "run_id": run["run_id"],
            "title": run["title"],
            "summary": run["summary"],
            "facility_id": op["facility_id"],
            "provider_id": op["provider_id"],
            "state": state.value,
            "outcome_class": text.OUTCOME_CLASS[state],
            "interrupted": any(s["step"] == "process_interrupted" for s in run["steps"]),
            "reason_code": op["reason_code"],
            "detail": op["detail"],
            "receipt_digest": op["receipt_digest"],
            "drawn_micro_trlusd": last["drawn_micro_trlusd"],
            "held_micro_trlusd": last["held_micro_trlusd"],
            "procurement_preset": run["procurement_preset"],
        }

    def get_operation(self, operation_id: str) -> JSON:
        run = self._run(operation_id)
        op = run["operation"]
        state = OperationState(op["state"])
        observed = {step["journal_state"] for step in run["steps"]}
        order = [s.value for s, _ in text.LIFECYCLE]
        # Journal transitions along the lifecycle are compare-and-set from the previous state, so
        # reaching a lifecycle state means every earlier one was passed through.
        furthest = max((order.index(s) for s in observed if s in order), default=-1)
        lifecycle = [
            {
                "state": s.value,
                "label": label,
                "reached": index <= furthest,
                "observed_in_snapshot": s.value in observed,
            }
            for index, (s, label) in enumerate(text.LIFECYCLE)
        ]
        quote = op["objects"]["quote"]
        return {
            **self._operation_summary(run),
            "source": {
                "kind": "recorded_offline_run",
                "fixture": self._offline["id"],
                "provenance": self._offline["provenance"],
                "harness": run["harness"],
            },
            "state_text": text.STATE_TEXT[state],
            "lifecycle": lifecycle,
            "steps": [
                {
                    **step,
                    "state_text": None
                    if step["journal_state"] is None
                    else text.STATE_TEXT[OperationState(step["journal_state"])],
                }
                for step in run["steps"]
            ],
            "operation": {k: v for k, v in op.items() if k != "objects"},
            "objects": op["objects"],
            "quote_max_total_cost_micro_trlusd": None
            if quote is None
            else max_total_cost_value(Quote.from_canonical(quote)),
            "core_records": run["core_records"],
            "response_authorization": {
                "required": True,
                "note": "Retrieve and acknowledge each need an Ed25519 authorization signed by "
                "the agent's own key (domain credence/integration/response-authorization/v1, "
                "single-use nonce, at most 300 s). The UI never holds or asks for that key.",
            },
        }

    # --- getAccountingExposure -------------------------------------------------------------

    def get_accounting_exposure(self, operation_id: str) -> JSON:
        run = self._run(operation_id)
        facility = run["facility"]
        timeline = [
            {
                "step": s["step"],
                "at": s["at"],
                "journal_state": s["journal_state"],
                **s["core"]["exposure"],
            }
            for s in run["steps"]
        ]
        current = timeline[-1]
        return {
            "operation_id": operation_id,
            "facility_id": facility["facility_id"],
            "source": "Core AccountingView.exposure, recorded during this offline run; each "
            "recorded run uses its own database, so exposures are not combined across runs.",
            "invariant": "limit = drawn + held + headroom",
            "terms": {
                "limit": "The facility limit stored in Core.",
                "drawn": "Accounting-settled verified spending. Not confirmed XRPL loan principal.",
                "held": "Live reservation holds (each purchase's maximum cost until it settles, "
                "is released or expires). Not funds locked on XRPL.",
                "headroom": "limit - drawn - held",
            },
            "facility": {
                "minimum_evidence_tier": _tier(EvidenceTier(facility["minimum_evidence_tier"])),
                "allowed_providers": facility["allowed_providers"],
                "status": facility["status"],
                "maturity_at": facility["maturity_at"],
                "xrpl_loan_id": facility["xrpl_loan_id"],
            },
            "current": current,
            "timeline": timeline,
        }

    # --- getReceiptEvidence ----------------------------------------------------------------

    def _receipt_sources(self) -> list[JSON]:
        sources: list[JSON] = []
        registry = self._live["registry"]
        for scenario in ("scenario_a_general", "scenario_b_controlled"):
            delivery = self._live[scenario]["delivery"]
            chosen = self._live[scenario]["chosen_provider_id"]
            request = next(
                q["purchase_request"]
                for q in self._live[scenario]["qualifications"]
                if q["provider_id"] == chosen
            )
            prefix = "nebius" if chosen == NEBIUS else "local"
            sources.append(
                {
                    "source": "recorded_live_observation",
                    "source_label": f"Recorded live run {self._live['observed_at']} "
                    f"({self._live['id']})",
                    "operation_id": None,
                    "registry": registry,
                    "receipt": delivery["receipt"],
                    "recorded_receipt_digest": delivery["receipt_digest"],
                    "gateway_signature": delivery["gateway_signature"],
                    "provider_attestation": delivery["provider_attestation"],
                    "provider_signature": delivery["provider_attestation_signature"],
                    "purchase_request": request,
                    "execution_profile": self._live["objects"][f"{prefix}_execution_profile"],
                    "response_utf8": None,
                    "verifier": {
                        "outcome": delivery["verifier_decision"]["outcome"],
                        "reason_code": delivery["verifier_decision"]["reason_code"],
                        "decision_digest": None,
                        "decision": delivery["verifier_decision"],
                    },
                    "accounting": {
                        "settled": None,
                        "note": "This observation predates integrated accounting: Core's "
                        "verifier accepted it on the gateway path; no accounting settlement "
                        "was recorded.",
                    },
                }
            )
        for run in self._runs.values():
            op = run["operation"]
            if op["receipt_digest"] is None:
                continue
            retrieved = next(
                (s.get("response_utf8") for s in run["steps"] if s["step"] == "retrieved"), None
            )
            last = run["steps"][-1]["core"]
            sources.append(
                {
                    "source": "recorded_offline_run",
                    "source_label": f"Offline run: {run['title']} (fake providers, TEST keys)",
                    "operation_id": op["operation_id"],
                    "registry": self._offline["registry"],
                    "receipt": op["objects"]["receipt"],
                    "recorded_receipt_digest": op["receipt_digest"],
                    "gateway_signature": op["objects"]["gateway_signature"],
                    "provider_attestation": op["objects"]["provider_attestation"],
                    "provider_signature": op["objects"]["provider_signature"],
                    "purchase_request": op["objects"]["purchase_request"],
                    "execution_profile": op["objects"]["execution_profile"],
                    "response_utf8": retrieved,
                    "verifier": {
                        "outcome": op["settlement_outcome"],
                        "reason_code": op["reason_code"],
                        "decision_digest": op["verifier_decision_digest"],
                        "decision": None,
                    },
                    "accounting": {
                        "settled": last["receipt_accounting_settled"],
                        "chain_head": last["chain_head"],
                        "note": "Core's accounting record at the end of the recorded run.",
                    },
                }
            )
        return sources

    def list_receipts(self) -> list[JSON]:
        return [
            {
                "receipt_digest": s["recorded_receipt_digest"],
                "source": s["source"],
                "source_label": s["source_label"],
                "operation_id": s["operation_id"],
                "provider_id": s["receipt"]["provider_id"],
                "evidence": _tier(EvidenceTier(s["receipt"]["evidence_tier"])),
                "price_micro_trlusd": s["receipt"]["price_micro_trlusd"],
                "completed_at": s["receipt"]["completed_at"],
                "verifier_outcome": s["verifier"]["outcome"],
                "accounting_settled": s["accounting"]["settled"],
            }
            for s in self._receipt_sources()
        ]

    def get_receipt_evidence(self, receipt_digest: str) -> JSON:
        found = [
            s for s in self._receipt_sources() if s["recorded_receipt_digest"] == receipt_digest
        ]
        if not found:
            raise ProductError("UNKNOWN_RECEIPT", receipt_digest, 404)
        s = found[0]
        receipt = DeliveryReceiptBody.from_canonical(s["receipt"])
        gateway = SignedObject.from_canonical(s["gateway_signature"])
        attestation = (
            None
            if s["provider_attestation"] is None
            else ProviderAttestationBody.from_canonical(s["provider_attestation"])
        )
        provider_signed = (
            None
            if s["provider_signature"] is None
            else SignedObject.from_canonical(s["provider_signature"])
        )
        request = PurchaseRequest.from_canonical(s["purchase_request"])
        profile = ExecutionProfile.from_canonical(s["execution_profile"])
        registry = _registry(s["registry"])
        registered = {e["key_id"]: e for e in s["registry"]}
        response = s["response_utf8"]
        checks = receipt_checks(
            receipt,
            gateway,
            attestation,
            provider_signed,
            request,
            profile,
            registry,
            s["recorded_receipt_digest"],
            None if response is None else response.encode("utf-8"),
            "response bytes not available in this record; digest shown as recorded"
            if response is None
            else "recomputed from the response the agent retrieved",
        )
        digest = receipt.digest().value
        tier = receipt.evidence_tier
        signer = registered.get(gateway.key_id.value)
        return {
            "receipt_digest": digest,
            "source": s["source"],
            "source_label": s["source_label"],
            "operation_id": s["operation_id"],
            "provider_id": receipt.provider_id.value,
            "provider": PROVIDER_PROFILES.get(receipt.provider_id.value),
            "model_identifier": profile.model_identifier,
            "execution_profile_digest": receipt.execution_profile_digest.value,
            "evidence": _tier(tier),
            "evidence_ladder": [
                {
                    **_tier(t),
                    "present": t.value <= tier.value,
                    "implemented": t is not EvidenceTier.MEASURED,
                }
                for t in EvidenceTier
            ],
            "receipt": s["receipt"],
            "gateway_signature": s["gateway_signature"],
            "gateway_signer": signer,
            "provider_attestation": s["provider_attestation"],
            "provider_signature": s["provider_signature"],
            "purchase_request": s["purchase_request"],
            "checks": checks,
            "checks_note": "Recomputed now by Core's digest and signature functions against the "
            "registry recorded with this evidence. The verifier decision below is Core's "
            "recorded decision; it is not re-run here.",
            "verifier": s["verifier"],
            "chain": {
                "previous_receipt_digest": None
                if receipt.previous_receipt_digest is None
                else receipt.previous_receipt_digest.value,
                "first_on_facility": receipt.previous_receipt_digest is None,
                "accounting_settled": s["accounting"]["settled"],
                "chain_head": s["accounting"].get("chain_head"),
                "is_chain_head": s["accounting"].get("chain_head") == digest,
                "note": s["accounting"]["note"],
            },
            "price_micro_trlusd": receipt.price_micro_trlusd.value,
            "input_token_count": receipt.input_token_count,
            "output_token_count": receipt.output_token_count,
        }

    # --- getPaymentStatus ------------------------------------------------------------------

    def get_payment_status(self, operation_id: str) -> JSON:
        run = self._run(operation_id)
        op = run["operation"]
        return {
            "operation_id": operation_id,
            "xrpl": {
                "state": "not_submitted",
                "source": "baseline",
                "transaction_hash": None,
                "detail": "No XRPL transaction is built, signed or submitted at this baseline. "
                "XRPL settlement is not implemented.",
            },
            "accounting": {
                "settlement_outcome": op["settlement_outcome"],
                "state": op["state"],
                "note": "Accounting settlement is Credence's internal record. It is not an XRPL "
                "payment and does not confirm one.",
            },
        }

    def xrpl_settlement_fixtures(self) -> JSON:
        return self._xrpl_states
