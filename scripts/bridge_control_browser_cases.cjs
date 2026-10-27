"use strict";

const assert = require("node:assert/strict");

module.exports = async function verifyDecoratedControls({ page, request, readChat, add, worker }) {
  const id = await add("decorated-controls");
  await request({ type: "bridge:save-global-settings", settings: { masterEnabled: false } });
  await request({ type: "bridge:update-conversation", conversationId: id,
    patch: { enabled: true, intervalOverrideMinutes: 17 } });
  const original = await readChat(id);
  let identity = 0;
  const append = (html, role = "assistant") => page.evaluate(({ html, role, identity }) => {
    const answer = document.createElement("div");
    answer.dataset.messageAuthorRole = role;
    answer.dataset.messageId = `decorated-answer-${identity}`;
    answer.innerHTML = html;
    document.body.append(answer);
  }, { html, role, identity: ++identity });

  async function accept(html, marker, enabled, interval, scheduled) {
    const before = await readChat(id);
    const started = Date.now();
    await append(html);
    const matches = (chat) => chat.generation === before.generation + 1 && chat.lastControlAction === marker &&
      chat.enabled === enabled && chat.intervalOverrideMinutes === interval &&
      Boolean(chat.nextRunAt) === scheduled && (!scheduled || chat.nextRunAt !== before.nextRunAt);
    const deadline = Date.now() + 5000;
    let after = await readChat(id);
    while (!matches(after) && Date.now() < deadline) {
      await page.waitForTimeout(50);
      after = await readChat(id);
    }
    assert.ok(matches(after), `${html}: ${JSON.stringify(after)}`);
    for (const key of ["agentBinding", "repositoryId", "repository", "bindingRevision", "assistantBaseline"]) {
      assert.equal(after[key], original[key], `${html} changed ${key}`);
    }
    assert.equal((await request({ type: "bridge:get-state" })).state.settings.masterEnabled, false);
    assert.equal(await worker.evaluate((id) => chrome.alarms.get(`local-agent-chat:${id}`), id), undefined);
    if (marker === "[LAB:NEXT=10m]") {
      assert.ok(Date.parse(after.nextRunAt) >= started + 600_000,
        JSON.stringify({ started, nextRunAt: after.nextRunAt, lastControlAt: after.lastControlAt }));
      assert.ok(Date.parse(after.nextRunAt) <= Date.now() + 600_000);
    }
    return after;
  }

  await accept("<p>Acknowledged.[LAB:PAUSE]</p>", "[LAB:PAUSE]", false, 17, false);
  await accept('<p>Acknowledged. "[LAB:RESUME]"</p>', "[LAB:RESUME]", true, 17, true);
  await accept("<p>Check later. [LAB:NEXT=10m] -</p>", "[LAB:NEXT=10m]", true, 17, true);
  await accept("<p>Interval:<strong>[LAB:INTERVAL=5]</strong>.</p>", "[LAB:INTERVAL=5]", true, 5, true);
  await accept("<pre><code>[LAB:PAUSE]</code></pre>", "[LAB:PAUSE]", false, 5, false);
  await accept("<p><code>[LAB:RESUME]</code></p>", "[LAB:RESUME]", true, 5, true);
  await accept("<blockquote>[LAB:STOP]</blockquote>", "[LAB:STOP]", false, null, false);
  await accept("<p>Done.[LOCAL_AGENT_BRIDGE:RESUME]</p>", "[LOCAL_AGENT_BRIDGE:RESUME]", true, null, true);
  await accept("<p>[LAB:PAUSE]</p><p>—</p>", "[LAB:PAUSE]", false, null, false);
  await accept("<p>[LAB:PAUSE] [LAB:RESUME]!</p>", "[LAB:RESUME]", true, null, true);

  const duplicateBaseline = await readChat(id);
  await page.evaluate(() => {
    const answers = document.querySelectorAll('[data-message-author-role="assistant"]');
    answers[answers.length - 1].append(document.createElement("span"));
  });
  await page.waitForTimeout(1000);
  assert.deepEqual(await readChat(id), duplicateBaseline);
  const resumed = await accept("<p>[LAB:PAUSE] [LAB:RESUME]!</p>", "[LAB:RESUME]", true, null, true);
  assert.ok(Date.parse(resumed.nextRunAt) > Date.parse(duplicateBaseline.nextRunAt));
  console.log("PASS: decorated and rendered Markdown controls reach the worker with unchanged binding, Master and timing semantics");

  for (const html of [
    "<p>[LAB:PAUSE] Continue.</p>", "<p>[LAB:PAUSE]</p><p>—</p><p>Continue.</p>",
    "<p>[LAB:PAUSE] [LAB:NEXT=29s]</p>", "<p>[LAB:PAUSE] [LAB:</p>",
    "<p>[LAB:NEXT=86401s].</p>", "<p>[LAB:INTERVAL=90s] —</p>",
    "<p>[lab:pause]</p>", "<p>[LAB:PAUSE]🙂</p>"
  ]) {
    const before = await readChat(id);
    await append(html);
    await page.waitForTimeout(1000);
    assert.deepEqual(await readChat(id), before, html);
  }
  const beforeUser = await readChat(id);
  await append("<p>Acknowledged.[LAB:PAUSE]</p>", "user");
  await page.waitForTimeout(1000);
  assert.deepEqual(await readChat(id), beforeUser, "user text must not become an assistant control");
  await accept("<p>Acknowledged.[LAB:PAUSE].</p>", "[LAB:PAUSE]", false, null, false);
  assert.equal(await page.evaluate(() => window.submits), 0);
  console.log("PASS: trailing prose, invalid final candidates, invalid ranges and user messages leave conversation state unchanged");

  const currentDomBefore = await readChat(id);
  await page.evaluate(() => {
    document.querySelectorAll('[data-message-author-role="assistant"]').forEach((node) => node.remove());
    const turn = document.createElement("div");
    turn.dataset.turnKey = "current-dom-assistant-control";
    const answer = document.createElement("div");
    answer.dataset.conversationRole = "assistant";
    answer.textContent = "[LAB:RESUME]";
    turn.append(answer);
    document.body.append(turn);
  });
  const currentDomDeadline = Date.now() + 5000;
  let currentDomAfter = await readChat(id);
  while (
    (currentDomAfter.generation !== currentDomBefore.generation + 1 ||
      currentDomAfter.lastControlAction !== "[LAB:RESUME]") &&
    Date.now() < currentDomDeadline
  ) {
    await page.waitForTimeout(50);
    currentDomAfter = await readChat(id);
  }
  assert.equal(currentDomAfter.generation, currentDomBefore.generation + 1);
  assert.equal(currentDomAfter.lastControlAction, "[LAB:RESUME]");
  assert.equal(currentDomAfter.enabled, true);
  console.log("PASS: current ChatGPT data-conversation-role assistant controls reach the worker");

  const mixedAssistantBefore = await readChat(id);
  await page.evaluate(() => {
    const turn = document.createElement("div");
    turn.dataset.turnKey = "mixed-current-grouped-assistant-control";
    const answer = document.createElement("div");
    answer.textContent = "[LAB:PAUSE]";
    const user = document.createElement("div");
    user.dataset.userMessageBubble = "";
    user.textContent = "ordinary user text";
    turn.append(answer, user);
    document.body.append(turn);
  });
  const mixedAssistantDeadline = Date.now() + 5000;
  let mixedAssistantAfter = await readChat(id);
  while ((mixedAssistantAfter.generation !== mixedAssistantBefore.generation + 1 || mixedAssistantAfter.lastControlAction !== "[LAB:PAUSE]") && Date.now() < mixedAssistantDeadline) {
    await page.waitForTimeout(50);
    mixedAssistantAfter = await readChat(id);
  }
  assert.equal(mixedAssistantAfter.generation, mixedAssistantBefore.generation + 1);
  assert.equal(mixedAssistantAfter.lastControlAction, "[LAB:PAUSE]");
  assert.equal(mixedAssistantAfter.enabled, false);
  console.log("PASS: a newer grouped assistant turn wins over older explicit assistant nodes");

  const groupedBefore = await readChat(id);
  await page.evaluate(() => {
    document.querySelectorAll('[data-message-author-role="assistant"], [data-conversation-role="assistant"]').forEach((node) => node.remove());
    const turn = document.createElement("div");
    turn.dataset.turnKey = "grouped-assistant-control";
    const answer = document.createElement("div");
    answer.textContent = "[LAB:RESUME]";
    const user = document.createElement("div");
    user.dataset.userMessageBubble = "";
    user.textContent = "[LAB:PAUSE]";
    turn.append(answer, user);
    document.body.append(turn);
  });
  const groupedDeadline = Date.now() + 5000;
  let groupedAfter = await readChat(id);
  while (
    (groupedAfter.generation !== groupedBefore.generation + 1 ||
      groupedAfter.lastControlAction !== "[LAB:RESUME]") &&
    Date.now() < groupedDeadline
  ) {
    await page.waitForTimeout(50);
    groupedAfter = await readChat(id);
  }
  assert.equal(groupedAfter.generation, groupedBefore.generation + 1);
  assert.equal(groupedAfter.lastControlAction, "[LAB:RESUME]");
  assert.equal(groupedAfter.enabled, true);

  const assistantOnlyBefore = await readChat(id);
  await page.evaluate(() => {
    const turn = document.createElement("div");
    turn.dataset.turnKey = "grouped-assistant-only-control";
    const answer = document.createElement("p");
    answer.textContent = "[LAB:PAUSE]";
    const copy = document.createElement("button");
    copy.textContent = "Copy";
    const more = document.createElement("button");
    more.textContent = "More";
    turn.append(answer, copy, more);
    document.body.append(turn);
  });
  const assistantOnlyDeadline = Date.now() + 5000;
  let assistantOnlyAfter = await readChat(id);
  while ((assistantOnlyAfter.generation !== assistantOnlyBefore.generation + 1 || assistantOnlyAfter.lastControlAction !== "[LAB:PAUSE]") && Date.now() < assistantOnlyDeadline) {
    await page.waitForTimeout(50);
    assistantOnlyAfter = await readChat(id);
  }
  assert.equal(assistantOnlyAfter.generation, assistantOnlyBefore.generation + 1, JSON.stringify(assistantOnlyAfter));
  assert.equal(assistantOnlyAfter.lastControlAction, "[LAB:PAUSE]");
  assert.equal(assistantOnlyAfter.enabled, false);
  console.log("PASS: assistant-only grouped turn without role/user markers reaches LAB control scanner");

  const userOnlyBefore = await readChat(id);
  await page.evaluate(() => {
    const turn = document.createElement("div");
    turn.dataset.turnKey = "grouped-user-only";
    const user = document.createElement("div");
    user.dataset.userMessageBubble = "";
    user.textContent = "[LAB:STOP]";
    turn.append(user);
    document.body.append(turn);
  });
  await page.waitForTimeout(1000);
  assert.deepEqual(await readChat(id), userOnlyBefore, "user-only grouped turn must never become an assistant control");

  const shadowBefore = await readChat(id);
  await page.evaluate(() => {
    const controlTurn = document.createElement("div");
    controlTurn.dataset.turnKey = "grouped-control-before-user-only";
    const answer = document.createElement("div");
    answer.textContent = "[LAB:PAUSE]";
    const controlUser = document.createElement("div");
    controlUser.dataset.userMessageBubble = "";
    controlUser.textContent = "ordinary user text";
    controlTurn.append(answer, controlUser);
    document.body.append(controlTurn);

    const userOnlyTurn = document.createElement("div");
    userOnlyTurn.dataset.turnKey = "grouped-newer-user-only-shadow";
    const user = document.createElement("div");
    user.dataset.userMessageBubble = "";
    user.textContent = "new user prompt before the 600ms assistant scan";
    userOnlyTurn.append(user);
    document.body.append(userOnlyTurn);
  });
  const shadowDeadline = Date.now() + 5000;
  let shadowAfter = await readChat(id);
  while ((shadowAfter.generation !== shadowBefore.generation + 1 || shadowAfter.lastControlAction !== "[LAB:PAUSE]") && Date.now() < shadowDeadline) {
    await page.waitForTimeout(50);
    shadowAfter = await readChat(id);
  }
  assert.equal(shadowAfter.generation, shadowBefore.generation + 1, JSON.stringify(shadowAfter));
  assert.equal(shadowAfter.lastControlAction, "[LAB:PAUSE]");
  assert.equal(shadowAfter.enabled, false);
  console.log("PASS: a newer user-only grouped turn cannot shadow the preceding assistant control");
  console.log("PASS: grouped turn fallback strips user bubbles before parsing assistant controls");

  // Hold Send disabled long enough to observe the feedback insertion deterministically,
  // then release the same button and require the in-flight Bridge delivery to submit it.
  await add("help-feedback-submit");
  const beforeHelpUsers = await page.locator('[data-message-author-role="user"]').count();
  await page.evaluate(() => { document.querySelector("#composer-submit-button").disabled = true; });
  await page.evaluate(() => {
    const answer = document.createElement("div");
    answer.dataset.messageAuthorRole = "assistant";
    answer.dataset.messageId = "help-feedback-answer";
    answer.textContent = "[LAB:HELP]";
    document.body.append(answer);
  });
  await page.waitForFunction(() => {
    const composer = document.querySelector("#prompt-textarea");
    const text = composer?.innerText || composer?.textContent || "";
    return text.includes("[LA_BRIDGE_FEEDBACK]") && text.includes("command=HELP");
  }, null, { timeout: 5000 });
  const inserted = await page.evaluate(() => ({
    composer: document.querySelector("#prompt-textarea")?.innerText || document.querySelector("#prompt-textarea")?.textContent || "",
    submits: window.submits,
    buttonDisabled: document.querySelector("#composer-submit-button")?.disabled,
    buttonConnected: document.querySelector("#composer-submit-button")?.isConnected,
    buttonType: document.querySelector("#composer-submit-button")?.type
  }));
  assert.match(inserted.composer, /\[LA_BRIDGE_FEEDBACK\]/);
  assert.match(inserted.composer, /command=HELP/);
  assert.equal(inserted.submits, 0);
  assert.equal(inserted.buttonDisabled, true);
  assert.equal(inserted.buttonConnected, true);
  assert.equal(inserted.buttonType, "submit");
  console.log("PASS: LAB HELP feedback reaches the composer before Send is released");

  await page.evaluate(() => { document.querySelector("#composer-submit-button").disabled = false; });
  await page.waitForFunction((beforeCount) => {
    const messages = document.querySelectorAll('[data-message-author-role="user"]');
    if (messages.length <= beforeCount) return false;
    const latest = messages[messages.length - 1];
    const text = latest.innerText || latest.textContent || "";
    return text.includes("[LA_BRIDGE_FEEDBACK]") && text.includes("command=HELP");
  }, beforeHelpUsers, { timeout: 7000 });
  assert.equal(await page.evaluate(() => window.submits), 1);
  assert.equal(await page.locator("#prompt-textarea").textContent(), "");
  console.log("PASS: LAB HELP feedback is submitted automatically after the live Send control becomes ready");

  const operatorBaselineId = await add("operator-replay-baseline");
  const operatorBaselineUrl = page.url();
  await page.evaluate(() => {
    try { globalThis.__localAgentChatBridgeState?.dispose?.(); } catch (_error) {}
    globalThis.__localAgentChatBridgeState = null;
  });
  await append("<p>[LAB:OP:REMOVE]</p>", "user");
  const operatorTabId = await worker.evaluate(async (url) => {
    const tabs = await chrome.tabs.query({ url: ["https://chatgpt.com/*", "https://chat.openai.com/*"] });
    return tabs.find((tab) => tab.url === url)?.id || null;
  }, operatorBaselineUrl);
  assert.ok(operatorTabId, "fixture tab id must be discoverable");
  const ensured = await request({
    type: "bridge:ensure-tab-content",
    tabId: operatorTabId,
    expectedUrl: operatorBaselineUrl
  });
  assert.equal(ensured.ok, true, ensured.reason);
  await page.waitForTimeout(1200);
  assert.ok(await readChat(operatorBaselineId), "historical OP marker must be baselined, not replayed after reinjection");
  await append("<p>[LAB:OP:REMOVE]</p>", "user");
  const operatorDeadline = Date.now() + 5000;
  while (await readChat(operatorBaselineId) && Date.now() < operatorDeadline) {
    await page.waitForTimeout(50);
  }
  assert.equal(await readChat(operatorBaselineId), undefined, "new user-authored OP marker must execute after baseline");
  console.log("PASS: operator LAB controls baseline historical user messages across content reinjection");

  const retainedId = await add("retained-unconfirmed-browser-case");
  await page.evaluate(() => { window.dropDelivery = true; });
  const firstUnconfirmed = await request({ type: "bridge:run-now", conversationId: retainedId });
  assert.equal(firstUnconfirmed.reason, "delivery_unconfirmed");
  const retainedPrompt = await page.locator("#prompt-textarea").textContent();
  assert.match(retainedPrompt, /\[LA_CHAT=chat-[0-9a-f]{8}\]/);
  assert.equal(await page.evaluate(() => window.submits), 1);
  await page.locator("#prompt-textarea").fill(`${retainedPrompt} operator-edit`);
  const blockedRetry = await request({ type: "bridge:run-now", conversationId: retainedId });
  assert.equal(blockedRetry.reason, "composer_not_empty");
  assert.equal(await page.evaluate(() => window.submits), 1);
  assert.match(await page.locator("#prompt-textarea").textContent(), /operator-edit$/);
  await page.evaluate(() => { window.dropDelivery = false; });
  console.log("PASS: unconfirmed Bridge prompt stays visible and any operator edit blocks automatic reuse");
};
