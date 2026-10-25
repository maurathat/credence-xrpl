// Network safety guard for the Claw Street live demo.
//
// The demo only reads. This guard enforces that in the browser itself:
// - HTTP(S): only GET, HEAD and OPTIONS, and only to the hosts the demo needs. Everything else (analytics, ads,
//   consent beacons, any POST) is aborted.
// - WebSockets: only the public XRPL Testnet nodes the explorer reads from. Every outgoing XRPL command is inspected,
//   and commands that sign, submit or create keys are dropped before they leave the browser.
// Every blocked request is recorded, and the run summary prints it.

export const ALLOWED_HOSTS = new Set([
  "creds-for-ai.lovable.app",
  "fonts.googleapis.com",
  "fonts.gstatic.com",
  "testnet.xrpl.org",
]);

const READ_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
const XRPL_TESTNET_WS = /^wss:\/\/(s|clio)\.altnet\.rippletest\.net(:\d+)?\/?$/;

// XRPL API methods that sign, submit, create keys or change server state. None is needed to view a transaction.
export const DENIED_XRPL_COMMANDS = new Set([
  "submit",
  "submit_multisigned",
  "sign",
  "sign_for",
  "wallet_propose",
  "channel_authorize",
  "validation_create",
  "ledger_accept",
  "connect",
  "stop",
  "can_delete",
  "ledger_cleaner",
  "log_level",
  "logrotate",
  "peer_reservations_add",
  "peer_reservations_del",
]);

export async function installGuard(context, log) {
  const blocked = [];
  const xrplCommands = new Map();

  await context.route("**/*", (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (url.protocol === "data:" || url.protocol === "blob:") return route.continue();
    if (!READ_METHODS.has(req.method()) || !ALLOWED_HOSTS.has(url.hostname)) {
      blocked.push(`${req.method()} ${url.host}${url.pathname}`.slice(0, 120));
      return route.abort("blockedbyclient");
    }
    return route.continue();
  });

  await context.routeWebSocket(/.*/, (ws) => {
    if (!XRPL_TESTNET_WS.test(ws.url())) {
      blocked.push(`WS ${ws.url()}`);
      ws.close({ code: 1008, reason: "blocked by demo guard" });
      return;
    }
    const server = ws.connectToServer();
    ws.onMessage((message) => {
      let command = "(unparsed)";
      try {
        const parsed = JSON.parse(typeof message === "string" ? message : message.toString());
        command = String(parsed.command ?? parsed.method ?? "(none)");
      } catch {
        // Unparseable frames are not XRPL commands; drop them.
        blocked.push(`WS frame (unparseable) to ${ws.url()}`);
        return;
      }
      xrplCommands.set(command, (xrplCommands.get(command) ?? 0) + 1);
      if (DENIED_XRPL_COMMANDS.has(command)) {
        blocked.push(`WS ${command} to ${ws.url()}`);
        log?.(`guard: dropped XRPL command "${command}"`);
        return;
      }
      server.send(message);
    });
  });

  return {
    summary() {
      const counts = {};
      for (const b of blocked) {
        const key = b.split("/")[0];
        counts[key] = (counts[key] ?? 0) + 1;
      }
      return { blockedTotal: blocked.length, blockedByTarget: counts, xrplCommandsSeen: Object.fromEntries(xrplCommands) };
    },
    deniedXrplCommandAttempts() {
      return blocked.filter((b) => b.startsWith("WS ") && [...DENIED_XRPL_COMMANDS].some((c) => b.startsWith(`WS ${c} `)));
    },
  };
}
