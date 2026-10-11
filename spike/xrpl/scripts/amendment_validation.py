"""Strict validation for the xrpld 3.4.0 `feature` response."""

from __future__ import annotations

from typing import Any

REQUIRED_AMENDMENTS: dict[str, str] = {
    "MPTokensV1": "950AE2EA4654E47F04AA8739C0B214E242097E802FD372D24047A89AB1F5EC38",  # gitleaks:allow -- public protocol ID
    "SingleAssetVault": "81BD2619B6B3C8625AC5D0BC01DE17F06C3F0AB95C7C87C93715B87A4FD240D8",
    "LendingProtocol": "565B90CA1AB2B9D42208ED10884188C64F9E19083DECB9634AAF06EB03299509",
    "LendingProtocolV1_1": "A360E2BFD775A5B0DCE1C36C16DF31B72735A57584FD163655D2F9564F8E7AC8",
    "fixCleanup3_1_3": "303ACB16CF8DBD3B5C34F131A9D19A7DE01AE05F480A8A682B869D1B4AAC8CFC",
    "fixCleanup3_2_0": "21B8D2F76F68E11E9C077A43BBBC394136E9987E99DDB73966DD68419467E431",
    "fixCleanup3_3_0": "3298D47E1F3A8A24FECAA30F699B8FE1DD234E072834BA099AD8180FFCE0FEC4",
    "fixCleanup3_4_0": "98433DD001A5737F773D74F8CA2A25A065089C73B2E611C760BAF369E4FECA76",
}


class AmendmentValidationError(ValueError):
    """The raw feature response does not satisfy the required amendment set."""


def validate_feature_response(response: dict[str, Any]) -> dict[str, Any]:
    """Validate a complete JSON-RPC response and return machine-readable state.

    The amendment map is intentionally resolved through `result.features`.
    Treating amendment IDs as direct children of `result` caused the original
    false BLOCKED result.
    """
    result = response.get("result")
    if not isinstance(result, dict):
        raise AmendmentValidationError("feature response has no result object")
    if result.get("status") != "success":
        raise AmendmentValidationError("feature response status is not success")

    features = result.get("features")
    if not isinstance(features, dict):
        raise AmendmentValidationError("feature response has no result.features map")

    checks: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for name, amendment_id in REQUIRED_AMENDMENTS.items():
        entry = features.get(amendment_id)
        exists = isinstance(entry, dict)
        supported = exists and entry.get("supported") is True
        enabled = exists and entry.get("enabled") is True
        reported_name = entry.get("name") if exists else None
        name_matches = reported_name == name
        checks[name] = {
            "id": amendment_id,
            "exists": exists,
            "reported_name": reported_name,
            "name_matches": name_matches,
            "supported": supported,
            "enabled": enabled,
        }
        if not exists:
            errors.append(f"{name}: missing ID {amendment_id}")
        elif not name_matches:
            errors.append(f"{name}: response name is {reported_name!r}")
        if exists and not supported:
            errors.append(f"{name}: not supported")
        if exists and not enabled:
            errors.append(f"{name}: not enabled")

    validation = {
        "valid": not errors,
        "required_count": len(REQUIRED_AMENDMENTS),
        "response_amendment_count": len(features),
        "amendments": checks,
        "errors": errors,
    }
    if errors:
        raise AmendmentValidationError("; ".join(errors))
    return validation
