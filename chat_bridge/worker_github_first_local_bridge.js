/* GitHub-first transport: Local Agent owns GitHub, this worker owns only
 * creating the child tab and injecting one immutable prompt. No DOM results.
 */
const GITHUB_FIRST_LOCAL_BRIDGE_KEY = "githubFirstLocalBridgeKeyV1";
// Kept in MV3 session storage so a native macOS dialog may close the popup
// without losing the one-time operator-approved pairing ticket.
const GITHUB_FIRST_PAIR_SESSION_KEY = "githubFirstPairSessionV1";
const GITHUB_FIRST_PAIR_TTL_MS = 120000;
const GITHUB_FIRST_LOCAL_RECORD_PREFIX = "githubFirstLocalSubmitV1:";
const GITHUB_FIRST_LOCAL_ENDPOINT = "http://127.0.0.1:8768";
// Release gate: GitHub-first Sends use the authenticated localhost transport.
// A durable transaction claim precedes each Send; UNKNOWN never replays.
const GITHUB_FIRST_LOCAL_SEND_RELEASED = true;
let githubFirstLocalQueue = Promise.resolve();

function githubFirstLocalSerialize(work) {
  const pending = githubFirstLocalQueue.catch(() => undefined).then(work);
  githubFirstLocalQueue = pending.catch(() => undefined);
  return pending;
}

function githubFirstLocalValidate(launch) {
  if (!launch || typeof launch !== "object" || Array.isArray(launch) ||
      Object.keys(launch).sort().join(",") !== "child_request_id,dispatch_id,intent,schema_version" ||
      launch.schema_version !== 1 ||
      !/^fabric-[0-9a-f]{32}$/.test(String(launch.dispatch_id || "")) ||
      !/^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$/.test(String(launch.child_request_id || ""))) {
    throw new Error("Invalid Local Agent Bridge launch identity");
  }
  validateConversationSpawnBrowserIntent(launch.intent);
  return launch;
}

async function githubFirstLocalKeyStatus() {
  const stored = await chrome.storage.local.get(GITHUB_FIRST_LOCAL_BRIDGE_KEY);
  const key = String(stored[GITHUB_FIRST_LOCAL_BRIDGE_KEY] || "");
  return { paired: /^[A-Za-z0-9_-]{32,128}$/.test(key), key };
}

async function githubFirstLocalSaveKey(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]{32,128}$/.test(value)) {
    return { ok: false, reason: "invalid_local_bridge_pairing_key" };
  }
  await chrome.storage.local.set({ [GITHUB_FIRST_LOCAL_BRIDGE_KEY]: value });
  return { ok: true, paired: true };
}

async function githubFirstLocalFetch(key, endpoint, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);
  try {
    const response = await fetch(GITHUB_FIRST_LOCAL_ENDPOINT + endpoint, {
      ...options, signal: controller.signal,
      cache: "no-store", redirect: "error", credentials: "omit",
      headers: { "X-Local-Agent-Bridge-Key": key,
        ...(options.body ? { "Content-Type": "application/json" } : {}) }
    });
    if (!response.ok || response.redirected) throw new Error("Local Bridge unavailable");
    const raw = await response.text();
    if (raw.length > 131072) throw new Error("Local Bridge response oversized");
    return JSON.parse(raw);
  } finally {
    clearTimeout(timeout);
  }
}

// Read-only readiness probe is always permitted, independent of Send.
// A saved key alone is not proof that the local runner is paired or reachable.
async function githubFirstLocalProbe() {
  const { paired, key } = await githubFirstLocalKeyStatus();
  if (!paired) return { paired: false, reachable: false };
  try {
    const status = await githubFirstLocalFetch(key, "/v1/status");
    return { paired: true, reachable: status?.ok === true,
      pairingOnly: status?.mode === "pair_only" };
  } catch (_error) {
    return { paired: true, reachable: false };
  }
}

// Select only the operator's active tab URL; never inspect chat transcript.
async function githubFirstLocalSelectParent() {
  const { paired, key } = await githubFirstLocalKeyStatus();
  if (!paired) return { ok: false, reason: "bridge_not_paired" };
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!Array.isArray(tabs) || tabs.length !== 1 ||
      !Number.isInteger(tabs[0].id)) return { ok: false, reason: "active_tab_ambiguous" };
  const parent = normalizeConversationUrl(tabs[0].url || "");
  if (!parent) return { ok: false, reason: "open_one_chatgpt_conversation" };
  const reply = await githubFirstLocalFetch(key, "/v1/parent", {
    method: "POST", body: JSON.stringify({ parent_url: parent })
  });
  return reply?.ok && reply.parent_url === parent
    ? { ok: true, parentUrl: parent }
    : { ok: false, reason: "parent_registration_failed" };
}

