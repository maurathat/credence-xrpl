// The seven demo stages. Selectors and expected text were taken from the live site
// (https://creds-for-ai.lovable.app) and the XRPL Testnet explorer on 2026-10-25 with Playwright; see README.md.
// Each stage navigates, performs only read-only or client-side-simulation actions, and checks what it shows.

export const SITE = "https://creds-for-ai.lovable.app";
export const TX_HASH = "D26C5DDC6F51877D745462BD7A6C8C8FE24972137637E2B62D1E269D0F76DE79";
export const TX_URL = `https://testnet.xrpl.org/transactions/${TX_HASH}`;
export const TX_LEDGER = "21807427";
export const LOGO_ALT = "Claw Street — Powered by Credence"; // the home page's animated hero logo (an <img>)
export const TX_MEMO = "XRPL connectivity proof transaction. Not governed by Credence procurement authorization.";

// "Simulate attack" picks one of these six cases at random (observed on the live site; the UI can't choose one).
// The runner reads the case shown and says the matching line.
export const ATTACK_LINES = {
  "Over limit": "Over limit: 900 units, 392.40 tRLUSD, is more than the available credit. Stopped at authorization.",
  "Wrong provider": "Wrong provider: Helios Grid isn't on the facility allowlist. Stopped at authorization.",
  "Expired key": "Expired key: the agent's delegate key has expired, so only the root can re-delegate. Stopped at authorization.",
  "Replayed receipt": "Replayed receipt: that receipt's request digest was already used by REQ-4815. Stopped at the receipt check.",
  "FP8 vs BF16": "Wrong precision: the receipt declares FP8, the facility approves BF16. Stopped at verification.",
  "Wrong model": "Wrong model: the receipt declares OpenCoder-34B, the facility approves Meridian-70B-Instruct. Stopped at verification.",
};

