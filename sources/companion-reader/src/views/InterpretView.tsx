import React, { useEffect, useMemo, useState } from 'react'
import {
  Sparkles,
  Copy,
  BookmarkPlus,
  BookmarkCheck,
  RotateCw,
  ArrowUpRight,
  BookOpen,
  Check,
  X,
  MessageSquarePlus,
  ChevronDown,
} from 'lucide-react'
import {
  INTERPRET_PRESETS,
  type EbookItem,
  type InterpretPreset,
  type SentenceContextMatch,
  type SentenceNote,
} from '../types/ebook'
import { findSentenceContextInBooks } from '../services/ebook'
import {
  copyTextToSystemClipboard,
  interpretSentenceWithContext,
} from '../services/ai'
import { dbService } from '../services/db'
import { AudioButton } from '../components/AudioButton'
import { MarkdownContent } from '../components/MarkdownContent'

interface InterpretViewProps {
  books: EbookItem[]
  activeBookId?: string
  activeChapterId?: string
  initialSentence?: string
  initialSourceKind?: string
  incomingVersion?: number
  onSelectWord: (
    term: string,
    hint?: {
      bookId?: string
      bookTitle?: string
      chapterId?: string
      chapterTitle?: string
      paraIndex?: number
      sentence?: string
    }
  ) => void
  onJumpToChapter: (
    bookId: string,
    chapterId: string,
    paraIndex: number,
    highlightTerm?: string
  ) => void
}