// An operator presses Connect in the trusted extension popup. Native approval
// happens on the Mac; a pairing ticket is neither a GitHub token nor Send grant.
async function githubFirstLocalPairStart() {
  const previous = (await chrome.storage.session.get(GITHUB_FIRST_PAIR_SESSION_KEY))[GITHUB_FIRST_PAIR_SESSION_KEY];
  if (previous && previous.status === "pending" &&
      Date.now() - previous.startedAt < GITHUB_FIRST_PAIR_TTL_MS) {
    return { ok: true, ticket: previous.ticket, code: previous.code, resumed: true };
  }
  const response = await fetch(GITHUB_FIRST_LOCAL_ENDPOINT + "/v1/pair/start", {
    method: "POST", cache: "no-store", redirect: "error", credentials: "omit",
    headers: { "Content-Type": "application/json" }, body: "{}"
  });
  if (!response.ok || response.redirected) throw new Error("Local Agent pairing unavailable");
  const result = await response.json();
  if (!result?.ok || !/^[A-Za-z0-9_-]{32,128}$/.test(result.ticket || "") ||
      !/^\d{6}$/.test(result.code || "")) throw new Error("Invalid pairing response");
  await chrome.storage.session.set({ [GITHUB_FIRST_PAIR_SESSION_KEY]: {
    ticket: result.ticket, code: result.code, startedAt: Date.now(), status: "pending"
  } });
  return { ok: true, ticket: result.ticket, code: result.code };
}

async function githubFirstLocalPairFinish(ticket) {
  if (typeof ticket !== "string" || !/^[A-Za-z0-9_-]{32,128}$/.test(ticket)) {
    throw new Error("Invalid pairing ticket");
  }
  const response = await fetch(GITHUB_FIRST_LOCAL_ENDPOINT + "/v1/pair/finish", {
    method: "POST", cache: "no-store", redirect: "error", credentials: "omit",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ticket })
  });
  if (!response.ok || response.redirected) throw new Error("Local Agent pairing expired");
  const result = await response.json();
  if (result?.status === "paired" && result.ok === true) {
    // Do not forward the secret to the popup or include it in logs.
    return githubFirstLocalSaveKey(result.key);
  }
  if (result?.status === "pending" && result.ok === true) return { ok: true, status: "pending" };
  return { ok: false, status: result?.status === "denied" ? "denied" : "expired" };
}

// Work in the service worker, never in the ephemeral Chrome popup. Status
// checks and the existing minute alarm both reconcile after popup closure.
// No browser Send is triggered by reconciliation.
let githubFirstPairReconcilePromise = null;
async function githubFirstLocalPairReconcile() {
  if (githubFirstPairReconcilePromise) return githubFirstPairReconcilePromise;
  githubFirstPairReconcilePromise = (async () => {
    const session = (await chrome.storage.session.get(GITHUB_FIRST_PAIR_SESSION_KEY))[GITHUB_FIRST_PAIR_SESSION_KEY];
    if (!session) return { status: "idle" };
    if (session.status !== "pending") return { status: session.status };
    if (Date.now() - session.startedAt >= GITHUB_FIRST_PAIR_TTL_MS) {
      await chrome.storage.session.set({ [GITHUB_FIRST_PAIR_SESSION_KEY]: { status: "expired" } });
      return { status: "expired" };
    }
    try {
      const outcome = await githubFirstLocalPairFinish(session.ticket);
      if (outcome?.ok && outcome.paired) {
        await chrome.storage.session.set({ [GITHUB_FIRST_PAIR_SESSION_KEY]: null });
        return { status: "paired" };
      }
      if (outcome?.status === "pending") return { status: "pending", code: session.code };
      const status = outcome?.status === "denied" ? "denied" : "expired";
      await chrome.storage.session.set({ [GITHUB_FIRST_PAIR_SESSION_KEY]: { status } });
      return { status };
    } catch (_error) {
      // Preserve the bounded ticket for a subsequent status/alarm retry.
      return { status: "pending", code: session.code };
    }
  })();
  try { return await githubFirstPairReconcilePromise; }
  finally { githubFirstPairReconcilePromise = null; }
}

