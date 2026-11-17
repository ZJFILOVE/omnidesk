/** Let long plugin articles scroll even when spatial focus is on their toolbar. */
export function scrollReader(event: KeyboardEvent): boolean {
  const focused = document.querySelector<HTMLElement>('[data-spatial-focused="true"]');
  const target = event.target instanceof HTMLElement ? event.target : null;
  const workspace = (focused?.closest('[data-testid="plugin-workspace"]')
    ?? target?.closest('[data-testid="plugin-workspace"]')) as HTMLElement | null;
  const reader = workspace?.querySelector<HTMLElement>('[data-reader-scroll="true"]');
  if (!reader || reader.closest('[aria-hidden="true"], [hidden]') || reader.scrollHeight <= reader.clientHeight) return false;
  const page = Math.max(80, reader.clientHeight * 0.85);
  const step = ({ ArrowDown: 64, j: 64, ArrowUp: -64, k: -64, PageDown: page, PageUp: -page } as Record<string, number>)[event.key];
  if (step === undefined && event.key !== 'Home' && event.key !== 'End') return false;
  // Preserve nested controls such as select menus and code blocks.
  if (target?.closest('input, textarea, select, [contenteditable="true"], pre')) return false;
  const top = event.key === 'Home' ? 0 : event.key === 'End' ? reader.scrollHeight : reader.scrollTop + step;
  reader.scrollTop = Math.max(0, Math.min(reader.scrollHeight - reader.clientHeight, top));
  return true;
}
