import type { BackendPluginRecord } from '../api';
import {
  INITIAL_PLUGINS,
  type ConfigFieldSchema,
  type DomainKind,
  type PluginItem,
} from '../data/mock';
import type { PluginHealthEntry } from '../hooks/useCoreEvents';

const VALID_DOMAINS = new Set<DomainKind>(['core', 'media', 'text', 'book']);
const VALID_KINDS = new Set<PluginItem['kind']>(['provider', 'workspace', 'hybrid', 'extension']);

function buildFallbackMap(): Map<string, PluginItem> {
  return new Map(INITIAL_PLUGINS.map((p) => [p.id, p]));
}

function normalizeSchema(
  rawSchema: BackendPluginRecord['config_schema'],
  fallback?: ConfigFieldSchema[],
): ConfigFieldSchema[] {
  if (Array.isArray(rawSchema) && rawSchema.length > 0) {
    return rawSchema.map((field) => ({
      key: field.key,
      label: field.label,
      type: field.type,
      default: field.type === 'secret' ? '' : (field.default ?? ''),
      description: field.description,
      required: field.required,
      options: field.options,
    }));
  }
  return fallback ?? [];
}

export function buildDefaultConfigForSchema(
  schema: readonly ConfigFieldSchema[],
): Record<string, string | boolean | number> {
  const defaults: Record<string, string | boolean | number> = {};
  for (const field of schema) {
    defaults[field.key] = field.type === 'secret' ? '' : field.default;
  }
  return defaults;
}

export function mapBackendPluginRecord(
  rec: BackendPluginRecord,
  fallbackMap: ReadonlyMap<string, PluginItem> = buildFallbackMap(),
): PluginItem {
  const fallback = fallbackMap.get(rec.plugin_id);
  const kind: PluginItem['kind'] = VALID_KINDS.has(rec.kind as PluginItem['kind'])
    ? (rec.kind as PluginItem['kind'])
    : (fallback?.kind ?? 'provider');
  const domain: DomainKind = VALID_DOMAINS.has(rec.domain as DomainKind)
    ? (rec.domain as DomainKind)
    : (fallback?.domain ?? 'core');

  const state = rec.state || 'discovered';
  const isFaulted = state === 'faulted' || state === 'invalid';
  const runtimeError = typeof rec.error === 'string' ? rec.error : '';
  const installed = state !== 'uninstalled';
  const enabled =
    isFaulted || state === 'unloaded'
      ? false
      : typeof rec.enabled === 'boolean'
        ? rec.enabled
        : true;
  const healthOk = !isFaulted && !runtimeError;
  const verifyStatus: PluginItem['verifyStatus'] = isFaulted ? 'failed' : 'idle';
  const schema = normalizeSchema(rec.config_schema, fallback?.config_schema);

  return {
    id: rec.plugin_id,
    name: rec.name && rec.name.trim() ? rec.name : (fallback?.name ?? rec.plugin_id),
    version: rec.version && rec.version.trim() ? rec.version : (fallback?.version ?? '0.0.0'),
    kind,
    domain,
    author: rec.author && rec.author.trim() ? rec.author : (fallback?.author ?? 'OmniDesk'),
    description:
      rec.description && rec.description.trim()
        ? rec.description
        : (runtimeError || fallback?.description || ''),
    installed,
    enabled,
    healthOk,
    hotReloadMs: fallback?.hotReloadMs ?? 0,
    verifyStatus,
    runtimeState: state,
    runtimeError,
    reloadCount: typeof rec.reload_count === 'number' ? rec.reload_count : 0,
    provides: Array.isArray(rec.provides) ? rec.provides : (fallback?.provides ?? []),
    consumes: Array.isArray(rec.consumes) ? rec.consumes : (fallback?.consumes ?? []),
    permissions:
      Array.isArray(rec.permissions) && rec.permissions.length > 0
        ? rec.permissions
        : (fallback?.permissions ?? []),
    config_schema: schema,
    initialConfig: buildDefaultConfigForSchema(schema),
  };
}

export function mapBackendPlugins(records: readonly BackendPluginRecord[]): PluginItem[] {
  const fallbackMap = buildFallbackMap();
  return records.map((rec) => mapBackendPluginRecord(rec, fallbackMap));
}

export function applyHealthUpdates(
  plugins: readonly PluginItem[],
  healthMap: Record<string, PluginHealthEntry> | undefined,
): PluginItem[] {
  if (!healthMap) {
    return [...plugins];
  }
  return plugins.map((plugin) => {
    const entry = healthMap[plugin.id];
    if (!entry) {
      return plugin;
    }
    const nextState = entry.state ?? plugin.runtimeState;
    const nextEnabled =
      nextState === 'unloaded' || nextState === 'faulted'
        ? false
        : nextState === 'loaded' || nextState === 'reloaded'
          ? true
          : plugin.enabled;
    return {
      ...plugin,
      healthOk: entry.healthOk,
      enabled: nextEnabled,
      runtimeState: nextState,
      runtimeError: entry.lastError ?? plugin.runtimeError,
      verifyStatus: entry.healthOk ? plugin.verifyStatus : 'failed',
    };
  });
}

export function splitConfigAndSecrets(
  plugin: PluginItem,
  values: Record<string, string | boolean | number>,
): {
  plainConfig: Record<string, string | boolean | number>;
  secretValues: Record<string, string>;
} {
  const secretKeys = new Set(
    plugin.config_schema.filter((f) => f.type === 'secret').map((f) => f.key),
  );
  const plainConfig: Record<string, string | boolean | number> = {};
  const secretValues: Record<string, string> = {};

  for (const field of plugin.config_schema) {
    const val = values[field.key] ?? field.default;
    if (secretKeys.has(field.key)) {
      const text = String(val ?? '');
      if (text.trim().length > 0) {
        secretValues[field.key] = text;
      }
    } else {
      plainConfig[field.key] = val;
    }
  }
  return { plainConfig, secretValues };
}
