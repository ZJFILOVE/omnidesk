export type ThemeMode = 'paper' | 'graphite';

export type BuiltinWorkbenchId = 'media' | 'text' | 'book' | 'ai' | 'store';
export type PluginWorkbenchId = `plugin:${string}`;
export type WorkbenchId = BuiltinWorkbenchId | PluginWorkbenchId;

export type ContentDomain = 'media' | 'text' | 'book' | 'core';

export type DockLayout = 'side' | 'bottom';

export type FocusZone = 'nav' | 'main' | 'dock' | 'overlay';

export type Direction = 'left' | 'down' | 'up' | 'right';

export interface WorkbenchMeta {
  id: WorkbenchId;
  index?: number;
  label: string;
  shortLabel: string;
  domain: ContentDomain;
  shortcut: string;
  description: string;
  badge?: string;
  pluginId?: string;
}

export interface ActiveEntityContext {
  domain: ContentDomain;
  workbench: WorkbenchId;
  itemId: string;
  title: string;
  subtitle?: string;
  sourceId?: string;
  kind?: string;
  url?: string;
  extra?: Record<string, unknown>;
}

export interface FocusRect {
  left: number;
  top: number;
  width: number;
  height: number;
  right: number;
  bottom: number;
}

export interface FocusNodeRegistration {
  id: string;
  element: HTMLElement;
  zone?: FocusZone;
  label?: string;
  disabled?: boolean;
  priority?: number;
  onActivate?: () => void;
}

export interface HintTarget {
  id: string;
  code: string;
  label?: string;
  zone: FocusZone;
  rect: FocusRect;
}

export interface HintOverlayProps {
  active: boolean;
  targets: HintTarget[];
  query: string;
  onSelect: (id: string) => void;
  onCancel: () => void;
}

export interface PaletteCommand {
  id: string;
  title: string;
  subtitle?: string;
  category: 'workbench' | 'theme' | 'dock' | 'navigation' | 'search' | 'plugin';
  shortcut?: string;
  domain?: ContentDomain;
  keywords?: string[];
  action: () => void;
}

export interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  activeWorkbench: WorkbenchId;
  onSelectWorkbench: (id: WorkbenchId) => void;
  theme: ThemeMode;
  onToggleTheme: () => void;
  dockOpen: boolean;
  onToggleDock: () => void;
  onTriggerHints?: () => void;
  commands?: PaletteCommand[];
  initialQuery?: string;
}

export interface CompanionDockProps {
  open: boolean;
  layout: DockLayout;
  activeWorkbench: WorkbenchId;
  domain: ContentDomain;
  contextEntity?: ActiveEntityContext | null;
  dockWidgets?: readonly import('./api/types').DockWidgetSnapshot[];
  pendingPrompt?: string | null;
  onConsumePrompt?: () => void;
  onRefreshExtensions?: () => void;
  onClose: () => void;
  onOpenAiWorkbench: () => void;
  onOpenPluginStore: () => void;
}

export interface WorkbenchProps {
  isActive?: boolean;
  theme: ThemeMode;
  focusedItemId: string | null;
  searchTrigger?: number;
  navigationPayload?: Record<string, unknown> | null;
  onContextChange?: (ctx: ActiveEntityContext | null) => void;
  onOpenDock?: (prompt?: string) => void;
  onNavigateWorkbench?: (id: WorkbenchId, payload?: Record<string, unknown>) => void;
}

export interface PluginConfigField {
  key: string;
  label: string;
  type: 'string' | 'secret' | 'bool' | 'int' | 'select';
  default?: string | boolean | number;
  description?: string;
  required?: boolean;
  options?: string[];
}

export interface PluginManifestSummary {
  schema_version: 2;
  id: string;
  name: string;
  version: string;
  kind: 'provider' | 'workspace' | 'hybrid' | 'extension';
  domain: ContentDomain;
  description?: string;
  author?: string;
  enabled?: boolean;
  provides?: string[];
  consumes?: string[];
  permissions?: string[];
  config_schema?: PluginConfigField[];
}

export interface CapabilityResult<T = Record<string, unknown>> {
  ok: boolean;
  data: T;
  error: string;
  provider_plugin_id: string;
}
