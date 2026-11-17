import 'katex/dist/katex.min.css';
import type { Element, ElementContent } from 'hast';
import React, { useMemo } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import rehypeKatex from 'rehype-katex';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import {
  highlightCode,
  isExternalUrl,
  prepareMarkdownSource,
  sanitizeUrl,
  type MarkdownBlockKind,
} from '../utils/markdown';

export interface MarkdownRendererProps {
  content: string;
  blockKind?: MarkdownBlockKind;
  language?: string;
  compact?: boolean;
  muted?: boolean;
  className?: string;
}

const HAIRLINE = 'var(--border-subtle, rgba(28, 27, 25, 0.055))';

function extractHastText(node: ElementContent | Element): string {
  if (node.type === 'text') {
    return node.value;
  }
  if ('children' in node && Array.isArray(node.children)) {
    return node.children.map((child) => extractHastText(child)).join('');
  }
  return '';
}

function extractCodeInfoFromPre(node?: Element): { language: string; code: string } | null {
  if (!node || !Array.isArray(node.children)) {
    return null;
  }
  const codeNode = node.children.find(
    (child): child is Element => child.type === 'element' && child.tagName === 'code',
  );
  if (!codeNode) {
    return null;
  }

  const classNames: unknown = codeNode.properties?.className;
  const classList = Array.isArray(classNames)
    ? classNames.map(String)
    : typeof classNames === 'string'
      ? classNames.split(/\s+/)
      : [];
  const langClass = classList.find((cls) => cls.startsWith('language-'));
  const language = langClass ? langClass.slice('language-'.length) : 'text';
  const rawText = extractHastText(codeNode).replace(/\n$/, '');
  return { language, code: rawText };
}

