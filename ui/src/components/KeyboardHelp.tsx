import { useEffect } from 'react';
import { Keyboard, ShieldCheck, X } from 'lucide-react';
import { useImeGuard } from '../hooks/useImeGuard';
import { DOMAIN_CSS_VARS } from '../theme';
import type { ContentDomain, WorkbenchId, WorkbenchMeta } from '../types';

export interface KeyboardHelpProps {
  open: boolean;
  onClose: () => void;
  activeWorkbench?: WorkbenchId;
  activeMeta?: WorkbenchMeta;
}

export interface ShortcutBinding {
  keys: string[];
  description: string;
}

export interface ShortcutGroup {
  id: string;
  title: string;
  domain: ContentDomain;
  items: ShortcutBinding[];
}

export const SHORTCUT_GROUPS: readonly ShortcutGroup[] = [
  {
    id: 'workbenches',
    title: '全局工作台切换',
    domain: 'core',
    items: [
      { keys: ['Ctrl', '1'], description: '影视音乐 (Media)' },
      { keys: ['Ctrl', '2'], description: '文字期刊 (Text)' },
      { keys: ['Ctrl', '3'], description: '书本 PDF (Book)' },
      { keys: ['Ctrl', '4'], description: 'AI 工作台 (AI)' },
      { keys: ['Ctrl', '5'], description: '插件中心 (Plugins)' },
      { keys: ['Alt', '字母'], description: '直达确定性分配的插件专属页签' },
    ],
  },
  {
    id: 'spatial-focus',
    title: '2D 空间焦点引擎',
    domain: 'text',
    items: [
      { keys: ['h', 'j', 'k', 'l'], description: '跨分栏几何最近邻移动焦点' },
      { keys: ['←', '↓', '↑', '→'], description: '方向键二维空间焦点跳转' },
      { keys: ['f'], description: '触发全屏字母闪跳 (Hint Overlay)' },
      { keys: ['Ctrl', 'Tab'], description: '在主舞台与 F8 伴读栏之间切换焦点区' },
      { keys: ['Enter'], description: '激活当前焦点卡片或打开详情' },
    ],
  },
  {
    id: 'overlays-controls',
    title: '效率面板与阅读控制',
    domain: 'media',
    items: [
      { keys: ['Ctrl', 'K'], description: '打开全局命令面板 (Command Palette)' },
      { keys: ['F8'], description: '开关无遮罩响应式伴读栏 (Side / Bottom)' },
      { keys: ['/'], description: '聚焦当前工作台搜索过滤框' },
      { keys: ['t'], description: '切换 Paper (#F6F5F3) / Graphite (#181718) 主题' },
      { keys: ['Space'], description: '播放 / 暂停当前音视频媒体' },
      { keys: ['←', '→'], description: '影视音乐页：后退 / 前进 10 秒' },
      { keys: ['↑', '↓', 'M'], description: '影视音乐页：音量 ±5% / 静音与恢复' },
      { keys: [',', '.', 'Backspace'], description: '降低 / 提高倍速（每次 0.1×），恢复 1×' },
      { keys: ['Shift+P', 'Shift+N'], description: '上一项 / 下一项' },
      { keys: ['F9'], description: '跳过当前位置的赞助片段（v1）' },
      { keys: ['F11'], description: '影视音乐页：切换全屏（v1）' },
      { keys: ['?'], description: '打开 / 关闭当前快捷键帮助面板' },
    ],
  },
  {
    id: 'ime-safety',
    title: '中文输入法 IME 隔离',
    domain: 'book',
    items: [
      { keys: ['compositionstart'], description: 'Fcitx5 / Rime 候选词激活期间自动锁定单键快捷键' },
      { keys: ['Enter'], description: '选词上屏不误触发送或命令执行' },
      { keys: ['Esc'], description: '优先交给输入法关闭候选框，退出后恢复面板关闭' },
    ],
  },
];

