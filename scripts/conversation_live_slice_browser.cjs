"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const readline = require("node:readline");

let chromium;
try {
  ({ chromium } = require(process.env.LOCAL_AGENT_PLAYWRIGHT_MODULE || "playwright"));
} catch (error) {
  process.stderr.write("Conversation live slice requires Playwright.\n");
  throw error;
}

const MAX_PROTOCOL_LINE_CHARS = 128 * 1024;
const MAX_BOOTSTRAP_CHARS = 32_768;
const DEFAULT_LOGIN_TIMEOUT_MS = 10 * 60 * 1000;
const AUTH_SESSION_PROBE_TIMEOUT_MS = 1500;
const COMPOSER_WAIT_MS = 30_000;
const IDENTITY_WAIT_MS = 30_000;
const TX_RE = /^spawn-[0-9a-f]{64}$/;
const DIGEST_RE = /^sha256:[0-9a-f]{64}$/;
const CLAIM_PREFIX = "local-agent:conversation-spawn:";
const HASH_PREFIX = "la-spawn=";
const ACTIONS = new Set([
  "wait_ready", "create", "recover_create", "reattach_pre_submit", "probe",
  "submit", "reconcile", "open_child", "observe_child", "close_child", "shutdown"
]);

const spawnPages = new Map();
const childPages = new Map();
const pageIds = new WeakMap();
let nextPageId = 0;

function parseArgs(argv) {
  const result = { profile: "", headless: false };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--profile") result.profile = argv[++i] || "";
    else if (arg === "--extension") i += 1;
    else if (arg === "--headless") result.headless = true;
    else throw new Error(`unsupported live slice browser argument: ${arg}`);
  }
  if (!result.profile || !path.isAbsolute(result.profile)) throw new Error("--profile must be absolute");
  return result;
}

function executable(pathValue, field) {
  if (!path.isAbsolute(pathValue)) throw new Error(`${field} must be absolute`);
  const stat = fs.statSync(pathValue);
  if (!stat.isFile()) throw new Error(`${field} must point to a file`);
  fs.accessSync(pathValue, fs.constants.X_OK);
  return pathValue;
}

function chromeExecutable() {
  const explicit = String(process.env.LOCAL_AGENT_CONVERSATION_CHROME_EXECUTABLE || "").trim();
  if (explicit) return executable(explicit, "LOCAL_AGENT_CONVERSATION_CHROME_EXECUTABLE");
  if (process.platform === "darwin") {
    const branded = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
    if (fs.existsSync(branded)) return executable(branded, "Google Chrome executable");
  }
  const legacy = String(process.env.LOCAL_AGENT_CHROME_EXECUTABLE || "").trim();
  return legacy ? executable(legacy, "LOCAL_AGENT_CHROME_EXECUTABLE") : null;
}

function browserProfile(cliProfile, headless) {
  const explicit = String(process.env.LOCAL_AGENT_CONVERSATION_BROWSER_PROFILE || "").trim();
  let selected = explicit;
  if (!selected && !headless && process.platform === "darwin") {
    selected = path.join(
      os.homedir(), "Library", "Application Support",
      "local-agent-dev-stage8-cf-manual", "browser-profile", "live-slice"
    );
  }
  if (!selected) selected = cliProfile;
  if (!path.isAbsolute(selected)) throw new Error("conversation browser profile must be absolute");
  fs.mkdirSync(selected, { recursive: true });
  const stat = fs.lstatSync(selected);
  if (stat.isSymbolicLink() || !stat.isDirectory()) throw new Error("conversation browser profile must be a regular directory");
  return selected;
}

function jsonLine(payload) { return `${JSON.stringify(payload)}\n`; }
function reply(id, result) { process.stdout.write(jsonLine({ id, ok: true, result })); }
function fail(id, error) { process.stdout.write(jsonLine({ id, ok: false, error: String(error?.stack || error) })); }

