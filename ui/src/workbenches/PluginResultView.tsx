import { ExternalLink as ExternalLinkIcon, FileText, MessageSquare, Star } from 'lucide-react';
import { MarkdownRenderer } from '../components/MarkdownRenderer';
import { ExternalLink } from '../components/ExternalLink';

type Dict = Record<string, unknown>;

interface PluginResultViewProps {
  payload: unknown;
  actionId: string;
  onOpenItem?: (item: Dict) => void;
}

const isRecord = (value: unknown): value is Dict =>
  Boolean(value) && typeof value === 'object' && !Array.isArray(value);

const asRecords = (value: unknown): Dict[] =>
  Array.isArray(value) ? value.filter(isRecord) : [];

const text = (value: unknown): string => {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return '';
};

const safeHref = (value: unknown): string => {
  const href = text(value).trim();
  return href.startsWith('https://') || href.startsWith('http://') ? href : '';
};

export function resolveBody(payload: unknown): Dict {
  if (!isRecord(payload)) return {};
  const body = isRecord(payload.result) ? payload.result : isRecord(payload.data) ? payload.data : payload;
  return !Array.isArray(body.items) && Array.isArray(body.results) ? { ...body, items: body.results } : body;
}

function FeedItemCard({ item, index, onOpenItem }: { item: Dict; index: number; onOpenItem?: (item: Dict) => void }) {
  const title = text(item.title || item.name || item.id || `条目 ${index + 1}`);
  const excerpt = text(item.excerpt || item.summary || item.subtitle || item.description);
  const source = text(item.source_name || item.source || item.category);
  const author = text(item.author || item.authors);
  const published = text(item.published_at || item.created_at);
  const commentCount = Number(item.comment_count ?? 0);
  const score = Number(item.score ?? 0);
  const url = safeHref(item.url);
  const tags = Array.isArray(item.tags) ? item.tags.map(text).filter(Boolean).slice(0, 5) : [];

  return (
    <article className="rounded-[11px] border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3.5 transition-colors hover:border-[var(--border-line)]">
      <div className="flex items-start gap-3">
        <span className="mt-0.5 inline-flex h-6 min-w-6 shrink-0 items-center justify-center rounded-full bg-[var(--bg-soft)] font-mono text-[12px] text-[var(--text-muted)]">
          {index + 1}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-3">
            <h3 className="text-[15px] font-medium leading-[1.5] text-[var(--text-primary)]">
              {onOpenItem ? (
                <button type="button" onClick={() => onOpenItem(item)}
                  className="cursor-pointer text-left hover:text-[var(--focus-ring)] hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--focus-ring)]">{title}</button>
              ) : url ? (
                <ExternalLink
                  href={url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-start gap-1.5 hover:text-[var(--focus-ring)]"
                >
                  <span>{title}</span>
                  <ExternalLinkIcon className="mt-1 h-3 w-3 shrink-0" aria-hidden="true" />
                </ExternalLink>
              ) : (
                title
              )}
            </h3>
            {onOpenItem && url ? <ExternalLink href={url} aria-label={`打开原文：${title}`} title="在浏览器打开原文"
              className="shrink-0 rounded p-2 text-[var(--text-muted)] hover:bg-[var(--hover-fill)]">
              <ExternalLinkIcon className="h-4 w-4" aria-hidden="true" />
            </ExternalLink> : null}
          </div>
          {excerpt ? (
            <p className="mt-1 line-clamp-3 text-[13.5px] leading-[1.55] text-[var(--text-muted)]">
              {excerpt}
            </p>
          ) : null}
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12.5px] text-[var(--text-subtle)]">
            {source ? <span>{source}</span> : null}
            {author ? <span>{author}</span> : null}
            {published ? <span>{published}</span> : null}
            {commentCount > 0 ? (
              <span className="inline-flex items-center gap-1">
                <MessageSquare className="h-3 w-3" aria-hidden="true" />
                {commentCount}
              </span>
            ) : null}
            {score > 0 ? (
              <span className="inline-flex items-center gap-1">
                <Star className="h-3 w-3" aria-hidden="true" />
                {score}
              </span>
            ) : null}
          </div>
          {tags.length > 0 ? (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {tags.map((tag) => (
                <span
                  key={tag}
                  className="rounded-full bg-[var(--bg-soft)] px-2 py-0.5 text-[12px] text-[var(--text-muted)]"
                >
                  {tag}
                </span>
              ))}
            </div>
          ) : null}
        </div>
      </div>
    </article>
  );
}

