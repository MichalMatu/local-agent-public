"use strict";

// Isolated Chromium fixtures: never contact ChatGPT or the operator's browser profile.
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

const fixtureShell = (mode) => `<!doctype html><html><body>
<div data-message-author-role="assistant" data-message-id="old-answer">Previous answer</div>
<form id="composer-form">
  <div id="prompt-textarea" contenteditable="true" data-lexical-editor="true"></div>
  <button id="composer-submit-button" type="submit">Send</button>
</form>
<script>
window.mode = ${JSON.stringify(mode)};
window.submits = 0;
window.retries = 0;
window.retryTimes = [];
function appendAssistant(id, text) {
  const answer = document.createElement('div');
  answer.dataset.messageAuthorRole = 'assistant';
  answer.dataset.messageId = id;
  answer.textContent = text;
  document.body.append(answer);
  return answer;
}
function addTimeout() {
  const message = document.createElement('div');
  message.dataset.messageAuthorRole = 'assistant';
  message.dataset.messageId = window.mode === 'same-node' ? 'timeout-same-node' : 'timeout-stale';
  const error = document.createElement('div');
  error.className = 'text-token-text-error';
  error.innerHTML = '<p>Message delivery timed out. Please try again.</p>';
  const retry = document.createElement('button');
  retry.dataset.testid = 'regenerate-thread-error-button';
  retry.textContent = 'Retry';
  retry.onclick = () => {
    window.retries++;
    window.retryTimes.push(Date.now());
    if (window.mode !== 'same-node') return;
    const stop = document.createElement('button');
    stop.dataset.testid = 'stop-button';
    stop.textContent = 'Stop';
    document.body.append(stop);
    const generationMs = window.retries === 1 ? 10000 : 300;
    setTimeout(() => stop.remove(), generationMs);
  };
  error.append(retry);
  message.append(error);
  document.body.append(message);
  if (window.mode === 'stale-card') {
    setTimeout(() => appendAssistant('newer-answer', 'A newer successful assistant answer'), 300);
  }
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
  setTimeout(addTimeout, 100);
};
</script></body></html>`;

(async () => {
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), "bridge-assistant-resilience-"));
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

    const staleUrl = "https://chatgpt.com/c/assistant-error-stale-fixture";
    const sameNodeUrl = "https://chatgpt.com/c/assistant-error-same-node-fixture";
    await context.route("https://**/*", (route) => {
      const url = route.request().url();
      if (url === staleUrl) return route.fulfill({ contentType: "text/html", body: fixtureShell("stale-card") });
      if (url === sameNodeUrl) return route.fulfill({ contentType: "text/html", body: fixtureShell("same-node") });
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
    const waitForConversation = async (chatId, predicate, timeoutMs = 25_000) => {
      const deadline = Date.now() + timeoutMs;
      while (Date.now() < deadline) {
        const state = (await request({ type: "bridge:get-state" })).state;
        const conversation = state.conversations?.[chatId] || null;
        if (predicate(conversation)) return conversation;
        await new Promise((resolve) => setTimeout(resolve, 500));
      }
      throw new Error(`conversation ${chatId} did not reach expected state within ${timeoutMs} ms`);
    };

    await request({
      type: "bridge:save-global-settings",
      settings: { runtimeUrl: `chrome-extension://${extensionId}/runtime.example.json` }
    });
    const matrix = catalog.agents.find((agent) => agent.repository_id === "example-hub");

    const stalePage = await context.newPage();
    await stalePage.goto(staleUrl);
    let added = await request({
      type: "bridge:upsert-conversation",
      conversation: {
        url: staleUrl,
        label: "stale timeout fixture",
        agentBinding: matrix.agent_binding,
        enabled: true
      }
    });
    assert.equal(added.ok, true, added.error);
    let delivery = await request({ type: "bridge:run-now", conversationId: added.conversation.id });
    assert.equal(delivery.ok, true);
    await stalePage.waitForFunction(() => document.body.textContent.includes("A newer successful assistant answer"));
    await stalePage.waitForTimeout(3000);
    assert.equal(await stalePage.evaluate(() => window.submits), 1);
    assert.equal(await stalePage.evaluate(() => window.retries), 0,
      "a stale timeout card behind a newer assistant turn must never be retried");
    await request({ type: "bridge:remove-conversation", conversationId: added.conversation.id });
    await stalePage.close();
    console.log("PASS: stale timeout card is ignored once a newer assistant turn exists");

    const sameNodePage = await context.newPage();
    await sameNodePage.goto(sameNodeUrl);
    added = await request({
      type: "bridge:upsert-conversation",
      conversation: {
        url: sameNodeUrl,
        label: "same-node timeout fixture",
        agentBinding: matrix.agent_binding,
        enabled: true
      }
    });
    assert.equal(added.ok, true, added.error);
    delivery = await request({ type: "bridge:run-now", conversationId: added.conversation.id });
    assert.equal(delivery.ok, true);

    await sameNodePage.waitForFunction(() => window.retries === 3, null, { timeout: 55_000 });
    const finalState = await waitForConversation(
      added.conversation.id,
      (conversation) => conversation?.lastStatus === "assistant_retry_exhausted" && conversation.enabled === false,
      20_000
    );
    assert.equal(finalState.lastStatus, "assistant_retry_exhausted");
    assert.equal(finalState.enabled, false);
    assert.equal(await sameNodePage.evaluate(() => window.submits), 1,
      "bounded Retry recovery must never duplicate the original Bridge user message");
    assert.equal(await sameNodePage.evaluate(() => window.retries), 3,
      "same-node timeout recovery must stop at the durable three-attempt cap");
    const retryTimes = await sameNodePage.evaluate(() => window.retryTimes.slice());
    assert.ok(retryTimes[1] - retryTimes[0] >= 14_000,
      "second Retry must wait for the long first generation to finish plus bounded backoff");
    assert.ok(retryTimes[2] - retryTimes[1] >= 22_000,
      "third Retry must retain the longer backoff after the second same-node failure");
    console.log("PASS: same timeout node survives a >8s generation, retries exactly three times and fails closed");
  } finally {
    await context?.close();
    await fs.rm(profile, { recursive: true, force: true });
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
