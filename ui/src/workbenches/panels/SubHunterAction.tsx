import React from 'react';
import { FocusButton } from '../../components/WorkbenchLayout';
import type { MediaItem } from '../../data/mock';
import {
  runSubHunterPipeline,
  type SubtitleOutputMode,
} from '../mediaActions';

const SUBTITLE_OUTPUT_MODES = [
  { id: 'bilingual', label: '双语' },
  { id: 'translated', label: '仅译文' },
  { id: 'original', label: '仅原文' },
] as const satisfies ReadonlyArray<{ id: SubtitleOutputMode; label: string }>;

export interface SubHunterActionProps {
  item: MediaItem;
  activeFocusId: string;
  onFocus: (id: string) => void;
  onStatusChange: (msg: string) => void;
}

export const SubHunterAction: React.FC<SubHunterActionProps> = ({
  item,
  activeFocusId,
  onFocus,
  onStatusChange,
}) => {
  const [outputMode, setOutputMode] = React.useState<SubtitleOutputMode>('bilingual');

  return (
    <div className="flex flex-wrap items-center justify-end gap-2">
      <div role="group" aria-label="字幕输出模式" className="flex items-center gap-1">
        {SUBTITLE_OUTPUT_MODES.map((mode) => (
          <FocusButton
            key={mode.id}
            focusId={`media:subtitle-mode:${mode.id}`}
            activeFocusId={activeFocusId}
            onFocus={onFocus}
            onActivate={() => setOutputMode(mode.id)}
            ariaPressed={outputMode === mode.id}
            surface={outputMode === mode.id ? 'soft' : 'ghost'}
            className="px-2 py-1 text-[13px]"
          >
            {mode.label}
          </FocusButton>
        ))}
      </div>
      <FocusButton
        focusId="media:action:subhunter"
        activeFocusId={activeFocusId}
        onFocus={onFocus}
        onActivate={() => {
          onStatusChange('SubHunter 正在检索字幕…');
          void runSubHunterPipeline(item, {
            outputMode,
            onProgress: onStatusChange,
          })
            .then((result) => onStatusChange(result.message))
            .catch((err) =>
              onStatusChange(
                `SubHunter 失败：${err instanceof Error ? err.message : String(err)}`,
              ),
            );
        }}
        surface="soft"
        className="px-3 py-1.5 text-[14px] font-medium"
      >
        运行 SubHunter 字幕检索与时间轴对齐
      </FocusButton>
    </div>
  );
};
