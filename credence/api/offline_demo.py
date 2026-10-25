"""Run the I2a offline marketplace: the product API with the IntegrationService embedded.

    uv run python -m credence.api.offline_demo init  <demo-dir>
    uv run python -m credence.api.offline_demo serve <demo-dir> [--port 8000]

OFFLINE ONLY. Providers are the offline mocks of `offline_world.py` (no provider is called), prices
are configured demo prices in micro-tRLUSD (tRLUSD is a test token), and no XRPL transaction is
built, signed or submitted. The server binds to loopback only.

`init` creates two separate directories:
- `<demo-dir>/agent/` belongs to the agent: its own Ed25519 key (`agent.key`, owner-only) and its
  session token (`session-token`, owner-only). The agent CLI (`credence.agent.cli`) reads them.
  The server never reads this directory.
- `<demo-dir>/server/` belongs to the server: `agents.json` (owner-only) with each agent's
  public key ID, borrower account, the SHA-256 of its session token and the token's expiry
  (never the token or any private key),
  and the SQLite database, which this one process owns (`ProcessOwnership`).

`serve` opens the IntegrationService on that database (recovery runs first), registers the
configured facilities once, and serves the API with it embedded in this process.

Owner: API/UI (I2a integration). Provisional.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from collections.abc import Callable
from pathlib import Path

from nacl.signing import SigningKey

from credence.api.app import create_app
from credence.api.marketplace import AgentDirectory, AgentPrincipal, Marketplace, token_digest
from credence.api.offline_world import GATEWAY_TEST_KEY, OfflineWorld, offline_world
from credence.api.product import utc_now
from credence.core.primitives import KeyId, Timestamp
from credence.core.settlement_coordinator import SettlementRefused
from credence.core.signatures import KeyRegistry, key_id_for
from credence.integration.service import IntegrationService

DATABASE = "credence-i2a.sqlite3"
SESSION_TTL_SECONDS = 12 * 3600  # demo session tokens expire after 12 hours
AGENT_ENTRY_MEMBERS = {"agent_id", "agent_key_id", "subject", "token_sha256", "token_expires_at"}
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _write_private(path: Path, data: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(data)


def init(demo_dir: Path, agent_id: str = "researchbot-demo") -> None:
    """Create a fresh demo: one agent with its own key and session token. Never overwrites."""
    if demo_dir.exists() and any(demo_dir.iterdir()):
        raise SystemExit(f"{demo_dir} is not empty; choose a new directory")
    agent_dir, server_dir = demo_dir / "agent", demo_dir / "server"
    agent_dir.mkdir(parents=True, mode=0o700)
    server_dir.mkdir(mode=0o700)
    world = offline_world()
    borrowers = {c.facility.borrower_account.value for c in world.facilities.values()}
    if len(borrowers) != 1:
        raise SystemExit("the offline market configuration must have one borrower")
    subject = borrowers.pop()
    key = SigningKey.generate()
    key_id = key_id_for(key.verify_key).value
    token = "cred-agent-" + secrets.token_urlsafe(32)
    _write_private(agent_dir / "agent.key", bytes(key).hex() + "\n")
    _write_private(agent_dir / "session-token", token + "\n")
    (agent_dir / "agent.json").write_text(
        json.dumps({"agent_id": agent_id, "agent_key_id": key_id}, indent=2) + "\n",
        encoding="ascii",
    )
    entry = {
        "agent_id": agent_id,
        "agent_key_id": key_id,
        "subject": subject,
        "token_sha256": token_digest(token),
        "token_expires_at": utc_now().plus_seconds(SESSION_TTL_SECONDS).value,
    }
    _write_private(server_dir / "agents.json", json.dumps([entry], indent=2) + "\n")
    del key, token
    print(f"agent key and session token: {agent_dir} (owner-only; the server never reads them)")
    print(f"server configuration: {server_dir / 'agents.json'} (public key IDs, token digests)")


def load_agents(server_dir: Path) -> list[dict[str, str]]:
    entries = json.loads((server_dir / "agents.json").read_text(encoding="ascii"))
    for entry in entries:
        if set(entry) != AGENT_ENTRY_MEMBERS:
            raise SystemExit(
                f"agents.json: each entry must have exactly {sorted(AGENT_ENTRY_MEMBERS)} "
                "(public key ID, subject, token digest and expiry; no tokens or keys)"
            )
    return list(entries)


def open_marketplace(
    database: Path,
    agents: list[dict[str, str]],
    *,
    world: OfflineWorld | None = None,
    clock: Callable[[], Timestamp] = utc_now,
    registry: KeyRegistry | None = None,
) -> tuple[IntegrationService, Marketplace, OfflineWorld]:
    """Open the service (recovery first), register the configured facilities once, and wrap it.

    `registry` is the operator's registry (built from `agents` when omitted); revoking a key on
    it while the service is open persists the revocation (I1g)."""
    world = world or offline_world()
    service = IntegrationService(
        database,
        registry=registry
        or world.registry({KeyId(a["agent_key_id"]): a["subject"] for a in agents}),
        gateway_key=GATEWAY_TEST_KEY,
        providers=world.providers(),
        clock=clock,
    )
    try:
        for config in world.facilities.values():
            try:
                service.register_facility(config.facility)
            except SettlementRefused as exc:
                if exc.reason != "FACILITY_ALREADY_REGISTERED":
                    raise
        directory = AgentDirectory(
            {
                a["token_sha256"]: AgentPrincipal(
                    a["agent_id"], KeyId(a["agent_key_id"]), Timestamp(a["token_expires_at"])
                )
                for a in agents
            }
        )
        marketplace = Marketplace(
            service,
            facilities=world.facilities,
            offers=world.offers,
            directory=directory,
            clock=clock,
        )
    except BaseException:
        service.close()
        raise
    return service, marketplace, world


def serve(demo_dir: Path, host: str, port: int) -> None:
    import uvicorn

    if host not in LOOPBACK:
        raise SystemExit("the offline demo binds to loopback only")
    server_dir = demo_dir / "server"
    service, marketplace, _ = open_marketplace(server_dir / DATABASE, load_agents(server_dir))
    try:
        uvicorn.run(create_app(marketplace=marketplace), host=host, port=port, workers=1)
    finally:
        service.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="credence.api.offline_demo", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_init = sub.add_parser("init", help="create a fresh demo directory")
    p_init.add_argument("demo_dir", type=Path)
    p_init.add_argument("--agent-id", default="researchbot-demo")
    p_serve = sub.add_parser("serve", help="serve the API with the service embedded")
    p_serve.add_argument("demo_dir", type=Path)
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    if args.command == "init":
        init(args.demo_dir, args.agent_id)
    else:
        serve(args.demo_dir, args.host, args.port)


if __name__ == "__main__":
    main(sys.argv[1:])
