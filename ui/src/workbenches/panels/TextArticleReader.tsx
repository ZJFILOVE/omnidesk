import React, { useRef, useState } from 'react';
import { TranslationButton } from '../../components/TranslationContext';
import MarkdownRenderer from '../../components/MarkdownRenderer';
import { ArticleImage } from '../../components/ArticleImage';
import SelectionBubble, { type SelectionActionEvent } from '../../components/SelectionBubble';
import {
  FocusButton,
  HAIRLINE_BORDER,
  focusClass,
  focusableProps,
} from '../../components/WorkbenchLayout';
import {
  EditorialCallout,
  SegmentedPills,
  TagChip,
} from '../../components/WorkbenchPrimitives';
import type { ArticleBlock, TextArticle } from '../../data/mock';
import type { BilingualMode } from './CommentTreePanel';
import { ExternalLink } from '../../components/ExternalLink';
import { resolveCommunityLink } from '../../utils/communityLinks';

const BILINGUAL_OPTIONS: { id: BilingualMode; label: string }[] = [
  { id: 'bilingual', label: '双语对照' },
  { id: 'zh', label: '仅译文' },
  { id: 'en', label: '仅原文' },
];

export interface TextArticleReaderProps {
  article: TextArticle;
  bilingualMode: BilingualMode;
  activeFocusId: string;
  onFocus: (id: string) => void;
  onBilingualModeChange: (mode: BilingualMode) => void;
  onSendToBabelDoc: () => void;
  onOpenChromeApp: () => void;
  onSelectBlock: (blockId: string) => void;
  onSelectionAction?: (event: SelectionActionEvent) => void;
  onLookupEtymology?: (text: string, contextSentence: string) => void;
  onParseClause?: (text: string, contextSentence: string) => void;
  onAddToVocab?: (text: string, contextSentence: string) => void;
  onSendToCompanion?: (text: string, contextSentence: string) => void;
  onCopySelection?: (text: string) => void;
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
  onSelectionAction,
  onLookupEtymology,
  onParseClause,
  onAddToVocab,
  onSendToCompanion,
  onCopySelection,
}) => {
  const showEn = bilingualMode === 'bilingual' || bilingualMode === 'en';
  const showZh = bilingualMode === 'bilingual' || bilingualMode === 'zh';
  const blocksRef = useRef<HTMLDivElement | null>(null);
  const [translations, setTranslations] = useState<Record<string, Record<string, string>>>({});

  const renderBlock = (block: ArticleBlock) => {
    block = { ...block, translated: translations[article.id]?.[block.id] ?? block.translated };
    if (block.kind === 'image' && block.imageUrl) {
      return <ArticleImage key={block.id} url={block.imageUrl} alt={block.alt} caption={block.caption} />;
    }
    const blockFocusId = `text:block:${article.id}:${block.id}`;
    const isFocused = activeFocusId === blockFocusId;
    const translatedKind =
      block.kind === 'code' || block.kind === 'formula' ? 'paragraph' : block.kind;
    const isTonalBlock = block.kind === 'formula' || block.kind === 'code';

    return (
      <div
        key={block.id}
        data-block-id={block.id}
        data-block-kind={block.kind}
        data-focused={isFocused ? 'true' : undefined}
        {...focusableProps(blockFocusId, activeFocusId, onFocus, () => {
          const hasSelection =
            typeof window !== 'undefined' && Boolean(window.getSelection()?.toString().trim());
          if (!hasSelection) {
            onSelectBlock(block.id);
          }
        })}
        className={focusClass(
          isFocused,
          `cursor-pointer rounded-[12px] transition-colors ${
            isTonalBlock
              ? 'px-4 py-3.5'
              : block.kind === 'quote'
                ? 'border-l-[3px] py-2 pr-3 pl-4'
                : block.kind === 'heading'
                  ? 'mt-8 border-t border-[var(--border-subtle)] px-3 pt-6 pb-3'
                  : 'px-3 py-2.5 hover:bg-[var(--hover-fill)]'
          }`,
        )}
        style={{
          backgroundColor: isTonalBlock ? 'var(--bg-sunken)' : undefined,
          borderColor: block.kind === 'quote' ? 'var(--domain-text)' : undefined,
        }}
      >
        {showEn ? (
          <div data-selection-source="original">
            <MarkdownRenderer
              content={block.original}
              blockKind={block.kind}
              language={block.language}
            />
          </div>
        ) : null}

        {showZh && block.translated ? (
          <div
            data-selection-source="translation"
            className={showEn ? 'mt-3 border-t pt-3' : ''}
            style={{ borderColor: HAIRLINE_BORDER }}
          >
            <MarkdownRenderer
              content={block.translated}
              blockKind={translatedKind}
              muted={block.kind !== 'heading'}
            />
          </div>
        ) : null}
      </div>
    );
  };

  return (
    <article className="flex w-full min-w-0 flex-col gap-7 px-4 py-7 sm:px-6">
      {/* Magazine / Journal Editorial Masthead */}
      <header className="flex flex-col gap-4 border-b pb-6" style={{ borderColor: HAIRLINE_BORDER }}>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap items-center gap-2 text-[14px]">
            <span className="font-semibold tracking-wide uppercase" style={{ color: 'var(--domain-text)' }}>
              {article.sourceName}
            </span>
            <span className="text-[var(--text-subtle)]">·</span>
            <span className="text-[var(--text-muted)]">{article.publishedAt}</span>
            <span className="text-[var(--text-subtle)]">·</span>
            <span className="text-[var(--text-muted)]">{article.readTime}</span>
          </div>

          <SegmentedPills
            options={BILINGUAL_OPTIONS}
            value={bilingualMode}
            onChange={onBilingualModeChange}
            focusPrefix="text:mode:"
            activeFocusId={activeFocusId}
            onFocus={onFocus}
            tone="text"
            ariaLabel="双语阅读模式"
          />
        </div>

        <div className="pt-1">
          <h2 className="omni-editorial-title text-[26px] leading-[1.32] font-bold text-[var(--text-primary)] sm:text-[30px]">
            {article.titleZh}
          </h2>
          {article.title !== article.titleZh ? <p className="mt-2 font-serif text-[17px] leading-[1.55] italic text-[var(--text-muted)]">
            {article.title}
          </p> : null}
        </div>

        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[14.5px] text-[var(--text-muted)]">
          <span>作者：{article.author}</span>
          {article.journalName ? <span>刊物：{article.journalName}</span> : null}
          {article.impactScore ? <span>{article.impactScore}</span> : null}
          {article.doi ? (
            <span className="font-mono text-[13.5px] text-[var(--text-subtle)]">DOI {article.doi}</span>
          ) : null}
        </div>

        <EditorialCallout domain="text">
          <MarkdownRenderer content={article.summaryZh} compact />
        </EditorialCallout>

        <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
          <div className="flex flex-wrap gap-1.5">
            {article.tags.map((tag) => (
              <TagChip key={tag} tone="neutral" mono>
                #{tag}
              </TagChip>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <TranslationButton label={translations[article.id] ? '重新翻译文章' : '翻译文章'} request={{ title: article.title,
              segments: article.blocks.filter(block => block.original.trim() && !['image', 'code', 'formula'].includes(block.kind)).map(block => ({ id: block.id, text: block.original })),
              onTranslated: results => { setTranslations(previous => ({ ...previous, [article.id]: Object.fromEntries(results.map(result => [result.id, result.translation])) })); onBilingualModeChange('bilingual'); },
            }} />
            {article.url && resolveCommunityLink(article.url) ? <ExternalLink href={article.url}
              className="inline-flex min-h-11 items-center rounded-lg border border-[var(--border-line)] px-3 py-1.5 text-[14px] font-medium text-[var(--domain-text)]">
              打开原帖与讨论
            </ExternalLink> : null}
            <FocusButton
              focusId="text:action:babeldoc"
              activeFocusId={activeFocusId}
              onFocus={onFocus}
              onActivate={onSendToBabelDoc}
              surface="soft"
              className="px-3 py-1.5 text-[14px] font-medium"
            >
              转交 BabelDOC
            </FocusButton>
            {article.url ? <ExternalLink href={article.url} externalOnly
              className="inline-flex min-h-11 items-center rounded-lg bg-[var(--bg-soft)] px-3 py-1.5 text-[14px] font-medium text-[var(--domain-text)]">在 Chrome 打开</ExternalLink> : <FocusButton
              focusId="text:action:chrome"
              activeFocusId={activeFocusId}
              onFocus={onFocus}
              onActivate={onOpenChromeApp}
              selected
              tone="text"
              className="px-3 py-1.5 text-[14px] font-medium"
            >
              Chrome --app 阅读
            </FocusButton>}
          </div>
        </div>
      </header>

      {/* Let the reading surface use the available workbench width. */}
      <div ref={blocksRef} data-testid="text-article-blocks" className="flex flex-col gap-3.5">
        {article.blocks.map(renderBlock)}
      </div>

      <SelectionBubble
        containerRef={blocksRef}
        allowedSelector='[data-selection-source="original"]'
        disabled={!showEn}
        onLookupEtymology={onLookupEtymology}
        onParseClause={onParseClause}
        onAddToVocab={onAddToVocab}
        onSendToCompanion={onSendToCompanion}
        onCopy={onCopySelection}
        onAction={onSelectionAction}
      />
    </article>
  );
};

export default TextArticleReader;
