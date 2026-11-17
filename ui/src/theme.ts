import type { ContentDomain, ThemeMode, WorkbenchId, WorkbenchMeta } from './types';

export const THEME_STORAGE_KEY = 'omnidesk:theme';
export const DOCK_ASPECT_THRESHOLD = 1.45;

export interface ThemePalette {
  mode: ThemeMode;
  label: string;
  canvas: string;
  surface: string;
  soft: string;
  active: string;
  text: string;
  muted: string;
  borderLine: string;
  focusRing: string;
  domains: Record<ContentDomain, string>;
}

export const THEME_PALETTES: Record<ThemeMode, ThemePalette> = {
  paper: {
    mode: 'paper',
    label: 'Paper #F6F5F3',
    canvas: '#F6F5F3',
    surface: '#FCFBF9',
    soft: '#E6E2DA',
    active: '#F3EAEF',
    text: '#1C1B19',
    muted: '#59544D',
    borderLine: 'rgba(28, 27, 25, 0.09)',
    focusRing: '#1D4ED8',
    domains: {
      media: '#A41F48',
      text: '#185194',
      book: '#165E37',
      core: '#8F4906',
    },
  },
  graphite: {
    mode: 'graphite',
    label: 'Graphite #181718',
    canvas: '#181718',
    surface: '#201F22',
    soft: '#2C2A2F',
    active: '#39252E',
    text: '#F2F0EB',
    muted: '#A7AAB4',
    borderLine: 'rgba(255, 255, 255, 0.095)',
    focusRing: '#60A5FA',
    domains: {
      media: '#F48FB1',
      text: '#7EC0FF',
      book: '#73DFA5',
      core: '#F5BC69',
    },
  },
};

export const WORKBENCHES: readonly WorkbenchMeta[] = [
  {
    id: 'media',
    index: 1,
    label: '影视音乐',
    shortLabel: 'Media',
    domain: 'media',
    shortcut: 'Ctrl+1',
    description: '视频精播 · Apple Music 双语歌词 · 飞牛 NAS 原盘',
  },
  {
    id: 'text',
    index: 2,
    label: '文字期刊',
    shortLabel: 'Text',
    domain: 'text',
    shortcut: 'Ctrl+2',
    description: '学术期刊精读 · 社区嵌套讨论 · 双语对照',
  },
  {
    id: 'book',
    index: 3,
    label: '书本 PDF',
    shortLabel: 'Book',
    domain: 'book',
    shortcut: 'Ctrl+3',
    description: '书架封面墙 · 双栏原版对照 · SM-2 生词记忆',
  },
  {
    id: 'ai',
    index: 4,
    label: 'AI 工作台',
    shortLabel: 'AI',
    domain: 'core',
    shortcut: 'Ctrl+4',
    description: '对话主舞台 · Magpie 路由调度 · 双模策展隔离',
  },
  {
    id: 'store',
    index: 5,
    label: '插件中心',
    shortLabel: 'Plugins',
    domain: 'core',
    shortcut: 'Ctrl+5',
    description: 'Python 插件管理 · 健康巡检 · 0600 隔离配置',
  },
] as const;

export const WORKBENCH_MAP: Record<WorkbenchId, WorkbenchMeta> = {
  media: WORKBENCHES[0],
  text: WORKBENCHES[1],
  book: WORKBENCHES[2],
  ai: WORKBENCHES[3],
  store: WORKBENCHES[4],
};

export const DOMAIN_CSS_VARS: Record<ContentDomain, string> = {
  media: 'var(--domain-media)',
  text: 'var(--domain-text)',
  book: 'var(--domain-book)',
  core: 'var(--domain-core)',
};

export const DOMAIN_LABELS: Record<ContentDomain, string> = {
  media: 'Media',
  text: 'Text',
  book: 'Book',
  core: 'Core',
};

export function getInitialTheme(): ThemeMode {
  if (typeof window === 'undefined') {
    return 'paper';
  }
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    if (stored === 'paper' || stored === 'graphite') {
      return stored;
    }
  } catch {
    // Ignore storage access errors in restricted webviews.
  }
  if (typeof document !== 'undefined') {
    const attr = document.documentElement.getAttribute('data-theme');
    if (attr === 'paper' || attr === 'graphite') {
      return attr;
    }
  }
  return 'paper';
}

export function applyTheme(mode: ThemeMode): void {
  if (typeof document !== 'undefined') {
    const root = document.documentElement;
    root.setAttribute('data-theme', mode);
    root.style.colorScheme = mode === 'graphite' ? 'dark' : 'light';
    root.style.backgroundColor = THEME_PALETTES[mode].canvas;
  }
  if (typeof window !== 'undefined') {
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, mode);
    } catch {
      // Ignore storage persistence errors.
    }
  }
}

export function nextTheme(mode: ThemeMode): ThemeMode {
  return mode === 'paper' ? 'graphite' : 'paper';
}

export function resolveDockLayout(width: number, height: number): 'side' | 'bottom' {
  if (height <= 0) {
    return 'side';
  }
  return width / height >= DOCK_ASPECT_THRESHOLD ? 'side' : 'bottom';
}
