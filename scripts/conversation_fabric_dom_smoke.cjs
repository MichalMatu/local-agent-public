"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require(process.env.LOCAL_AGENT_PLAYWRIGHT_MODULE || "playwright");

const root = path.resolve(__dirname, "..");
const bridge = path.join(root, "chat_bridge");
const parentUrl = "https://chatgpt.com/c/cf-parent-smoke";
const childUrl = "https://chatgpt.com/c/cf-child-smoke";
const completionMarkerRe = /LOCAL_AGENT_CF_CHILD_COMPLETE:[0-9a-f]{8}:[A-Za-z0-9._-]{1,64}:[0-9a-f]{8}/;

const parentFixture = `<!doctype html><html><body>
<form id="composer-form">
  <div id="prompt-textarea" contenteditable="true" role="textbox"></div>
  <button data-testid="composer-submit-button" type="submit">Send</button>
</form>
<div id="turns"></div>
<script>
window.__submitted = [];
window.__buttonClicks = 0;
window.__formSubmits = 0;
window.__editorInputEvents = 0;
window.__beforeInputEvents = 0;
window.__editorState = "";
window.__silentAcceptedClickDelay = 0;
window.__blockButtonSubmit = true;
document.execCommand = (command, _showUi, value) => {
  if (command !== "insertText") return false;
  const composer = document.querySelector("#prompt-textarea");
  composer.textContent = String(value ?? "");
  // Model the current-browser edge case where execCommand emits an input event but
  // application editor state still requires a fully-described explicit InputEvent.
  composer.dispatchEvent(new Event("input", { bubbles: true }));
  return true;
};
document.addEventListener("beforeinput", (event) => {
  if (event.target?.id !== "prompt-textarea") return;
  window.__beforeInputEvents += 1;
}, true);
document.addEventListener("input", (event) => {
  if (event.target?.id !== "prompt-textarea") return;
  window.__editorInputEvents += 1;
  if (!event.inputType) return;
  window.__editorState = String(event.target.innerText || event.target.textContent || "");
}, true);
window.__appendUserTurn = (text) => {
  if (!text) return;
  window.__submitted.push(text);
  const turn = document.createElement("div");
  turn.dataset.turnKey = "user-" + window.__submitted.length;
  const message = document.createElement("div");
  message.dataset.messageAuthorRole = "user";
  message.textContent = text;
  turn.appendChild(message);
  document.querySelector("#turns").appendChild(turn);
  const composer = document.querySelector("#prompt-textarea");
  composer.textContent = "";
  composer.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "deleteContent" }));
};
document.querySelector('[data-testid="composer-submit-button"]').addEventListener("click", (event) => {
  window.__buttonClicks += 1;
  if (window.__blockButtonSubmit) {
    event.preventDefault();
    return;
  }
  const delay = Number(window.__silentAcceptedClickDelay || 0);
  if (!delay) return;
  event.preventDefault();
  const text = String(window.__editorState || "").trim();
  setTimeout(() => window.__appendUserTurn(text), delay);
});
document.querySelector("#composer-form").addEventListener("submit", (event) => {
  window.__formSubmits += 1;
  event.preventDefault();
  const text = String(window.__editorState || "").trim();
  window.__appendUserTurn(text);
});
document.querySelector("#prompt-textarea").addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.shiftKey) return;
  const text = String(window.__editorState || "").trim();
  if (!text) return;
  event.preventDefault();
  window.__appendUserTurn(text);
});
</script>
</body></html>`;

const childFixture = `<!doctype html><html><body><div id="turns"></div></body></html>`;

async function installParentChromeStub(page) {
  await page.addInitScript(() => {
    window.__cfRuntimeMessages = [];
    const runtime = {
      async sendMessage(message) {
        window.__cfRuntimeMessages.push(JSON.parse(JSON.stringify(message)));
        return message.type === "bridge:conversation-fabric-feedback" ? { ok: true } :
          {
            ok: true,
            reason: "conversation_fabric_started",
            campaignId: "cf-1234567890abcdef",
            feedbackPrompt: String(globalThis.__nextFabricFeedbackPrompt || "FABRIC FEEDBACK")
          };
      },
      onMessage: { addListener() {}, removeListener() {} }
    };
    globalThis.chrome = globalThis.chrome || {};
    globalThis.chrome.runtime = runtime;
  });
}

