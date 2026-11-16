import React, { useEffect, useMemo, useRef, useState } from 'react';
import WorkbenchLayout, {
  FocusButton,
  HAIRLINE_BORDER,
  SectionLabel,
  focusClass,
  focusableInputProps,
  focusableProps,
  useWorkbenchFocus,
  type WorkbenchViewProps,
} from '../components/WorkbenchLayout';
import {
  TEXT_ARTICLES,
  TEXT_CHANNELS,
  type TextArticle,
  type TextChannelId,
} from '../data/mock';
import CommentTreePanel, { type BilingualMode } from './panels/CommentTreePanel';
import TextArticleReader from './panels/TextArticleReader';

export const TextWorkbench: React.FC<WorkbenchViewProps> = ({
  focusId,
  onFocus,
  focusedItemId,
  searchTrigger,
  onContextChange,
  onNavigateWorkbench,
}) => {
  const [activeFocusId, emitFocus] = useWorkbenchFocus(
    `text:article:${TEXT_ARTICLES[0]?.id ?? ''}`,
    focusId,
    onFocus,
    focusedItemId,
  );
  const [activeChannel, setActiveChannel] = useState<TextChannelId>('all');
  const [dailyOnly, setDailyOnly] = useState<boolean>(false);
  const [query, setQuery] = useState<string>('');
  const [selectedArticleId, setSelectedArticleId] = useState<string>(TEXT_ARTICLES[0]?.id ?? '');
  const [bilingualMode, setBilingualMode] = useState<BilingualMode>('bilingual');
  const [collapsedComments, setCollapsedComments] = useState<Record<string, boolean>>({});
  const [votedComments, setVotedComments] = useState<Record<string, boolean>>({});
  const [notesByArticle, setNotesByArticle] = useState<Record<string, string[]>>({
    'text-journal-kv-compression': [
      '与 Magpie 长上下文缓存策略对照：可将注意力熵阈值接入前置压缩探针。',
    ],
  });
  const [noteDraft, setNoteDraft] = useState<string>('');
  const [actionBanner, setActionBanner] = useState<string>('双语排版引擎就绪');
  const searchRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (searchTrigger && searchTrigger > 0) searchRef.current?.focus();
  }, [searchTrigger]);

  const filteredArticles = useMemo(() => {
    const q = query.trim().toLowerCase();
    return TEXT_ARTICLES.filter((article) => {
      if (activeChannel !== 'all' && article.channelId !== activeChannel) return false;
      if (dailyOnly && article.curationMode !== 'daily_immutable') return false;
      if (!q) return true;
      return (
        article.title.toLowerCase().includes(q) ||
        article.titleZh.toLowerCase().includes(q) ||
        article.summaryZh.toLowerCase().includes(q) ||
        article.tags.some((t) => t.toLowerCase().includes(q))
      );
    });
  }, [activeChannel, dailyOnly, query]);

  const selectedArticle: TextArticle | undefined = useMemo(
    () =>
      TEXT_ARTICLES.find((a) => a.id === selectedArticleId) ??
      filteredArticles[0] ??
      TEXT_ARTICLES[0],
    [selectedArticleId, filteredArticles],
  );

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
    setNotesByArticle((prev) => ({
      ...prev,
      [selectedArticle.id]: [...(prev[selectedArticle.id] ?? []), text],
    }));
    setNoteDraft('');
    setActionBanner('已写入本地 ## Notes 区块（隔离于自动生成内容）');
  };

  return (
    <WorkbenchLayout
      domain="text"
      title="文字社区与科学期刊工作台 · TextWorkbench"
      subtitle="Journal 审稿级拆解、每日早报、Reddit/Linux.do 嵌套评论树与双语精读"
      badge="Text · Ctrl+2"
      headerActions={
        <div className="flex flex-wrap items-center gap-2">
          <input
            ref={searchRef}
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="过滤标题、摘要、标签..."
            aria-label="搜索文章"
            data-focused={activeFocusId === 'text:search' ? 'true' : undefined}
            {...focusableInputProps('text:search', activeFocusId, emitFocus)}
            className={focusClass(
              activeFocusId === 'text:search',
              'w-52 rounded-md border px-2.5 py-1 text-xs',
            )}
            style={{ backgroundColor: 'var(--bg-canvas, #F6F5F3)', borderColor: HAIRLINE_BORDER }}
          />
          <FocusButton
            focusId="text:filter:daily"
            activeFocusId={activeFocusId}
            onFocus={emitFocus}
            onActivate={() => setDailyOnly((v) => !v)}
            selected={dailyOnly}
            tone="ok"
            surface="soft"
            className="px-2.5 py-1 text-xs font-medium"
          >
            {dailyOnly ? '仅显示每日精选 ✓' : '全部 / 每日精选'}
          </FocusButton>
        </div>
      }
      toolbar={
        <div className="flex w-full flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-1.5">
            {TEXT_CHANNELS.map((ch) => (
              <FocusButton
                key={ch.id}
                focusId={`text:channel:${ch.id}`}
                activeFocusId={activeFocusId}
                onFocus={emitFocus}
                onActivate={() => setActiveChannel(ch.id)}
                selected={activeChannel === ch.id}
                tone="text"
                surface="surface"
                className="px-2.5 py-1 text-xs font-medium"
              >
                {ch.label} ({ch.count})
              </FocusButton>
            ))}
          </div>
          <span className="font-mono text-[11px]" style={{ color: 'var(--text-muted, #66625D)' }}>
            {actionBanner}
          </span>
        </div>
      }
      leftPane={
        <div className="flex flex-col gap-2 p-3">
          <SectionLabel className="px-1">文章列表 ({filteredArticles.length})</SectionLabel>
          {filteredArticles.map((article) => {
            const artFocusId = `text:article:${article.id}`;
            const isSelected = selectedArticle?.id === article.id;
            const isFocused = activeFocusId === artFocusId;
            return (
              <article
                key={article.id}
                role="button"
                aria-pressed={isSelected}
                data-focused={isFocused ? 'true' : undefined}
                {...focusableProps(artFocusId, activeFocusId, emitFocus, () => {
                  setSelectedArticleId(article.id);
                  setActionBanner(`已加载：${article.titleZh}`);
                })}
                className={focusClass(isFocused, 'cursor-pointer rounded-lg border p-3 text-left transition-colors')}
                style={{
                  backgroundColor: isSelected ? 'var(--status-info-bg, #E8F2FD)' : 'var(--bg-canvas, #F6F5F3)',
                  borderColor: isSelected ? 'var(--domain-text, #2260AA)' : HAIRLINE_BORDER,
                }}
              >
                <div className="mb-1 flex items-center justify-between gap-1.5">
                  <span className="truncate font-mono text-[10px] font-medium" style={{ color: 'var(--domain-text, #2260AA)' }}>
                    {article.sourceName}
                  </span>
                  {article.curationMode === 'daily_immutable' ? (
                    <span
                      className="shrink-0 rounded px-1.5 py-0.5 text-[9px] font-semibold"
                      style={{ backgroundColor: 'var(--status-ok-bg, #E7F5ED)', color: 'var(--status-ok-fg, #1E7244)' }}
                    >
                      每日精选
                    </span>
                  ) : article.previouslyRecommendedOn ? (
                    <span
                      className="shrink-0 rounded px-1.5 py-0.5 text-[9px] font-semibold"
                      style={{ backgroundColor: 'var(--status-warn-bg, #FCF2DF)', color: 'var(--status-warn-fg, #AC5F0C)' }}
                    >
                      经典重温
                    </span>
                  ) : null}
                </div>
                <h3 className="line-clamp-2 text-xs leading-snug font-semibold">{article.titleZh}</h3>
                <p className="mt-0.5 line-clamp-1 font-mono text-[10px]" style={{ color: 'var(--text-muted, #66625D)' }}>
                  {article.title}
                </p>
                <div className="mt-2 flex items-center justify-between font-mono text-[10px]" style={{ color: 'var(--text-subtle, #8A847D)' }}>
                  <span>{article.readTime}</span>
                  <span>{article.impactScore ?? article.publishedAt}</span>
                </div>
              </article>
            );
          })}
        </div>
      }
      mainPane={
        selectedArticle ? (
          <TextArticleReader
            article={selectedArticle}
            bilingualMode={bilingualMode}
            activeFocusId={activeFocusId}
            onFocus={emitFocus}
            onBilingualModeChange={setBilingualMode}
            onSendToBabelDoc={() => {
              setActionBanner(`已提交至 book.bilingual_pdf.v1：${selectedArticle.titleZh}`);
              onNavigateWorkbench?.('book', { fromArticleId: selectedArticle.id });
            }}
            onOpenChromeApp={() => setActionBanner('已唤起托管 Chrome --app + MV3 双语扩展')}
            onSelectBlock={(blockId) =>
              setActionBanner(`已聚焦段落 ${blockId} · 可按 F8 送入伴读解释`)
            }
          />
        ) : null
      }
      rightPane={
        selectedArticle ? (
          <CommentTreePanel
            articleId={selectedArticle.id}
            comments={selectedArticle.comments}
            bilingualMode={bilingualMode}
            collapsedComments={collapsedComments}
            votedComments={votedComments}
            notes={notesByArticle[selectedArticle.id] ?? []}
            noteDraft={noteDraft}
            activeFocusId={activeFocusId}
            onFocus={emitFocus}
            onToggleCollapse={(id) =>
              setCollapsedComments((prev) => ({ ...prev, [id]: !prev[id] }))
            }
            onToggleVote={(id) => setVotedComments((prev) => ({ ...prev, [id]: !prev[id] }))}
            onNoteDraftChange={setNoteDraft}
            onAddNote={handleAddNote}
          />
        ) : null
      }
      footer={
        <>
          <span>焦点锚点: {activeFocusId || '未锁定'}</span>
          <span>双语排版 · KaTeX 公式 · 可折叠评论树</span>
        </>
      }
    />
  );
};

export default TextWorkbench;
