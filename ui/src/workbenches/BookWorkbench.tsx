import React, { useEffect, useMemo, useState } from 'react';
import { BookMarked, BookOpen, ChevronDown, ChevronUp, Search } from 'lucide-react';
import type { SelectionActionEvent } from '../components/SelectionBubble';
import StateView from '../components/StateView';
import WorkbenchLayout, {
  FocusButton,
  HAIRLINE_BORDER,
  focusClass,
  focusableProps,
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
import BookSpreadReader from './panels/BookSpreadReader';
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

function createVocabCard(
  word: string,
  contextSentence: string,
  bookTitle: string,
  page?: number,
): VocabCard {
  const slug =
    word
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-|-$/g, '') || 'term';
  return {
    id: `vocab-${slug}-${Date.now()}`,
    word,
    phonetic: '/待注音/',
    pos: word.includes(' ') ? 'phrase' : 'term',
    meaningZh: '待 AI 伴读补充释义（来自划词加入）',
    etymology: page ? `摘自《${bookTitle}》p.${page}` : `摘自《${bookTitle}》`,
    contextSentence: contextSentence || word,
    bookTitle,
    sourceDate: new Date().toISOString().slice(0, 10),
    intervalDays: 1,
    easeFactor: 2.5,
    dueLabel: '新加入 · 今日待复习',
  };
}

export interface BookWorkbenchProps extends WorkbenchViewProps {
  onSelectionAction?: (event: SelectionActionEvent) => void;
}