async function installChildChromeStub(page) {
  await page.addInitScript(() => {
    const listeners = [];
    globalThis.chrome = globalThis.chrome || {};
    globalThis.chrome.runtime = {
      onMessage: {
        addListener(listener) { listeners.push(listener); },
        removeListener(listener) {
          const index = listeners.indexOf(listener);
          if (index >= 0) listeners.splice(index, 1);
        }
      }
    };
    window.__dispatchExtensionMessage = (message) => new Promise((resolve) => {
      let settled = false;
      const sendResponse = (response) => {
        if (settled) return;
        settled = true;
        resolve(response);
      };
      for (const listener of [...listeners]) {
        const keepAlive = listener(message, {}, sendResponse);
        if (keepAlive === true) return;
      }
      if (!settled) resolve(undefined);
    });
  });
}

async function runStreamingControlSmoke(context) {
  const page = await context.newPage();
  await installParentChromeStub(page);
  await page.goto(parentUrl, { waitUntil: "domcontentloaded" });
  for (const filename of [
    "control_protocol.js",
    "conversation_fabric_protocol.js",
    "content_retry.js",
    "conversation_fabric_content.js"
  ]) {
    await page.addScriptTag({ path: path.join(bridge, filename) });
  }

  const control = {
    schema_version: 1,
    action: "delegate",
    children: [{ id: "stream-check", role: "verification", prompt: "Verify one bounded topic." }]
  };
  await page.evaluate((control) => {
    const turn = document.createElement("div");
    turn.dataset.turnKey = "assistant-streaming-control";
    const message = document.createElement("div");
    message.id = "streaming-control-message";
    message.dataset.messageAuthorRole = "assistant";
    message.textContent = [
      "Starting delegation.",
      "<<<LOCAL_AGENT_CF",
      JSON.stringify(control)
    ].join("\n");
    turn.appendChild(message);
    document.querySelector("#turns").appendChild(turn);
  }, control);

  await page.waitForTimeout(900);
  assert.equal(
    await page.evaluate(() => window.__submitted.some(text => text.includes("control_close_missing"))),
    false,
    "first observation of a missing close marker must be treated as a possibly streaming assistant turn"
  );
  assert.equal(
    await page.evaluate(() => window.__cfRuntimeMessages.filter(message =>
      message.type === "bridge:conversation-fabric-control"
    ).length),
    0
  );

  await page.evaluate((control) => {
    document.querySelector("#streaming-control-message").textContent = [
      "Starting delegation.",
      "<<<LOCAL_AGENT_CF",
      JSON.stringify(control),
      "LOCAL_AGENT_CF>>>"
    ].join("\n");
  }, control);
  await page.waitForFunction(() => window.__cfRuntimeMessages.some(message =>
    message.type === "bridge:conversation-fabric-control"
  ));
  await page.waitForTimeout(250);
  assert.deepEqual(
    await page.evaluate(() => ({
      submitted: [...window.__submitted],
      composer: document.querySelector("#prompt-textarea")?.textContent || ""
    })),
    { submitted: [], composer: "" },
    "a completed streaming control must dispatch without any parent-composer feedback"
  );
  await page.close();
}

