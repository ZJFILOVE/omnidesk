import { useEffect } from 'react';
import { Keyboard, ShieldCheck, X } from 'lucide-react';
import { useImeGuard } from '../hooks/useImeGuard';
import { DOMAIN_CSS_VARS, WORKBENCH_MAP } from '../theme';
import type { ContentDomain, WorkbenchId } from '../types';

export interface KeyboardHelpProps {
  open: boolean;
  onClose: () => void;
  activeWorkbench?: WorkbenchId;
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

export function KeyboardHelp({ open, onClose, activeWorkbench }: KeyboardHelpProps) {
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

  const activeMeta = activeWorkbench ? WORKBENCH_MAP[activeWorkbench] : null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-[var(--backdrop-scrim)]"
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
        className="omni-theme-surface flex max-h-[84vh] w-full max-w-3xl flex-col overflow-hidden rounded-xl border border-[var(--border-strong)] bg-[var(--bg-surface)] text-[var(--text-primary)] shadow-2xl"
      >
        <header className="flex items-center justify-between gap-3 border-b border-[var(--border-line)] bg-[var(--bg-canvas)] px-5 py-3.5">
          <div className="flex items-center gap-2.5">
            <Keyboard className="h-4 w-4 text-[var(--focus-ring)]" aria-hidden="true" />
            <div>
              <h2 className="text-sm font-semibold text-[var(--text-primary)]">
                全键盘操作与快捷键总览
              </h2>
              <p className="text-xs text-[var(--text-muted)]">
                {activeMeta
                  ? `当前工作台：${activeMeta.label} (${activeMeta.shortcut})`
                  : 'Paper & Graphite 桌面端 · 键盘控制一切'}
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <span className="inline-flex items-center gap-1 rounded border border-[var(--border-line)] bg-[var(--status-ok-bg)] px-2 py-0.5 text-[11px] font-medium text-[var(--status-ok-fg)]">
              <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
              IME 自动避让
            </span>
            <button
              type="button"
              onClick={onClose}
              aria-label="关闭快捷键帮助"
              className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-transparent text-[var(--text-muted)] hover:border-[var(--border-line)] hover:bg-[var(--hover-fill)] hover:text-[var(--text-primary)]"
            >
              <X className="h-4 w-4" aria-hidden="true" />
            </button>
          </div>
        </header>

        <div className="grid grid-cols-1 gap-3 overflow-y-auto p-5 md:grid-cols-2">
          {SHORTCUT_GROUPS.map((group) => (
            <section
              key={group.id}
              className="rounded-lg border border-[var(--border-line)] bg-[var(--bg-canvas)] p-3.5"
            >
              <div className="mb-2.5 flex items-center gap-2 border-b border-[var(--border-line)] pb-2">
                <span
                  aria-hidden="true"
                  className="h-2 w-2 rounded-full"
                  style={{ backgroundColor: DOMAIN_CSS_VARS[group.domain] }}
                />
                <h3 className="text-xs font-semibold tracking-wide text-[var(--text-primary)]">
                  {group.title}
                </h3>
              </div>

              <ul className="space-y-2">
                {group.items.map((item) => (
                  <li
                    key={`${group.id}-${item.keys.join('+')}`}
                    className="flex items-center justify-between gap-3 text-xs"
                  >
                    <span className="text-[var(--text-muted)]">{item.description}</span>
                    <span className="flex shrink-0 items-center gap-1">
                      {item.keys.map((keyLabel) => (
                        <kbd
                          key={keyLabel}
                          className="rounded border border-[var(--keycap-border)] bg-[var(--keycap-bg)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--keycap-fg)] shadow-[0_1px_0_var(--keycap-shadow)]"
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

        <footer className="flex items-center justify-between border-t border-[var(--border-line)] bg-[var(--bg-soft)] px-5 py-2.5 text-[11px] text-[var(--text-muted)]">
          <span>所有焦点始终绑定于稳定 itemId，异步数据刷新绝不丢失当前光标。</span>
          <span>按 Esc 或 ? 关闭</span>
        </footer>
      </div>
    </div>
  );
}

export default KeyboardHelp;
