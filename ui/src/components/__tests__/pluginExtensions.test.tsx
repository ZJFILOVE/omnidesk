// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from '../../App';
import { CompanionDock } from '../CompanionDock';
import { DeclarativePanel } from '../DeclarativePanel';
import PluginWorkspace from '../../workbenches/PluginWorkspace';

afterEach(() => {
  cleanup();
  delete window.__TAURI__;
  vi.restoreAllMocks();
});

const TAB_SCHEMA = {
  type: 'Section',
  title: 'Bilibili 收藏同步',
  description: '声明式插件页签',
  children: [
    {
      type: 'Markdown',
      content: '**热门内容**已从插件载入。',
    },
    {
      type: 'KeyValue',
      items: [
        { key: 'status', label: '连接状态', value: 'online' },
        { key: 'count', label: '待同步', value: 3 },
      ],
    },
    {
      type: 'Form',
      id: 'filters',
      fields: [
        { name: 'query', label: '关键词', type: 'string', default: 'rust' },
        { name: 'limit', label: '数量', type: 'int', default: 5 },
        { name: 'enabled', label: '启用过滤', type: 'bool', default: true },
      ],
    },
    {
      type: 'ActionRow',
      actions: [
        { id: 'search', label: '同步收藏', variant: 'primary' },
        { id: 'reset', label: '重置' },
      ],
    },
    {
      type: 'List',
      title: '待同步条目',
      items: [
        { id: 'bv-1', title: '第一支视频', subtitle: '稍后观看' },
        { id: 'bv-2', title: '第二支视频', subtitle: '收藏夹' },
      ],
    },
  ],
};

const DOCK_SCHEMA = {
  type: 'Section',
  title: 'Apple Music',
  children: [
    { type: 'Markdown', content: '当前曲目歌词已载入。' },
    {
      type: 'ActionRow',
      actions: [{ id: 'sync', label: '同步歌词', variant: 'primary' }],
    },
  ],
};

function installTauriMock(
  uiActionSchema: unknown = { type: 'Markdown', content: '动作已执行' },
  uiActionExtra: Record<string, unknown> = {},
) {
  const invoke = vi.fn(async (cmd: string, payload?: { args?: Record<string, unknown> }) => {
    if (cmd === 'call_capability') {
      const capability = payload?.args?.capability;
      if (capability === 'llm.router.v1') {
        return {
          ok: true,
          provider_plugin_id: 'plugin-magpie-agent',
          data: { gateway_running: true, primary_member: 'group/omnidesk-active' },
          error: '',
        };
      }
      return { ok: true, data: { sessions: [] }, error: '' };
    }
    if (cmd === 'call') {
      const op = payload?.args?.op;
      if (op === 'domain.snapshot') {
        return {
          ok: true,
          provider_plugin_id: '',
          data: {
            media_sources: [],
            text_sources: [],
            book_sources: [],
            workspace_tabs: [
              {
                tab_id: 'bili-main',
                plugin_id: 'plugin-bilibili',
                title: 'Bilibili',
                domain: 'media',
                badge: 'B站',
                ui_schema: TAB_SCHEMA,
              },
            ],
            dock_widgets: [
              {
                widget_id: 'music-lyrics',
                plugin_id: 'plugin-music',
                title: 'Apple Music 歌词',
                supported_domains: ['media'],
                ui_schema: DOCK_SCHEMA,
              },
            ],
          },
          error: '',
        };
      }
      if (op === 'ui.action') {
        return {
          ok: true,
          provider_plugin_id: 'plugin-bilibili',
          data: { ...uiActionExtra, ui_schema: uiActionSchema },
          error: '',
        };
      }
    }
    return { ok: true, data: {}, error: '' };
  });
  window.__TAURI__ = {
    core: {
      invoke: invoke as unknown as NonNullable<NonNullable<Window['__TAURI__']>['core']>['invoke'],
    },
  };
  return invoke;
}

