"use strict";

// Offline real-extension proof: parent control -> four same-browser children, including
// three real post-submit ambiguous route transitions -> durable partial capture -> real
// MV3 service-worker restart -> exact ownership recovery without bootstrap replay ->
// terminal feedback -> second restart -> no replay.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require(process.env.LOCAL_AGENT_PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "..");
const catalog = require(path.join(root, "chat_bridge/runtime.example.json"));
const parentUrl = "https://chatgpt.com/c/fabric-parent-proof";
const fixture = `<!doctype html><html><head><style>#prompt-textarea { white-space: pre-wrap; }</style></head><body>
<div id="turns"></div>
<form><div id="prompt-textarea" class="ProseMirror" contenteditable="true" role="textbox"></div>
<button id="composer-submit-button" type="submit">Send</button></form>
<script>
window.submitted = [];
window.__childId = null;
window.__completionMarker = null;
window.__childCompleted = false;
window.__delayedChildUrl = "";
window.releaseDelayedRoute = () => {
  if (!window.__delayedChildUrl) return;
  history.pushState({}, "", window.__delayedChildUrl);
  window.__delayedChildUrl = "";
  turn("assistant", "WORKING_" + String(window.__childId || "").toUpperCase());
};
function turn(role, text) {
  const row = document.createElement("div");
  row.dataset.turnKey = role + "-" + document.querySelectorAll("[data-turn-key]").length;
  const message = document.createElement("div");
  if (role === "assistant") {
    const unit = document.createElement("div");
    unit.dataset.contentSearchUnitKey = row.dataset.turnKey + ":assistant";
    const heading = document.createElement("h4");
    heading.dataset.conversationRole = role;
    heading.textContent = "ChatGPT said:";
    unit.appendChild(heading);
    message.dataset.chatgptSelectionMessageId = row.dataset.turnKey;
    unit.appendChild(message);
    row.appendChild(unit);
    const rating = document.createElement("div");
    rating.textContent = "Is this conversation helpful so far?";
    row.appendChild(rating);
  } else {
    message.dataset.messageAuthorRole = role;
    row.appendChild(message);
  }
  message.textContent = text;
  document.querySelector("#turns").appendChild(row);
}
window.completeChild = () => {
  if (!window.__childId || !window.__completionMarker || window.__childCompleted) return;
  window.__childCompleted = true;
  turn("assistant", "RESULT_" + window.__childId.toUpperCase() + "\\n" + window.__completionMarker);
};
document.querySelector("form").onsubmit = (event) => {
  event.preventDefault();
  const composer = document.querySelector("#prompt-textarea");
  const text = composer.innerText || composer.textContent;
  window.submitted.push(text);
  turn("user", text);
  composer.textContent = "";
  composer.dispatchEvent(new Event("input", {bubbles: true}));
  if (text === "INITIAL_PARENT") {
    history.pushState({}, "", "/c/fabric-parent-proof");
    turn("assistant", "READY");
    return;
  }
  const child = text.match(/^child_id=(.+)$/m);
  if (child) {
    window.__childId = child[1];
    const completion = text.match(/LOCAL_AGENT_CF_CHILD_COMPLETE:[0-9a-f]{8}:[A-Za-z0-9._-]{1,64}:[0-9a-f]{8}/);
    if (!completion) throw new Error("child bootstrap missing completion marker");
    window.__completionMarker = completion[0];

    if (child[1] === "fast") {
      history.pushState({}, "", "/c/fabric-child-fast");
      window.completeChild();
      return;
    }

    // Reproduce the production race: Send succeeds and the exact page claim is durable,
    // then routing passes through a recognized provisional URL and briefly lands on an
    // unrecognized route. spawn_content therefore reports submission ambiguity even
    // though the bootstrap was submitted exactly once. The test releases the canonical
    // child route only after the MV3 worker has restarted.
    history.pushState({}, "", "/uc/fabric-" + child[1]);
    window.__delayedChildUrl = "/c/fabric-child-" + child[1];
    setTimeout(() => {
      if (window.__delayedChildUrl) history.pushState({}, "", "/routing-pending/c/fabric-child-" + child[1] + "/hold");
    }, 50);
    return;
  }
  if (
    text.includes("completed.") &&
    text.includes("RESULT_FAST") &&
    text.includes("RESULT_PRODUCT") &&
    text.includes("RESULT_RELIABILITY") &&
    text.includes("RESULT_ARCHITECTURE")
  ) {
    turn("assistant", "PARENT_SYNTHESIS: RESULT_FAST + RESULT_PRODUCT + RESULT_RELIABILITY + RESULT_ARCHITECTURE");
  }
};
</script></body></html>`;

