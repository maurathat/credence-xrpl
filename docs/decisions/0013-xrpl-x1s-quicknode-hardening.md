# 0013. XRPL X1s: QuickNode transport hardening

- Status: Proposed. Awaiting targeted independent X1s acceptance.
- Date: 2026-10-24
- Scope: XRPL X1s, offline. Base `baadf60` (X1q). Amends decision 0012.
- Source: Maura's X1s brief; Salvador's X1r findings Q1-Q4. Contract:
  `contracts/QUICKNODE-ADAPTER-CONTRACT.md`. Report: `spike/xrpl/X1S-HARDENING.md`.

## Decisions

1. **Contain hostile responses (Q1).**
   - Remote JSON is parsed only through `parse_json_bounded`, which enforces size, nesting
     (checked before the recursive parser) and number limits.
   - XRPL amounts and integers are bounded to XRPL's own ranges before any arithmetic.
   - Public reads raise only `QuickNodeError`, and `observe_payment` never raises. Anything
     unanticipated becomes `RESPONSE_PROCESSING_FAILED`.
   - Malformed HTTP is `MALFORMED_HTTP_RESPONSE`.
2. **Absolute deadlines (Q2).**
   - The client moves from `urllib` to `http.client`, driven under one absolute deadline per
     attempt.
   - A watchdog timer shuts the socket down at the deadline, DNS is bounded for the caller, the
     body is read with `read1` against the remaining time, and the retry sequence has a total
     budget.
   - The uncancellable DNS lookup and the platform behavior of socket shutdown are documented
     rather than claimed away.
3. **Credential isolation by construction (Q3).**
   - The token-bearing request target lives only in one frame that returns, rather than raises,
     its failures.
   - Every QuickNode error is raised elsewhere, with no cause or context.
   - The remaining exposure (the endpoint must hold its URL) is documented.
4. **The probe reports instead of crashing (Q4).**
   - Each check is contained and partial results are kept.
   - Exit codes: 0 pass, 1 failed, 2 not configured, 3 incomplete.

## Consequences

- Trust policy, network isolation, the read-only surface and the
  no-finalization binding are unchanged.
- Real-endpoint verification remains PENDING until an endpoint with a rotated token is configured
  privately. Request 0010 remains open.
