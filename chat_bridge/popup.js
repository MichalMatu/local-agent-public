"use strict";
const $ = selector => document.querySelector(selector);
const connection = $("#connection");
const status = $("#status");
const safety = $("#safety");
const message = $("#message");
const autoWakeFeedback = $("#autoWakeStatus");
const wakeState = $("#wakeState");
const wakeTarget = $("#autoWakeTarget");
let autoWakeEditing = false;

const WAKE_MESSAGES = {
  not_configured: "Choose an interval and start Auto Wake for this chat.",
  scheduled: "Scheduled. Chrome will send the message at the next interval.",
  sent: "Last message sent. Waiting for the next interval.",
  stopped: "Stopped. No messages will be sent.",
  sending: "The previous Send outcome is uncertain. Check the chat before restarting.",
  chat_not_open: "Target chat is not open. This interval was skipped.",
  chat_ambiguous: "More than one tab has this chat open. This interval was skipped.",
  composer_busy: "The chat has an unsent draft. Auto Wake did not touch it.",
  invalid_configuration: "Saved settings were invalid. Auto Wake was stopped."
};
function wakeStatusDescription(state) {
  const raw = state.lastStatus || "not_configured";
  if (raw.startsWith("paused_")) {
    return "Paused after an unconfirmed Send. Check the chat before manually restarting.";
  }
  return WAKE_MESSAGES[raw] || "Check the target chat and Auto Wake settings.";
}
function wakeDisplayTarget(url) {
  if (!url) return "Not selected";
  const id = url.split("/").pop() || "";
  return "Chat …" + id.slice(-12);
}
function updateWakeUi(state) {
  wakeState.className = "wake-state";
  wakeState.textContent = state.enabled ? "Active" :
    /^paused_|^sending$|^invalid_configuration$/.test(state.lastStatus) ? "Needs attention" : "Off";
  if (state.enabled) wakeState.classList.add("active");
  else if (wakeState.textContent === "Needs attention") wakeState.classList.add("attention");
  wakeTarget.textContent = wakeDisplayTarget(state.conversationUrl);
  wakeTarget.title = state.conversationUrl || "No chat selected";
  if (!autoWakeEditing) {
    $("#autoWakeInterval").value = state.intervalMinutes;
    $("#autoWakePrompt").value = state.prompt;
  }
  $("#autoWakeStop").disabled = !state.enabled && wakeState.textContent !== "Needs attention";
  $("#autoWakeStart").textContent = state.enabled ? "Apply to this chat" : "Start for this chat";
  const date = state.nextWakeAt ? new Date(state.nextWakeAt) : null;
  const next = date && Number.isFinite(date.getTime()) ?
    ` Next wake: ${date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}.` : "";
  autoWakeFeedback.textContent = wakeStatusDescription(state) + next;
}
async function refreshAutoWake() {
  const state = await chrome.runtime.sendMessage({ type: "bridge:auto-wake-status" });
  if (!state?.ok) { autoWakeFeedback.textContent = "Auto Wake status unavailable."; return; }
  updateWakeUi(state);
}
async function refreshTransport() {
  const reply = await chrome.runtime.sendMessage({ type: "bridge:github-first-local-status" });
  const paired = Boolean(reply?.paired);
  connection.classList.toggle("connected", paired && reply.reachable && !reply.pairingOnly);
  connection.textContent = !reply?.ok ? "Unavailable" :
    !paired ? "Not connected" :
    reply.pairingOnly ? "Pairing only" :
    reply.reachable ? "Connected" : "Runner offline";
  $("#version").textContent = reply?.extensionVersion ? `· v${reply.extensionVersion}` : "· GitHub-first";
  status.textContent = !reply?.ok ? "Cannot reach local transport status." :
    `${reply.transactions} transport events · ${reply.unknown} unresolved`;
  safety.textContent = reply?.sendReleased ?
    "GitHub-first Send is enabled for authenticated dispatches." :
    "GitHub-first Send is locked. Auto Wake is a separate optional browser action.";
  $("#connect").hidden = paired;
  $("#useParent").disabled = !paired || !reply.reachable || reply.pairingOnly;
  const code = $("#pairingCode");
  if (paired) code.textContent = "";
  else if (reply?.pairing?.status === "pending")
    code.textContent = `Confirm code ${reply.pairing.code} in the macOS dialog.`;
  else if (reply?.pairing?.status === "denied")
    code.textContent = "Pairing denied. Use Connect to try again.";
  else if (reply?.pairing?.status === "expired")
    code.textContent = "Pairing expired. Use Connect to try again.";
  else if (reply?.ok) code.textContent = "Connect to enable child-chat delegation.";
  if (reply?.unresolvedReasons?.length) {
    status.title = `Unresolved: ${reply.unresolvedReasons.join(", ")}`;
  } else status.removeAttribute("title");
}
$("#connect").addEventListener("click", async () => {
  const button = $("#connect");
  button.disabled = true;
  $("#pairingCode").textContent = "Contacting Local Agent…";
  try {
    const reply = await chrome.runtime.sendMessage({ type: "bridge:github-first-local-pair-start" });
    if (!reply?.ok) throw new Error("Pairing service unavailable");
    $("#pairingCode").textContent = `Confirm code ${reply.code} in the macOS dialog.`;
  } catch (_error) {
    $("#pairingCode").textContent = "Could not start pairing. Check the Local Agent connection.";
  } finally { button.disabled = false; }
});
$("#useParent").addEventListener("click", async () => {
  const feedback = $("#parentStatus");
  feedback.textContent = "Selecting this chat…";
  try {
    const reply = await chrome.runtime.sendMessage({ type: "bridge:github-first-local-select-parent" });
    feedback.textContent = reply?.ok ?
      `Delegation parent selected: ${wakeDisplayTarget(reply.parentUrl)}.` :
      "Could not select this chat as parent. Open a ChatGPT conversation.";
  } catch (_error) { feedback.textContent = "Local Agent connection unavailable."; }
});
$("#pair").addEventListener("click", async () => {
  const field = $("#pairingKey");
  try {
    const reply = await chrome.runtime.sendMessage({ type: "bridge:github-first-local-pair", key: field.value });
    message.textContent = reply?.ok ? "Pairing key saved." : "Pairing rejected. Check the machine-only key.";
  } catch (_error) { message.textContent = "Pairing unavailable."; }
  field.value = "";
  await refreshTransport().catch(() => undefined);
});
for (const id of ["#autoWakeInterval", "#autoWakePrompt"]) {
  $(id).addEventListener("input", () => { autoWakeEditing = true; });
}
$("#autoWakeStart").addEventListener("click", async () => {
  const button = $("#autoWakeStart");
  const intervalMinutes = Number($("#autoWakeInterval").value);
  const prompt = $("#autoWakePrompt").value;
  if (!Number.isInteger(intervalMinutes) || intervalMinutes < 2 || intervalMinutes > 1440 ||
      !prompt.trim() || prompt.length > 1200) {
    autoWakeFeedback.textContent = "Enter 2–1440 minutes and a non-empty message.";
    return;
  }
  button.disabled = true;
  try {
    const reply = await chrome.runtime.sendMessage({ type: "bridge:auto-wake-start",
      settings: { intervalMinutes, prompt } });
    if (!reply?.ok) {
      autoWakeFeedback.textContent = reply?.reason === "wait_for_current_wake" ?
        "The previous wake is still processing. Check this chat before restarting." :
        "Could not start. Open the ChatGPT chat you want to wake.";
      return;
    }
    autoWakeEditing = false;
    updateWakeUi(reply);
  } catch (_error) { autoWakeFeedback.textContent = "Auto Wake unavailable."; }
  finally { button.disabled = false; }
});
$("#autoWakeStop").addEventListener("click", async () => {
  const button = $("#autoWakeStop");
  button.disabled = true;
  try {
    const reply = await chrome.runtime.sendMessage({ type: "bridge:auto-wake-stop" });
    if (reply?.ok) { autoWakeEditing = false; updateWakeUi(reply); }
    else autoWakeFeedback.textContent = "Could not stop Auto Wake.";
  } catch (_error) { autoWakeFeedback.textContent = "Auto Wake unavailable."; }
});
Promise.allSettled([refreshTransport(), refreshAutoWake()]);
// The popup is ephemeral. Poll only while it is open; never dispatch Send here.
setInterval(() => {
  refreshTransport().catch(() => { connection.textContent = "Unavailable"; });
  refreshAutoWake().catch(() => { autoWakeFeedback.textContent = "Auto Wake unavailable."; });
}, 5000);