export const BookWorkbench: React.FC<BookWorkbenchProps> = ({
  focusId,
  onFocus,
  focusedItemId,
  onContextChange,
  onOpenDock,
  onAddVocab,
  sharedVocab,
  onNavigateWorkbench,
  onSelectionAction,
}) => {
  const [activeFocusId, emitFocus] = useWorkbenchFocus(
    `book:shelf:${BOOK_ITEMS[0]?.id ?? ''}`,
    focusId,
    onFocus,
    focusedItemId,
  );
  const [books, setBooks] = useState<BookItem[]>(BOOK_ITEMS);
  const [leftTab, setLeftTab] = useState<'shelf' | 'search'>('shelf');
  // Compact spine strip by default so the dual-page book spread maximizes above the fold at 1280x800
  const [shelfOpen, setShelfOpen] = useState<boolean>(false);
  const [vocabOpen, setVocabOpen] = useState<boolean>(true);
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
    if (!sharedVocab?.length) return;
    setVocabCards((prev) => {
      const existingLower = new Set(prev.map((c) => c.word.toLowerCase()));
      const additions: VocabCard[] = [];
      for (const entry of sharedVocab) {
        const clean = entry.word.trim();
        if (!clean || existingLower.has(clean.toLowerCase())) continue;
        existingLower.add(clean.toLowerCase());
        additions.push(
          createVocabCard(
            clean,
            entry.contextSentence ?? clean,
            entry.sourceTitle ?? 'TextWorkbench',
          ),
        );
      }
      return additions.length > 0 ? [...additions.reverse(), ...prev] : prev;
    });
  }, [sharedVocab]);

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
      setVocabOpen(true);
      setStatusMessage(`已定位词元「${matched.word}」— ${matched.meaningZh}`);
    } else {
      setStatusMessage(`已记录划词「${term}」（等待 BabelDOC 术语库解析）`);
    }
  };

  const handleAddSelectionToVocab = (rawWord: string, contextSentence: string) => {
    const cleanWord = rawWord.trim();
    if (!cleanWord) return;
    const bookTitle = selectedBook?.title ?? '当前书籍';
    const existing = vocabCards.find((c) => c.word.toLowerCase() === cleanWord.toLowerCase());
    if (existing) {
      setActiveVocabId(existing.id);
      setVocabRevealed(true);
      setVocabOpen(true);
      setStatusMessage(`「${existing.word}」已在生词本中，已聚焦到 SM-2 卡片`);
    } else {
      const nextCard = createVocabCard(cleanWord, contextSentence, bookTitle, activeSpread?.page);
      setVocabCards((prev) => [nextCard, ...prev]);
      setActiveVocabId(nextCard.id);
      setVocabRevealed(true);
      setVocabOpen(true);
      setStatusMessage(`已加入生词本「${cleanWord}」（SM-2 队列共 ${vocabCards.length + 1} 词）`);
    }
    onAddVocab?.({
      word: cleanWord,
      contextSentence,
      sourceTitle: bookTitle,
    });
  };

  const handleSelectionAction = (event: SelectionActionEvent) => {
    onSelectionAction?.(event);
    const title = selectedBook?.title ?? '当前书籍';
    const pageSuffix = activeSpread ? ` p.${activeSpread.page}` : '';
    if (event.action === 'etymology') {
      const matched = vocabCards.find((c) => c.word.toLowerCase() === event.text.toLowerCase());
      if (matched) {
        setActiveVocabId(matched.id);
        setVocabRevealed(true);
        setVocabOpen(true);
        setStatusMessage(`词源联动「${matched.word}」：${matched.etymology}`);
      } else {
        onOpenDock?.(`请查词源与语境释义：「${event.text}」（语境：${event.contextSentence}）`);
        setStatusMessage(`已查词源「${event.text}」（已同步 F8 伴读栏）`);
      }
    } else if (event.action === 'clause') {
      onOpenDock?.(`请拆分长难句结构并标注从句层次：\n"${event.text}"`);
      setStatusMessage(`已送 F8 拆解长难句：「${event.text.slice(0, 24)}」`);
    } else if (event.action === 'vocab') {
      handleAddSelectionToVocab(event.text, event.contextSentence);
    } else if (event.action === 'companion') {
      onOpenDock?.(`请结合《${title}》${pageSuffix} 解读选区：\n"${event.text}"`);
      setStatusMessage('已将选区送入 F8 伴读栏');
    } else if (event.action === 'copy') {
      setStatusMessage(`已复制选区：「${event.text.slice(0, 26)}」`);
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
      testId="workbench-book"
      title="书本与双栏精读"
      subtitle="2:3 书架封面墙 · 双栏原版对照 · SM-2 生词记忆"
      editorialHeader
      rightWidthClass="@[860px]/workbench:w-[292px]"
      headerActions={
        <div className="flex flex-wrap items-center gap-2">
          <FocusButton
            focusId="book:toggle:shelf"
            activeFocusId={activeFocusId}
            onFocus={emitFocus}
            onActivate={() => setShelfOpen((v) => !v)}
            selected={shelfOpen}
            tone="book"
            surface="soft"
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-[12.5px] font-medium"
          >
            {shelfOpen ? (
              <ChevronUp className="h-3.5 w-3.5" aria-hidden="true" />
            ) : (
              <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
            )}
            <span>{shelfOpen ? '收起封面墙' : `展开封面墙 (${books.length})`}</span>
          </FocusButton>
          <FocusButton
            focusId="book:toggle:vocab"
            activeFocusId={activeFocusId}
            onFocus={emitFocus}
            onActivate={() => setVocabOpen((v) => !v)}
            selected={vocabOpen}
            tone="book"
            surface="soft"
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-[12.5px] font-medium"
          >
            <BookMarked className="h-3.5 w-3.5" aria-hidden="true" />
            <span>生词台 ({vocabCards.length})</span>
          </FocusButton>
          <FocusButton
            focusId="book:action:glossary-lock"
            activeFocusId={activeFocusId}
            onFocus={emitFocus}
            onActivate={() => setGlossaryLocked((v) => !v)}
            selected={glossaryLocked}
            tone="ok"
            surface="soft"
            className="px-3 py-1.5 text-[12.5px] font-medium"
          >
            {glossaryLocked ? '术语表锁定 ✓' : '术语表未锁定'}
          </FocusButton>
        </div>
      }
      toolbar={
        <div className="flex w-full items-center justify-between gap-2 text-[12.5px] text-[var(--text-muted)]">
          <span>双栏原版对照 · 划词支持词源拆解与生词入库</span>
          <span data-testid="book-status-message" className="truncate font-medium">
            {statusMessage}
          </span>
        </div>
      }
      mainPane={
        <div className="flex flex-col">
          {shelfOpen ? (
            <BookShelfSidebar
              books={books}
              catalog={filteredCatalog}
              selectedBookId={selectedBook?.id ?? ''}
              leftTab={leftTab}
              catalogQuery={catalogQuery}
              activeFocusId={activeFocusId}
              onFocus={emitFocus}
              onTabChange={(tab) => {
                setLeftTab(tab);
                setShelfOpen(true);
              }}
              onCatalogQueryChange={setCatalogQuery}
              onSelectBook={(book) => {
                setSelectedBookId(book.id);
                setStatusMessage(`已打开《${book.title}》`);
              }}
              onImportBook={handleImportCatalogBook}
            />
          ) : (
            /* Compact Book Spine Strip: keeps 2:3 mini covers & Z-Lib tab reachable while maximizing the reading spread */
            <div
              className="flex flex-wrap items-center justify-between gap-3 border-b px-6 py-2.5"
              style={{
                borderColor: HAIRLINE_BORDER,
                backgroundColor: 'color-mix(in srgb, var(--bg-surface) 60%, var(--bg-canvas))',
              }}
            >
              <div className="flex min-w-0 flex-1 items-center gap-2 overflow-x-auto py-0.5">
                {books.map((book) => {
                  const shelfId = `book:shelf:${book.id}`;
                  const isSelected = selectedBook?.id === book.id;
                  const isFocused = activeFocusId === shelfId;
                  return (
                    <button
                      key={book.id}
                      type="button"
                      aria-pressed={isSelected}
                      data-focused={isFocused ? 'true' : undefined}
                      {...focusableProps(shelfId, activeFocusId, emitFocus, () => {
                        setSelectedBookId(book.id);
                        setStatusMessage(`已打开《${book.title}》`);
                      })}
                      className={focusClass(
                        isFocused,
                        'flex shrink-0 cursor-pointer items-center gap-2.5 rounded-[10px] px-2.5 py-1.5 text-left transition-colors hover:bg-[var(--hover-fill)]',
                      )}
                      style={{
                        backgroundColor: isSelected
                          ? 'color-mix(in srgb, var(--domain-book) 11%, var(--bg-surface))'
                          : 'transparent',
                      }}
                    >
                      <span
                        className="relative flex h-9 w-6 shrink-0 items-end justify-center overflow-hidden rounded-[4px] text-white"
                        style={{
                          background: book.coverGradient,
                          boxShadow: 'inset 2px 0 0 rgba(255,255,255,0.28), var(--shadow-page)',
                        }}
                        aria-hidden="true"
                      >
                        {isSelected ? <BookOpen className="mb-1 h-2.5 w-2.5 opacity-90" /> : null}
                      </span>
                      <span className="min-w-0 max-w-[148px]">
                        <span className="block truncate text-[12.5px] font-semibold text-[var(--text-primary)]">
                          {book.title}
                        </span>
                        <span className="block font-mono text-[11.5px] text-[var(--text-muted)]">
                          {book.format.toUpperCase()} · {book.progressPct}%
                        </span>
                      </span>
                    </button>
                  );
                })}
              </div>

              <div className="flex shrink-0 items-center gap-1.5">
                <FocusButton
                  focusId="book:tab:shelf"
                  activeFocusId={activeFocusId}
                  onFocus={emitFocus}
                  onActivate={() => {
                    setLeftTab('shelf');
                    setShelfOpen(true);
                  }}
                  surface="soft"
                  className="px-2.5 py-1.5 text-[12px] font-medium"
                >
                  封面墙
                </FocusButton>
                <FocusButton
                  focusId="book:tab:search"
                  activeFocusId={activeFocusId}
                  onFocus={emitFocus}
                  onActivate={() => {
                    setLeftTab('search');
                    setShelfOpen(true);
                  }}
                  surface="soft"
                  className="inline-flex items-center gap-1 px-2.5 py-1.5 text-[12px] font-medium"
                >
                  <Search className="h-3 w-3" aria-hidden="true" />
                  <span>Z-Lib 搜书</span>
                </FocusButton>
              </div>
            </div>
          )}

          {selectedBook && activeSpread ? (
            <BookSpreadReader
              book={selectedBook}
              spread={activeSpread}
              spreadIndex={spreadIndex}
              activeVocab={activeVocab}
              activeFocusId={activeFocusId}
              onFocus={emitFocus}
              onTurnPage={handleTurnPage}
              onLookupTerm={handleLookupTerm}
              onSelectionAction={handleSelectionAction}
            />
          ) : (
            <div className="p-8">
              <StateView mode="empty" title="书架暂无可读卷册" description="可切换到 Z-Lib 搜书导入一本图书。" />
            </div>
          )}
        </div>
      }
      rightPane={
        vocabOpen && activeVocab ? (
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
        ) : undefined
      }
    />
  );
};

export default BookWorkbench;
