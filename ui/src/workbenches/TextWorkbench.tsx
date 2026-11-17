import React, { useEffect, useMemo, useRef, useState } from 'react';
import { PanelLeftClose, PanelLeftOpen } from 'lucide-react';
import { isTauriAvailable } from '../api';
import { LiveSourceControls } from './LiveSourceControls';
import type { SelectionActionEvent } from '../components/SelectionBubble';
import StateView from '../components/StateView';
import WorkbenchLayout, {
  FocusButton,
  HAIRLINE_BORDER,
  useWorkbenchFocus,
  type WorkbenchViewProps,
} from '../components/WorkbenchLayout';
import {
  PanelSectionHeading,
  SearchInput,
  SegmentedPills,
  SelectableRow,
} from '../components/WorkbenchPrimitives';
import {
  TEXT_ARTICLES,
  TEXT_CHANNELS,
  type TextArticle,
} from '../data/mock';
import { useLiveArticleDetail, useLiveTextFeed, usePersistentNotes } from '../hooks/useLiveDomain';
import CommentTreePanel, { type BilingualMode } from './panels/CommentTreePanel';
import TextArticleReader from './panels/TextArticleReader';

export interface TextWorkbenchProps extends WorkbenchViewProps {
  onSelectionAction?: (event: SelectionActionEvent) => void;
}

