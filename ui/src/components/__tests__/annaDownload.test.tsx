// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { callCapability } from '../../api';
import { AnnasDownloadPanel } from '../AnnasDownloadPanel';
import { BOOK_SEARCH_CATALOG } from '../../data/mock';
vi.mock('../../api', async original => ({ ...await original<object>(), callCapability: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
const md5 = 'a'.repeat(32);
const entry = { ...BOOK_SEARCH_CATALOG[0], itemId: md5, providerPluginId: 'plugin-annas-archive' };
const slow = `/slow_download/${md5}/2/3`;
const detail = { title: 'Anna file', author: 'Author', format: 'pdf', size: '2 MB', md5, description: 'A file, not a format collection', member_api_available: false,
  download_options: [{ kind: 'slow', label: 'Slow partner #2', download_token: slow }, { kind: 'external', label: 'External library', download_token: 'https://library.example/file' }] };
it('requires a real source, keeps waiting visible and opens selected verification page', async () => {
  const completed = vi.fn();
  vi.mocked(callCapability).mockImplementation(async args => {
    if (args.payload?.action === 'detail') return { ok: true, data: detail } as never;
    if (args.payload?.action === 'download') return { ok: false, data: { wait_seconds: 30 }, error: { code: 'waiting', message: '慢速下载正在排队' } } as never;
    return { ok: true, data: {} } as never;
  });
  render(<AnnasDownloadPanel entry={entry} onClose={() => {}} onDownloaded={completed} />);
  await screen.findByText('Anna file');
  expect((screen.getByRole('button', { name: '下载并校验文件' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByLabelText(/Slow partner #2/));
  fireEvent.click(screen.getByRole('button', { name: '下载并校验文件' }));
  await screen.findByText(/请等待 30 秒/);
  expect(completed).not.toHaveBeenCalled();
  expect((screen.getByRole('button', { name: '下载并校验文件' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button', { name: '在 Chrome 打开 / 完成验证' }));
  await waitFor(() => expect(callCapability).toHaveBeenCalledWith(expect.objectContaining({ payload: expect.objectContaining({ action: 'open_browser', item_id: md5, download_token: slow }) })));
});
it('does not auto-download an external source and reports verified download success', async () => {
  const completed = vi.fn();
  vi.mocked(callCapability).mockImplementation(async args => args.payload?.action === 'detail' ? { ok: true, data: detail } as never : { ok: true, data: { file_path: '/books/anna.pdf' } } as never);
  render(<AnnasDownloadPanel entry={entry} onClose={() => {}} onDownloaded={completed} />);
  await screen.findByText('Anna file');
  fireEvent.click(screen.getByLabelText(/External library/));
  expect((screen.getByRole('button', { name: '下载并校验文件' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByLabelText(/Slow partner #2/));
  fireEvent.click(screen.getByRole('button', { name: '下载并校验文件' }));
  await waitFor(() => expect(completed).toHaveBeenCalledWith('/books/anna.pdf'));
});
