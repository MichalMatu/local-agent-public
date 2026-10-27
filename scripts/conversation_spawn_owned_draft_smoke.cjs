"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const path = require("node:path");
const { chromium } = require(process.env.LOCAL_AGENT_PLAYWRIGHT_MODULE || "playwright");

const root = path.resolve(__dirname, "..");
const BOOTSTRAP_INSTRUCTIONS = [
  "Treat this bootstrap as the complete admitted startup authority for this child conversation.",
  "Work only from the immutable request fields and content-addressed context references below.",
  "Do not substitute browser state, a moved repository ref, or unreferenced transcript history for admitted authority."
];

function sha256(text) {
  return crypto.createHash("sha256").update(String(text), "utf8").digest("hex");
}

function canonicalJson(value) {
  if (value === null) return "null";
  if (typeof value === "string" || typeof value === "boolean" || typeof value === "number") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  return `{${Object.keys(value).sort().map((key) => (
    `${JSON.stringify(key)}:${canonicalJson(value[key])}`
  )).join(",")}}`;
}

function ownedBootstrap(label) {
  const request = {
    agent_binding: "b0e94f93-478f-4fc6-abed-159d3702362f",
    bootstrap_contract_version: 1,
    context_refs: [],
    id: `owned-draft-${label}`,
    parent_conversation_url: "https://chatgpt.com/c/parent-owned-draft",
    repository_commit_sha: "d65cbaacf70ee272e4263ab0e73428aa1376210e",
    repository_id: "local-agent",
    repository_ref: "develop/conversation-fabric",
    role: "verification",
    schema_version: 2,
    scope: { summary: `Owned draft ${label}` },
    workflow_id: "stage8-live-slice",
    workflow_node_id: "stage8-live-child",
    workflow_node_introduction_digest: `sha256:${sha256(`node:${label}`)}`,
    workflow_node_revision: 1
  };
  const record = {
    bootstrap_contract_version: 1,
    child_request_digest: `sha256:${sha256(canonicalJson(request))}`,
    instructions: BOOTSTRAP_INSTRUCTIONS,
    request
  };
  return `LOCAL AGENT CHILD BOOTSTRAP\n${JSON.stringify(record, null, 2)}\n`;
}

function spawnMessage(label) {
  const bootstrapText = `LOCAL AGENT CHILD BOOTSTRAP\ncurrent=${label}`;
  return {
    type: "bridge:spawn-bootstrap",
    transactionId: `spawn-${sha256(`transaction:${label}`)}`,
    childRequestDigest: `sha256:${sha256(`request:${label}`)}`,
    bootstrapDigest: `sha256:${sha256(bootstrapText)}`,
    bootstrapText
  };
}

const fixture = `<!doctype html><html><body>
<form id="composer-form">
  <div id="prompt-textarea" class="ProseMirror" contenteditable="true" role="textbox"></div>
  <button id="composer-submit-button" type="submit">Send</button>
</form>
<script>
window.submits = 0;
document.querySelector("#composer-form").addEventListener("submit", (event) => {
  event.preventDefault();
  window.submits++;
  const composer = document.querySelector("#prompt-textarea");
  const message = document.createElement("div");
  message.dataset.messageAuthorRole = "user";
  message.textContent = composer.innerText || composer.textContent || "";
  document.body.append(message);
  composer.replaceChildren();
  history.pushState({}, "", "/c/synthetic-owned-draft");
});
</script>
</body></html>`;

async function installSpawnContent(page) {
  await page.goto("https://chatgpt.com/", { waitUntil: "domcontentloaded" });
  await page.evaluate(() => {
    globalThis.LocalAgentBridgeProtocol = {
      normalizeConversationUrl(value) {
        try {
          const parsed = new URL(value);
          if (parsed.protocol !== "https:" || parsed.hostname !== "chatgpt.com") return null;
          if (parsed.search || parsed.hash) return null;
          const match = parsed.pathname.match(/^\/c\/([A-Za-z0-9_-]{1,200})\/?$/);
          return match ? `https://chatgpt.com/c/${match[1]}` : null;
        } catch (_error) {
          return null;
        }
      }
    };
    globalThis.__spawnListener = null;
    Object.defineProperty(globalThis, "chrome", {
      configurable: true,
      value: {
        runtime: {
          onMessage: {
            addListener(listener) { globalThis.__spawnListener = listener; },
            removeListener(listener) {
              if (globalThis.__spawnListener === listener) globalThis.__spawnListener = null;
            }
          }
        }
      }
    });
  });
  await page.addScriptTag({ path: path.join(root, "chat_bridge", "spawn_content.js") });
  assert.equal(await page.evaluate(() => typeof globalThis.__spawnListener), "function");
}