async function githubFirstLocalReportAck(key, transactionId, childUrl) {
  return githubFirstLocalFetch(key, "/v1/ack", {
    method: "POST", body: JSON.stringify({ transaction_id: transactionId, child_url: childUrl })
  });
}

async function githubFirstLocalRunLaunch(launch, ops = {}) {
  githubFirstLocalValidate(launch);
  await requireConversationSpawnBootstrapDigest(launch.intent);
  const key = GITHUB_FIRST_LOCAL_RECORD_PREFIX + launch.intent.transaction_id;
  const store = ops.storage || chrome.storage.local;
  const stored = await store.get(key);
  if (stored[key]) {
    if (stored[key].dispatch_id !== launch.dispatch_id ||
        stored[key].child_request_id !== launch.child_request_id) {
      throw new Error("Local Bridge immutable transaction collision");
    }
    return { ok: false, status: "already_consumed", childUrl: stored[key].child_url || "" };
  }
  // The prompt can never be replayed after this durable local write.
  const record = {
    dispatch_id: launch.dispatch_id, child_request_id: launch.child_request_id,
    phase: "submission_unknown", child_url: "", tab_id: null
  };
  await store.set({ [key]: record });
  const create = ops.create || createConversationSpawnTab;
  const submit = ops.submit || submitConversationSpawnBootstrap;
  const prepare = ops.prepare || ensureConversationSpawnContent;
  let tabId = null;
  try {
    const created = await create(launch.intent);
    if (!created?.ok || !Number.isInteger(created.tabId)) return { ok: false, status: "tab_unknown" };
    tabId = created.tabId;
    record.tab_id = tabId;
    await store.set({ [key]: record });
    const readiness = await prepare(tabId);
    if (!readiness?.ok) return { ok: false, status: "not_ready" };
    const result = await submit({ ...launch.intent, tab_id: tabId });
    // Bounded, non-secret technical status for operator diagnostics only.
    record.last_reason = ["composer_cleared_after_click", "submit_boundary_without_clear",
      "send_click_unknown", "send_not_attempted", "route_changed_after_click",
      "composer_injection_unknown", "composer_changed_no_click",
      "spawn_not_ready", "spawn_injection_unknown", "spawn_content_unavailable",
      "spawn_page_not_ready", "composer_unavailable", "composer_not_empty",
      "composer_write_failed", "submission_unknown"].includes(result?.reason)
      ? result.reason : "unknown";
    await store.set({ [key]: record });
    const childUrl = normalizeConversationUrl(result?.childConversationUrl || "");
    if (childUrl && result?.ok) {
      record.phase = "ack_pending";
      record.child_url = childUrl;
      await store.set({ [key]: record });
      return { ok: true, status: "ack_pending", childUrl };
    }
  } catch (_error) {
    // No exception after the durable unknown marker is a retry permit.
  }
  return { ok: false, status: "submission_unknown" };
}

async function githubFirstLocalRunParentWake(wake, ops = {}) {
  if (!wake || wake.kind !== "parent_wake" || wake.schema_version !== 1 ||
      !/^wake-[0-9a-f]{64}$/.test(String(wake.wake_id || "")) ||
      !/^fabric-[0-9a-f]{32}$/.test(String(wake.dispatch_id || "")) ||
      normalizeConversationUrl(wake.parent_url || "") !== wake.parent_url ||
      !Array.isArray(wake.child_request_ids) ||
      !wake.child_request_ids.length || wake.child_request_ids.length > 4) {
    throw new Error("Invalid GitHub-first parent wake");
  }
  const marker = "githubFirstLocalParentWakeV1:" + wake.wake_id;
  const storage = ops.storage || chrome.storage.local;
  if ((await storage.get(marker))[marker]) return { ok: false, status: "already_consumed" };
  const text = "[LOCAL_AGENT_GITHUB_FIRST_RESULTS] Private GitHub Fabric dispatch " +
    wake.dispatch_id + " has published child results for " +
    wake.child_request_ids.join(", ") + ". Read and verify the child result files " +
    "on fabric-data in example-owner/local-agent-fabric-private with GitHub Connector; " +
    "integrate the results and continue the current goal. Do not read child replies from DOM.";
  if (text.length > 1200) return { ok: false, status: "wake_oversized" };
  // This marker is written before even locating the tab. No unknown wake retry.
  await storage.set({ [marker]: "submission_unknown" });
  try {
    const matches = (await chrome.tabs.query({ url: [
      "https://chatgpt.com/c/*", "https://chat.openai.com/c/*"
    ] })).filter(tab => normalizeConversationUrl(tab.url || "") === wake.parent_url);
    if (matches.length !== 1 || !Number.isInteger(matches[0].id)) {
      return { ok: false, status: "parent_unavailable" };
    }
    const tabId = matches[0].id;
    const send = ops.send || (async () => {
      await chrome.scripting.executeScript({ target: { tabId, frameIds: [0] },
        files: ["github_first_composer.js", "github_first_prompt_content.js"] });
      return chrome.tabs.sendMessage(tabId, { type: "bridge:github-first-parent-wake",
        parentUrl: wake.parent_url, wakeId: wake.wake_id, text }, { frameId: 0 });
    });
    const result = await send({ tabId, text });
    if (result?.ok && result.submitted) {
      await storage.set({ [marker]: "ack_pending" });
      return { ok: true, status: "ack_pending" };
    }
  } catch (_error) { /* permanent unknown */ }
  return { ok: false, status: "submission_unknown" };
}

