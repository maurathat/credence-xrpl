# Troubleshooting

Run `npm run check` first. It names the stage and the exact text or control that is missing, and saves one
screenshot per stage in `output/check-<time>/`.

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| `Executable doesn't exist … chromium` | Playwright's browser isn't installed | `npx playwright install chromium` |
| `npm ci` fails | No network, or an old npm | Use Node 20.11+; on a fresh machine, run `npm ci` before the event |
| `check` reports `expected text not visible: "…"` on a Claw Street stage | The live site changed (Lovable publishes from its `main`) | Open the page and compare it with the screenshot in `output/check-…`. Update the selector or text in `stages.mjs`. Every expectation lives there |
| `could not click` on **Reset** | Lovable's badge covers it | The runner dismisses the badge unless `--keep-badge` is set. If Lovable changes the badge, close it by hand with its **×** |
| `check` reports `unknown attack case "…"` | The site added or renamed an attack case | Add it to `ATTACK_LINES` in `stages.mjs` and to the table in `NARRATION.md` |
| The attack shown isn't "Over limit" | The site picks the attack case at random | Expected. Say the line the terminal prints after `▸▸` |
| Stage 4 numbers are wrong (not 351.94 / 29.6%) | The page was reloaded after the simulation | Press **r** to replay stage 4; it re-runs the simulation itself |
| The header still says "Available 362.40 tRLUSD" after the purchase | The site's header bar doesn't update from the simulation (site behaviour) | Point at the **Active facility** panel, which does update |
| Overview or Receipts don't show the new purchase | The simulation only changes the Credit page (site behaviour) | Narrate the receipts and history as the facility's seeded record |
| Explorer stage fails or is slow | testnet.xrpl.org or the Testnet nodes are slow or down; it's third-party | Press **Enter** to move on, or show `output/check-…/07-xrpl-testnet.png` or the backup video. The transaction is permanent, so it will be there later |
| Explorer shows a cookie banner | The guard blocks the consent script, so normally there's none | Click **Close this consent banner** (it's the explorer's own control) |
| Keys don't respond in presenter mode | The terminal running `npm run present` doesn't have focus | Click the terminal, then press Enter. The browser window doesn't take the keys |
| Presenter window is too small or too large | Display scaling | Use `--fullscreen`, or zoom the browser with ⌘+ / ⌘−. The runner doesn't fix the viewport in present mode |
| Recording is too fast for the narration | — | `npm run record -- --slow 1.2` stretches on-screen motion, or edit `seconds` per stage in `stages.mjs` |
| Wi-Fi is down at the venue | Everything in the demo is online | Play `output/record-…/claw-street-demo.webm`. Make a fresh recording the day of the event |
| `Dropped XRPL command …` in the log | Something on a page tried to send a signing or submission command | The guard blocked it. Nothing left the browser. Report the page and command |

## Before going on stage

1. `npm run check`: all stages pass.
2. `npm run selftest`: 5 PASS.
3. `npm run record`: a fresh backup video, played once to the end.
4. `npm run present -- --fullscreen`, then walk through with the keys once.
5. Close other browser windows and notifications. Keep the backup video open in a second window.
