import { useEffect, useRef, useState } from 'react';
import { callCapability, formatRpcError, isTauriAvailable } from '../api';
import { MineruSettings } from './MineruSettings';

export function PdfParseButton({ bookId, page, onParsed }: { bookId: string; page: number; onParsed: () => void }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const version = useRef(0);
  useEffect(() => { version.current += 1; setBusy(false); setMessage(''); return () => { version.current += 1; }; }, [bookId, page]);
  const parse = async () => {
    if (busy) return;
    const current = version.current;
    setBusy(true); setMessage(`正在解析第 ${page} 页；仅上传此页，完成后可翻译或交给 Agent 阅读。`);
    try {
      const result = await callCapability<{ format: string; image_count: number }>({ capability: 'book.bilingual_pdf.v1',
        providerPluginId: 'plugin-pdf-babeldoc', timeoutMs: 1100000,
        payload: { action: 'parse_page', book_id: bookId, page_number: page, upload_current_page: true } });
      if (current !== version.current) return;
      if (!result.ok) { setMessage(formatRpcError(result.error)); return; }
      setMessage(`已解析正文与 ${result.data.image_count} 张图片，可点击“翻译当前页”。`);
      onParsed();
    } catch (error) { if (current === version.current) setMessage(String(error)); }
    finally { if (current === version.current) setBusy(false); }
  };
  return <div className="flex w-full flex-wrap items-center gap-2">
    <button type="button" disabled={busy || !isTauriAvailable()} onClick={() => void parse()}
      className="min-h-11 rounded-lg border border-[var(--border-line)] px-3 text-sm disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-[var(--focus-ring)]">
      {busy ? 'MinerU 解析中…' : '上传当前页至 MinerU 解析'}
    </button>
    <span role="status" aria-live="polite" className="max-w-xl text-sm text-[var(--text-muted)]">{message}</span>
    <MineruSettings />
  </div>;
}
