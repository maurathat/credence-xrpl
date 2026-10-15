"""Procurement matcher: does a service class (with its execution profile) satisfy a procurement class?

Pure and deterministic. Structural requirements only; evidence tier is judged separately so the
policy engine can tell PROFILE_NOT_ALLOWED from TIER_TOO_LOW.

Rules (contracts/POLICY-VERIFIER.md):
- model_family: equal.
- precision, quantization: if the procurement class leaves the dimension null, the buyer imposes
  no constraint and any profile value, asserted or not, matches. Otherwise the profile's value
  must be asserted (not null) and in the allowed set. There is no precision ranking.
- delivery_mode: equal.
- jurisdiction: asserted (not null) and in allowed_jurisdictions.
- data residency: the service's residency set is non-empty and every member is allowed (subset);
  overlap alone is not enough.
- provider: the quoting provider is in allowed_providers.
A null or empty (not asserted) service or profile value never satisfies a constraint that is
present. Only precision and quantization can be left unconstrained by the buyer.

Owner: Credence Core.
"""

from credence.core.objects import ExecutionProfile, ProcurementClass, ServiceClass
from credence.core.primitives import EvidenceTier, ProviderId

REQUIREMENTS = (
    "model_family",
    "precision",
    "quantization",
    "delivery_mode",
    "jurisdiction",
    "data_residency",
    "provider",
)


def _permits(allowed: tuple[str, ...] | None, asserted: str | None) -> bool:
    """Null allowed set: no constraint. Otherwise the value must be asserted and allowed."""
    if allowed is None:
        return True
    return asserted is not None and asserted in allowed


def structural_failures(
    procurement: ProcurementClass,
    service_class: ServiceClass,
    profile: ExecutionProfile,
    provider_id: ProviderId,
) -> tuple[str, ...]:
    """The structural requirements the service fails, in REQUIREMENTS order; empty means a match."""
    residency = service_class.data_residency
    passed = {
        "model_family": service_class.model_family == procurement.model_family,
        "precision": _permits(procurement.allowed_precisions, profile.precision),
        "quantization": _permits(procurement.allowed_quantizations, profile.quantization),
        "delivery_mode": service_class.delivery_mode is procurement.delivery_mode,
        "jurisdiction": service_class.jurisdiction is not None
        and service_class.jurisdiction in procurement.allowed_jurisdictions,
        "data_residency": bool(residency)
        and set(residency) <= set(procurement.allowed_data_residencies),
        "provider": provider_id in procurement.allowed_providers,
    }
    return tuple(name for name in REQUIREMENTS if not passed[name])


def required_tier(facility_minimum: EvidenceTier, procurement: ProcurementClass) -> EvidenceTier:
    """The stricter of the facility's and the procurement class's minimum evidence tier."""
    return max(facility_minimum, procurement.minimum_evidence_tier, key=lambda tier: tier.value)


def meets_tier(service_class: ServiceClass, required: EvidenceTier) -> bool:
    return service_class.evidence_tier.value >= required.value
