// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { scrollReader } from '../../utils/readerScroll';
import { MediaInspector } from '../../workbenches/panels/MediaInspector';
import { MEDIA_ITEMS } from '../../data/mock';
import { WorkbenchLayout } from '../WorkbenchLayout';
import { MediaCover } from '../MediaCover';
import { ArticleImage } from '../ArticleImage';
import { callCapability } from '../../api';

vi.mock('../../api', async original => ({ ...await original<object>(), isTauriAvailable: () => true, callCapability: vi.fn() }));
const storage = new Map<string, string>();
Object.defineProperty(window, 'localStorage', { configurable: true, value: {
  getItem: (key: string) => storage.get(key) ?? null,
  setItem: (key: string, value: string) => storage.set(key, value),
  clear: () => storage.clear(),
} });
afterEach(() => { cleanup(); vi.restoreAllMocks(); window.localStorage.clear(); });

it('scrolls a long plugin article while spatial focus remains on the toolbar', () => {
  render(<div data-testid="plugin-workspace"><button data-spatial-focused="true">刷新</button><div data-reader-scroll="true" data-testid="reader" /></div>);
  const reader = screen.getByTestId('reader');
  Object.defineProperties(reader, { clientHeight: { value: 600 }, scrollHeight: { value: 3000 } });
  expect(scrollReader(new KeyboardEvent('keydown', { key: 'PageDown' }))).toBe(true);
  expect(reader.scrollTop).toBe(510);
  scrollReader(new KeyboardEvent('keydown', { key: 'End' }));
  expect(reader.scrollTop).toBe(2400);
  scrollReader(new KeyboardEvent('keydown', { key: 'k' }));
  expect(reader.scrollTop).toBe(2336);
});

it('centers each current lyric in its pane without stealing playback control focus', () => {
  const noop = vi.fn();
  const item = { ...MEDIA_ITEMS[0], kind: 'music' as const, lyrics: [
    { timeSec: 2, original: 'First cue', translation: '' }, { timeSec: 12, original: 'Second cue', translation: '' },
  ] };
  const props = { item, playing: true, currentSec: 2, activeLyricIndex: 0, nextSponsorSegment: null, inEphemeralQueue: false,
    activeFocusId: 'media:player:seek', onFocus: noop, onTogglePlay: noop, onSeek: noop, onSkipSponsor: noop, onQueueEphemeral: noop, onStatusChange: noop };
  const { rerender } = render(<MediaInspector {...props} />);
  const pane = screen.getByRole('region', { name: '同步歌词' });
  const scrollTo = vi.fn();
  pane.scrollTo = scrollTo;
  const slider = screen.getByRole('slider', { name: '播放时间轴' });
  slider.focus(); noop.mockClear();
  rerender(<MediaInspector {...props} currentSec={12} activeLyricIndex={1} />);
  expect(scrollTo).toHaveBeenCalledTimes(1);
  expect(document.activeElement).toBe(slider);
  expect(noop).not.toHaveBeenCalled();
  expect(screen.getByText('Second cue').closest('button')?.getAttribute('aria-pressed')).toBe('true');
});

it('resizes the comments pane with keyboard and restores its saved width', () => {
  const panel = <WorkbenchLayout domain="text" title="Reader" mainPane="Article" rightPane="Comments" resizableRightPane />;
  const { unmount } = render(panel);
  Object.defineProperty(screen.getByRole('separator').parentElement, 'clientWidth', { value: 1200 });
  fireEvent.keyDown(screen.getByRole('separator'), { key: 'ArrowLeft' });
  expect(screen.getByRole('separator').getAttribute('aria-valuenow')).toBe('352');
  unmount(); render(panel);
  expect(screen.getByRole('separator').getAttribute('aria-valuenow')).toBe('352');
});

it('loads missing daily music artwork through Adam by song ID', async () => {
  vi.mocked(callCapability).mockResolvedValue({ ok: true, data: { data_url: 'data:image/jpeg;base64,/9j/' } } as never);
  const { container } = render(<MediaCover item={{ ...MEDIA_ITEMS[0], appleMusicSongId: 'cover-test-123' }} />);
  await waitFor(() => expect(container.querySelector('img')?.src).toBe('data:image/jpeg;base64,/9j/'));
  expect(callCapability).toHaveBeenCalledWith(expect.objectContaining({ providerPluginId: 'plugin-music', payload: { action: 'cover', item_id: 'cover-test-123' } }));
});

it('recovers failed Shuiyuan images using the authenticated plugin', async () => {
  vi.mocked(callCapability).mockResolvedValue({ ok: true, data: { data_url: 'data:image/png;base64,aGVsbG8=' } } as never);
  const url = 'https://shuiyuan.sjtu.edu.cn/uploads/default/original/1X/test.png';
  render(<ArticleImage url={url} />);
  fireEvent.error(screen.getByRole('img'));
  await waitFor(() => expect(screen.getByRole('img').getAttribute('src')).toBe('data:image/png;base64,aGVsbG8='));
  expect(callCapability).toHaveBeenCalledWith(expect.objectContaining({ providerPluginId: 'plugin-shuiyuan', payload: { action: 'image', url } }));
});
