"use strict";

// Offline opt-in content-protocol v2 smoke. The production worker never
// dispatches bridge:spawn-bootstrap-v2, and the installed extension never
// imports the phase adapter, so this cannot open or submit real child chats.
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require(process.env.LOCAL_AGENT_PLAYWRIGHT_MODULE || "playwright");

const bridge = path.resolve(__dirname, "..", "chat_bridge");
const url = "https://chatgpt.com/";
const bootstrap = "LOCAL AGENT CHILD BOOTSTRAP\nPUBLIC SYNTHETIC PROTOCOL V2 FIXTURE";
const intent = Object.freeze({
  transactionId: "spawn-" + "a".repeat(64),
  childRequestDigest: "sha256:" + "b".repeat(64),
  bootstrapDigest: "sha256:" + crypto.createHash("sha256").update(bootstrap).digest("hex"),
  bootstrapText: bootstrap
});
const pageHtml = "<!doctype html><html><head><style>" +
  "#prompt-textarea { min-height: 60px; width: 380px; }" +
  "</style></head><body><form id='form'><div id='prompt-textarea' contenteditable='true'>" +
  "</div><button id='composer-submit-button' type='button'>Send</button></form>" +
  "<div id='turns'></div><script>" +
  "window.__sent=0; document.querySelector('button').onclick=()=>{" +
  "window.__sent++; localStorage.setItem('sent-count',String(Number(localStorage.getItem('sent-count')||0)+1));" +
  "const user=document.createElement('div'); user.dataset.messageAuthorRole='user';" +
  "window.__submittedComposerText=document.querySelector('#prompt-textarea').innerText;" +
  "user.textContent=window.__submittedComposerText;" +
  "document.querySelector('#turns').appendChild(user);" +
  "history.pushState({},'', '/c/spawn-v2-synthetic-child');" +
  "};</script></body></html>";

async function load(page, phase) {
  await page.addScriptTag({ path: path.join(bridge, "control_protocol.js") });
  if (phase) {
    await page.addScriptTag({ path: path.join(bridge, "spawn_phase_model.js") });
    await page.addScriptTag({ path: path.join(bridge, "spawn_phase_storage.js") });
  }
  await page.addScriptTag({ path: path.join(bridge, "spawn_content.js") });
}

async function send(page, type = "bridge:spawn-bootstrap-v2") {
  return page.evaluate(({ intent, type }) => new Promise((resolve, reject) => {
    const listeners = globalThis.__messageListeners;
    const message = { type, ...intent };
    let settled = false;
    for (const listener of listeners) {
      const async = listener(message, {}, result => {
        if (!settled) { settled = true; resolve(result); }
      });
      if (async === true) return;
    }
    reject(new Error("no installed spawn content listener"));
  }), { intent, type });
}
const snapshot = page => page.evaluate(() => ({
  sent: Number(localStorage.getItem("sent-count") || 0),
  tabSends: window.__sent,
  submittedComposerText: window.__submittedComposerText || "",
  draft: document.querySelector("#prompt-textarea")?.textContent,
  rawClaim: sessionStorage.getItem("local-agent-spawn-phase-v2:" + "spawn-" + "a".repeat(64))
}));

