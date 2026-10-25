# Claw Street — Powered by Credence

## Short description

AI agents procure inference under spending, credit and evidence policies. Credence evaluates
offers, verifies the agent's purchase signature and delivery evidence, and records facility accounting.

## Submission description

An autonomous agent needs more than a way to transfer money: it needs explicit authority over
what it may buy, from whom, at what cost and with what evidence of delivery.

Claw Street, built by Kessai Labs and powered by Credence, demonstrates that procurement loop.
The buyer specifies its model family, allowed providers, execution profile and evidence floor.
Credence Core filters offers and reserves their maximum cost against a purpose-bound facility.
The agent verifies and signs the prepared purchase outside the browser. After an offline mock
provider executes, Core verifies receipt evidence and replaces the reservation with the
verified charge. Response retrieval and acknowledgement each require agent authorization.

The local demo includes two configured provider offers. A general Tier 1 policy selects a
lower-cost witnessed offer. A controlled Tier 2 policy refuses an incompatible profile and
selects the qualifying local-provider mock. The browser shows policy decisions, signed
purchase progression, receipt checks, facility exposure and the recorded activity journal.

This submission runs locally with offline mock providers. Prices are integer micro-tRLUSD;
tRLUSD is a synthetic test unit. Accounting settlement is internal to Credence. No provider
receives a payment and no XRPL marketplace transaction is built, signed or submitted. The
public website is a separate browser simulation with seeded data. XRPL settlement,
QuickNode, t54/x402 payments and Ledger hardware signing are not connected. Tier 1 means the
gateway witnessed a response; Tier 2 means a provider signed what it says it served. Neither
proves that exact model weights ran.

## Judge links

- Website: https://creds-for-ai.lovable.app — browser simulation.
- Working local demo: https://github.com/maurathat/credence-xrpl/tree/maurathat/claw-street-release/release/claw-street
- Frontend source: https://github.com/maurathat/creds-for-ai/tree/release/claw-street-ui
- Demo video: upload `claw-street-local-demo.webm` after adding your narration, then paste the
  accessible video URL in the submission form. Do not use a local filesystem link in that form.
- Founder pitch: record yourself in 9:16 using `FOUNDER-PITCH.md` and the supplied portrait cards.

Repository visibility is not changed by this release. Supply judge access or an approved
source archive if either repository remains private. Verify the video link in a signed-out
browser before submitting.

## Keep separate

Core/X2-0R and the native payout design remain blocked and outside this release. No test result,
website status or separate connectivity transaction grants authority to sign or submit an
XRPL payment. Historical test reports describe their original exact code revisions; the
submission release includes its own validation report.
