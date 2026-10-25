#!/usr/bin/env node
// Claw Street live demo runner. Read-only: see guard.mjs. Usage: see README.md.
//
//   node demo.mjs present    headed browser; pauses before every stage until you press a key
//   node demo.mjs rehearse   headed browser; advances on a timer (about 3:00), so you can practise narration
//   node demo.mjs record     headless; records a backup video with captions to output/<timestamp>/
//   node demo.mjs check      headless, fast; verifies every stage against the live site and exits 0 or 1
//
// Options: --from <stage-id>  start at a stage      --no-highlight  don't outline the focused element
//          --captions / --no-captions  on-screen narration captions (default: on for record only)
//          --fullscreen  presenter browser in fullscreen   --slow <factor>  stretch on-screen motion (default 1)
//          --headless    run present/rehearse without a visible window (for testing the flow)
//          --keep-badge  leave Lovable's "Edit with Lovable" badge on screen (by default it is dismissed: it covers
//                        the Credit page's Reset button)

import { chromium } from "playwright";
import { mkdirSync, renameSync, writeFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import readline from "node:readline";
import { installGuard } from "./guard.mjs";
import { LOGO_ALT, SITE, STAGES, TOTAL_SECONDS } from "./stages.mjs";

const args = process.argv.slice(2);
const mode = args[0];
if (!["present", "rehearse", "record", "check"].includes(mode)) {
  console.error("usage: node demo.mjs <present|rehearse|record|check> [--from <stage-id>] [--no-highlight] [--captions|--no-captions] [--fullscreen] [--slow <factor>]");
  process.exit(2);
}
const flag = (name) => args.includes(name);
const option = (name, fallback) => (args.includes(name) ? args[args.indexOf(name) + 1] : fallback);
const fromId = option("--from", STAGES[0].id);
const highlight = !flag("--no-highlight") && mode !== "check";
const captions = flag("--captions") || (mode === "record" && !flag("--no-captions"));
const slow = Number(option("--slow", "1"));
const keepBadge = flag("--keep-badge");
const scrollBehavior = mode === "check" ? "instant" : "smooth";
const startIndex = STAGES.findIndex((s) => s.id === fromId);
if (startIndex < 0) {
  console.error(`unknown stage "${fromId}"; stages: ${STAGES.map((s) => s.id).join(", ")}`);
  process.exit(2);
}

const stamp = new Date().toISOString().replace(/[:.]/g, "-");
const outDir = join(import.meta.dirname, "output", `${mode}-${stamp}`);
if (mode === "record" || mode === "check") mkdirSync(outDir, { recursive: true });
const log = (...m) => console.log(...m);
const failures = [];

// ---------- browser ----------
const headed = (mode === "present" || mode === "rehearse") && !flag("--headless");
const browser = await chromium.launch({
  headless: !headed,
  args: headed ? [flag("--fullscreen") ? "--start-fullscreen" : "--start-maximized"] : [],
});
const context = await browser.newContext({
  viewport: headed ? null : { width: 1440, height: 900 },
  ...(mode === "record" ? { recordVideo: { dir: outDir, size: { width: 1440, height: 900 } } } : {}),
});
const guard = await installGuard(context, log);
const page = await context.newPage();
page.setDefaultTimeout(20000);

// ---------- helpers given to each stage ----------
const beat = (ms) => (mode === "check" ? Promise.resolve() : page.waitForTimeout(ms * slow));
let currentCaption = "";

async function paintCaption() {
  if (!captions || !currentCaption) return;
  await page.evaluate((text) => {
    let el = document.getElementById("__demo_caption");
    if (!el) {
      el = document.createElement("div");
      el.id = "__demo_caption";
      Object.assign(el.style, {
        position: "fixed", left: "50%", bottom: "24px", transform: "translateX(-50%)", zIndex: 2147483647,
        maxWidth: "1100px", padding: "12px 18px", borderRadius: "10px", background: "rgba(5,10,8,0.88)",
        color: "#e8fff1", font: "500 17px/1.45 Inter, system-ui, sans-serif", border: "1px solid #39ff88",
        boxShadow: "0 6px 30px rgba(0,0,0,0.5)", pointerEvents: "none",
      });
      document.body.appendChild(el);
    }
    el.textContent = text;
  }, currentCaption).catch(() => {});
}

const h = {
  page,
  heading: (name) => page.getByRole("heading", { name, exact: true }).first(),
  logo: () => page.getByRole("img", { name: LOGO_ALT, exact: true }).first(),
  async goto(path) {
    await page.goto(SITE + path, { waitUntil: "networkidle" });
    if (!keepBadge) {
      // Lovable's floating builder badge; its own "Dismiss" button hides it client-side.
      const dismiss = page.locator("#lovable-badge-close");
      if (await dismiss.isVisible().catch(() => false)) await dismiss.click().catch(() => {});
    }
    await paintCaption();
    await beat(900);
  },
  async gotoUrl(url) {
    await page.goto(url, { waitUntil: "networkidle", timeout: 60000 });
    await paintCaption();
    await beat(1200);
  },
  async expectText(text) {
    try {
      await page.getByText(text, { exact: false }).first().waitFor({ state: "visible", timeout: 15000 });
    } catch {
      failures.push(`${currentStage?.id}: expected text not visible: "${text}"`);
      log(`  ! expected text not visible: "${text}"`);
    }
  },
  async focus(locator, { holdMs = 1800 } = {}) {
    try {
      await locator.waitFor({ state: "visible", timeout: 15000 });
      await locator.evaluate((el, behavior) => el.scrollIntoView({ behavior, block: "center" }), scrollBehavior);
      await beat(700);
      if (highlight) {
        await locator.evaluate((el) => {
          const prev = el.style.outline;
          el.style.outline = "3px solid #39ff88";
          el.style.outlineOffset = "4px";
          setTimeout(() => { el.style.outline = prev; }, 2600);
        });
      }
      await beat(holdMs);
    } catch (e) {
      failures.push(`${currentStage?.id}: could not focus element (${e.message.split("\n")[0]})`);
      log(`  ! could not focus element: ${e.message.split("\n")[0]}`);
    }
  },
  async say(line) {
    // A line that depends on what the page shows (for example the random attack case).
    log(`   ▸▸ ${line}`);
    currentCaption = line;
    await paintCaption();
  },
  fail(message) {
    failures.push(`${currentStage?.id}: ${message}`);
    log(`  ! ${message}`);
  },
  async click(locator) {
    try {
      await locator.waitFor({ state: "visible", timeout: 15000 });
      await locator.evaluate((el, behavior) => el.scrollIntoView({ behavior, block: "center" }), scrollBehavior);
      await beat(600);
      await locator.click();
      await beat(1200);
    } catch (e) {
      failures.push(`${currentStage?.id}: could not click (${e.message.split("\n")[0]})`);
      log(`  ! could not click: ${e.message.split("\n")[0]}`);
    }
  },
};

// ---------- presenter keys ----------
let keyResolver = null;
if (mode === "present") {
  readline.emitKeypressEvents(process.stdin);
  if (process.stdin.isTTY) process.stdin.setRawMode(true);
  process.stdin.on("keypress", (str, key) => {
    if (key && key.ctrl && key.name === "c") finish(130);
    if (!keyResolver) return;
    const name = key?.name;
    const action = name === "q" ? "quit" : name === "b" || name === "left" ? "back" : name === "r" ? "replay" : "next";
    const resolve = keyResolver;
    keyResolver = null;
    resolve(action);
  });
}
const waitForKey = () => new Promise((resolve) => (keyResolver = resolve));

// ---------- run ----------
let currentStage = null;
const timings = [];

async function finish(code) {
  const summary = guard.summary();
  log("\nNetwork guard:", JSON.stringify(summary));
  const denied = guard.deniedXrplCommandAttempts();
  if (denied.length) log("Dropped signing/submission commands:", denied);
  await context.close().catch(() => {});
  await browser.close().catch(() => {});
  if (mode === "record") {
    const video = readdirSync(outDir).find((f) => f.endsWith(".webm"));
    if (video) renameSync(join(outDir, video), join(outDir, "claw-street-demo.webm"));
  }
  if (mode === "record" || mode === "check") {
    writeFileSync(join(outDir, "run.json"), JSON.stringify({ mode, startedAt: stamp, timings, failures, guard: summary, deniedXrplCommandAttempts: denied }, null, 2));
    log(`Output: ${outDir}`);
  }
  if (failures.length) {
    log(`\n${failures.length} check(s) failed:`);
    for (const f of failures) log(`  - ${f}`);
  } else if (code === 0) {
    log("\nAll stage checks passed.");
  }
  process.exit(code === 0 && failures.length && mode === "check" ? 1 : code);
}

log(`Claw Street live demo · mode=${mode} · ${STAGES.length} stages · planned ${Math.floor(TOTAL_SECONDS / 60)}:${String(TOTAL_SECONDS % 60).padStart(2, "0")}`);
if (mode === "present") log("Keys: Enter/Space/→ next · r replay stage · b/← previous stage · q quit\n");

let i = startIndex;
const runStart = Date.now();
while (i < STAGES.length) {
  currentStage = STAGES[i];
  const s = currentStage;
  currentCaption = s.cue.join(" ");
  log(`\n━━ ${s.title}  (${s.seconds}s)`);
  for (const line of s.cue) log(`   ▸ ${line}`);
  await paintCaption();
  const t0 = Date.now();
  await s.run(h);
  if (mode === "check") await page.screenshot({ path: join(outDir, `${String(i + 1).padStart(2, "0")}-${s.id}.png`) });
  const actionSeconds = (Date.now() - t0) / 1000;
  if (mode === "rehearse" || mode === "record") {
    const remaining = s.seconds * 1000 * slow - (Date.now() - t0);
    if (remaining > 0) await page.waitForTimeout(remaining);
    else log(`   (actions took ${actionSeconds.toFixed(1)}s, over the ${s.seconds}s budget)`);
  }
  timings.push({ stage: s.id, actionSeconds: Number(actionSeconds.toFixed(1)), stageSeconds: Number(((Date.now() - t0) / 1000).toFixed(1)) });
  if (mode === "present") {
    log(i + 1 < STAGES.length ? `   ⏸  ready: press Enter for "${STAGES[i + 1].title}"` : "   ⏸  last stage: press Enter to finish");
    const action = await waitForKey();
    if (action === "quit") break;
    if (action === "replay") continue;
    if (action === "back") { i = Math.max(0, i - 1); continue; }
  }
  i += 1;
}
log(`\nTotal ${((Date.now() - runStart) / 1000).toFixed(1)}s`);
await finish(0);
