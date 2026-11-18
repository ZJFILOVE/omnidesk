import { callCapability, formatRpcError, invokeDomain } from '../api';
import type { LyricLine, MediaItem } from '../data/mock';

interface SubtitleCandidate {
  id?: string;
  provider?: string;
  title?: string;
  language?: string;
  filename?: string;
}

interface SubtitleDownloadFile {
  path?: string;
  filename?: string;
}

export type SubtitleOutputMode = 'bilingual' | 'translated' | 'original';

function basename(path: string): string {
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] || path;
}

function dirname(path: string): string {
  const idx = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'));
  return idx > 0 ? path.slice(0, idx) : '';
}

export async function runSubHunterPipeline(
  item: MediaItem,
  options: {
    outputMode?: SubtitleOutputMode;
    onProgress?: (message: string) => void;
  } = {},
): Promise<{ ok: boolean; message: string; outputPath?: string }> {
  const outputMode = options.outputMode ?? 'bilingual';
  const onProgress = options.onProgress;
  const title = item.title.trim();
  if (!title) {
    return { ok: false, message: '当前媒体缺少片名，无法检索字幕' };
  }

  onProgress?.('SubHunter 正在检索字幕…');
  const mediaPath = item.nasMeta?.path?.trim();
  const outputDir = mediaPath ? dirname(mediaPath) : '';
  const search = await callCapability<{ results?: SubtitleCandidate[] }>({
    capability: 'subtitle.fetch.v1',
    payload: {
      action: 'search',
      title,
      ...(mediaPath ? { filename: basename(mediaPath) } : {}),
      timeout_sec: 60,
    },
    timeoutMs: 90_000,
  });
  if (!search.available) {
    return { ok: false, message: '离线预览模式不可调用 subtitle.fetch.v1' };
  }
  if (!search.ok) {
    return { ok: false, message: `字幕检索失败 ${formatRpcError(search.error)}` };
  }

  const results = Array.isArray(search.data.results) ? search.data.results : [];
  if (!results.length) {
    return { ok: false, message: `未找到「${title}」的字幕候选` };
  }
  const candidate =
    results.find((entry) => /双|bilingual/i.test(entry.language ?? '')) ??
    results.find((entry) => /eng|english|英/i.test(entry.language ?? '')) ??
    results[0];
  if (!candidate.id || !candidate.provider) {
    return { ok: false, message: '字幕候选缺少 provider/id，无法下载' };
  }

  onProgress?.(`找到 ${results.length} 条候选，正在下载「${candidate.title ?? title}」…`);
  const download = await callCapability<{ files?: SubtitleDownloadFile[] }>({
    capability: 'subtitle.fetch.v1',
    payload: {
      action: 'download',
      provider: candidate.provider,
      id: candidate.id,
      language: candidate.language,
      filename: candidate.filename,
      ...(outputDir ? { output_dir: outputDir } : {}),
      timeout_sec: 120,
    },
    timeoutMs: 180_000,
  });
  if (!download.available) {
    return { ok: false, message: '离线预览模式不可调用 subtitle.fetch.v1' };
  }
  if (!download.ok) {
    return { ok: false, message: `字幕下载失败 ${formatRpcError(download.error)}` };
  }
  const downloadedPath = download.data.files?.[0]?.path;
  if (!downloadedPath) {
    return { ok: false, message: '字幕下载完成，但没有返回可用的本地路径' };
  }

  let translatedPath = downloadedPath;
  let translateNote = '';
  const language = candidate.language ?? '';
  const languageText = `${language} ${candidate.title ?? ''}`;
  const alreadyBilingual =
    /双|bilingual|中英/i.test(languageText) ||
    (/zh|chs|cht/i.test(language) && /en|eng|英/i.test(language));
  const alreadyChinese = /zh|中|简|繁|chs|cht/i.test(languageText);
  if (outputMode === 'original') {
    translateNote = '仅保留原字幕';
  } else if (alreadyBilingual && outputMode === 'bilingual') {
    translateNote = '已使用现成双语字幕，跳过翻译';
  } else if (alreadyChinese) {
    translateNote = '已使用中文字幕，跳过翻译';
  } else {
    onProgress?.(
      outputMode === 'bilingual'
        ? '字幕已下载，正在生成双语字幕…'
        : '字幕已下载，正在生成中文译文…',
    );
    const translate = await callCapability<{
      output_path?: string;
      translated_count?: number;
      skipped?: boolean;
    }>({
      capability: 'subtitle.translate.v1',
      payload: {
        action: 'translate',
        subtitle_path: downloadedPath,
        target_lang: 'zh-CN',
        output_mode: outputMode,
        timeout_sec: 900,
      },
      timeoutMs: 900_000,
    });
    if (!translate.available) {
      translateNote = '当前运行时不支持字幕翻译，已保留原字幕';
    } else if (!translate.ok) {
      translateNote = `字幕翻译失败（${formatRpcError(translate.error)}），已保留原字幕`;
    } else if (translate.data.output_path) {
      translatedPath = translate.data.output_path;
    }
  }

  if (!mediaPath) {
    const suffix = translateNote ? `；${translateNote}` : '';
    return {
      ok: true,
      message: `字幕已就绪：${basename(translatedPath)}${suffix}（缺少视频路径，未做时间轴对齐）`,
      outputPath: translatedPath,
    };
  }

  onProgress?.('正在用 ffsubsync 对齐字幕时间轴…');
  const sync = await callCapability<{ output_path?: string; skipped?: boolean }>({
    capability: 'subtitle.sync.v1',
    payload: {
      action: 'sync',
      subtitle_path: translatedPath,
      reference_path: mediaPath,
      timeout_sec: 900,
    },
    timeoutMs: 960_000,
  });
  const suffix = translateNote ? `；${translateNote}` : '';
  if (!sync.available) {
    return {
      ok: true,
      message: `字幕已生成：${basename(translatedPath)}${suffix}；运行时离线，未做时间轴对齐`,
      outputPath: translatedPath,
    };
  }
  if (!sync.ok) {
    return {
      ok: false,
      message: `字幕已生成：${basename(translatedPath)}${suffix}；时间轴对齐失败 ${formatRpcError(sync.error)}`,
      outputPath: translatedPath,
    };
  }
  const finalPath = sync.data.output_path ?? translatedPath;
  return {
    ok: true,
    message: `已完成：${basename(finalPath)}${suffix}`,
    outputPath: finalPath,
  };
}

