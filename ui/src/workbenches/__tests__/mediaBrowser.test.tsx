// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MediaBrowser } from '../MediaBrowser';
import { MediaCover } from '../../components/MediaCover';
import { toMediaItem } from '../../hooks/useLiveDomain';
import { callCapability } from '../../api';
vi.mock('../../api', async () => ({ ...await vi.importActual('../../api'), callCapability: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
const raw = { id: 'BV1xK4y1Z7aa', source_id: 'plugin-bilibili', title: '视频标题', creator: '作者', cover_url: 'http://i1.hdslb.com/a.jpg', play_url: 'https://www.bilibili.com/video/BV1xK4y1Z7aa', extra: { mid: 42 }, duration_sec: 60 };
describe('media browsing', () => {
  it('separates playback from creator navigation and shows text dynamics', async () => {
    vi.mocked(callCapability).mockResolvedValue({ available: true, ok: true, error: null, providerPluginId: 'plugin-bilibili', data: { cards: [{ id: 'd1', name: '作者', mid: '42', published: '今天', text: '一条图文动态', images: [] }], has_more: false } });
    const play = vi.fn();
    render(<MediaBrowser sourceId="plugin-bilibili" category="recommend" items={[toMediaItem(raw)!]} onPlay={play} />);
    fireEvent.click(screen.getByRole('button', { name: '播放 视频标题' }));
    expect(play).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole('button', { name: 'UP / 频道：作者' }));
    await screen.findByText('一条图文动态');
    expect(vi.mocked(callCapability).mock.calls[0][0].payload).toEqual({ action: 'space', mid: '42', page: 1 });
    expect(play).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByText('返回'));
    expect(screen.getByText('视频标题')).toBeTruthy();
  });
  it('opens album contents rather than playing collection URLs', async () => {
    vi.mocked(callCapability).mockResolvedValue({ available: true, ok: true, error: null, providerPluginId: 'plugin-music', data: { items: [{ ...raw, id: 'track', source_id: 'plugin-music', title: '专辑歌曲', kind: 'music' }] } });
    const play = vi.fn();
    const album = toMediaItem({ ...raw, id: 'album', source_id: 'plugin-music', title: '我的专辑', kind: 'music', extra: { collection: { id: '123', type: 'albums' } } })!;
    render(<MediaBrowser sourceId="plugin-music" category="recommendations" items={[album]} onPlay={play} />);
    fireEvent.click(screen.getByRole('button', { name: '打开 我的专辑' }));
    await screen.findByText('专辑歌曲');
    expect(play).not.toHaveBeenCalled();
    expect(vi.mocked(callCapability).mock.calls[0][0].payload).toMatchObject({ action: 'collection', id: '123', type: 'albums' });
  });
  it('surfaces social feed failures and retries', async () => {
    vi.mocked(callCapability).mockResolvedValueOnce({ available: true, ok: false, providerPluginId: "plugin-bilibili", error: { code: 'unauthorized', message: '请登录 B站' } }).mockResolvedValueOnce({ available: true, ok: true, error: null, providerPluginId: 'plugin-bilibili', data: { users: [{ id: '42', name: '关注的UP' }] } });
    render(<MediaBrowser sourceId="plugin-bilibili" category="followings" items={[]} onPlay={vi.fn()} />);
    await screen.findByRole('alert');
    fireEvent.click(screen.getByText('重试'));
    await screen.findByText('关注的UP');
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  });
  it('upgrades HTTP Bilibili covers to pass the desktop CSP', () => {
    const { container } = render(<MediaCover url="http://i1.hdslb.com/a.jpg" />);
    expect(container.querySelector('img')?.src).toBe('https://i1.hdslb.com/a.jpg');
    fireEvent.error(container.querySelector('img')!);
    expect(screen.getByText('封面暂不可用')).toBeTruthy();
    expect(container.querySelector('button')).toBeNull();
  });
});
