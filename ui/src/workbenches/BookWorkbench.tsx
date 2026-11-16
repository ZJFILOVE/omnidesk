import React, { useEffect, useMemo, useState } from 'react';
import WorkbenchLayout, {
  FocusButton,
  HAIRLINE_BORDER,
  useWorkbenchFocus,
  type WorkbenchViewProps,
} from '../components/WorkbenchLayout';
import {
  BOOK_ITEMS,
  BOOK_SEARCH_CATALOG,
  INITIAL_VOCAB_CARDS,
  type BookCatalogItem,
  type BookItem,
  type VocabCard,
} from '../data/mock';
import BookShelfSidebar from './panels/BookShelfSidebar';
import VocabSm2Panel, { type Sm2Grade } from './panels/VocabSm2Panel';

function applySm2(card: VocabCard, grade: Sm2Grade): VocabCard {
  const qualityMap: Record<Sm2Grade, number> = { again: 1, hard: 3, good: 4, easy: 5 };
  const q = qualityMap[grade];
  const nextEase = Math.max(
    1.3,
    Number((card.easeFactor + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))).toFixed(2)),
  );
  let nextInterval = 1;
  if (grade === 'hard') nextInterval = Math.max(1, Math.round(card.intervalDays * 1.2));
  else if (grade === 'good')
    nextInterval = card.intervalDays <= 1 ? 3 : Math.round(card.intervalDays * nextEase);
  else if (grade === 'easy')
    nextInterval = Math.max(4, Math.round(card.intervalDays * nextEase * 1.3));
  return {
    ...card,
    intervalDays: nextInterval,
    easeFactor: nextEase,
    dueLabel: grade === 'again' ? '10 分钟后再复习' : `${nextInterval} 天后到期`,
  };
}