export const MarkdownRenderer: React.FC<MarkdownRendererProps> = ({
  content,
  blockKind,
  language,
  compact = false,
  muted = false,
  className = '',
}) => {
  const source = useMemo(
    () => prepareMarkdownSource(content, blockKind, language),
    [content, blockKind, language],
  );

  const components = useMemo<Components>(
    () => ({
      h1: ({ children }) => (
        <h1 className="mt-3 mb-2 text-[21px] leading-[1.35] font-bold tracking-tight first:mt-0 last:mb-0">
          {children}
        </h1>
      ),
      h2: ({ children }) => (
        <h2 className="mt-2.5 mb-2 text-[19px] leading-[1.38] font-bold tracking-tight first:mt-0 last:mb-0">
          {children}
        </h2>
      ),
      h3: ({ children }) => (
        <h3 className="mt-2 mb-1.5 text-[17px] leading-[1.42] font-semibold first:mt-0 last:mb-0">
          {children}
        </h3>
      ),
      h4: ({ children }) => (
        <h4 className="mt-2 mb-1 text-[15.5px] leading-[1.45] font-semibold first:mt-0 last:mb-0">
          {children}
        </h4>
      ),
      p: ({ children }) => (
        <p
          className={`${compact ? 'my-1 text-[13.5px] leading-[1.72]' : 'my-2 text-[16px] leading-[1.78]'} first:mt-0 last:mb-0`}
        >
          {children}
        </p>
      ),
      blockquote: ({ children }) => (
        <blockquote
          className="my-2.5 border-l-[3px] pl-4 italic first:mt-0 last:mb-0"
          style={{
            borderColor: 'var(--domain-text, #185194)',
            color: 'var(--text-secondary, #43403B)',
          }}
        >
          {children}
        </blockquote>
      ),
      ul: ({ children }) => (
        <ul
          className={`${compact ? 'my-1 text-[13.5px] leading-[1.72]' : 'my-2 text-[16px] leading-[1.78]'} list-disc space-y-1.5 pl-5 first:mt-0 last:mb-0`}
        >
          {children}
        </ul>
      ),
      ol: ({ children }) => (
        <ol
          className={`${compact ? 'my-1 text-[13.5px] leading-[1.72]' : 'my-2 text-[16px] leading-[1.78]'} list-decimal space-y-1.5 pl-5 first:mt-0 last:mb-0`}
        >
          {children}
        </ol>
      ),
      li: ({ children }) => <li className="leading-[1.78]">{children}</li>,
      table: ({ children }) => (
        <div
          className="my-3 overflow-x-auto rounded-[12px] first:mt-0 last:mb-0"
          style={{ backgroundColor: 'var(--bg-surface)', boxShadow: 'var(--shadow-page)' }}
        >
          <table className="w-full border-collapse text-left text-[13.5px] leading-[1.65]">{children}</table>
        </div>
      ),
      thead: ({ children }) => (
        <thead style={{ backgroundColor: 'var(--bg-sunken, #EDEAE3)' }}>{children}</thead>
      ),
      th: ({ children }) => (
        <th className="border-b px-3.5 py-2 font-semibold" style={{ borderColor: HAIRLINE }}>
          {children}
        </th>
      ),
      td: ({ children }) => (
        <td className="border-t px-3.5 py-2 align-top" style={{ borderColor: HAIRLINE }}>
          {children}
        </td>
      ),
      a: ({ href, children, title }) => {
        const safeHref = sanitizeUrl(href, 'link');
        if (!safeHref) {
          return (
            <span
              data-blocked-link="true"
              title="已拦截不安全链接"
              className="cursor-not-allowed underline decoration-dotted opacity-75"
              style={{ color: 'var(--status-err-fg, #AA1D33)' }}
            >
              {children}
            </span>
          );
        }
        const external = isExternalUrl(safeHref);
        return (
          <a
            href={safeHref}
            title={title}
            target={external ? '_blank' : undefined}
            rel={external ? 'noopener noreferrer' : undefined}
            className="underline underline-offset-3 hover:opacity-80"
            style={{ color: 'var(--domain-text, #185194)' }}
          >
            {children}
          </a>
        );
      },
      img: ({ src, alt, title }) => {
        const safeSrc = sanitizeUrl(src, 'image');
        if (!safeSrc) {
          return (
            <span
              data-blocked-image="true"
              className="inline-block rounded-md px-2 py-0.5 font-mono text-[12px]"
              style={{
                backgroundColor: 'var(--status-err-bg, #FCE9ED)',
                color: 'var(--status-err-fg, #AA1D33)',
              }}
            >
              [已拦截不安全图片{alt ? `: ${alt}` : ''}]
            </span>
          );
        }
        return (
          <img
            src={safeSrc}
            alt={alt ?? ''}
            title={title}
            loading="lazy"
            className="my-3 max-w-full rounded-[12px]"
            style={{ boxShadow: 'var(--shadow-soft)' }}
          />
        );
      },
      code: ({ className: codeClassName, children }) => (
        <code
          className={`rounded px-1.5 py-0.5 font-mono text-[0.86em] ${codeClassName ?? ''}`}
          style={{
            backgroundColor: 'var(--bg-soft, #E6E2DA)',
          }}
        >
          {children}
        </code>
      ),
      pre: ({ node, children }) => {
        const info = extractCodeInfoFromPre(node);
        if (!info) {
          return (
            <pre
              className="my-2.5 overflow-x-auto rounded-[12px] p-4 font-mono text-[13px] leading-[1.68]"
              style={{ backgroundColor: 'var(--bg-sunken, #EDEAE3)' }}
            >
              {children}
            </pre>
          );
        }
        const { resolvedLanguage, nodes } = highlightCode(info.code, info.language);
        return (
          <div
            className="omni-md-code my-2.5 overflow-hidden rounded-[12px] first:mt-0 last:mb-0"
            style={{ backgroundColor: 'var(--bg-sunken, #EDEAE3)' }}
          >
            <div
              className="flex items-center justify-between border-b px-4 py-1.5 font-mono text-[12px] uppercase"
              style={{
                borderColor: HAIRLINE,
                color: 'var(--text-muted, #59544D)',
              }}
            >
              <span data-testid="markdown-code-lang" data-code-lang={resolvedLanguage}>
                {resolvedLanguage}
              </span>
            </div>
            <pre className="m-0 overflow-x-auto p-4 font-mono text-[13px] leading-[1.68]">
              <code className={`hljs language-${resolvedLanguage}`}>{nodes}</code>
            </pre>
          </div>
        );
      },
    }),
    [compact],
  );

  return (
    <div
      data-testid="markdown-renderer"
      className={`omni-markdown min-w-0 break-words ${className}`}
      style={{
        color: muted ? 'var(--text-secondary, #43403B)' : 'var(--text-primary, #1C1B19)',
      }}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[[rehypeKatex, { strict: 'ignore', throwOnError: false }]]}
        urlTransform={(url, key) => sanitizeUrl(url, key === 'src' ? 'image' : 'link') ?? ''}
        components={components}
      >
        {source}
      </ReactMarkdown>
    </div>
  );
};

export default MarkdownRenderer;
