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

function spawnIntent(tabId = null) {
  const requestDigest = `sha256:${sha256("request:owned-route-identity")}`;
  const bootstrapText = [
    "LOCAL AGENT CHILD BOOTSTRAP",
    `child_request_digest=${requestDigest}`,
    "request=owned-route-identity"
  ].join("\n");
  return {
    schema_version: 1,
    transaction_id: `spawn-${sha256("tx:owned-route-identity")}`,
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
  const user = document.createElement("div");
  user.setAttribute("data-user-message-bubble", "");
  user.textContent = "COLLAPSED";
  document.querySelector("#thread").append(user);
  composer.textContent = "";
  history.pushState({}, "", "/uc/synthetic-owned-route");
  setTimeout(() => history.pushState({}, "", "/c/synthetic-owned-route-child"), 350);
});
</script>
</body></html>`;

async function callWorker(worker, name, value) {
  assert.match(name, /^[A-Za-z][A-Za-z0-9_]*$/);
  return worker.evaluate(`Promise.resolve(${name}(${JSON.stringify(value)}))`);
}

(async () => {
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), "conversation-spawn-owned-route-"));
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
    const pending = spawnIntent();
    const created = await callWorker(worker, "createConversationSpawnTab", pending);
    assert.equal(created.ok, true, JSON.stringify(created));
    const intent = { ...pending, tab_id: created.tabId };

    const deadline = Date.now() + 5000;
    let ready = null;
    while (Date.now() < deadline) {
      ready = await callWorker(worker, "ensureConversationSpawnContent", created.tabId);
      if (ready?.ok && ready?.route === "fresh" && ready?.readiness?.ok) break;
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    assert.equal(ready?.ok, true, JSON.stringify(ready));
    assert.equal(ready?.route, "fresh", JSON.stringify(ready));

    const result = await callWorker(worker, "submitConversationSpawnBootstrap", intent);
    assert.equal(result.ok, true, JSON.stringify(result));
    assert.equal(result.reason, "identity_discovered", JSON.stringify(result));
    assert.equal(result.childConversationUrl, "https://chatgpt.com/c/synthetic-owned-route-child");
    assert.equal(result.identitySource, "owned_provisional_canonical_transition", JSON.stringify(result));

    const page = context.pages().find((candidate) => candidate.url().includes("synthetic-owned-route-child"));
    assert.ok(page, "canonical synthetic child page must exist");
    assert.equal(await page.locator('[data-user-message-bubble]').count(), 1);
    assert.equal((await page.locator('[data-user-message-bubble]').innerText()).trim(), "COLLAPSED");
    console.log("PASS: exact submitted claim + provisional route + claimed canonical tab confirms child identity");
  } finally {
    if (context) await context.close().catch(() => null);
    await fs.rm(profile, { recursive: true, force: true });
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
