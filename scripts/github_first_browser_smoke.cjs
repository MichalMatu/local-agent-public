/* Browser-only acceptance: isolated MV3 extension boots with Send locked.
 * Never navigate to or submit a real ChatGPT conversation.
 */
"use strict";
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require(process.env.LOCAL_AGENT_PLAYWRIGHT_MODULE || "playwright");

(async () => {
  const extensionDir = path.resolve(__dirname, "../chat_bridge");
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "la-github-first-chrome-"));
  let context;
  try {
    context = await chromium.launchPersistentContext(profile, {
      headless: true,
      args: [
        `--disable-extensions-except=${extensionDir}`,
        `--load-extension=${extensionDir}`
      ]
    });
    let serviceWorker = context.serviceWorkers()[0];
    if (!serviceWorker) serviceWorker = await context.waitForEvent("serviceworker", { timeout: 15000 });
    const result = await serviceWorker.evaluate(() => ({
      version: chrome.runtime.getManifest().version,
      sendReleased: GITHUB_FIRST_LOCAL_SEND_RELEASED
    }));
    if (result.version !== "0.9.0" || result.sendReleased !== false) {
      throw new Error("GitHub-first browser extension identity or Send gate mismatch");
    }
    console.log("GITHUB_FIRST_CHROMIUM_BOOT_PASS no_prompt_submit=1");
  } finally {
    if (context) await context.close();
    fs.rmSync(profile, { recursive: true, force: true });
  }
})().catch(error => { console.error(String(error)); process.exitCode = 1; });
