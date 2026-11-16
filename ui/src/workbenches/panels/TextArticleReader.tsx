import React from 'react';
import {
  FocusButton,
  HAIRLINE_BORDER,
  focusClass,
  focusableProps,
} from '../../components/WorkbenchLayout';
import type { ArticleBlock, TextArticle } from '../../data/mock';
import type { BilingualMode } from './CommentTreePanel';

const MODE_LABELS: Record<BilingualMode, string> = {
  bilingual: '双语对照',
  zh: '仅译文',
  en: '仅原文',
};

export interface TextArticleReaderProps {
  article: TextArticle;
  bilingualMode: BilingualMode;
  activeFocusId: string;
  onFocus: (id: string) => void;
  onBilingualModeChange: (mode: BilingualMode) => void;
  onSendToBabelDoc: () => void;
  onOpenChromeApp: () => void;
  onSelectBlock: (blockId: string) => void;
}

export const TextArticleReader: React.FC<TextArticleReaderProps> = ({
  article,
  bilingualMode,
  activeFocusId,
  onFocus,
  onBilingualModeChange,
  onSendToBabelDoc,
  onOpenChromeApp,
  onSelectBlock,
}) => {
  const showEn = bilingualMode === 'bilingual' || bilingualMode === 'en';
  const showZh = bilingualMode === 'bilingual' || bilingualMode === 'zh';

  const renderBlock = (block: ArticleBlock) => {
    const blockFocusId = `text:block:${article.id}:${block.id}`;
    const isFocused = activeFocusId === blockFocusId;
    return (
      <div
        key={block.id}
        data-focused={isFocused ? 'true' : undefined}
        {...focusableProps(blockFocusId, activeFocusId, onFocus, () => onSelectBlock(block.id))}
        className={focusClass(isFocused, 'cursor-pointer rounded-lg border p-3.5 transition-colors')}
        style={{
          backgroundColor:
            block.kind === 'formula' || block.kind === 'code'
              ? 'var(--bg-soft, #F1EFEC)'
              : 'var(--bg-surface, #FCFBF9)',
          borderColor:
            block.kind === 'quote' ? 'var(--domain-text, #2260AA)' : HAIRLINE_BORDER,
          borderLeftWidth: block.kind === 'quote' ? '3px' : undefined,
        }}
      >
        <div className="mb-1.5 flex items-center justify-between font-mono text-[10px] uppercase">
          <span style={{ color: 'var(--domain-text, #2260AA)' }}>
            {block.kind}
            {block.language ? ` · ${block.language}` : ''}
          </span>
          <span style={{ color: 'var(--text-subtle, #8A847D)' }}>#{block.id}</span>
        </div>

        {showEn ? (
          block.kind === 'code' ? (
            <pre
              className="overflow-x-auto rounded border p-2.5 font-mono text-xs leading-relaxed"
              style={{ backgroundColor: 'var(--bg-canvas, #F6F5F3)', borderColor: HAIRLINE_BORDER }}
            >
              <code>{block.original}</code>
            </pre>
          ) : block.kind === 'formula' ? (
            <div
              className="rounded border px-3 py-2 font-mono text-xs"
              style={{ backgroundColor: 'var(--bg-canvas, #F6F5F3)', borderColor: HAIRLINE_BORDER }}
            >
              {block.original}
            </div>
          ) : (
            <p className={block.kind === 'heading' ? 'text-sm font-semibold' : 'text-[13.5px] leading-relaxed'}>
              {block.original}
            </p>
          )
        ) : null}

        {showZh && block.translated ? (
          <p
            className={`${showEn ? 'mt-2 border-t pt-2' : ''} text-[13.5px] leading-relaxed`}
            style={{
              borderColor: HAIRLINE_BORDER,
              color: block.kind === 'heading' ? 'var(--text-primary, #222120)' : 'var(--text-muted, #66625D)',
            }}
          >
            {block.translated}
          </p>
        ) : null}
      </div>
    );
  };

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-4 p-5">
      <header
        className="rounded-lg border p-4"
        style={{ backgroundColor: 'var(--bg-surface, #FCFBF9)', borderColor: HAIRLINE_BORDER }}
      >
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <span className="font-mono text-xs font-semibold" style={{ color: 'var(--domain-text, #2260AA)' }}>
            {article.sourceName}
          </span>
          <div className="flex flex-wrap items-center gap-1.5">
            {(['bilingual', 'zh', 'en'] as const).map((mode) => (
              <FocusButton
                key={mode}
                focusId={`text:mode:${mode}`}
                activeFocusId={activeFocusId}
                onFocus={onFocus}
                onActivate={() => onBilingualModeChange(mode)}
                selected={bilingualMode === mode}
                tone="text"
                surface="soft"
                className="px-2 py-0.5 text-[11px] font-medium"
              >
                {MODE_LABELS[mode]}
              </FocusButton>
            ))}
          </div>
        </div>

        <h2 className="text-lg leading-snug font-bold">{article.titleZh}</h2>
        <p className="mt-1 font-mono text-xs" style={{ color: 'var(--text-muted, #66625D)' }}>
          {article.title}
        </p>

        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs" style={{ color: 'var(--text-muted, #66625D)' }}>
          <span>作者：{article.author}</span>
          <span>日期：{article.publishedAt}</span>
          {article.doi ? <span className="font-mono">DOI: {article.doi}</span> : null}
        </div>

        <p
          className="mt-3 rounded border p-2.5 text-xs leading-relaxed"
          style={{ backgroundColor: 'var(--bg-soft, #F1EFEC)', borderColor: HAIRLINE_BORDER }}
        >
          {article.summaryZh}
        </p>

        <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap gap-1.5">
            {article.tags.map((tag) => (
              <span
                key={tag}
                className="rounded px-2 py-0.5 font-mono text-[10px]"
                style={{ backgroundColor: 'var(--bg-soft, #F1EFEC)', color: 'var(--text-muted, #66625D)' }}
              >
                #{tag}
              </span>
            ))}
          </div>
          <div className="flex items-center gap-1.5">
            <FocusButton
              focusId="text:action:babeldoc"
              activeFocusId={activeFocusId}
              onFocus={onFocus}
              onActivate={onSendToBabelDoc}
              surface="soft"
              className="px-2.5 py-1 text-[11px] font-medium"
            >
              转交 BabelDOC
            </FocusButton>
            <FocusButton
              focusId="text:action:chrome"
              activeFocusId={activeFocusId}
              onFocus={onFocus}
              onActivate={onOpenChromeApp}
              selected
              tone="text"
              className="px-2.5 py-1 text-[11px] font-medium"
            >
              Chrome --app 阅读
            </FocusButton>
          </div>
        </div>
      </header>

      <div className="flex flex-col gap-3">{article.blocks.map(renderBlock)}</div>
    </div>
  );
};

export default TextArticleReader;
