// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { callCapability } from '../../api';
import { BookMirrorSettings } from '../BookMirrorSettings';
vi.mock('../../api', async original => ({ ...await original<object>(), isTauriAvailable: () => true, callCapability: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
it('saves each provider mirror independently and refreshes search only after success', async () => {
  const onChanged = vi.fn();
  vi.mocked(callCapability).mockImplementation(async args => ({ ok: true, data: { mirror_url: args.payload?.mirror_url || 'https://z-library.sk', mirrors: [{ url: 'https://z-lib.gd', label: 'z-lib.gd' }] } }) as never);
  render(<BookMirrorSettings onChanged={onChanged} />);
  fireEvent.click(screen.getByRole('button', { name: '镜像与自定义地址' }));
  await waitFor(() => expect((screen.getByLabelText('Z-Library 镜像地址') as HTMLInputElement).value).toBe('https://z-library.sk'));
  fireEvent.change(screen.getByLabelText('Z-Library 镜像'), { target: { value: 'https://z-lib.gd' } });
  fireEvent.click(screen.getByRole('button', { name: '应用 Z-Library 镜像' }));
  await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
  expect(callCapability).toHaveBeenCalledWith(expect.objectContaining({ providerPluginId: 'plugin-zlib', payload: { action: 'configure_mirror', mirror_url: 'https://z-lib.gd' } }));
});
