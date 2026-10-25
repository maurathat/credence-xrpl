# Demo dataset, reset and recovery

The demo dataset is generated, not shipped. A fresh `init` gives every judge session the same starting state: one agent
(`researchbot-demo`), two facilities with a limit of 10,000,000 micro-tRLUSD each and nothing drawn, the two offline mock
providers, and the recorded offline runs and recorded live evidence that the API serves read-only. No demo directory
belongs in Git.

## Fresh dataset (before each judging slot)

```bash
# Stop the API first (Ctrl-C in its terminal).
DEMO=~/claw-street-demo-$(date +%Y%m%d-%H%M)
uv run python -m credence.api.offline_demo init  "$DEMO"     # refuses a directory that isn't empty
uv run python -m credence.api.offline_demo serve "$DEMO"     # 127.0.0.1:8000
cat "$DEMO/agent/session-token"                              # paste into Agent session in the browser
```

Use the same `$DEMO` for the agent CLI (`--demo-dir "$DEMO"`). The session token expires 12 hours after `init`.

**Expected first run:** Scenario A drawn 0 → 2, Scenario B drawn 0 → 8 (micro-tRLUSD). Each later run adds the same
receipt price (A +2, B +8).

## Reset

Start a new directory as above. The old directory can be kept as a record, or deleted by the operator. Resetting never
needs a database edit, and there is no "clear" endpoint.

## Recovery

| Symptom | Cause | Action |
| --- | --- | --- |
| Browser shows the API as unavailable | API not running, or the web app was built for another address | Start `serve`; check `VITE_CREDENCE_API_BASE_URL` |
| `OwnershipError: … is served by another process` (printed as a traceback) | A second server was started on the same database | Stop the other server. Only one process may own the database |
| `401 SESSION_EXPIRED` / `INVALID_SESSION` | Token past 12 hours, or pasted wrongly | Re-paste the token; if expired, make a fresh dataset |
| API stopped mid-demo | Process exit | Run `serve` again on the **same** directory. Recovery runs at start; drawn amounts and operations are kept (verified across a restart) |
| A purchase shows `unknown` or `released_undelivered` | The recorded failure-path runs, or an interrupted purchase | Expected states. They are documented on the Activity page; don't edit the database |
| Browser flow stuck mid-lifecycle | UI or paste problem | Use the CLI fallback: `agent.cli --demo-dir "$DEMO" purchase --preset scenario-a-general --prompt "Synthetic Credence test prompt."` |

## Before going on stage

1. Make a fresh dataset, then confirm that `curl -s http://127.0.0.1:8000/v0/health` reports `live_purchases` false,
   `ledger.connected` false and `integration.xrpl` `not_connected`.
2. Sign in, run Scenario A once as a dry rehearsal (drawn 0 → 2), then make a fresh dataset again for the judges, or
   explain the non-zero start.
3. Keep a terminal ready with the `sign` and `authorize` commands from `README.md`.
4. Keep the recorded video as a fallback if the laptop fails.
