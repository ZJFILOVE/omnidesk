import { Settings2 } from 'lucide-react';
import type { DomainSourceDescriptor } from '../api';
import {
  FocusButton,
  type ToneKind,
} from '../components/WorkbenchLayout';
import {
  SegmentedPills,
  type PillOption,
} from '../components/WorkbenchPrimitives';

const CATEGORY_LABELS: Record<string, string> = {
  recommend: '推荐',
  popular: '热门',
  dynamic: '动态',
  bangumi: '追番',
  live: '直播',
  search: '搜索',
  subscriptions: '订阅',
  trending: '热门',
  podcast: '播客',
  channels: '频道',
  watch_later: '稍后再看',
  history: '历史',
  favorites: '收藏',
  playlists: '播放列表',
};

const QUALITY_LABELS: Record<string, string> = {
  auto: '自动',
  best: '最佳',
  '4k': '4K',
  '2160p': '2160p',
  '1440p': '1440p',
  '1080p60': '1080p60',
  '1080p+': '1080p+',
  '1080p': '1080p',
  '720p': '720p',
  '480p': '480p',
  '360p': '360p',
  audio: '仅音频',
};

function label(value: string): string {
  return CATEGORY_LABELS[value] ?? QUALITY_LABELS[value] ?? value;
}

interface MediaSourceToolbarProps {
  sources: DomainSourceDescriptor[];
  sourceId: string;
  onSourceChange: (sourceId: string) => void;
  categories: string[];
  category: string;
  onCategoryChange: (category: string) => void;
  qualityOptions: string[];
  quality: string;
  onQualityChange: (quality: string) => void;
  settingsLabel?: string;
  onOpenSettings?: () => void;
  activeFocusId: string;
  onFocus: (id: string) => void;
  tone?: ToneKind;
}

export function MediaSourceToolbar({
  sources,
  sourceId,
  onSourceChange,
  categories,
  category,
  onCategoryChange,
  qualityOptions,
  quality,
  onQualityChange,
  settingsLabel,
  onOpenSettings,
  activeFocusId,
  onFocus,
  tone = 'media',
}: MediaSourceToolbarProps) {
  if (sources.length === 0 && categories.length === 0 && qualityOptions.length === 0) {
    return null;
  }
  const sourceOptions: PillOption<string>[] = sources.map((source) => ({
    id: source.sourceId,
    label: source.title || source.sourceId,
  }));
  const categoryOptions: PillOption<string>[] = categories.map((entry) => ({
    id: entry,
    label: label(entry),
  }));
  const qualityChoices: PillOption<string>[] = [
    ...(qualityOptions.length > 0 ? [{ id: '', label: '默认' }] : []),
    ...qualityOptions.map((entry) => ({ id: entry, label: label(entry) })),
  ];

  return (
    <div className="flex w-full flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        {sourceOptions.length > 1 ? (
          <>
            <span className="text-[13px] font-medium text-[var(--text-muted)]">来源</span>
            <SegmentedPills
              options={sourceOptions}
              value={sourceId}
              onChange={onSourceChange}
              focusPrefix="media:source:"
              activeFocusId={activeFocusId}
              onFocus={onFocus}
              tone={tone}
              ariaLabel="选择媒体来源"
            />
          </>
        ) : null}
        {settingsLabel && onOpenSettings ? (
          <FocusButton
            focusId="media:plugin-settings"
            activeFocusId={activeFocusId}
            onFocus={onFocus}
            onActivate={onOpenSettings}
            ariaLabel={settingsLabel}
            surface="surface"
            className="ml-auto inline-flex items-center gap-1.5 border border-[var(--border-line)] px-3 py-1.5 text-[13.5px] font-medium"
          >
            <Settings2 className="h-3.5 w-3.5" aria-hidden="true" />
            <span>{settingsLabel}</span>
          </FocusButton>
        ) : null}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {categoryOptions.length > 0 ? (
          <>
            <span className="text-[13px] font-medium text-[var(--text-muted)]">分类</span>
            <SegmentedPills
              options={categoryOptions}
              value={category}
              onChange={onCategoryChange}
              focusPrefix="media:category:"
              activeFocusId={activeFocusId}
              onFocus={onFocus}
              tone={tone}
              ariaLabel="选择来源分类"
            />
          </>
        ) : null}
        {qualityChoices.length > 1 ? (
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <span className="text-[13px] font-medium text-[var(--text-muted)]">画质</span>
            <SegmentedPills
              options={qualityChoices}
              value={quality}
              onChange={onQualityChange}
              focusPrefix="media:quality:"
              activeFocusId={activeFocusId}
              onFocus={onFocus}
              tone={tone}
              ariaLabel="选择播放画质"
            />
          </div>
        ) : null}
      </div>
    </div>
  );
}

export default MediaSourceToolbar;
