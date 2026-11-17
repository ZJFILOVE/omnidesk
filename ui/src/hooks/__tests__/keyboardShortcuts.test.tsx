// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from '../../App';
import { useGlobalKeys } from '../useGlobalKeys';

afterEach(() => {
  cleanup();
  delete window.__TAURI__;
  vi.restoreAllMocks();
});

describe('useGlobalKeys shortcuts and IME isolation', () => {
  it('dispatches ?, Ctrl+Tab, Space, and Alt+<letter> while isolating during IME composition', () => {
    const onToggleHelp = vi.fn();
    const onCloseHelp = vi.fn();
    const onCycleZone = vi.fn();
    const onSpace = vi.fn();
    const onAltShortcut = vi.fn(() => true);
    const onToggleTheme = vi.fn();

    renderHook(() =>
      useGlobalKeys({
        paletteOpen: false,
        hintActive: false,
        helpOpen: false,
        onSelectWorkbench: vi.fn(),
        onTogglePalette: vi.fn(),
        onClosePalette: vi.fn(),
        onToggleDock: vi.fn(),
        onToggleTheme,
        onToggleHelp,
        onCloseHelp,
        onTriggerHints: vi.fn(),
        onCancelHints: vi.fn(),
        onMove: vi.fn(() => null),
        onActivate: vi.fn(() => false),
        onSpace,
        onCycleZone,
        onAltShortcut,
        onTriggerSearch: vi.fn(),
      }),
    );

    fireEvent.keyDown(window, { key: '?' });
    expect(onToggleHelp).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(window, { key: 'Tab', ctrlKey: true });
    expect(onCycleZone).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(window, { key: ' ', code: 'Space' });
    expect(onSpace).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(window, { key: 'p', altKey: true });
    expect(onAltShortcut).toHaveBeenCalledWith('p');

    fireEvent.keyDown(window, { key: 't', altKey: true });
    expect(onToggleTheme).toHaveBeenCalledTimes(1);

    // IME composition must block all of them
    act(() => {
      window.dispatchEvent(new Event('compositionstart'));
    });
    fireEvent.keyDown(window, { key: '?' });
    fireEvent.keyDown(window, { key: 'Tab', ctrlKey: true });
    fireEvent.keyDown(window, { key: ' ', code: 'Space' });
    fireEvent.keyDown(window, { key: 'j', altKey: true });

    expect(onToggleHelp).toHaveBeenCalledTimes(1);
    expect(onCycleZone).toHaveBeenCalledTimes(1);
    expect(onSpace).toHaveBeenCalledTimes(1);
    expect(onAltShortcut).toHaveBeenCalledTimes(1);

    act(() => {
      window.dispatchEvent(new Event('compositionend'));
    });
    fireEvent.keyDown(window, { key: '?' });
    expect(onToggleHelp).toHaveBeenCalledTimes(2);
  });
});

describe('App keyboard integration, OverlayLayer/KeyboardHelp mount, and F8 textarea blur', () => {
  it('mounts OverlayLayer & KeyboardHelp in App, handles ?, Space playback, and Alt+<letter>', () => {
    render(<App />);

    expect(screen.getByTestId('overlay-layer')).not.toBeNull();
    expect(screen.getByTestId('offline-preview-banner').textContent).toContain('离线预览：数据为示例');

    // ? opens KeyboardHelp
    fireEvent.keyDown(window, { key: '?' });
    expect(screen.getByTestId('keyboard-help-dialog')).not.toBeNull();

    // ? again closes KeyboardHelp without double-toggling
    fireEvent.keyDown(window, { key: '?' });
    expect(screen.queryByTestId('keyboard-help-dialog')).toBeNull();

    // Space toggles playback on MediaWorkbench (default workbench)
    expect(screen.getByText('PAUSED')).not.toBeNull();
    fireEvent.keyDown(window, { key: ' ', code: 'Space' });
    expect(screen.getByText('PLAYING')).not.toBeNull();

    // Alt+P switches directly to PluginStore
    fireEvent.keyDown(window, { key: 'p', altKey: true });
    expect(screen.getByTestId('workbench-store')).not.toBeNull();
    expect(screen.getByRole('heading', { name: '插件中心' })).not.toBeNull();
  });

  it('blurs F8 CompanionDock textarea on Ctrl+Tab so h/j/k/l navigation works immediately', () => {
    const focusOrder: Record<string, number> = {
      'nav-workbench-media': 0,
      'nav-workbench-book': 1,
      'nav-workbench-text': 2,
      'nav-workbench-ai': 3,
      'nav-workbench-plugins': 4,
    };
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      const id = this.dataset?.focusId ?? '';
      const navIdx = focusOrder[id];
      if (navIdx !== undefined) {
        const top = 60 + navIdx * 48;
        return { left: 16, top, width: 180, height: 36, right: 196, bottom: top + 36, x: 16, y: top, toJSON: () => ({}) } as DOMRect;
      }
      return { left: 240, top: 80, width: 320, height: 40, right: 560, bottom: 120, x: 240, y: 80, toJSON: () => ({}) } as DOMRect;
    });

    render(<App />);

    // Open F8 dock
    fireEvent.keyDown(window, { key: 'F8' });
    const dock = screen.getByTestId('companion-dock');
    const textarea = dock.querySelector('textarea') as HTMLTextAreaElement;
    expect(textarea).not.toBeNull();

    // Focus textarea inside F8 dock
    textarea.focus();
    expect(document.activeElement).toBe(textarea);

    // Pressing 'h' inside textarea must NOT hijack typing into spatial navigation
    fireEvent.keyDown(textarea, { key: 'h' });
    expect(document.activeElement).toBe(textarea);

    // Ctrl+Tab cycles back to nav and blurs the textarea
    fireEvent.keyDown(textarea, { key: 'Tab', ctrlKey: true });
    expect(document.activeElement).not.toBe(textarea);

    const statusBarText = screen.getByText(/焦点:/).parentElement?.textContent ?? '';
    expect(statusBarText).toContain('nav-workbench-media');

    // Now h/j/k/l moves spatial focus immediately without being blocked by textarea
    fireEvent.keyDown(document.body, { key: 'j' });
    const nextStatusText = screen.getByText(/焦点:/).parentElement?.textContent ?? '';
    expect(nextStatusText).toContain('nav-workbench-book');
  });
});
