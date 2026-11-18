// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { callCapability } from '../../api';
import { TranslationContext, triggerActiveTranslation, type TranslationRequest } from '../TranslationContext';
import { TranslationPanel } from '../TranslationPanel';
import TextArticleReader from '../../workbenches/panels/TextArticleReader';
import { MediaInspector } from '../../workbenches/panels/MediaInspector';
import { useMediaLyrics } from '../../workbenches/useMediaLyrics';
import { MEDIA_ITEMS, TEXT_ARTICLES } from '../../data/mock';

vi.mock('../../api', async importOriginal => ({ ...await importOriginal<object>(), isTauriAvailable: () => true, callCapability: vi.fn() }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
const noop = () => {};

it('article button preserves paragraph IDs and writes translations back into bilingual reading', () => {
  let request: TranslationRequest | undefined;
  const article = { ...TEXT_ARTICLES[0], blocks: [
    { id: 'p1', kind: 'paragraph' as const, original: 'Hello world' },
    { id: 'code', kind: 'code' as const, original: 'let x = 1' },
  ] };
  const mode = vi.fn();
  render(<TranslationContext.Provider value={value => { request = value; }}><TextArticleReader article={article} bilingualMode="bilingual" activeFocusId=""
    onFocus={noop} onBilingualModeChange={mode} onSendToBabelDoc={noop} onOpenChromeApp={noop} onSelectBlock={noop} /></TranslationContext.Provider>);
  fireEvent.click(screen.getByRole('button', { name: '翻译文章' }));
  expect(request?.segments).toEqual([{ id: 'p1', text: 'Hello world' }]);
  act(() => request?.onTranslated?.([{ id: 'p1', translation: '你好世界' }]));
  expect(screen.getByText('你好世界')).toBeTruthy();
  expect(mode).toHaveBeenCalledWith('bilingual');
});

it.each(['music', 'video'] as const)('%s exposes cue translation and preserves seek timestamps', kind => {
  let request: TranslationRequest | undefined;
  const seek = vi.fn();
  const item = { ...MEDIA_ITEMS[0], kind, lyrics: [{ timeSec: 12.5, original: 'Original line', translation: '' }] };
  render(<TranslationContext.Provider value={value => { request = value; }}><MediaInspector item={item} playing={false} currentSec={0} activeLyricIndex={-1}
    nextSponsorSegment={null} inEphemeralQueue={false} activeFocusId="" onFocus={noop} onTogglePlay={noop} onSeek={seek} onSkipSponsor={noop} onQueueEphemeral={noop} onStatusChange={noop} /></TranslationContext.Provider>);
  fireEvent.click(screen.getByRole('button', { name: kind === 'music' ? '翻译歌词' : '翻译字幕' }));
  expect(request?.segments).toEqual([{ id: '0', text: 'Original line' }]);
  act(() => request?.onTranslated?.([{ id: '0', translation: '逐行译文' }]));
  fireEvent.click(screen.getByText('逐行译文'));
  expect(seek).toHaveBeenCalledWith(12.5);
  request = undefined;
  expect(triggerActiveTranslation()).toBe(true);
  expect((request as TranslationRequest | undefined)?.autoStart).toBe(true);
});

it('YouTube loads original cues without spending translation tokens automatically', async () => {
  vi.mocked(callCapability).mockResolvedValue({ available: true, ok: true, providerPluginId: 'plugin-youtube', error: null, data: { cues: [{ start_ms: 12500, original: 'Video caption', translation: '' }] } });
  const item = { ...MEDIA_ITEMS[0], kind: 'video' as const, sourceId: 'plugin-youtube', id: 'testVideo01' };
  const { result } = renderHook(() => useMediaLyrics(item));
  await waitFor(() => expect(result.current.lines).toHaveLength(1));
  expect(callCapability).toHaveBeenCalledWith(expect.objectContaining({ capability: 'media.youtube.v1', payload: expect.objectContaining({ action: 'fetch_subtitles', bilingual: false }) }));
  expect(result.current.lines[0].timeSec).toBe(12.5);
});

it('translation panel submits independent models and concurrency then updates the source', async () => {
  const onTranslated = vi.fn();
  vi.mocked(callCapability).mockImplementation(async args => {
    const action = args.payload?.action;
    if (action === 'list_models') return { available: true, ok: true, data: { models: [] } } as never;
    if (action === 'translate') return { available: true, ok: true, data: { results: [{ id: 'cue-7', source: 'Hello', translation: '你好' }] } } as never;
    return { available: true, ok: true, data: { translation_gateway_url: '', effective_gateway_url: 'http://127.0.0.1:3425', translation_models: 'provider/translator', translation_concurrency: 3, translation_batch_size: 2 } } as never;
  });
  render(<TranslationPanel open request={{ title: 'Test captions', segments: [{ id: 'cue-7', text: 'Hello' }], onTranslated }} onClose={noop} />);
  await waitFor(() => expect((screen.getByRole('button', { name: '开始翻译' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: '开始翻译' }));
  await waitFor(() => expect(onTranslated).toHaveBeenCalled());
  expect(callCapability).toHaveBeenCalledWith(expect.objectContaining({ capability: 'llm.translate.v1', payload: expect.objectContaining({ action: 'translate', models: ['provider/translator'], concurrency: 3, batch_size: 2, segments: [{ id: 'cue-7', text: 'Hello' }] }) }));
  expect(screen.getByText('你好')).toBeTruthy();
});

it('switches translation backends without reusing gateway model IDs', async () => {
  const config = { translation_backend: 'magpie', translation_models: 'gateway/model', translation_agy_models: 'agy-model', translation_codex_models: 'codex-model', translation_concurrency: 2, translation_batch_size: 1 };
  vi.mocked(callCapability).mockImplementation(async args => {
    if (args.payload?.action === 'list_models') return { ok: true, data: { models: [] } } as never;
    if (args.payload?.action === 'translate') return { ok: true, data: { results: [{ id: '0', source: 'Hello', translation: '你好' }] } } as never;
    return { ok: true, data: args.payload?.action === 'configure' ? args.payload : config } as never;
  });
  render(<TranslationPanel open request={null} onClose={noop} />);
  await waitFor(() => expect((screen.getByRole('button', { name: '保存翻译配置' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.change(screen.getByLabelText('翻译后端'), { target: { value: 'agy' } });
  expect(screen.getByDisplayValue('agy-model')).toBeTruthy();
  fireEvent.change(screen.getByLabelText('待翻译原文'), { target: { value: 'Hello' } });
  fireEvent.click(screen.getByRole('button', { name: '开始翻译' }));
  await waitFor(() => expect(screen.getByText('你好')).toBeTruthy());
  expect(callCapability).toHaveBeenCalledWith(expect.objectContaining({ payload: expect.objectContaining({ backend: 'agy', models: ['agy-model'] }) }));
});

it('renders subtitle translation, native extraction, and save-as-SRT controls for fnOS NAS items', async () => {
  const { MediaInspector } = await import('../../workbenches/panels/MediaInspector');
  const nasItem = {
    ...MEDIA_ITEMS.find(item => item.kind === 'nas')!,
    lyrics: [{ timeSec: 1, original: 'Yes, chef!', translation: '' }],
  };
  render(
    <MediaInspector
      item={nasItem}
      playing={false}
      currentSec={0}
      activeLyricIndex={0}
      nextSponsorSegment={null}
      inEphemeralQueue={false}
      activeFocusId=""
      onFocus={noop}
      onTogglePlay={noop}
      onSeek={noop}
      onSkipSponsor={noop}
      onQueueEphemeral={noop}
      onStatusChange={noop}
    />,
  );
  expect(screen.getByRole('button', { name: '翻译字幕' })).toBeTruthy();
  expect(screen.getByRole('button', { name: '提取原生字幕' })).toBeTruthy();
  expect(screen.getByRole('button', { name: '保存为 SRT' })).toBeTruthy();
});
