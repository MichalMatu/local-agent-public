"use strict";

// Isolated Chromium only; do not launch or attach to the operator's Chrome.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require(process.env.LOCAL_AGENT_PLAYWRIGHT_MODULE || "playwright");

const DISPATCH = "fabric-" + "a".repeat(32);
const TOKEN = "github_pat_isolated_popup_smoke_example_6D4k";
const extension = path.resolve(__dirname, "../chat_bridge");
const timeout = setTimeout(() => {
  console.error("PRIVATE_POPUP_CHROMIUM_TIMEOUT");
  process.exit(124);
}, 100000);
timeout.unref();

(async () => {
  const profile = await fs.mkdtemp(path.join(os.tmpdir(), "fabric-popup-0816-"));
  let context;
  try {
    context = await chromium.launchPersistentContext(profile, {
      channel: "chromium",
      headless: true,
      ignoreDefaultArgs: ["--disable-extensions"],
      args: ["--disable-background-networking",
        "--disable-extensions-except=" + extension,
        "--load-extension=" + extension]
    });
    await context.setOffline(true);
    const worker = context.serviceWorkers()[0] ||
      await context.waitForEvent("serviceworker", { timeout: 15000 });
    const extensionId = new URL(worker.url()).host;
    async function popup() {
      const page = await context.newPage();
      await page.goto("chrome-extension://" + extensionId + "/popup.html");
      return page;
    }
    let page = await popup();
    await page.locator(".settings-panel summary").click();
    assert.equal(await page.locator(".settings-panel").evaluate(el => el.open), true);
    await page.locator("#privateDispatchId").fill(DISPATCH);
    await page.locator("#privateFabricSaveDispatch").click();
    await page.locator("#privateFabricDispatchState").getByText("Saved for this extension").waitFor();

    await page.close();
    page = await popup();
    await page.waitForFunction(() => document.querySelector(".settings-panel").open);
    assert.equal(await page.locator("#privateDispatchId").inputValue(), DISPATCH);
    await page.locator("#privateGitHubToken").fill(TOKEN);
    await page.locator("#privateFabricSaveToken").click();
    await page.waitForFunction(suffix => {
      const status = document.querySelector("#privateFabricTokenState").textContent;
      return status.includes("••••" + suffix);
    }, TOKEN.slice(-4));
    assert.equal(await page.locator("#privateGitHubToken").inputValue(), "");
    assert.ok(!(await page.locator("#privateFabricTokenState").textContent()).includes(TOKEN));
    const stored = await page.evaluate(() => chrome.storage.local.get(null));
    assert.ok(!JSON.stringify(stored).includes(TOKEN),
      "private token must never persist in chrome.storage.local");

    await page.close();
    page = await popup();
    await page.waitForFunction(() =>
      document.querySelector(".settings-panel").open &&
      document.querySelector("#privateFabricTokenState").textContent.includes("••••" + "6D4k")
    );
    assert.equal(await page.locator("#privateDispatchId").inputValue(), DISPATCH);
    await page.locator("#privateFabricForgetToken").click();
    await page.locator("#privateFabricTokenState").getByText("No token saved for this browser session").waitFor();
    await page.close();

    page = await popup();
    await page.waitForFunction(() => document.querySelector(".settings-panel").open);
    assert.equal(await page.locator("#privateDispatchId").inputValue(), DISPATCH);
    assert.equal((await page.locator("#privateFabricTokenState").textContent()),
      "No token saved for this browser session");
    console.log("PRIVATE_POPUP_CHROMIUM_REOPEN_PERSISTENCE_PASS");
  } finally {
    if (context) await context.close().catch(() => undefined);
    await fs.rm(profile, { recursive: true, force: true });
    clearTimeout(timeout);
  }
})().catch(err => { console.error(err); process.exitCode = 1; });
