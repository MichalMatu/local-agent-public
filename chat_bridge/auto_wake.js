/* Optional Chrome-only timer for one operator-selected ChatGPT conversation.
 * No GitHub polling, task scheduling, child delegation, or DOM reply reading.
 */
const AUTO_WAKE_ALARM = "local-agent-optional-auto-wake";
const AUTO_WAKE_KEY = "localAgentOptionalAutoWakeV1";
const AUTO_WAKE_DEFAULT_PROMPT = "Check the current task and GitHub results. Continue if safe; otherwise explain what needs my decision.";
const AUTO_WAKE_MINUTES_MIN = 2;
const AUTO_WAKE_MINUTES_MAX = 1440;

async function autoWakeRead() {
  const raw = (await chrome.storage.local.get(AUTO_WAKE_KEY))[AUTO_WAKE_KEY];
  return raw && typeof raw === "object" && !Array.isArray(raw) ? raw : null;
}

function autoWakeValidSettings(state) {
  return Boolean(state &&
    Number.isInteger(state.intervalMinutes) &&
    state.intervalMinutes >= AUTO_WAKE_MINUTES_MIN &&
    state.intervalMinutes <= AUTO_WAKE_MINUTES_MAX &&
    typeof state.prompt === "string" && state.prompt.trim() && state.prompt.length <= 1200 &&
    typeof state.conversationUrl === "string" && state.conversationUrl.length > 0 &&
    normalizeConversationUrl(state.conversationUrl) === state.conversationUrl);
}

async function autoWakeStatus() {
  const state = await autoWakeRead();
  return { ok: true, enabled: state?.enabled === true && autoWakeValidSettings(state),
    conversationUrl: state?.conversationUrl || "",
    intervalMinutes: state?.intervalMinutes || 10,
    prompt: state?.prompt || AUTO_WAKE_DEFAULT_PROMPT,
    nextWakeAt: state?.enabled && autoWakeValidSettings(state) ? state.nextWakeAt : null,
    lastStatus: state?.lastStatus || "not_configured" };
}

async function autoWakeStart({ intervalMinutes, prompt }) {
  if (!Number.isInteger(intervalMinutes) ||
      intervalMinutes < AUTO_WAKE_MINUTES_MIN || intervalMinutes > AUTO_WAKE_MINUTES_MAX ||
      typeof prompt !== "string" || !prompt.trim() || prompt.length > 1200) {
    return { ok: false, reason: "invalid_auto_wake_settings" };
  }
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const conversationUrl = tabs.length === 1 ? normalizeConversationUrl(tabs[0].url) : "";
  if (!conversationUrl) return { ok: false, reason: "open_target_chatgpt_chat" };
  const current = await autoWakeRead();
  if (current?.lastStatus === "sending") {
    return { ok: false, reason: "wait_for_current_wake" };
  }
  const nextWakeAt = Date.now() + intervalMinutes * 60000;
  await chrome.storage.local.set({ [AUTO_WAKE_KEY]: {
    enabled: true, conversationUrl, intervalMinutes, prompt,
    nextWakeAt, lastStatus: "scheduled", attemptId: ""
  } });
  await chrome.alarms.create(AUTO_WAKE_ALARM, { when: nextWakeAt });
  return autoWakeStatus();
}

async function autoWakeStop() {
  const state = await autoWakeRead();
  if (state) await chrome.storage.local.set({ [AUTO_WAKE_KEY]: {
    ...state, enabled: false, nextWakeAt: null, lastStatus: "stopped", attemptId: ""
  } });
  await chrome.alarms.clear(AUTO_WAKE_ALARM);
  return autoWakeStatus();
}

async function autoWakeInitialize() {
  const state = await autoWakeRead();
  if (!state?.enabled) return chrome.alarms.clear(AUTO_WAKE_ALARM);
  if (!autoWakeValidSettings(state) || !Number.isFinite(state.nextWakeAt)) {
    await chrome.storage.local.set({ [AUTO_WAKE_KEY]: {
      ...state, enabled: false, nextWakeAt: null, lastStatus: "invalid_configuration"
    } });
    return chrome.alarms.clear(AUTO_WAKE_ALARM);
  }
  // An overdue alarm after Chrome restart does not replay missed messages.
  const nextWakeAt = state.nextWakeAt > Date.now() ? state.nextWakeAt :
    Date.now() + state.intervalMinutes * 60000;
  if (nextWakeAt !== state.nextWakeAt) {
    await chrome.storage.local.set({ [AUTO_WAKE_KEY]: { ...state, nextWakeAt } });
  }
  return chrome.alarms.create(AUTO_WAKE_ALARM, { when: nextWakeAt });
}

async function autoWakeSend(state, attemptId) {
  const tabs = (await chrome.tabs.query({ url: [
    "https://chatgpt.com/c/*", "https://chat.openai.com/c/*"
  ] })).filter(tab => normalizeConversationUrl(tab.url) === state.conversationUrl);
  if (tabs.length !== 1 || !Number.isInteger(tabs[0].id)) {
    return { status: tabs.length > 1 ? "chat_ambiguous" : "chat_not_open",
      retryNextInterval: true };
  }
  const tabId = tabs[0].id;
  try {
    await chrome.scripting.executeScript({ target: { tabId, frameIds: [0] },
      files: ["github_first_composer.js", "auto_wake_content.js"] });
    const result = await chrome.tabs.sendMessage(tabId, {
      type: "bridge:optional-auto-wake", attemptId,
      conversationUrl: state.conversationUrl, text: state.prompt
    }, { frameId: 0 });
    if (result?.ok && result.submitted === true) {
      return { status: "sent", retryNextInterval: true };
    }
    // The content script rejects pre-existing drafts *before* invoking Send.
    // This is the only confirmed non-effect eligible for next-interval retry.
    if (result?.reason === "composer_busy") {
      return { status: "composer_busy", retryNextInterval: true };
    }
    return { status: "paused_" + (result?.reason || "unknown"), retryNextInterval: false };
  } catch (_error) {
    return { status: "paused_delivery_unknown", retryNextInterval: false };
  }
}

async function autoWakeOnAlarm() {
  const state = await autoWakeRead();
  if (!state?.enabled) return;
  if (!autoWakeValidSettings(state) || !Number.isFinite(state.nextWakeAt)) {
    return autoWakeInitialize();
  }
  const now = Date.now();
  if (now + 1000 < state.nextWakeAt) return autoWakeInitialize();
  if (now - state.nextWakeAt > state.intervalMinutes * 60000) {
    return autoWakeInitialize(); // Missed while sleeping: no catch-up storm.
  }
  // Consume the alarm BEFORE any browser effect. A crash leaves Auto Wake
  // paused, never a replay of an ambiguous Send.
  const attemptId = crypto.randomUUID();
  await chrome.storage.local.set({ [AUTO_WAKE_KEY]: {
    ...state, enabled: false, nextWakeAt: null, lastStatus: "sending", attemptId
  } });
  const outcome = await autoWakeSend(state, attemptId);
  const current = await autoWakeRead();
  if (current?.attemptId !== attemptId) return; // Operator stopped/reconfigured.
  const enabled = outcome.retryNextInterval;
  const nextWakeAt = enabled ? Date.now() + state.intervalMinutes * 60000 : null;
  await chrome.storage.local.set({ [AUTO_WAKE_KEY]: {
    ...current, enabled, nextWakeAt, lastStatus: outcome.status, attemptId: ""
  } });
  if (enabled) await chrome.alarms.create(AUTO_WAKE_ALARM, { when: nextWakeAt });
}
