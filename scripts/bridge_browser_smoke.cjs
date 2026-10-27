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
      timer = setTimeout(() => reject(new Error(`${label} exceeded 10 seconds`)), 10_000);
    })]);
  } finally { clearTimeout(timer); }
}

const fixture = `<!doctype html><html><body>
<div data-message-author-role="assistant" data-message-id="old-answer">Previous answer</div>
<form id="composer-form">
  <div id="prompt-textarea" class="ProseMirror" contenteditable="true" role="textbox"></div>
  <button id="composer-submit-button" type="submit">Send</button>
</form>
<script>
window.submits = 0;
window.dropDelivery = false;
window.replaceComposerOnSubmit = false;
window.currentUserDom = false;
window.virtualizeUserTurns = false;
document.querySelector('form').onsubmit = (event) => {
  event.preventDefault();
  window.submits++;
  if (window.dropDelivery) return;
  const form = document.querySelector('#composer-form');
  const input = form.querySelector('#prompt-textarea');
  const message = document.createElement('div');
  if (window.currentUserDom) message.dataset.userMessageBubble = '';
  else message.dataset.messageAuthorRole = 'user';
  message.textContent = input.innerText || input.textContent || '';
  message.dataset.messageId = 'submitted-' + window.submits;
  if (window.virtualizeUserTurns) document.querySelectorAll('[data-message-author-role="user"], [data-user-message-bubble]').forEach(node => node.remove());
  document.body.append(message);
  if (window.replaceComposerOnSubmit) {
    form.innerHTML = '<div id="prompt-textarea" class="ProseMirror" contenteditable="true" role="textbox"></div><button id="composer-submit-button" type="submit">Send</button>';
    return;
  }
  input.textContent = '';
  input.dispatchEvent(new Event('input', { bubbles: true }));
};
</script></body></html>`;

