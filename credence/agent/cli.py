"""Agent command line for the I2a offline marketplace. The agent's key stays in this process.

    uv run python -m credence.agent.cli --demo-dir DIR sign --preparation PREP --prompt TEXT
    uv run python -m credence.agent.cli --demo-dir DIR purchase --preset scenario-a-general --prompt TEXT
    uv run python -m credence.agent.cli --demo-dir DIR authorize --operation OP --action retrieve
    uv run python -m credence.agent.cli --demo-dir DIR retrieve --operation OP
    uv run python -m credence.agent.cli --demo-dir DIR acknowledge --operation OP

Reads `<demo-dir>/agent/` (created by `credence.api.offline_demo init`): the agent's key and its
session token. `sign` and `authorize` print JSON for pasting into the UI; the UI never sees the key.
Prints no key material.

Owner: Inference (ResearchBot / agent SDK). Written in the I2a integration; provisional.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from nacl.signing import SigningKey

from credence.agent.client import AgentClient, Expectations, urllib_http
from credence.integration.authorization import ResponseAction


def _client(demo_dir: Path, api: str) -> AgentClient:
    agent_dir = demo_dir / "agent"
    seed = bytes.fromhex((agent_dir / "agent.key").read_text(encoding="ascii").strip())
    token = (agent_dir / "session-token").read_text(encoding="ascii").strip()
    return AgentClient(urllib_http(api), token, SigningKey(seed))


def _prompt(args: argparse.Namespace) -> bytes:
    if args.prompt_file is not None:
        return Path(args.prompt_file).read_bytes()
    return str(args.prompt).encode("utf-8")


def _expectations(client: AgentClient, args: argparse.Namespace) -> Expectations:
    session = client.session()
    return Expectations(
        facility_ids=frozenset(session["facility_ids"]),
        allowed_providers=None if not args.provider else frozenset(args.provider),
        max_total_cost_micro_trlusd=args.max_cost,
        minimum_evidence_tier=args.min_tier,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="credence.agent.cli", description=__doc__)
    parser.add_argument("--demo-dir", type=Path, required=True)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_prompt(p: argparse.ArgumentParser) -> None:
        group = p.add_mutually_exclusive_group(required=True)
        group.add_argument("--prompt")
        group.add_argument("--prompt-file")
        p.add_argument("--provider", action="append", help="providers the agent accepts")
        p.add_argument("--max-cost", type=int, help="agent's ceiling, micro-tRLUSD")
        p.add_argument("--min-tier", type=int, default=0, help="agent's evidence floor")

    p_sign = sub.add_parser("sign", help="verify a preparation and print the signature JSON")
    p_sign.add_argument("--preparation", required=True)
    with_prompt(p_sign)
    p_buy = sub.add_parser("purchase", help="prepare, verify, sign and submit")
    p_buy.add_argument("--preset", required=True)
    with_prompt(p_buy)
    for name in ("authorize", "retrieve", "acknowledge"):
        p = sub.add_parser(name)
        p.add_argument("--operation", required=True)
        if name == "authorize":
            p.add_argument("--action", choices=["retrieve", "acknowledge"], required=True)
    args = parser.parse_args(argv)
    client = _client(args.demo_dir, args.api)

    if args.command == "sign":
        prompt = _prompt(args)
        prepared = client.preparation(args.preparation)
        print(json.dumps(client.sign_preparation(prepared, prompt, _expectations(client, args))))
    elif args.command == "purchase":
        prompt = _prompt(args)
        status, data = urllib_http(args.api)("GET", "/v0/procurement/presets", None, {})
        presets = {p["preset_id"]: p["form"] for p in json.loads(data)} if status == 200 else {}
        if args.preset not in presets:
            raise SystemExit(f"unknown preset {args.preset}")
        print(
            json.dumps(
                client.purchase(presets[args.preset], prompt, _expectations(client, args)),
                indent=2,
            )
        )
    elif args.command == "authorize":
        print(json.dumps(client.authorization(args.operation, ResponseAction(args.action))))
    elif args.command == "retrieve":
        sys.stdout.write(client.retrieve(args.operation).decode("utf-8") + "\n")
    else:
        client.acknowledge(args.operation)
        print(f"acknowledged {args.operation}")


if __name__ == "__main__":
    main(sys.argv[1:])
