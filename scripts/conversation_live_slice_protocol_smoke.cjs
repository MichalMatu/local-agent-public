"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const readline = require("node:readline");
const { spawn } = require("node:child_process");

const root = path.resolve(__dirname, "..");
const CHILD_URL = "https://chatgpt.com/c/22222222-2222-4222-8222-222222222222";

function sha256(text) {
  return crypto.createHash("sha256").update(String(text), "utf8").digest("hex");
}

function intent(label, tabId = null) {
  const bootstrapText = `LOCAL AGENT CHILD BOOTSTRAP\nrequest=${label}`;
  return {
    schema_version: 1,
    transaction_id: `spawn-${sha256(`transaction:${label}`)}`,
    child_request_digest: `sha256:${sha256(`request:${label}`)}`,
    bootstrap_digest: `sha256:${sha256(bootstrapText)}`,
    bootstrap_text: bootstrapText,
    tab_id: tabId
  };
}

const fakePlaywrightSource = `"use strict";
const childUrl = ${JSON.stringify(CHILD_URL)};
const worker = {
  url() { return "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/service_worker.js"; },
  async evaluate(expression) {
    if (expression.includes("findConversationSpawnMarkerTabs(")) {
      return { ok: true, reason: "tab_recovered", tabId: 55 };
    }
    if (expression.includes("createConversationSpawnTab(")) {
      return { ok: true, reason: "tab_created", tabId: 55 };
    }
    if (expression.includes("probeConversationSpawnContent(")) {
      return {
        ok: true,
        reason: "ready",
        route: "fresh",
        readiness: { ok: true, reason: "spawn_ready" }
      };
    }
    if (expression.includes("submitConversationSpawnBootstrap(")) {
      return { ok: true, reason: "identity_discovered", childConversationUrl: childUrl };
    }
    if (expression.includes("reconcileConversationSpawn(")) {
      return { ok: true, reason: "identity_discovered", childConversationUrl: childUrl };
    }
    throw new Error("unexpected service-worker expression: " + expression);
  }
};
const page = {
  closed: false,
  async goto() { return null; },
  url() { return "https://chatgpt.com/"; },
  isClosed() { return this.closed; },
  locator() {
    return { first() { return { async isVisible() { return true; } }; } };
  },
  async waitForTimeout() {},
  async close() { this.closed = true; }
};
module.exports = {
  chromium: {
    async launchPersistentContext() {
      return {
        serviceWorkers() { return [worker]; },
        async waitForEvent() { return worker; },
        async newPage() { page.closed = false; return page; },
        async close() {}
      };
    }
  }
};
`;

function bounded(label, promise, timeoutMs = 10_000) {
  let timer;
  return Promise.race([
    promise.finally(() => clearTimeout(timer)),
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${label} exceeded ${timeoutMs} ms`)), timeoutMs);
    })
  ]);
}

(async () => {
  const temp = await fs.mkdtemp(path.join(os.tmpdir(), "conversation-live-slice-protocol-"));
  const fakeModule = path.join(temp, "fake-playwright.cjs");
  const profile = path.join(temp, "profile");
  await fs.writeFile(fakeModule, fakePlaywrightSource, "utf8");

  const child = spawn(process.execPath, [
    path.join(root, "scripts", "conversation_live_slice_browser.cjs"),
    "--profile",
    profile,
    "--extension",
    path.join(root, "chat_bridge"),
    "--headless"
  ], {
    cwd: root,
    env: {
      ...process.env,
      LOCAL_AGENT_PLAYWRIGHT_MODULE: fakeModule
    },
    stdio: ["pipe", "pipe", "pipe"]
  });

  let stderr = "";
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const lines = readline.createInterface({ input: child.stdout, crlfDelay: Infinity });
  const waiting = [];
  lines.on("line", (line) => {
    const waiter = waiting.shift();
    if (!waiter) throw new Error(`unexpected actuator response: ${line}`);
    try {
      waiter.resolve(JSON.parse(line));
    } catch (error) {
      waiter.reject(error);
    }
  });

  let nextId = 0;
  const request = async (action, payload = {}) => {
    const id = ++nextId;
    const responsePromise = new Promise((resolve, reject) => waiting.push({ resolve, reject }));
    child.stdin.write(`${JSON.stringify({ id, action, payload })}\n`);
    const response = await bounded(`protocol ${action}`, responsePromise);
    assert.equal(response.id, id, JSON.stringify(response));
    assert.equal(response.ok, true, response.error || JSON.stringify(response));
    assert.equal(typeof response.result, "object");
    return response.result;
  };

  try {
    const ready = await request("wait_ready", { timeout_ms: 1_000 });
    assert.deepEqual(ready, { ok: true, reason: "chatgpt_ready", url: "https://chatgpt.com/" });

    const pending = intent("protocol-smoke");
    const created = await request("create", { intent: pending });
    assert.deepEqual(created, { ok: true, reason: "tab_created", tabId: 55 });

    const recovered = await request("recover_create", { intent: pending });
    assert.deepEqual(recovered, { ok: true, reason: "tab_recovered", tabId: 55 });

    const active = { ...pending, tab_id: 55 };
    const probe = await request("probe", { intent: active });
    assert.deepEqual(probe, { ok: true, reason: "spawn_ready" });

    const submitted = await request("submit", { intent: active });
    assert.deepEqual(submitted, {
      ok: true,
      reason: "identity_discovered",
      childConversationUrl: CHILD_URL
    });

    const reconciled = await request("reconcile", { intent: active });
    assert.deepEqual(reconciled, {
      ok: true,
      reason: "identity_discovered",
      childConversationUrl: CHILD_URL
    });

    const shutdown = await request("shutdown");
    assert.deepEqual(shutdown, { ok: true, reason: "shutdown" });
    child.stdin.end();
    const exitCode = await bounded(
      "protocol actuator exit",
      new Promise((resolve) => child.once("exit", resolve))
    );
    assert.equal(exitCode, 0, stderr);
    console.log("Conversation live slice actuator protocol smoke passed.");
  } finally {
    if (child.exitCode === null) child.kill("SIGTERM");
    lines.close();
    await fs.rm(temp, { recursive: true, force: true });
  }
})().catch((error) => {
  console.error(error?.stack || error);
  process.exitCode = 1;
});
