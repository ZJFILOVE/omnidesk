import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  callCapability,
  formatRpcError,
  isTauriAvailable,
  type RpcErrorInfo,
} from '../api';

export type AuthRequirement = 'required' | 'optional' | 'none';
export type AuthStateKind =
  | 'authenticated'
  | 'guest_cookie_only'
  | 'optional_guest'
  | 'missing_required'
  | 'no_auth_needed';

export interface AuthProviderDefinition {
  id: string;
  name: string;
  pluginId: string;
  capability: string;
  statusAction: string;
  loginAction?: string;
  syncAction?: string;
  logoutAction?: string;
  requirement: AuthRequirement;
  detail: string;
}

export interface AuthProviderState {
  definition: AuthProviderDefinition;
  kind: AuthStateKind;
  raw: Record<string, unknown>;
  error: RpcErrorInfo | null;
}

export interface AuthCenterState {
  providers: AuthProviderState[];
  loading: boolean;
  actionBusy: string | null;
  actionMessage: string;
  refresh: () => Promise<void>;
  runAction: (providerId: string, action: 'login' | 'sync' | 'logout') => Promise<void>;
}

export const AUTH_PROVIDERS: readonly AuthProviderDefinition[] = [
  {
    id: 'zhihu',
    name: '知乎',
    pluginId: 'plugin-zhihu',
    capability: 'zhihu.auth.v1',
    statusAction: 'status',
    loginAction: 'login',
    syncAction: 'sync_cookies',
    logoutAction: 'logout',
    requirement: 'required',
    detail: 'Chrome 会话 / SecretVault Cookie',
  },
  {
    id: 'reddit',
    name: 'Reddit',
    pluginId: 'plugin-reddit',
    capability: 'reddit.auth.v1',
    statusAction: 'status',
    loginAction: 'login',
    syncAction: 'sync_cookies',
    logoutAction: 'logout',
    requirement: 'required',
    detail: 'OAuth / Chrome Cookie 桥接',
  },
  {
    id: 'v2ex',
    name: 'V2EX',
    pluginId: 'plugin-v2ex',
    capability: 'text.source.v1',
    statusAction: 'auth_status',
    loginAction: 'login',
    syncAction: 'sync_cookies',
    logoutAction: 'logout',
    requirement: 'required',
    detail: 'PAT 优先，其次 Chrome / Vault Cookie',
  },
  {
    id: 'linuxdo',
    name: 'Linux.do',
    pluginId: 'plugin-linuxdo',
    capability: 'text.source.v1',
    statusAction: 'auth_status',
    loginAction: 'open_login',
    syncAction: 'sync_cookies',
    logoutAction: 'logout',
    requirement: 'required',
    detail: 'Discourse 会话与 Chrome Cookie',
  },
  {
    id: 'shuiyuan',
    name: '水源社区',
    pluginId: 'plugin-shuiyuan',
    capability: 'text.source.v1',
    statusAction: 'auth_status',
    loginAction: 'open_login',
    syncAction: 'sync_cookies',
    logoutAction: 'logout',
    requirement: 'required',
    detail: 'SJTU 统一身份 / Chrome Cookie',
  },
  {
    id: 'x',
    name: 'X / Twitter',
    pluginId: 'plugin-x',
    capability: 'text.source.v1',
    statusAction: 'auth_status',
    loginAction: 'open_login',
    syncAction: 'sync_cookies',
    logoutAction: 'logout',
    requirement: 'required',
    detail: '登录窗口与 Cookie 同步',
  },
  {
    id: 'bilibili',
    name: '哔哩哔哩',
    pluginId: 'plugin-bilibili',
    capability: 'bilibili.media.v1',
    statusAction: 'status',
    loginAction: 'qr_login_start',
    logoutAction: 'logout',
    requirement: 'optional',
    detail: '扫码登录 / SecretVault Cookie',
  },
  {
    id: 'zlib',
    name: 'Z-Library',
    pluginId: 'plugin-zlib',
    capability: 'book.search.v1',
    statusAction: 'status',
    requirement: 'optional',
    detail: '可匿名检索，下载通常需要账号或 Cookie',
  },
  {
    id: 'annas',
    name: "Anna's Archive",
    pluginId: 'plugin-annas-archive',
    capability: 'book.search.v1',
    statusAction: 'status',
    requirement: 'optional',
    detail: 'API Key / Chrome Cookie',
  },
  {
    id: 'music',
    name: 'Apple Music',
    pluginId: 'plugin-music',
    capability: 'media.playlist.v1',
    statusAction: 'status',
    requirement: 'optional',
    detail: '歌单写入授权与国区可播性',
  },
  {
    id: 'youtube',
    name: 'YouTube',
    pluginId: 'plugin-youtube',
    capability: 'media.youtube.v1',
    statusAction: 'auth_status',
    requirement: 'optional',
    detail: '公开内容可免登录，订阅需要 OAuth',
  },
  {
    id: 'chrome',
    name: '受管 Chrome',
    pluginId: 'plugin-chrome-web',
    capability: 'web.embed.v1',
    statusAction: 'status',
    requirement: 'none',
    detail: '登录窗口与 Cookie 抓取桥接',
  },
] as const;

function rawStatus(data: Record<string, unknown>): Record<string, unknown> {
  const inner = data.auth;
  return inner && typeof inner === 'object' && !Array.isArray(inner)
    ? { ...data, ...(inner as Record<string, unknown>) }
    : data;
}

function truthyFlag(raw: Record<string, unknown>, ...keys: string[]): boolean {
  return keys.some((key) => raw[key] === true);
}

