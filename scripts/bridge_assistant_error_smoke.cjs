"use strict";

// Isolated Chromium fixture: never contacts ChatGPT or the operator's profile.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require(process.env.LOCAL_AGENT_PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "..");
const catalog = require(path.join(root, "chat_bridge/runtime.example.json"));

async function bounded(label, promise) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${label} exceeded 15 seconds`)), 15_000);
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
window.submits = 0;
window.retries = 0;
window.assistantErrorText = 'Message delivery timed out. Please try again.';
function addTimeout() {
  const message = document.createElement('div');
  message.dataset.messageAuthorRole = 'assistant';
  message.dataset.messageId = 'timeout-answer-' + window.submits;
  const error = document.createElement('div');
  error.className = 'text-token-text-error';
  error.innerHTML = '<p>' + window.assistantErrorText + '</p>';
  const retry = document.createElement('button');
  retry.dataset.testid = 'regenerate-thread-error-button';
  retry.textContent = 'Retry';
  retry.onclick = () => {
    window.retries++;
    message.remove();
    setTimeout(() => {
      const answer = document.createElement('div');
      answer.dataset.messageAuthorRole = 'assistant';
      answer.dataset.messageId = 'recovered-answer';
      answer.textContent = 'Recovered response';
      document.body.append(answer);
    }, 50);
  };
  error.append(retry);
  message.append(error);
  document.body.append(message);
}
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
  setTimeout(addTimeout, 500);
};
</script></body></html>`;

(async () => {
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), "bridge-assistant-error-"));
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
    await context.route("https://**/*", (route) => {
      if (route.request().url() === "https://chatgpt.com/c/assistant-error-fixture") {
        return route.fulfill({ contentType: "text/html", body: fixture });
      }
      return route.abort();
    });

    const worker = context.serviceWorkers()[0] || await context.waitForEvent("serviceworker");
    await bounded("runtime fixture setup", worker.evaluate((runtime) => {
      globalThis.fetch = async () => ({ ok: true, json: async () => runtime });
    }, catalog));
    const extensionId = new URL(worker.url()).host;
    const popup = await context.newPage();
    await popup.goto(`chrome-extension://${extensionId}/popup.html`);
    const request = (message) => bounded(message.type, popup.evaluate((value) => chrome.runtime.sendMessage(value), message));
    await request({
      type: "bridge:save-global-settings",
      settings: { runtimeUrl: `chrome-extension://${extensionId}/runtime.example.json` }
    });

    const page = await context.newPage();
    const url = "https://chatgpt.com/c/assistant-error-fixture";
    await page.goto(url);
    const matrix = catalog.agents.find((agent) => agent.repository_id === "example-hub");
    const added = await request({
      type: "bridge:upsert-conversation",
      conversation: {
        url,
        label: "assistant error fixture",
        agentBinding: matrix.agent_binding,
        enabled: true
      }
    });
    assert.equal(added.ok, true, added.error);

    const delivery = await request({ type: "bridge:run-now", conversationId: added.conversation.id });
    assert.equal(delivery.ok, true);
    assert.equal(delivery.reason, "sent");
    await page.waitForFunction(() => window.retries === 1, null, { timeout: 7000 });
    assert.equal(await page.evaluate(() => window.submits), 1, "Retry must not create a duplicate user message");
    assert.equal(await page.evaluate(() => window.retries), 1);
    await page.waitForFunction(() => document.body.textContent.includes("Recovered response"));

    let state = (await request({ type: "bridge:get-state" })).state;
    let conversation = state.conversations[added.conversation.id];
    assert.equal(conversation.enabled, true);
    assert.equal(conversation.lastStatus, "assistant_retry_1");

    await page.evaluate(() => {
      window.assistantErrorText = "Resume stream unavailable";
    });
    const resumeDelivery = await request({ type: "bridge:run-now", conversationId: added.conversation.id });
    assert.equal(resumeDelivery.ok, true);
    assert.equal(resumeDelivery.reason, "sent");
    await page.waitForFunction(() => window.retries === 2, null, { timeout: 7000 });
    assert.equal(await page.evaluate(() => window.submits), 2,
      "resume-stream Retry must not create a duplicate user message");
    assert.equal(await page.evaluate(() => window.retries), 2);

    state = (await request({ type: "bridge:get-state" })).state;
    conversation = state.conversations[added.conversation.id];
    assert.equal(conversation.enabled, true);
    assert.equal(conversation.lastStatus, "assistant_retry_1");
    console.log("PASS: timeout and resume-stream assistant errors recover through bounded native Retry");
  } finally {
    await context?.close();
    await fs.rm(profile, { recursive: true, force: true });
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