function validateRequest(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("browser actuator request must be an object");
  if (!Number.isInteger(value.id) || value.id < 1) throw new Error("request id must be positive");
  const action = String(value.action || "");
  if (!ACTIONS.has(action)) throw new Error(`unsupported browser actuator action: ${action}`);
  return { id: value.id, action, payload: value.payload || {} };
}

function validateIntent(intent) {
  if (!intent || typeof intent !== "object" || Array.isArray(intent)) throw new Error("spawn intent must be an object");
  if (intent.schema_version !== 1) throw new Error("spawn schema_version must be 1");
  if (!TX_RE.test(String(intent.transaction_id || ""))) throw new Error("invalid spawn transaction_id");
  if (!DIGEST_RE.test(String(intent.child_request_digest || ""))) throw new Error("invalid child_request_digest");
  if (!DIGEST_RE.test(String(intent.bootstrap_digest || ""))) throw new Error("invalid bootstrap_digest");
  if (typeof intent.bootstrap_text !== "string" || !intent.bootstrap_text.trim() || intent.bootstrap_text.length > MAX_BOOTSTRAP_CHARS) {
    throw new Error("bootstrap_text must be bounded non-empty text");
  }
  if (intent.tab_id !== null && (!Number.isInteger(intent.tab_id) || intent.tab_id < 1)) throw new Error("tab_id must be positive or null");
  const digest = `sha256:${crypto.createHash("sha256").update(intent.bootstrap_text, "utf8").digest("hex")}`;
  if (digest !== intent.bootstrap_digest) throw new Error("bootstrap digest mismatch");
  return intent;
}

function composerSelector() {
  return ["#prompt-textarea", 'form [contenteditable="true"][data-lexical-editor="true"]', 'form [contenteditable="true"]', "form textarea"].join(",");
}

function submitSelector() {
  return ['button[data-testid="send-button"]', 'button[data-testid="composer-send-button"]', "#composer-submit-button", 'form button[type="submit"]'].join(",");
}

function canonicalHome(raw) {
  try {
    const u = new URL(String(raw || ""));
    return u.protocol === "https:" && ["chatgpt.com", "chat.openai.com"].includes(u.hostname) && (u.pathname.replace(/\/+$/, "") || "/") === "/";
  } catch (_error) { return false; }
}

function canonicalChild(raw) {
  try {
    const u = new URL(String(raw || ""));
    const match = u.pathname.replace(/\/+$/, "").match(/^\/c\/([A-Za-z0-9-]+)$/);
    if (u.protocol !== "https:" || !["chatgpt.com", "chat.openai.com"].includes(u.hostname) || !match || u.search || u.hash) return "";
    return `https://chatgpt.com/c/${match[1]}`;
  } catch (_error) { return ""; }
}

function claimKey(tx) { return `${CLAIM_PREFIX}${tx}`; }
function markerUrl(tx) { return `https://chatgpt.com/#${HASH_PREFIX}${encodeURIComponent(tx)}`; }

