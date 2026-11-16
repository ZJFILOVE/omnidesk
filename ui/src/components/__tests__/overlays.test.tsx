// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, renderHook, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { isEditableTarget, useImeGuard } from '../../hooks/useImeGuard';
import { CommandPalette } from '../CommandPalette';
import { CompanionDock } from '../CompanionDock';
import { HintOverlay } from '../HintOverlay';
import { KeyboardHelp } from '../KeyboardHelp';
import { OverlayLayer } from '../OverlayLayer';

const originalInnerWidth = window.innerWidth;
const originalInnerHeight = window.innerHeight;

afterEach(() => {
  cleanup();
  Object.defineProperty(window, 'innerWidth', {
    configurable: true,
    writable: true,
    value: originalInnerWidth,
  });
  Object.defineProperty(window, 'innerHeight', {
    configurable: true,
    writable: true,
    value: originalInnerHeight,
  });
});

describe('useImeGuard', () => {
  it('tracks composition lifecycle and ignores IME key events', () => {
    const { result } = renderHook(() => useImeGuard());

    expect(result.current.isComposing).toBe(false);
    expect(result.current.shouldIgnoreShortcut({ key: 'Enter' })).toBe(false);
    expect(result.current.shouldIgnoreShortcut({ key: 'Process' })).toBe(true);
    expect(result.current.shouldIgnoreShortcut({ keyCode: 229 })).toBe(true);
    expect(result.current.shouldIgnoreShortcut({ isComposing: true })).toBe(true);

    act(() => {
      window.dispatchEvent(new Event('compositionstart'));
    });

    expect(result.current.isComposing).toBe(true);
    expect(result.current.isComposingRef.current).toBe(true);
    expect(result.current.shouldIgnoreShortcut({ key: 'f' })).toBe(true);

    act(() => {
      window.dispatchEvent(new Event('compositionend'));
    });

    expect(result.current.isComposing).toBe(false);
    expect(result.current.isComposingRef.current).toBe(false);
    expect(result.current.shouldIgnoreShortcut({ key: 'f' })).toBe(false);
  });

  it('detects editable targets for single-key shortcut isolation', () => {
    const input = document.createElement('input');
    const textarea = document.createElement('textarea');
    const div = document.createElement('div');
    const editableDiv = document.createElement('div');
    editableDiv.setAttribute('contenteditable', 'true');

    expect(isEditableTarget(input)).toBe(true);
    expect(isEditableTarget(textarea)).toBe(true);
    expect(isEditableTarget(editableDiv)).toBe(true);
    expect(isEditableTarget(div)).toBe(false);
  });
});

