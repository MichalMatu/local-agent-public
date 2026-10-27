/* Isolated optional Auto Wake delivery; uses the existing proven Send only. */
(() => {
  "use strict";
  if (globalThis.__localAgentOptionalAutoWakeV1) return;
  globalThis.__localAgentOptionalAutoWakeV1 = true;
  const editor = globalThis.LocalAgentGithubFirstComposerV1;
  if (!editor) return;
  const attempted = new Set();
  chrome.runtime.onMessage.addListener((message, _sender, reply) => {
    if (message?.type !== "bridge:optional-auto-wake") return false;
    const id = String(message.attemptId || "");
    const url = String(message.conversationUrl || "");
    const valid = /^https:\/\/chatgpt\.com\/c\/[A-Za-z0-9_-]{1,200}$/.test(url) &&
      location.href.replace(/\/$/, "") === url &&
      /^[0-9a-f-]{36}$/.test(id) && typeof message.text === "string" &&
      message.text.trim() && message.text.length <= 1200;
    if (!valid || attempted.has(id)) {
      reply({ ok: false, reason: "invalid_or_consumed_wake" });
      return false;
    }
    const key = "local-agent-optional-auto-wake:" + id;
    if (sessionStorage.getItem(key)) {
      reply({ ok: false, reason: "wake_already_attempted" });
      return false;
    }
    // Auto Wake never adopts even a byte-identical operator draft. Unlike
    // child dispatch, it has no ownership claim on any pre-existing composer.
    const composer = editor.findComposer();
    if (!composer || ("value" in composer ? composer.value : composer.textContent || "").trim()) {
      reply({ ok: false, reason: composer ? "composer_busy" : "composer_unavailable" });
      return false;
    }
    attempted.add(id);
    sessionStorage.setItem(key, "submission_unknown");
    editor.submitOnce({
      text: message.text,
      routeAllowed: () => location.href.replace(/\/$/, "") === url,
      beforeMutation: () => sessionStorage.setItem(key, "composer_attempted"),
      beforeClick: () => sessionStorage.setItem(key, "send_click_attempted"),
      phase: phase => sessionStorage.setItem(key, phase)
    }).then(result => reply({ ok: result.ok, submitted: result.submitted === true,
      reason: result.reason })).catch(() => reply({ ok: false, reason: "delivery_unknown" }));
    return true;
  });
})();