function markerTransaction(raw) {
  try {
    const u = new URL(String(raw || ""));
    if (!canonicalHome(u.href)) return "";
    const hash = u.hash.replace(/^#/, "");
    if (!hash.startsWith(HASH_PREFIX)) return "";
    const tx = decodeURIComponent(hash.slice(HASH_PREFIX.length));
    return TX_RE.test(tx) ? tx : "";
  } catch (_error) { return ""; }
}

function claim(intent, state, childUrl = "") {
  return {
    transactionId: intent.transaction_id,
    childRequestDigest: intent.child_request_digest,
    bootstrapDigest: intent.bootstrap_digest,
    state,
    childConversationUrl: childUrl
  };
}

async function writeClaim(page, intent, state, childUrl = "") {
  const value = claim(intent, state, childUrl);
  await page.evaluate(({ key, value }) => {
    const raw = JSON.stringify(value);
    sessionStorage.setItem(key, raw);
    localStorage.setItem(key, raw);
  }, { key: claimKey(intent.transaction_id), value });
}

async function readClaim(page, tx, storageName) {
  if (!page || page.isClosed()) return null;
  try {
    return await page.evaluate(({ key, storageName }) => {
      try {
        const raw = window[storageName].getItem(key);
        if (!raw) return null;
        const value = JSON.parse(raw);
        return value && typeof value === "object" ? value : null;
      } catch (_error) { return null; }
    }, { key: claimKey(tx), storageName });
  } catch (_error) { return null; }
}

function pageId(page) {
  const current = pageIds.get(page);
  if (current) return current;
  const id = ++nextPageId;
  pageIds.set(page, id);
  return id;
}

async function pageOwns(page, intent) {
  if (!page || page.isClosed()) return false;
  if (markerTransaction(page.url()) === intent.transaction_id) return true;
  const value = await readClaim(page, intent.transaction_id, "sessionStorage");
  return Boolean(value && value.transactionId === intent.transaction_id && value.childRequestDigest === intent.child_request_digest && value.bootstrapDigest === intent.bootstrap_digest);
}

async function findSpawnPage(context, intent) {
  const cached = spawnPages.get(intent.transaction_id);
  if (cached && await pageOwns(cached, intent)) return cached;
  spawnPages.delete(intent.transaction_id);

  if (intent.tab_id !== null) {
    for (const page of context.pages()) {
      if (pageIds.get(page) === intent.tab_id && await pageOwns(page, intent)) {
        spawnPages.set(intent.transaction_id, page);
        return page;
      }
    }
  }

  const matches = [];
  for (const page of context.pages()) if (await pageOwns(page, intent)) matches.push(page);
  if (matches.length > 1) throw new Error("multiple pages claim one spawn transaction");
  if (matches.length === 1) {
    spawnPages.set(intent.transaction_id, matches[0]);
    return matches[0];
  }
  return null;
}

async function createPage(context, rawIntent, reason = "tab_created") {
  const intent = validateIntent(rawIntent);
  const existing = await findSpawnPage(context, intent);
  if (existing) return { ok: true, reason: "tab_recovered", tabId: pageId(existing) };
  const page = await context.newPage();
  await page.goto(markerUrl(intent.transaction_id), { waitUntil: "domcontentloaded", timeout: 15_000 }).catch(() => null);
  if (!canonicalHome(page.url())) {
    await page.close().catch(() => null);
    return { ok: false, reason: "spawn_page_not_ready" };
  }
  await writeClaim(page, intent, "created");
  spawnPages.set(intent.transaction_id, page);
  return { ok: true, reason, tabId: pageId(page) };
}

async function recoverPage(context, rawIntent, reason = "tab_recreated") {
  const intent = validateIntent(rawIntent);
  const existing = await findSpawnPage(context, intent);
  if (existing) return { ok: true, reason: "tab_recovered", tabId: pageId(existing) };
  return createPage(context, { ...intent, tab_id: null }, reason);
}

async function waitComposer(page) {
  const deadline = Date.now() + COMPOSER_WAIT_MS;
  while (Date.now() < deadline) {
    if (canonicalHome(page.url()) && await page.locator(composerSelector()).first().isVisible().catch(() => false)) return true;
    await page.waitForTimeout(100);
  }
  return false;
}

async function probe(context, rawIntent) {
  const intent = validateIntent(rawIntent);
  let page = await findSpawnPage(context, intent);
  if (!page) {
    const recovery = await recoverPage(context, intent);
    if (!recovery.ok) return recovery;
    page = await findSpawnPage(context, { ...intent, tab_id: recovery.tabId });
  }
  if (!page) return { ok: false, reason: "spawn_tab_unavailable" };
  if (!canonicalHome(page.url())) return { ok: false, reason: "spawn_unexpected_route", route: page.url() };
  return await waitComposer(page)
    ? { ok: true, reason: "spawn_ready" }
    : { ok: false, reason: "spawn_composer_not_found" };
}

async function fillComposer(page, text) {
  const composer = page.locator(composerSelector()).first();
  await composer.waitFor({ state: "visible", timeout: COMPOSER_WAIT_MS });
  await composer.fill(text).catch(async () => {
    await page.evaluate(({ selector, value }) => {
      const element = document.querySelector(selector);
      if (!(element instanceof HTMLElement)) throw new Error("composer unavailable");
      element.focus();
      element.textContent = value;
      element.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
    }, { selector: composerSelector(), value: text });
  });
}

async function submit(context, rawIntent) {
  const intent = validateIntent(rawIntent);
  let page = await findSpawnPage(context, intent);
  if (!page) {
    const recovery = await recoverPage(context, intent);
    if (!recovery.ok) return recovery;
    page = await findSpawnPage(context, { ...intent, tab_id: recovery.tabId });
  }
  if (!page) return { ok: false, reason: "spawn_tab_unavailable" };

  const existing = canonicalChild(page.url());
  if (existing) return { ok: true, reason: "spawn_already_submitted", childConversationUrl: existing };
  if (!canonicalHome(page.url())) return { ok: false, reason: "spawn_unexpected_route", route: page.url() };

  await writeClaim(page, intent, "submitting");
  await fillComposer(page, intent.bootstrap_text);
  const button = page.locator(submitSelector()).first();
  const canClick = await button.isVisible().catch(() => false) && !await button.isDisabled().catch(() => false);
  if (canClick) await button.click();
  else await page.locator(composerSelector()).first().press("Enter");

  const deadline = Date.now() + IDENTITY_WAIT_MS;
  while (Date.now() < deadline) {
    const childUrl = canonicalChild(page.url());
    if (childUrl) {
      await writeClaim(page, intent, "submitted", childUrl).catch(() => null);
      return { ok: true, reason: "spawn_submitted", childConversationUrl: childUrl };
    }
    await page.waitForTimeout(100);
  }
  return { ok: false, reason: "spawn_identity_timeout", route: page.url() };
}

async function reconcile(context, rawIntent) {
  const intent = validateIntent(rawIntent);
  const page = await findSpawnPage(context, intent);
  if (page) {
    const direct = canonicalChild(page.url());
    if (direct) return { ok: true, reason: "spawn_identity_recovered", childConversationUrl: direct };
    const sessionValue = await readClaim(page, intent.transaction_id, "sessionStorage");
    const persistentValue = await readClaim(page, intent.transaction_id, "localStorage");
    const claimed = canonicalChild(sessionValue?.childConversationUrl || persistentValue?.childConversationUrl || "");
    if (claimed) return { ok: true, reason: "spawn_identity_recovered", childConversationUrl: claimed };
  }

  let probePage = context.pages().find((candidate) => canonicalHome(candidate.url())) || null;
  const created = !probePage;
  if (!probePage) {
    probePage = await context.newPage();
    await probePage.goto("https://chatgpt.com/", { waitUntil: "domcontentloaded", timeout: 10_000 }).catch(() => null);
  }
  try {
    const value = await readClaim(probePage, intent.transaction_id, "localStorage");
    const claimed = canonicalChild(value?.childConversationUrl || "");
    return claimed
      ? { ok: true, reason: "spawn_identity_recovered", childConversationUrl: claimed }
      : { ok: false, reason: "spawn_identity_unresolved" };
  } finally {
    if (created && !probePage.isClosed()) await probePage.close().catch(() => null);
  }
}

async function authenticated(page) {
  if (!canonicalHome(page.url())) return false;
  if (await page.locator('[data-testid="accounts-profile-button"]').count().catch(() => 0)) return true;
  try {
    return await page.evaluate(async (timeoutMs) => {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      try {
        const response = await fetch("/api/auth/session", { credentials: "include", cache: "no-store", signal: controller.signal });
        if (!response.ok) return false;
        const value = await response.json().catch(() => null);
        return Boolean(value && typeof value === "object" && value.user && typeof value.user === "object");
      } catch (_error) { return false; }
      finally { clearTimeout(timer); }
    }, AUTH_SESSION_PROBE_TIMEOUT_MS);
  } catch (_error) { return false; }
}

async function waitReady(context, timeoutMs) {
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 30 * 60 * 1000) throw new Error("invalid login timeout");
  const page = await context.newPage();
  try {
    await page.goto("https://chatgpt.com/", { waitUntil: "domcontentloaded", timeout: Math.min(timeoutMs, 5000) }).catch(() => null);
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (await authenticated(page)) return { ok: true, reason: "chatgpt_ready", url: page.url() };
      await page.waitForTimeout(500);
    }
    return { ok: false, reason: "chatgpt_login_timeout", url: page.url() };
  } finally {
    await page.close().catch(() => null);
  }
}

