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
    soft: '#F1EFEC',
    active: '#F9E7ED',
    text: '#222120',
    muted: '#66625D',
    borderLine: 'rgba(36, 33, 29, 0.12)',
    focusRing: '#2563EB',
    domains: {
      media: '#BA345A',
      text: '#2260AA',
      book: '#1E7244',
      core: '#AC5F0C',
    },
  },
  graphite: {
    mode: 'graphite',
    label: 'Graphite #181718',
    canvas: '#181718',
    surface: '#232122',
    soft: '#2B2729',
    active: '#422630',
    text: '#ECEEF2',
    muted: '#9EA4B0',
    borderLine: 'rgba(255, 255, 255, 0.10)',
    focusRing: '#60A5FA',
    domains: {
      media: '#F486A8',
      text: '#7DB8FA',
      book: '#6ED69C',
      core: '#F2B862',
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
    description: '视频流、Apple Music 双语歌词、飞牛 NAS 与 libmpv 播放舞台',
  },
  {
    id: 'text',
    index: 2,
    label: '文字期刊',
    shortLabel: 'Text',
    domain: 'text',
    shortcut: 'Ctrl+2',
    description: '社区高密阅读、科学期刊 DOI 精读、KaTeX 公式与双语对照',
  },
  {
    id: 'book',
    index: 3,
    label: '书本 PDF',
    shortLabel: 'Book',
    domain: 'book',
    shortcut: 'Ctrl+3',
    description: '2:3 书架、BabelDOC 双栏 PDF、划词伴读与 SM-2 生词闪卡',
  },
  {
    id: 'ai',
    index: 4,
    label: 'AI 工作台',
    shortLabel: 'AI',
    domain: 'core',
    shortcut: 'Ctrl+4',
    description: 'Magpie 路由控制、内驻 Agent 对话与双模策展控制台',
  },
  {
    id: 'store',
    index: 5,
    label: '插件中心',
    shortLabel: 'Plugins',
    domain: 'core',
    shortcut: 'Ctrl+5',
    description: '零 ABI Python 插件管理、0600 凭据表单与健康状态巡检',
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
