"use strict";

// Isolated Chromium fixture: never contacts ChatGPT or the operator's profile.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require(process.env.LOCAL_AGENT_PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "..");
const catalog = require(path.join(root, "chat_bridge/runtime.example.json"));

async function bounded(label, promise, timeoutMs = 15_000) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${label} exceeded ${timeoutMs} ms`)), timeoutMs);
    })]);
  } finally {
    clearTimeout(timer);
  }
}

async function waitUntil(label, predicate, timeoutMs = 10_000, intervalMs = 100) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  throw new Error(`${label} exceeded ${timeoutMs} ms`);
}

const fixture = `<!doctype html><html><body>
<div data-message-author-role="assistant" data-message-id="old-answer">Previous answer</div>
<div data-message-author-role="user" data-message-id="manual-user">Manual operator request</div>
<div data-message-author-role="assistant" data-message-id="manual-timeout">
  <div class="text-token-text-error">
    <p>Message delivery timed out. Please try again.</p>
    <button data-testid="regenerate-thread-error-button" id="manual-retry">Retry</button>
  </div>
</div>
<form id="composer-form">
  <div id="prompt-textarea" contenteditable="true" data-lexical-editor="true"></div>
  <button id="composer-submit-button" type="submit">Send</button>
</form>
<script>
window.submits = 0;
window.retries = 0;
document.querySelector('#manual-retry').onclick = () => { window.retries++; };
document.querySelector('form').onsubmit = (event) => {
  event.preventDefault();
  window.submits++;
  const input = document.querySelector('#prompt-textarea');
  const user = document.createElement('div');
  user.dataset.messageAuthorRole = 'user';
  user.dataset.messageId = 'bridge-user-' + window.submits;
  user.textContent = input.innerText || input.textContent || '';
  document.body.append(user);
  input.textContent = '';
  input.dispatchEvent(new Event('input', { bubbles: true }));
};
</script></body></html>`;

(async () => {
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), "bridge-assistant-unowned-"));
  let context;
  try {
    const extension = path.join(root, "chat_bridge");
    context = await chromium.launchPersistentContext(profile, {
      channel: "chromium",
      ignoreDefaultArgs: ["--disable-extensions"],
      headless: true,
      args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`]
    });
    await context.setOffline(true);

    const url = "https://chatgpt.com/c/assistant-error-unowned-fixture";
    await context.route("https://**/*", (route) => {
      if (route.request().url() === url) return route.fulfill({ contentType: "text/html", body: fixture });
      return route.abort();
    });

    const worker = context.serviceWorkers()[0] || await context.waitForEvent("serviceworker");
    await bounded("runtime fixture setup", worker.evaluate((runtime) => {
      globalThis.fetch = async () => ({ ok: true, json: async () => runtime });
    }, catalog));
    const extensionId = new URL(worker.url()).host;
    const popup = await context.newPage();
    await popup.goto(`chrome-extension://${extensionId}/popup.html`);
    const request = (message, timeoutMs = 15_000) => bounded(
      message.type,
      popup.evaluate((value) => chrome.runtime.sendMessage(value), message),
      timeoutMs
    );
    await request({
      type: "bridge:save-global-settings",
      settings: { runtimeUrl: `chrome-extension://${extensionId}/runtime.example.json` }
    });

    const page = await context.newPage();
    await page.goto(url);
    const matrix = catalog.agents.find((agent) => agent.repository_id === "example-hub");
    const added = await request({
      type: "bridge:upsert-conversation",
      conversation: {
        url,
        label: "operator timeout fixture",
        agentBinding: matrix.agent_binding,
        enabled: true
      }
    });
    assert.equal(added.ok, true, added.error);

    const blocked = await request({ type: "bridge:run-now", conversationId: added.conversation.id });
    assert.equal(blocked.ok, false);
    assert.equal(blocked.reason, "assistant_recovery_pending");
    assert.equal(await page.evaluate(() => window.submits), 0,
      "unresolved operator timeout must block a fresh Bridge wake");

    await waitUntil("operator timeout diagnostic state", async () => {
      const response = await request({ type: "bridge:get-state" });
      return response?.state?.conversations?.[added.conversation.id]?.lastStatus ===
        "assistant_delivery_timeout_unowned";
    });
    await page.waitForTimeout(2500);
    assert.equal(await page.evaluate(() => window.retries), 0,
      "operator-authored timeout must never be clicked automatically");

    await page.evaluate(() => document.querySelector('[data-message-id="manual-timeout"]')?.remove());
    const resumed = await request({ type: "bridge:run-now", conversationId: added.conversation.id });
    assert.equal(resumed.ok, true);
    assert.equal(resumed.reason, "sent");
    assert.equal(await page.evaluate(() => window.submits), 1,
      "normal wake may resume after the stale timeout card is gone");
    assert.equal(await page.evaluate(() => window.retries), 0);

    console.log("PASS: operator timeout is diagnostic-only, blocks overlap, and never auto-clicks Retry");
  } finally {
    await context?.close();
    await fs.rm(profile, { recursive: true, force: true });
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
