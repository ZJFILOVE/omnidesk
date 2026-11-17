import {
  AlertTriangle,
  Chrome,
  LoaderCircle,
  LogIn,
  LogOut,
  RefreshCw,
  ShieldCheck,
  X,
} from 'lucide-react';
import {
  authKindLabel,
  type AuthCenterState,
  type AuthProviderState,
  type AuthStateKind,
} from '../hooks/useAuthCenter';

function kindTone(kind: AuthStateKind): { bg: string; fg: string } {
  switch (kind) {
    case 'authenticated':
      return { bg: 'var(--status-ok-bg)', fg: 'var(--status-ok-fg)' };
    case 'guest_cookie_only':
    case 'optional_guest':
      return { bg: 'var(--status-warn-bg)', fg: 'var(--status-warn-fg)' };
    case 'missing_required':
      return { bg: 'var(--status-err-bg)', fg: 'var(--status-err-fg)' };
    case 'no_auth_needed':
      return { bg: 'var(--bg-soft)', fg: 'var(--text-muted)' };
  }
}

function safeDetail(raw: Record<string, unknown>): string {
  const value = raw.cookie_source ?? raw.source_used ?? raw.chrome_cookie_source;
  if (typeof value !== 'string') return '';
  const trimmed = value.trim();
  if (!trimmed || trimmed.length > 80 || trimmed.includes('=') || trimmed.includes(';')) return '';
  return trimmed;
}

function ProviderRow({
  provider,
  state,
}: {
  provider: AuthProviderState;
  state: AuthCenterState;
}) {
  const tone = kindTone(provider.kind);
  const source = safeDetail(provider.raw);
  const busy = (action: string) =>
    state.actionBusy === `${provider.definition.id}:${action}`;
  const definition = provider.definition;
  return (
    <article className="rounded-[12px] border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-[15.5px] font-semibold">{definition.name}</h3>
            <span
              className="rounded-full px-2 py-0.5 text-[12.5px] font-medium"
              style={{ backgroundColor: tone.bg, color: tone.fg }}
            >
              {authKindLabel(provider.kind)}
            </span>
            <span className="font-mono text-[12.5px] text-[var(--text-subtle)]">
              {definition.pluginId}
            </span>
          </div>
          <p className="mt-1 text-[13.5px] text-[var(--text-muted)]">{definition.detail}</p>
          {source ? (
            <p className="mt-1 font-mono text-[12.5px] text-[var(--text-subtle)]">
              Cookie 来源：{source}
            </p>
          ) : null}
          {provider.error ? (
            <p className="mt-1.5 text-[13px] text-[var(--status-err-fg)]">
              [{provider.error.code}] {provider.error.message}
            </p>
          ) : null}
          {provider.kind === 'guest_cookie_only' ? (
            <p className="mt-1.5 flex items-center gap-1.5 text-[13px] text-[var(--status-warn-fg)]">
              <AlertTriangle className="h-3.5 w-3.5" />
              当前只有匿名 Cookie，不等同于真实登录；受限内容仍可能失败。
            </p>
          ) : null}
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          {definition.loginAction ? (
            <button
              type="button"
              onClick={() => void state.runAction(definition.id, 'login')}
              disabled={state.actionBusy !== null}
              className="inline-flex cursor-pointer items-center gap-1.5 rounded-[8px] bg-[var(--bg-soft)] px-2.5 py-1.5 text-[13px] font-medium hover:brightness-[0.98] disabled:opacity-50"
            >
              {busy('login') ? (
                <LoaderCircle className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <LogIn className="h-3.5 w-3.5" />
              )}
              登录
            </button>
          ) : null}
          {definition.syncAction ? (
            <button
              type="button"
              onClick={() => void state.runAction(definition.id, 'sync')}
              disabled={state.actionBusy !== null}
              className="inline-flex cursor-pointer items-center gap-1.5 rounded-[8px] border border-[var(--border-line)] px-2.5 py-1.5 text-[13px] font-medium hover:bg-[var(--hover-fill)] disabled:opacity-50"
            >
              {busy('sync') ? (
                <LoaderCircle className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Chrome className="h-3.5 w-3.5" />
              )}
              从 Chrome 抓 Cookie
            </button>
          ) : null}
          {definition.logoutAction ? (
            <button
              type="button"
              onClick={() => void state.runAction(definition.id, 'logout')}
              disabled={state.actionBusy !== null}
              className="inline-flex cursor-pointer items-center gap-1.5 rounded-[8px] px-2.5 py-1.5 text-[13px] text-[var(--text-muted)] hover:bg-[var(--hover-fill)] hover:text-[var(--status-err-fg)] disabled:opacity-50"
            >
              {busy('logout') ? (
                <LoaderCircle className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <LogOut className="h-3.5 w-3.5" />
              )}
              退出
            </button>
          ) : null}
        </div>
      </div>
    </article>
  );
}

