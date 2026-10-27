/* Thin MV3 entrypoint. Transport alarm plus isolated optional Auto Wake. */
const GITHUB_FIRST_BRIDGE_ALARM = "local-agent-github-first-transport";

async function initializeGithubFirstLocalBridge() {
  await chrome.alarms.create(GITHUB_FIRST_BRIDGE_ALARM, { periodInMinutes: 1 });
  await githubFirstLocalPairReconcile().catch(() => undefined);
  await githubFirstLocalSerialize(() => autoWakeInitialize());
  return pollGithubFirstLocalBridge();
}

chrome.runtime.onInstalled.addListener(() => {
  initializeGithubFirstLocalBridge().catch(() => undefined);
});
chrome.runtime.onStartup.addListener(() => {
  initializeGithubFirstLocalBridge().catch(() => undefined);
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === AUTO_WAKE_ALARM) {
    githubFirstLocalSerialize(() => autoWakeOnAlarm()).catch(() => undefined);
    return;
  }
  if (alarm.name !== GITHUB_FIRST_BRIDGE_ALARM) return;
  githubFirstLocalPairReconcile().catch(() => undefined);
  pollGithubFirstLocalBridge().catch(() => undefined);
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (sender?.id !== chrome.runtime.id ||
      sender?.url !== chrome.runtime.getURL("popup.html") ||
      !message || typeof message !== "object") {
    sendResponse({ ok: false, reason: "operator_ui_required" });
    return false;
  }
  if (message.type === "bridge:auto-wake-status") {
    autoWakeStatus().then(sendResponse).catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (message.type === "bridge:auto-wake-start") {
    githubFirstLocalSerialize(() => autoWakeStart(message.settings || {})).then(sendResponse)
      .catch(() => sendResponse({ ok: false, reason: "auto_wake_start_failed" }));
    return true;
  }
  if (message.type === "bridge:auto-wake-stop") {
    githubFirstLocalSerialize(() => autoWakeStop()).then(sendResponse).catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (message.type === "bridge:github-first-local-status") {
    (async () => {
      const pairing = await githubFirstLocalPairReconcile();
      const { paired, reachable, pairingOnly } = await githubFirstLocalProbe();
      const stored = await chrome.storage.local.get(null);
      const items = Object.entries(stored)
        .filter(([key]) => key.startsWith(GITHUB_FIRST_LOCAL_RECORD_PREFIX) ||
          key.startsWith("githubFirstLocalParentWakeV1:"))
        .map(([, value]) => typeof value === "string" ? value : String(value?.phase || "unknown"));
      const unresolvedReasons = Object.entries(stored)
        .filter(([key, value]) => key.startsWith(GITHUB_FIRST_LOCAL_RECORD_PREFIX) &&
          value?.phase === "submission_unknown")
        .map(([, value]) => value?.last_reason || "unknown")
        .slice(0, 4);
      return { ok: true, paired, reachable, pairingOnly, pairing,
        sendReleased: GITHUB_FIRST_LOCAL_SEND_RELEASED,
        extensionVersion: chrome.runtime.getManifest().version,
        transactions: items.length,
        unknown: items.filter(item => item === "submission_unknown").length,
        unresolvedReasons };
    })().then(sendResponse).catch(() => sendResponse({ ok: false }));
    return true;
  }
  if (message.type === "bridge:github-first-local-select-parent") {
    githubFirstLocalSelectParent().then(sendResponse)
      .catch(() => sendResponse({ ok: false, reason: "local_parent_unavailable" }));
    return true;
  }
  if (message.type === "bridge:github-first-local-pair-start") {
    githubFirstLocalPairStart().then(sendResponse)
      .catch(() => sendResponse({ ok: false, reason: "local_agent_pairing_offline" }));
    return true;
  }
  if (message.type === "bridge:github-first-local-pair-finish") {
    githubFirstLocalPairFinish(message.ticket).then(sendResponse)
      .catch(() => sendResponse({ ok: false, reason: "pairing_failed" }));
    return true;
  }
  if (message.type === "bridge:github-first-local-pair") {
    githubFirstLocalSaveKey(message.key).then(sendResponse)
      .catch(() => sendResponse({ ok: false, reason: "local_pairing_failed" }));
    return true;
  }
  return false;
});

initializeGithubFirstLocalBridge().catch(() => undefined);
