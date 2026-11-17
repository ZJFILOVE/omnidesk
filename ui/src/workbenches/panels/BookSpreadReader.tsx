import React, { useRef } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import MarkdownRenderer from '../../components/MarkdownRenderer';
import { isSafeImageUrl } from '../../components/MediaCover';
import SelectionBubble, { type SelectionActionEvent } from '../../components/SelectionBubble';
import { FocusButton, HAIRLINE_BORDER } from '../../components/WorkbenchLayout';
import type { BookItem, BookSpread, VocabCard } from '../../data/mock';

export interface BookSpreadReaderProps {
  book: BookItem;
  spread: BookSpread;
  spreadIndex: number;
  totalPages?: number;
  activeVocab?: VocabCard;
  activeFocusId: string;
  onFocus: (id: string) => void;
  onTurnPage: (delta: number) => void;
  onLookupTerm: (term: string) => void;
  onLookupEtymology?: (text: string, contextSentence: string) => void;
  onParseClause?: (text: string, contextSentence: string) => void;
  onAddToVocab?: (text: string, contextSentence: string) => void;
  onSendToCompanion?: (text: string, contextSentence: string) => void;
  onCopySelection?: (text: string) => void;
  onSelectionAction?: (event: SelectionActionEvent) => void;
}

export const BookSpreadReader: React.FC<BookSpreadReaderProps> = ({
  book,
  spread,
  spreadIndex,
  totalPages,
  activeVocab,
  activeFocusId,
  onFocus,
  onTurnPage,
  onLookupTerm,
  onLookupEtymology,
  onParseClause,
  onAddToVocab,
  onSendToCompanion,
  onCopySelection,
  onSelectionAction,
}) => {
  const originalColumnRef = useRef<HTMLElement | null>(null);

  return (
    <div className="flex flex-1 flex-col gap-5 p-6">
      {/* Quiet Book Folio Bar */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h2 className="omni-editorial-title truncate text-[23px] font-bold text-[var(--text-primary)]">
            {book.title}
          </h2>
          <div className="mt-0.5 text-[14.5px] text-[var(--text-muted)]">
            {spread.chapterTitle} · {book.author}
          </div>
        </div>

        <div className="flex items-center gap-2">
          <FocusButton
            focusId="book:nav:prev"
            activeFocusId={activeFocusId}
            onFocus={onFocus}
            onActivate={() => onTurnPage(-1)}
            disabled={totalPages !== undefined ? spread.page <= 1 : spreadIndex <= 0}
            surface="surface"
            className="inline-flex items-center gap-1 px-3 py-1.5 text-[14px] font-medium"
          >
            <ChevronLeft className="h-3.5 w-3.5" aria-hidden="true" />
            <span>上一跨页</span>
          </FocusButton>
          <span className="px-2 font-mono text-[14px] text-[var(--text-muted)]">
            {totalPages !== undefined ? `p.${spread.page} / ${totalPages}` : `p.${spread.page} (${spreadIndex + 1}/${Math.max(1, book.spreads.length)})`}
          </span>
          <FocusButton
            focusId="book:nav:next"
            activeFocusId={activeFocusId}
            onFocus={onFocus}
            onActivate={() => onTurnPage(1)}
            disabled={totalPages !== undefined ? spread.page >= totalPages : spreadIndex >= book.spreads.length - 1}
            surface="surface"
            className="inline-flex items-center gap-1 px-3 py-1.5 text-[14px] font-medium"
          >
            <span>下一跨页</span>
            <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />
          </FocusButton>
        </div>
      </div>

      {/* Maximized Open-Book Dual-Page Folio Stage */}
      <div className={`grid flex-1 grid-cols-1 gap-4 ${spread.rightParagraphs.length ? "@[620px]/workbench:grid-cols-2" : ""}`}>
        <section
          ref={originalColumnRef}
          data-testid="book-original-column"
          data-selection-source="original"
          className="omni-book-gutter-left flex flex-col gap-5 rounded-[16px] p-6 sm:p-8"
          style={{ backgroundColor: 'var(--bg-surface)' }}
        >
          <div
            className="flex items-center justify-between border-b pb-3 text-[14px] font-semibold"
            style={{ borderColor: HAIRLINE_BORDER, color: 'var(--domain-book)' }}
          >
            <span>{spread.leftTitle}</span>
            <span className="font-mono text-[13.5px] text-[var(--text-subtle)]">ORIGINAL</span>
          </div>
          {spread.leftParagraphs.map((para) => (
            <div key={para.id} className="flex flex-col gap-3">
              {para.imageUrl && isSafeImageUrl(para.imageUrl) ? (
                <img src={para.imageUrl} alt={para.text.replace(/^\[Image: |\]$/g, '')} className="max-h-[70vh] max-w-full self-center object-contain" />
              ) : <MarkdownRenderer content={para.text} />}
              {para.keyTerms?.length ? (
                <div className="flex flex-wrap items-center gap-1.5 pt-0.5">
                  <span className="text-[13.5px] text-[var(--text-subtle)]">词元划查：</span>
                  {para.keyTerms.map((term) => (
                    <FocusButton
                      key={`${para.id}:${term}`}
                      focusId={`book:term:${para.id}:${term}`}
                      activeFocusId={activeFocusId}
                      onFocus={onFocus}
                      onActivate={() => onLookupTerm(term)}
                      selected={activeVocab?.word.toLowerCase() === term.toLowerCase()}
                      tone="book"
                      surface="soft"
                      className="px-2.5 py-0.5 font-mono text-[13.5px] font-medium"
                    >
                      {term}
                    </FocusButton>
                  ))}
                </div>
              ) : null}
            </div>
          ))}
        </section>

        {spread.rightParagraphs.length > 0 ? <section
          data-testid="book-translation-column"
          data-selection-source="translation"
          className="flex flex-col gap-5 rounded-[16px] p-6 sm:p-8"
          style={{
            backgroundColor: 'var(--bg-surface)',
            boxShadow: 'var(--shadow-page)',
          }}
        >
          <div
            className="flex items-center justify-between border-b pb-3 text-[14px] font-semibold"
            style={{ borderColor: HAIRLINE_BORDER, color: 'var(--domain-book)' }}
          >
            <span>{spread.rightTitle}</span>
            <span className="font-mono text-[13.5px] text-[var(--text-subtle)]">译文对照</span>
          </div>
          {spread.rightParagraphs.map((para) => (
            para.imageUrl && isSafeImageUrl(para.imageUrl)
              ? <img key={para.id} src={para.imageUrl} alt="译文页面" className="max-w-full self-center object-contain" />
              : <MarkdownRenderer key={para.id} content={para.text} muted />
          ))}
          {spread.rightParagraphs.length === 0 ? <p className="text-[var(--text-muted)]">本页暂无译文。</p> : null}
        </section> : null}
      </div>

      <SelectionBubble
        containerRef={originalColumnRef}
        onLookupEtymology={onLookupEtymology}
        onParseClause={onParseClause}
        onAddToVocab={onAddToVocab}
        onSendToCompanion={onSendToCompanion}
        onCopy={onCopySelection}
        onAction={onSelectionAction}
      />
    </div>
  );
};

export default BookSpreadReader;
