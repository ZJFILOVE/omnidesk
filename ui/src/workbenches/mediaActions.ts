import { callCapability, formatRpcError } from '../api';
import type { MediaItem } from '../data/mock';

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
