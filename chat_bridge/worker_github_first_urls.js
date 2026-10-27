/* Canonical tab URL identity only; never reads a transcript. */
function normalizeConversationUrl(raw) {
  try {
    const url = new URL(String(raw || ""));
    if (url.protocol !== "https:" || !["chatgpt.com", "chat.openai.com"].includes(url.hostname)) return "";
    const path = url.pathname.replace(/\/$/, "");
    const match = /^\/c\/([A-Za-z0-9_-]{1,200})$/.exec(path);
    return match ? "https://chatgpt.com/c/" + match[1] : "";
  } catch (_error) {
    return "";
  }
}
