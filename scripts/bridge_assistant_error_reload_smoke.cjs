"use strict";

// Isolated Chromium fixture: verifies retry identity/accounting across a full page reload.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require(process.env.LOCAL_AGENT_PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "..");
const catalog = require(path.join(root, "chat_bridge/runtime.example.json"));

async function bounded(label, promise, timeoutMs = 20_000) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${label} exceeded ${timeoutMs} ms`)), timeoutMs);
    })]);
  } finally {
    clearTimeout(timer);
  }
}

const fixture = `<!doctype html><html><body>
<div data-message-author-role="assistant" data-message-id="old-answer">Previous answer</div>
<form id="composer-form">
  <div id="prompt-textarea" contenteditable="true" data-lexical-editor="true"></div>
  <button id="composer-submit-button" type="submit">Send</button>
</form>
<script>
function count(key) { return Number(localStorage.getItem(key) || '0'); }
function appendUser(prompt) {
  const user = document.createElement('div');
  user.dataset.messageAuthorRole = 'user';
  user.dataset.messageId = 'bridge-user-rehydrated';
  user.textContent = prompt;
  document.body.append(user);
}
function appendRecovered() {
  const answer = document.createElement('div');
  answer.dataset.messageAuthorRole = 'assistant';
  answer.dataset.messageId = 'recovered-after-reload';
  answer.textContent = 'Recovered after page reload';
  document.body.append(answer);
}
function addTimeout() {
  const message = document.createElement('div');
  message.dataset.messageAuthorRole = 'assistant';
  message.dataset.messageId = 'timeout-rehydrated';
  const error = document.createElement('div');
  error.className = 'text-token-text-error';
  error.innerHTML = '<p>Message delivery timed out. Please try again.</p>';
  const retry = document.createElement('button');
  retry.dataset.testid = 'regenerate-thread-error-button';
  retry.textContent = 'Retry';
  retry.onclick = () => {
    const next = count('retryCount') + 1;
    localStorage.setItem('retryCount', String(next));
    if (next === 1) {
      setTimeout(() => location.reload(), 50);
      return;
    }
    message.remove();
    appendRecovered();
  };
  error.append(retry);
  message.append(error);
  document.body.append(message);
}
const retained = localStorage.getItem('bridgePrompt');
if (retained) {
  appendUser(retained);
  addTimeout();
}
document.querySelector('form').onsubmit = (event) => {
  event.preventDefault();
  const input = document.querySelector('#prompt-textarea');
  const prompt = input.innerText || input.textContent || '';
  localStorage.setItem('bridgePrompt', prompt);
  localStorage.setItem('submitCount', String(count('submitCount') + 1));
  appendUser(prompt);
  input.textContent = '';
  input.dispatchEvent(new Event('input', { bubbles: true }));
  setTimeout(addTimeout, 100);
};
</script></body></html>`;

(async () => {
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), "bridge-assistant-reload-"));
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

    const url = "https://chatgpt.com/c/assistant-error-reload-fixture";
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
        label: "assistant timeout reload fixture",
        agentBinding: matrix.agent_binding,
        enabled: true
      }
    });
    assert.equal(added.ok, true, added.error);

    const delivery = await request({ type: "bridge:run-now", conversationId: added.conversation.id });
    assert.equal(delivery.ok, true);
    assert.equal(delivery.reason, "sent");

    await page.waitForFunction(() => Number(localStorage.getItem('retryCount') || '0') >= 1, null, {
      timeout: 10_000
    });
    await page.waitForFunction(() => Number(localStorage.getItem('retryCount') || '0') === 2, null, {
      timeout: 20_000
    });
    await page.waitForFunction(() => document.body.textContent.includes('Recovered after page reload'), null, {
      timeout: 5000
    });

    assert.equal(await page.evaluate(() => Number(localStorage.getItem('submitCount') || '0')), 1,
      "page reload recovery must not submit the Bridge user prompt again");
    assert.equal(await page.evaluate(() => Number(localStorage.getItem('retryCount') || '0')), 2,
      "durable retry accounting must continue with attempt two after page reload");

    const state = (await request({ type: "bridge:get-state" })).state;
    const conversation = state.conversations[added.conversation.id];
    assert.equal(conversation.enabled, true);
    assert.equal(conversation.lastStatus, "assistant_retry_2");
    console.log("PASS: page reload preserves timeout identity and resumes at retry attempt two without resubmission");
  } finally {
    await context?.close();
    await fs.rm(profile, { recursive: true, force: true });
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
