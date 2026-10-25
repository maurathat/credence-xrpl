import { chromium } from "playwright";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve, join } from "node:path";
import readline from "node:readline/promises";

const args = process.argv.slice(2);
const mode = args[0] ?? "check";
assert(["check", "record", "present"].includes(mode), "Use check, record or present");
const option = (name, fallback) => {
  const position = args.indexOf(name);
  return position === -1 ? fallback : args[position + 1];
};
const root = resolve(import.meta.dirname, "../../..");
const demoDir = resolve(option("--demo-dir", process.env.CREDENCE_DEMO_DIR ?? ""));
assert(demoDir !== process.cwd(), "Set --demo-dir to an initialized offline demo directory");
const site = new URL(option("--site", "http://127.0.0.1:5173"));
const api = new URL(option("--api", "http://127.0.0.1:8000"));
for (const address of [site, api]) {
  assert(address.protocol === "http:" && address.hostname === "127.0.0.1", "Loopback HTTP only");
  assert(!address.username && !address.password, "Credentials in URLs are refused");
}
const token = readFileSync(join(demoDir, "agent/session-token"), "utf8").trim();
const stamp = new Date().toISOString().replace(/[:.]/g, "-");
const output = resolve(option("--output", join(import.meta.dirname, "output", `local-${mode}-${stamp}`)));
mkdirSync(output, { recursive: true });
const browser = await chromium.launch({ headless: mode !== "present" && !args.includes("--headed") });
const context = await browser.newContext({
  viewport: { width: 1440, height: 900 },
  serviceWorkers: "block",
  ...(mode === "record" ? { recordVideo: { dir: output, size: { width: 1440, height: 900 } } } : {}),
});
const blocked = [];
const errors = [];
const timings = [];
const operations = [];
const allowedPosts = /^\/v0\/(procurement\/evaluate|live\/purchases\/(prepare|submit)|live\/operations\/[^/]+\/response)$/;
await context.route("**/*", async (route) => {
  const request = route.request();
  const address = new URL(request.url());
  const localRead = [site.origin, api.origin].includes(address.origin) && ["GET", "HEAD", "OPTIONS"].includes(request.method());
  const localWrite = address.origin === api.origin && request.method() === "POST" && allowedPosts.test(address.pathname);
  if (localRead || localWrite) return route.continue();
  blocked.push(`${request.method()} ${address.origin}${address.pathname}`);
  return route.abort("blockedbyclient");
});
await context.routeWebSocket(/.*/, (socket) => {
  const address = new URL(socket.url());
  if (address.protocol === "ws:" && address.host === site.host) socket.connectToServer();
  else {
    blocked.push(`WS ${address.origin}`);
    socket.close();
  }
});
const page = await context.newPage();
page.setDefaultTimeout(15000);
page.on("pageerror", (error) => errors.push(error.message));
const terminal = mode === "present" ? readline.createInterface({ input: process.stdin, output: process.stdout }) : null;
const agent = (...command) => {
  const python = process.env.CREDENCE_PYTHON;
  const executable = python || "uv";
  const prefix = python ? [] : ["run", "python"];
  return execFileSync(executable, [...prefix, "-m", "credence.agent.cli", "--demo-dir", demoDir, "--api", api.origin, ...command], {
    cwd: root,
    encoding: "utf8",
    env: { ...process.env, PYTHONPATH: root },
    timeout: 20000,
    stdio: ["ignore", "pipe", "pipe"],
  }).trim();
};
const get = async (path, auth = true) => {
  const response = await fetch(new URL(path, api), { headers: auth ? { authorization: `Bearer ${token}` } : {} });
  assert(response.ok, `${path}: HTTP ${response.status}`);
  return response.json();
};
const goto = async (path) => {
  await page.goto(new URL(path, site).href, { waitUntil: "networkidle" });
};
const visible = async (text) => page.getByText(text, { exact: false }).first().waitFor({ state: "visible" });
const stage = async (id, seconds, caption, action) => {
  const started = Date.now();
  console.log(`${id}: ${caption}`);
  await action();
  await page.evaluate((text) => {
    let caption = document.getElementById("demo-caption");
    if (!caption) {
      caption = document.createElement("div");
      caption.id = "demo-caption";
      Object.assign(caption.style, {
        position: "fixed", bottom: "18px", left: "10%", width: "80%", zIndex: "99999", padding: "15px 20px",
        borderRadius: "10px", background: "#071421f5", color: "#eaf2f8", border: "1px solid #3dff8f",
        font: "500 18px/1.45 system-ui", boxSizing: "border-box", pointerEvents: "none",
      });
      document.body.appendChild(caption);
    }
    caption.textContent = text;
  }, caption);
  await page.screenshot({ path: join(output, `${id}.png`) });
  if (mode === "record") await page.waitForTimeout(Math.max(0, seconds * 1000 - (Date.now() - started)));
  if (terminal) await terminal.question("Enter for the next stage. ");
  timings.push({ id, seconds: (Date.now() - started) / 1000 });
};
const evaluate = async () => {
  const response = page.waitForResponse((response) => response.url().endsWith("/v0/procurement/evaluate") && response.request().method() === "POST");
  await page.getByRole("button", { name: "Run Core policy dry run", exact: true }).click();
  assert((await response).ok());
  await page.getByRole("button", { name: "Prepare purchase (offline mock providers)", exact: true }).waitFor();
};
const purchase = async () => {
  const preparedResponse = page.waitForResponse((response) => response.url().endsWith("/v0/live/purchases/prepare") && response.request().method() === "POST");
  await page.getByRole("button", { name: "Prepare purchase (offline mock providers)", exact: true }).click();
  const response = await preparedResponse;
  assert(response.ok());
  const prepared = await response.json();
  const prompt = await page.getByLabel("Inference workload (prompt)").inputValue();
  const signature = agent("sign", "--preparation", prepared.preparation_id, "--prompt", prompt);
  await page.getByLabel("Agent signature JSON").fill(signature);
  await page.getByRole("button", { name: "Submit signed purchase", exact: true }).click();
  await page.waitForURL(`**/live/${prepared.operation_id}`);
  await visible("Receipt and evidence");
  const operation = await get(`/v0/live/operations/${prepared.operation_id}`);
  assert.equal(operation.state, "settled");
  const payment = await get(`/v0/live/operations/${prepared.operation_id}/payment`);
  assert.equal(payment.xrpl.transaction_hash, null);
  assert.notEqual(payment.xrpl.state, "settled");
  const receipt = await get(`/v0/live/receipts/${encodeURIComponent(operation.receipt_digest)}`);
  assert(receipt.accounting.settled);
  assert(receipt.checks.some((check) => check.result === "pass"));
  assert(!receipt.checks.some((check) => check.result === "fail"));
  operations.push({ operationId: prepared.operation_id, provider: operation.provider_id, receipt: receipt.receipt_digest, charge: receipt.price_micro_trlusd, xrpl: payment.xrpl.state });
  return prepared.operation_id;
};
let succeeded = false;
try {
  const health = await get("/v0/health", false);
  assert.equal(health.live_purchases, false);
  assert.equal(health.ledger.connected, false);
  assert.equal(health.integration.xrpl, "not_connected");
  const initial = await get("/v0/live/facilities");
  let operationId;
  await stage("01-overview", 20, "Claw Street — Powered by Credence. AI-agent procurement under spending, credit and evidence policies. This local demo runs Core with offline mock providers.", async () => {
    await goto("/");
    await visible("Working local purchase flow");
    await page.getByLabel("Agent session token").fill(token);
    await page.getByRole("button", { name: "Use session", exact: true }).click();
    await visible("Signed in as researchbot-demo");
  });
  await stage("02-market", 20, "Compare configured offers for the same model family. These are demo prices in micro-tRLUSD, a synthetic test unit. Core evaluates which offers qualify.", async () => {
    await goto("/market");
    await visible("Nebius");
    await visible("offline");
  });
  await stage("03-policy", 30, "Scenario B requires a controlled execution profile and Tier 2 evidence. Core refuses the mismatched offer. Scenario A admits a lower-cost witnessed offer.", async () => {
    await goto("/procure");
    await page.getByRole("button", { name: /Scenario B/ }).click();
    await evaluate();
    await visible("PROFILE_NOT_ALLOWED");
    await page.getByRole("button", { name: /Scenario A/ }).click();
    await evaluate();
    const cap = page.getByLabel("Maximum acceptable price", { exact: false });
    await cap.fill("1000");
    await evaluate();
    assert(await page.getByRole("button", { name: "Prepare purchase (offline mock providers)", exact: true }).isDisabled());
    await cap.fill("");
    await evaluate();
  });
  await stage("04-signed-purchase", 35, "The backend prepares the request. The local agent verifies and signs it outside the browser. Core verifies the offline delivery evidence and records the charge. No XRPL transaction is created.", async () => {
    operationId = await purchase();
    assert.equal(operations[0].charge, 2);
    await page.getByText("Receipt and evidence", { exact: true }).scrollIntoViewIfNeeded();
  });
  await stage("05-response", 25, "The agent authorizes response retrieval, then acknowledges delivery with a separate single-use authorization. The retained response is deleted after acknowledgement.", async () => {
    for (const action of ["retrieve", "acknowledge"]) {
      const authorization = agent("authorize", "--operation", operationId, "--action", action);
      await page.getByLabel("Agent response authorization JSON").fill(authorization);
      const response = page.waitForResponse((response) => response.url().endsWith(`/operations/${operationId}/response`) && response.request().method() === "POST");
      await page.getByRole("button", { name: action === "retrieve" ? "Retrieve response" : "Acknowledge (delete retained copy)", exact: true }).click();
      assert((await response).ok());
      await visible(action === "retrieve" ? "Retrieved (synthetic reply" : "Acknowledged");
    }
    assert.equal((await get(`/v0/live/operations/${operationId}`)).state, "acknowledged");
  });
  await stage("06-credit", 20, "The maximum quote reservation becomes a verified charge of 2 micro-tRLUSD. Facility accounting changes; XRPL payment remains not submitted.", async () => {
    await goto("/credit");
    await visible("Accounting");
    const current = await get("/v0/live/facilities");
    const before = initial.find((facility) => facility.facility_id === "facility-market-general");
    const after = current.find((facility) => facility.facility_id === "facility-market-general");
    assert.equal(after.exposure.drawn_micro_trlusd - before.exposure.drawn_micro_trlusd, 2);
    assert.equal(after.exposure.held_micro_trlusd, 0);
  });
  await stage("07-evidence", 20, "Receipts bind requests, execution profiles and responses. Tier 1 means gateway-witnessed; Tier 2 means the provider signed what it says it served. Neither proves exact weights ran.", async () => {
    await goto("/receipts");
    await page.getByRole("table", { name: "Integrated receipts", exact: true }).waitFor();
    await visible("exact model weights");
  });
  await stage("08-close", 10, "Policy decides. The agent signs. Credence verifies and accounts. The public website is a separate browser simulation. XRPL marketplace settlement and hardware signing remain unconnected.", async () => {
    await goto("/activity");
    await page.locator(`a[href="/live/${operationId}"]`).first().waitFor();
  });
  if (mode === "check") {
    await goto("/procure");
    await page.getByRole("button", { name: /Scenario B/ }).click();
    await evaluate();
    await purchase();
    assert.equal(operations[1].charge, 8);
    const denied = await fetch(new URL("/v0/live/session", api), { headers: { authorization: "Bearer invalid-demo-session" } });
    assert.equal(denied.status, 401);
  }
  assert.deepEqual(errors, [], "Browser errors");
  assert.deepEqual(blocked, [], "Unexpected external or state-changing network request");
  succeeded = true;
  console.log("PASS: signed purchases, receipts, retrieval, acknowledgement, accounting and network guard.");
} catch (error) {
  await page.screenshot({ path: join(output, "failure.png"), fullPage: true }).catch(() => {});
  throw error;
} finally {
  terminal?.close();
  const video = page.video();
  await context.close();
  await browser.close();
  if (video) await video.saveAs(join(output, "claw-street-local-demo.webm"));
  writeFileSync(join(output, "run.json"), JSON.stringify({ succeeded, mode, site: site.origin, api: api.origin, timings, operations, errors, blocked, recording: mode === "record" ? "Silent recording with captions; add founder narration." : null }, null, 2));
  console.log(`Artifacts: ${output}`);
}