describe('CommandPalette', () => {
  it('renders nothing when closed and executes filtered command when open', () => {
    const onClose = vi.fn();
    const onCommand = vi.fn();

    const { rerender } = render(
      <CommandPalette open={false} onClose={onClose} onCommand={onCommand} />,
    );
    expect(screen.queryByRole('dialog')).toBeNull();

    rerender(<CommandPalette open={true} onClose={onClose} onCommand={onCommand} />);
    const input = screen.getByLabelText('命令搜索') as HTMLInputElement;
    expect(screen.getByRole('dialog')).not.toBeNull();

    fireEvent.change(input, { target: { value: 'Graphite' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    expect(onCommand).toHaveBeenCalledWith('theme:toggle');
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('ignores Enter and Escape while IME composition is active', () => {
    const onClose = vi.fn();
    const onCommand = vi.fn();

    render(<CommandPalette open={true} onClose={onClose} onCommand={onCommand} />);
    const input = screen.getByLabelText('命令搜索');

    fireEvent.compositionStart(input);
    fireEvent.keyDown(input, { key: 'Enter' });
    fireEvent.keyDown(input, { key: 'Escape' });

    expect(onCommand).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.compositionEnd(input);
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('CompanionDock', () => {
  it('renders non-modal dock and adapts side/bottom layout by viewport aspect ratio', () => {
    const onClose = vi.fn();

    Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: 1600 });
    Object.defineProperty(window, 'innerHeight', { configurable: true, writable: true, value: 900 });

    render(<CompanionDock open={true} onClose={onClose} />);
    const dock = screen.getByTestId('companion-dock');
    expect(dock.getAttribute('role')).toBe('complementary');
    expect(dock.getAttribute('data-dock-layout')).toBe('side');

    act(() => {
      Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: 900 });
      Object.defineProperty(window, 'innerHeight', { configurable: true, writable: true, value: 900 });
      window.dispatchEvent(new Event('resize'));
    });

    expect(dock.getAttribute('data-dock-layout')).toBe('bottom');
  });

  it('guards Enter submit during IME composition and submits once composition ends', () => {
    const onClose = vi.fn();
    render(<CompanionDock open={true} onClose={onClose} />);

    const textarea = screen.getByLabelText('伴读栏提问输入') as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: '请解释这段证明' } });

    fireEvent.compositionStart(textarea);
    fireEvent.keyDown(textarea, { key: 'Enter' });
    const messages = within(screen.getByTestId('companion-messages'));
    expect(messages.queryByText('请解释这段证明')).toBeNull();

    fireEvent.compositionEnd(textarea);
    fireEvent.keyDown(textarea, { key: 'Enter' });
    expect(messages.getByText('请解释这段证明')).not.toBeNull();

    fireEvent.click(screen.getByLabelText('关闭伴读栏'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('KeyboardHelp', () => {
  it('renders shortcuts dialog when open and closes on Escape unless IME is composing', () => {
    const onClose = vi.fn();
    const { rerender } = render(<KeyboardHelp open={false} onClose={onClose} />);
    expect(screen.queryByRole('dialog')).toBeNull();

    rerender(<KeyboardHelp open={true} onClose={onClose} />);
    expect(screen.getByTestId('keyboard-help-dialog')).not.toBeNull();
    expect(screen.getByText('全局工作台切换')).not.toBeNull();

    act(() => {
      window.dispatchEvent(new Event('compositionstart'));
    });
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).not.toHaveBeenCalled();

    act(() => {
      window.dispatchEvent(new Event('compositionend'));
    });
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('HintOverlay', () => {
  const targets = [
    { id: 'card-media-1', label: 'A', x: 120, y: 140 },
    { id: 'card-text-2', label: 'S', x: 320, y: 140 },
  ];

  it('picks target on matching key or click and closes on Escape', () => {
    const onPick = vi.fn();
    const onClose = vi.fn();

    render(<HintOverlay open={true} targets={targets} onPick={onPick} onClose={onClose} />);
    const btnA = screen.getByRole('button', { name: 'A' });
    expect(btnA.style.left).toBe('120px');
    expect(btnA.style.top).toBe('140px');

    fireEvent.keyDown(window, { key: 's' });
    expect(onPick).toHaveBeenCalledWith('card-text-2');

    fireEvent.click(btnA);
    expect(onPick).toHaveBeenCalledWith('card-media-1');

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('assigns deterministic letter codes for long labels and respects IME composition', () => {
    const onPick = vi.fn();
    const onClose = vi.fn();
    const longTargets = [
      { id: 'item-1', label: 'Sparse Attention Video', x: 50, y: 80 },
      { id: 'item-2', label: 'BabelDOC Paper', x: 250, y: 80 },
    ];

    render(<HintOverlay open={true} targets={longTargets} onPick={onPick} onClose={onClose} />);

    act(() => {
      window.dispatchEvent(new Event('compositionstart'));
    });
    fireEvent.keyDown(window, { key: 'a' });
    expect(onPick).not.toHaveBeenCalled();

    act(() => {
      window.dispatchEvent(new Event('compositionend'));
    });
    fireEvent.keyDown(window, { key: 'a' });
    expect(onPick).toHaveBeenCalledWith('item-1');
  });
});

describe('OverlayLayer', () => {
  it('mounts all overlays and handles global shortcuts in uncontrolled mode', () => {
    const onPickHint = vi.fn();
    render(
      <OverlayLayer
        hintTargets={[{ id: 'target-1', label: 'J', x: 100, y: 100 }]}
        onPickHint={onPickHint}
      />,
    );

    expect(screen.getByTestId('overlay-layer')).not.toBeNull();
    expect(screen.queryByTestId('command-palette-dialog')).toBeNull();

    act(() => {
      window.dispatchEvent(new Event('compositionstart'));
    });
    fireEvent.keyDown(window, { key: 'f' });
    expect(screen.queryByTestId('hint-overlay')).toBeNull();
    act(() => {
      window.dispatchEvent(new Event('compositionend'));
    });

    fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
    expect(screen.getByTestId('command-palette-dialog')).not.toBeNull();
    fireEvent.keyDown(screen.getByLabelText('命令搜索'), { key: 'Escape' });
    expect(screen.queryByTestId('command-palette-dialog')).toBeNull();

    fireEvent.keyDown(window, { key: 'F8' });
    expect(screen.getByTestId('companion-dock')).not.toBeNull();

    fireEvent.keyDown(window, { key: '?' });
    expect(screen.getByTestId('keyboard-help-dialog')).not.toBeNull();
    fireEvent.keyDown(window, { key: '?' });
    expect(screen.queryByTestId('keyboard-help-dialog')).toBeNull();

    fireEvent.keyDown(window, { key: 'f' });
    expect(screen.getByTestId('hint-overlay')).not.toBeNull();
    fireEvent.keyDown(window, { key: 'j' });
    expect(onPickHint).toHaveBeenCalledWith('target-1');
    expect(screen.queryByTestId('hint-overlay')).toBeNull();
  });

  it('supports controlled overlay props and forwards command callbacks', () => {
    const onCloseCommandPalette = vi.fn();
    const onCommand = vi.fn();

    render(
      <OverlayLayer
        commandPaletteOpen={true}
        onCloseCommandPalette={onCloseCommandPalette}
        onCommand={onCommand}
        companionOpen={true}
        keyboardHelpOpen={false}
        hintsOpen={false}
      />,
    );

    expect(screen.getByTestId('command-palette-dialog')).not.toBeNull();
    expect(screen.getByTestId('companion-dock')).not.toBeNull();

    const input = screen.getByLabelText('命令搜索');
    fireEvent.keyDown(input, { key: 'Enter' });

    expect(onCommand).toHaveBeenCalledWith('workbench:media');
    expect(onCloseCommandPalette).toHaveBeenCalledTimes(1);
  });
});