function ownership(payload) {
  const raw = payload?.ownership;
  if (raw === undefined || raw === null) return null;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("child ownership must be an object");
  if (!TX_RE.test(String(raw.transaction_id || "")) || !DIGEST_RE.test(String(raw.child_request_digest || "")) || !DIGEST_RE.test(String(raw.bootstrap_digest || ""))) {
    throw new Error("invalid child ownership");
  }
  return { transactionId: raw.transaction_id, childRequestDigest: raw.child_request_digest, bootstrapDigest: raw.bootstrap_digest };
}

function sameOwnership(a, b) {
  if (a === null || b === null) return a === b;
  return Boolean(a && b && a.transactionId === b.transactionId && a.childRequestDigest === b.childRequestDigest && a.bootstrapDigest === b.bootstrapDigest);
}

function childUrl(payload) {
  const raw = String(payload?.child_conversation_url || "");
  const canonical = canonicalChild(raw);
  if (!canonical || canonical !== raw) throw new Error("child lifecycle requires canonical child URL");
  return canonical;
}

async function ownedChild(context, payload, create) {
  const url = childUrl(payload);
  const wanted = ownership(payload);
  const cached = childPages.get(url);
  if (cached?.page && !cached.page.isClosed() && canonicalChild(cached.page.url()) === url) {
    if (!sameOwnership(cached.ownership, wanted)) return { ok: false, reason: "child_ownership_conflict" };
    return { ok: true, page: cached.page, source: "actuator_owned", url };
  }
  childPages.delete(url);

  if (wanted) {
    for (const page of context.pages().filter((candidate) => canonicalChild(candidate.url()) === url)) {
      const value = await readClaim(page, wanted.transactionId, "sessionStorage");
      if (value && value.childRequestDigest === wanted.childRequestDigest && value.bootstrapDigest === wanted.bootstrapDigest) {
        childPages.set(url, { page, ownership: wanted });
        return { ok: true, page, source: "spawn_claim", url };
      }
    }
  }

  if (!create) return { ok: false, reason: "child_owned_tab_missing" };
  const page = await context.newPage();
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 15_000 }).catch(() => null);
  if (canonicalChild(page.url()) !== url) {
    await page.close().catch(() => null);
    return { ok: false, reason: "child_route_not_ready" };
  }
  childPages.set(url, { page, ownership: wanted });
  return { ok: true, page, source: "observer_created", url };
}

