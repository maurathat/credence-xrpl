# Deployment readiness

Status: **local backend demo plus recorded video; separate public browser simulation.**
The corrected browser simulation is published at https://creds-for-ai.lovable.app from hosted
frontend commit `a7bb600b43a8b7dc4e5ff297723810306861fac6`. All 13 public routes, the purchase/attack
simulations and routing strategies were checked in Playwright. It does not connect to this backend.
No backend deployment or hosted payment service is part of the submission.

## Checklist

| Area | Current state (verified on `9844530`) | Ready? |
| --- | --- | --- |
| Frontend build | `bun run build` with nitro preset **`cloudflare-module`** (a Cloudflare Workers bundle; `nitro.json` suggests `wrangler deploy`). `npx vite preview` serves it locally | Local: yes. Hosted: needs a Cloudflare account, or an untested switch to a Node preset |
| Frontend API address | `VITE_CREDENCE_API_BASE_URL` is read at build time; it defaults to `http://127.0.0.1:8000` (`web/src/lib/credence-api/client.ts:43`) | Local: yes. Hosted: rebuild with the public API origin |
| Backend API | `credence.api.offline_demo serve` (FastAPI and uvicorn, 1 worker). It **refuses any non-loopback host** (`credence/api/offline_demo.py:155`) | Local: yes. Public: only behind a same-host reverse proxy, which needs an owner decision (below) |
| CORS | Only `http://localhost` and `http://127.0.0.1` origins (`credence/api/app.py:39`). A public origin is refused unless web and API share one origin | Same-origin proxy avoids CORS; any other layout needs an API change (API/UI owner) |
| Environment | No secrets or API keys needed. Optional: `VITE_CREDENCE_API_BASE_URL`; `CREDENCE_LOCAL_QWEN=1` / `CREDENCE_LOCAL_LLAMA=1` only for opt-in real-model tests | Yes |
| Demo account | `offline_demo init <dir>` creates one agent (`researchbot-demo`), an Ed25519 key and a 12-hour session token (`SESSION_TTL_SECONDS`), each owner-only. Two facilities: `facility-market-general` and `facility-market-controlled` (limit 10,000,000 micro-tRLUSD each) | Yes; re-init within 12 hours of judging |
| Local models | None for the demo; both providers are offline mocks | Yes |
| Secrets | The agent key and session token stay in `<dir>/agent/`; the server keeps only public key IDs and token digests (`<dir>/server/agents.json`). Test keys come from public labels in `offline_world.py`. gitleaks is clean on the tree and history. Never commit a demo directory | Yes; keep demo dirs outside the repo |
| Persistence | One SQLite file, `<dir>/server/credence-i2a.sqlite3`, owned by one process (an ownership lock refuses a second server). Recovery runs at start. Drawn amounts and operations survive a restart (verified) | Yes, single host only |
| Startup | See `README.md`: `init`, then `serve`, then `bun run dev` (or `vite preview` after a build) | Yes |
| Blocked financial operations | No XRPL adapter is imported by the product; x402 `executable` and `submission_authorized` are always false; no live provider calls | Yes: nothing financial can be enabled by configuration |

## Simplest deployments

1. **Recommended: local demo plus a recorded video.** Run the README steps on the presenter's laptop, on loopback,
   and record the three-minute script. Judges can reproduce it from the README. No code change, no exposure, nothing
   to authorize beyond publishing the repository and the video.
2. **Public read-mostly demo on one host (owner decision required).**
   - Setup: one small VM. The API stays on `127.0.0.1:8000`, unchanged. The web bundle is rebuilt with
     `VITE_CREDENCE_API_BASE_URL=https://<host>` and served on loopback. A TLS reverse proxy (for example Caddy) sends
     `/v0/*` to the API and everything else to the web server, so the browser sees a single origin and CORS is not
     involved.
   - Purchases still need the agent key, so only the operator can buy (CLI over SSH). Visitors can browse.
   - **This exposes the API publicly, which the loopback-only boundary was reviewed against.** It needs Maura's
     authorization and an independent security review. Missing today: rate limiting, abuse handling, and a decision
     on whether to publish a session token.
   - The web side also needs either a Cloudflare deployment or a tested Node build.
3. **Not useful: frontend only** (Lovable or Cloudflare without the API). Every screen would show the API as
   unavailable.

## Remaining operations

1. Upload the narrated demo and founder pitch, then supply their accessible URLs.
2. Supply judge repository access. Repository visibility is unchanged.
3. The stale `/v0/health` value `"baseline": "a3ec0a31…"` remains an identified API/UI limitation.
   Verify the running code with the Git revision and submission validation, not that legacy label.