export async function runMediaInspectorCapability(statusMsg: string): Promise<string> {
  if (statusMsg.includes('FunASR')) {
    const res = await callCapability({
      capability: 'speech.asr.v1',
      payload: { action: 'status', probe_runtime: false },
    });
    if (!res.available) {
      return '离线预览模式不可调用 speech.asr.v1';
    }
    return res.ok
      ? `FunASR 就绪（backend=${String(res.data.backend ?? 'vulkan')}）`
      : `FunASR 不可用 ${formatRpcError(res.error)}`;
  }

  if (statusMsg.includes('SubHunter')) {
    const res = await callCapability({
      capability: 'subtitle.fetch.v1',
      payload: { action: 'status' },
    });
    if (!res.available) {
      return '离线预览模式不可调用 subtitle.fetch.v1';
    }
    return res.ok
      ? 'SubHunter + ffsubsync 就绪'
      : `SubHunter 不可用 ${formatRpcError(res.error)}`;
  }

  return statusMsg;
}

export async function enqueueEphemeralTrack(
  item: MediaItem | undefined,
): Promise<{ ok: boolean; message: string }> {
  const songMatch = item?.playUrl.match(/\/song\/([0-9]+)/);
  const trackIds = songMatch?.[1] ? [songMatch[1]] : [];
  const res = await callCapability({
    capability: 'media.playlist.v1',
    payload: {
      action: 'enqueue_playback',
      mode: 'interactive_ephemeral',
      record_permanent_history: false,
      track_ids: trackIds,
    },
  });
  if (!res.available) {
    return { ok: false, message: '离线预览模式不可写入真实灵感电台队列' };
  }
  if (!res.ok) {
    return { ok: false, message: `入队失败 ${formatRpcError(res.error)}` };
  }
  return { ok: true, message: '已加入「OmniDesk AI 灵感电台」（未触碰每日歌单）' };
}