(async () => {
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), "spawn-v2-opt-in-"));
  let context;
  try {
    context = await chromium.launchPersistentContext(profile, {
      channel: "chromium", headless: true, args: ["--disable-background-networking"]
    });
    await context.setOffline(true);
    await context.route("https://chatgpt.com/**", route =>
      route.fulfill({ contentType: "text/html", body: pageHtml }));
    await context.addInitScript(() => {
      globalThis.__messageListeners = [];
      globalThis.chrome = {
        runtime: { onMessage: {
          addListener(callback) { globalThis.__messageListeners.push(callback); },
          removeListener(callback) {
            globalThis.__messageListeners = globalThis.__messageListeners.filter(x => x !== callback);
          }
        } }
      };
    });

    const page = await context.newPage();
    await page.goto(url);
    await load(page, false);
    const disabled = await send(page);
    assert.equal(disabled.reason, "spawn_v2_inactive");
    assert.equal((await snapshot(page)).draft, "");
    assert.equal((await snapshot(page)).sent, 0);

    await page.addScriptTag({ path: path.join(bridge, "spawn_phase_model.js") });
    await page.addScriptTag({ path: path.join(bridge, "spawn_phase_storage.js") });
    const done = await send(page);
    assert.equal(done.ok, true, JSON.stringify(done));
    assert.equal(done.reason, "identity_discovered");
    assert.equal((await snapshot(page)).sent, 1);
    assert.equal((await snapshot(page)).tabSends, 1);
    assert.equal(
      (await snapshot(page)).submittedComposerText.replace(/\s+/g, " ").trim(),
      bootstrap.replace(/\s+/g, " ").trim(),
      "only actual composer text can produce a successful child echo"
    );
    assert.equal(JSON.parse((await snapshot(page)).rawClaim).phase, "submitted");
    const duplicate = await send(page);
    assert.equal(duplicate.reason, "identity_discovered");
    const legacy = await send(page, "bridge:spawn-bootstrap");
    assert.equal(legacy.reason, "spawn_v2_claim_exists");
    assert.equal((await snapshot(page)).sent, 1);

    // Same page/transaction after reload cannot submit the bootstrap again.
    await page.reload();
    await load(page, true);
    const afterReload = await send(page);
    assert.equal(afterReload.reason, "spawn_submission_ambiguous");
    assert.equal((await snapshot(page)).sent, 1);

    // A Send exception leaves durable submit_armed: never a second click.
    const ambiguous = await context.newPage();
    await ambiguous.goto(url);
    await load(ambiguous, true);
    await ambiguous.evaluate(() => {
      document.querySelector("button").click = () => { throw new Error("lost Send ACK"); };
    });
    const failed = await send(ambiguous);
    assert.equal(failed.reason, "spawn_v2_submission_ambiguous");
    assert.equal(JSON.parse((await snapshot(ambiguous)).rawClaim).phase, "submit_armed");
    assert.equal((await snapshot(ambiguous)).tabSends, 0,
      "throwing button must not be misreported as a submitted message");
    assert.equal((await snapshot(ambiguous)).sent, 1);
    assert.equal((await send(ambiguous, "bridge:spawn-bootstrap")).reason, "spawn_v2_claim_exists");
    await ambiguous.reload();
    await load(ambiguous, true);
    assert.equal((await send(ambiguous)).reason, "spawn_submission_ambiguous");
    assert.equal((await snapshot(ambiguous)).sent, 1);

    // Two protocols racing on the same page cannot both Send.
    const concurrent = await context.newPage();
    await concurrent.goto(url);
    await load(concurrent, true);
    // Deliver both messages inside one browser task, rather than through
    // separate Playwright evaluate calls which may be serialized by CDP.
    const pair = await concurrent.evaluate(intent => {
      const deliver = type => new Promise((resolve, reject) => {
        const message = { type, ...intent };
        for (const listener of globalThis.__messageListeners) {
          if (listener(message, {}, resolve) === true) return;
        }
        reject(new Error("missing spawn content listener"));
      });
      return Promise.all([
        deliver("bridge:spawn-bootstrap-v2"),
        deliver("bridge:spawn-bootstrap")
      ]);
    }, intent);
    assert.equal(pair.filter(x => x.reason === "spawn_submission_in_progress").length, 1);
    assert.equal(pair.filter(x => x.reason === "identity_discovered").length, 1);
    assert.equal((await snapshot(concurrent)).tabSends, 1);

    // Corrupt legacy claims fail closed rather than allowing a new v2 claim.
    const corrupt = await context.newPage();
    await corrupt.goto(url);
    await load(corrupt, true);
    await corrupt.evaluate(() => {
      sessionStorage.setItem("local-agent:conversation-spawn:" + "spawn-" + "a".repeat(64), "{invalid");
    });
    assert.equal((await send(corrupt)).reason, "spawn_legacy_claim_invalid");
    assert.equal((await snapshot(corrupt)).tabSends, 0);
    assert.equal((await snapshot(corrupt)).draft, "");

    const unreadable = await context.newPage();
    await unreadable.goto(url);
    await load(unreadable, true);
    await unreadable.evaluate(() => {
      const original = Storage.prototype.getItem;
      Storage.prototype.getItem = function(key) {
        if (key.startsWith("local-agent:conversation-spawn:")) throw Error("storage denied");
        return original.call(this, key);
      };
    });
    assert.equal((await send(unreadable)).reason, "spawn_legacy_claim_invalid");
    assert.equal((await snapshot(unreadable)).tabSends, 0);
    assert.equal((await snapshot(unreadable)).draft, "");

    // Reject foreign drafts and ambiguous duplicate editors without changes.
    const foreign = await context.newPage();
    await foreign.goto(url);
    await load(foreign, true);
    await foreign.evaluate(() => { document.querySelector("#prompt-textarea").textContent = "USER DRAFT"; });
    assert.equal((await send(foreign)).reason, "spawn_composer_not_empty");
    assert.equal((await snapshot(foreign)).draft, "USER DRAFT");
    assert.equal((await snapshot(foreign)).rawClaim, null);
    const duplicateComposer = await context.newPage();
    await duplicateComposer.goto(url);
    await load(duplicateComposer, true);
    await duplicateComposer.evaluate(() => {
      document.querySelector("form").insertAdjacentHTML(
        "beforeend", "<div contenteditable='true' style='min-height:10px'>other</div>"
      );
    });
    assert.equal((await send(duplicateComposer)).reason, "spawn_v2_composer_ambiguous");
    assert.equal((await snapshot(duplicateComposer)).rawClaim, null);

    // A storage write failure must leave the composer completely untouched.
    const denied = await context.newPage();
    await denied.goto(url);
    await load(denied, true);
    await denied.evaluate(() => {
      const original = Storage.prototype.setItem;
      Storage.prototype.setItem = function(key, value) {
        if (key.startsWith("local-agent-spawn-phase-v2:")) throw Error("synthetic quota denied");
        return original.call(this, key, value);
      };
    });
    assert.equal((await send(denied)).reason, "spawn_v2_claim_invalid");
    assert.equal((await snapshot(denied)).draft, "");
    assert.equal((await snapshot(denied)).rawClaim, null);
    console.log("Experimental spawn-v2 content integration smoke passed.");
  } finally {
    if (context) await context.close().catch(() => undefined);
    await fs.rm(profile, { recursive: true, force: true });
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