export const STAGES = [
  {
    id: "overview",
    title: "1 · Open Claw Street and the Overview",
    seconds: 22,
    cue: [
      "Claw Street is a marketplace where AI agents buy inference under rules they can't rewrite. It's powered by Credence.",
      "This is the operator's overview: one credit facility, its utilization, and recent purchases.",
      "Everything on this site is a hackathon prototype with seeded demo data, and the page says so.",
    ],
    async run(h) {
      await h.goto("/");
      await h.focus(h.logo(), { holdMs: 2500 });
      await h.click(h.page.getByRole("link", { name: "Open terminal" }).first());
      await h.page.waitForURL("**/overview");
      await h.expectText("Overview");
      await h.focus(h.heading("Facility utilization & cumulative verified spend"));
      await h.focus(h.heading("Market stack"));
      await h.focus(h.page.getByText("All figures are seeded demo data", { exact: false }).first());
    },
  },
  {
    id: "marketplace",
    title: "2 · Marketplace: compare providers under a procurement policy",
    seconds: 30,
    cue: [
      "Same order, five provider offers: price per verified inference unit, latency, evidence tier, residency.",
      "The routing strategy is the agent's procurement policy. Watch which offer becomes Best.",
      "These strategy buttons simulate price, evidence and allowlist selection in the browser. Credence Core runs only in the separate local demo.",
    ],
    async run(h) {
      await h.goto("/market");
      await h.expectText("Inference procurement");
      await h.focus(h.heading("Provider bids for ORD-2261"));
      for (const strategy of ["Cheapest", "Verified", "Credit-eligible"]) {
        await h.click(h.page.getByRole("radio", { name: strategy }));
        await h.expectText(`Best under ${strategy}:`);
        await h.focus(h.page.getByText(`Best under ${strategy}:`, { exact: false }).first());
      }
    },
  },
  {
    id: "agent-purchase",
    title: "3 · Agent purchase (client-side simulation on the Credit page)",
    seconds: 32,
    cue: [
      "Now the agent buys. This runs the site's purchase simulation in the browser; no provider is called and nothing is paid.",
      "The four displayed checks are simulated. No agent signature or provider receipt is verified on this public site.",
      "The simulated facility draw is 10.46 tRLUSD, a synthetic test unit. This is in-browser accounting only.",
    ],
    async run(h) {
      await h.goto("/credit");
      await h.expectText("Credence Credit");
      // Start from the seeded state so the numbers below are always the same.
      await h.click(h.page.getByRole("button", { name: "Reset", exact: true }));
      await h.expectText("362.40 tRLUSD");
      await h.focus(h.heading("Simulate inference purchase"));
      await h.click(h.page.getByRole("button", { name: "Simulate inference purchase", exact: true }));
      await h.expectText("Facility drawn +10.46 tRLUSD (in-browser accounting simulation — no payment, nothing settled on XRPL)");
      await h.expectText("Simulated draw recorded in in-browser accounting only.");
      await h.focus(h.page.getByText("Simulated draw recorded in in-browser accounting only.", { exact: false }).first());
    },
  },
  {
    id: "credit",
    title: "4 · Credit & payments: the facility accounting changed",
    seconds: 26,
    cue: [
      "In the facility panel, available credit went from 362.40 to 351.94 tRLUSD, outstanding to 148.06, and utilization from 27.5% to 29.6%.",
      "Now an attack. The site picks one of six attack cases at random; whichever it is, it's rejected, nothing is drawn and balances don't change.",
      "tRLUSD is a synthetic test unit here. These balances are simulated in the browser; nothing is paid.",
    ],
    async run(h) {
      // Normally continues on stage 3's page. If started directly (--from credit) or replayed after the attack,
      // re-create the simulated purchase first so the numbers are right.
      const settled = h.page.getByText("Simulated draw recorded in in-browser accounting only.", { exact: false }).first();
      if (!h.page.url().endsWith("/credit") || !(await settled.isVisible().catch(() => false))) {
        await h.goto("/credit");
        await h.click(h.page.getByRole("button", { name: "Reset", exact: true }));
        await h.click(h.page.getByRole("button", { name: "Simulate inference purchase", exact: true }));
        await h.expectText("Simulated draw recorded in in-browser accounting only.");
      }
      await h.focus(h.heading("Active facility"));
      await h.expectText("351.94 tRLUSD");
      await h.expectText("148.06 tRLUSD");
      await h.focus(h.heading("Utilization"));
      await h.expectText("29.6%");
      await h.focus(h.heading("Simulate inference purchase"));
      await h.click(h.page.getByRole("button", { name: "Simulate attack", exact: true }));
      await h.expectText("Rejected — no draw, balances unchanged.");
      const label = await h.page.getByText(/^Attack case:/).first().innerText().catch(() => "");
      const attack = label.replace("Attack case:", "").trim();
      if (ATTACK_LINES[attack]) await h.say(ATTACK_LINES[attack]);
      else h.fail(`unknown attack case "${attack}"; add it to ATTACK_LINES`);
      await h.focus(h.page.getByText("Rejected — no draw, balances unchanged.", { exact: false }).first());
      await h.focus(h.heading("Facility requests"));
    },
  },
  {
    id: "evidence",
    title: "5a · Evidence: receipts and verification",
    seconds: 22,
    cue: [
      "The page displays seeded example receipts and their evidence tiers. These hosted receipts are not cryptographically verified.",
      "Tier 2 means the provider signed what it says it served. It is not proof that exact weights ran; the page says that too.",
      "The evidence ladder shows which levels are present for these receipts and which are roadmap.",
    ],
    async run(h) {
      await h.goto("/receipts");
      await h.expectText("Inference receipts");
      await h.focus(h.heading("Receipts"));
      await h.expectText("provider signed what it says it served");
      await h.focus(h.heading("Evidence ladder"));
    },
  },
  {
    id: "activity",
    title: "5b · Activity: purchase history",
    seconds: 12,
    cue: [
      "And the history: each request with its outcome. Passes, and rejections where the policy said no: wrong profile, over limit.",
    ],
    async run(h) {
      await h.goto("/overview");
      await h.focus(h.heading("Recent receipt activity"));
      await h.expectText("REQ-4821");
      await h.expectText("REJECT");
    },
  },
  {
    id: "xrpl-testnet",
    title: "6 · XRPL Testnet connectivity proof (separate from the marketplace)",
    seconds: 26,
    cue: [
      "Separately, we proved XRPL connectivity on the public Testnet: one native XRP payment of a single drop.",
      "Validated in ledger 21807427. The memo says what it is: a connectivity proof, not governed by Credence procurement authorization.",
      "It's not a Credence-governed marketplace settlement. That's the next integration step.",
    ],
    async run(h) {
      await h.gotoUrl(TX_URL);
      await h.expectText(TX_HASH);
      await h.click(h.page.getByText("Detailed", { exact: true }).first());
      await h.expectText(`validated in ledger ${TX_LEDGER}`);
      await h.expectText(TX_MEMO);
      await h.focus(h.page.getByText(`validated in ledger ${TX_LEDGER}`, { exact: false }).first());
      await h.focus(h.page.getByText(TX_MEMO, { exact: false }).first());
    },
  },
  {
    id: "close",
    title: "7 · Close on the Claw Street logo",
    seconds: 10,
    cue: ["Claw Street, powered by Credence. Explore this browser simulation, then run the local signed-purchase demo. Thank you."],
    async run(h) {
      await h.goto("/");
      await h.focus(h.logo(), { holdMs: 5000 });
    },
  },
];

export const TOTAL_SECONDS = STAGES.reduce((s, x) => s + x.seconds, 0);