export async function extractNasEmbeddedSubtitles(
  item: MediaItem,
): Promise<{ ok: boolean; message: string }> {
  const res = await invokeDomain<{ count?: number; subtitle_path?: string; source?: string }>({
    domain: 'media',
    sourceId: 'plugin-fnos-nas',
    method: 'extract_subtitles',
    payload: {
      item_id: item.id,
      path: item.nasMeta?.path,
      force_embedded: true,
      save_srt: true,
    },
    timeoutMs: 60_000,
  });
  if (!res.available) {
    return { ok: false, message: '离线预览模式不可提取原生字幕' };
  }
  if (!res.ok) {
    return { ok: false, message: `提取原生字幕失败 ${formatRpcError(res.error)}` };
  }
  const saved = res.data.subtitle_path ? `，已保存 ${basename(res.data.subtitle_path)}` : '';
  return {
    ok: true,
    message: `已提取 ${Number(res.data.count ?? 0)} 条原生字幕${saved}`,
  };
}

export async function saveItemSubtitlesAsSrt(
  item: MediaItem,
  lines: LyricLine[],
  translations: Record<string, string> = {},
): Promise<{ ok: boolean; message: string; path?: string }> {
  if (!lines.length) {
    return { ok: false, message: '当前没有可保存的字幕内容' };
  }
  const merged = lines.map((line, index) => ({
    time_sec: line.timeSec,
    start_sec: line.timeSec,
    original: line.original,
    translation: translations[String(index)] ?? line.translation ?? '',
  }));

  const isYoutube =
    item.sourceId === 'plugin-youtube' || item.streamResolver?.providerPluginId === 'plugin-youtube';
  if (isYoutube) {
    const videoId = String(
      item.streamResolver?.payload?.video_id ?? item.streamResolver?.payload?.item_id ?? item.id ?? '',
    );
    const res = await callCapability<{ srt_path?: string }>({
      capability: 'media.youtube.v1',
      providerPluginId: 'plugin-youtube',
      payload: {
        action: 'save_srt',
        video_id: videoId,
        lines: merged,
        mode: 'bilingual',
      },
      timeoutMs: 30_000,
    });
    if (!res || !res.available) {
      return { ok: false, message: '离线预览模式不可保存 SRT' };
    }
    if (!res.ok) {
      return { ok: false, message: `保存 SRT 失败 ${formatRpcError(res.error)}` };
    }
    return {
      ok: true,
      message: `已保存 YouTube SRT：${basename(res.data.srt_path ?? `${videoId}.srt`)}（下次可直接加载）`,
      path: res.data.srt_path,
    };
  }

  if (item.kind === 'nas' || item.sourceId === 'plugin-fnos-nas') {
    const res = await invokeDomain<{ path?: string }>({
      domain: 'media',
      sourceId: 'plugin-fnos-nas',
      method: 'save_srt',
      payload: {
        item_id: item.id,
        media_path: item.nasMeta?.path,
        lines: merged,
        mode: 'bilingual',
      },
      timeoutMs: 30_000,
    });
    if (!res || !res.available) {
      return { ok: false, message: '离线预览模式不可保存 SRT' };
    }
    if (!res.ok) {
      return { ok: false, message: `保存 SRT 失败 ${formatRpcError(res.error)}` };
    }
    return {
      ok: true,
      message: `已保存 SRT：${basename(res.data.path ?? 'subtitle.srt')}（下次可直接加载）`,
      path: res.data.path,
    };
  }

  return { ok: false, message: '当前媒体源不支持保存为 SRT' };
}