async function waitFor(label, predicate, timeout = 30000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const value = await predicate();
    if (value) return value;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`${label} timed out`);
}

async function installRuntimeFetch(worker) {
  await worker.evaluate(runtime => {
    globalThis.fetch = async () => ({ ok: true, json: async () => runtime });
  }, catalog);
}

async function triggerProductionFabricPoll(worker) {
  return worker.evaluate(async () => {
    const originalPoll = pollConversationFabricCampaigns;
    let restored = false;
    let resolveCompleted;
    let rejectCompleted;
    const completed = new Promise((resolve, reject) => {
      resolveCompleted = resolve;
      rejectCompleted = reject;
    });
    const restore = () => {
      if (restored) return;
      restored = true;
      pollConversationFabricCampaigns = originalPoll;
    };
    const timeout = setTimeout(() => {
      restore();
      rejectCompleted(new Error("production Conversation Fabric poll alarm did not complete"));
    }, 10000);

    pollConversationFabricCampaigns = async function observedProductionFabricPoll(...args) {
      try {
        const result = await originalPoll(...args);
        resolveCompleted({ ok: true, completedAt: Date.now() });
        return result;
      } catch (error) {
        rejectCompleted(error);
        throw error;
      } finally {
        restore();
      }
    };

    try {
      await chrome.alarms.create(GITHUB_CONTROL_ALARM_NAME, { when: Date.now() + 50 });
      return await completed;
    } finally {
      clearTimeout(timeout);
      restore();
    }
  });
}

async function attachServiceWorkerController(context, version) {
  if (!version?.targetId || !version?.scriptURL) {
    throw new Error("restarted service worker is missing a CDP target");
  }
  const browser = context.browser();
  if (!browser) throw new Error("Chromium browser handle unavailable for service-worker controller");

  const cdp = await browser.newBrowserCDPSession();
  const attached = await cdp.send("Target.attachToTarget", {
    targetId: version.targetId,
    flatten: false
  });
  const sessionId = attached.sessionId;
  let sequence = 0;
  const pending = new Map();

  const onMessage = event => {
    if (event.sessionId !== sessionId) return;
    let message;
    try {
      message = JSON.parse(event.message);
    } catch (_error) {
      return;
    }
    if (!Number.isInteger(message.id) || !pending.has(message.id)) return;
    const entry = pending.get(message.id);
    pending.delete(message.id);
    clearTimeout(entry.timeout);
    if (message.error) {
      entry.reject(new Error(`${message.error.message || "CDP command failed"} (${message.error.code || "unknown"})`));
    } else {
      entry.resolve(message.result || {});
    }
  };
  cdp.on("Target.receivedMessageFromTarget", onMessage);

  async function send(method, params = {}) {
    const id = ++sequence;
    const response = new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        pending.delete(id);
        reject(new Error(`nested CDP command timed out: ${method}`));
      }, 10000);
      pending.set(id, { resolve, reject, timeout });
    });
    await cdp.send("Target.sendMessageToTarget", {
      sessionId,
      message: JSON.stringify({ id, method, params })
    });
    return response;
  }

  await send("Runtime.enable");

  return {
    url: () => version.scriptURL,
    async evaluate(pageFunction, arg) {
      const serialized = arg === undefined ? "undefined" : JSON.stringify(arg);
      const response = await send("Runtime.evaluate", {
        expression: `(${pageFunction.toString()})(${serialized})`,
        awaitPromise: true,
        returnByValue: true
      });
      if (response.exceptionDetails) {
        throw new Error(response.exceptionDetails.exception?.description || response.exceptionDetails.text || "service-worker evaluation failed");
      }
      return response.result?.value;
    },
    async dispose() {
      for (const entry of pending.values()) {
        clearTimeout(entry.timeout);
        entry.reject(new Error("service-worker controller disposed"));
      }
      pending.clear();
      cdp.off("Target.receivedMessageFromTarget", onMessage);
      try {
        await cdp.send("Target.detachFromTarget", { sessionId });
      } catch (_error) {}
      await cdp.detach();
    }
  };
}

