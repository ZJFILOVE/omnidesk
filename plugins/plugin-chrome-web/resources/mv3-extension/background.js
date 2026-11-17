/* OmniDesk local MV3 service worker (strictly offline/loopback; zero remote code). */
const api = globalThis.chrome;
const ALLOWED_PROTOCOLS = new Set(["http:", "https:"]);

function isSafeHttpUrl(rawUrl) {
  try {
    const parsed = new URL(rawUrl);
    return ALLOWED_PROTOCOLS.has(parsed.protocol) && Boolean(parsed.hostname);
  } catch (_) {
    return false;
  }
}

if (api?.commands?.onCommand) {
  api.commands.onCommand.addListener(async (command, commandTab) => {
    if (command !== "toggle-bilingual") return;
    let tab = commandTab;
    if (!tab && api.tabs?.query) {
      [tab] = await api.tabs.query({ active: true, currentWindow: true });
    }
    if (!tab?.id || !isSafeHttpUrl(tab.url || "")) return;
    try {
      await api.tabs.sendMessage(tab.id, { type: "omnidesk:toggle-bilingual" });
    } catch (_) {
      // Tab may not have content script injected yet (e.g., restricted browser pages).
    }
  });
}

if (api?.runtime?.onMessage) {
  api.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (sender?.id && api.runtime?.id && sender.id !== api.runtime.id) {
      return false;
    }
    if (!message || typeof message !== "object") {
      return false;
    }
    if (message.type === "omnidesk:ping") {
      sendResponse({ ok: true, bridge: "omnidesk-mv3", version: "0.2.0" });
      return false;
    }
    if (message.type === "omnidesk:extract-tab") {
      (async () => {
        const [tab] = await api.tabs.query({ active: true, currentWindow: true });
        if (!tab?.id || !isSafeHttpUrl(tab.url || "")) {
          sendResponse({ ok: false, error_code: "invalid_argument", error: "No active http/https tab" });
          return;
        }
        try {
          const snapshot = await api.tabs.sendMessage(tab.id, { type: "omnidesk:collect-dom" });
          sendResponse({ ok: true, ...snapshot });
        } catch (err) {
          sendResponse({ ok: false, error_code: "service_offline", error: String(err?.message || err) });
        }
      })();
      return true;
    }
    return false;
  });
}
