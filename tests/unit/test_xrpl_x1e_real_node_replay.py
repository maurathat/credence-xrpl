"""X1e: replay real rippled 3.4.0 (API v2) responses through the transport, offline.

The responses in ``spike/xrpl/x1e/responses/`` were captured read-only from the pinned local
standalone node (see ``spike/xrpl/x1e/MANIFEST.json``). Replaying them checks, in every test run,
that the transport parses the real node's shapes, and that the committed evidence is intact.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from xrpl.core.addresscodec import encode_node_public_key

from credence.xrpl_adapter.rippled import (
    RippledTransport,
    RippledTransportConfig,
    TransportRefused,
)
from credence.xrpl_adapter.types import LookupDisposition

EVIDENCE = Path(__file__).parents[2] / "spike" / "xrpl" / "x1e"
MANIFEST = json.loads((EVIDENCE / "MANIFEST.json").read_text(encoding="utf-8"))
NODE = MANIFEST["node"]
GENESIS = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"
UNFUNDED = "rrrrrrrrrrrrrrrrrrrrBZbvji"
HISTORICAL_TX = "C8E822A0DFFC673D0F91048310281C0259CD00735F216A848DA36708EEF43C74"
REPLAY_NODE_KEY = encode_node_public_key(bytes.fromhex("02" + "22" * 32))
OTHER_NODE_KEY = encode_node_public_key(bytes.fromhex("02" + "33" * 32))
CONFIG = RippledTransportConfig(
    source_id="replay-x1e",
    expected_network_id=NODE["network_id"],
    expected_build_version=NODE["build_version"],
    expected_node_public_key=REPLAY_NODE_KEY,
)


def _key(method: str, params: Mapping[str, Any]) -> str:
    return json.dumps([method, dict(params)], sort_keys=True)


class ReplayClient:
    """Answers exactly the requests that were captured; anything else is a test failure."""

    def __init__(self) -> None:
        shapes = json.loads((EVIDENCE / "shapes.json").read_text(encoding="utf-8"))
        self.responses = {
            _key(item["method"], item["params"]): (
                EVIDENCE / "responses" / item["file"]
            ).read_bytes()
            for item in shapes
        }
        self.unmatched: list[str] = []

    def call(self, method: str, params: Mapping[str, Any]) -> bytes:
        key = _key(method, params)
        if key not in self.responses:
            self.unmatched.append(key)
            raise AssertionError(f"request was not captured from the real node: {key}")
        # Captured public node identity was redacted for the repository's generic secret scanner.
        # Substitute one valid public-only test identity after manifest verification.
        return self.responses[key].replace(b"REDACTED_PUBLIC_NODE_KEY", REPLAY_NODE_KEY.encode())


def transport(config: RippledTransportConfig = CONFIG) -> tuple[RippledTransport, ReplayClient]:
    client = ReplayClient()
    return RippledTransport(client, config), client


def test_evidence_matches_its_manifest() -> None:
    actual = {
        path.relative_to(EVIDENCE).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in EVIDENCE.rglob("*.json")
        if path.name != "MANIFEST.json"
    }
    assert actual == MANIFEST["files"]


def test_evidence_contains_no_secret_material() -> None:
    seed = re.compile(r"\bs[1-9A-HJ-NP-Za-km-z]{28,30}\b")
    for path in EVIDENCE.rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert not seed.search(text), path
        assert '"secret"' not in text and '"seed"' not in text, path


def test_real_server_state_and_ledger_establish_pinned_identity() -> None:
    real, _ = transport()
    identity = real.validated_ledger_identity()
    assert (identity.network_id, identity.build_version, identity.node_public_key) == (
        21_337,
        "3.4.0",
        REPLAY_NODE_KEY,
    )
    assert identity.ledger_index == NODE["validated_ledger"]["seq"]
    assert identity.ledger_hash == NODE["validated_ledger"]["hash"]
    assert identity.base_fee_drops == 10
    assert (identity.reserve_base_drops, identity.reserve_increment_drops) == (
        10_000_000,
        2_000_000,
    )


def test_real_account_info_objects_and_lines_parse() -> None:
    real, _ = transport()
    identity = real.validated_ledger_identity()
    genesis = real.account_snapshot(GENESIS, identity)
    assert genesis.exists and genesis.sequence == 1 and genesis.regular_key is None
    assert genesis.signer_list is None and genesis.tickets == ()
    unfunded = real.account_snapshot(UNFUNDED, identity)
    assert not unfunded.exists and unfunded.ledger_hash == identity.ledger_hash
    line = real.trust_line(GENESIS, UNFUNDED, "74524C5553440000000000000000000000000000", identity)
    assert not line.exists


def test_real_unbounded_tx_not_found_responses_are_not_non_inclusion_proof() -> None:
    real, _ = transport()
    for tx_hash in ("AB" * 32, HISTORICAL_TX):
        result = real.lookup(tx_hash)
        assert (result.disposition, result.payment, result.detail) == (
            LookupDisposition.NOT_FOUND,
            None,
            "BOUNDED_NON_INCLUSION_PROOF_UNAVAILABLE",
        )


def test_real_validated_ledger_observation() -> None:
    real, _ = transport()
    observation = real.validated_ledger(GENESIS)
    assert observation.history_complete and observation.has_trusted_identity()
    assert observation.account_sequence == 1


def test_real_node_wrong_pins_are_refused() -> None:
    for changes, code in (
        ({"expected_node_public_key": OTHER_NODE_KEY}, "NODE_IDENTITY_MISMATCH"),
        ({"expected_network_id": 1}, "NETWORK_MISMATCH"),
        ({"expected_build_version": "3.4.1"}, "BUILD_VERSION_MISMATCH"),
    ):
        config = RippledTransportConfig(**{**CONFIG.__dict__, **changes})
        real, _ = transport(config)
        with pytest.raises(TransportRefused) as raised:
            real.validated_ledger_identity()
        assert raised.value.code == code


def test_sanitized_node_key_is_bound_to_the_reported_identity() -> None:
    report = (EVIDENCE.parent / "X1E-REAL-NODE-PREFLIGHT.md").read_text(encoding="utf-8")
    reported = re.findall(r"`(n9[1-9A-HJ-NP-Za-km-z]{40,60})`", report)
    assert reported, "the X1e report records the node's public key"
    assert {hashlib.sha256(key.encode()).hexdigest() for key in reported} == {
        NODE["node_identity_sha256"]
    }
    assert NODE["pubkey_node"] == "REDACTED_PUBLIC_NODE_KEY"