async function send(page, message) {
  return page.evaluate((payload) => new Promise((resolve, reject) => {
    let responded = false;
    const timer = setTimeout(() => reject(new Error("spawn content response timeout")), 10_000);
    const done = (response) => {
      if (responded) return;
      responded = true;
      clearTimeout(timer);
      resolve(response);
    };
    try {
      const asyncResponse = globalThis.__spawnListener(payload, {}, done);
      if (asyncResponse !== true && !responded) {
        clearTimeout(timer);
        reject(new Error("spawn content listener did not respond"));
      }
    } catch (error) {
      clearTimeout(timer);
      reject(error);
    }
  }), message);
}

async function setComposer(page, text) {
  await page.locator("#prompt-textarea").evaluate((composer, value) => {
    composer.textContent = value;
  }, text);
}

async function composerText(page) {
  return page.locator("#prompt-textarea").evaluate((composer) => (
    composer.innerText || composer.textContent || ""
  ));
}

(async () => {
  const launchOptions = { headless: true };
  if (process.env.LOCAL_AGENT_CHROME_EXECUTABLE) {
    launchOptions.executablePath = process.env.LOCAL_AGENT_CHROME_EXECUTABLE;
  }
  const browser = await chromium.launch(launchOptions);
  const context = await browser.newContext();
  await context.route("https://chatgpt.com/**", (route) => route.fulfill({
    status: 200,
    contentType: "text/html",
    body: fixture
  }));

  try {
    const ownedPage = await context.newPage();
    await installSpawnContent(ownedPage);
    const staleDraft = ownedBootstrap("stale");
    await setComposer(ownedPage, staleDraft);

    const ownedProbe = await send(ownedPage, { type: "bridge:spawn-capabilities" });
    assert.equal(ownedProbe.ok, true, JSON.stringify(ownedProbe));
    assert.equal(ownedProbe.readiness.ok, true, JSON.stringify(ownedProbe));
    assert.equal(ownedProbe.readiness.reason, "spawn_ready", JSON.stringify(ownedProbe));
    assert.equal(ownedProbe.readiness.ownedBootstrapDraft, true, JSON.stringify(ownedProbe));

    const current = spawnMessage("current");
    const submitted = await send(ownedPage, current);
    assert.equal(submitted.ok, true, JSON.stringify(submitted));
    assert.equal(submitted.reason, "identity_discovered", JSON.stringify(submitted));
    assert.equal(submitted.childConversationUrl, "https://chatgpt.com/c/synthetic-owned-draft");
    assert.equal(await ownedPage.evaluate(() => window.submits), 1);
    assert.equal(
      await ownedPage.locator('[data-message-author-role="user"]').last().textContent(),
      current.bootstrapText
    );

    const arbitraryPage = await context.newPage();
    await installSpawnContent(arbitraryPage);
    const arbitraryDraft = "keep this operator draft exactly";
    await setComposer(arbitraryPage, arbitraryDraft);
    const arbitraryProbe = await send(arbitraryPage, { type: "bridge:spawn-capabilities" });
    assert.equal(arbitraryProbe.ok, true, JSON.stringify(arbitraryProbe));
    assert.equal(arbitraryProbe.readiness.ok, false, JSON.stringify(arbitraryProbe));
    assert.equal(arbitraryProbe.readiness.reason, "spawn_composer_not_empty", JSON.stringify(arbitraryProbe));
    const arbitrarySubmit = await send(arbitraryPage, spawnMessage("arbitrary"));
    assert.equal(arbitrarySubmit.ok, false, JSON.stringify(arbitrarySubmit));
    assert.equal(arbitrarySubmit.reason, "spawn_composer_not_empty", JSON.stringify(arbitrarySubmit));
    assert.equal(await composerText(arbitraryPage), arbitraryDraft);
    assert.equal(await arbitraryPage.evaluate(() => window.submits), 0);

    const malformedPage = await context.newPage();
    await installSpawnContent(malformedPage);
    const malformedDraft = "LOCAL AGENT CHILD BOOTSTRAP\n{\"bootstrap_contract_version\":1}";
    await setComposer(malformedPage, malformedDraft);
    const malformedProbe = await send(malformedPage, { type: "bridge:spawn-capabilities" });
    assert.equal(malformedProbe.ok, true, JSON.stringify(malformedProbe));
    assert.equal(malformedProbe.readiness.ok, false, JSON.stringify(malformedProbe));
    assert.equal(malformedProbe.readiness.reason, "spawn_composer_not_empty", JSON.stringify(malformedProbe));
    assert.equal(
      (await composerText(malformedPage)).replace(/\s+/g, " "),
      malformedDraft.replace(/\s+/g, " ")
    );
    assert.equal(await malformedPage.evaluate(() => window.submits), 0);

    console.log("Conversation spawn owned stale draft smoke passed.");
  } finally {
    await context.close().catch(() => null);
    await browser.close().catch(() => null);
  }
})().catch((error) => {
  console.error(error?.stack || error);
  process.exitCode = 1;
});