function ArticleView({ article }: { article: Dict }) {
  const title = text(article.title || article.name);
  const subtitle = text(article.subtitle || article.excerpt || article.summary_zh);
  const author = text(article.author || article.authors);
  const published = text(article.published_at);
  const blocks = asRecords(article.paragraphs ?? article.blocks);

  return (
    <article className="rounded-[12px] border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-5">
      <div className="mb-4 border-b border-[var(--border-subtle)] pb-3">
        <h2 className="text-[19px] font-semibold leading-[1.4] tracking-tight text-[var(--text-primary)]">
          {title || '文章内容'}
        </h2>
        {subtitle ? (
          <p className="mt-1.5 text-[14px] leading-[1.6] text-[var(--text-muted)]">{subtitle}</p>
        ) : null}
        <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[12.5px] text-[var(--text-subtle)]">
          {author ? <span>{author}</span> : null}
          {published ? <span>{published}</span> : null}
        </div>
      </div>
      <div className="space-y-3">
        {blocks.length === 0 ? (
          <p className="text-[14px] text-[var(--text-muted)]">正文暂未返回。</p>
        ) : (
          blocks.map((block, index) => {
            const kind = text(block.kind).toLowerCase();
            const original = text(block.original || block.text || block.content);
            const translated = text(block.translated);
            if (!original && !translated) return null;
            if (kind === 'code') {
              return (
                <pre
                  key={text(block.id) || index}
                  className="overflow-auto rounded-[9px] bg-[var(--bg-canvas)] p-3 font-mono text-[13px] leading-[1.55] text-[var(--text-primary)]"
                >
                  {original}
                </pre>
              );
            }
            return (
              <div key={text(block.id) || index} className={translated ? 'grid gap-2 md:grid-cols-2' : ''}>
                {original ? (
                  <MarkdownRenderer
                    content={original}
                    compact
                    className="text-[14px] leading-[1.72] text-[var(--text-primary)]"
                  />
                ) : null}
                {translated ? (
                  <MarkdownRenderer
                    content={translated}
                    compact
                    className="border-l border-[var(--border-subtle)] pl-3 text-[14px] leading-[1.72] text-[var(--text-secondary)]"
                  />
                ) : null}
              </div>
            );
          })
        )}
      </div>
    </article>
  );
}

function KeyValueSummary({ body }: { body: Dict }) {
  const candidates: Dict = isRecord(body.auth) ? body.auth : body;
  const entries = Object.entries(candidates)
    .filter(([key, value]) => {
      if (['items', 'article', 'chapters', 'paragraphs', 'blocks', 'comments', 'ui_schema'].includes(key)) {
        return false;
      }
      return typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean';
    })
    .slice(0, 12);
  if (entries.length === 0) return null;

  return (
    <dl className="grid gap-x-5 gap-y-2 rounded-[11px] border border-[var(--border-subtle)] bg-[var(--bg-surface)] p-3.5 sm:grid-cols-2">
      {entries.map(([key, value]) => (
        <div key={key} className="flex items-center justify-between gap-3 border-b border-[var(--border-subtle)] pb-1.5">
          <dt className="font-mono text-[12.5px] text-[var(--text-subtle)]">{key}</dt>
          <dd className="min-w-0 break-words text-right text-[13px] text-[var(--text-primary)]">
            {text(value)}
          </dd>
        </div>
      ))}
    </dl>
  );
}

export function PluginResultView({ payload, actionId, onOpenItem }: PluginResultViewProps) {
  const body = resolveBody(payload);
  const items = asRecords(body.items);
  const article = isRecord(body.article)
    ? body.article
    : Object.keys(body).some((key) => key === 'paragraphs' || key === 'blocks') && text(body.title)
      ? body
      : null;
  const chapters = asRecords(body.chapters);
  const loadingAction = actionId === 'fetch_feed' || actionId === 'fetch_items';
  const hasContent = items.length > 0 || Boolean(article) || chapters.length > 0;
  const hasSummary = Object.keys(body).length > 1;

  if (!hasContent && !loadingAction && !hasSummary) return null;

  return (
    <section data-testid="plugin-result" className="mt-4 space-y-3">
      {items.length > 0 ? (
        <div className="flex items-center justify-between gap-3">
          <h2 className="inline-flex items-center gap-2 text-[15.5px] font-semibold text-[var(--text-primary)]">
            <FileText className="h-4 w-4" aria-hidden="true" />
            内容
          </h2>
          <span className="font-mono text-[12.5px] text-[var(--text-subtle)]">
            {items.length} 条
          </span>
        </div>
      ) : null}
      {items.map((item, index) => (
        <FeedItemCard key={text(item.id) || index} item={item} index={index} onOpenItem={onOpenItem} />
      ))}
      {article ? <ArticleView article={article} /> : null}
      {chapters.length > 0 ? (
        <div className="space-y-2">
          <h2 className="text-[15.5px] font-semibold text-[var(--text-primary)]">章节</h2>
          {chapters.map((chapter, index) => (
            <div
              key={text(chapter.id) || index}
              className="rounded-[10px] border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-3.5 py-2.5 text-[13.5px] text-[var(--text-primary)]"
            >
              {text(chapter.title || chapter.name || chapter.label) || `章节 ${index + 1}`}
            </div>
          ))}
        </div>
      ) : null}
      {items.length === 0 && loadingAction ? (
        <div className="rounded-[11px] border border-[var(--border-subtle)] bg-[var(--bg-surface)] px-3.5 py-3 text-[13.5px] text-[var(--text-muted)]">
          当前栏目没有返回内容。
        </div>
      ) : null}
      <KeyValueSummary body={body} />
    </section>
  );
}

export default PluginResultView;