export const InterpretView: React.FC<InterpretViewProps> = ({
  books,
  activeBookId,
  activeChapterId,
  initialSentence = '',
  initialSourceKind = '手动输入',
  incomingVersion = 0,
  onSelectWord,
  onJumpToChapter,
}) => {
  const [sentence, setSentence] = useState<string>(initialSentence)
  const [sourceKind, setSourceKind] = useState<string>(initialSourceKind)
  const [presets, setPresets] = useState<InterpretPreset[]>(['context', 'expressions'])
  const [followUpQuestion, setFollowUpQuestion] = useState<string>('')
  const [showCustomContext, setShowCustomContext] = useState<boolean>(false)
  const [customContextText, setCustomContextText] = useState<string>('')
  const [showSurroundingParas, setShowSurroundingParas] = useState<boolean>(true)

  const [busy, setBusy] = useState<boolean>(false)
  const [error, setError] = useState<string>('')
  const [result, setResult] = useState<string>('')
  const [savedNoteId, setSavedNoteId] = useState<string | null>(null)
  const [toastMsg, setToastMsg] = useState<string>('')

  const showToast = (msg: string) => {
    setToastMsg(msg)
    window.setTimeout(() => {
      setToastMsg((curr) => (curr === msg ? '' : curr))
    }, 2200)
  }

  // Automatically match sentence in imported EPUB/MOBI books
  const matchedContext: SentenceContextMatch | null = useMemo(() => {
    if (!sentence.trim()) return null
    return findSentenceContextInBooks(sentence, books, activeBookId, activeChapterId)
  }, [sentence, books, activeBookId, activeChapterId])

  // Extract clickable content words from the target sentence
  const sentenceWords = useMemo(() => {
    const matches = sentence.match(/[A-Za-z'’-]{3,}/g) || []
    const seen = new Set<string>()
    const out: string[] = []
    for (const w of matches) {
      const clean = w.replace(/^['’-]+|['’-]+$/g, '')
      const lower = clean.toLowerCase()
      if (clean.length >= 3 && !seen.has(lower)) {
        seen.add(lower)
        out.push(clean)
      }
    }
    return out.slice(0, 18)
  }, [sentence])

  // When incoming sentence arrives from reader click or Android ACTION_PROCESS_TEXT
  useEffect(() => {
    if (initialSentence && initialSentence.trim()) {
      setSentence(initialSentence.trim())
      setSourceKind(initialSourceKind || '来自选词')
      setResult('')
      setError('')
      setSavedNoteId(null)
    }
  }, [initialSentence, initialSourceKind, incomingVersion])

  const togglePreset = (id: InterpretPreset) => {
    setPresets((prev) => {
      if (prev.includes(id)) {
        return prev.length > 1 ? prev.filter((p) => p !== id) : prev
      }
      return [...prev, id]
    })
  }

  const runInterpretation = async (
    targetText = sentence,
    targetSource = sourceKind,
    targetCtx = matchedContext,
    force = false
  ) => {
    const clean = targetText.trim()
    if (!clean || busy) return
    setBusy(true)
    setError('')
    setSavedNoteId(null)
    try {
      const output = await interpretSentenceWithContext(
        {
          sentence: clean,
          presets,
          followUpQuestion,
          context: targetCtx,
          customContextText: showCustomContext ? customContextText : undefined,
        },
        force
      )
      setResult(output)
      // Auto-save to reading notes so every deep reading session is preserved
      const noteId = `note-${Date.now().toString(36)}`
      const note: SentenceNote = {
        id: noteId,
        createdAt: Date.now(),
        updatedAt: Date.now(),
        sentence: clean,
        sourceKind: targetSource,
        presets: [...presets],
        followUpQuestion: followUpQuestion.trim() || undefined,
        aiInterpretation: output,
        context: targetCtx,
      }
      await dbService.saveSentenceNote(note)
      setSavedNoteId(noteId)
    } catch (err) {
      setError(err instanceof Error ? err.message : '调用飞牛 6-luna 解读失败')
    } finally {
      setBusy(false)
    }
  }

  const handleCopyResult = async () => {
    if (!result) return
    const ok = await copyTextToSystemClipboard(result)
    showToast(ok ? '解读结果已复制到剪贴板' : '复制失败')
  }

  return (
    <div className="max-w-4xl mx-auto px-3.5 sm:px-6 py-4 pb-28 space-y-5">
      {toastMsg && (
        <div className="fixed top-16 left-1/2 -translate-x-1/2 z-50 px-4 py-2 rounded-full bg-[#263c33] text-[#fdfbf7] text-xs font-medium shadow-lg animate-in fade-in">
          {toastMsg}
        </div>
      )}

      {/* Header Banner inspired by Wordcraft */}
      <div className="p-4 sm:p-5 rounded-3xl bg-[#fdfbf7] border border-[#ded6c6] shadow-xs flex flex-wrap items-center justify-between gap-3">
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <span className="px-2 py-0.5 rounded-md bg-[#dce9d9] text-[#263c33] font-mono text-xs font-semibold">
              语境解读台
            </span>
            <h2 className="text-lg sm:text-xl font-bold text-[#2e2a24] font-serif">
              结合上下文，读懂每一句
            </h2>
          </div>
          <p className="text-xs text-[#72685b]">
            书内点选句子即可弹窗查看解读，也可在这里输入问题继续追问。
          </p>
        </div>

      </div>

      {/* Section 01: Target Sentence */}
      <section className="p-4 sm:p-6 rounded-3xl bg-[#fdfbf7] border border-[#ded6c6] shadow-xs space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2.5">
            <span className="font-mono text-xs font-semibold text-[#325f50] bg-[#dce9d9] px-2 py-0.5 rounded-md">
              01
            </span>
            <h3 className="text-base font-semibold text-[#2e2a24]">待解读的英文句子</h3>
            <span className="text-xs px-2 py-0.5 rounded-full bg-[#f1ece1] text-[#72685b] border border-[#ded6c6]">
              {sourceKind}
            </span>
          </div>

          <div className="flex items-center gap-1.5">
            {sentence.trim() && <AudioButton term={sentence} size="sm" labelText="朗读原句" />}
            {sentence.trim() && (
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  setSentence('')
                  setResult('')
                  setError('')
                  setSavedNoteId(null)
                }}
                className="p-1.5 rounded-xl bg-[#f1ece1] text-[#72685b] hover:text-[#2e2a24] cursor-pointer"
                title="清空句子"
              >
                <X size={15} />
              </button>
            )}
          </div>
        </div>

        <textarea
          value={sentence}
          onChange={(e) => {
            setSentence(e.target.value)
            setSourceKind('手动编辑')
          }}
          rows={3}
          placeholder="在此粘贴或输入你在 EPUB / MOBI 电子书中复制的英文句子..."
          className="w-full p-3.5 rounded-2xl bg-[#f8f5ef] border border-[#ded6c6] text-base sm:text-lg font-serif text-[#2e2a24] placeholder-[#8a7f70] leading-relaxed focus:outline-hidden focus:border-[#996021]"
        />

        {/* Clickable words in sentence */}
        {sentenceWords.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5 pt-1">
            <span className="text-[11px] text-[#8a7f70] mr-1">点词查词源/台词：</span>
            {sentenceWords.map((w) => (
              <button
                key={w}
                type="button"
                onClick={() =>
                  onSelectWord(w, {
                    bookId: matchedContext?.bookId,
                    bookTitle: matchedContext?.bookTitle,
                    chapterId: matchedContext?.chapterId,
                    chapterTitle: matchedContext?.chapterTitle,
                    paraIndex: matchedContext?.paraIndex,
                    sentence,
                  })
                }
                className="px-2 py-0.5 rounded-lg bg-[#f1ece1] hover:bg-[#efe2cc] border border-[#ded6c6] text-xs font-serif text-[#5c5347] hover:text-[#7a4b19] transition-colors cursor-pointer"
              >
                {w}
              </button>
            ))}
          </div>
        )}
      </section>

      {/* Section 02: Auto-Matched Book Context */}
      <section className="p-4 sm:p-6 rounded-3xl bg-[#fdfbf7] border border-[#ded6c6] shadow-xs space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2.5">
            <span className="font-mono text-xs font-semibold text-[#325f50] bg-[#dce9d9] px-2 py-0.5 rounded-md">
              02
            </span>
            <h3 className="text-base font-semibold text-[#2e2a24]">自动定位的书中上下文</h3>
            {matchedContext ? (
              <span className="px-2.5 py-0.5 rounded-full bg-[#e7f0e8] border border-[#c5dbc8] text-[#35593d] text-xs font-medium">
                {matchedContext.confidence === 'exact' ? '✓ 已精准定位原书段落' : '≈ 已语义定位原书段落'}
              </span>
            ) : sentence.trim() ? (
              <span className="px-2.5 py-0.5 rounded-full bg-[#f1ece1] border border-[#ded6c6] text-[#72685b] text-xs">
                未在已导电子书中命中，将直接解读或使用补充背景
              </span>
            ) : null}
          </div>

          <div className="flex items-center gap-2">
            {matchedContext && (
              <button
                type="button"
                onClick={() =>
                  onJumpToChapter(
                    matchedContext.bookId,
                    matchedContext.chapterId,
                    matchedContext.paraIndex
                  )
                }
                className="inline-flex items-center gap-1 px-2.5 py-1 rounded-xl bg-[#efe2cc] border border-[#d6b483] text-xs font-semibold text-[#7a4b19] hover:brightness-95 cursor-pointer"
              >
                <BookOpen size={13} />
                <span>跳回原书第 {matchedContext.paraIndex} 段</span>
                <ArrowUpRight size={13} />
              </button>
            )}
            <button
              type="button"
              onClick={() => setShowCustomContext((p) => !p)}
              className="text-xs text-[#8c5820] hover:underline cursor-pointer"
            >
              {showCustomContext ? '收起手动背景' : '手动补充背景'}
            </button>
          </div>
        </div>

        {matchedContext ? (
          <div className="rounded-2xl bg-[#f8f5ef] border border-[#e5decfe6] p-3.5 sm:p-4 space-y-2.5">
            <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-[#72685b] pb-2 border-b border-[#e5decfe6]">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="font-semibold text-[#996021]">《{matchedContext.bookTitle}》</span>
                <span>·</span>
                <span className="font-semibold text-[#2e2a24]">{matchedContext.chapterTitle}</span>
                {matchedContext.chapterByline && <span>({matchedContext.chapterByline})</span>}
              </div>
              <button
                type="button"
                onClick={() => setShowSurroundingParas((p) => !p)}
                className="inline-flex items-center gap-1 text-xs text-[#72685b] hover:text-[#2e2a24] cursor-pointer"
              >
                <span>{showSurroundingParas ? '折叠前后段' : '展开前后段'}</span>
                <ChevronDown
                  size={13}
                  className={showSurroundingParas ? 'rotate-180 transition-transform' : ''}
                />
              </button>
            </div>

            {showSurroundingParas && matchedContext.previousParagraph && (
              <div className="text-xs sm:text-sm text-[#72685b] font-serif border-l-2 border-[#d5cbb8] pl-3 leading-relaxed">
                <span className="font-sans text-[11px] font-semibold text-[#8a7f70] block mb-0.5">
                  前一段落 (第 {matchedContext.paraIndex - 1} 段)：
                </span>
                {matchedContext.previousParagraph}
              </div>
            )}

            <div className="p-3 rounded-xl bg-[#fdfbf7] border border-[#dfc6a4] text-sm sm:text-base font-serif text-[#2e2a24] leading-relaxed">
              <span className="font-sans text-[11px] font-semibold text-[#996021] block mb-1">
                当前所在段落 (第 {matchedContext.paraIndex} 段)：
              </span>
              {matchedContext.currentParagraph}
            </div>

            {showSurroundingParas && matchedContext.nextParagraph && (
              <div className="text-xs sm:text-sm text-[#72685b] font-serif border-l-2 border-[#d5cbb8] pl-3 leading-relaxed">
                <span className="font-sans text-[11px] font-semibold text-[#8a7f70] block mb-0.5">
                  后一段落 (第 {matchedContext.paraIndex + 1} 段)：
                </span>
                {matchedContext.nextParagraph}
              </div>
            )}
          </div>
        ) : (
          <p className="text-xs text-[#72685b]">
            {sentence.trim()
              ? '该句不在当前书架已导入的章节中（或属于外部文章），AI 将直接根据句子本身及下方你补充的背景进行解读。'
              : '复制或输入句子后，系统会自动在已导入的 EPUB / MOBI 电子书中检索它所在的章节以及前后段落。'}
          </p>
        )}

        {showCustomContext && (
          <textarea
            value={customContextText}
            onChange={(e) => setCustomContextText(e.target.value)}
            rows={3}
            placeholder="（可选）如果是读外刊或未导入的书，可在此粘贴前后的段落或说明文章主题..."
            className="w-full p-3 rounded-2xl bg-[#f8f5ef] border border-[#ded6c6] text-xs sm:text-sm text-[#2e2a24] placeholder-[#8a7f70]"
          />
        )}
      </section>

      {/* Section 03: Interpretation Presets & Follow-up Question (Wordcraft Style) */}
      <section className="p-4 sm:p-6 rounded-3xl bg-[#fdfbf7] border border-[#ded6c6] shadow-xs space-y-4">
        <div className="flex items-center gap-2.5">
          <span className="font-mono text-xs font-semibold text-[#325f50] bg-[#dce9d9] px-2 py-0.5 rounded-md">
            03
          </span>
          <h3 className="text-base font-semibold text-[#2e2a24]">解读维度与追问（可多选组合）</h3>
        </div>

        <div className="flex flex-wrap gap-2">
          {INTERPRET_PRESETS.map((preset) => {
            const active = presets.includes(preset.id)
            return (
              <button
                key={preset.id}
                type="button"
                disabled={busy}
                onClick={() => togglePreset(preset.id)}
                className={`px-3.5 py-2 rounded-2xl border text-xs sm:text-sm font-medium inline-flex items-center gap-1.5 transition-all cursor-pointer ${
                  active
                    ? 'bg-[#dce9d9] border-[#325f50] text-[#263c33] font-semibold'
                    : 'bg-[#f8f5ef] border-[#ded6c6] text-[#5c5347] hover:bg-[#f1ece1]'
                }`}
              >
                {active && <Check size={14} className="text-[#325f50]" />}
                <span>{preset.label}</span>
              </button>
            )
          })}
        </div>

        <p className="text-xs text-[#72685b]">
          {INTERPRET_PRESETS.filter((p) => presets.includes(p.id))
            .map((p) => p.shortDesc)
            .join(' · ')}
        </p>

        <div className="space-y-1.5 pt-1">
          <label className="text-xs font-medium text-[#5c5347] flex items-center gap-1.5">
            <MessageSquarePlus size={14} className="text-[#996021]" />
            <span>补充追问（可选，针对这句话你有哪里没看懂？）</span>
          </label>
          <input
            type="text"
            value={followUpQuestion}
            onChange={(e) => setFollowUpQuestion(e.target.value)}
            placeholder="例如：这里的 it 指代前文的什么？为什么作者说他不是 machinelike？"
            className="w-full px-3.5 py-2.5 rounded-2xl bg-[#f8f5ef] border border-[#ded6c6] text-xs sm:text-sm text-[#2e2a24] placeholder-[#8a7f70] focus:outline-hidden focus:border-[#996021]"
          />
        </div>

        <button
          type="button"
          disabled={busy || !sentence.trim()}
          onClick={() => void runInterpretation(sentence, sourceKind, matchedContext, !!result)}
          className="w-full min-h-12 rounded-2xl bg-[#325f50] hover:brightness-105 text-[#fdfbf7] font-semibold text-sm sm:text-base flex items-center justify-center gap-2 shadow-xs transition-all cursor-pointer disabled:opacity-50"
        >
          <Sparkles size={18} />
          <span>
            {busy
              ? '飞牛 6-luna 正在结合上下文深度解读...'
              : result
              ? '重新结合上下文解读'
              : '✨ 让 6-luna 结合上下文解读此句'}
          </span>
        </button>
      </section>

      {/* Section 04: AI Interpretation Result */}
      {(result || error) && (
        <section className="p-4 sm:p-6 rounded-3xl bg-[#f8f2e7] border border-[#dfc6a4] shadow-xs space-y-4 animate-in fade-in">
          <div className="flex flex-wrap items-center justify-between gap-2 pb-2.5 border-b border-[#e5decfe6]">
            <div className="flex items-center gap-2">
              <span className="font-mono text-xs font-semibold text-[#996021] bg-[#efe2cc] px-2 py-0.5 rounded-md">
                04
              </span>
              <span className="text-sm font-semibold text-[#996021] flex items-center gap-1.5">
                <Sparkles size={15} />
                <span>飞牛 6-luna · 语境深度解读</span>
              </span>
            </div>

            {result && (
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={handleCopyResult}
                  className="inline-flex items-center gap-1 px-3 py-1.5 rounded-xl bg-[#fdfbf7] border border-[#ded6c6] text-xs font-medium text-[#5c5347] hover:bg-[#f1ece1] cursor-pointer"
                >
                  <Copy size={13} />
                  <span>复制解读</span>
                </button>
                <span className="inline-flex items-center gap-1 px-3 py-1.5 rounded-xl bg-[#e7f0e8] border border-[#c5dbc8] text-xs font-medium text-[#35593d]">
                  {savedNoteId ? <BookmarkCheck size={13} /> : <BookmarkPlus size={13} />}
                  <span>已自动收入笔记</span>
                </span>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void runInterpretation(sentence, sourceKind, matchedContext, true)}
                  className="inline-flex items-center gap-1 text-xs text-[#8c5820] hover:underline cursor-pointer"
                >
                  <RotateCw size={12} className={busy ? 'animate-spin' : ''} />
                  <span>重算</span>
                </button>
              </div>
            )}
          </div>

          {error ? (
            <p className="text-xs sm:text-sm text-[#94352b]">{error}</p>
          ) : (
            <MarkdownContent content={result} />
          )}
        </section>
      )}
    </div>
  )
}