// Browser E2E for the exact-assistant terminal parsing fix: a ChatGPT turn may
// append toolbar/UI text after the assistant message, but that is not prose.
async function runTurnWrapperTerminalSmoke(context) {
  const page = await context.newPage();
  await installParentChromeStub(page);
  try {
    await page.goto(parentUrl, { waitUntil: "domcontentloaded" });
    for (const filename of [
      "control_protocol.js",
      "conversation_fabric_protocol.js",
      "content_retry.js",
      "conversation_fabric_content.js"
    ]) {
      await page.addScriptTag({ path: path.join(bridge, filename) });
    }

    const control = {
      schema_version: 1,
      action: "delegate",
      children: [{ id: "wrapper-check", role: "verification",
        prompt: "Verify that turn UI is not assistant prose." }]
    };
    await page.evaluate((value) => {
      const turn = document.createElement("section");
      turn.dataset.turnKey = "assistant-with-trailing-toolbar";
      const message = document.createElement("article");
      message.dataset.messageAuthorRole = "assistant";
      message.dataset.messageId = "assistant-with-toolbar-message";
      message.textContent = [
        "Delegation with a separate toolbar.",
        "<<<LOCAL_AGENT_CF",
        JSON.stringify(value),
        "LOCAL_AGENT_CF>>>"
      ].join("\n");
      const toolbar = document.createElement("aside");
      toolbar.id = "assistant-turn-toolbar";
      toolbar.textContent = "Copy Share Read aloud";
      turn.append(message, toolbar);
      document.querySelector("#turns").appendChild(turn);
    }, control);
    await page.waitForFunction(() => window.__cfRuntimeMessages.some(
      message => message.type === "bridge:conversation-fabric-control"
    ));
    const dispatched = await page.evaluate(() => window.__cfRuntimeMessages.filter(
      message => message.type === "bridge:conversation-fabric-control"
    ));
    assert.equal(dispatched.length, 1,
      "a valid terminal assistant control must dispatch despite sibling UI text");
    assert.equal(dispatched[0].control.action, "delegate");
    assert.equal(dispatched[0].control.children.length, 1);
    assert.equal(dispatched[0].assistantIdentity, "assistant-with-trailing-toolbar");

    // Changing only the turn toolbar cannot change the control signature
    // or trigger a second send even when the scanner re-observes the DOM.
    await page.evaluate(() => {
      document.querySelector("#assistant-turn-toolbar").textContent =
        "Copy Share Read aloud Updated toolbar";
    });
    await page.waitForTimeout(900);
    assert.deepEqual(await page.evaluate(() => ({
      controls: window.__cfRuntimeMessages.filter(
        message => message.type === "bridge:conversation-fabric-control"
      ).length,
      submitted: [...window.__submitted],
      composer: document.querySelector("#prompt-textarea")?.textContent || ""
    })), { controls: 1, submitted: [], composer: "" },
    "unrelated turn UI must not replay delegation or touch the composer");
  } finally {
    await page.close();
  }
}

