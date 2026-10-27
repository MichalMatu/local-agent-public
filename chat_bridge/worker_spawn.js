const CONVERSATION_SPAWN_BROWSER_SCHEMA_VERSION = 1;
const CONVERSATION_SPAWN_CONTENT_PROTOCOL_VERSION = 1;
const MAX_CONVERSATION_SPAWN_BOOTSTRAP_CHARS = 32_768;
const CONVERSATION_SPAWN_TRANSACTION_RE = /^spawn-[0-9a-f]{64}$/;
const CONVERSATION_SPAWN_DIGEST_RE = /^sha256:[0-9a-f]{64}$/;
const CONVERSATION_SPAWN_FIELDS = new Set([
  "schema_version",
  "transaction_id",
  "child_request_digest",
  "bootstrap_digest",
  "bootstrap_text",
  "tab_id"
]);
const CONVERSATION_SPAWN_MARKER = "la-spawn";
const CONVERSATION_SPAWN_STAGING_PAGE = "spawn_staging.html";
const CONVERSATION_SPAWN_TAB_CLAIM_PREFIX = "conversation-spawn-tab:";
const CONVERSATION_SPAWN_PROVISIONAL_EVIDENCE_PREFIX = "conversation-spawn-provisional:";

function validateConversationSpawnBrowserIntent(intent, { requireTab = false } = {}) {
  if (!intent || typeof intent !== "object" || Array.isArray(intent)) {
    throw new Error("conversation spawn browser intent must be an object");
  }
  const keys = Object.keys(intent);
  const extra = keys.filter((key) => !CONVERSATION_SPAWN_FIELDS.has(key));
  const missing = Array.from(CONVERSATION_SPAWN_FIELDS).filter((key) => !Object.hasOwn(intent, key));
  if (extra.length) {
    throw new Error(`conversation spawn browser intent contains unsupported fields: ${extra.sort().join(",")}`);
  }
  if (missing.length) {
    throw new Error(`conversation spawn browser intent is missing fields: ${missing.sort().join(",")}`);
  }
  if (intent.schema_version !== CONVERSATION_SPAWN_BROWSER_SCHEMA_VERSION) {
    throw new Error(`conversation spawn browser schema_version must be ${CONVERSATION_SPAWN_BROWSER_SCHEMA_VERSION}`);
  }
  if (!CONVERSATION_SPAWN_TRANSACTION_RE.test(String(intent.transaction_id || ""))) {
    throw new Error("conversation spawn transaction_id is invalid");
  }
  if (!CONVERSATION_SPAWN_DIGEST_RE.test(String(intent.child_request_digest || ""))) {
    throw new Error("conversation spawn child_request_digest is invalid");
  }
  if (!CONVERSATION_SPAWN_DIGEST_RE.test(String(intent.bootstrap_digest || ""))) {
    throw new Error("conversation spawn bootstrap_digest is invalid");
  }
  if (
    typeof intent.bootstrap_text !== "string" ||
    !intent.bootstrap_text.trim() ||
    intent.bootstrap_text.length > MAX_CONVERSATION_SPAWN_BOOTSTRAP_CHARS
  ) {
    throw new Error("conversation spawn bootstrap_text must be non-empty bounded text");
  }
  if (intent.tab_id !== null && (!Number.isInteger(intent.tab_id) || intent.tab_id < 1)) {
    throw new Error("conversation spawn tab_id must be a positive integer or null");
  }
  if (requireTab && !Number.isInteger(intent.tab_id)) {
    throw new Error("conversation spawn browser action requires the original tab_id");
  }
  return intent;
}

async function conversationSpawnSha256(text) {
  const bytes = new TextEncoder().encode(String(text));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("");
}

async function requireConversationSpawnBootstrapDigest(intent) {
  validateConversationSpawnBrowserIntent(intent);
  const actual = `sha256:${await conversationSpawnSha256(intent.bootstrap_text)}`;
  if (actual !== intent.bootstrap_digest) {
    throw new Error("conversation spawn bootstrap digest mismatch");
  }
}

function conversationSpawnMarkerUrl(transactionId) {
  if (!CONVERSATION_SPAWN_TRANSACTION_RE.test(String(transactionId || ""))) {
    throw new Error("conversation spawn transaction_id is invalid");
  }
  const staging = new URL(chrome.runtime.getURL(CONVERSATION_SPAWN_STAGING_PAGE));
  staging.hash = `${CONVERSATION_SPAWN_MARKER}=${encodeURIComponent(transactionId)}`;
  return staging.href;
}