(async () => {
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), "bridge-browser-"));
  let context;
  try {
    const extension = path.join(root, "chat_bridge");
    const launch = () => chromium.launchPersistentContext(profile, {
      channel: "chromium",
      ignoreDefaultArgs: ["--disable-extensions"], headless: true,
      args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`]
    });
    context = await launch();
    await context.setOffline(true);
    const installRoutes = () => context.route("https://**/*", (route) => {
      if (route.request().url().startsWith("https://chatgpt.com/c/bridge-fixture")) {
        return route.fulfill({ contentType: "text/html", body: fixture });
      }
      return route.abort();
    });
    await installRoutes();
    const worker = context.serviceWorkers()[0] || await context.waitForEvent("serviceworker");
    const mockRuntime = async () => bounded("runtime fixture setup", worker.evaluate((runtime) => {
      globalThis.fetch = async () => ({ ok: true, json: async () => runtime });
    }, catalog));
    await mockRuntime();
    const extensionId = new URL(worker.url()).host;
    let page = await context.newPage();
    let popup = await context.newPage();
    await popup.goto(`chrome-extension://${extensionId}/popup.html`);
    const request = (message) => bounded(message.type, popup.evaluate((value) => chrome.runtime.sendMessage(value), message));
    const localRuntimeUrl = `chrome-extension://${extensionId}/runtime.example.json`;
    const runtimeSetup = await request({
      type: "bridge:save-global-settings",
      settings: { runtimeUrl: localRuntimeUrl }
    });
    assert.equal(runtimeSetup?.ok, true, runtimeSetup?.error || "could not persist local runtime fixture");
    const readChat = async (id) => (await request({ type: "bridge:get-state" })).state.conversations[id];
    const popupMetrics = () => popup.evaluate(() => {
      const rect = document.body.getBoundingClientRect();
      const scroll = document.querySelector(".content-scroll");
      return {
        width: Math.round(rect.width),
        height: Math.round(rect.height),
        clientHeight: Math.round(scroll.clientHeight),
        scrollHeight: Math.round(scroll.scrollHeight)
      };
    });
    const add = async (suffix) => {
      const url = `https://chatgpt.com/c/bridge-fixture-${suffix}`;
      await page.goto(url);
      const result = await request({ type: "bridge:upsert-conversation", conversation: {
        url,
        label: suffix,
        agentBinding: catalog.agents.find((agent) => agent.repository_id === "example-tracker").agent_binding,
        enabled: false
      } });
      assert.equal(result.ok, true, result.error);
      return result.conversation.id;
    };
    const run = (id) => request({ type: "bridge:run-now", conversationId: id });
    const prepare = () => page.evaluate(() => { document.querySelector("button").disabled = true; });
    const waitInsertion = () => page.waitForFunction(() => document.querySelector("#prompt-textarea").textContent.length > 0);
    const composerText = () => page.locator("#prompt-textarea").textContent();

    const compactPopup = await popupMetrics();
    assert.equal(compactPopup.width, 420);
    assert.ok(compactPopup.height < 560, `compact popup should shrink to content, got ${compactPopup.height}px`);
    assert.ok(
      compactPopup.scrollHeight <= compactPopup.clientHeight + 1,
      `compact popup should not scroll: ${compactPopup.scrollHeight}/${compactPopup.clientHeight}`
    );

    const settingsSummary = popup.locator(".settings-panel summary");
    await settingsSummary.click();
    await popup.waitForFunction(
      (height) => document.body.getBoundingClientRect().height > height,
      compactPopup.height
    );
    const advancedPopup = await popupMetrics();
    assert.equal(advancedPopup.width, 420);
    assert.ok(advancedPopup.height <= 600, `advanced popup exceeded 600px: ${advancedPopup.height}`);
    assert.equal(await popup.locator(".current-chat-panel").isVisible(), false);
    assert.equal(await popup.locator(".conversation-section").isVisible(), false);
    assert.ok(
      advancedPopup.scrollHeight <= advancedPopup.clientHeight + 1,
      `advanced view should use available height before scrolling: ${advancedPopup.scrollHeight}/${advancedPopup.clientHeight}`
    );
    await settingsSummary.click();
    await popup.waitForFunction(
      (height) => document.body.getBoundingClientRect().height <= height + 1,
      compactPopup.height
    );
    console.log("PASS: compact popup grows into a dedicated Advanced view without unnecessary scrolling");

    let id = await add("success");
    const successId = id;
    assert.equal((await run(id)).reason, "sent");
    assert.equal(await page.evaluate(() => window.submits), 1);
    assert.match(await page.locator('[data-message-author-role="user"]').innerText(), /LA_CHAT=chat-[0-9a-f]{8}/);
    assert.equal((await readChat(id)).bootstrapPending, false);
    console.log("PASS: confirmed contenteditable form delivery and transport-only chat envelope");

    id = await add("current-user-dom");
    await page.evaluate(() => { window.currentUserDom = true; });
    assert.equal((await run(id)).reason, "sent");
    assert.equal(await page.evaluate(() => window.submits), 1);
    assert.match(await page.locator('[data-user-message-bubble]').innerText(), /LA_CHAT=chat-[0-9a-f]{8}/);
    assert.equal((await readChat(id)).bootstrapPending, false);
    console.log("PASS: current ChatGPT data-user-message-bubble confirms exact user delivery");

    id = await add("mixed-user-dom");
    assert.equal((await run(id)).reason, "sent");
    assert.equal(await page.locator('[data-message-author-role="user"]').count(), 1);
    await page.evaluate(() => { window.currentUserDom = true; });
    assert.equal((await run(id)).reason, "sent");
    assert.equal(await page.evaluate(() => window.submits), 2);
    assert.equal(await page.locator('[data-user-message-bubble]').count(), 1);
    console.log("PASS: later current user bubble confirms delivery even when an older legacy user turn remains");

    await page.evaluate(() => { window.virtualizeUserTurns = true; window.currentUserDom = false; });
    assert.equal((await run(id)).reason, "sent");
    assert.equal((await run(id)).reason, "sent");
    assert.equal(await page.locator('[data-message-author-role="user"]').count(), 1);
    assert.equal(await page.evaluate(() => window.submits), 4);
    console.log("PASS: new exact user turn confirms delivery when virtualization keeps the message count constant");

    id = await add("replacement");
    await page.evaluate(() => { window.replaceComposerOnSubmit = true; });
    assert.equal((await run(id)).reason, "sent");
    assert.equal(await page.evaluate(() => window.submits), 1);
    assert.match(await page.locator('[data-message-author-role="user"]').innerText(), /LA_CHAT=chat-[0-9a-f]{8}/);
    console.log("PASS: composer DOM replacement after submit still confirms exact user delivery");

    id = await add("delayed-onclick");
    await page.evaluate(() => {
      const button = document.querySelector("#composer-submit-button");
      button.addEventListener("click", (event) => {
        event.preventDefault();
        const input = document.querySelector("#prompt-textarea");
        const text = input.innerText || input.textContent || "";
        setTimeout(() => {
          const message = document.createElement("div");
          message.dataset.messageAuthorRole = "user";
          message.dataset.messageId = "silent-app-accepted";
          message.textContent = text;
          document.body.append(message);
          input.textContent = "";
          input.dispatchEvent(new Event("input", { bubbles: true }));
        }, 1500);
      }, { capture: true, once: true });
    });
    assert.equal((await run(id)).reason, "sent");
    await page.waitForTimeout(1700);
    assert.equal(
      await page.evaluate(() => window.submits),
      0,
      "delayed application onClick acceptance must not be followed by form.requestSubmit"
    );
    assert.equal(await page.locator('[data-message-id="silent-app-accepted"]').count(), 1);
    console.log("PASS: delayed application click acceptance remains single-shot without form fallback");

    id = await add("draft");
    await prepare();
    let delivery = run(id);
    await waitInsertion();
    await page.locator("#prompt-textarea").fill("Operator draft must survive");
    assert.equal((await delivery).reason, "send_button_not_ready");
    assert.equal(await composerText(), "Operator draft must survive");
    assert.equal(await page.evaluate(() => window.submits), 0);
    console.log("PASS: concurrent operator draft preserved");

    id = await add("whitespace-edit");
    await prepare();
    delivery = run(id);
    await waitInsertion();
    await page.evaluate(() => {
      const input = document.querySelector("#prompt-textarea");
      input.textContent = ` ${input.textContent}`;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      document.querySelector("button").disabled = false;
    });
    assert.equal((await delivery).reason, "composer_changed");
    assert.ok((await composerText()).startsWith(" "));
    assert.equal(await page.evaluate(() => window.submits), 0);
    console.log("PASS: whitespace-only operator edit is preserved and never submitted");

    id = await add("navigation");
    await prepare();
    delivery = run(id);
    await waitInsertion();
    await page.evaluate(() => {
      history.pushState({}, "", "/c/bridge-fixture-other");
      document.querySelector("button").disabled = false;
    });
    assert.equal((await delivery).ok, false);
    assert.equal(await page.evaluate(() => window.submits), 0);
    console.log("PASS: SPA navigation cannot redirect a wake");

    id = await add("overlap");
    await prepare();
    delivery = run(id);
    await waitInsertion();
    assert.equal((await run(id)).reason, "delivery_in_progress");
    await page.evaluate(() => { document.querySelector("button").disabled = false; });
    assert.equal((await delivery).reason, "sent");
    assert.equal(await page.evaluate(() => window.submits), 1);
    console.log("PASS: overlapping sends submit once");

    id = await add("unconfirmed");
    const unconfirmedId = id;
    const unconfirmedUrl = `https://chatgpt.com/c/bridge-fixture-unconfirmed`;
    await page.evaluate(() => { window.dropDelivery = true; });
    assert.equal((await run(id)).reason, "delivery_unconfirmed");
    assert.equal((await readChat(id)).enabled, false);
    assert.equal(Object.hasOwn(await readChat(id), "pendingDelivery"), false);
    assert.equal((await run(id)).reason, "delivery_unconfirmed");
    assert.equal(await page.evaluate(() => window.submits), 2);
    console.log("PASS: unconfirmed submission stays diagnostic and never creates a blocking journal");

    const removeUnconfirmedId = await add("remove-unconfirmed");
    await page.evaluate(() => { window.dropDelivery = true; });
    assert.equal((await run(removeUnconfirmedId)).reason, "delivery_unconfirmed");
    await popup.reload();
    await popup.waitForFunction(() => document.querySelectorAll(".conversation-card").length === 11);
    await popup.waitForFunction(
      (height) => document.body.getBoundingClientRect().height > height,
      compactPopup.height
    );

    const popupSize = await popupMetrics();
    assert.equal(popupSize.width, 420);
    assert.ok(popupSize.height <= 600, `crowded popup exceeded 600px: ${popupSize.height}`);
    assert.ok(popupSize.height > compactPopup.height, "crowded popup should grow beyond compact height");
    assert.ok(popupSize.scrollHeight > popupSize.clientHeight, "crowded popup should scroll only after reaching its height cap");
    assert.equal(await popup.locator(".brand-orb").count(), 0);
    assert.equal((await popup.locator(".brand-title").innerText()).replace(/\s+/g, " "), "Local Agent · Chat Bridge");
    assert.equal(await popup.locator(".card-editor").count(), 0);
    assert.equal(await popup.getByRole("button", { name: "Run now", exact: true }).count(), 11);
    assert.equal(await popup.locator(".wake-input-wrap input").count(), 11);
    assert.equal(await popup.locator(".enable-switch input").count(), 11);
    assert.equal(await popup.locator(".switch-control .switch-track").count(), 12);
    assert.equal(await popup.locator(".delivery-resolution").count(), 0);
    assert.equal(
      await popup.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--accent").trim()),
      ""
    );
    const switchGeometry = await popup.evaluate(() => {
      const read = (selector) => {
        const style = getComputedStyle(document.querySelector(selector));
        return { width: style.width, height: style.height, borderRadius: style.borderRadius };
      };
      return {
        master: read(".master-control .switch-track"),
        chat: read(".enable-switch .switch-track")
      };
    });
    assert.deepEqual(switchGeometry.master, switchGeometry.chat);
    assert.match(
      await popup.locator(".current-chat-panel .info-icon").getAttribute("title"),
      /only schedules and transports wakes/i
    );

    const successCard = popup.locator(".conversation-card").filter({ hasText: "success" });
    const wakeInput = successCard.locator(".wake-input-wrap input");
    await wakeInput.fill("17");
    await wakeInput.press("Enter");
    await popup.waitForFunction(() => document.querySelector("#message").textContent.includes("17 min"));
    assert.equal((await readChat(successId)).intervalOverrideMinutes, 17);

    let nativeDialogSeen = false;
    popup.on("dialog", async (dialog) => {
      nativeDialogSeen = true;
      await dialog.dismiss();
    });
    const removableCard = popup.locator(".conversation-card").filter({ hasText: "remove-unconfirmed" });
    await removableCard.getByRole("button", { name: "Remove", exact: true }).click();
    await popup.waitForFunction(() =>
      !Array.from(document.querySelectorAll(".conversation-card")).some((card) =>
        card.textContent.includes("remove-unconfirmed")
      )
    );
    assert.equal(nativeDialogSeen, false);
    const stateAfterRemove = (await request({ type: "bridge:get-state" })).state;
    assert.equal(stateAfterRemove.conversations[removeUnconfirmedId], undefined);
    assert.equal(Object.values(stateAfterRemove.conversations).some((item) => Object.hasOwn(item, "pendingDelivery")), false);
    console.log("PASS: adaptive monochrome popup has unified switches, no delivery-resolution UI and no native dialogs");

    id = await add("busy-pause");
    await request({ type: "bridge:update-conversation", conversationId: id,
      patch: { enabled: true, intervalOverrideMinutes: 17 } });
    // Keep fixture alarms away from the assertions while preserving Master authority.
    await request({ type: "bridge:save-global-settings", settings: { masterEnabled: false } });
    await page.evaluate(() => {
      const stop = document.createElement("button");
      stop.dataset.testid = "stop-button";
      stop.textContent = "Stop";
      document.body.append(stop);
      const answer = document.createElement("div");
      answer.dataset.messageAuthorRole = "assistant";
      answer.dataset.messageId = "new-pause-answer";
      answer.textContent = "Acknowledged. [LAB:PAUSE]";
      document.body.append(answer);
    });
    assert.equal((await run(id)).reason, "assistant_busy");
    await page.waitForTimeout(800);
    assert.equal((await readChat(id)).enabled, true, "streaming controls must wait for completion");
    assert.equal(await page.evaluate(() => window.submits), 0);
    // ChatGPT can retain the stop node and change only its visibility after completion.
    await page.locator('[data-testid="stop-button"]').evaluate((button) => { button.hidden = true; });
    await popup.waitForFunction((chatId) =>
      document.querySelector(`[data-conversation-id="${chatId}"] .status-text`)?.textContent === "paused_by_assistant",
    id, { timeout: 4000 });
    const pausedChat = await readChat(id);
    assert.equal(pausedChat.enabled, false);
    assert.equal(pausedChat.nextRunAt, null);
    assert.equal(pausedChat.intervalOverrideMinutes, 17);
    assert.equal((await request({ type: "bridge:get-state" })).state.settings.masterEnabled, false);
    assert.equal(await worker.evaluate((chatId) => chrome.alarms.get(`local-agent-chat:${chatId}`), id), undefined);
    console.log("PASS: inline PAUSE waits for visible generation to end, clears the alarm and updates the open popup");

    id = await add("hidden-stop");
    for (const testId of ["stop-button", "composer-stop-button"]) {
      for (const style of ["display:none", "visibility:hidden", "opacity:0"]) {
        await page.evaluate(({ testId, style }) => {
          document.querySelectorAll('[data-testid="stop-button"], [data-testid="composer-stop-button"]').forEach((e) => e.remove());
          const stop = document.createElement("button");
          stop.dataset.testid = testId;
          stop.textContent = "Stop";
          stop.style.cssText = style;
          document.body.append(stop);
        }, { testId, style });
        assert.equal((await run(id)).reason, "sent", `${testId} with ${style} must not block delivery`);
      }
    }
    await page.locator('[data-testid="composer-stop-button"]').evaluate((button) => { button.style.cssText = ""; });
    assert.equal((await run(id)).reason, "assistant_busy");
    assert.equal(await page.evaluate(() => window.submits), 6);
    console.log("PASS: hidden stop controls allow delivery; a visible stop control still blocks submission");

    await require("./bridge_control_browser_cases.cjs")({ page, request, readChat, add, worker });

    await bounded("browser shutdown", context.close());
    context = await launch();
    await context.setOffline(true);
    await installRoutes();
    page = await context.newPage();
    await page.goto(unconfirmedUrl);
    await page.evaluate(() => { window.dropDelivery = true; });
    popup = await context.newPage();
    await popup.goto(`chrome-extension://${extensionId}/popup.html`);
    assert.equal((await run(unconfirmedId)).reason, "delivery_unconfirmed");
    assert.equal(await page.evaluate(() => window.submits), 1);
    const restartedChat = await readChat(unconfirmedId);
    assert.equal(Object.hasOwn(restartedChat, "pendingDelivery"), false);
    console.log("PASS: worker restart has no durable delivery journal or manual-resolution gate");
  } finally {
    if (context) await context.close();
    await fs.rm(profile, { recursive: true, force: true });
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
