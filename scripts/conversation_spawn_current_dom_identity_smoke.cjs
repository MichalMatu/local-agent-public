"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require(process.env.LOCAL_AGENT_PLAYWRIGHT_MODULE || "playwright");

const root = path.resolve(__dirname, "..");

function sha256(text) {
  return crypto.createHash("sha256").update(String(text), "utf8").digest("hex");
}

function spawnIntent(label, tabId = null) {
  const bootstrapText = [
    "LOCAL AGENT CHILD BOOTSTRAP",
    `request=${label}`,
    `nonce=${sha256(label).slice(0, 16)}`
  ].join("\n");
  return {
    schema_version: 1,
    transaction_id: `spawn-${sha256(`tx:${label}`)}`,
    child_request_digest: `sha256:${sha256(`request:${label}`)}`,
    bootstrap_digest: `sha256:${sha256(bootstrapText)}`,
    bootstrap_text: bootstrapText,
    tab_id: tabId
  };
}

const fixture = `<!doctype html><html><body>
<form>
  <div id="prompt-textarea" contenteditable="true" data-lexical-editor="true"></div>
  <button id="composer-submit-button" type="submit">Send</button>
</form>
<script>
document.querySelector("form").addEventListener("submit", (event) => {
  event.preventDefault();
  const composer = document.querySelector("#prompt-textarea");
  const text = composer.innerText || composer.textContent || "";
  const turn = document.createElement("div");
  turn.dataset.turnKey = "current-dom-user-turn";
  const user = document.createElement("div");
  user.setAttribute("data-user-message-bubble", "");
  user.textContent = text;
  turn.append(user);
  document.body.append(turn);
  composer.textContent = "";
  history.pushState({}, "", "/c/synthetic-current-dom-child");
});
</script>
</body></html>`;

async function callWorker(worker, name, value) {
  assert.match(name, /^[A-Za-z][A-Za-z0-9_]*$/);
  return worker.evaluate(`Promise.resolve(${name}(${JSON.stringify(value)}))`);
}

(async () => {
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), "conversation-spawn-current-dom-"));
  let context;
  try {
    const extension = path.join(root, "chat_bridge");
    const executablePath = process.env.LOCAL_AGENT_CHROME_EXECUTABLE || undefined;
    context = await chromium.launchPersistentContext(profile, {
      ...(executablePath ? { executablePath } : { channel: "chromium" }),
      headless: true,
      args: [
        `--disable-extensions-except=${extension}`,
        `--load-extension=${extension}`
      ]
    });
    await context.setOffline(true);
    await context.route("https://chatgpt.com/**", (route) =>
      route.fulfill({ contentType: "text/html", body: fixture })
    );

    const worker = context.serviceWorkers()[0] || await context.waitForEvent("serviceworker");
    const pending = spawnIntent("current-dom-identity");
    const created = await callWorker(worker, "createConversationSpawnTab", pending);
    assert.equal(created.ok, true, JSON.stringify(created));
    assert.ok(Number.isInteger(created.tabId), JSON.stringify(created));
    const intent = { ...pending, tab_id: created.tabId };

    const readyDeadline = Date.now() + 5000;
    let ready = null;
    while (Date.now() < readyDeadline) {
      ready = await callWorker(worker, "ensureConversationSpawnContent", created.tabId);
      if (ready?.ok && ready?.route === "fresh" && ready?.readiness?.ok) break;
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    assert.equal(ready?.ok, true, JSON.stringify(ready));
    assert.equal(ready?.route, "fresh", JSON.stringify(ready));
    assert.equal(ready?.readiness?.ok, true, JSON.stringify(ready));

    const result = await callWorker(worker, "submitConversationSpawnBootstrap", intent);
    assert.equal(result.ok, true, JSON.stringify(result));
    assert.equal(result.reason, "identity_discovered", JSON.stringify(result));
    assert.equal(
      result.childConversationUrl,
      "https://chatgpt.com/c/synthetic-current-dom-child",
      JSON.stringify(result)
    );
    assert.equal(result.identitySource, "dom_contract", JSON.stringify(result));

    const page = context.pages().find((candidate) => candidate.url().includes("synthetic-current-dom-child"));
    assert.ok(page, "canonical synthetic child page must exist");
    assert.equal(await page.locator('[data-message-author-role="user"]').count(), 0);
    assert.equal(await page.locator('[data-user-message-bubble]').count(), 1);
    console.log("PASS: current data-user-message-bubble DOM confirms exact spawned child identity");
  } finally {
    if (context) await context.close().catch(() => null);
    await fs.rm(profile, { recursive: true, force: true });
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