export const TextWorkbench: React.FC<TextWorkbenchProps> = ({
  focusId,
  onFocus,
  focusedItemId,
  searchTrigger,
  onContextChange,
  onOpenDock,
  onAddVocab,
  onNavigateWorkbench,
  onSelectionAction,
}) => {
  const [activeFocusId, emitFocus] = useWorkbenchFocus(
    `text:article:${TEXT_ARTICLES[0]?.id ?? ''}`,
    focusId,
    onFocus,
    focusedItemId,
  );
  const [activeChannel, setActiveChannel] = useState<string>(() => isTauriAvailable() ? '' : 'all');
  const [activeSourceId, setActiveSourceId] = useState('');
  const [dailyOnly, setDailyOnly] = useState<boolean>(false);
  // Default false so 1280x800 opens as a serene 2-column 68-76ch reading sanctuary + margin notes
  const [tocOpen, setTocOpen] = useState<boolean>(false);
  const [query, setQuery] = useState<string>('');
  const [selectedArticleId, setSelectedArticleId] = useState<string>(TEXT_ARTICLES[0]?.id ?? '');
  const [bilingualMode, setBilingualMode] = useState<BilingualMode>('bilingual');
  const [collapsedComments, setCollapsedComments] = useState<Record<string, boolean>>({});
  const [votedComments, setVotedComments] = useState<Record<string, boolean>>({});
  const [seedNotes] = useState<Record<string, string[]>>({
    'text-journal-kv-compression': [
      '与 Magpie 长上下文缓存策略对照：可将注意力熵阈值接入前置压缩探针。',
    ],
  });
  const [noteDraft, setNoteDraft] = useState<string>('');
  const [actionBanner, setActionBanner] = useState<string>('双语精读排版就绪');
  const searchRef = useRef<HTMLInputElement | null>(null);
  const liveFeed = useLiveTextFeed('', activeChannel, activeSourceId);
  const articles = isTauriAvailable() ? liveFeed.items : TEXT_ARTICLES;
  const { notesByItem, addNote } = usePersistentNotes('text');

  useEffect(() => {
    if (searchTrigger && searchTrigger > 0) searchRef.current?.focus();
  }, [searchTrigger]);

  const filteredArticles = useMemo(() => {
    const q = query.trim().toLowerCase();
    return articles.filter((article) => {
      if (!isTauriAvailable() && activeChannel !== 'all' && article.channelId !== activeChannel) return false;
      if (dailyOnly && article.curationMode !== 'daily_immutable') return false;
      if (!q) return true;
      return (
        article.title.toLowerCase().includes(q) ||
        article.titleZh.toLowerCase().includes(q) ||
        article.summaryZh.toLowerCase().includes(q) ||
        article.tags.some((t) => t.toLowerCase().includes(q))
      );
    });
  }, [articles, activeChannel, dailyOnly, query]);

  const selectedArticle: TextArticle | undefined = useMemo(
    () =>
      filteredArticles.find((a) => a.id === selectedArticleId) ??
      filteredArticles[0],
    [selectedArticleId, filteredArticles],
  );

  const liveDetail = useLiveArticleDetail(
    liveFeed.live ? selectedArticle?.id ?? '' : '',
    liveFeed.sourceId,
  );
  const readerArticle: TextArticle | undefined = useMemo(() => {
    if (!selectedArticle) return undefined;
    if (!liveFeed.live) return selectedArticle;
    const blocks = liveDetail.blocks.length > 0 ? liveDetail.blocks : selectedArticle.blocks;
    const comments = liveDetail.comments.length > 0 ? liveDetail.comments : selectedArticle.comments;
    if (blocks === selectedArticle.blocks && comments === selectedArticle.comments) return selectedArticle;
    return { ...selectedArticle, blocks, comments };
  }, [liveDetail.blocks, liveDetail.comments, liveFeed.live, selectedArticle]);

  useEffect(() => {
    if (!onContextChange) return;
    if (!selectedArticle) {
      onContextChange(null);
      return;
    }
    onContextChange({
      domain: 'text',
      workbench: 'text',
      itemId: selectedArticle.id,
      title: selectedArticle.titleZh,
      subtitle: selectedArticle.sourceName,
      kind: selectedArticle.channelId,
    });
  }, [onContextChange, selectedArticle]);

  const handleAddNote = () => {
    if (!selectedArticle || !noteDraft.trim()) return;
    const text = noteDraft.trim();
    addNote(selectedArticle.id, text);
    setNoteDraft('');
    setActionBanner('已写入 ## Notes 区块并持久化到本地存储');
  };

  const handleSelectionAction = (event: SelectionActionEvent) => {
    onSelectionAction?.(event);
    const snippet = event.text.length > 26 ? `${event.text.slice(0, 26)}…` : event.text;
    const title = selectedArticle?.titleZh ?? '当前文章';
    if (event.action === 'etymology') {
      onOpenDock?.(`请查词源与语境用法：「${event.text}」（语境：${event.contextSentence}）`);
      setActionBanner(`已请求词源拆解：「${snippet}」`);
    } else if (event.action === 'clause') {
      onOpenDock?.(`请拆分长难句并标注从句层次：\n"${event.text}"`);
      setActionBanner(`已送 F8 拆解长难句：「${snippet}」`);
    } else if (event.action === 'vocab') {
      onAddVocab?.({
        word: event.text,
        contextSentence: event.contextSentence,
        sourceTitle: title,
      });
      setActionBanner(`已加入生词本：「${snippet}」`);
    } else if (event.action === 'companion') {
      onOpenDock?.(`请结合《${title}》解读选区：\n"${event.text}"`);
      setActionBanner(`已送入 F8 伴读栏：「${snippet}」`);
    } else if (event.action === 'copy') {
      setActionBanner(`已复制选区：「${snippet}」`);
    }
  };

  const resetFilters = () => {
    setQuery('');
    setActiveChannel(isTauriAvailable() ? '' : 'all');
    setDailyOnly(false);
  };

  return (
    <WorkbenchLayout
      domain="text"
      testId="workbench-text"
      title="文字与期刊"
      subtitle="舒适长文精读 · 编辑部旁注评论 · 双语对照"
      editorialHeader
      leftWidthClass="@[860px]/workbench:w-[248px]"
      rightWidthClass="@[860px]/workbench:w-[328px]"
      headerActions={
        <div className="flex flex-wrap items-center gap-2">
          <FocusButton
            focusId="text:toggle:toc"
            activeFocusId={activeFocusId}
            onFocus={emitFocus}
            onActivate={() => setTocOpen((v) => !v)}
            selected={tocOpen}
            tone="text"
            surface="soft"
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-[14px] font-medium"
          >
            {tocOpen ? (
              <PanelLeftClose className="h-3.5 w-3.5" aria-hidden="true" />
            ) : (
              <PanelLeftOpen className="h-3.5 w-3.5" aria-hidden="true" />
            )}
            <span>{tocOpen ? '收起目录' : `期刊目录 (${filteredArticles.length})`}</span>
          </FocusButton>
          <SearchInput
            focusId="text:search"
            activeFocusId={activeFocusId}
            onFocus={emitFocus}
            value={query}
            onChange={setQuery}
            placeholder="过滤标题、摘要、标签..."
            ariaLabel="搜索文章"
            inputRef={searchRef}
            widthClass="w-56"
          />
          <FocusButton
            focusId="text:filter:daily"
            activeFocusId={activeFocusId}
            onFocus={emitFocus}
            onActivate={() => setDailyOnly((v) => !v)}
            selected={dailyOnly}
            tone="ok"
            surface="soft"
            className="px-3 py-1.5 text-[14px] font-medium"
          >
            {dailyOnly ? '仅每日精选 ✓' : '每日精选'}
          </FocusButton>
        </div>
      }
      toolbar={
        <div className="flex w-full flex-wrap items-center justify-between gap-2.5">
          <LiveSourceControls domain="text" feed={liveFeed} activeFocusId={activeFocusId} onFocus={emitFocus}
            onSourceChange={(sourceId) => { setActiveSourceId(sourceId); setActiveChannel(''); }} />
          <SegmentedPills
            options={isTauriAvailable() ? [{ id: '', label: '默认频道' }, ...liveFeed.channels] : TEXT_CHANNELS}
            value={activeChannel}
            onChange={setActiveChannel}
            focusPrefix="text:channel:"
            activeFocusId={activeFocusId}
            onFocus={emitFocus}
            tone="text"
            ariaLabel="文章频道筛选"
          />
          <span className="text-[14px] text-[var(--text-muted)]">
            {liveFeed.live ? (
              <span className="mr-2 font-mono text-[13.5px] text-[var(--accent-ok)]">
                LIVE ·{' '}
                {liveFeed.sources.find((s) => s.sourceId === liveFeed.sourceId)?.title ??
                  liveFeed.sourceId}
              </span>
            ) : null}
            {actionBanner}
          </span>
        </div>
      }
      leftPane={
        tocOpen ? (
          <div className="flex flex-col gap-2.5 p-4">
            <PanelSectionHeading title="期刊与社区目录" meta={`${filteredArticles.length} 篇`} />
            {filteredArticles.length === 0 ? (
              <StateView
                mode="empty"
                title="没有匹配的文章"
                description="尝试清空关键词或切换频道。"
                activeFocusId={activeFocusId}
                onFocus={emitFocus}
                action={{
                  label: '查看全部文章',
                  focusId: 'text:empty:reset',
                  onActivate: resetFilters,
                }}
              />
            ) : (
              <div className="flex flex-col gap-1 px-1 py-1">
                {filteredArticles.map((article) => (
                  <SelectableRow
                    key={article.id}
                    focusId={`text:article:${article.id}`}
                    activeFocusId={activeFocusId}
                    selected={selectedArticle?.id === article.id}
                    domain="text"
                    onFocus={emitFocus}
                    onActivate={() => {
                      setSelectedArticleId(article.id);
                      setActionBanner(`已加载：${article.titleZh}`);
                    }}
                    title={article.titleZh}
                    eyebrow={
                      <>
                        <span className="truncate font-medium" style={{ color: 'var(--domain-text)' }}>
                          {article.sourceName}
                        </span>
                        {article.curationMode === 'daily_immutable' ? (
                          <span className="shrink-0 font-medium" style={{ color: 'var(--status-ok-fg)' }}>
                            精选
                          </span>
                        ) : article.previouslyRecommendedOn ? (
                          <span className="shrink-0 font-medium" style={{ color: 'var(--status-warn-fg)' }}>
                            重温
                          </span>
                        ) : null}
                      </>
                    }
                    meta={
                      <>
                        <span>{article.readTime}</span>
                        <span>·</span>
                        <span>{article.impactScore ?? article.publishedAt}</span>
                      </>
                    }
                  />
                ))}
              </div>
            )}
          </div>
        ) : undefined
      }
      mainPane={
        <div className="flex flex-col">
          {/* Sleek Single-Row Issue Strip when vertical TOC drawer is collapsed */}
          {!tocOpen && filteredArticles.length > 0 ? (
            <nav
              aria-label="本期文章快速切换"
              className="border-b px-6 py-2"
              style={{
                borderColor: HAIRLINE_BORDER,
                backgroundColor: 'color-mix(in srgb, var(--bg-surface) 55%, var(--bg-canvas))',
              }}
            >
              <div className="omni-prose-measure mx-auto flex flex-nowrap items-center gap-1.5 overflow-x-auto px-1.5 py-2">
                {filteredArticles.map((article) => {
                  const isSelected = selectedArticle?.id === article.id;
                  return (
                    <FocusButton
                      key={article.id}
                      focusId={`text:article:${article.id}`}
                      activeFocusId={activeFocusId}
                      onFocus={emitFocus}
                      onActivate={() => {
                        setSelectedArticleId(article.id);
                        setActionBanner(`已加载：${article.titleZh}`);
                      }}
                      selected={isSelected}
                      tone="text"
                      surface={isSelected ? 'surface' : 'ghost'}
                      className="shrink-0 max-w-[220px] truncate px-2.5 py-1 text-left text-[14px] font-medium"
                    >
                      <span className="mr-1.5 font-mono text-[13px] opacity-75">
                        {article.sourceName.split(' ')[0]}
                      </span>
                      <span>{article.titleZh}</span>
                    </FocusButton>
                  );
                })}
              </div>
            </nav>
          ) : null}

          {readerArticle ? (
            <TextArticleReader
              article={readerArticle}
              bilingualMode={bilingualMode}
              activeFocusId={activeFocusId}
              onFocus={emitFocus}
              onBilingualModeChange={setBilingualMode}
              onSendToBabelDoc={() => {
                setActionBanner(`已提交至 book.bilingual_pdf.v1：${readerArticle.titleZh}`);
                onNavigateWorkbench?.('book', { fromArticleId: readerArticle.id });
              }}
              onOpenChromeApp={() => setActionBanner('已唤起托管 Chrome --app + MV3 双语扩展')}
              onSelectBlock={(blockId) =>
                setActionBanner(`已聚焦段落 ${blockId} · 可按 F8 送入伴读解释`)
              }
              onSelectionAction={handleSelectionAction}
            />
          ) : (
            <div className="omni-prose-measure mx-auto w-full p-8">
              <StateView
                mode="empty"
                title="没有匹配的文章"
                description="尝试清空搜索词或切换频道。"
                activeFocusId={activeFocusId}
                onFocus={emitFocus}
                action={{
                  label: '查看全部文章',
                  focusId: 'text:empty:reset',
                  onActivate: resetFilters,
                }}
              />
            </div>
          )}
        </div>
      }
      rightPane={
        readerArticle ? (
          <CommentTreePanel
            articleId={readerArticle.id}
            comments={readerArticle.comments}
            bilingualMode={bilingualMode}
            collapsedComments={collapsedComments}
            votedComments={votedComments}
            notes={[
              ...(seedNotes[readerArticle.id] ?? []),
              ...(notesByItem[readerArticle.id] ?? []).map((note) => note.text),
            ]}
            noteDraft={noteDraft}
            activeFocusId={activeFocusId}
            error={readerArticle.commentsError ?? liveDetail.error ?? null}
            onFocus={emitFocus}
            onToggleCollapse={(id) =>
              setCollapsedComments((prev) => ({ ...prev, [id]: !prev[id] }))
            }
            onToggleVote={(id) => setVotedComments((prev) => ({ ...prev, [id]: !prev[id] }))}
            onNoteDraftChange={setNoteDraft}
            onAddNote={handleAddNote}
          />
        ) : undefined
      }
    />
  );
};

export default TextWorkbench;