async function runParentSmoke(context) {
  const page = await context.newPage();
  await installParentChromeStub(page);
  await page.goto(parentUrl, { waitUntil: "domcontentloaded" });
  for (const filename of [
    "control_protocol.js",
    "conversation_fabric_protocol.js",
    "content_retry.js",
    "conversation_fabric_content.js"
  ]) {
    await page.addScriptTag({ path: path.join(bridge, filename) });
  }

  const control = {
    schema_version: 1,
    action: "delegate",
    children: [
      { id: "audit", role: "research", prompt: "Audit one bounded topic." },
      { id: "verify", role: "verification", prompt: "Verify one independent topic." }
    ]
  };
  const malformedAssistantText = [
    "I am delegating two bounded reasoning jobs.",
    "<<<LOCAL_AGENT_CF",
    JSON.stringify(control),
    "LOCAL_AGENT_CF>>>",
    "This trailing prose makes the control unsupported."
  ].join("\n");
  await page.evaluate((text) => {
    const turn = document.createElement("div");
    turn.dataset.turnKey = "assistant-parent-control-invalid";
    const message = document.createElement("div");
    message.dataset.messageAuthorRole = "assistant";
    for (const line of text.split("\n")) {
      const paragraph = document.createElement("p");
      paragraph.textContent = line;
      message.appendChild(paragraph);
    }
    turn.appendChild(message);
    document.querySelector("#turns").appendChild(turn);
  }, malformedAssistantText);

  await page.waitForTimeout(900);
  assert.equal(
    await page.evaluate(() => window.__cfRuntimeMessages.filter(message =>
      message.type === "bridge:conversation-fabric-control"
    ).length),
    0,
    "visible malformed/non-terminal control must be diagnosed without Fabric control dispatch"
  );
  assert.deepEqual(
    await page.evaluate(() => ({
      submitted: [...window.__submitted],
      composer: document.querySelector("#prompt-textarea")?.textContent || ""
    })),
    { submitted: [], composer: "" },
    "malformed Fabric diagnostics must remain machine-only"
  );

  const assistantText = [
    "I am delegating two bounded reasoning jobs.",
    "<<<LOCAL_AGENT_CF",
    JSON.stringify(control),
    "LOCAL_AGENT_CF>>>"
  ].join("\n");
  await page.evaluate((text) => {
    const turn = document.createElement("div");
    turn.dataset.turnKey = "assistant-parent-control";
    const message = document.createElement("div");
    message.dataset.messageAuthorRole = "assistant";
    for (const line of text.split("\n")) {
      const paragraph = document.createElement("p");
      paragraph.textContent = line;
      message.appendChild(paragraph);
    }
    turn.appendChild(message);
    document.querySelector("#turns").appendChild(turn);
  }, assistantText);

  await page.waitForFunction(() => window.__cfRuntimeMessages.some(message =>
    message.type === "bridge:conversation-fabric-control"
  ));
  const runtimeMessage = await page.evaluate(() => window.__cfRuntimeMessages.find(message =>
    message.type === "bridge:conversation-fabric-control"
  ));
  assert.equal(runtimeMessage.type, "bridge:conversation-fabric-control");
  assert.equal(runtimeMessage.conversationUrl, parentUrl);
  assert.equal(runtimeMessage.control.action, "delegate");
  assert.equal(runtimeMessage.control.children.length, 2);
  assert.equal(runtimeMessage.contentProtocolVersion, require(path.join(bridge, "control_protocol.js")).CONTENT_PROTOCOL_VERSION);

  const markers = runtimeMessage.control.children.map((child) => {
    const match = String(child.prompt || "").match(completionMarkerRe);
    assert.ok(match, `missing completion marker for child ${child.id}`);
    assert.match(child.prompt, /Never emit this marker in progress/);
    return match[0];
  });
  assert.notEqual(markers[0], markers[1], "each child must have a unique completion marker");

  await page.waitForTimeout(250);
  const submitPath = await page.evaluate(() => ({
    submitted: [...window.__submitted],
    composer: document.querySelector("#prompt-textarea")?.textContent || "",
    buttonClicks: window.__buttonClicks,
    formSubmits: window.__formSubmits,
    editorInputEvents: window.__editorInputEvents,
    beforeInputEvents: window.__beforeInputEvents
  }));
  assert.deepEqual(submitPath.submitted, []);
  assert.equal(submitPath.composer, "");
  assert.equal(submitPath.buttonClicks, 0);
  assert.equal(submitPath.formSubmits, 0);
  assert.equal(submitPath.editorInputEvents, 0);
  assert.equal(submitPath.beforeInputEvents, 0);


  const beforeBlockedButton = await page.evaluate(() => ({
    submitted: window.__submitted.length,
    buttonClicks: window.__buttonClicks,
    formSubmits: window.__formSubmits
  }));
  await page.evaluate(() => {
    window.__nextFabricFeedbackPrompt = "FABRIC BLOCKED BUTTON REGRESSION";
    window.__blockButtonSubmit = true;
    const turn = document.createElement("div");
    turn.dataset.turnKey = "assistant-parent-blocked-button";
    const message = document.createElement("div");
    message.dataset.messageAuthorRole = "assistant";
    message.textContent = [
      "<<<LOCAL_AGENT_CF",
      JSON.stringify({
        schema_version: 1,
        action: "inspect",
        campaign_id: "cf-1234567890abcdef"
      }),
      "LOCAL_AGENT_CF>>>"
    ].join("\n");
    turn.appendChild(message);
    document.querySelector("#turns").appendChild(turn);
  });
  await page.waitForFunction(() => window.__cfRuntimeMessages.filter(message =>
    message.type === "bridge:conversation-fabric-control"
  ).length >= 2);
  await page.waitForTimeout(250);
  const blockedButton = await page.evaluate(() => ({
    submitted: window.__submitted.length,
    composer: document.querySelector("#prompt-textarea")?.textContent || "",
    buttonClicks: window.__buttonClicks,
    formSubmits: window.__formSubmits
  }));
  assert.equal(
    blockedButton.submitted,
    beforeBlockedButton.submitted,
    "Fabric worker feedback must not create a direct parent submit"
  );
  assert.equal(blockedButton.composer, "", "Fabric worker feedback must not leave a parent draft");
  assert.equal(blockedButton.buttonClicks, beforeBlockedButton.buttonClicks);
  assert.equal(blockedButton.formSubmits, beforeBlockedButton.formSubmits);

  await page.evaluate(() => {
    window.__blockButtonSubmit = true;
    window.__nextFabricFeedbackPrompt = "FABRIC EXACT OPERATOR DRAFT";
    const composer = document.querySelector("#prompt-textarea");
    composer.textContent = "FABRIC EXACT OPERATOR DRAFT";
    composer.dispatchEvent(new InputEvent("input", {
      bubbles: true,
      inputType: "insertText",
      data: "FABRIC EXACT OPERATOR DRAFT"
    }));
    const turn = document.createElement("div");
    turn.dataset.turnKey = "assistant-parent-exact-draft-collision";
    const message = document.createElement("div");
    message.dataset.messageAuthorRole = "assistant";
    message.textContent = [
      "<<<LOCAL_AGENT_CF",
      JSON.stringify({
        schema_version: 1,
        action: "inspect",
        campaign_id: "cf-1234567890abcdef"
      }),
      "LOCAL_AGENT_CF>>>"
    ].join("\n");
    turn.appendChild(message);
    document.querySelector("#turns").appendChild(turn);
  });
  await page.waitForFunction(() => window.__cfRuntimeMessages.filter(message =>
    message.type === "bridge:conversation-fabric-control"
  ).length >= 3);
  await page.waitForTimeout(250);
  assert.equal(
    await page.locator("#prompt-textarea").textContent(),
    "FABRIC EXACT OPERATOR DRAFT",
    "Fabric must preserve an exact byte-for-byte operator draft without same-instance ownership"
  );
  assert.equal(
    (await page.evaluate(() => window.__submitted)).length,
    blockedButton.submitted,
    "exact operator draft collision must not create a submit"
  );

  await page.close();
  return markers[0];
}

