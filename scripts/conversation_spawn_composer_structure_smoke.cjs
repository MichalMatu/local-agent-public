"use strict";

// Real Chromium + real Chat Bridge regression for a ChatGPT composer rerender that keeps
// the exact logical bootstrap while changing the contenteditable DOM into block elements.
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
  const bootstrapText = [
    "LOCAL AGENT CHILD BOOTSTRAP",
    "{",
    '  "request": "composer-structure-smoke",',
    '  "scope": "exact logical text only"',
    "}",
    ""
  ].join("\n");
  return {
    schema_version: 1,
    transaction_id: `spawn-${sha256("composer-structure-transaction")}`,
    child_request_digest: `sha256:${sha256("composer-structure-request")}`,
    bootstrap_digest: `sha256:${sha256(bootstrapText)}`,
    bootstrap_text: bootstrapText,
    tab_id: tabId
  };
}

function markerUrl(intent) {
  return `https://chatgpt.com/#la-spawn=${intent.transaction_id}`;
}

const fixture = `<!doctype html><html><body>
<form id="composer-form">
  <div id="prompt-textarea" class="ProseMirror" contenteditable="true" role="textbox"></div>
  <button id="composer-submit-button" type="submit" disabled>Send</button>
</form>
<script>
window.submits = 0;
window.blockRenders = 0;
window.logicalBootstrap = "";
window.browserEncodedBootstrap = "";
window.expectedLogicalBootstrap = "";
window.normalizedOnce = false;

function currentComposerText(element) {
  return element.innerText || element.textContent || "";
}

function renderAsBlocks(element, text) {
  const replacement = element.cloneNode(false);
  for (const line of text.split("\\n")) {
    const paragraph = document.createElement("p");
    if (line) paragraph.textContent = line;
    else paragraph.append(document.createElement("br"));
    replacement.append(paragraph);
  }
  element.replaceWith(replacement);
  return replacement;
}

document.addEventListener("input", (event) => {
  if (window.normalizedOnce || event.target?.id !== "prompt-textarea") return;
  const current = event.target;
  const browserText = currentComposerText(current);
  const logicalText = window.expectedLogicalBootstrap || browserText;
  if (!logicalText) return;
  window.normalizedOnce = true;
  window.browserEncodedBootstrap = browserText;
  window.logicalBootstrap = logicalText;
  renderAsBlocks(current, logicalText);
  history.replaceState({}, "", "/?mweb_fallback=1" + location.hash);
  window.blockRenders++;
  window.renderedParagraphCount = document.querySelectorAll("#prompt-textarea > p").length;
}, true);

document.querySelector("form").onsubmit = (event) => {
  event.preventDefault();
  window.submits++;
  const message = document.createElement("div");
  message.dataset.messageAuthorRole = "user";
  message.textContent = window.logicalBootstrap;
  document.body.append(message);
  document.querySelector("#prompt-textarea").replaceChildren();
  history.pushState({}, "", "/c/synthetic-composer-structure");
};
</script></body></html>`;

async function bounded(label, promise, timeoutMs = 15_000) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`${label} exceeded ${timeoutMs} ms`)), timeoutMs);
      })
    ]);
  } finally {
    clearTimeout(timer);
  }
}

(async () => {
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), "conversation-composer-structure-"));
  let context;
  try {
    const extension = path.join(root, "chat_bridge");
    context = await chromium.launchPersistentContext(profile, {
      channel: "chromium",
      headless: true,
      args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`]
    });
    await context.setOffline(true);
    await context.route("https://**/*", (route) => {
      if (route.request().url().startsWith("https://chatgpt.com/")) {
        return route.fulfill({ status: 200, contentType: "text/html", body: fixture });
      }
      return route.abort();
    });

    const worker = context.serviceWorkers()[0] || await bounded(
      "extension service worker",
      context.waitForEvent("serviceworker"),
      8000
    );
    const callWorker = async (name, value, timeoutMs = 15_000) => {
      assert.match(name, /^[A-Za-z][A-Za-z0-9_]*$/);
      return bounded(
        `worker ${name}`,
        worker.evaluate(`Promise.resolve(${name}(${JSON.stringify(value)}))`),
        timeoutMs
      );
    };

    const pending = spawnIntent();
    const page = await context.newPage();
    await page.goto(markerUrl(pending));
    assert.equal(page.url(), markerUrl(pending));

    const recovered = await callWorker("createConversationSpawnTab", pending);
    assert.equal(recovered.ok, true, JSON.stringify(recovered));
    assert.equal(recovered.reason, "tab_recovered", JSON.stringify(recovered));
    assert.ok(Number.isInteger(recovered.tabId));

    await page.evaluate((bootstrapText) => {
      window.expectedLogicalBootstrap = bootstrapText;
    }, pending.bootstrap_text);
    const durable = { ...pending, tab_id: recovered.tabId };
    const delivered = callWorker("submitConversationSpawnBootstrap", durable);

    await page.waitForFunction(() => window.blockRenders === 1);
    assert.equal(new URL(page.url()).searchParams.get("mweb_fallback"), "1");
    assert.equal(await page.evaluate(() => window.logicalBootstrap), pending.bootstrap_text);
    assert.notEqual(await page.evaluate(() => window.browserEncodedBootstrap), pending.bootstrap_text);
    assert.equal(
      await page.evaluate(() => window.browserEncodedBootstrap.includes("\u00a0")),
      true,
      "Chromium should exercise NBSP encoding for JSON indentation"
    );
    assert.equal(await page.evaluate(() => window.renderedParagraphCount), pending.bootstrap_text.split("\n").length);

    await page.evaluate(() => { document.querySelector("#composer-submit-button").disabled = false; });
    const result = await delivered;
    assert.equal(result.ok, true, JSON.stringify(result));
    assert.equal(result.reason, "identity_discovered", JSON.stringify(result));
    assert.equal(result.childConversationUrl, "https://chatgpt.com/c/synthetic-composer-structure");
    assert.equal(await page.evaluate(() => window.submits), 1);

    console.log("Conversation spawn block-structured composer rerender smoke passed.");
  } finally {
    if (context) await context.close().catch(() => null);
    await fs.rm(profile, { recursive: true, force: true });
  }
})().catch((error) => {
  console.error(error?.stack || error);
  process.exitCode = 1;
});
