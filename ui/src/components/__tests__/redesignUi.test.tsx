// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from '../../App';
import { MediaWorkbench } from '../../workbenches/MediaWorkbench';
import { PluginStore } from '../../workbenches/PluginStore';
import { TextWorkbench } from '../../workbenches/TextWorkbench';
import { CompanionDock } from '../CompanionDock';
import { StateView } from '../StateView';

const originalInnerWidth = window.innerWidth;
const originalInnerHeight = window.innerHeight;

afterEach(() => {
  cleanup();
  delete window.__TAURI__;
  vi.restoreAllMocks();
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

describe('UI redesign: focus states, 4-state views, offline banner, F8 layout & keyboard help', () => {
  it('applies visible focus classes (omni-focus-ring, omni-focus-row, data-focused) and moves via h/j/k/l', async () => {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (
      this: HTMLElement,
    ) {
      const id = this.dataset?.focusId ?? '';
      if (id === 'media:card:media-yt-attention') {
        return { left: 900, top: 180, width: 280, height: 64, right: 1180, bottom: 244, x: 900, y: 180, toJSON: () => ({}) } as DOMRect;
      }
      if (id === 'media:card:media-bili-wayland') {
        return { left: 900, top: 260, width: 280, height: 64, right: 1180, bottom: 324, x: 900, y: 260, toJSON: () => ({}) } as DOMRect;
      }
      if (id.startsWith('nav-workbench-')) {
        return { left: 16, top: 120, width: 120, height: 36, right: 136, bottom: 156, x: 16, y: 120, toJSON: () => ({}) } as DOMRect;
      }
      return { left: 220, top: 160, width: 420, height: 48, right: 640, bottom: 208, x: 220, y: 160, toJSON: () => ({}) } as DOMRect;
    });

    render(<App />);

    const firstCard = document.querySelector('[data-focus-id="media:card:media-yt-attention"]') as HTMLElement;
    const secondCard = document.querySelector('[data-focus-id="media:card:media-bili-wayland"]') as HTMLElement;
    expect(firstCard).not.toBeNull();
    expect(secondCard).not.toBeNull();

    await waitFor(() => {
      expect(firstCard.getAttribute('data-spatial-focused')).toBe('true');
    });
    expect(firstCard.className).toContain('omni-focus-ring');
    expect(firstCard.className).toContain('omni-focus-row');
    expect(firstCard.getAttribute('data-focused')).toBe('true');

    fireEvent.keyDown(window, { key: 'j' });

    await waitFor(() => {
      expect(secondCard.getAttribute('data-spatial-focused')).toBe('true');
    });
    expect(secondCard.className).toContain('omni-focus-ring');
    expect(secondCard.getAttribute('data-focused')).toBe('true');
    expect(firstCard.className).not.toContain('omni-focus-ring');
  });

  it('renders unified offline preview banner outside Tauri across shell and workbenches', () => {
    delete window.__TAURI__;
    render(<App />);

    const banner = screen.getByTestId('offline-preview-banner');
    expect(banner.getAttribute('data-state-mode')).toBe('offline');
    expect(banner.getAttribute('role')).toBe('status');
    expect(banner.textContent).toContain('离线预览：数据为示例');
    expect(screen.getByTestId('shell-runtime-status').textContent).toBe('离线预览');
  });

  it('renders loading, empty, error, and offline StateView modes and supports empty search recovery', () => {
    const onEmptyReset = vi.fn();
    const { rerender } = render(
      <StateView
        mode="loading"
        testId="state-probe"
        title="加载中"
        description="正在同步最新内容"
      />,
    );
    const loadingEl = screen.getByTestId('state-probe');
    expect(loadingEl.getAttribute('data-state-mode')).toBe('loading');
    expect(loadingEl.getAttribute('aria-busy')).toBe('true');
    expect(loadingEl.getAttribute('role')).toBe('status');

    rerender(
      <StateView
        mode="error"
        testId="state-probe"
        title="加载失败"
        error={{ code: 'rpc_timeout', message: 'Core 未响应' }}
      />,
    );
    const errorEl = screen.getByTestId('state-probe');
    expect(errorEl.getAttribute('data-state-mode')).toBe('error');
    expect(errorEl.getAttribute('role')).toBe('alert');
    expect(errorEl.textContent).toContain('[rpc_timeout] Core 未响应');

    rerender(
      <StateView
        mode="empty"
        testId="state-probe"
        action={{ label: '重置条件', focusId: 'probe:reset', onActivate: onEmptyReset }}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: '重置条件' }));
    expect(onEmptyReset).toHaveBeenCalledTimes(1);

    cleanup();

    // Real workbench empty search recovery in MediaWorkbench, TextWorkbench, PluginStore
    render(<MediaWorkbench />);
    fireEvent.change(screen.getByLabelText('搜索媒体条目'), { target: { value: 'nonexistent-xyz' } });
    expect(screen.getByText('没有匹配的媒体条目')).not.toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '重置筛选条件' }));
    expect(screen.queryByText('没有匹配的媒体条目')).toBeNull();
    cleanup();

    render(<TextWorkbench />);
    fireEvent.change(screen.getByLabelText('搜索文章'), { target: { value: 'nonexistent-xyz' } });
    expect(screen.getByText('没有匹配的文章')).not.toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '查看全部文章' }));
    expect(screen.queryByText('没有匹配的文章')).toBeNull();
    cleanup();

    render(<PluginStore />);
    fireEvent.change(screen.getByLabelText('搜索插件'), { target: { value: 'nonexistent-xyz' } });
    expect(screen.getByText('没有匹配的插件')).not.toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '显示全部插件' }));
    expect(screen.queryByText('没有匹配的插件')).toBeNull();
  });

  it('switches F8 dock layout between side and bottom at 1280x800 vs narrow viewport without obscuring main stage', () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: 1280 });
    Object.defineProperty(window, 'innerHeight', { configurable: true, writable: true, value: 800 });

    const { container } = render(<App />);
    const shellRoot = container.querySelector('[data-theme]') as HTMLElement;
    expect(shellRoot.getAttribute('data-dock-open')).toBe('false');

    // Press F8 -> side dock opens and reserves right padding
    fireEvent.keyDown(window, { key: 'F8' });
    const dock = screen.getByTestId('companion-dock');
    expect(dock.getAttribute('data-dock-layout')).toBe('side');
    expect(shellRoot.getAttribute('data-dock-open')).toBe('true');
    expect(shellRoot.getAttribute('data-dock-layout')).toBe('side');
    expect(shellRoot.style.paddingRight).toContain('--dock-side-width');

    // Resize to narrow aspect ratio -> bottom dock + bottom padding
    act(() => {
      Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: 900 });
      Object.defineProperty(window, 'innerHeight', { configurable: true, writable: true, value: 900 });
      window.dispatchEvent(new Event('resize'));
    });

    expect(dock.getAttribute('data-dock-layout')).toBe('bottom');
    expect(shellRoot.getAttribute('data-dock-layout')).toBe('bottom');
    expect(shellRoot.style.paddingBottom).toContain('--dock-bottom-height');

    // Press F8 again -> dock closes cleanly
    fireEvent.keyDown(window, { key: 'F8' });
    expect(screen.queryByTestId('companion-dock')).toBeNull();
    expect(shellRoot.getAttribute('data-dock-open')).toBe('false');

    // Standalone CompanionDock also supports manual side/bottom toggle
    cleanup();
    Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: 1440 });
    Object.defineProperty(window, 'innerHeight', { configurable: true, writable: true, value: 900 });
    render(<CompanionDock open={true} onClose={vi.fn()} />);
    const standaloneDock = screen.getByTestId('companion-dock');
    expect(standaloneDock.getAttribute('data-dock-layout')).toBe('side');
    fireEvent.click(screen.getByRole('button', { name: '切换为底部分栏' }));
    expect(standaloneDock.getAttribute('data-dock-layout')).toBe('bottom');
  });

  it('keeps KeyboardHelp accessible via header button and ? / Escape shortcuts', () => {
    render(<App />);

    expect(screen.queryByTestId('keyboard-help-dialog')).toBeNull();
    fireEvent.click(document.querySelector('[data-focus-id="shell-help"]') as HTMLElement);

    const dialog = screen.getByTestId('keyboard-help-dialog');
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    expect(screen.getByText('全键盘操作与快捷键总览')).not.toBeNull();
    expect(screen.getByText('2D 空间焦点引擎')).not.toBeNull();
    expect(screen.getByText('中文输入法 IME 隔离')).not.toBeNull();

    fireEvent.click(screen.getByRole('button', { name: '关闭快捷键帮助' }));
    expect(screen.queryByTestId('keyboard-help-dialog')).toBeNull();
  });
});
