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
  const requestDigest = `sha256:${sha256(`request:${label}`)}`;
  const bootstrapText = [
    "LOCAL AGENT CHILD BOOTSTRAP",
    `child_request_digest=${requestDigest}`,
    `request=${label}`
  ].join("\n");
  return {
    schema_version: 1,
    transaction_id: `spawn-${sha256(`tx:${label}`)}`,
    child_request_digest: requestDigest,
    bootstrap_digest: `sha256:${sha256(bootstrapText)}`,
    bootstrap_text: bootstrapText,
    tab_id: tabId
  };
}

const fixture = `<!doctype html><html><body>
<main>
  <form>
    <div id="prompt-textarea" contenteditable="true" data-lexical-editor="true"></div>
    <button id="composer-submit-button" type="submit">Send</button>
  </form>
  <section id="thread"></section>
</main>
<script>
document.querySelector("form").addEventListener("submit", (event) => {
  event.preventDefault();
  const composer = document.querySelector("#prompt-textarea");
  const text = composer.innerText || composer.textContent || "";
  const turn = document.createElement("div");
  turn.dataset.turnKey = "collapsed-current-dom-user-turn";
  const user = document.createElement("div");
  user.setAttribute("data-user-message-bubble", "");
  const visible = document.createElement("span");
  visible.textContent = "…";
  const hidden = document.createElement("span");
  hidden.style.display = "none";
  hidden.textContent = text;
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "Show more";
  user.append(visible, hidden, button);
  turn.append(user);
  document.querySelector("#thread").append(turn);
  composer.textContent = "";
  history.pushState({}, "", "/c/synthetic-collapsed-child");
});
</script>
</body></html>`;

async function callWorker(worker, name, value) {
  assert.match(name, /^[A-Za-z][A-Za-z0-9_]*$/);
  return worker.evaluate(`Promise.resolve(${name}(${JSON.stringify(value)}))`);
}

(async () => {
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), "conversation-spawn-collapsed-"));
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
    const pending = spawnIntent("collapsed-identity");
    const created = await callWorker(worker, "createConversationSpawnTab", pending);
    assert.equal(created.ok, true, JSON.stringify(created));
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

    const result = await callWorker(worker, "submitConversationSpawnBootstrap", intent);
    assert.equal(result.ok, true, JSON.stringify(result));
    assert.equal(result.reason, "identity_discovered", JSON.stringify(result));
    assert.equal(result.childConversationUrl, "https://chatgpt.com/c/synthetic-collapsed-child");
    assert.equal(result.identitySource, "dom_contract_embedded_text", JSON.stringify(result));

    const page = context.pages().find((candidate) => candidate.url().includes("synthetic-collapsed-child"));
    assert.ok(page, "canonical synthetic child page must exist");
    const user = page.locator('[data-user-message-bubble]');
    assert.equal(await user.count(), 1);
    const visible = (await user.innerText()).trim().replace(/\s+/g, " ");
    assert.notEqual(visible, pending.bootstrap_text.trim().replace(/\s+/g, " "));
    const raw = (await user.textContent()).trim().replace(/\s+/g, " ");
    assert.equal(raw.includes(pending.bootstrap_text.trim().replace(/\s+/g, " ")), true);
    console.log("PASS: collapsed user bubble confirms exact bootstrap from embedded DOM text");
  } finally {
    if (context) await context.close().catch(() => null);
    await fs.rm(profile, { recursive: true, force: true });
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
