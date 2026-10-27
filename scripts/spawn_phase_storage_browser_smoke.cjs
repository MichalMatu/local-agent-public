"use strict";

// Isolated real DOM/sessionStorage proof for the inactive v2 claim adapter.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require(process.env.LOCAL_AGENT_PLAYWRIGHT_MODULE || "playwright");

const bridge = path.join(__dirname, "..", "chat_bridge");
const url = "https://chatgpt.com/c/spawn-phase-v2-synthetic";
const fixture = "<!doctype html><html><body><div id='composer' contenteditable='true'></div></body></html>";
const intent = {
  transaction_id: "spawn-" + "a".repeat(64),
  child_request_digest: "sha256:" + "b".repeat(64),
  bootstrap_digest: "sha256:" + "c".repeat(64)
};

async function install(page) {
  await page.addScriptTag({ path: path.join(bridge, "spawn_phase_model.js") });
  await page.addScriptTag({ path: path.join(bridge, "spawn_phase_storage.js") });
}

(async () => {
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), "spawn-v2-page-"));
  let browser;
  try {
    browser = await chromium.launchPersistentContext(profile, { channel: "chromium", headless: true });
    await browser.setOffline(true);
    await browser.route("https://chatgpt.com/**", route =>
      route.fulfill({ contentType: "text/html", body: fixture }));
    const page = await browser.newPage();
    await page.goto(url);
    await install(page);

    const initial = await page.evaluate(identity => {
      const phase = globalThis.LocalAgentSpawnPhaseStorage;
      const composer = document.querySelector("#composer");
      const before = composer.textContent;
      const denied = {
        getItem: key => sessionStorage.getItem(key),
        setItem() { throw new Error("storage quota"); }
      };
      let error = null;
      try {
        phase.prepare(denied, identity);
      } catch (e) { error = String(e); }
      if (!error?.includes("storage quota")) throw new Error("storage denial must fail closed");
      if (composer.textContent !== before) throw new Error("composer mutated before persisted claim");

      const created = phase.prepare(sessionStorage, identity);
      if (created.claim.phase !== "prepared") throw new Error("phase claim not prepared");
      phase.mutatePreparedComposer(sessionStorage, identity, () => {
        composer.textContent = "SYNTHETIC ONLY";
      });
      if (composer.textContent !== "SYNTHETIC ONLY") throw new Error("synthetic draft missing");
      phase.advance(sessionStorage, identity, "draft_verified");
      return { claim: phase.read(sessionStorage, identity), draft: composer.textContent };
    }, intent);
    assert.equal(initial.claim.phase, "draft_verified");
    assert.equal(initial.draft, "SYNTHETIC ONLY");

    // Same page reload preserves the exact page-local claim, so no fresh draft
    // can be written without an explicit, bounded retry.
    await page.reload();
    await install(page);
    const afterReload = await page.evaluate(identity => {
      const phase = globalThis.LocalAgentSpawnPhaseStorage;
      const claim = phase.read(sessionStorage, identity);
      let writes = 0;
      try {
        phase.mutatePreparedComposer(sessionStorage, identity, () => writes++);
      } catch (_error) {}
      return { claim, writes };
    }, intent);
    assert.equal(afterReload.claim.phase, "draft_verified");
    assert.equal(afterReload.writes, 0);

    const armed = await page.evaluate(async identity => {
      const phase = globalThis.LocalAgentSpawnPhaseStorage;
      let sends = 0;
      try {
        await phase.armAndSend(sessionStorage, identity, () => {
          sends++;
          throw new Error("ack lost after Send");
        });
      } catch (error) {
        if (!String(error).includes("ack lost")) throw error;
      }
      return { sends, claim: phase.read(sessionStorage, identity) };
    }, intent);
    assert.equal(armed.sends, 1);
    assert.equal(armed.claim.phase, "submit_armed");

    await page.reload();
    await install(page);
    const final = await page.evaluate(async identity => {
      const phase = globalThis.LocalAgentSpawnPhaseStorage;
      let sends = 0;
      const recovered = phase.prepare(sessionStorage, identity);
      try { await phase.armAndSend(sessionStorage, identity, () => sends++); } catch (_error) {}
      return { action: recovered.action, claim: phase.read(sessionStorage, identity), sends };
    }, intent);
    assert.equal(final.action, "reconcile_only");
    assert.equal(final.claim.phase, "submit_armed");
    assert.equal(final.sends, 0);
    console.log("Inactive spawn-v2 page-local claim browser smoke passed.");
  } finally {
    if (browser) await browser.close().catch(() => undefined);
    await fs.rm(profile, { recursive: true, force: true });
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
