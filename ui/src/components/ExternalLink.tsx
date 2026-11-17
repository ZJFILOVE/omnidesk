import { useContext, useState, type AnchorHTMLAttributes, type MouseEvent } from 'react';
import { callCapability, formatRpcError, isTauriAvailable } from '../api';
import { CommunityLinkContext } from './CommunityLinkContext';

/** Tauri WebViews do not reliably open target=_blank; use the host's browser bridge. */
export function ExternalLink({ href, children, externalOnly = false, ...props }: AnchorHTMLAttributes<HTMLAnchorElement> & { externalOnly?: boolean }) {
  const openCommunity = useContext(CommunityLinkContext);
  const [error, setError] = useState<string | null>(null);
  const [opening, setOpening] = useState(false);
  const open = async (event: MouseEvent<HTMLAnchorElement>) => {
    event.stopPropagation();
    if (!externalOnly && href && event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey && openCommunity(href)) {
      event.preventDefault();
      return;
    }
    if (!isTauriAvailable() || !href || !/^https?:\/\//i.test(href)) return;
    event.preventDefault();
    if (opening) return;
    setOpening(true);
    setError(null);
    try {
      const result = await callCapability({ capability: 'web.embed.v1', providerPluginId: 'plugin-chrome-web',
        payload: { action: 'open', url: href }, timeoutMs: 30000 });
      if (!result.ok) setError(`打开链接失败：${formatRpcError(result.error)}`);
    } catch (err) {
      setError(`打开链接失败：${String(err)}`);
    } finally { setOpening(false); }
  };
  return <>
    <a {...props} href={href} target="_blank" rel="noopener noreferrer" aria-busy={opening}
      onClick={(event) => void open(event)} onAuxClick={(event) => { if (event.button === 1) void open(event); }}>{children}</a>
    {error ? <span role="alert" className="ml-2 text-[13px] text-[var(--status-err-fg)]">{error}</span> : null}
  </>;
}
