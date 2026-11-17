/* OmniDesk minimal MV3 content script (safe DOM inspection; no remote code or raw innerHTML injection). */
(() => {
  if (globalThis.__omnideskChromeBridge) return;
  globalThis.__omnideskChromeBridge = true;

  const SKIP_SELECTOR =
    "script,style,noscript,template,pre,code,kbd,samp,textarea,input,select,button,svg,math,canvas,video,audio,iframe,[data-omnidesk-owned]";
  const BLOCK_SELECTOR = "p,h1,h2,h3,h4,h5,h6,li,blockquote,figcaption,dt,dd";
  const api = globalThis.chrome;
  let bilingualActive = false;

  function collectDomSnapshot() {
    const blocks = [];
    const seen = new Set();
    for (const el of document.querySelectorAll(BLOCK_SELECTOR)) {
      if (el.closest(SKIP_SELECTOR) || el.hidden) continue;
      const text = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
      if (!text || seen.has(text)) continue;
      seen.add(text);
      blocks.push({ id: `b${blocks.length + 1}`, tag: el.localName, text });
      if (blocks.length >= 200) break;
    }
    const links = [];
    for (const anchor of document.querySelectorAll("a[href]")) {
      try {
        const resolved = new URL(anchor.getAttribute("href") || "", window.location.href);
        if (resolved.protocol !== "http:" && resolved.protocol !== "https:") continue;
        links.push({
          text: (anchor.textContent || "").replace(/\s+/g, " ").trim(),
          href: resolved.href,
        });
      } catch (_) {
        // Ignore malformed links.
      }
      if (links.length >= 200) break;
    }
    return {
      title: document.title || "",
      url: window.location.href,
      blocks,
      links,
    };
  }

  function clearAnnotations() {
    for (const node of document.querySelectorAll("[data-omnidesk-owned]")) {
      node.remove();
    }
    bilingualActive = false;
  }

  if (api?.runtime?.onMessage) {
    api.runtime.onMessage.addListener((message, _sender, sendResponse) => {
      if (!message || typeof message !== "object") return false;
      if (message.type === "omnidesk:collect-dom") {
        sendResponse(collectDomSnapshot());
        return false;
      }
      if (message.type === "omnidesk:toggle-bilingual") {
        if (bilingualActive) {
          clearAnnotations();
        } else {
          bilingualActive = true;
        }
        sendResponse({ ok: true, active: bilingualActive });
        return false;
      }
      if (message.type === "omnidesk:render-bilingual" && Array.isArray(message.segments)) {
        clearAnnotations();
        bilingualActive = true;
        const map = new Map(message.segments.map((item) => [item.original, item.translated]));
        for (const el of document.querySelectorAll(BLOCK_SELECTOR)) {
          if (el.closest(SKIP_SELECTOR)) continue;
          const original = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
          const translated = map.get(original);
          if (typeof translated !== "string" || !translated.trim()) continue;
          const host = document.createElement("span");
          host.dataset.omnideskOwned = "translation";
          host.style.display = "block";
          const shadow = host.attachShadow({ mode: "closed" });
          const box = document.createElement("div");
          box.style.cssText = "border-left:3px solid #3fa592;padding:2px 8px;margin:4px 0 8px;";
          box.textContent = translated;
          shadow.appendChild(box);
          el.insertAdjacentElement("afterend", host);
        }
        sendResponse({ ok: true, active: true });
        return false;
      }
      return false;
    });
  }
})();
