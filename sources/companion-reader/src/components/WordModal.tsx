import React, { useEffect, useState } from 'react'
import { Star, Sparkles, BookOpen, Film, RotateCw, ArrowUpRight } from 'lucide-react'
import type { EbookItem, UserWordRecord, WordOccurrence } from '../types/ebook'
import { findWordOccurrencesInBooks } from '../services/ebook'
import { dbService } from '../services/db'
import {
  explainWordEtymology,
  getCachedEtymology,
  loadCachedEtymologyAsync,
} from '../services/ai'
import { ReaderDialog } from './ReaderDialog'
import { AudioButton } from './AudioButton'
import { MarkdownContent } from './MarkdownContent'

interface WordModalProps {
  term: string | null
  books: EbookItem[]
  activeBookId?: string
  contextHint?: {
    bookId?: string
    bookTitle?: string
    chapterId?: string
    chapterTitle?: string
    paraIndex?: number
    sentence?: string
  } | null
  onClose: () => void
  onJumpToChapter: (bookId: string, chapterId: string, paraIndex: number, highlightTerm?: string) => void
  onMatchTV: (term: string) => void
  onInterpretSentence: (sentence: string, sourceKind?: string) => void
}

export const WordModal: React.FC<WordModalProps> = ({
  term,
  books,
  activeBookId,
  contextHint,
  onClose,
  onJumpToChapter,
  onMatchTV,
  onInterpretSentence,
}) => {
  const [record, setRecord] = useState<UserWordRecord | null>(null)
  const [occurrences, setOccurrences] = useState<WordOccurrence[]>([])
  const [aiNote, setAiNote] = useState<string>('')
  const [aiBusy, setAiBusy] = useState(false)
  const [aiError, setAiError] = useState('')

  useEffect(() => {
    if (!term) return
    const clean = term.trim().replace(/^[^a-zA-Z]+|[^a-zA-Z]+$/g, '') || term.trim()
    dbService.getWordRecord(clean).then(setRecord)
    const hits = findWordOccurrencesInBooks(clean, books, contextHint?.bookId || activeBookId, 8)
    setOccurrences(hits)
    const sentence = contextHint?.sentence || hits[0]?.sentence || ''
    const syncCached = getCachedEtymology(clean, sentence) || ''
    setAiNote(syncCached)
    setAiError('')
    setAiBusy(false)
    if (!syncCached) {
      loadCachedEtymologyAsync(clean, sentence).then((val) => {
        if (val) setAiNote(val)
      })
    }
  }, [term, books, activeBookId, contextHint])

  if (!term) return null

  const cleanTerm = term.trim().replace(/^[^a-zA-Z]+|[^a-zA-Z]+$/g, '') || term.trim()
  const primaryOccurrence = occurrences[0]
  const primarySentence = contextHint?.sentence || primaryOccurrence?.sentence || record?.contextSentence || ''
  const primaryBookTitle = contextHint?.bookTitle || primaryOccurrence?.bookTitle || record?.bookTitle || ''
  const primaryChapterTitle =
    contextHint?.chapterTitle || primaryOccurrence?.chapterTitle || record?.chapterTitle || ''

  const handleGenerateAi = async (force = false) => {
    if (aiBusy) return
    setAiBusy(true)
    setAiError('')
    try {
      const res = await explainWordEtymology(
        {
          term: cleanTerm,
          meaning: record?.meaning,
          bookTitle: primaryBookTitle,
          chapterTitle: primaryChapterTitle,
          contextSentence: primarySentence,
        },
        force
      )
      setAiNote(res)
      const firstLine = res
        .split('\n')
        .map((s) => s.replace(/^【[^】]+】/, '').trim())
        .find((s) => s.length > 2)
      const saved = await dbService.upsertWordFromBook({
        term: cleanTerm,
        meaning: record?.meaning || (firstLine ? firstLine.slice(0, 80) : ''),
        bookId: contextHint?.bookId || primaryOccurrence?.bookId,
        bookTitle: primaryBookTitle,
        chapterId: contextHint?.chapterId || primaryOccurrence?.chapterId,
        chapterTitle: primaryChapterTitle,
        paraIndex: contextHint?.paraIndex || primaryOccurrence?.paraIndex,
        contextSentence: primarySentence,
        aiNote: res,
        isStarred: record?.isStarred ?? true,
      })
      setRecord(saved)
    } catch (err) {
      setAiError(err instanceof Error ? err.message : '调用飞牛 6-luna 失败')
    } finally {
      setAiBusy(false)
    }
  }

  const handleToggleStar = async () => {
    const nextStarred = await dbService.toggleStar(cleanTerm, {
      bookId: contextHint?.bookId || primaryOccurrence?.bookId,
      bookTitle: primaryBookTitle,
      chapterId: contextHint?.chapterId || primaryOccurrence?.chapterId,
      chapterTitle: primaryChapterTitle,
      paraIndex: contextHint?.paraIndex || primaryOccurrence?.paraIndex,
      contextSentence: primarySentence,
      aiNote: aiNote || undefined,
    })
    const updated = await dbService.getWordRecord(cleanTerm)
    setRecord(updated || (record ? { ...record, isStarred: nextStarred } : null))
  }

  return (
    <ReaderDialog title={`单词查询 · ${cleanTerm}`} onClose={onClose}>
        <div className="p-4 sm:p-5 border-b border-[#e5decfe6] bg-[#f8f5ef] flex items-start justify-between gap-3">
          <div className="space-y-1">
            <div className="flex flex-wrap items-center gap-2.5">
              <h2 className="text-2xl sm:text-3xl font-bold font-serif text-[#2e2a24]">{cleanTerm}</h2>
              <AudioButton term={cleanTerm} size="sm" showLabel />
              <button
                type="button"
                onClick={() => onMatchTV(cleanTerm)}
                className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full bg-[#efe2cc] border border-[#d6b483] text-xs font-medium text-[#7a4b19] hover:brightness-95 cursor-pointer"
              >
                <Film size={13} />
                <span>看美剧怎么用</span>
              </button>
            </div>
            {record?.meaning && <p className="text-xs sm:text-sm text-[#5c5347]">{record.meaning}</p>}
          </div>

          <div className="flex items-center gap-1.5 shrink-0">
            <button
              type="button"
              onClick={handleToggleStar}
              className={`px-3 py-1.5 rounded-xl border text-xs font-medium flex items-center gap-1 cursor-pointer ${
                record?.isStarred
                  ? 'bg-[#efe2cc] border-[#c89d66] text-[#996021]'
                  : 'bg-[#f1ece1] border-[#ded6c6] text-[#72685b]'
              }`}
            >
              <Star size={14} className={record?.isStarred ? 'fill-[#996021]' : ''} />
              <span>{record?.isStarred ? '已入生词本' : '加入生词本'}</span>
            </button>

          </div>
        </div>

        <div className="p-4 sm:p-6 space-y-5 overflow-y-auto text-sm">
          {primarySentence && <section className="space-y-2">
            <p className="text-xs text-[#72685b]">{primaryBookTitle} · {primaryChapterTitle}</p>
            <blockquote className="font-serif border-l-2 border-[#c89d66] pl-3">{primarySentence}</blockquote>
            <button type="button" className="reader-action" onClick={() => onInterpretSentence(primarySentence, '来自书内查词')}>解读所在原句</button>
          </section>}
          {/* AI Etymology & Book Context Breakdown */}
          <div className="space-y-3" aria-live="polite" aria-busy={aiBusy}>
            <button
              type="button"
              disabled={aiBusy}
              onClick={() => handleGenerateAi(false)}
              className="w-full min-h-11 rounded-2xl bg-[#efe2cc] hover:bg-[#e5d3b4] border border-[#d6b483] px-4 py-2.5 text-xs sm:text-sm font-semibold text-[#7a4b19] flex items-center justify-center gap-2 transition-colors cursor-pointer disabled:opacity-60"
            >
              <Sparkles size={16} className="text-[#996021]" />
              <span>
                {aiBusy
                  ? 'AI 正在查询词义与词源…'
                  : aiNote
                  ? '查看 AI 词义与词源'
                  : 'AI 查询词义与词源'}
              </span>
            </button>

            {(aiNote || aiError) && (
              <div className="p-4 rounded-2xl bg-[#f8f2e7] border border-[#dfc6a4] space-y-2">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-semibold text-[#996021] flex items-center gap-1.5">
                    <Sparkles size={14} />
                    <span>飞牛 6-luna · 语境词义与词源拆解</span>
                  </span>
                  {aiNote && (
                    <button
                      type="button"
                      disabled={aiBusy}
                      onClick={() => handleGenerateAi(true)}
                      className="inline-flex items-center gap-1 text-xs text-[#8c5820] hover:underline cursor-pointer"
                    >
                      <RotateCw size={12} className={aiBusy ? 'animate-spin' : ''} />
                      重新生成
                    </button>
                  )}
                </div>
                {aiError ? (
                  <p role="alert" className="text-xs text-[#94352b]">{aiError}</p>
                ) : (
                  <MarkdownContent content={aiNote} />
                )}
              </div>
            )}
          </div>

          {/* Occurrences in User's Imported Books */}
          <div className="space-y-2.5">
            <div className="flex items-center justify-between">
              <h3 className="text-xs font-semibold text-[#72685b] flex items-center gap-1.5">
                <BookOpen size={14} className="text-[#996021]" />
                <span>在已导入电子书中出现的原句 ({occurrences.length})</span>
              </h3>
            </div>

            {occurrences.length === 0 ? (
              <div className="p-4 rounded-2xl bg-[#f8f5ef] border border-[#ded6c6] text-xs text-[#72685b]">
                当前书架中的书籍暂未检索到「{cleanTerm}」的精确匹配，但你依然可以使用上方 AI 拆解词源，或去看美剧台词。
              </div>
            ) : (
              <div className="space-y-2.5">
                {occurrences.map((occ, idx) => (
                  <div
                    key={`${occ.bookId}-${occ.chapterId}-${occ.paraIndex}-${idx}`}
                    className="p-3.5 rounded-2xl bg-[#f8f5ef] border border-[#ded6c6] space-y-2"
                  >
                    <div className="flex items-center justify-between gap-2 text-xs text-[#72685b]">
                      <span className="font-medium text-[#996021] truncate">
                        《{occ.bookTitle}》 · {occ.chapterTitle} (第 {occ.paraIndex} 段)
                      </span>
                      <div className="flex items-center gap-1.5 shrink-0">
                        <AudioButton term={occ.sentence} size="sm" />
                        <button
                          type="button"
                          onClick={() => {
                            onInterpretSentence(occ.sentence, '来自书内查词')
                            onClose()
                          }}
                          className="px-2.5 py-1 rounded-xl bg-[#efe2cc] border border-[#d6b483] text-[#7a4b19] font-medium hover:brightness-95 cursor-pointer"
                        >
                          ✨ 结合上下文解读此句
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            onJumpToChapter(occ.bookId, occ.chapterId, occ.paraIndex, cleanTerm)
                            onClose()
                          }}
                          className="inline-flex items-center gap-0.5 px-2 py-1 rounded-xl bg-[#fdfbf7] border border-[#ded6c6] text-[#5c5347] hover:bg-[#f1ece1] cursor-pointer"
                        >
                          <span>跳到段落</span>
                          <ArrowUpRight size={12} />
                        </button>
                      </div>
                    </div>
                    <p className="font-serif text-sm sm:text-base text-[#2e2a24] leading-relaxed">
                      {occ.sentence}
                    </p>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      <button type="button" className="reader-action reader-primary" onClick={onClose}>继续阅读</button>
    </ReaderDialog>
  )
}