async function openChild(context, payload) {
  const result = await ownedChild(context, payload, true);
  return result.ok
    ? { ok: true, reason: "child_opened", child_conversation_url: result.url, ownership_source: result.source }
    : { ok: false, reason: result.reason };
}

async function observeChild(context, payload) {
  const result = await ownedChild(context, payload, true);
  if (!result.ok) return { ok: false, reason: result.reason };
  const snapshot = await result.page.evaluate((maxChars) => {
    const visible = (element) => {
      if (!(element instanceof HTMLElement)) return false;
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
    };
    const generating = Array.from(document.querySelectorAll('button[data-testid="stop-button"], button[data-testid="composer-stop-button"]')).some(visible);
    let text = "";
    let identity = "";
    for (const turn of Array.from(document.querySelectorAll('[data-turn-key]')).reverse()) {
      const clone = turn.cloneNode(true);
      clone.querySelectorAll('[data-message-author-role="user"], [data-conversation-role="user"], [data-user-message-bubble], button, [role="button"], form, textarea, .sr-only').forEach((item) => item.remove());
      const value = String(clone.textContent || "").replace(/^\s*ChatGPT said:\s*/i, "").trim();
      if (!value) continue;
      text = value;
      identity = String(turn.getAttribute("data-turn-key") || "");
      break;
    }
    const truncated = text.length > maxChars;
    if (truncated) text = text.slice(0, maxChars);
    return { generating, text, identity, truncated };
  }, 16_384);
  const fallback = snapshot.text ? `assistant:${crypto.createHash("sha256").update(`${result.url}\n${snapshot.text}`, "utf8").digest("hex")}` : "";
  return {
    ok: true,
    reason: snapshot.generating ? "child_generating" : (snapshot.text ? "child_result_ready" : "child_result_missing"),
    child_conversation_url: result.url,
    assistant_identity: snapshot.identity || fallback,
    assistant_text: snapshot.text,
    truncated: snapshot.truncated === true
  };
}

