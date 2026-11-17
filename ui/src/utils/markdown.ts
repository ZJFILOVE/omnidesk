import hljs from 'highlight.js/lib/common';
import { createElement, Fragment, type ReactNode } from 'react';

export type MarkdownBlockKind =
  | 'heading'
  | 'paragraph'
  | 'quote'
  | 'code'
  | 'formula'
  | 'image';

const CONTROL_AND_SPACE_RE = /[\u0000-\u0020\u007F]+/g;
const SCHEME_RE = /^([a-zA-Z][a-zA-Z0-9+.-]*):/;
const ALLOWED_LINK_SCHEMES = new Set(['http', 'https', 'mailto', 'tel']);
const SAFE_DATA_IMAGE_RE = /^data:image\/(png|jpeg|jpg|gif|webp|avif);base64,[a-z0-9+/=]+$/i;
const EXTERNAL_URL_RE = /^(https?:)?\/\//i;

export function sanitizeUrl(rawUrl: string | undefined | null, kind: 'link' | 'image'): string | null {
  if (!rawUrl) {
    return null;
  }
  const trimmed = rawUrl.trim();
  if (!trimmed) {
    return null;
  }

  // Strip ASCII control chars/whitespace before checking scheme so obfuscated
  // payloads like "java\tscript:alert(1)" are caught deterministically.
  const normalizedForScheme = trimmed.replace(CONTROL_AND_SPACE_RE, '');
  const schemeMatch = SCHEME_RE.exec(normalizedForScheme);
  if (!schemeMatch) {
    return trimmed;
  }

  const scheme = schemeMatch[1].toLowerCase();
  if (kind === 'link') {
    return ALLOWED_LINK_SCHEMES.has(scheme) ? trimmed : null;
  }

  if (scheme === 'http' || scheme === 'https') {
    return trimmed;
  }
  if (scheme === 'data') {
    return SAFE_DATA_IMAGE_RE.test(normalizedForScheme) ? trimmed : null;
  }
  return null;
}

export function isExternalUrl(url: string): boolean {
  return EXTERNAL_URL_RE.test(url.trim());
}

function getFenceForCode(code: string): string {
  const matches = code.match(/`+/g);
  const maxRun = matches ? Math.max(...matches.map((m) => m.length)) : 0;
  return '`'.repeat(Math.max(3, maxRun + 1));
}

export function prepareMarkdownSource(
  content: string,
  blockKind?: MarkdownBlockKind,
  language?: string,
): string {
  const raw = content ?? '';
  const trimmed = raw.trim();

  if (!blockKind || blockKind === 'paragraph') {
    return raw;
  }

  if (blockKind === 'heading') {
    if (!trimmed) return '';
    if (/^#{1,6}(\s|$)/.test(trimmed)) {
      return trimmed;
    }
    return `### ${trimmed}`;
  }

  if (blockKind === 'quote') {
    if (!trimmed) return '';
    if (trimmed.startsWith('>')) {
      return trimmed;
    }
    return raw
      .split('\n')
      .map((line) => (line.trim() ? `> ${line}` : '>'))
      .join('\n');
  }

  if (blockKind === 'code') {
    if (/^(`{3,}|~{3,})/.test(trimmed)) {
      return trimmed;
    }
    const fence = getFenceForCode(raw);
    const lang = (language ?? '').trim() || 'text';
    return `${fence}${lang}\n${raw}\n${fence}`;
  }

  if (blockKind === 'formula') {
    if (!trimmed) return '';
    if (/^```math\b/i.test(trimmed)) {
      return trimmed;
    }
    if (trimmed.startsWith('$$') && trimmed.endsWith('$$') && trimmed.length > 4) {
      const inner = trimmed.slice(2, -2).trim();
      return `$$\n${inner}\n$$`;
    }
    if (trimmed.startsWith('\\[') && trimmed.endsWith('\\]') && trimmed.length > 4) {
      const inner = trimmed.slice(2, -2).trim();
      return `$$\n${inner}\n$$`;
    }
    return `$$\n${trimmed}\n$$`;
  }

  if (blockKind === 'image') {
    if (/^!\[.*\]\(.*\)/.test(trimmed) || trimmed.startsWith('<img')) {
      return trimmed;
    }
    if (!/\s/.test(trimmed)) {
      return `![](${trimmed})`;
    }
    return raw;
  }

  return raw;
}

const ENTITY_MAP: Record<string, string> = {
  '&amp;': '&',
  '&lt;': '<',
  '&gt;': '>',
  '&quot;': '"',
  '&#x27;': "'",
  '&#39;': "'",
};

function decodeHtmlEntities(text: string): string {
  return text.replace(/&(amp|lt|gt|quot|#x27|#39);/g, (entity) => ENTITY_MAP[entity] ?? entity);
}

const TOKEN_TAG_RE = /<span class="([^"]*)">|<\/span>/g;

function parseHighlightHtmlToNodes(html: string): ReactNode[] {
  const stack: { className?: string; children: ReactNode[] }[] = [{ children: [] }];
  let lastIndex = 0;
  let keyCounter = 0;

  TOKEN_TAG_RE.lastIndex = 0;
  let match: RegExpExecArray | null = TOKEN_TAG_RE.exec(html);

  while (match !== null) {
    if (match.index > lastIndex) {
      const chunk = decodeHtmlEntities(html.slice(lastIndex, match.index));
      if (chunk) {
        stack[stack.length - 1].children.push(chunk);
      }
    }

    if (match[0] === '</span>') {
      if (stack.length > 1) {
        const finished = stack.pop()!;
        const el = createElement(
          'span',
          { key: `hljs-${keyCounter++}`, className: finished.className },
          ...finished.children,
        );
        stack[stack.length - 1].children.push(el);
      }
    } else {
      stack.push({ className: match[1], children: [] });
    }

    lastIndex = TOKEN_TAG_RE.lastIndex;
    match = TOKEN_TAG_RE.exec(html);
  }

  if (lastIndex < html.length) {
    const tail = decodeHtmlEntities(html.slice(lastIndex));
    if (tail) {
      stack[stack.length - 1].children.push(tail);
    }
  }

  while (stack.length > 1) {
    const unclosed = stack.pop()!;
    stack[stack.length - 1].children.push(
      createElement(
        'span',
        { key: `hljs-${keyCounter++}`, className: unclosed.className },
        ...unclosed.children,
      ),
    );
  }

  return stack[0].children;
}

const LANGUAGE_ALIASES: Record<string, string> = {
  py: 'python',
  ts: 'typescript',
  tsx: 'typescript',
  js: 'javascript',
  jsx: 'javascript',
  sh: 'bash',
  zsh: 'bash',
  shell: 'bash',
  yml: 'yaml',
  toml: 'ini',
  rs: 'rust',
  md: 'markdown',
};

export function highlightCode(
  code: string,
  language?: string,
): { resolvedLanguage: string; nodes: ReactNode } {
  const rawLang = (language ?? '').trim().toLowerCase();
  const normalizedLang = LANGUAGE_ALIASES[rawLang] ?? rawLang;

  if (
    !normalizedLang ||
    normalizedLang === 'text' ||
    normalizedLang === 'plaintext' ||
    !hljs.getLanguage(normalizedLang)
  ) {
    return {
      resolvedLanguage: rawLang || 'text',
      nodes: createElement(Fragment, null, code),
    };
  }

  const result = hljs.highlight(code, { language: normalizedLang, ignoreIllegals: true });
  return {
    resolvedLanguage: rawLang || normalizedLang,
    nodes: createElement(Fragment, null, ...parseHighlightHtmlToNodes(result.value)),
  };
}
