/* Fixed-pointer parent wake only; never inspect assistant replies. */
(() => {
  "use strict";
  if (globalThis.__localAgentGithubFirstPromptV1) return;
  globalThis.__localAgentGithubFirstPromptV1 = true;
  const editor = globalThis.LocalAgentGithubFirstComposerV1;
  if (!editor) return;
  const RE = /^https:\/\/chatgpt\.com\/c\/[A-Za-z0-9_-]{1,200}$/;
  const consumed = new Set();
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== "bridge:github-first-parent-wake") return false;
    const parent = String(message.parentUrl || "");
    const id = String(message.wakeId || "");
    if (!RE.test(parent) || location.href.replace(/\/$/, "") !== parent ||
        !/^wake-[0-9a-f]{64}$/.test(id) ||
        typeof message.text !== "string" || message.text.length > 1200 ||
        consumed.has(id)) {
      sendResponse({ ok: false, reason: "invalid_wake" });
      return false;
    }
    const key = "local-agent-github-first-parent-wake:" + id;
    if (sessionStorage.getItem(key)) {
      sendResponse({ ok: false, reason: "wake_already_attempted" });
      return false;
    }
    consumed.add(id);
    const routeAllowed = () => location.href.replace(/\/$/, "") === parent;
    editor.submitOnce({
      text: message.text, routeAllowed,
      beforeMutation: () => sessionStorage.setItem(key, "submission_unknown"),
      phase: phase => sessionStorage.setItem(key, phase),
      beforeClick: () => sessionStorage.setItem(key, "send_click_attempted")
    }).then(result => sendResponse({ ok: result.ok, submitted: result.submitted === true,
      reason: result.reason })).catch(() => sendResponse({ ok: false, reason: "wake_unknown" }));
    return true;
  });
})();