export interface AuthCenterPanelProps {
  open: boolean;
  onClose: () => void;
  state: AuthCenterState;
}

export function AuthCenterPanel({ open, onClose, state }: AuthCenterPanelProps) {
  if (!open) return null;
  const counts = {
    authenticated: state.providers.filter((p) => p.kind === 'authenticated').length,
    warning: state.providers.filter(
      (p) => p.kind === 'guest_cookie_only' || p.kind === 'optional_guest',
    ).length,
    missing: state.providers.filter((p) => p.kind === 'missing_required').length,
  };

  return (
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-[var(--backdrop-scrim)] p-4"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-label="统一认证中心"
        data-testid="auth-center-panel"
        className="omni-theme-surface flex max-h-[84vh] w-full max-w-4xl flex-col overflow-hidden rounded-[18px] bg-[var(--bg-elevated)] text-[var(--text-primary)]"
        style={{ boxShadow: 'var(--shadow-elevated)' }}
      >
        <header className="flex shrink-0 items-start justify-between gap-3 border-b border-[var(--border-subtle)] px-5 py-4">
          <div>
            <h2 className="flex items-center gap-2 text-[19px] font-semibold tracking-tight">
              <ShieldCheck className="h-5 w-5 text-[var(--domain-core)]" />
              统一认证中心
            </h2>
            <p className="mt-1 text-[13.5px] text-[var(--text-muted)]">
              聚合各插件真实登录态；只展示脱敏状态，不读取或显示 Cookie/Token 原文。
            </p>
          </div>
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={() => void state.refresh()}
              className="inline-flex cursor-pointer items-center gap-1.5 rounded-[8px] border border-[var(--border-line)] bg-[var(--bg-surface)] px-2.5 py-1.5 text-[13.5px] text-[var(--text-muted)]"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${state.loading ? 'animate-spin' : ''}`} />
              重新检查
            </button>
            <button
              type="button"
              onClick={onClose}
              aria-label="关闭统一认证中心"
              className="inline-flex h-8 w-8 cursor-pointer items-center justify-center rounded-[8px] text-[var(--text-muted)] hover:bg-[var(--hover-fill)]"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </header>

        <div className="grid grid-cols-3 gap-2 border-b border-[var(--border-subtle)] px-5 py-3">
          <div className="rounded-[9px] bg-[var(--status-ok-bg)] px-3 py-2 text-[var(--status-ok-fg)]">
            <div className="font-mono text-[19px] font-semibold">{counts.authenticated}</div>
            <div className="text-[12.5px]">真实登录</div>
          </div>
          <div className="rounded-[9px] bg-[var(--status-warn-bg)] px-3 py-2 text-[var(--status-warn-fg)]">
            <div className="font-mono text-[19px] font-semibold">{counts.warning}</div>
            <div className="text-[12.5px]">匿名 Cookie / 可选访客</div>
          </div>
          <div className="rounded-[9px] bg-[var(--status-err-bg)] px-3 py-2 text-[var(--status-err-fg)]">
            <div className="font-mono text-[19px] font-semibold">{counts.missing}</div>
            <div className="text-[12.5px]">必需登录缺失</div>
          </div>
        </div>

        {state.actionMessage ? (
          <div className="mx-5 mt-3 rounded-[9px] bg-[var(--bg-soft)] px-3 py-2 text-[13.5px] text-[var(--text-secondary)]">
            {state.actionMessage}
          </div>
        ) : null}
        {state.loading && state.providers.length === 0 ? (
          <div className="flex flex-1 items-center justify-center gap-2 py-16 text-[14px] text-[var(--text-muted)]">
            <LoaderCircle className="h-4 w-4 animate-spin" /> 正在检查各插件登录态…
          </div>
        ) : (
          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
            <div className="grid grid-cols-1 gap-2.5 xl:grid-cols-2">
              {state.providers.map((provider) => (
                <ProviderRow key={provider.definition.id} provider={provider} state={state} />
              ))}
            </div>
          </div>
        )}
      </section>
    </div>
  );
}

export default AuthCenterPanel;