async function closeChild(context, payload) {
  const result = await ownedChild(context, payload, false);
  if (!result.ok) {
    return result.reason === "child_owned_tab_missing"
      ? { ok: true, reason: "child_already_closed", child_conversation_url: childUrl(payload) }
      : { ok: false, reason: result.reason };
  }
  await result.page.close();
  childPages.delete(result.url);
  return { ok: true, reason: "child_closed", child_conversation_url: result.url };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const options = { headless: args.headless, args: ["--restore-last-session"] };
  const executablePath = chromeExecutable();
  if (executablePath) options.executablePath = executablePath;
  const context = await chromium.launchPersistentContext(browserProfile(args.profile, args.headless), options);

  let closing = false;
  const close = async () => {
    if (closing) return;
    closing = true;
    await context.close().catch(() => null);
  };

  const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  let chain = Promise.resolve();
  input.on("line", (line) => {
    if (line.length > MAX_PROTOCOL_LINE_CHARS) return;
    chain = chain.then(async () => {
      let request = null;
      try {
        request = validateRequest(JSON.parse(line));
        const intent = request.payload.intent;
        let result;
        if (request.action === "wait_ready") result = await waitReady(context, request.payload?.timeout_ms ?? DEFAULT_LOGIN_TIMEOUT_MS);
        else if (request.action === "create") result = await createPage(context, intent);
        else if (request.action === "recover_create") result = await recoverPage(context, intent);
        else if (request.action === "reattach_pre_submit") result = await recoverPage(context, intent, "tab_reattached");
        else if (request.action === "probe") result = await probe(context, intent);
        else if (request.action === "submit") result = await submit(context, intent);
        else if (request.action === "reconcile") result = await reconcile(context, intent);
        else if (request.action === "open_child") result = await openChild(context, request.payload);
        else if (request.action === "observe_child") result = await observeChild(context, request.payload);
        else if (request.action === "close_child") result = await closeChild(context, request.payload);
        else {
          result = { ok: true, reason: "shutdown" };
          reply(request.id, result);
          await close();
          input.close();
          return;
        }
        reply(request.id, result);
      } catch (error) {
        fail(request?.id || 1, error);
      }
    });
  });
  input.on("close", () => chain.finally(close).catch(() => null));
  process.on("SIGINT", () => close().finally(() => process.exit(130)));
  process.on("SIGTERM", () => close().finally(() => process.exit(143)));
}

main().catch((error) => {
  process.stderr.write(`${String(error?.stack || error)}\n`);
  process.exitCode = 1;
});