function cookieBridgeHint(raw: Record<string, unknown>): boolean {
  const source = String(raw.cookie_source ?? raw.chrome_cookie_source ?? '').toLowerCase();
  return (
    source.includes('web.cookies') ||
    source.includes('chrome') ||
    truthyFlag(
      raw,
      'chrome_bridge_available',
      'chrome_cookie_configured',
      'cookie_configured',
      'has_cookie',
      'has_cookie_file',
      'has_z_c0',
      'has_d_c0',
      'has_a2',
      'has_v2ex_tab',
      'has_pb3_session',
    )
  );
}

function classify(definition: AuthProviderDefinition, raw: Record<string, unknown>): AuthStateKind {
  if (definition.requirement === 'none') return 'no_auth_needed';
  const authenticated =
    truthyFlag(
      raw,
      'authenticated',
      'is_authenticated',
      'logged_in',
      'has_write_auth',
      'has_live_apple_music_auth',
      'oauth_configured',
      'has_api_token',
      'api_key_configured',
      'has_api_key',
    ) ||
    ['authenticated', 'logged_in', 'confirmed', 'logged_in_web'].includes(
      String(raw.status ?? '').toLowerCase(),
    );
  if (authenticated) return 'authenticated';
  if (cookieBridgeHint(raw)) return 'guest_cookie_only';
  return definition.requirement === 'required' ? 'missing_required' : 'optional_guest';
}

async function fetchProviderState(
  definition: AuthProviderDefinition,
): Promise<AuthProviderState> {
  const result = await callCapability<Record<string, unknown>>({
    capability: definition.capability,
    providerPluginId: definition.pluginId,
    payload: { action: definition.statusAction },
    timeoutMs: 15000,
  });
  if (!result.available || !result.ok) {
    return {
      definition,
      kind: definition.requirement === 'required' ? 'missing_required' : 'optional_guest',
      raw: {},
      error: result.available
        ? result.error
        : { code: 'tauri_unavailable', message: '离线预览不可读取登录态' },
    };
  }
  const raw = rawStatus(result.data ?? {});
  return {
    definition,
    kind: classify(definition, raw),
    raw,
    error: null,
  };
}

export function useAuthCenter(enabled = isTauriAvailable()): AuthCenterState {
  const [providers, setProviders] = useState<AuthProviderState[]>([]);
  const [loading, setLoading] = useState(false);
  const [actionBusy, setActionBusy] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState('');
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const refresh = useCallback(async () => {
    if (!enabled) {
      setProviders(
        AUTH_PROVIDERS.map((definition) => ({
          definition,
          kind: definition.requirement === 'none' ? 'no_auth_needed' : 'optional_guest',
          raw: {},
          error: { code: 'tauri_unavailable', message: '离线预览不可读取登录态' },
        })),
      );
      return;
    }
    setLoading(true);
    const next = await Promise.all(AUTH_PROVIDERS.map(fetchProviderState));
    if (!mountedRef.current) return;
    setProviders(next);
    setLoading(false);
  }, [enabled]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const runAction = useCallback(
    async (providerId: string, action: 'login' | 'sync' | 'logout') => {
      const provider = providers.find((entry) => entry.definition.id === providerId);
      if (!provider) return;
      if (!enabled) {
        setActionMessage('离线预览模式不能执行登录或 Cookie 同步');
        return;
      }
      const actionId =
        action === 'login'
          ? provider.definition.loginAction
          : action === 'sync'
            ? provider.definition.syncAction
            : provider.definition.logoutAction;
      if (!actionId) {
        setActionMessage(`${provider.definition.name} 未声明 ${action} 动作`);
        return;
      }
      const busyKey = `${providerId}:${action}`;
      setActionBusy(busyKey);
      setActionMessage('');
      const result = await callCapability<Record<string, unknown>>({
        capability: provider.definition.capability,
        providerPluginId: provider.definition.pluginId,
        payload: { action: actionId },
        timeoutMs: 120000,
      });
      if (!mountedRef.current) return;
      setActionBusy(null);
      if (!result.available) {
        setActionMessage('Core 控制通道不可用');
        return;
      }
      if (!result.ok) {
        setActionMessage(`${provider.definition.name}：${formatRpcError(result.error)}`);
        return;
      }
      const message =
        action === 'login'
          ? `${provider.definition.name} 登录窗口已拉起，完成后请同步 Cookie`
          : action === 'sync'
            ? `${provider.definition.name} Cookie 已从 Chrome 同步`
            : `${provider.definition.name} 登录态已清除`;
      setActionMessage(message);
      await refresh();
    },
    [enabled, providers, refresh],
  );

  const sortedProviders = useMemo(() => {
    const rank: Record<AuthStateKind, number> = {
      missing_required: 0,
      guest_cookie_only: 1,
      optional_guest: 2,
      authenticated: 3,
      no_auth_needed: 4,
    };
    return [...providers].sort((a, b) => rank[a.kind] - rank[b.kind]);
  }, [providers]);

  return {
    providers: sortedProviders,
    loading,
    actionBusy,
    actionMessage,
    refresh,
    runAction,
  };
}

export function authKindLabel(kind: AuthStateKind): string {
  switch (kind) {
    case 'authenticated':
      return '真实登录';
    case 'guest_cookie_only':
      return '匿名 Cookie 警告';
    case 'optional_guest':
      return '可选访客';
    case 'missing_required':
      return '必需登录缺失';
    case 'no_auth_needed':
      return '无需认证';
  }
}
