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
window.submits = 0;
document.querySelector("form").addEventListener("submit", (event) => {
  event.preventDefault();
  window.submits++;
  const composer = document.querySelector("#prompt-textarea");
  const text = composer.innerText || composer.textContent || "";
  const appendUserMessage = () => {
    const message = document.createElement("div");
    message.dataset.messageAuthorRole = "user";
    message.textContent = text;
    document.body.append(message);
  };
  const delayedUserMessage = text.includes("request=child-message-delayed");
  if (delayedUserMessage) setTimeout(appendUserMessage, 6_500);
  else appendUserMessage();
  composer.textContent = "";
  if (delayedUserMessage) {
    history.pushState({}, "", "/c/synthetic-message-delayed-child");
    return;
  }
  const localPlaceholder = text.includes("request=local-to-child");
  history.pushState(
    {},
    "",
    localPlaceholder
      ? "/c/local-chatgpt%3A65b0b0eb-b67b-415a-a1e5-92715bc5855f"
      : "/uc/transient-synthetic-child"
  );
  const delayed = text.includes("request=uc-to-child-delayed") || text.includes("request=local-to-child-delayed");
  const target = text.includes("request=local-to-child")
    ? "/c/synthetic-local-final-child"
    : delayed
      ? "/c/synthetic-delayed-final-child"
      : text.includes("request=uc-to-child")
        ? "/c/synthetic-final-child"
        : text.includes("request=uc-to-root")
          ? "/"
          : "/unexpected-after-uc";
  setTimeout(() => history.pushState({}, "", target), text.includes("request=local-to-child-delayed") ? 6_500 : delayed ? 11_000 : 350);
});
</script>
</body></html>`;

async function callWorker(worker, name, value) {
  assert.match(name, /^[A-Za-z][A-Za-z0-9_]*$/);
  return worker.evaluate(`Promise.resolve(${name}(${JSON.stringify(value)}))`);
}

async function prepareSpawn(worker, pending) {
  const created = await callWorker(worker, "createConversationSpawnTab", pending);
  assert.equal(created.ok, true, JSON.stringify(created));
  assert.ok(Number.isInteger(created.tabId), JSON.stringify(created));
  const value = { ...pending, tab_id: created.tabId };
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) {
    const ready = await callWorker(worker, "ensureConversationSpawnContent", created.tabId);
    if (ready?.ok && ready?.route === "fresh" && ready?.readiness?.ok) {
      return { value, tabId: created.tabId };
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error("spawn fixture did not become ready");
}

(async () => {
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), "conversation-spawn-uc-transition-"));
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

    const diagnostic = await prepareSpawn(worker, spawnIntent("uc-diagnostic"));
    const diagnosticPage = context.pages()[context.pages().length - 1];
    assert.equal(new URL(diagnosticPage.url()).pathname, "/");
    await diagnosticPage.evaluate((intent) => {
      sessionStorage.setItem(
        `local-agent:conversation-spawn:${intent.transaction_id}`,
        JSON.stringify({
          transactionId: intent.transaction_id,
          childRequestDigest: intent.child_request_digest,
          bootstrapDigest: intent.bootstrap_digest,
          state: "submitted"
        })
      );
      const user = document.createElement("div");
      user.dataset.messageAuthorRole = "user";
      user.textContent = intent.bootstrap_text;
      document.body.append(user);
      const assistant = document.createElement("div");
      assistant.dataset.messageAuthorRole = "assistant";
      assistant.textContent = "Synthetic assistant output";
      document.body.append(assistant);
      const stop = document.createElement("button");
      stop.dataset.testid = "stop-button";
      stop.textContent = "Stop";
      document.body.append(stop);
      history.pushState({}, "", "/uc/synthetic-diagnostic");
    }, diagnostic.value);
    const diagnosticResult = await callWorker(
      worker,
      "inspectConversationSpawnContentState",
      diagnostic.value
    );
    assert.equal(diagnosticResult.ok, true, JSON.stringify(diagnosticResult));
    assert.equal(diagnosticResult.reason, "spawn_provisional_diagnostic");
    assert.equal(diagnosticResult.claimState, "submitted");
    assert.equal(diagnosticResult.contentRoute, "provisional");
    assert.equal(diagnosticResult.exactUserMessage, true);
    assert.equal(diagnosticResult.userMessageCount, 1);
    assert.equal(diagnosticResult.assistantMessageCount, 1);
    assert.equal(diagnosticResult.assistantGenerating, true);
    assert.ok(diagnosticResult.latestAssistantTextLength > 0);
    assert.equal(diagnosticResult.composerPresent, true);
    assert.equal(diagnosticResult.composerHasText, false);
    assert.equal(diagnosticResult.sendButtonReady, true);
    assert.equal(diagnosticResult.childConversationUrl, undefined);
    await worker.evaluate(`chrome.tabs.remove(${diagnostic.tabId})`);
    console.log("PASS: provisional /uc diagnostic reports submit and assistant state without accepting identity");

    const success = await prepareSpawn(worker, spawnIntent("uc-to-child"));
    const successResult = await callWorker(worker, "submitConversationSpawnBootstrap", success.value);
    assert.equal(successResult.ok, true, JSON.stringify(successResult));
    assert.equal(successResult.reason, "identity_discovered", JSON.stringify(successResult));
    assert.equal(successResult.childConversationUrl, "https://chatgpt.com/c/synthetic-final-child");
    assert.ok(!successResult.childConversationUrl.includes("/uc/"));
    console.log("PASS: transient /uc route settles to canonical /c before child identity is accepted");

    const messageDelayed = await prepareSpawn(worker, spawnIntent("child-message-delayed"));
    const messageDelayedStarted = Date.now();
    const messageDelayedResult = await callWorker(
      worker,
      "submitConversationSpawnBootstrap",
      messageDelayed.value
    );
    assert.equal(messageDelayedResult.ok, true, JSON.stringify(messageDelayedResult));
    assert.equal(messageDelayedResult.reason, "identity_discovered", JSON.stringify(messageDelayedResult));
    assert.equal(
      messageDelayedResult.childConversationUrl,
      "https://chatgpt.com/c/synthetic-message-delayed-child"
    );
    assert.ok(
      Date.now() - messageDelayedStarted >= 6000,
      "canonical child identity must wait for delayed exact user message"
    );
    console.log("PASS: canonical /c waits for delayed user-message DOM before accepting identity");

    const local = await prepareSpawn(worker, spawnIntent("local-to-child"));
    const localResult = await callWorker(worker, "submitConversationSpawnBootstrap", local.value);
    assert.equal(localResult.ok, true, JSON.stringify(localResult));
    assert.equal(localResult.reason, "identity_discovered", JSON.stringify(localResult));
    assert.equal(localResult.childConversationUrl, "https://chatgpt.com/c/synthetic-local-final-child");
    assert.ok(!localResult.childConversationUrl.includes("local-chatgpt"));
    console.log("PASS: local-chatgpt placeholder settles to canonical /c before identity is accepted");

    const localDelayed = await prepareSpawn(worker, spawnIntent("local-to-child-delayed"));
    const localDelayedResult = await callWorker(worker, "submitConversationSpawnBootstrap", localDelayed.value);
    assert.equal(localDelayedResult.ok, true, JSON.stringify(localDelayedResult));
    assert.equal(localDelayedResult.reason, "identity_discovered", JSON.stringify(localDelayedResult));
    assert.equal(localDelayedResult.childConversationUrl, "https://chatgpt.com/c/synthetic-local-final-child");
    console.log("PASS: local-chatgpt placeholder can outlive the 5s content wait and settle via worker fallback");

    const delayed = await prepareSpawn(worker, spawnIntent("uc-to-child-delayed"));
    const delayedResult = await callWorker(worker, "submitConversationSpawnBootstrap", delayed.value);
    assert.equal(delayedResult.ok, true, JSON.stringify(delayedResult));
    assert.equal(delayedResult.reason, "identity_discovered", JSON.stringify(delayedResult));
    assert.equal(
      delayedResult.childConversationUrl,
      "https://chatgpt.com/c/synthetic-delayed-final-child"
    );
    assert.ok(!delayedResult.childConversationUrl.includes("/uc/"));
    console.log("PASS: /uc may remain provisional beyond the former 10s bound and still settle canonically");

    const rootRoute = await prepareSpawn(worker, spawnIntent("uc-to-root"));
    const rootResult = await callWorker(worker, "submitConversationSpawnBootstrap", rootRoute.value);
    assert.equal(rootResult.ok, false, JSON.stringify(rootResult));
    assert.equal(rootResult.reason, "spawn_submission_ambiguous", JSON.stringify(rootResult));
    assert.equal(rootResult.route, "unexpected_after_provisional", JSON.stringify(rootResult));
    assert.equal(rootResult.childConversationUrl, undefined);
    console.log("PASS: /uc returning to fresh root remains fail-closed");

    const rejected = await prepareSpawn(worker, spawnIntent("uc-to-unexpected"));
    const rejectedResult = await callWorker(worker, "submitConversationSpawnBootstrap", rejected.value);
    assert.equal(rejectedResult.ok, false, JSON.stringify(rejectedResult));
    assert.equal(rejectedResult.reason, "spawn_submission_ambiguous", JSON.stringify(rejectedResult));
    assert.equal(rejectedResult.route, "unexpected_after_provisional", JSON.stringify(rejectedResult));
    assert.equal(rejectedResult.childConversationUrl, undefined);
    console.log("PASS: transient /uc route never becomes child identity without canonical /c");
  } finally {
    if (context) await context.close().catch(() => null);
    await fs.rm(profile, { recursive: true, force: true });
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
