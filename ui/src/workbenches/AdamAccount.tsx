import { useState } from 'react';
import { callCapability } from '../api';

export function AdamAccount() {
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [code, setCode] = useState('');
  const [status, setStatus] = useState('');
  const [busy, setBusy] = useState(false);
  async function run(action: string) {
    setBusy(true);
    try {
      const payload = action === 'authenticate' ? { action, email, password } : action === 'challenge' ? { action, code } : { action };
      const res = await callCapability<Record<string, unknown>>({ capability: 'media.adam.v1', providerPluginId: 'plugin-music', payload, timeoutMs: 45_000 });
      if (!res.ok) { setStatus(res.error.message); return; }
      const snapshot = res.data;
      const state = typeof snapshot.state === 'object' && snapshot.state !== null ? snapshot.state as Record<string, unknown> : {};
      setStatus(action === 'sync_library' ? '资料库同步已提交，请稍后刷新。' : [state.authentication, state.session, state.fairplay, snapshot.message].filter(v => typeof v === 'string').join(' · ') || '请求已完成，请点击检查状态查看登录进度。');
    } catch { setStatus('Adam 服务连接失败，请重试'); }
    finally { setPassword(''); setCode(''); setBusy(false); }
  }
  const cls = 'min-h-11 rounded-lg border border-[var(--border-line)] bg-[var(--bg-surface)] px-3 py-2 text-sm focus-visible:outline-2 focus-visible:outline-[var(--domain-media)]';
  return <div>
    <button className={cls} aria-expanded={open} onClick={() => { setOpen(v => !v); if (!open) void run('status'); }}>Apple Music 账号</button>
    {open && <div className="my-4 w-full max-w-lg rounded-xl border border-[var(--border-line)] bg-[var(--bg-surface)] p-5" aria-label="Apple Music 登录">
      <h3 className="mb-3 font-semibold">Apple ID 与双重认证</h3>
      <p className="mb-3 text-sm text-[var(--text-muted)]">复用 Adam 登录会话。已有会话时可直接播放。</p>
      <form className="grid gap-3" onSubmit={e => { e.preventDefault(); void run('authenticate'); }}>
        <label className="grid gap-1 text-sm">Apple ID<input className={cls} type="email" autoComplete="username" value={email} onChange={e => setEmail(e.target.value)} required /></label>
        <label className="grid gap-1 text-sm">密码<input className={cls} type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} required /></label>
        <button className={cls} disabled={busy}>登录</button>
      </form>
      <form className="mt-3 flex flex-wrap items-end gap-2" onSubmit={e => { e.preventDefault(); void run('challenge'); }}>
        <label className="grid gap-1 text-sm">验证码<input className={cls} inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" value={code} onChange={e => setCode(e.target.value)} required /></label><button className={cls} disabled={busy}>提交验证码</button>
      </form>
      <div className="mt-3 flex flex-wrap gap-2"><button className={cls} disabled={busy} onClick={() => void run('resend')}>重发验证码</button><button className={cls} disabled={busy} onClick={() => void run('status')}>检查状态</button><button className={cls} disabled={busy} onClick={() => void run('sync_library')}>同步资料库</button></div>
      <p role="status" className="mt-3 break-words text-sm">{busy ? '正在处理…' : status}</p>
    </div>}
  </div>;
}