async function restartServiceWorker(context, worker, { clearSession = true } = {}) {
  const extensionId = new URL(worker.url()).hostname;
  const page = context.pages().find(candidate => candidate.url().startsWith("https://chatgpt.com/"));
  if (!page) throw new Error("ChatGPT page unavailable for MV3 restart proof");

  await worker.evaluate(async shouldClear => {
    if (shouldClear) await chrome.storage.session.clear();
  }, clearSession);
  if (typeof worker.dispose === "function") await worker.dispose();

  // Capture both the extension content-script world and Chromium's actual service-worker
  // lifecycle. CDP is harness-only observation/interruption; recovery itself remains the
  // installed extension's normal message/alarm/durable-state path.
  const cdp = await context.newCDPSession(page);
  const executionContexts = new Set();
  const workerVersions = new Map();
  cdp.on("Runtime.executionContextCreated", event => {
    if (Number.isInteger(event?.context?.id)) executionContexts.add(event.context.id);
  });
  cdp.on("ServiceWorker.workerVersionUpdated", event => {
    for (const version of event?.versions || []) {
      if (version?.versionId) workerVersions.set(version.versionId, version);
    }
  });

  try {
    await cdp.send("Runtime.enable");
    await cdp.send("ServiceWorker.enable");

    const contentContextId = await waitFor("extension isolated content world", async () => {
      for (const contextId of executionContexts) {
        try {
          const probe = await cdp.send("Runtime.evaluate", {
            expression: "globalThis.chrome?.runtime?.id || ''",
            contextId,
            returnByValue: true
          });
          if (probe?.result?.value === extensionId) return contextId;
        } catch (_error) {}
      }
      return null;
    }, 5000);

    const runningVersion = await waitFor("running extension service worker", () => {
      return Array.from(workerVersions.values()).find(version =>
        String(version.scriptURL || "").startsWith(`chrome-extension://${extensionId}/`) &&
        version.runningStatus === "running"
      ) || null;
    }, 5000);

    await cdp.send("ServiceWorker.stopAllWorkers");
    await waitFor("stopped extension service worker", () => {
      return workerVersions.get(runningVersion.versionId)?.runningStatus === "stopped";
    }, 5000);

    // Wake the stopped worker through the extension's already-injected isolated content
    // world using a real, side-effect-free production message handler.
    const wake = await cdp.send("Runtime.evaluate", {
      expression: `chrome.runtime.sendMessage({type:"bridge:control-context",conversationUrl:${JSON.stringify(parentUrl)}})`,
      contextId: contentContextId,
      awaitPromise: true,
      returnByValue: true
    });
    assert.equal(wake?.result?.value?.ok, true, "content-script runtime message must wake the stopped worker");

    const restartedVersion = await waitFor("running extension service worker after wake", () => {
      return Array.from(workerVersions.values()).find(version =>
        String(version.scriptURL || "").startsWith(`chrome-extension://${extensionId}/`) &&
        version.runningStatus === "running" &&
        version.targetId
      ) || null;
    }, 10000);

    // Playwright does not reliably publish a Worker object for a restarted MV3 worker.
    // Attach a harness-only CDP controller to Chromium's real restarted worker target so
    // the remainder of the test can keep exercising the same production worker globals.
    const nextWorker = await attachServiceWorkerController(context, restartedVersion);
    assert.equal(
      await nextWorker.evaluate(id => chrome.runtime.id === id, extensionId),
      true,
      "CDP controller must be attached to the restarted Bridge service worker"
    );
    await installRuntimeFetch(nextWorker);
    return nextWorker;
  } finally {
    await cdp.detach();
  }
}

async function contentControllerPresent(worker, expectedUrl) {
  return worker.evaluate(async url => {
    const normalized = normalizeConversationUrl(url);
    const tabs = await chrome.tabs.query({});
    const tab = tabs.find(candidate =>
      Number.isInteger(candidate.id) &&
      normalizeConversationUrl(candidate.url || "") === normalized
    );
    if (!tab?.id) return false;
    try {
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id, frameIds: [0] },
        world: "ISOLATED",
        func: () => Boolean(globalThis.__localAgentConversationFabricContent)
      });
      return Boolean(results?.[0]?.result);
    } catch (_error) {
      return false;
    }
  }, expectedUrl);
}