describe('Declarative plugin UI', () => {
  it('opens a feed article and returns to the accumulated list', async () => {
    const invoke = vi.fn(async (_cmd: string, payload?: { args?: { params?: { action_id?: string } } }) => ({ ok: true, data: { ok: true, result:
      payload?.args?.params?.action_id === 'fetch_article'
        ? { article: { title: '主题详情', paragraphs: [{ original: '可阅读的真实正文' }] } }
        : { items: [{ id: 'post-1', title: '打开这个主题', url: 'https://example.org/post' }] },
    } }));
    window.__TAURI__ = { core: { invoke: invoke as NonNullable<NonNullable<Window['__TAURI__']>['core']>['invoke'] } };
    render(<PluginWorkspace tab={{ tab_id: 'topics', plugin_id: 'plugin-zhihu', title: '知乎', domain: 'text', ui_schema: { type: 'ActionRow', actions: [{ id: 'fetch_feed', label: '刷新栏目' }] } }} />);
    fireEvent.click(await screen.findByRole('button', { name: '打开这个主题' }));
    await screen.findByText('可阅读的真实正文');
    fireEvent.click(screen.getByRole('button', { name: '返回列表' }));
    await screen.findByRole('button', { name: '打开这个主题' });
    expect(screen.queryByText('可阅读的真实正文')).toBeNull();
  });

  it('appends plugin pages using the successful query and cursor, and retries without losing entries', async () => {
    let fail = true;
    const requests: Record<string, unknown>[] = [];
    const invoke = vi.fn(async (_cmd: string, payload?: { args?: { op?: string; params?: { state?: Record<string, unknown> } } }) => {
      const state = payload?.args?.params?.state ?? {};
      requests.push(state);
      if (state.page === 2 && fail) return { ok: true, data: { ok: false, error: '请求失败' } };
      return { ok: true, data: { ok: true, result: {
        items: state.page === 2 ? [{ id: 'one', title: '第一批' }, { id: 'two', title: '第二批' }] : [{ id: 'one', title: '第一批' }],
        next_cursor: state.page === 2 ? '' : 'cursor-2', has_more: state.page !== 2,
      } } };
    });
    window.__TAURI__ = { core: { invoke: invoke as NonNullable<NonNullable<Window['__TAURI__']>['core']>['invoke'] } };
    render(<PluginWorkspace tab={{ tab_id: 'x', plugin_id: 'plugin-x', title: 'X', domain: 'text',
      ui_schema: { type: 'ActionRow', actions: [{ id: 'fetch_feed', label: '刷新栏目' }] },
    }} />);
    await screen.findByText('第一批');
    fireEvent.click(screen.getByRole('button', { name: '加载下一页' }));
    fireEvent.click(await screen.findByRole('button', { name: '重试下一页' }));
    await screen.findByRole('button', { name: '重试下一页' });
    expect(screen.getByText('第一批')).not.toBeNull();
    fail = false;
    fireEvent.click(screen.getByRole('button', { name: '重试下一页' }));
    await screen.findByText('第二批');
    expect(screen.getAllByText('第一批')).toHaveLength(1);
    expect(requests.slice(1)).toEqual(Array(3).fill({ page: 2, cursor: 'cursor-2' }));
    expect(screen.queryByRole('button', { name: '加载下一页' })).toBeNull();
    expect(screen.getByText(/没有更多内容了/)).not.toBeNull();
  });

  it('auto-loads a text feed and renders readable entries alongside the control schema', async () => {
    const invoke = installTauriMock(
      {
        type: 'Section',
        title: '知乎控制台',
        children: [
          {
            type: 'ActionRow',
            actions: [{ id: 'fetch_feed', label: '刷新栏目', variant: 'primary' }],
          },
        ],
      },
      {
        ok: true,
        result: {
          ok: true,
          count: 1,
          items: [
            {
              id: 'zhihu-1',
              title: '如何评价新版 OmniDesk？',
              excerpt: '这是一条可见的知乎热榜内容。',
              source_name: '知乎热榜',
              author: '测试作者',
              comment_count: 12,
              score: 345,
              tags: ['编程'],
              url: 'https://www.zhihu.com/question/1',
            },
          ],
        },
      },
    );

    render(
      <PluginWorkspace
        tab={{
          tab_id: 'zhihu',
          plugin_id: 'plugin-zhihu',
          title: '知乎',
          domain: 'text',
          badge: 'ZHIHU',
          ui_schema: {
            type: 'Section',
            title: '知乎控制台',
            children: [
              {
                type: 'ActionRow',
                actions: [{ id: 'fetch_feed', label: '刷新栏目', variant: 'primary' }],
              },
            ],
          },
        }}
      />,
    );

    expect(await screen.findByText('如何评价新版 OmniDesk？')).not.toBeNull();
    expect(screen.getByText('这是一条可见的知乎热榜内容。')).not.toBeNull();
    expect(screen.getByText('12')).not.toBeNull();
    expect(screen.getByText('345')).not.toBeNull();
    expect(screen.getByTestId('plugin-result')).not.toBeNull();
    expect(invoke).toHaveBeenCalledWith('call', {
      args: expect.objectContaining({
        op: 'ui.action',
        params: expect.objectContaining({
          kind: 'workspace_tab',
          spec_id: 'zhihu',
          action_id: 'fetch_feed',
          plugin_id: 'plugin-zhihu',
        }),
      }),
    });
  });

  it('auto-loads plugin actions outside the text domain', async () => {
    const invoke = installTauriMock(
      { type: 'Markdown', content: '媒体条目已加载' },
      {
        ok: true,
        result: {
          ok: true,
          count: 1,
          items: [{ id: 'movie-1', title: '测试电影', source_name: 'Radarr' }],
        },
      },
    );

    render(
      <PluginWorkspace
        tab={{
          tab_id: 'arr-imdb',
          plugin_id: 'plugin-arr-imdb',
          title: 'IMDb · Arr',
          domain: 'media',
          badge: 'ARR',
          ui_schema: {
            type: 'Section',
            children: [
              {
                type: 'ActionRow',
                actions: [{ id: 'fetch_items', label: '拉取条目', variant: 'primary' }],
              },
            ],
          },
        }}
      />,
    );

    expect(await screen.findByText('测试电影')).not.toBeNull();
    expect(invoke).toHaveBeenCalledWith('call', {
      args: expect.objectContaining({
        op: 'ui.action',
        params: expect.objectContaining({
          kind: 'workspace_tab',
          spec_id: 'arr-imdb',
          action_id: 'fetch_items',
          plugin_id: 'plugin-arr-imdb',
        }),
      }),
    });
  });

  it('renders Section, Markdown, KeyValue, Form, ActionRow, and List with form state', () => {
    const onAction = vi.fn();
    render(
      <DeclarativePanel
        schema={TAB_SCHEMA}
        focusPrefix="test-plugin"
        onAction={onAction}
      />,
    );

    expect(screen.getByText('Bilibili 收藏同步')).not.toBeNull();
    expect(screen.getByText('热门内容')).not.toBeNull();
    expect(screen.getByText('连接状态')).not.toBeNull();
    expect(screen.getByText('online')).not.toBeNull();
    expect(screen.getByText('第一支视频')).not.toBeNull();
    expect(screen.getByText('第二支视频')).not.toBeNull();

    fireEvent.change(screen.getByLabelText('关键词'), { target: { value: 'compiler' } });
    fireEvent.change(screen.getByLabelText('数量'), { target: { value: '8' } });
    fireEvent.click(screen.getByLabelText('启用过滤'));
    fireEvent.click(screen.getByRole('button', { name: '同步收藏' }));

    expect(onAction).toHaveBeenCalledWith('search', {
      query: 'compiler',
      limit: 8,
      enabled: false,
    });
  });

  it('keeps unknown custom schema actionable through status/refresh controls', () => {
    const onAction = vi.fn();
    render(
      <DeclarativePanel
        schema={{ kind: 'lyrics_dock', title: '专用歌词视图' }}
        focusPrefix="unknown-view"
        onAction={onAction}
      />,
    );

    expect(screen.getByText('专用歌词视图')).not.toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '读取状态' }));
    expect(onAction).toHaveBeenCalledWith('status', {});
  });

  it('renders QR images and submits select option values instead of labels', () => {
    const onAction = vi.fn();
    render(
      <DeclarativePanel
        schema={{
          type: 'Section',
          title: '扫码登录',
          children: [
            {
              type: 'Image',
              src: 'data:image/png;base64,iVBORw0KGgo=',
              alt: 'Bilibili 登录二维码',
              width: 180,
            },
            {
              type: 'Form',
              id: 'codec-form',
              fields: [
                {
                  name: 'preferred_codec',
                  label: '视频编码',
                  type: 'select',
                  default: 'hevc',
                  options: [
                    { value: 'hevc', label: 'HEVC (推荐)' },
                    { value: 'av1', label: 'AV1' },
                  ],
                },
              ],
            },
            {
              type: 'ActionRow',
              actions: [{ id: 'set_settings', label: '保存设置' }],
            },
          ],
        }}
        focusPrefix="codec-test"
        onAction={onAction}
      />,
    );

    expect(screen.getByAltText('Bilibili 登录二维码').getAttribute('src')).toBe(
      'data:image/png;base64,iVBORw0KGgo=',
    );
    expect(screen.getByRole('option', { name: 'HEVC (推荐)' })).not.toBeNull();
    fireEvent.change(screen.getByLabelText('视频编码'), { target: { value: 'av1' } });
    fireEvent.click(screen.getByRole('button', { name: '保存设置' }));
    expect(onAction).toHaveBeenCalledWith('set_settings', {
      preferred_codec: 'av1',
    });
  });

  it('loads plugin workspace tabs from domain.snapshot and applies ui_schema returned by ui.action', async () => {
    const invoke = installTauriMock({ type: 'Markdown', content: '已同步 3 条收藏' });
    render(<App />);

    await waitFor(() => {
      expect(
        document.querySelector('[data-focus-id="nav-workbench-plugin:plugin-bilibili:bili-main"]'),
      ).not.toBeNull();
    });
    expect(
      document.querySelector('[data-focus-id="nav-workbench-plugin:plugin-bilibili:bili-main"]')
        ?.textContent,
    ).toContain('Bilibili');

    fireEvent.click(
      document.querySelector(
        '[data-focus-id="nav-workbench-plugin:plugin-bilibili:bili-main"]',
      ) as HTMLElement,
    );

    expect(await screen.findByTestId('plugin-workspace')).not.toBeNull();
    expect(screen.getByText('Bilibili 收藏同步')).not.toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '同步收藏' }));

    await waitFor(() => {
      expect(screen.getByText('已同步 3 条收藏')).not.toBeNull();
    });
    expect(invoke).toHaveBeenCalledWith('call', {
      args: expect.objectContaining({
        op: 'ui.action',
        params: expect.objectContaining({
          kind: 'workspace_tab',
          spec_id: 'bili-main',
          action_id: 'search',
          plugin_id: 'plugin-bilibili',
          state: { query: 'rust', limit: 5, enabled: true },
        }),
      }),
    });
  });

  it('renders matching F8 dock widgets and dispatches dock_widget actions', async () => {
    const invoke = installTauriMock({ type: 'Markdown', content: '歌词已同步' });
    render(
      <CompanionDock
        open={true}
        onClose={vi.fn()}
        activeWorkbench="media"
        domain="media"
        dockWidgets={[
          {
            widget_id: 'music-lyrics',
            plugin_id: 'plugin-music',
            title: 'Apple Music 歌词',
            supported_domains: ['media'],
            ui_schema: DOCK_SCHEMA,
          },
          {
            widget_id: 'book-only',
            plugin_id: 'plugin-ebook',
            title: '仅书本组件',
            supported_domains: ['book'],
            ui_schema: { type: 'Markdown', content: '不应出现' },
          },
        ]}
      />,
    );

    fireEvent.click(screen.getByRole('tab', { name: /组件/ }));
    expect(screen.getByTestId('plugin-dock-widgets')).not.toBeNull();
    expect(screen.getAllByTestId('plugin-dock-widget')).toHaveLength(1);
    expect(screen.getByText('Apple Music 歌词')).not.toBeNull();
    expect(screen.queryByText('仅书本组件')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: '同步歌词' }));
    await waitFor(() => {
      expect(screen.getByText('歌词已同步')).not.toBeNull();
    });
    expect(invoke).toHaveBeenCalledWith('call', {
      args: expect.objectContaining({
        op: 'ui.action',
        params: expect.objectContaining({
          kind: 'dock_widget',
          spec_id: 'music-lyrics',
          action_id: 'sync',
          plugin_id: 'plugin-music',
        }),
      }),
    });
  });
});
