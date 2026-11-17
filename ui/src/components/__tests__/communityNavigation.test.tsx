// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ExternalLink } from '../ExternalLink';
import { CommunityLinkContext } from '../CommunityLinkContext';
import { EmbeddedWebReader } from '../EmbeddedWebReader';
import PluginWorkspace from '../../workbenches/PluginWorkspace';
import { resolveCommunityLink } from '../../utils/communityLinks';
import { toTextArticle } from '../../hooks/useLiveDomain';
import { App } from '../../App';

afterEach(() => { cleanup(); delete window.__TAURI__; vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function mockReader() {
  const invoke = vi.fn(async (_command: string, _payload?: Record<string, unknown>): Promise<unknown> => ({ ok: true, data: {} }));
  window.__TAURI__ = { core: { invoke: invoke as NonNullable<NonNullable<Window['__TAURI__']>['core']>['invoke'] } };
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ x: 10, y: 20, width: 800, height: 600 } as DOMRect);
  return invoke;
}

describe('Community links', () => {
  it('preserves post, comment and query targets while rejecting deceptive or unsafe hosts', () => {
    const url = 'https://old.reddit.com/r/bicycling/comments/1xna4ty/tips/comment/?sort=new#discussion';
    expect(resolveCommunityLink(url)).toEqual({ site: 'reddit', url });
    expect(resolveCommunityLink('http://linux.do/t/topic/123/4')?.url).toBe('https://linux.do/t/topic/123/4');
    expect(resolveCommunityLink('https://redd.it/abc')?.site).toBe('reddit');
    for (const href of ['https://reddit.com.evil.test/a', 'https://evil.test/reddit.com', 'javascript:alert(1)', 'https://name@reddit.com/a', 'https://reddit.com:123/a']) {
      expect(resolveCommunityLink(href)).toBeNull();
    }
  });

  it('keeps the source URL on curated feed items', () => {
    expect(toTextArticle({ id: 'curated-1', url: 'https://www.reddit.com/comments/abc' })?.url).toBe('https://www.reddit.com/comments/abc');
  });

  it('routes an ordinary click internally and keeps the explicit Chrome action external', async () => {
    const invoke = mockReader();
    const open = vi.fn(() => true);
    render(<CommunityLinkContext.Provider value={open}>
      <ExternalLink href="https://www.reddit.com/comments/abc">原帖</ExternalLink>
      <ExternalLink href="https://www.reddit.com/comments/abc" externalOnly>Chrome</ExternalLink>
    </CommunityLinkContext.Provider>);
    fireEvent.click(screen.getByText('原帖'));
    expect(open).toHaveBeenCalledExactlyOnceWith('https://www.reddit.com/comments/abc');
    expect(invoke).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('Chrome'));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('call', expect.objectContaining({ args: expect.objectContaining({ op: 'capability.call', params: expect.objectContaining({ capability: 'web.embed.v1' }) }) })));
    expect(open).toHaveBeenCalledTimes(1);
  });

  it('opens the requested native page once and preserves browsing position across resize and hide/show', async () => {
    const invoke = mockReader();
    const navigation = { site: 'reddit' as const, url: 'https://www.reddit.com/comments/abc', requestId: 1 };
    const { rerender } = render(<EmbeddedWebReader site="reddit" navigation={navigation} active />);
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('web_reader', expect.objectContaining({ action: 'show', site: 'reddit', url: navigation.url })));
    await act(async () => { window.dispatchEvent(new Event('resize')); });
    rerender(<EmbeddedWebReader site="reddit" navigation={navigation} active={false} />);
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('web_reader', expect.objectContaining({ action: 'hide', site: 'reddit' })));
    rerender(<EmbeddedWebReader site="reddit" navigation={navigation} active />);
    await act(async () => {});
    expect(invoke.mock.calls.filter(([, payload]) => payload?.url)).toHaveLength(1);
    rerender(<EmbeddedWebReader site="reddit" navigation={{ ...navigation, requestId: 2 }} active />);
    await act(async () => {});
    expect(invoke.mock.calls.filter(([, payload]) => payload?.url)).toHaveLength(2);
  });

  it('defaults Reddit to its webpage without fetching the API, and accepts a deep link from list mode', async () => {
    const invoke = mockReader();
    const tab = { tab_id: 'reddit', plugin_id: 'plugin-reddit', title: 'Reddit', domain: 'text' as const,
      ui_schema: { type: 'ActionRow', actions: [{ id: 'fetch_feed', label: '拉取帖子流' }] } };
    const onReturn = vi.fn();
    const { rerender } = render(<PluginWorkspace tab={tab} onReturnToSource={onReturn} />);
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('web_reader', expect.objectContaining({ action: 'show', site: 'reddit' })));
    expect(invoke.mock.calls.every(([command]) => command === 'web_reader')).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: '切换帖子列表' }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('call', expect.anything()));
    rerender(<PluginWorkspace tab={tab} onReturnToSource={onReturn} navigation={{ site: 'reddit', url: 'https://www.reddit.com/comments/next', requestId: 1 }} />);
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('web_reader', expect.objectContaining({ url: 'https://www.reddit.com/comments/next' })));
    fireEvent.click(screen.getByRole('button', { name: '返回来源' }));
    expect(onReturn).toHaveBeenCalledTimes(1);
    rerender(<PluginWorkspace tab={tab} returnToList onReturnToSource={onReturn}
      navigation={{ site: 'reddit', url: 'https://www.reddit.com/comments/next', requestId: 2 }} />);
    fireEvent.click(screen.getByRole('button', { name: '返回帖子列表' }));
    expect(screen.getByRole('button', { name: '内嵌网页' })).toBeTruthy();
    expect(onReturn).toHaveBeenCalledTimes(1);
  });

  it('routes a rendered source link to the installed plugin and returns to its source workbench', async () => {
    const invoke = mockReader();
    const url = 'https://www.reddit.com/r/bicycling/comments/1xna4ty/tips/';
    invoke.mockImplementation(async (_command, payload) => {
      const args = payload?.args as { op?: string } | undefined;
      if (args?.op === 'domain.snapshot') return { ok: true, data: {
        media_sources: [], text_sources: [], book_sources: [], dock_widgets: [],
        workspace_tabs: [
          { tab_id: 'source', plugin_id: 'plugin-test-source', title: '推荐来源', domain: 'text', ui_schema: { type: 'Markdown', content: `[推荐骑行原帖](${url})` } },
          { tab_id: 'reddit', plugin_id: 'plugin-reddit', title: 'Reddit', domain: 'text', ui_schema: {} },
        ],
      } };
      return { ok: true, data: {} };
    });
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: /推荐来源/ }));
    fireEvent.click(await screen.findByRole('link', { name: '推荐骑行原帖' }));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('web_reader', expect.objectContaining({ site: 'reddit', url })));
    fireEvent.click(screen.getByRole('button', { name: '返回来源' }));
    await screen.findByRole('link', { name: '推荐骑行原帖' });
  });
});