function conversationSpawnStagingMarkerFromUrl(rawUrl) {
  try {
    const url = new URL(String(rawUrl || ""));
    const staging = new URL(chrome.runtime.getURL(CONVERSATION_SPAWN_STAGING_PAGE));
    if (
      url.protocol !== staging.protocol ||
      url.host !== staging.host ||
      url.pathname !== staging.pathname
    ) {
      return "";
    }
    const params = new URLSearchParams(url.hash.replace(/^#/, ""));
    const transactionId = params.get(CONVERSATION_SPAWN_MARKER) || "";
    return CONVERSATION_SPAWN_TRANSACTION_RE.test(transactionId) ? transactionId : "";
  } catch (_error) {
    return "";
  }
}

function conversationSpawnMarkerFromUrl(rawUrl) {
  const stagingMarker = conversationSpawnStagingMarkerFromUrl(rawUrl);
  if (stagingMarker) return stagingMarker;
  try {
    const url = new URL(String(rawUrl || ""));
    const isLegacyChatMarker = (
      url.protocol === "https:" &&
      url.hostname === "chatgpt.com" &&
      (url.pathname.replace(/\/+$/, "") || "/") === "/"
    );
    if (!isLegacyChatMarker) return "";
    const params = new URLSearchParams(url.hash.replace(/^#/, ""));
    const transactionId = params.get(CONVERSATION_SPAWN_MARKER) || "";
    return CONVERSATION_SPAWN_TRANSACTION_RE.test(transactionId) ? transactionId : "";
  } catch (_error) {
    return "";
  }
}

function conversationSpawnMarkerFromTab(tab) {
  return (
    conversationSpawnMarkerFromUrl(tab?.url) ||
    conversationSpawnMarkerFromUrl(tab?.pendingUrl)
  );
}

function conversationSpawnTabClaimKey(transactionId) {
  if (!CONVERSATION_SPAWN_TRANSACTION_RE.test(String(transactionId || ""))) {
    throw new Error("conversation spawn transaction_id is invalid");
  }
  return `${CONVERSATION_SPAWN_TAB_CLAIM_PREFIX}${transactionId}`;
}

async function rememberConversationSpawnTab(transactionId, tabId) {
  if (!Number.isInteger(tabId) || tabId < 1) {
    throw new Error("conversation spawn tab claim requires a positive tab id");
  }
  await chrome.storage.session.set({
    [conversationSpawnTabClaimKey(transactionId)]: tabId
  });
}

async function conversationSpawnClaimedTabId(transactionId) {
  const key = conversationSpawnTabClaimKey(transactionId);
  const stored = await chrome.storage.session.get(key);
  const tabId = stored?.[key];
  return Number.isInteger(tabId) && tabId > 0 ? tabId : null;
}

async function conversationSpawnTabClaimMatches(transactionId, tabId) {
  return await conversationSpawnClaimedTabId(transactionId) === tabId;
}

function conversationSpawnProvisionalEvidenceKey(transactionId) {
  if (!CONVERSATION_SPAWN_TRANSACTION_RE.test(String(transactionId || ""))) {
    throw new Error("conversation spawn transaction_id is invalid");
  }
  return `${CONVERSATION_SPAWN_PROVISIONAL_EVIDENCE_PREFIX}${transactionId}`;
}

async function conversationSpawnTransactionForClaimedTab(tabId) {
  if (!Number.isInteger(tabId) || tabId < 1) return "";
  const stored = await chrome.storage.session.get(null);
  const matches = Object.entries(stored).filter(([key, value]) =>
    key.startsWith(CONVERSATION_SPAWN_TAB_CLAIM_PREFIX) && value === tabId
  );
  if (matches.length !== 1) return "";
  const transactionId = matches[0][0].slice(CONVERSATION_SPAWN_TAB_CLAIM_PREFIX.length);
  return CONVERSATION_SPAWN_TRANSACTION_RE.test(transactionId) ? transactionId : "";
}

async function rememberConversationSpawnProvisionalRoute(tabId) {
  const transactionId = await conversationSpawnTransactionForClaimedTab(tabId);
  if (!transactionId) return false;
  await chrome.storage.session.set({
    [conversationSpawnProvisionalEvidenceKey(transactionId)]: true
  });
  return true;
}

async function conversationSpawnSawProvisionalRoute(transactionId, tabId) {
  if (!await conversationSpawnTabClaimMatches(transactionId, tabId)) return false;
  const key = conversationSpawnProvisionalEvidenceKey(transactionId);
  const stored = await chrome.storage.session.get(key);
  return stored?.[key] === true;
}

function conversationSpawnFreshChatUrl(rawUrl) {
  try {
    const url = new URL(String(rawUrl || ""));
    const path = url.pathname.replace(/\/+$/, "") || "/";
    return (
      url.protocol === "https:" &&
      ["chatgpt.com", "chat.openai.com"].includes(url.hostname) &&
      path === "/" &&
      !url.search &&
      !url.hash
    );
  } catch (_error) {
    return false;
  }
}

async function findConversationSpawnMarkerTabs(transactionId) {
  const tabs = await chrome.tabs.query({});
  return tabs.filter((tab) =>
    Number.isInteger(tab.id) && conversationSpawnMarkerFromTab(tab) === transactionId
  );
}

async function validateConversationSpawnTabRoute(intent, tab) {
  const tabId = Number(tab?.id || 0);
  if (!Number.isInteger(tabId) || tabId < 1) {
    return { ok: false, reason: "spawn_tab_unavailable" };
  }
  const rawUrl = String(tab?.url || "");
  const childUrl = normalizeConversationUrl(rawUrl);
  if (childUrl) {
    return await conversationSpawnTabClaimMatches(intent.transaction_id, tabId)
      ? { ok: true, route: "child", childConversationUrl: childUrl }
      : { ok: false, reason: "spawn_tab_claim_mismatch" };
  }
  const marker = conversationSpawnMarkerFromUrl(rawUrl);
  if (marker) {
    if (marker !== intent.transaction_id) {
      return { ok: false, reason: "spawn_tab_claim_mismatch" };
    }
    await rememberConversationSpawnTab(intent.transaction_id, tabId);
    return { ok: true, route: "staging" };
  }
  if (conversationSpawnFreshChatUrl(rawUrl)) {
    return await conversationSpawnTabClaimMatches(intent.transaction_id, tabId)
      ? { ok: true, route: "fresh" }
      : { ok: false, reason: "spawn_tab_claim_mismatch" };
  }
  const pendingMarker = conversationSpawnMarkerFromUrl(tab?.pendingUrl);
  if (pendingMarker) {
    if (pendingMarker !== intent.transaction_id) {
      return { ok: false, reason: "spawn_tab_claim_mismatch" };
    }
    await rememberConversationSpawnTab(intent.transaction_id, tabId);
    return { ok: false, reason: "spawn_page_not_ready" };
  }
  if (conversationSpawnFreshChatUrl(tab?.pendingUrl)) {
    return await conversationSpawnTabClaimMatches(intent.transaction_id, tabId)
      ? { ok: false, reason: "spawn_page_not_ready" }
      : { ok: false, reason: "spawn_tab_claim_mismatch" };
  }
  return { ok: false, reason: "spawn_unexpected_route" };
}

async function probeConversationSpawnContent(tabId) {
  try {
    const response = await chrome.tabs.sendMessage(tabId, {
      type: "bridge:spawn-capabilities",
      protocolVersion: CONVERSATION_SPAWN_CONTENT_PROTOCOL_VERSION
    }, { frameId: 0 });
    return response?.protocolVersion === CONVERSATION_SPAWN_CONTENT_PROTOCOL_VERSION
      ? response
      : { ok: false, reason: "spawn_content_protocol_mismatch" };
  } catch (error) {
    return { ok: false, reason: "spawn_content_unavailable", error: String(error) };
  }
}

async function ensureConversationSpawnContent(tabId) {
  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch (error) {
    return { ok: false, reason: "spawn_tab_unavailable", error: String(error) };
  }
  const stagingMarker = (
    conversationSpawnStagingMarkerFromUrl(tab?.url) ||
    conversationSpawnStagingMarkerFromUrl(tab?.pendingUrl)
  );
  if (stagingMarker) {
    await rememberConversationSpawnTab(stagingMarker, tabId);
    try {
      await chrome.tabs.update(tabId, { url: "https://chatgpt.com/" });
    } catch (error) {
      return { ok: false, reason: "spawn_content_unavailable", error: String(error) };
    }
    const deadline = Date.now() + 5000;
    let reachedFreshRoute = false;
    while (Date.now() < deadline) {
      try {
        tab = await chrome.tabs.get(tabId);
      } catch (error) {
        return { ok: false, reason: "spawn_tab_unavailable", error: String(error) };
      }
      if (conversationSpawnFreshChatUrl(tab?.url)) {
        reachedFreshRoute = true;
        break;
      }
      if (normalizeConversationUrl(String(tab?.url || ""))) {
        return { ok: false, reason: "spawn_unexpected_route" };
      }
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    if (!reachedFreshRoute) {
      return { ok: false, reason: "spawn_page_not_ready" };
    }
  }

  let probe = await probeConversationSpawnContent(tabId);
  if (probe?.ok) return probe;
  if (!["spawn_content_unavailable", "spawn_content_protocol_mismatch"].includes(String(probe?.reason || ""))) {
    return probe;
  }
  try {
    await chrome.scripting.executeScript({
      target: { tabId, frameIds: [0] },
      files: ["github_first_composer.js", "spawn_content.js"]
    });
  } catch (error) {
    return { ok: false, reason: "spawn_content_unavailable", error: String(error) };
  }
  return probeConversationSpawnContent(tabId);
}

async function recoverConversationSpawnTab(intent) {
  validateConversationSpawnBrowserIntent(intent);
  const claimedTabId = await conversationSpawnClaimedTabId(intent.transaction_id);
  if (claimedTabId) {
    try {
      const claimedTab = await chrome.tabs.get(claimedTabId);
      const route = await validateConversationSpawnTabRoute(intent, claimedTab);
      if (route.ok || route.reason === "spawn_page_not_ready") {
        return { ok: true, reason: "tab_recovered", tabId: claimedTabId };
      }
      if (route.reason === "spawn_tab_claim_mismatch") return route;
    } catch (_error) {}
  }

  const marked = await findConversationSpawnMarkerTabs(intent.transaction_id);
  if (marked.length > 1) {
    throw new Error("multiple tabs claim the same conversation spawn transaction");
  }
  if (marked.length === 1) {
    await rememberConversationSpawnTab(intent.transaction_id, marked[0].id);
    return { ok: true, reason: "tab_recovered", tabId: marked[0].id };
  }
  return null;
}

async function createConversationSpawnTab(intent) {
  validateConversationSpawnBrowserIntent(intent);
  await requireConversationSpawnBootstrapDigest(intent);
  if (intent.tab_id !== null) {
    try {
      const existing = await chrome.tabs.get(intent.tab_id);
      if (existing?.id) {
        const marker = conversationSpawnMarkerFromTab(existing);
        const claimed = await conversationSpawnTabClaimMatches(intent.transaction_id, existing.id);
        if (marker === intent.transaction_id || claimed) {
          await rememberConversationSpawnTab(intent.transaction_id, existing.id);
          return { ok: true, reason: "tab_recovered", tabId: existing.id };
        }
        throw new Error("conversation spawn original tab claim does not match transaction");
      }
    } catch (error) {
      if (String(error?.message || error).includes("claim does not match")) throw error;
    }
    throw new Error("conversation spawn original tab is unavailable; replacement requires explicit recovery");
  }

  const recovered = await recoverConversationSpawnTab(intent);
  if (recovered) return recovered;

  const tab = await chrome.tabs.create({
    url: conversationSpawnMarkerUrl(intent.transaction_id),
    active: false
  });
  if (!Number.isInteger(tab?.id)) {
    throw new Error("conversation spawn tab creation did not return a tab id");
  }
  await rememberConversationSpawnTab(intent.transaction_id, tab.id);
  return { ok: true, reason: "tab_created", tabId: tab.id };
}

async function reattachConversationSpawnTab(intent) {
  validateConversationSpawnBrowserIntent(intent, { requireTab: true });
  await requireConversationSpawnBootstrapDigest(intent);
  try {
    const existing = await chrome.tabs.get(intent.tab_id);
    if (existing?.id) {
      const route = await validateConversationSpawnTabRoute(intent, existing);
      if (route.ok || route.reason === "spawn_page_not_ready") {
        return { ok: true, reason: "tab_recovered", tabId: existing.id };
      }
      if (route.reason === "spawn_tab_claim_mismatch") return route;
    }
  } catch (_error) {}

  const recovered = await recoverConversationSpawnTab(intent);
  if (recovered) return recovered;

  const tab = await chrome.tabs.create({
    url: conversationSpawnMarkerUrl(intent.transaction_id),
    active: false
  });
  if (!Number.isInteger(tab?.id)) {
    throw new Error("conversation spawn reattach did not return a tab id");
  }
  await rememberConversationSpawnTab(intent.transaction_id, tab.id);
  return { ok: true, reason: "tab_reattached", tabId: tab.id };
}

async function sendConversationSpawnContentMessage(intent, type) {
  validateConversationSpawnBrowserIntent(intent, { requireTab: true });
  await requireConversationSpawnBootstrapDigest(intent);
  let tab;
  try {
    tab = await chrome.tabs.get(intent.tab_id);
  } catch (error) {
    return { ok: false, reason: "spawn_tab_unavailable", error: String(error) };
  }
  if (!tab?.id) return { ok: false, reason: "spawn_tab_unavailable" };

  const route = await validateConversationSpawnTabRoute(intent, tab);
  if (!route.ok) return route;
  const ready = await ensureConversationSpawnContent(tab.id);
  if (!ready?.ok) return ready;
  try {
    const response = await chrome.tabs.sendMessage(tab.id, {
      type,
      protocolVersion: CONVERSATION_SPAWN_CONTENT_PROTOCOL_VERSION,
      transactionId: intent.transaction_id,
      childRequestDigest: intent.child_request_digest,
      bootstrapDigest: intent.bootstrap_digest,
      bootstrapText: intent.bootstrap_text
    }, { frameId: 0 });
    if (response?.protocolVersion !== CONVERSATION_SPAWN_CONTENT_PROTOCOL_VERSION) {
      return { ok: false, reason: "spawn_content_protocol_mismatch" };
    }
    if (response?.childConversationUrl) {
      const canonical = normalizeConversationUrl(response.childConversationUrl);
      if (!canonical || canonical !== response.childConversationUrl) {
        return { ok: false, reason: "spawn_child_identity_invalid" };
      }
      return { ...response, childConversationUrl: canonical };
    }
    return response || { ok: false, reason: "spawn_content_unavailable" };
  } catch (error) {
    return { ok: false, reason: "spawn_content_unavailable", error: String(error) };
  }
}

async function inspectConversationSpawnContentState(intent) {
  validateConversationSpawnBrowserIntent(intent, { requireTab: true });
  await requireConversationSpawnBootstrapDigest(intent);
  let tab;
  try {
    tab = await chrome.tabs.get(intent.tab_id);
  } catch (error) {
    return { ok: false, reason: "spawn_tab_unavailable", error: String(error) };
  }
  if (!tab?.id) return { ok: false, reason: "spawn_tab_unavailable" };
  if (!await conversationSpawnTabClaimMatches(intent.transaction_id, tab.id)) {
    return { ok: false, reason: "spawn_tab_claim_mismatch" };
  }
  const ready = await ensureConversationSpawnContent(tab.id);
  if (!ready?.ok) return ready;
  try {
    const response = await chrome.tabs.sendMessage(tab.id, {
      type: "bridge:spawn-diagnostic",
      protocolVersion: CONVERSATION_SPAWN_CONTENT_PROTOCOL_VERSION,
      transactionId: intent.transaction_id,
      childRequestDigest: intent.child_request_digest,
      bootstrapDigest: intent.bootstrap_digest,
      bootstrapText: intent.bootstrap_text
    }, { frameId: 0 });
    if (response?.protocolVersion !== CONVERSATION_SPAWN_CONTENT_PROTOCOL_VERSION) {
      return { ok: false, reason: "spawn_content_protocol_mismatch" };
    }
    return response || { ok: false, reason: "spawn_content_unavailable" };
  } catch (error) {
    return { ok: false, reason: "spawn_content_unavailable", error: String(error) };
  }
}

async function submitConversationSpawnBootstrap(intent) {
  return sendConversationSpawnContentMessage(intent, "bridge:spawn-bootstrap");
}

async function reconcileConversationSpawn(intent) {
  return sendConversationSpawnContentMessage(intent, "bridge:spawn-reconcile");
}
