"use strict";

// Synthetic Conversation Fabric spawn proof. This fixture is offline and never contacts
// the operator's real ChatGPT session.
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

function intent(label, tabId = null) {
  const bootstrapText = [
    "[LA_CONVERSATION_FABRIC_CHILD]",
    `request=${label}`,
    `nonce=${sha256(`bootstrap:${label}`).slice(0, 16)}`,
    "Continue only the admitted bounded reasoning scope."
  ].join("\n");
  return {
    schema_version: 1,
    transaction_id: `spawn-${sha256(`transaction:${label}`)}`,
    child_request_digest: `sha256:${sha256(`request:${label}`)}`,
    bootstrap_digest: `sha256:${sha256(bootstrapText)}`,
    bootstrap_text: bootstrapText,
    tab_id: tabId
  };
}

function markerUrl(spawnIntent) {
  return `https://chatgpt.com/#la-spawn=${spawnIntent.transaction_id}`;
}

async function bounded(label, promise, timeoutMs = 12_000) {
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

async function poll(label, predicate, timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = await predicate();
    if (value) return value;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`${label} exceeded ${timeoutMs} ms`);
}

const fixture = `<!doctype html><html><body>
<form id="composer-form">
  <div id="prompt-textarea" class="ProseMirror" contenteditable="true" role="textbox"></div>
  <button id="composer-submit-button" type="submit">Send</button>
</form>
<script>
window.submits = 0;
window.spawnRouteMode = "normal";
window.replaceComposerOnInput = false;
window.composerReplacements = 0;
window.spawnTargetPath = (() => {
  const params = new URLSearchParams(location.hash.replace(/^#/, ""));
  const transaction = params.get("la-spawn") || "spawn-unknown";
  return "/c/synthetic-" + transaction.slice(-12);
})();
window.completeSpawnRoute = () => history.pushState({}, "", window.spawnTargetPath);
document.addEventListener("input", (event) => {
  if (!window.replaceComposerOnInput || event.target?.id !== "prompt-textarea") return;
  const current = event.target;
  queueMicrotask(() => {
    if (!current.isConnected) return;
    const replacement = current.cloneNode(true);
    current.replaceWith(replacement);
    window.replaceComposerOnInput = false;
    window.composerReplacements++;
  });
}, true);
document.querySelector("form").onsubmit = (event) => {
  event.preventDefault();
  window.submits++;
  const input = document.querySelector("#prompt-textarea");
  const message = document.createElement("div");
  message.dataset.messageAuthorRole = "user";
  message.textContent = input.innerText || input.textContent || "";
  document.body.append(message);
  input.textContent = "";
  input.dispatchEvent(new Event("input", { bubbles: true }));
  if (window.spawnRouteMode === "normal") window.completeSpawnRoute();
  if (window.spawnRouteMode === "unexpected") history.pushState({}, "", "/unexpected");
};
</script></body></html>`;

