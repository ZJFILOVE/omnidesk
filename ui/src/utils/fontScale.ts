const KEY = 'omnidesk-font-scale';
export function getFontScale(): number {
  try { const value = Number(localStorage.getItem(KEY)); return value >= 1 && value <= 1.6 ? value : 1.2; } catch { return 1.2; }
}
export function applyFontScale(value = getFontScale()) {
  const scale = Math.max(1, Math.min(1.6, value));
  document.documentElement.style.setProperty('--ui-font-scale', String(scale));
  try { localStorage.setItem(KEY, String(scale)); } catch { /* Private storage can be disabled. */ }
}
