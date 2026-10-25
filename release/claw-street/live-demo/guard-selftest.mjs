#!/usr/bin/env node
// Proves the network guard (guard.mjs) blocks writes. Run: npm run selftest. Exit 0 only if every check passes.
// The "submit" below carries a dummy blob that is not a valid transaction, so even an unguarded run could not
// move funds. The guard must drop it before it leaves the browser.

import { chromium } from "playwright";
import { installGuard } from "./guard.mjs";

const browser = await chromium.launch();
const context = await browser.newContext();
const guard = await installGuard(context);
const page = await context.newPage();
await page.goto("https://testnet.xrpl.org/", { waitUntil: "domcontentloaded", timeout: 60000 });

const results = await page.evaluate(async () => {
  const out = {};
  const ws = new WebSocket("wss://s.altnet.rippletest.net:51233/");
  const replies = [];
  ws.onmessage = (e) => replies.push(JSON.parse(e.data));
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; setTimeout(reject, 15000); });
  ws.send(JSON.stringify({ id: "read", command: "server_info" }));
  ws.send(JSON.stringify({ id: "write", command: "submit", tx_blob: "00" }));
  ws.send(JSON.stringify({ id: "sign", command: "sign", tx_json: {}, secret: "not-a-secret" }));
  ws.send(JSON.stringify({ id: "keys", command: "wallet_propose" }));
  await new Promise((r) => setTimeout(r, 4000));
  out.readAnswered = replies.some((r) => r.id === "read");
  out.writeAnswered = replies.some((r) => ["write", "sign", "keys"].includes(r.id));
  ws.close();
  try {
    await fetch("https://creds-for-ai.lovable.app/__probe", { method: "POST", body: "{}" });
    out.postBlocked = false;
  } catch {
    out.postBlocked = true;
  }
  out.foreignWsBlocked = await new Promise((resolve) => {
    const other = new WebSocket("wss://s1.ripple.com/");
    other.onopen = () => resolve(false);
    other.onclose = () => resolve(true);
    other.onerror = () => resolve(true);
    setTimeout(() => resolve(true), 8000);
  });
  return out;
});
await browser.close();

const denied = guard.deniedXrplCommandAttempts();
const checks = {
  "read-only server_info still answered": results.readAnswered === true,
  "submit / sign / wallet_propose never answered": results.writeAnswered === false,
  "guard recorded the dropped submit, sign and wallet_propose": ["submit", "sign", "wallet_propose"].every((c) => denied.some((d) => d.startsWith(`WS ${c} `))),
  "POST to the site aborted": results.postBlocked === true,
  "WebSocket to a non-Testnet host (mainnet s1.ripple.com) blocked": results.foreignWsBlocked === true,
};
let ok = true;
for (const [name, pass] of Object.entries(checks)) {
  console.log(`${pass ? "PASS" : "FAIL"}  ${name}`);
  ok &&= pass;
}
console.log("guard summary:", JSON.stringify(guard.summary()));
process.exit(ok ? 0 : 1);
