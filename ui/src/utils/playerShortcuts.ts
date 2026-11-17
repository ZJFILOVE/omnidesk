export type PlayerShortcut = 'back' | 'forward' | 'quieter' | 'louder' | 'mute' | 'slower' | 'faster' | 'normal' | 'previous' | 'next' | 'skip';
export const PLAYER_SHORTCUT_EVENT = 'omnidesk:player-shortcut';
export function playerShortcut(event: Pick<KeyboardEvent, 'key' | 'shiftKey' | 'ctrlKey' | 'metaKey' | 'altKey'>): PlayerShortcut | undefined {
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  if (event.shiftKey) return ({ N: 'next', P: 'previous', '>': 'next', '<': 'previous' } as const)[event.key as 'N'];
  return ({ ArrowLeft: 'back', ArrowRight: 'forward', ArrowDown: 'quieter', ArrowUp: 'louder', m: 'mute', ',': 'slower', '，': 'slower', '.': 'faster', '。': 'faster', Backspace: 'normal', F9: 'skip' } as Record<string, PlayerShortcut>)[event.key];
}