export function KeyboardHelp({
  open,
  onClose,
  activeWorkbench,
  activeMeta: activeMetaProp,
}: KeyboardHelpProps) {
  const ime = useImeGuard();

  useEffect(() => {
    if (!open || typeof window === 'undefined') {
      return undefined;
    }

    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (ime.shouldIgnoreShortcut(event)) {
        return;
      }
      if (event.key === 'Escape' || event.key === '?') {
        event.preventDefault();
        event.stopPropagation();
        onClose();
      }
    };

    window.addEventListener('keydown', handleKeyDown, true);
    return () => {
      window.removeEventListener('keydown', handleKeyDown, true);
    };
  }, [open, onClose, ime]);

  if (!open) {
    return null;
  }

  const activeMeta = activeMetaProp ?? (activeWorkbench ? { label: String(activeWorkbench), shortcut: '' } : null);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-[var(--backdrop-scrim)] p-4"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) {
          onClose();
        }
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="快捷键帮助"
        data-testid="keyboard-help-dialog"
        style={{ boxShadow: 'var(--shadow-elevated)' }}
        className="omni-theme-surface omni-drawer-motion flex max-h-[84vh] w-full max-w-3xl flex-col overflow-hidden rounded-[18px] bg-[var(--bg-elevated)] text-[var(--text-primary)]"
      >
        <header className="flex items-center justify-between gap-3 border-b border-[var(--border-subtle)] px-6 py-4">
          <div className="flex items-center gap-3">
            <Keyboard className="h-4 w-4 text-[var(--focus-ring)]" aria-hidden="true" />
            <div>
              <h2 className="text-[17.5px] font-semibold text-[var(--text-primary)]">
                全键盘操作与快捷键总览
              </h2>
              <p className="text-[14px] text-[var(--text-muted)]">
                {activeMeta
                  ? `当前工作台：${activeMeta.label} (${activeMeta.shortcut})`
                  : 'Paper & Graphite 桌面端 · 全键盘优先'}
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2.5">
            <span className="inline-flex items-center gap-1.5 rounded-full bg-[var(--status-ok-bg)] px-2.5 py-0.5 text-[13.5px] font-medium text-[var(--status-ok-fg)]">
              <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
              IME 自动避让
            </span>
            <button
              type="button"
              onClick={onClose}
              aria-label="关闭快捷键帮助"
              title="关闭快捷键帮助 (Esc / ?)"
              className="inline-flex h-7 w-7 cursor-pointer items-center justify-center rounded-[8px] text-[var(--text-muted)] hover:bg-[var(--hover-fill)] hover:text-[var(--text-primary)]"
            >
              <X className="h-4 w-4" aria-hidden="true" />
            </button>
          </div>
        </header>

        <div className="grid grid-cols-1 gap-4 overflow-y-auto p-6 md:grid-cols-2">
          {SHORTCUT_GROUPS.map((group) => (
            <section
              key={group.id}
              className="rounded-[14px] bg-[var(--bg-canvas)] p-4.5"
            >
              <div className="mb-3 flex items-center gap-2 pb-1">
                <span
                  aria-hidden="true"
                  className="h-2 w-2 rounded-full"
                  style={{ backgroundColor: DOMAIN_CSS_VARS[group.domain] }}
                />
                <h3 className="text-[15px] font-semibold text-[var(--text-primary)]">
                  {group.title}
                </h3>
              </div>

              <ul className="space-y-2.5">
                {group.items.map((item) => (
                  <li
                    key={`${group.id}-${item.keys.join('+')}`}
                    className="flex items-center justify-between gap-3 text-[14.5px]"
                  >
                    <span className="text-[var(--text-muted)]">{item.description}</span>
                    <span className="flex shrink-0 items-center gap-1">
                      {item.keys.map((keyLabel) => (
                        <kbd
                          key={keyLabel}
                          className="omni-keycap rounded-[5px] px-1.5 py-0.5 text-[13.5px]"
                        >
                          {keyLabel}
                        </kbd>
                      ))}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>

        <footer className="flex items-center justify-between border-t border-[var(--border-subtle)] bg-[var(--bg-sunken)]/55 px-6 py-2.5 text-[13.5px] text-[var(--text-muted)]">
          <span>所有焦点始终绑定于稳定节点 ID，异步刷新不丢失当前位置。</span>
          <span>按 Esc 或 ? 关闭</span>
        </footer>
      </div>
    </div>
  );
}

export default KeyboardHelp;
