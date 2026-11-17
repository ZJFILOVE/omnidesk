// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { callCapability } from '../../api';
import { MineruSettings } from '../MineruSettings';
import { PdfParseButton } from '../PdfParseButton';

vi.mock('../../api', () => ({ callCapability: vi.fn(), isTauriAvailable: () => true, formatRpcError: () => '配置失败' }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });
const config = { mineru_wrapper: '', mineru_python: '', mineru_token_file: '', token_configured: true,
  wrapper_available: true, python_available: true, token_file_available: false };

it('reads only token status, saves typed secret, and clears the password field', async () => {
  vi.mocked(callCapability).mockResolvedValue({ ok: true, available: true, data: config, providerPluginId: 'plugin-pdf-babeldoc', error: null });
  render(<MineruSettings />);
  fireEvent.click(screen.getByText('MinerU 设置 · 账号与解析接口'));
  await screen.findByText('配置已读取。');
  const input = screen.getByLabelText('MinerU API Token') as HTMLInputElement;
  expect(input.type).toBe('password');
  expect(input.value).toBe('');
  fireEvent.change(input, { target: { value: 'test-secret-token' } });
  fireEvent.click(screen.getByRole('button', { name: '保存 MinerU 配置' }));
  await screen.findByText('MinerU 配置已保存；尚未上传 PDF。');
  expect(callCapability).toHaveBeenLastCalledWith(expect.objectContaining({ payload: expect.objectContaining({ action: 'configure_mineru', mineru_api_token: 'test-secret-token' }) }));
  expect(input.value).toBe('');
  expect(screen.queryByText('test-secret-token')).toBeNull();
});

it('uploads only the selected page and refreshes the reader after success', async () => {
  vi.mocked(callCapability).mockResolvedValue({ ok: true, available: true, data: { format: 'content_list', image_count: 2 }, providerPluginId: 'plugin-pdf-babeldoc', error: null });
  const refresh = vi.fn();
  render(<PdfParseButton bookId="pdf-id" page={7} onParsed={refresh} />);
  expect(callCapability).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '上传当前页至 MinerU 解析' }));
  await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
  expect(callCapability).toHaveBeenCalledWith(expect.objectContaining({ payload: { action: 'parse_page', book_id: 'pdf-id', page_number: 7, upload_current_page: true } }));
  expect(screen.getByText('已解析正文与 2 张图片，可点击“翻译当前页”。')).toBeTruthy();
});

it('does not refresh another book when an earlier parse finishes', async () => {
  let finish!: (value: Awaited<ReturnType<typeof callCapability>>) => void;
  vi.mocked(callCapability).mockImplementation(() => new Promise(resolve => { finish = resolve; }) as ReturnType<typeof callCapability>);
  const refresh = vi.fn();
  const view = render(<PdfParseButton bookId="first" page={1} onParsed={refresh} />);
  fireEvent.click(screen.getByRole('button', { name: '上传当前页至 MinerU 解析' }));
  view.rerender(<PdfParseButton bookId="second" page={1} onParsed={refresh} />);
  finish({ ok: true, available: true, data: { image_count: 1 }, providerPluginId: 'plugin-pdf-babeldoc', error: null });
  await waitFor(() => expect(screen.getByRole('button', { name: '上传当前页至 MinerU 解析' })).toBeTruthy());
  expect(refresh).not.toHaveBeenCalled();
});
