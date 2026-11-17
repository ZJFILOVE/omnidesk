import { BOOK_ITEMS, INITIAL_PLUGINS, MEDIA_ITEMS, TEXT_ARTICLES } from '../data/mock';
import { resolveWorkbenchMeta } from '../theme';
import type { ActiveEntityContext, WorkbenchId, WorkbenchMeta } from '../types';

export const DEFAULT_FOCUS_IDS: Partial<Record<WorkbenchId, string>> = {
  media: `media:card:${MEDIA_ITEMS[0]?.id ?? ''}`,
  text: `text:article:${TEXT_ARTICLES[0]?.id ?? ''}`,
  book: `book:shelf:${BOOK_ITEMS[0]?.id ?? ''}`,
  ai: 'ai:composer:input',
  store: `store:plugin:${INITIAL_PLUGINS[0]?.id ?? ''}`,
};

export const SEARCH_FOCUS_IDS: Partial<Record<WorkbenchId, string>> = {
  media: 'media:search',
  text: 'text:search',
  book: 'book:catalog:search',
  ai: 'ai:composer:input',
  store: 'store:search',
};

export function defaultFocusIdFor(workbench: WorkbenchId): string {
  return DEFAULT_FOCUS_IDS[workbench] ?? `plugin-workspace:${workbench}:root`;
}

export function searchFocusIdFor(workbench: WorkbenchId): string {
  return SEARCH_FOCUS_IDS[workbench] ?? `plugin-workspace:${workbench}:search`;
}

export function resolveContextEntity(
  workbench: WorkbenchId,
  focusedId: string | null,
  activeMeta?: WorkbenchMeta,
): ActiveEntityContext {
  const meta = activeMeta ?? resolveWorkbenchMeta(workbench);
  if (focusedId?.startsWith('media:card:')) {
    const item = MEDIA_ITEMS.find((m) => `media:card:${m.id}` === focusedId);
    if (item) return { domain: 'media', workbench, itemId: item.id, title: item.title, subtitle: item.subtitle, sourceId: item.sourceId, kind: item.kind };
  }
  if (focusedId?.startsWith('text:article:')) {
    const article = TEXT_ARTICLES.find((a) => `text:article:${a.id}` === focusedId);
    if (article) return { domain: 'text', workbench, itemId: article.id, title: article.titleZh || article.title, subtitle: article.sourceName };
  }
  if (focusedId?.startsWith('book:shelf:')) {
    const book = BOOK_ITEMS.find((b) => `book:shelf:${b.id}` === focusedId);
    if (book) return { domain: 'book', workbench, itemId: book.id, title: book.title, subtitle: book.author };
  }
  if (focusedId?.startsWith('store:plugin:')) {
    const plugin = INITIAL_PLUGINS.find((p) => `store:plugin:${p.id}` === focusedId);
    if (plugin) return { domain: plugin.domain, workbench, itemId: plugin.id, title: plugin.name, subtitle: plugin.description };
  }
  return { domain: meta.domain, workbench, itemId: focusedId ?? meta.id, title: meta.label };
}
