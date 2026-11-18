/* Fallback Alt+Shift+T hotkey listener for Chrome --app windows where commands may not fire. */
(() => {
  if (globalThis.__qwenTranslatorHotkey) return;
  globalThis.__qwenTranslatorHotkey = true;
  const api = globalThis.browser || globalThis.chrome;
  window.addEventListener('keydown', (event) => {
    if (!event.altKey || !event.shiftKey || event.ctrlKey || event.metaKey) return;
    if (event.code !== 'KeyT' && (event.key || '').toLowerCase() !== 't') return;
    const target = event.target;
    if (target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/i.test(target.tagName))) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    api.runtime.sendMessage({type: 'qwen:hotkey'}).catch(() => {});
  }, true);
})();