function fabricChildren(context) {
  return context.pages().filter(page => page.url().includes("/c/fabric-child-"));
}

async function pageForChild(context, childId) {
  return waitFor(`child page ${childId}`, async () => {
    for (const page of fabricChildren(context)) {
      if (await page.evaluate(() => window.__childId) === childId) return page;
    }
    return null;
  });
}

async function campaignSnapshot(worker, campaignId) {
  return worker.evaluate(id => loadConversationFabricCampaign(id), campaignId);
}

(async () => {
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), "fabric-browser-proof-"));
  let context;
  let worker;
  let parent;
  let campaignId = null;
  try {
    const extension = path.join(root, "chat_bridge");
    context = await chromium.launchPersistentContext(profile, {
      channel: "chromium",
      ignoreDefaultArgs: ["--disable-extensions"],
      headless: true,
      args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`]
    });
    await context.setOffline(true);
    await context.route("https://**/*", route => route.request().url().startsWith("https://chatgpt.com/")
      ? route.fulfill({ contentType: "text/html", body: fixture }) : route.abort());
    worker = context.serviceWorkers()[0] || await context.waitForEvent("serviceworker");
    await installRuntimeFetch(worker);

    parent = await context.newPage();
    parent.on("console", message => {
      if (["error", "warning"].includes(message.type())) console.error("PARENT:", message.text());
    });
    await parent.goto("https://chatgpt.com/");
    await parent.evaluate(() => {
      document.querySelector("#prompt-textarea").textContent = "INITIAL_PARENT";
      document.querySelector("form").requestSubmit();
    });
    await worker.evaluate(async url => {
      const parent = await upsertConversation({ url, enabled: true });
      await mutateState(state => { state.settings.masterEnabled = true; return state; });
      return parent.id;
    }, parentUrl);

    const control = {
      schema_version: 1,
      action: "delegate",
      children: [
        { id: "fast", role: "research", prompt: "Return RESULT_FAST." },
        { id: "product", role: "research", prompt: "Return RESULT_PRODUCT after route recovery." },
        { id: "reliability", role: "verification", prompt: "Return RESULT_RELIABILITY after route recovery." },
        { id: "architecture", role: "integration", prompt: "Return RESULT_ARCHITECTURE after route recovery." }
      ]
    };
    await parent.evaluate(controlValue => {
      window.turn("assistant", "<<<LOCAL_AGENT_CF\n" + JSON.stringify(controlValue) + "\nLOCAL_AGENT_CF>>>");
    }, control);

    const campaign = await waitFor("four child delegation with three ambiguous routes", () => worker.evaluate(async () => {
      const campaigns = await listConversationFabricCampaigns();
      return campaigns.find(value =>
        value.state === "running" &&
        value.children?.length === 4 &&
        value.children.filter(child => child.state === "submission_ambiguous").length === 3
      ) || null;
    }), 30000);
    campaignId = campaign.id;
    assert.equal(campaign.children.length, 4);
    assert.ok(campaign.children.every(child =>
      /^LOCAL_AGENT_CF_CHILD_COMPLETE:[0-9a-f]{8}:[A-Za-z0-9._-]{1,64}:[0-9a-f]{8}$/m
        .test(child.intent.bootstrap_text)
    ));
    await waitFor("four same-browser child tabs", () => fabricChildren(context).length === 4);
    for (const page of fabricChildren(context)) {
      assert.equal(await page.evaluate(() => window.submitted.length), 1, "every bootstrap is submitted exactly once");
    }
    assert.deepEqual(
      await parent.evaluate(() => ({
        submitted: [...window.submitted],
        composer: document.querySelector("#prompt-textarea")?.textContent || ""
      })),
      { submitted: ["INITIAL_PARENT"], composer: "" },
      "child spawning must never inject started/status feedback or leave a parent draft"
    );

    // Only the fast child is terminal. The three real post-submit ambiguous children
    // stay recoverable and must not be reported as failed or replayed.
    await triggerProductionFabricPoll(worker);
    const partiallyCaptured = await waitFor("durable fast result plus ambiguous children", async () => {
      const value = await campaignSnapshot(worker, campaignId);
      return value?.state === "running" &&
        value.results?.length === 1 &&
        value.children.filter(child => child.state === "submission_ambiguous").length === 3
        ? value
        : null;
    }, 20000);
    assert.deepEqual(partiallyCaptured.results.map(value => value.assistant_text), ["RESULT_FAST"]);
    assert.equal(partiallyCaptured.failed_children.length, 0);

    const productPage = await pageForChild(context, "product");
    const reliabilityPage = await pageForChild(context, "reliability");
    const architecturePage = await pageForChild(context, "architecture");
    const submissionsBeforeRecovery = await Promise.all(
      fabricChildren(context).map(page => page.evaluate(() => window.submitted.length))
    );

    // Restart while the three submissions are still ambiguous. Clear storage.session
    // so promotion must rebuild ownership from the child page's exact durable claim.
    worker = await restartServiceWorker(context, worker, { clearSession: true });
    await waitFor(
      "parent content reinjection in extension isolated world",
      () => contentControllerPresent(worker, parentUrl)
    );
    const afterReload = await campaignSnapshot(worker, campaignId);
    assert.equal(afterReload.state, "running");
    assert.deepEqual(afterReload.results.map(value => value.assistant_text), ["RESULT_FAST"]);
    assert.equal(
      afterReload.children.filter(child => child.state === "submission_ambiguous").length,
      3
    );

    await Promise.all([
      productPage.evaluate(() => window.releaseDelayedRoute()),
      reliabilityPage.evaluate(() => window.releaseDelayedRoute()),
      architecturePage.evaluate(() => window.releaseDelayedRoute())
    ]);

    // The normal production poll must prove exact page ownership, promote all three
    // ambiguous children to submitted, and still not replay any bootstrap.
    await triggerProductionFabricPoll(worker);
    const recoveredAfterReload = await waitFor("ambiguous children promoted after restart", async () => {
      const value = await campaignSnapshot(worker, campaignId);
      return value?.state === "running" &&
        value.children.every(child => child.state === "submitted") &&
        value.children.filter(child => child.recovered_submission_ambiguity === true).length === 3
        ? value
        : null;
    }, 20000);
    assert.deepEqual(recoveredAfterReload.results.map(value => value.assistant_text), ["RESULT_FAST"]);
    assert.deepEqual(
      await Promise.all(fabricChildren(context).map(page => page.evaluate(() => window.submitted.length))),
      submissionsBeforeRecovery,
      "worker-restart ambiguity recovery must not replay child bootstraps"
    );
    assert.equal(fabricChildren(context).length, 4, "ambiguity recovery must not create duplicate children");

    await productPage.evaluate(() => window.completeChild());
    await reliabilityPage.evaluate(() => window.completeChild());
    await architecturePage.evaluate(() => window.completeChild());

    // The real alarm route now captures all four results before exact owned-tab cleanup
    // and delivers terminal feedback through runFeedbackCycle exactly once.
    await triggerProductionFabricPoll(worker);
    await parent.waitForFunction(() => document.body.textContent.includes(
      "PARENT_SYNTHESIS: RESULT_FAST + RESULT_PRODUCT + RESULT_RELIABILITY + RESULT_ARCHITECTURE"
    ), null, { timeout: 30000 });
    const completed = await waitFor("terminal durable campaign", async () => {
      const value = await campaignSnapshot(worker, campaignId);
      return value?.state === "completed" && value.feedback_delivered ? value : null;
    }, 20000);
    assert.deepEqual(
      completed.results.map(value => value.assistant_text).sort(),
      ["RESULT_FAST", "RESULT_PRODUCT", "RESULT_RELIABILITY", "RESULT_ARCHITECTURE"].sort()
    );
    assert.ok(completed.results.every(value => !value.assistant_text.includes("LOCAL_AGENT_CF_CHILD_COMPLETE")));
    assert.ok(completed.results.every(value => !value.assistant_text.includes("LOCAL AGENT BROWSER CHILD")));
    assert.equal(fabricChildren(context).length, 0, "all exact owned child tabs must be closed");

    const terminalPrompts = await parent.evaluate(id => window.submitted.filter(text =>
      text.includes(`Conversation Fabric campaign ${id} completed.`)
    ), campaignId);
    assert.equal(terminalPrompts.length, 1, "terminal feedback must be submitted exactly once");

    // Restart the worker a second time and trigger the same production alarm route.
    // A completed, durably delivered campaign must not reach the parent again.
    worker = await restartServiceWorker(context, worker, { clearSession: true });
    const secondPoll = await triggerProductionFabricPoll(worker);
    assert.equal(
      secondPoll?.ok,
      true,
      "second post-reload production alarm callback must complete before no-replay assertions"
    );
    const afterSecondReload = await campaignSnapshot(worker, campaignId);
    assert.equal(afterSecondReload.state, "completed");
    assert.equal(afterSecondReload.feedback_delivered, true);
    const terminalPromptsAfterReload = await parent.evaluate(id => window.submitted.filter(text =>
      text.includes(`Conversation Fabric campaign ${id} completed.`)
    ), campaignId);
    assert.equal(terminalPromptsAfterReload.length, 1, "terminal campaign must not replay after restart/poll");
    assert.equal(parent.isClosed(), false, "parent must survive child cleanup and worker restarts");

    // Read-only recovery: child tabs are already gone, but a child-scoped inspect must
    // retrieve the durable vault copy without touching terminal delivery receipts.
    const inspectControl = {
      schema_version: 1,
      action: "inspect",
      campaign_id: campaignId,
      child_id: "product"
    };
    const submissionsBeforeInspect = await parent.evaluate(() => window.submitted.length);
    await parent.evaluate(controlValue => {
      window.turn("assistant", "<<<LOCAL_AGENT_CF\n" + JSON.stringify(controlValue) + "\nLOCAL_AGENT_CF>>>");
    }, inspectControl);
    await parent.waitForFunction(before => window.submitted.length > before &&
      window.submitted.some(text =>
        text.includes("Conversation Fabric result inspection:") &&
        text.includes("child=product") &&
        text.includes("RESULT_PRODUCT")
      ), submissionsBeforeInspect, { timeout: 20000 });
    assert.equal(fabricChildren(context).length, 0, "inspect must not reopen a completed child tab");
    const stillDelivered = await campaignSnapshot(worker, campaignId);
    assert.equal(stillDelivered.feedback_delivered, true, "inspect must not reset terminal delivery receipt");

    // Sequential-cycle proof: after campaign A is fully terminal and cleaned, the same
    // parent starts a fresh campaign B. B must not inherit A ownership/results/terminal
    // delivery and A must remain durably delivered without replay.
    const round2Delegate = {
      schema_version: 1,
      action: "delegate",
      children: [
        { id: "round2", role: "verification", prompt: "Return RESULT_ROUND2 in a fresh campaign." }
      ]
    };
    const firstCampaignBeforeRound2 = await campaignSnapshot(worker, campaignId);
    const firstTerminalCountBeforeRound2 = await parent.evaluate(id => window.submitted.filter(text =>
      text.includes(`Conversation Fabric campaign ${id} completed.`)
    ).length, campaignId);
    await parent.evaluate(controlValue => {
      window.turn("assistant", "<<<LOCAL_AGENT_CF\n" + JSON.stringify(controlValue) + "\nLOCAL_AGENT_CF>>>");
    }, round2Delegate);
    const round2Campaign = await waitFor("fresh sequential campaign B", () => worker.evaluate(async previousId => {
      const campaigns = await listConversationFabricCampaigns();
      return campaigns.find(value =>
        value.id !== previousId &&
        value.state === "running" &&
        value.children?.length === 1 &&
        value.children[0].id === "round2"
      ) || null;
    }, campaignId), 30000);
    assert.notEqual(round2Campaign.id, campaignId, "campaign B must have a fresh campaign id");
    assert.equal(round2Campaign.results.length, 0, "campaign B must not inherit campaign A results");
    assert.notEqual(round2Campaign.feedback_delivered, true, "campaign B starts with no terminal delivery receipt");
    assert.equal(fabricChildren(context).length, 1, "campaign B starts only its exact owned child");

    const round2Page = await pageForChild(context, "round2");
    assert.equal(await round2Page.evaluate(() => window.submitted.length), 1, "campaign B bootstrap is submitted once");
    await round2Page.evaluate(() => {
      window.releaseDelayedRoute();
      window.completeChild();
    });
    await triggerProductionFabricPoll(worker);
    const round2Completed = await waitFor("campaign B terminal delivery", async () => {
      const value = await campaignSnapshot(worker, round2Campaign.id);
      return value?.state === "completed" && value.feedback_delivered && value.results?.length === 1 ? value : null;
    }, 20000);
    assert.equal(round2Completed.results[0].assistant_text, "RESULT_ROUND2");
    assert.equal(round2Completed.children[0].id, "round2");
    assert.equal(fabricChildren(context).length, 0, "campaign B exact owned child must be cleaned before later work");

    const firstCampaignAfterRound2 = await campaignSnapshot(worker, campaignId);
    assert.equal(firstCampaignAfterRound2.state, "completed");
    assert.equal(firstCampaignAfterRound2.feedback_delivered, true);
    assert.deepEqual(firstCampaignAfterRound2.results, firstCampaignBeforeRound2.results, "campaign B must not mutate campaign A results");
    const firstTerminalCountAfterRound2 = await parent.evaluate(id => window.submitted.filter(text =>
      text.includes(`Conversation Fabric campaign ${id} completed.`)
    ).length, campaignId);
    assert.equal(
      firstTerminalCountAfterRound2,
      firstTerminalCountBeforeRound2,
      "starting/completing campaign B must not replay campaign A terminal feedback"
    );

    // Operator recovery: retire one problematic exact-owned child, then intentionally
    // re-delegate the missing bounded work with a fresh child id. No automatic replay.
    const retireDelegate = {
      schema_version: 1,
      action: "delegate",
      children: [
        { id: "stuck", role: "verification", prompt: "Remain pending until explicitly retired." }
      ]
    };
    await parent.evaluate(controlValue => {
      window.turn("assistant", "<<<LOCAL_AGENT_CF\n" + JSON.stringify(controlValue) + "\nLOCAL_AGENT_CF>>>");
    }, retireDelegate);
    const retireCampaign = await waitFor("retire scenario campaign", () => worker.evaluate(async previousId => {
      const campaigns = await listConversationFabricCampaigns();
      return campaigns.find(value =>
        value.id !== previousId &&
        value.state === "running" &&
        value.children?.length === 1 &&
        value.children[0].id === "stuck"
      ) || null;
    }, campaignId), 30000);
    const stuckPage = await pageForChild(context, "stuck");
    assert.equal(await stuckPage.evaluate(() => window.submitted.length), 1, "stuck bootstrap must be sent once");
    // Retire is intentionally stricter than cleanup: first let the page expose its
    // canonical child route so the exact page claim can be re-proven immediately before close.
    await stuckPage.evaluate(() => window.releaseDelayedRoute());

    const retireControl = {
      schema_version: 1,
      action: "retire",
      campaign_id: retireCampaign.id,
      child_id: "stuck"
    };
    const parentMessagesBeforeRetire = await parent.evaluate(() => window.submitted.length);
    await parent.evaluate(controlValue => {
      window.turn("assistant", "<<<LOCAL_AGENT_CF\n" + JSON.stringify(controlValue) + "\nLOCAL_AGENT_CF>>>");
    }, retireControl);
    await parent.waitForFunction(before => window.submitted.length > before &&
      window.submitted.some(text =>
        text.includes("was explicitly retired") &&
        text.includes("retryable missing coverage") &&
        text.includes("Wait for the campaign's terminal feedback")
      ), parentMessagesBeforeRetire, { timeout: 20000 });
    await waitFor("retired exact child tab closed", () =>
      context.pages().every(page => !page.url().includes("fabric-child-stuck"))
    );
    const retiredState = await campaignSnapshot(worker, retireCampaign.id);
    assert.equal(retiredState.children[0].state, "failed");
    assert.equal(retiredState.children[0].failure.reason, "operator_retired");
    assert.equal(retiredState.children[0].failure.retryable, true);

    await triggerProductionFabricPoll(worker);
    await waitFor("retire campaign terminal delivery", async () => {
      const value = await campaignSnapshot(worker, retireCampaign.id);
      return value?.state === "completed" && value.feedback_delivered ? value : null;
    }, 20000);

    const retryDelegate = {
      schema_version: 1,
      action: "delegate",
      children: [
        { id: "retry-stuck", role: "verification", prompt: "Return RESULT_RETRY after explicit reassignment." }
      ]
    };
    await parent.evaluate(controlValue => {
      window.turn("assistant", "<<<LOCAL_AGENT_CF\n" + JSON.stringify(controlValue) + "\nLOCAL_AGENT_CF>>>");
    }, retryDelegate);
    const retryCampaign = await waitFor("explicit reassignment campaign", () => worker.evaluate(async retiredId => {
      const campaigns = await listConversationFabricCampaigns();
      return campaigns.find(value =>
        value.id !== retiredId &&
        value.state === "running" &&
        value.children?.length === 1 &&
        value.children[0].id === "retry-stuck"
      ) || null;
    }, retireCampaign.id), 30000);
    const retryPage = await pageForChild(context, "retry-stuck");
    assert.equal(await retryPage.evaluate(() => window.submitted.length), 1, "replacement bootstrap is one explicit new submit");
    await retryPage.evaluate(() => {
      window.releaseDelayedRoute();
      window.completeChild();
    });
    await triggerProductionFabricPoll(worker);
    const retryCompleted = await waitFor("explicit reassignment result", async () => {
      const value = await campaignSnapshot(worker, retryCampaign.id);
      return value?.state === "completed" && value.results?.length === 1 ? value : null;
    }, 20000);
    assert.equal(retryCompleted.results[0].assistant_text, "RESULT_RETRY-STUCK");
    assert.equal(retryCompleted.children[0].id, "retry-stuck");

    // Negative regression: Send succeeds once, but the child never reaches a canonical
    // route. Force the durable campaign age beyond the bounded deadline and prove that
    // recovery fails closed without a second submit or replacement tab.
    const negativeControl = {
      schema_version: 1,
      action: "delegate",
      children: [
        { id: "never", role: "verification", prompt: "Remain on the unresolved route." }
      ]
    };
    await parent.evaluate(controlValue => {
      window.turn("assistant", "<<<LOCAL_AGENT_CF\n" + JSON.stringify(controlValue) + "\nLOCAL_AGENT_CF>>>");
    }, negativeControl);
    const negativeCampaign = await waitFor("negative ambiguous campaign", () => worker.evaluate(async previousId => {
      const campaigns = await listConversationFabricCampaigns();
      return campaigns.find(value =>
        value.id !== previousId &&
        value.state === "running" &&
        value.children?.length === 1 &&
        value.children[0].state === "submission_ambiguous"
      ) || null;
    }, campaignId), 30000);
    const neverPage = await pageForChild(context, "never");
    assert.equal(await neverPage.evaluate(() => window.submitted.length), 1);
    const childTabsBeforeTimeout = fabricChildren(context).length;
    await worker.evaluate(async id => {
      const value = await loadConversationFabricCampaign(id);
      value.created_at = new Date(Date.now() - (16 * 60 * 1000)).toISOString();
      await saveConversationFabricCampaign(value);
    }, negativeCampaign.id);

    await triggerProductionFabricPoll(worker);
    const negativeFailed = await waitFor("bounded ambiguous failure", async () => {
      const value = await campaignSnapshot(worker, negativeCampaign.id);
      return value?.state === "failed" ? value : null;
    }, 20000);
    assert.equal(negativeFailed.failure, "campaign_timed_out_with_pending_children");
    assert.equal(negativeFailed.children[0].state, "failed");
    assert.equal(negativeFailed.children[0].failure.reason, "spawn_submission_ambiguous_timeout");
    assert.equal(negativeFailed.results.length, 0);
    assert.equal(childTabsBeforeTimeout, 1, "negative case starts exactly one owned child tab");
    assert.equal(
      context.pages().filter(page => page.url().includes("/c/fabric-child-never")).length,
      0,
      "terminal cleanup must not leave the exactly-owned failed tab orphaned"
    );

    console.log(
      "PASS: real Bridge delegates four children, keeps three successful-but-ambiguous " +
      "post-submit routes recoverable, durably captures the fast sibling, rebuilds exact " +
      "ownership through a real MV3 worker restart without bootstrap replay or duplicate " +
      "children, captures 4/4 results before cleanup, delivers terminal feedback once, " +
      "and does not replay it after a second restart/poll."
    );
  } catch (error) {
    if (worker && campaignId) {
      try {
        console.error("CAMPAIGN STATE:", JSON.stringify(await campaignSnapshot(worker, campaignId)));
      } catch (_ignored) {}
    }
    if (parent && !parent.isClosed()) {
      console.error("PARENT STATE:", await parent.evaluate(() => ({
        text: document.body.innerText,
        submitted: window.submitted,
        controller: Boolean(globalThis.__localAgentConversationFabricContent)
      })));
    }
    throw error;
  } finally {
    if (context) await context.close();
    await fs.rm(profile, { recursive: true, force: true });
  }
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
