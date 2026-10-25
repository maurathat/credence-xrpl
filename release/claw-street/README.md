# Claw Street — Powered by Credence

Claw Street is an AI-agent procurement marketplace. An agent states what inference it may buy. Credence Core decides
which offers qualify under the agent's spending, credit and evidence policy. The agent signs the purchase with its own
key, and Credence records the verified delivery receipt and the accounting. Kessai Labs builds Claw Street on Credence
infrastructure; Credence is a Kessai product.

**This build is an offline demonstration.** The providers are offline mocks: no provider is called. Prices are
configured demo prices in micro-tRLUSD (tRLUSD is a test token). Accounting settlement is Credence's internal record,
not an XRPL payment. No XRPL transaction is built, signed or submitted. See [`DISCLOSURES.md`](DISCLOSURES.md).

| Document | Contents |
| --- | --- |
| [`DEMO-SCRIPT.md`](DEMO-SCRIPT.md) | Three-minute demonstration and judge walkthrough |
| [`PITCH-TALK-TRACK.txt`](PITCH-TALK-TRACK.txt) | Narration matched to the six-slide pitch deck |
| [`assets/claw-street-pitch.pdf`](assets/claw-street-pitch.pdf) | Original supplied six-slide pitch deck |
| [`SUBMISSION-ASSETS.md`](SUBMISSION-ASSETS.md) | GitHub downloads for video, complete kit and portrait cards |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Components, data flow and integration status |
| [`TEST-SUMMARY.md`](TEST-SUMMARY.md) | Reproducible test and browser results for this release candidate |
| [`DISCLOSURES.md`](DISCLOSURES.md) | Sponsor and third-party integration disclosures |
| [`DEPLOYMENT-READINESS.md`](DEPLOYMENT-READINESS.md) | Hosting checklist and the deployment decisions still open |
| [`DEMO-RESET.md`](DEMO-RESET.md) | Clean demo dataset, reset and recovery |
| [`SUBMISSION-CHECKLIST.md`](SUBMISSION-CHECKLIST.md) | Deliverables and unresolved blockers |
| [`TESTNET-PROOF.md`](TESTNET-PROOF.md) | Reserved slot for a separate XRPL Testnet connectivity proof (empty) |

## Requirements

- macOS or Linux, `git`, [`uv`](https://docs.astral.sh/uv/) (Python 3.12 is pinned), [`bun`](https://bun.sh) 1.4+.
- No API keys, accounts or local models are needed for the demo.

## Start the demo (two terminals, all on loopback)

```bash
git clone --branch maurathat/claw-street-release https://github.com/maurathat/credence-xrpl.git credence
cd credence
git submodule update --init          # custody serializer and the web app (release/claw-street-ui)
uv sync --locked

# Terminal 1: create the demo agent and start the offline API on 127.0.0.1:8000
uv run python -m credence.api.offline_demo init  ~/claw-street-demo
uv run python -m credence.api.offline_demo serve ~/claw-street-demo

# Terminal 2: start the web app on 127.0.0.1:5173
cd web && bun install --frozen-lockfile
VITE_CREDENCE_API_BASE_URL=http://127.0.0.1:8000 bun run dev --host 127.0.0.1 --port 5173
```

Open <http://127.0.0.1:5173>. To sign in as the demo agent, paste the contents of
`~/claw-street-demo/agent/session-token` into **Agent session**. The token is not a signing key, and it expires after
12 hours. The agent's private key never enters the browser. The agent signs from a third terminal:

```bash
uv run python -m credence.agent.cli --demo-dir ~/claw-street-demo sign --preparation <prep-id> --prompt "<prompt>"
uv run python -m credence.agent.cli --demo-dir ~/claw-street-demo authorize --operation <op-id> --action retrieve
uv run python -m credence.agent.cli --demo-dir ~/claw-street-demo authorize --operation <op-id> --action acknowledge
```

The same flow can run without the browser (the presets are `scenario-a-general` and `scenario-b-controlled`):

```bash
uv run python -m credence.agent.cli --demo-dir ~/claw-street-demo purchase \
  --preset scenario-a-general --prompt "Synthetic Credence test prompt."
```

## Verify

```bash
make test && make typecheck && make lint      # Python: unit, conformance, offline integration
(cd web && bun run typecheck && bun run build && bun test)
```

## Presenter and recording

With the API and website running, the runner completes the real local signed purchase flow and
keeps the agent key outside the browser. It only permits the configured loopback website and
API; external requests are blocked. Generated datasets and recordings stay outside Git.

```bash
cd release/claw-street/live-demo
npm ci
npx playwright install chromium
npm run local:check -- --demo-dir ~/claw-street-demo
npm run local:visible -- --demo-dir ~/claw-street-demo
npm run local:present -- --demo-dir ~/claw-street-demo
npm run local:record -- --demo-dir ~/claw-street-demo
```

Use a fresh dataset for the recording. `local:check` and `local:visible` each execute both
scenarios, adding 2 and 8 micro-tRLUSD to their respective facilities. `local:present` and
`local:record` execute Scenario A only. Presenter mode pauses after each stage. The recording
targets three minutes and includes captions but no audio. Add your own narration from
[`DEMO-SCRIPT.md`](DEMO-SCRIPT.md). Artifacts are written to `live-demo/output/` (ignored).
Override addresses with `--site http://127.0.0.1:PORT --api http://127.0.0.1:PORT`.

The older `present`, `record` and `check` commands drive the separate public browser simulation.
They do not exercise this backend. See [`SUBMISSION.md`](SUBMISSION.md) and
[`FOUNDER-PITCH.md`](FOUNDER-PITCH.md) for submission text and the portrait founder recording.