export const BookWorkbench: React.FC<WorkbenchViewProps> = ({
  focusId,
  onFocus,
  focusedItemId,
  onContextChange,
  onNavigateWorkbench,
}) => {
  const [activeFocusId, emitFocus] = useWorkbenchFocus(
    `book:shelf:${BOOK_ITEMS[0]?.id ?? ''}`,
    focusId,
    onFocus,
    focusedItemId,
  );
  const [books, setBooks] = useState<BookItem[]>(BOOK_ITEMS);
  const [leftTab, setLeftTab] = useState<'shelf' | 'search'>('shelf');
  const [catalogQuery, setCatalogQuery] = useState<string>('');
  const [selectedBookId, setSelectedBookId] = useState<string>(BOOK_ITEMS[0]?.id ?? '');
  const [spreadIndexByBook, setSpreadIndexByBook] = useState<Record<string, number>>({});
  const [glossaryLocked, setGlossaryLocked] = useState<boolean>(true);
  const [vocabCards, setVocabCards] = useState<VocabCard[]>(INITIAL_VOCAB_CARDS);
  const [activeVocabId, setActiveVocabId] = useState<string>(INITIAL_VOCAB_CARDS[0]?.id ?? '');
  const [vocabRevealed, setVocabRevealed] = useState<boolean>(true);
  const [statusMessage, setStatusMessage] = useState<string>('BabelDOC 双栏对照 + SM-2 生词台已同步');

  const selectedBook = useMemo(
    () => books.find((b) => b.id === selectedBookId) ?? books[0],
    [books, selectedBookId],
  );
  const spreadIndex = selectedBook
    ? Math.min(
        Math.max(0, spreadIndexByBook[selectedBook.id] ?? 0),
        Math.max(0, selectedBook.spreads.length - 1),
      )
    : 0;
  const activeSpread = selectedBook?.spreads[spreadIndex];
  const activeVocab = useMemo(
    () => vocabCards.find((v) => v.id === activeVocabId) ?? vocabCards[0],
    [vocabCards, activeVocabId],
  );
  const filteredCatalog = useMemo(() => {
    const q = catalogQuery.trim().toLowerCase();
    if (!q) return BOOK_SEARCH_CATALOG;
    return BOOK_SEARCH_CATALOG.filter(
      (item) =>
        item.title.toLowerCase().includes(q) ||
        item.author.toLowerCase().includes(q) ||
        item.summary.toLowerCase().includes(q),
    );
  }, [catalogQuery]);

  useEffect(() => {
    if (!onContextChange) return;
    if (!selectedBook) {
      onContextChange(null);
      return;
    }
    onContextChange({
      domain: 'book',
      workbench: 'book',
      itemId: selectedBook.id,
      title: selectedBook.title,
      subtitle: selectedBook.author,
      sourceId: selectedBook.sourcePlugin,
      kind: selectedBook.format,
    });
  }, [onContextChange, selectedBook]);

  const handleTurnPage = (delta: number) => {
    if (!selectedBook) return;
    const maxIdx = Math.max(0, selectedBook.spreads.length - 1);
    const nextIdx = Math.max(0, Math.min(maxIdx, spreadIndex + delta));
    setSpreadIndexByBook((prev) => ({ ...prev, [selectedBook.id]: nextIdx }));
    setStatusMessage(`已翻页至第 ${selectedBook.spreads[nextIdx]?.page ?? selectedBook.currentPage} 页`);
  };

  const handleLookupTerm = (term: string) => {
    const matched = vocabCards.find((c) => c.word.toLowerCase() === term.toLowerCase());
    if (matched) {
      setActiveVocabId(matched.id);
      setVocabRevealed(true);
      setStatusMessage(`已定位词元「${matched.word}」— ${matched.meaningZh}`);
    } else {
      setStatusMessage(`已记录划词「${term}」（等待 BabelDOC 术语库解析）`);
    }
  };

  const handleSm2Review = (grade: Sm2Grade) => {
    if (!activeVocab) return;
    const updated = applySm2(activeVocab, grade);
    const nextList = vocabCards.map((c) => (c.id === updated.id ? updated : c));
    setVocabCards(nextList);
    const nextCard = nextList[(nextList.findIndex((c) => c.id === updated.id) + 1) % nextList.length];
    if (nextCard) {
      setActiveVocabId(nextCard.id);
      setVocabRevealed(false);
    }
    setStatusMessage(`SM-2 已更新「${updated.word}」：间隔 ${updated.intervalDays} 天（EF ${updated.easeFactor}）`);
  };

  const handleImportCatalogBook = (entry: BookCatalogItem) => {
    const existing = books.find((b) => b.id === `imported-${entry.id}`);
    if (existing) {
      setSelectedBookId(existing.id);
      setLeftTab('shelf');
      setStatusMessage(`「${entry.title}」已在书架中`);
      return;
    }
    const newBook: BookItem = {
      id: `imported-${entry.id}`,
      title: entry.title,
      author: entry.author,
      format: entry.format,
      language: entry.language,
      progressPct: 0,
      totalPages: 420,
      currentPage: 1,
      coverGradient: entry.coverGradient,
      badge: entry.format === 'pdf' ? 'BabelDOC 队列' : 'EPUB 已导入',
      sourcePlugin: entry.format === 'pdf' ? 'plugin-pdf-babeldoc' : 'plugin-ebook',
      bilingualReady: true,
      spreads: [
        {
          page: 1,
          chapterTitle: 'Chapter 1 · Imported Preview',
          leftTitle: `${entry.format.toUpperCase()} Original`,
          leftParagraphs: [
            {
              id: `${entry.id}-p1`,
              text: `${entry.summary} Strict provenance tracks every imported volume.`,
              keyTerms: ['provenance'],
            },
          ],
          rightTitle: '伴读预览对照',
          rightParagraphs: [
            {
              id: `${entry.id}-p1-zh`,
              text: `已由 ${entry.sourceName} 下载并送入本地书架（${entry.sizeLabel}），支持双语对照与 SM-2 划词。`,
            },
          ],
        },
      ],
    };
    setBooks((prev) => [newBook, ...prev]);
    setSelectedBookId(newBook.id);
    setLeftTab('shelf');
    setStatusMessage(`已从 ${entry.sourceName} 导入「${entry.title}」至书架`);
  };

  return (
    <WorkbenchLayout
      domain="book"
      title="书架、BabelDOC 双栏 PDF 与 SM-2 生词台 · BookWorkbench"
      subtitle="2:3 书架 · 左右基线对齐双栏对照 · 划词词源与美剧原声切片跳播"
      badge="Book · Ctrl+3"
      headerActions={
        <div className="flex items-center gap-2">
          <FocusButton
            focusId="book:action:glossary-lock"
            activeFocusId={activeFocusId}
            onFocus={emitFocus}
            onActivate={() => setGlossaryLocked((v) => !v)}
            selected={glossaryLocked}
            tone="ok"
            surface="soft"
            className="px-2.5 py-1 text-xs font-medium"
          >
            {glossaryLocked ? '术语表锁定 ✓' : '术语表未锁定'}
          </FocusButton>
          <span className="font-mono text-[11px]" style={{ color: 'var(--text-muted, #66625D)' }}>
            {statusMessage}
          </span>
        </div>
      }
      leftPane={
        <BookShelfSidebar
          books={books}
          catalog={filteredCatalog}
          selectedBookId={selectedBook?.id ?? ''}
          leftTab={leftTab}
          catalogQuery={catalogQuery}
          activeFocusId={activeFocusId}
          onFocus={emitFocus}
          onTabChange={setLeftTab}
          onCatalogQueryChange={setCatalogQuery}
          onSelectBook={(book) => {
            setSelectedBookId(book.id);
            setStatusMessage(`已打开《${book.title}》`);
          }}
          onImportBook={handleImportCatalogBook}
        />
      }
      mainPane={
        selectedBook && activeSpread ? (
          <div className="flex flex-col gap-4 p-5">
            <div
              className="flex flex-wrap items-center justify-between gap-3 rounded-lg border p-3.5"
              style={{ backgroundColor: 'var(--bg-surface, #FCFBF9)', borderColor: HAIRLINE_BORDER }}
            >
              <div>
                <div className="text-sm font-bold">{selectedBook.title}</div>
                <div className="font-mono text-xs" style={{ color: 'var(--text-muted, #66625D)' }}>
                  {activeSpread.chapterTitle} · {selectedBook.sourcePlugin}
                </div>
              </div>
              <div className="flex items-center gap-2">
                <FocusButton
                  focusId="book:nav:prev"
                  activeFocusId={activeFocusId}
                  onFocus={emitFocus}
                  onActivate={() => handleTurnPage(-1)}
                  surface="soft"
                  className="px-2.5 py-1 text-xs font-medium"
                >
                  上一跨页
                </FocusButton>
                <span className="font-mono text-xs">
                  p.{activeSpread.page} ({spreadIndex + 1}/{selectedBook.spreads.length})
                </span>
                <FocusButton
                  focusId="book:nav:next"
                  activeFocusId={activeFocusId}
                  onFocus={emitFocus}
                  onActivate={() => handleTurnPage(1)}
                  surface="soft"
                  className="px-2.5 py-1 text-xs font-medium"
                >
                  下一跨页
                </FocusButton>
              </div>
            </div>

            <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
              <section
                className="flex flex-col gap-3 rounded-lg border p-4"
                style={{ backgroundColor: 'var(--bg-surface, #FCFBF9)', borderColor: HAIRLINE_BORDER }}
              >
                <div
                  className="border-b pb-2 font-mono text-xs font-semibold"
                  style={{ borderColor: HAIRLINE_BORDER, color: 'var(--domain-book, #1E7244)' }}
                >
                  {activeSpread.leftTitle}
                </div>
                {activeSpread.leftParagraphs.map((para) => (
                  <div key={para.id} className="flex flex-col gap-2">
                    <p className="text-[13.5px] leading-relaxed">{para.text}</p>
                    {para.keyTerms?.length ? (
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className="font-mono text-[10px]" style={{ color: 'var(--text-subtle, #8A847D)' }}>
                          词元划查:
                        </span>
                        {para.keyTerms.map((term) => (
                          <FocusButton
                            key={`${para.id}:${term}`}
                            focusId={`book:term:${para.id}:${term}`}
                            activeFocusId={activeFocusId}
                            onFocus={emitFocus}
                            onActivate={() => handleLookupTerm(term)}
                            selected={activeVocab?.word.toLowerCase() === term.toLowerCase()}
                            tone="book"
                            surface="soft"
                            className="px-2 py-0.5 font-mono text-[11px] font-medium"
                          >
                            {term}
                          </FocusButton>
                        ))}
                      </div>
                    ) : null}
                  </div>
                ))}
              </section>

              <section
                className="flex flex-col gap-3 rounded-lg border p-4"
                style={{ backgroundColor: 'var(--bg-surface, #FCFBF9)', borderColor: HAIRLINE_BORDER }}
              >
                <div
                  className="border-b pb-2 font-mono text-xs font-semibold"
                  style={{ borderColor: HAIRLINE_BORDER, color: 'var(--domain-book, #1E7244)' }}
                >
                  {activeSpread.rightTitle}
                </div>
                {activeSpread.rightParagraphs.map((para) => (
                  <p key={para.id} className="text-[13.5px] leading-relaxed" style={{ color: 'var(--text-muted, #66625D)' }}>
                    {para.text}
                  </p>
                ))}
              </section>
            </div>
          </div>
        ) : null
      }
      rightPane={
        activeVocab ? (
          <VocabSm2Panel
            vocabCards={vocabCards}
            activeVocab={activeVocab}
            vocabRevealed={vocabRevealed}
            activeFocusId={activeFocusId}
            onFocus={emitFocus}
            onSelectCard={(id) => {
              setActiveVocabId(id);
              setVocabRevealed(true);
            }}
            onToggleReveal={() => setVocabRevealed((r) => !r)}
            onRateSm2={handleSm2Review}
            onClipJump={(card) => {
              setStatusMessage(
                `media.clip_jump.v1 已跳播 ${card.nasClip?.showTitle} ${card.nasClip?.episode} @ ${card.nasClip?.timestampLabel}`,
              );
              onNavigateWorkbench?.('media', { clipWord: card.word });
            }}
          />
        ) : null
      }
      footer={
        <>
          <span>焦点锚点: {activeFocusId || '未锁定'}</span>
          <span>2:3 书架 · BabelDOC 双栏 · SM-2 + NAS 跳播</span>
        </>
      }
    />
  );
};

export default BookWorkbench;
