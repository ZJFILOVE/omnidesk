// @vitest-environment jsdom
import { act, cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { listenTauriEvent, mpvEmbedSetViewport } from '../../api';
import { MpvCanvas } from '../MpvCanvas';
vi.mock('../../api', () => ({ MPV_FRAME_CHANNEL: 'frame', isTauriEventsAvailable: () => true, listenTauriEvent: vi.fn(), mpvEmbedSetViewport: vi.fn(async () => ({ ok: true })) }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it('requests source cadence and serializes JPEG decoding while keeping the newest pending frame', async () => {
  let receive: (frame: unknown) => void = () => {};
  vi.mocked(listenTauriEvent).mockImplementation(async (_, callback) => { receive = callback; return () => {}; });
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ x: 0, y: 0, width: 1280, height: 720 } as DOMRect);
  const draw = vi.fn();
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: draw } as unknown as CanvasRenderingContext2D);
  const pending: Array<(bitmap: ImageBitmap) => void> = [];
  const decode = vi.fn(() => new Promise<ImageBitmap>(resolve => pending.push(resolve)));
  vi.stubGlobal('createImageBitmap', decode);
  render(<MpvCanvas active fps={59.94} />);
  await waitFor(() => expect(mpvEmbedSetViewport).toHaveBeenCalledWith(1280, 720, 60));
  const frame = (seq: number) => ({ seq, width: 1280, height: 720, mime: 'image/jpeg', data: 'AA==' });
  act(() => { receive(frame(1)); receive(frame(2)); receive(frame(3)); });
  expect(decode).toHaveBeenCalledTimes(1);
  const first = { close: vi.fn() } as unknown as ImageBitmap;
  await act(async () => pending[0](first));
  expect(decode).toHaveBeenCalledTimes(2);
  const newest = { close: vi.fn() } as unknown as ImageBitmap;
  await act(async () => pending[1](newest));
  expect(draw.mock.calls.map(call => call[0])).toEqual([first, newest]);
  expect(first.close).toHaveBeenCalled();
  act(() => receive(frame(2)));
  expect(decode).toHaveBeenCalledTimes(2);
});
