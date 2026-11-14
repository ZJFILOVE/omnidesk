import { useEffect, useMemo, useState } from 'react'
import { BookOpen, Copy, RotateCw } from 'lucide-react'
import type { EbookItem } from '../types/ebook'
import { findSentenceContextInBooks } from '../services/ebook'
import { copyTextToSystemClipboard, interpretSentenceWithContext, translateBookParagraph } from '../services/ai'
import { dbService } from '../services/db'
import { AudioButton } from './AudioButton'
import { MarkdownContent } from './MarkdownContent'
import { extractReaderWords } from '../services/readerWords'
import { ReaderDialog } from './ReaderDialog'

interface SentenceQueryModalProps {
  sentence: string
  sourceKind: string
  books: EbookItem[]
  activeBookId: string
  activeChapterId: string
  onClose: () => void
  onSelectWord: (term: string, hint?: {
    bookId?: string; bookTitle?: string; chapterId?: string; chapterTitle?: string
    paraIndex?: number; sentence?: string
  }) => void
  onFollowUp: () => void
}

export function SentenceQueryModal({ sentence, sourceKind, books, activeBookId,
  activeChapterId, onClose, onSelectWord, onFollowUp }: SentenceQueryModalProps) {
  const context = useMemo(() => findSentenceContextInBooks(sentence, books, activeBookId, activeChapterId),
    [sentence, books, activeBookId, activeChapterId])
  const [result, setResult] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(true)
  const [retry, setRetry] = useState(0)
  const [paragraph, setParagraph] = useState('')
  const [paragraphBusy, setParagraphBusy] = useState(false)
  const [paragraphError, setParagraphError] = useState('')
  const [notice, setNotice] = useState('')
  const words = useMemo(() => extractReaderWords(sentence), [sentence])

  useEffect(() => {
    let cancelled = false
    setBusy(true)
    setError('')
    interpretSentenceWithContext({ sentence, presets: ['context', 'expressions'], context }, retry > 0)
      .then(async (output) => {
        if (cancelled) return
        setResult(output)
        try {
          await dbService.saveSentenceNote({
            id: `note-${Date.now().toString(36)}`, createdAt: Date.now(), updatedAt: Date.now(),
            sentence, sourceKind, presets: ['context', 'expressions'], aiInterpretation: output, context,
          })
        } catch {
          if (!cancelled) setNotice('解读已完成，但笔记保存失败')
        }
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : '查询失败，请重试')
      })
      .finally(() => { if (!cancelled) setBusy(false) })
    return () => { cancelled = true }
  }, [sentence, sourceKind, context, retry])

  const translateParagraph = async () => {
    if (!context || paragraphBusy) return
    setParagraphBusy(true)
    setParagraphError('')
    try {
      setParagraph(await translateBookParagraph({
        chapterKey: `${context.bookId}::${context.chapterId}`, bookTitle: context.bookTitle,
        chapterTitle: context.chapterTitle, paraIndex: context.paraIndex,
        text: context.currentParagraph, previousParagraph: context.previousParagraph,
      }))
    } catch (reason) {
      setParagraphError(reason instanceof Error ? reason.message : '本段翻译失败')
    } finally { setParagraphBusy(false) }
  }

  return (
    <ReaderDialog title="句子解读" onClose={onClose}>
      <div className="space-y-4">
        {context && <p className="text-xs text-[#72685b]">{context.chapterTitle} · 第 {context.paraIndex} 段</p>}
        <blockquote className="font-serif text-base leading-relaxed border-l-2 border-[#c89d66] pl-3">{sentence}</blockquote>
        <div className="flex flex-wrap gap-2">
          <AudioButton term={sentence} size="sm" labelText="朗读原句" />
          <button type="button" className="reader-action" onClick={async () => {
            setNotice(await copyTextToSystemClipboard(result || sentence) ? '已复制' : '复制失败')
          }}><Copy size={15} />复制</button>
          {context && <button type="button" className="reader-action" disabled={paragraphBusy}
            onClick={() => void translateParagraph()}><BookOpen size={15} />{paragraphBusy ? '正在翻译本段…' : '译注本段'}</button>}
        </div>
        <div aria-live="polite" aria-busy={busy}>
          {busy ? <p className="py-4 text-sm text-[#72685b]">正在结合上下文解读…</p>
            : error ? <p role="alert" className="text-sm text-[#94352b]">{error}</p>
            : <MarkdownContent content={result} />}
        </div>
        {paragraphError && <p role="alert" className="text-sm text-[#94352b]">{paragraphError}</p>}
        {paragraph && <section className="border-t border-[#ded6c6] pt-3 space-y-2">
          <h3 className="text-sm font-semibold">本段译注</h3><MarkdownContent content={paragraph} />
        </section>}
        {words.length > 0 && <details open>
          <summary className="reader-action cursor-pointer">查询句中单词</summary>
          <div className="flex flex-wrap gap-2 pt-2">{words.map((word) => <button type="button" key={word}
            className="reader-action font-serif" onClick={() => {
              onClose()
              onSelectWord(word, context ? { ...context, sentence } : { sentence })
            }}>{word}</button>)}</div>
        </details>}
        <div className="flex flex-wrap gap-2 border-t border-[#ded6c6] pt-3">
          <button type="button" className="reader-action" disabled={busy} onClick={() => setRetry((current) => current + 1)}>
            <RotateCw size={15} />{error ? '重试' : '重新解读'}</button>
          <button type="button" className="reader-action" onClick={onFollowUp}>继续追问</button>
          <button type="button" className="reader-action reader-primary" onClick={onClose}>继续阅读</button>
        </div>
        {notice && <p role="status" className="text-xs text-[#72685b]">{notice}</p>}
      </div>
    </ReaderDialog>
  )
}