async function setChildAssistant(page, text, { generating = false } = {}) {
  await page.evaluate(({ text, generating }) => {
    const turns = document.querySelector("#turns");
    turns.textContent = "";
    const turn = document.createElement("div");
    turn.dataset.turnKey = "assistant-child-result";

    const user = document.createElement("div");
    user.dataset.messageAuthorRole = "user";
    user.textContent = "USER SECRET MUST NOT LEAK";
    turn.appendChild(user);

    const assistant = document.createElement("div");
    assistant.dataset.messageAuthorRole = "assistant";
    const body = document.createElement("div");
    body.className = "markdown";
    body.textContent = text;
    assistant.appendChild(body);
    const copy = document.createElement("button");
    copy.type = "button";
    copy.textContent = "Copy";
    assistant.appendChild(copy);
    turn.appendChild(assistant);
    turns.appendChild(turn);

    document.querySelectorAll('[data-testid="stop-button"], [data-testid="composer-stop-button"]').forEach((node) => node.remove());
    if (generating) {
      const stop = document.createElement("button");
      stop.dataset.testid = "stop-button";
      stop.textContent = "Stop";
      document.body.appendChild(stop);
    }
  }, { text, generating });
}

async function runChildSmoke(context, completionMarker) {
  const page = await context.newPage();
  await installChildChromeStub(page);
  await page.goto(childUrl, { waitUntil: "domcontentloaded" });
  const transactionId = `spawn-${"a".repeat(64)}`;
  const childRequestDigest = `sha256:${"b".repeat(64)}`;
  const bootstrapDigest = `sha256:${"c".repeat(64)}`;
  await page.evaluate(({ transactionId, childRequestDigest, bootstrapDigest }) => {
    sessionStorage.setItem(`local-agent:conversation-spawn:${transactionId}`, JSON.stringify({
      transactionId,
      childRequestDigest,
      bootstrapDigest,
      state: "submitted",
      childConversationUrl: location.href
    }));
  }, { transactionId, childRequestDigest, bootstrapDigest });

  for (const filename of ["control_protocol.js", "spawn_result_content.js"]) {
    await page.addScriptTag({ path: path.join(bridge, filename) });
  }
  const message = {
    type: "bridge:spawn-result",
    protocolVersion: 2,
    transactionId,
    childRequestDigest,
    bootstrapDigest,
    completionMarker
  };

  await setChildAssistant(page, "INTERMEDIATE AUDIT PROGRESS");
  await page.waitForTimeout(850);
  const pausedIntermediate = await page.evaluate(
    (payload) => window.__dispatchExtensionMessage(payload),
    message
  );
  assert.equal(pausedIntermediate.ok, true, JSON.stringify(pausedIntermediate));
  assert.equal(
    pausedIntermediate.reason,
    "child_generating",
    "stable intermediate output without a Stop button must remain pending after >700 ms"
  );

  await setChildAssistant(page, "INTERMEDIATE AUDIT PROGRESS\nTOOL WORK CONTINUES", { generating: true });
  const activeToolUse = await page.evaluate(
    (payload) => window.__dispatchExtensionMessage(payload),
    message
  );
  assert.equal(activeToolUse.ok, true, JSON.stringify(activeToolUse));
  assert.equal(activeToolUse.reason, "child_generating");

  // A plausible finished answer with the exact renderer-corrupted legacy
  // suffix (">>") must never be counted as completion.
  await setChildAssistant(page, "CHILD RESULT ALPHA\n>>");
  const brokenFooter = await page.evaluate(
    (payload) => window.__dispatchExtensionMessage(payload),
    message
  );
  assert.equal(brokenFooter.reason, "child_generating");

  // Reinstall the result listener to exercise extension/content-script reload recovery.
  await page.addScriptTag({ path: path.join(bridge, "spawn_result_content.js") });
  await setChildAssistant(page, `CHILD RESULT ALPHA\n${completionMarker}`);
  const result = await page.evaluate(
    (payload) => window.__dispatchExtensionMessage(payload),
    message
  );
  assert.equal(result.ok, true, JSON.stringify(result));
  assert.equal(result.reason, "child_result_ready");
  assert.equal(result.childConversationUrl, childUrl);
  assert.equal(result.assistantIdentity, "assistant-child-result");
  assert.equal(result.assistantText, "CHILD RESULT ALPHA");
  assert.doesNotMatch(result.assistantText, /LOCAL_AGENT_CF_CHILD_COMPLETE|USER SECRET|Copy/);

  // Already-issued, exactly claimed legacy children remain collectible.
  const legacyCompletionMarker = `<<<${completionMarker}>>>`;
  await setChildAssistant(page, `LEGACY CHILD RESULT\n${legacyCompletionMarker}`);
  const legacyResult = await page.evaluate(
    (payload) => window.__dispatchExtensionMessage(payload),
    { ...message, completionMarker: legacyCompletionMarker }
  );
  assert.equal(legacyResult.ok, true, JSON.stringify(legacyResult));
  assert.equal(legacyResult.reason, "child_result_ready");
  assert.equal(legacyResult.assistantText, "LEGACY CHILD RESULT");

  const rejected = await page.evaluate(
    (payload) => window.__dispatchExtensionMessage(payload),
    { ...message, childRequestDigest: `sha256:${"d".repeat(64)}` }
  );
  assert.equal(rejected.ok, false);
  assert.equal(rejected.reason, "spawn_claim_conflict");
  await page.close();
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  await context.route("https://chatgpt.com/**", async (route) => {
    const url = route.request().url();
    const body = url.includes("cf-child-smoke") ? childFixture : parentFixture;
    await route.fulfill({ status: 200, contentType: "text/html", body });
  });
  try {
    await runStreamingControlSmoke(context);
    await runTurnWrapperTerminalSmoke(context);
    const completionMarker = await runParentSmoke(context);
    await runChildSmoke(context, completionMarker);
    console.log("Conversation Fabric DOM smoke passed.");
  } finally {
    await context.close();
    await browser.close();
  }
})().catch((error) => {
  console.error(error?.stack || error);
  process.exitCode = 1;
});