(async () => {
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), "conversation-spawn-browser-"));
  let context;
  let cdp;
  let rpcId = 0;
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
        return route.fulfill({ contentType: "text/html", body: fixture });
      }
      return route.abort();
    });

    const initialWorker = context.serviceWorkers()[0] || await context.waitForEvent("serviceworker");
    const extensionId = new URL(initialWorker.url()).host;
    const extensionWorkerUrl = `chrome-extension://${extensionId}/service_worker.js`;
    const controller = context.pages()[0] || await context.newPage();
    cdp = await context.newCDPSession(controller);
    await cdp.send("ServiceWorker.enable");

    const workerTarget = async () => {
      const targets = await cdp.send("Target.getTargets");
      return targets.targetInfos.find((item) =>
        item.type === "service_worker" && item.url === extensionWorkerUrl
      ) || null;
    };

    const rawSessionCommand = async (sessionId, method, params = {}) => {
      const id = ++rpcId;
      return bounded(
        `${method} response`,
        new Promise(async (resolve, reject) => {
          const onMessage = (event) => {
            if (event.sessionId !== sessionId) return;
            let message;
            try {
              message = JSON.parse(event.message);
            } catch (_error) {
              return;
            }
            if (message.id !== id) return;
            cdp.off("Target.receivedMessageFromTarget", onMessage);
            if (message.error) reject(new Error(message.error.message || `${method} failed`));
            else resolve(message.result || {});
          };
          cdp.on("Target.receivedMessageFromTarget", onMessage);
          try {
            await cdp.send("Target.sendMessageToTarget", {
              sessionId,
              message: JSON.stringify({ id, method, params })
            });
          } catch (error) {
            cdp.off("Target.receivedMessageFromTarget", onMessage);
            reject(error);
          }
        }),
        12_000
      );
    };

    const evaluateWorker = async (expression, timeoutMs = 12_000) => {
      const target = await poll("extension worker target", workerTarget, 8000);
      const attached = await cdp.send("Target.attachToTarget", {
        targetId: target.targetId,
        flatten: false
      });
      try {
        const response = await bounded(
          "worker Runtime.evaluate",
          rawSessionCommand(attached.sessionId, "Runtime.evaluate", {
            expression,
            awaitPromise: true,
            returnByValue: true,
            userGesture: true
          }),
          timeoutMs
        );
        if (response.exceptionDetails) {
          const detail = response.exceptionDetails.exception?.description ||
            response.exceptionDetails.text || "worker evaluation failed";
          throw new Error(detail);
        }
        return response.result?.value;
      } finally {
        await cdp.send("Target.detachFromTarget", { sessionId: attached.sessionId }).catch(() => null);
      }
    };

    const callWorker = (name, value, timeoutMs = 12_000) => {
      assert.match(name, /^[A-Za-z][A-Za-z0-9_]*$/);
      return evaluateWorker(`Promise.resolve(${name}(${JSON.stringify(value)}))`, timeoutMs);
    };

    const createSpawn = (value) => callWorker("createConversationSpawnTab", value);
    const submitSpawn = (value) => callWorker("submitConversationSpawnBootstrap", value, 15_000);
    const reconcileSpawn = (value) => callWorker("reconcileConversationSpawn", value);
    const bridgeState = () => evaluateWorker("Promise.resolve(getBridgeState())");
    const chatTabs = () => evaluateWorker(
      'chrome.tabs.query({url:["https://chatgpt.com/*"]})'
    );
    const allTabs = () => evaluateWorker("chrome.tabs.query({})");
    const closeTab = (tabId) => evaluateWorker(`chrome.tabs.remove(${Number(tabId)}).then(() => true)`);

    const wakeExtensionWorker = async () => {
      const trigger = await context.newPage();
      try {
        await trigger.goto(`chrome-extension://${extensionId}/popup.html`);
        const wake = await trigger.evaluate(() => new Promise((resolve) => {
          chrome.runtime.sendMessage({ type: "bridge:get-state" }, (response) => {
            const error = chrome.runtime.lastError?.message || "";
            resolve({ error, response: response || null });
          });
        }));
        assert.equal(wake.error, "", wake.error);
        assert.ok(wake.response?.state, "popup message must wake the MV3 worker and return state");
      } finally {
        await trigger.close().catch(() => null);
      }
      await poll("extension worker target after wake", workerTarget, 8000);
    };

    const terminateExtensionWorker = async () => {
      assert.ok(await workerTarget(), "extension service worker target must exist before forced termination");
      await cdp.send("ServiceWorker.stopAllWorkers");
      await wakeExtensionWorker();
      assert.ok(await workerTarget(), "extension service worker target must exist after wake");
      await bridgeState();
    };

    const seedFixtureSpawn = async (pending) => {
      const page = await context.newPage();
      await page.goto(markerUrl(pending));
      assert.equal(page.url(), markerUrl(pending));
      const recovered = await createSpawn(pending);
      assert.equal(recovered.reason, "tab_recovered", JSON.stringify(recovered));
      assert.ok(Number.isInteger(recovered.tabId));
      return {
        page,
        tabId: recovered.tabId,
        value: { ...pending, tab_id: recovered.tabId }
      };
    };

    // Prove the production tabs.create path and immediate lost-ACK recovery. New creates
    // must stage on an extension-owned URL; putting #la-spawn on chatgpt.com itself causes
    // the real app to fall back to mweb before the composer is usable.
    await terminateExtensionWorker();
    const createProof = intent("tabs-create-proof");
    const tabsBeforeCreateProof = (await allTabs()).length;
    const created = await createSpawn(createProof);
    assert.equal(created.reason, "tab_created", JSON.stringify(created));
    const recoveredCreate = await createSpawn(createProof);
    assert.equal(recoveredCreate.reason, "tab_recovered", JSON.stringify(recoveredCreate));
    assert.equal(recoveredCreate.tabId, created.tabId);
    assert.equal((await allTabs()).length, tabsBeforeCreateProof + 1);
    const createdTab = await evaluateWorker(`chrome.tabs.get(${created.tabId})`);
    const createdUrl = String(createdTab?.url || createdTab?.pendingUrl || "");
    assert.match(
      createdUrl,
      new RegExp(`^chrome-extension://${extensionId}/spawn_staging\\.html#la-spawn=`),
      createdUrl
    );
    assert.equal(createdUrl.startsWith("https://chatgpt.com/#la-spawn="), false, createdUrl);
    await closeTab(created.tabId);
    console.log("PASS: production create stages off-domain and lost-create ACK recovery keeps one exact transaction tab");

    // Playwright-owned ChatGPT pages are intercepted by the offline fixture. Legacy
    // marker fixtures remain read-only compatibility coverage; production no longer creates them.
    const firstPending = intent("restart-before-bootstrap");
    const firstSeed = await seedFixtureSpawn(firstPending);

    // A stale/wrong tab id cannot redirect another transaction's bootstrap.
    const foreignPending = intent("foreign-tab-claim");
    const foreignSeed = await seedFixtureSpawn(foreignPending);
    const wrongClaim = { ...firstPending, tab_id: foreignSeed.tabId };
    const wrongClaimResult = await submitSpawn(wrongClaim);
    assert.equal(wrongClaimResult.reason, "spawn_tab_claim_mismatch", JSON.stringify(wrongClaimResult));
    assert.equal(await foreignSeed.page.evaluate(() => window.submits), 0);
    await foreignSeed.page.close();
    console.log("PASS: transaction A cannot consume transaction B's fresh tab claim");

    // Forced MV3 termination after tab discovery must continue in the same exact page.
    await terminateExtensionWorker();
    const firstResult = await submitSpawn(firstSeed.value);
    assert.equal(firstResult.ok, true, JSON.stringify(firstResult));
    assert.equal(firstResult.reason, "identity_discovered", JSON.stringify(firstResult));
    assert.equal(await firstSeed.page.evaluate(() => window.submits), 1);
    const firstChildUrl = firstResult.childConversationUrl;
    assert.match(firstChildUrl, /^https:\/\/chatgpt\.com\/c\/synthetic-/);

    // Route discovery alone does not silently create a normal Bridge conversation.
    const stateBeforeRegistration = await bridgeState();
    assert.equal(Object.keys(stateBeforeRegistration.conversations).length, 0);
    console.log("PASS: fresh-chat route transition is observed before durable registration/adoption");

    // Duplicate submit and a lost accepted ACK after restart reconcile only.
    const duplicate = await submitSpawn(firstSeed.value);
    assert.equal(duplicate.childConversationUrl, firstChildUrl, JSON.stringify(duplicate));
    assert.equal(await firstSeed.page.evaluate(() => window.submits), 1);
    await terminateExtensionWorker();
    const recoveredIdentity = await reconcileSpawn(firstSeed.value);
    assert.equal(recoveredIdentity.ok, true, JSON.stringify(recoveredIdentity));
    assert.equal(recoveredIdentity.childConversationUrl, firstChildUrl);
    assert.equal(await firstSeed.page.evaluate(() => window.submits), 1);
    console.log("PASS: accepted bootstrap survives lost ACK and MV3 termination without a second submit");

    // Termination after submission started but before child identity acknowledgement.
    const interruptedSeed = await seedFixtureSpawn(intent("restart-after-submit"));
    await interruptedSeed.page.evaluate(() => { window.spawnRouteMode = "delayed"; });
    const inFlight = submitSpawn(interruptedSeed.value).catch(() => null);
    await interruptedSeed.page.waitForFunction(() => window.submits === 1);
    await terminateExtensionWorker();
    await bounded("settle interrupted worker call", inFlight, 3000).catch(() => null);
    await interruptedSeed.page.evaluate(() => window.completeSpawnRoute());
    const interruptedRecovery = await reconcileSpawn(interruptedSeed.value);
    assert.equal(interruptedRecovery.ok, true, JSON.stringify(interruptedRecovery));
    assert.equal(interruptedRecovery.reason, "identity_discovered", JSON.stringify(interruptedRecovery));
    assert.equal(await interruptedSeed.page.evaluate(() => window.submits), 1);
    console.log("PASS: submission-started termination recovers the same exact child from the original tab claim");

    // An operator edit after insertion but before Send remains untouched and is never submitted.
    const editedSeed = await seedFixtureSpawn(intent("operator-edit"));
    await editedSeed.page.evaluate(() => { document.querySelector("#composer-submit-button").disabled = true; });
    const editedDelivery = submitSpawn(editedSeed.value);
    await editedSeed.page.waitForFunction(() => document.querySelector("#prompt-textarea").textContent.length > 0);
    await editedSeed.page.locator("#prompt-textarea").fill("Operator text must survive");
    await editedSeed.page.evaluate(() => { document.querySelector("#composer-submit-button").disabled = false; });
    const editedResult = await editedDelivery;
    assert.equal(editedResult.reason, "spawn_composer_changed", JSON.stringify(editedResult));
    assert.equal(await editedSeed.page.locator("#prompt-textarea").textContent(), "Operator text must survive");
    assert.equal(await editedSeed.page.evaluate(() => window.submits), 0);
    console.log("PASS: unrelated operator edit is preserved before the submit boundary");

    // ChatGPT may replace the contenteditable node after input. Continue only when the
    // replacement still carries the exact inserted bootstrap text.
    const replacedSeed = await seedFixtureSpawn(intent("composer-replacement"));
    await replacedSeed.page.evaluate(() => {
      window.replaceComposerOnInput = true;
      document.querySelector("#composer-submit-button").disabled = true;
    });
    const replacedDelivery = submitSpawn(replacedSeed.value);
    await replacedSeed.page.waitForFunction(() => window.composerReplacements === 1);
    assert.equal(
      await replacedSeed.page.locator("#prompt-textarea").evaluate(
        (element) => element.innerText || element.textContent || ""
      ),
      replacedSeed.value.bootstrap_text
    );
    await replacedSeed.page.evaluate(() => { document.querySelector("#composer-submit-button").disabled = false; });
    const replacedResult = await replacedDelivery;
    assert.equal(replacedResult.ok, true, JSON.stringify(replacedResult));
    assert.equal(replacedResult.reason, "identity_discovered", JSON.stringify(replacedResult));
    assert.equal(await replacedSeed.page.evaluate(() => window.submits), 1);
    console.log("PASS: exact-text composer DOM replacement remains safe to submit once");

    // Unexpected path fails before submission.
    const wrongRouteSeed = await seedFixtureSpawn(intent("unexpected-route"));
    await wrongRouteSeed.page.evaluate(() => history.pushState({}, "", "/unexpected"));
    const wrongRouteResult = await submitSpawn(wrongRouteSeed.value);
    assert.equal(wrongRouteResult.reason, "spawn_unexpected_route", JSON.stringify(wrongRouteResult));
    assert.equal(await wrongRouteSeed.page.evaluate(() => window.submits), 0);
    console.log("PASS: unexpected origin/path cannot consume a spawn bootstrap");

    // Ambiguous post-submit outcome never creates a replacement tab automatically.
    const ambiguousSeed = await seedFixtureSpawn(intent("ambiguous-close"));
    await ambiguousSeed.page.evaluate(() => { window.spawnRouteMode = "delayed"; });
    const ambiguousResult = await submitSpawn(ambiguousSeed.value);
    assert.equal(ambiguousResult.reason, "spawn_submission_ambiguous", JSON.stringify(ambiguousResult));
    assert.equal(await ambiguousSeed.page.evaluate(() => window.submits), 1);
    const tabCountBeforeClose = (await chatTabs()).length;
    await ambiguousSeed.page.close();
    const missingResult = await reconcileSpawn(ambiguousSeed.value);
    assert.equal(missingResult.reason, "spawn_tab_unavailable", JSON.stringify(missingResult));
    assert.equal((await chatTabs()).length, tabCountBeforeClose - 1);
    console.log("PASS: closed ambiguous original tab never authorizes an automatic replacement");

    // Invalid bootstrap identity is rejected before tabs.create.
    const invalid = intent("invalid-digest");
    invalid.bootstrap_digest = `sha256:${"0".repeat(64)}`;
    const tabsBeforeInvalid = (await allTabs()).length;
    await assert.rejects(createSpawn(invalid), /bootstrap digest mismatch/);
    assert.equal((await allTabs()).length, tabsBeforeInvalid);
    console.log("PASS: bootstrap digest mismatch is fail-closed before browser side effects");
  } finally {
    if (cdp) await cdp.detach().catch(() => null);
    if (context) await context.close().catch(() => null);
    await fs.rm(profile, { recursive: true, force: true });
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