async function githubFirstLocalPollOnce({ allowSend = GITHUB_FIRST_LOCAL_SEND_RELEASED } = {}) {
  if (allowSend !== true || !GITHUB_FIRST_LOCAL_SEND_RELEASED) {
    return { ok: true, status: "send_disabled" };
  }
  const { paired, key } = await githubFirstLocalKeyStatus();
  if (!paired) return { ok: false, status: "unpaired" };
  // Observe only browser routing identity, never the answer body. An unknown
  // transaction cannot Submit again, but may acquire a durable child URL.
  const saved = await chrome.storage.local.get(null);
  for (const [id, record] of Object.entries(saved)) {
    if (!id.startsWith(GITHUB_FIRST_LOCAL_RECORD_PREFIX) ||
        record?.phase !== "submission_unknown" || !Number.isInteger(record.tab_id)) continue;
    try {
      const tab = await chrome.tabs.get(record.tab_id);
      const childUrl = normalizeConversationUrl(tab?.url || "");
      const transactionId = id.slice(GITHUB_FIRST_LOCAL_RECORD_PREFIX.length);
      if (childUrl && await conversationSpawnTabClaimMatches(transactionId, record.tab_id)) {
        record.phase = "ack_pending";
        record.child_url = childUrl;
        await chrome.storage.local.set({ [id]: record });
      }
    } catch (_error) { /* unavailable tab is UNKNOWN, never a replay */ }
  }
  // A lost technical wake ACK may be retried, but never the ChatGPT injection.
  for (const [id, phase] of Object.entries(saved)) {
    if (id.startsWith("githubFirstLocalParentWakeV1:") && phase === "ack_pending") {
      const wakeId = id.slice("githubFirstLocalParentWakeV1:".length);
      const response = await githubFirstLocalFetch(key, "/v1/wake-ack", {
        method: "POST", body: JSON.stringify({ wake_id: wakeId })
      });
      if (response?.ok) await chrome.storage.local.set({ [id]: "acknowledged" });
    }
  }
  // ACK retries transmit only the previously observed URL, never a prompt.
  const observed = await chrome.storage.local.get(null);
  for (const [id, record] of Object.entries(observed)) {
    if (id.startsWith(GITHUB_FIRST_LOCAL_RECORD_PREFIX) && record?.phase === "ack_pending" &&
        normalizeConversationUrl(record.child_url || "") === record.child_url) {
      const transactionId = id.slice(GITHUB_FIRST_LOCAL_RECORD_PREFIX.length);
      const response = await githubFirstLocalReportAck(key, transactionId, record.child_url);
      if (response?.ok) {
        await chrome.storage.local.set({ [id]: { ...record, phase: "acknowledged" } });
      }
    }
  }
  const response = await githubFirstLocalFetch(key, "/v1/next");
  if (!response?.ok || !response.launch) return { ok: true, status: "idle" };
  if (response.launch.kind === "parent_wake") {
    const completed = await githubFirstLocalRunParentWake(response.launch);
    if (completed.ok && completed.status === "ack_pending") {
      await githubFirstLocalFetch(key, "/v1/wake-ack", {
        method: "POST", body: JSON.stringify({ wake_id: response.launch.wake_id })
      });
    }
    return completed;
  }
  return githubFirstLocalRunLaunch(response.launch);
}

function pollGithubFirstLocalBridge() {
  return githubFirstLocalSerialize(() => githubFirstLocalPollOnce());
}
