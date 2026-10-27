"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const readline = require("node:readline");
const { spawn } = require("node:child_process");

const root = path.resolve(__dirname, "..");
const playwrightModule = require.resolve(process.env.LOCAL_AGENT_PLAYWRIGHT_MODULE || "playwright");

function sha256(text) {
  return crypto.createHash("sha256").update(String(text), "utf8").digest("hex");
}

function intent(label, tabId = null) {
  const bootstrapText = [
    "LOCAL AGENT CHILD BOOTSTRAP",
    `request=${label}`,
    "Continue only the bounded synthetic live-slice smoke scope."
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

function ownership(label) {
  const value = intent(label);
  return {
    transaction_id: value.transaction_id,
    child_request_digest: value.child_request_digest,
    bootstrap_digest: value.bootstrap_digest
  };
}

const childResultUrl = "https://chatgpt.com/c/44444444-4444-4444-8444-444444444444";
const composerFixture = `<!doctype html><html><body>
<form id="composer-form">
  <div id="prompt-textarea" class="ProseMirror" contenteditable="true" role="textbox"></div>
  <button id="composer-submit-button" type="submit">Send</button>
</form>
<script>
  document.getElementById("composer-form").addEventListener("submit", (event) => {
    event.preventDefault();
    history.pushState({}, "", "/c/44444444-4444-4444-8444-444444444444");
  });
</script>
</body></html>`;

const childResultFixture = `<!doctype html><html><body>
<div data-turn-key="assistant-turn-1">
  <div data-user-message-bubble="true">USER TEXT MUST NOT LEAK</div>
  <div class="assistant-block">
    <h4 class="sr-only">ChatGPT said:</h4>
    <div class="markdown">RESULT_ALPHA</div>
  </div>
</div>
</body></html>`;

const wrapperSource = `"use strict";
const fs = require("node:fs");
const real = require(process.env.LOCAL_AGENT_PLAYWRIGHT_REAL_MODULE);
const composerFixture = fs.readFileSync(process.env.LOCAL_AGENT_LIVE_SLICE_FIXTURE, "utf8");
const childFixture = fs.readFileSync(process.env.LOCAL_AGENT_LIVE_SLICE_CHILD_FIXTURE, "utf8");
const childUrl = process.env.LOCAL_AGENT_LIVE_SLICE_CHILD_URL;
const authenticated = process.env.LOCAL_AGENT_LIVE_SLICE_AUTHENTICATED !== "0";
const profileControl = process.env.LOCAL_AGENT_LIVE_SLICE_PROFILE_CONTROL === "1";
const expectedExecutable = process.env.LOCAL_AGENT_EXPECTED_CHROME_EXECUTABLE || "";
module.exports = {
  chromium: {
    async launchPersistentContext(profile, options) {
      if (!Array.isArray(options.args) || !options.args.includes("--restore-last-session")) {
        throw new Error("browser must restore the persistent session");
      }
      if (expectedExecutable && options.executablePath !== expectedExecutable) {
        throw new Error("browser must pass the selected Chrome executable");
      }
      const launchOptions = { ...options };
      delete launchOptions.executablePath;
      launchOptions.channel = "chromium";
      const context = await real.chromium.launchPersistentContext(profile, launchOptions);
      await context.setOffline(true);
      await context.route("https://**/*", (route) => {
        const url = route.request().url();
        if (url === "https://chatgpt.com/api/auth/session") {
          return route.fulfill({
            status: 200,
            contentType: "application/json",
            body: JSON.stringify(authenticated ? { user: { id: "synthetic-user" } } : {})
          });
        }
        if (url === childUrl) {
          return route.fulfill({ status: 200, contentType: "text/html", body: childFixture });
        }
        if (url.startsWith("https://chatgpt.com/") || url.startsWith("https://chat.openai.com/")) {
          const rendered = profileControl
            ? composerFixture.replace("</body>", '<button data-testid="accounts-profile-button">Account</button></body>')
            : composerFixture;
          return route.fulfill({ status: 200, contentType: "text/html", body: rendered });
        }
        return route.abort();
      });
      // Chromium can restore a tab while launchPersistentContext is still
      // returning, before the isolated fixture route above is installed.
      // Reload only synthetic ChatGPT tabs in this disposable test profile
      // through the fixture route; keep their URLs and session claims intact.
      for (const page of context.pages()) {
        const url = page.url();
        if (
          !page.isClosed() &&
          (url.startsWith("https://chatgpt.com/") || url.startsWith("https://chat.openai.com/"))
        ) {
          await page.reload({ waitUntil: "domcontentloaded", timeout: 15_000 });
        }
      }
      return context;
    }
  }
};
`;

function bounded(label, promise, timeoutMs = 20_000) {
  let timer;
  return Promise.race([
    promise.finally(() => clearTimeout(timer)),
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${label} exceeded ${timeoutMs} ms`)), timeoutMs);
    })
  ]);
}

function startActuator({ profile, wrapperPath, fixturePath, childFixturePath, childUrl, authenticated = true, profileControl = false, chromeExecutable = null }) {
  const env = {
    ...process.env,
    LOCAL_AGENT_PLAYWRIGHT_MODULE: wrapperPath,
    LOCAL_AGENT_PLAYWRIGHT_REAL_MODULE: playwrightModule,
    LOCAL_AGENT_LIVE_SLICE_FIXTURE: fixturePath,
    LOCAL_AGENT_LIVE_SLICE_CHILD_FIXTURE: childFixturePath,
    LOCAL_AGENT_LIVE_SLICE_CHILD_URL: childUrl,
    LOCAL_AGENT_LIVE_SLICE_AUTHENTICATED: authenticated ? "1" : "0",
    LOCAL_AGENT_LIVE_SLICE_PROFILE_CONTROL: profileControl ? "1" : "0"
  };
  delete env.LOCAL_AGENT_CHROME_EXECUTABLE;
  delete env.LOCAL_AGENT_CONVERSATION_CHROME_EXECUTABLE;
  delete env.LOCAL_AGENT_EXPECTED_CHROME_EXECUTABLE;
  if (chromeExecutable) {
    env.LOCAL_AGENT_CONVERSATION_CHROME_EXECUTABLE = chromeExecutable;
    env.LOCAL_AGENT_EXPECTED_CHROME_EXECUTABLE = chromeExecutable;
  }

  const child = spawn(process.execPath, [
    path.join(root, "scripts", "conversation_live_slice_browser.cjs"),
    "--profile",
    profile,
    "--extension",
    path.join(root, "chat_bridge"),
    "--headless"
  ], { cwd: root, env, stdio: ["pipe", "pipe", "pipe"] });

  let stderr = "";
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const lines = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });
  const waiting = [];
  lines.on("line", (line) => {
    const waiter = waiting.shift();
    if (!waiter) throw new Error(`unexpected actuator response: ${line}`);
    try { waiter.resolve(JSON.parse(line)); } catch (error) { waiter.reject(error); }
  });

  let nextId = 0;
  const request = async (action, payload = {}, timeoutMs = 20_000) => {
    const id = ++nextId;
    const responsePromise = new Promise((resolve, reject) => waiting.push({ resolve, reject }));
    child.stdin.write(`${JSON.stringify({ id, action, payload })}\n`);
    const response = await bounded(`actuator ${action}`, responsePromise, timeoutMs);
    assert.equal(response.id, id, JSON.stringify(response));
    assert.equal(response.ok, true, response.error || JSON.stringify(response));
    return response.result;
  };

  const stop = async () => {
    if (child.exitCode !== null) return;
    const shutdown = await request("shutdown");
    assert.equal(shutdown.reason, "shutdown");
    child.stdin.end();
    const exitCode = await bounded("actuator exit", new Promise((resolve) => child.once("exit", resolve)), 10_000);
    assert.equal(exitCode, 0, stderr);
  };

  return { child, request, stop, stderr: () => stderr, closeLines: () => lines.close() };
}

(async () => {
  const temp = await fs.mkdtemp(path.join(os.tmpdir(), "conversation-live-slice-browser-"));
  const profile = path.join(temp, "profile");
  const fixturePath = path.join(temp, "fixture.html");
  const childFixturePath = path.join(temp, "child-result.html");
  const wrapperPath = path.join(temp, "playwright-wrapper.cjs");
  const fakeChromePath = path.join(temp, "fake-system-chrome");
  await fs.writeFile(fixturePath, composerFixture, "utf8");
  await fs.writeFile(childFixturePath, childResultFixture, "utf8");
  await fs.writeFile(wrapperPath, wrapperSource, "utf8");
  await fs.writeFile(fakeChromePath, "#!/bin/sh\nexit 1\n", "utf8");
  await fs.chmod(fakeChromePath, 0o755);

  let guest = null;
  let uiAuthenticated = null;
  let first = null;
  let second = null;
  let lifecycle = null;
  try {
    guest = startActuator({ profile: path.join(temp, "guest"), wrapperPath, fixturePath, childFixturePath, childUrl: childResultUrl, authenticated: false, chromeExecutable: fakeChromePath });
    const guestReady = await guest.request("wait_ready", { timeout_ms: 1200 }, 10_000);
    assert.equal(guestReady.ok, false, JSON.stringify(guestReady));
    assert.equal(guestReady.reason, "chatgpt_login_timeout");
    await guest.stop();
    guest = null;

    uiAuthenticated = startActuator({ profile: path.join(temp, "ui-auth"), wrapperPath, fixturePath, childFixturePath, childUrl: childResultUrl, authenticated: false, profileControl: true, chromeExecutable: fakeChromePath });
    const uiReady = await uiAuthenticated.request("wait_ready", { timeout_ms: 3000 }, 10_000);
    assert.equal(uiReady.ok, true, JSON.stringify(uiReady));
    assert.equal(uiReady.reason, "chatgpt_ready");
    await uiAuthenticated.stop();
    uiAuthenticated = null;

    const pending = intent("direct-browser-smoke");
    first = startActuator({ profile, wrapperPath, fixturePath, childFixturePath, childUrl: childResultUrl, chromeExecutable: fakeChromePath });
    assert.equal((await first.request("wait_ready", { timeout_ms: 5000 })).ok, true);
    const created = await first.request("create", { intent: pending });
    assert.equal(created.ok, true, JSON.stringify(created));
    assert.equal(created.reason, "tab_created");
    assert.ok(Number.isInteger(created.tabId));
    const durable = intent("direct-browser-smoke", created.tabId);
    const recovered = await first.request("recover_create", { intent: durable });
    assert.equal(recovered.ok, true, JSON.stringify(recovered));
    assert.equal(recovered.reason, "tab_recovered");
    await first.stop();
    first = null;

    second = startActuator({ profile, wrapperPath, fixturePath, childFixturePath, childUrl: childResultUrl, chromeExecutable: fakeChromePath });
    assert.equal((await second.request("wait_ready", { timeout_ms: 5000 })).ok, true);
    const restartRecovery = await second.request("recover_create", { intent: durable });
    assert.equal(restartRecovery.ok, true, JSON.stringify(restartRecovery));
    assert.ok(["tab_recovered", "tab_recreated"].includes(restartRecovery.reason), JSON.stringify(restartRecovery));
    const activeIntent = { ...durable, tab_id: restartRecovery.tabId };
    const probe = await second.request("probe", { intent: activeIntent }, 35_000);
    assert.equal(probe.ok, true, JSON.stringify(probe));
    assert.equal(probe.reason, "spawn_ready");
    const submitted = await second.request("submit", { intent: activeIntent }, 35_000);
    assert.equal(submitted.ok, true, JSON.stringify(submitted));
    assert.equal(submitted.childConversationUrl, childResultUrl);
    const reconciled = await second.request("reconcile", { intent: activeIntent });
    assert.equal(reconciled.ok, true, JSON.stringify(reconciled));
    assert.equal(reconciled.childConversationUrl, childResultUrl);
    await second.stop();
    second = null;

    lifecycle = startActuator({ profile: path.join(temp, "lifecycle"), wrapperPath, fixturePath, childFixturePath, childUrl: childResultUrl, chromeExecutable: fakeChromePath });
    assert.equal((await lifecycle.request("wait_ready", { timeout_ms: 5000 })).ok, true);
    const ownershipA = ownership("child-owner-a");
    const ownershipB = ownership("child-owner-b");
    const opened = await lifecycle.request("open_child", { child_conversation_url: childResultUrl, ownership: ownershipA });
    assert.equal(opened.ok, true, JSON.stringify(opened));
    const conflict = await lifecycle.request("close_child", { child_conversation_url: childResultUrl, ownership: ownershipB });
    assert.equal(conflict.ok, false, JSON.stringify(conflict));
    assert.equal(conflict.reason, "child_ownership_conflict");
    const observed = await lifecycle.request("observe_child", { child_conversation_url: childResultUrl, ownership: ownershipA });
    assert.equal(observed.ok, true, JSON.stringify(observed));
    assert.equal(observed.assistant_text, "RESULT_ALPHA");
    const closed = await lifecycle.request("close_child", { child_conversation_url: childResultUrl, ownership: ownershipA });
    assert.equal(closed.ok, true, JSON.stringify(closed));
    await lifecycle.stop();
    lifecycle = null;

    console.log("Conversation live slice direct-browser smoke passed.");
  } finally {
    for (const actuator of [guest, uiAuthenticated, first, second, lifecycle]) {
      if (!actuator) continue;
      if (actuator.child.exitCode === null) actuator.child.kill("SIGTERM");
      actuator.closeLines();
    }
    await fs.rm(temp, { recursive: true, force: true, maxRetries: 8, retryDelay: 100 });
  }
})().catch((error) => {
  console.error(error?.stack || error);
  process.exitCode = 1;
});
