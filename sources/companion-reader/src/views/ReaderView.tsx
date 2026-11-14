import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AArrowDown,
  AArrowUp,
  BookOpen,
  BookOpenText,
  BookText,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Menu,
  Settings,
  Star,
  FileUp,
  Languages,
  Library,
  RotateCw,
  ScrollText,
  Trash2,
  X,
} from 'lucide-react'
import type { EbookChapter, EbookItem } from '../types/ebook'
import { parseEbookFile } from '../services/ebook'
import { dbService } from '../services/db'
import {
  getCachedParagraphTranslation,
  loadCachedParagraphTranslationAsync,
  translateBookParagraph,
} from '../services/ai'
import { ReaderWords } from '../components/ReaderWords'
import type { WordContextHint } from '../types/ebook'
import { ReaderDialog } from '../components/ReaderDialog'
import type { NavTab } from '../components/BottomNav'
import { AudioButton } from '../components/AudioButton'
import { MarkdownContent } from '../components/MarkdownContent'
import { PAGE_GAP, usePagedLayout, type AnchorTarget, type ReaderAnchor } from '../hooks/usePagedLayout'

type ReaderMode = 'paged' | 'scroll'
type LookupMode = 'word' | 'sentence'
type FontScale = 0 | 1 | 2 | 3

const FONT_PROFILES: Array<{ label: string; sizePx: number; lineHeight: number }> = [
  { label: '小', sizePx: 16, lineHeight: 1.72 },
  { label: '标准', sizePx: 18, lineHeight: 1.78 },
  { label: '大', sizePx: 20, lineHeight: 1.82 },
  { label: '特大', sizePx: 22, lineHeight: 1.86 },
]

const MODE_KEY = 'ebook_reader_mode'
const FONT_KEY = 'ebook_reader_font_scale'

interface ReaderViewProps {
  books: EbookItem[]
  activeBookId: string
  activeChapterId: string
  jumpParaIndex?: number | null
  highlightTerm?: string
  jumpVersion?: number
  onSelectBookChapter: (bookId: string, chapterId: string) => void
  onReadingProgress?: (
    bookId: string,
    chapterId: string,
    paraIndex: number,
    sentenceIndex?: number
  ) => void
  onBooksChanged: (books: EbookItem[], selectedBookId?: string, selectedChapterId?: string) => void
  onSelectWord: (term: string, hint: WordContextHint) => void
  onOpenInInterpret: (sentence: string, sourceKind: string) => void
  onOpenStarred: () => void
  onOpenSettings: () => void
  onImmersiveChange: (immersive: boolean) => void
  onNavigate: (tab: NavTab) => void
}

export const ReaderView: React.FC<ReaderViewProps> = ({
  books,
  activeBookId,
  activeChapterId,
  jumpParaIndex,
  highlightTerm,
  jumpVersion,
  onSelectBookChapter,
  onReadingProgress,
  onBooksChanged,
  onSelectWord,
  onOpenInInterpret,
  onOpenStarred,
  onOpenSettings,
  onImmersiveChange,
  onNavigate,
}) => {
  const [readerMode, setReaderMode] = useState<ReaderMode>(() =>
    localStorage.getItem(MODE_KEY) === 'scroll' ? 'scroll' : 'paged'
  )
  const [fontScale, setFontScale] = useState<FontScale>(() => {
    const raw = Number(localStorage.getItem(FONT_KEY) ?? 1)
    return raw === 0 || raw === 1 || raw === 2 || raw === 3 ? raw : 1
  })
  const [lookupMode, setLookupMode] = useState<LookupMode>(() =>
    localStorage.getItem('ebook_lookup_mode') === 'sentence' ? 'sentence' : 'word'
  )
  const [isShelfOpen, setIsShelfOpen] = useState(false)
  const [isCatalogOpen, setIsCatalogOpen] = useState(false)
  const [chapterQuery, setChapterQuery] = useState('')
  const [sectionFilter, setSectionFilter] = useState('all')
  const [importing, setImporting] = useState(false)
  const [importError, setImportError] = useState('')
  const [toastMsg, setToastMsg] = useState('')
  const [isMenuOpen, setIsMenuOpen] = useState(false)
  const [navigationVersion, setNavigationVersion] = useState(0)

  const [activeParaHighlight, setActiveParaHighlight] = useState<number | null>(null)

  const [selectedSentence, setSelectedSentence] = useState<{
    paraIndex: number
    sentenceIndex: number
    text: string
  } | null>(null)
  const [paraTranslations, setParaTranslations] = useState<Record<number, string>>({})
  const [paraLoading, setParaLoading] = useState<Record<number, boolean>>({})
  const [paraErrors, setParaErrors] = useState<Record<number, string>>({})
  const [translatingAll, setTranslatingAll] = useState(false)

  const fileInputRef = useRef<HTMLInputElement>(null)
  const paragraphRefs = useRef<Record<number, HTMLDivElement | null>>({})
  const anchorRef = useRef<ReaderAnchor>({ paraIndex: 1, sentenceIndex: 0 })
  const pendingTargetRef = useRef<AnchorTarget | null>(null)
  const lastChapterKeyRef = useRef('')
  const lastJumpVersionRef = useRef(jumpVersion)
  const touchStartRef = useRef<{ x: number; y: number; t: number } | null>(null)
  const ignoreClickUntilRef = useRef(0)

  const showToast = (msg: string) => {
    setToastMsg(msg)
    window.setTimeout(() => {
      setToastMsg((curr) => (curr === msg ? '' : curr))
    }, 2200)
  }

  const activeBook = useMemo(
    () => books.find((b) => b.id === activeBookId) || books[0] || null,
    [books, activeBookId]
  )

  const activeChapter: EbookChapter | null = useMemo(() => {
    if (!activeBook) return null
    return (
      activeBook.chapters.find((c) => c.id === activeChapterId) ||
      activeBook.chapters[0] ||
      null
    )
  }, [activeBook, activeChapterId])

  const chapterKey = activeBook && activeChapter ? `${activeBook.id}::${activeChapter.id}` : ''
  const fontProfile = FONT_PROFILES[fontScale]
  const isPaged = readerMode === 'paged'
  const immersive = isPaged && !!activeChapter
  const currentBookId = activeBook?.id

  useEffect(() => { onImmersiveChange(immersive) }, [immersive, onImmersiveChange])

  const resolveTarget = useCallback((): AnchorTarget => {
    if (lastJumpVersionRef.current !== jumpVersion && jumpParaIndex) {
      lastJumpVersionRef.current = jumpVersion
      pendingTargetRef.current = null
      lastChapterKeyRef.current = chapterKey
      const target = { paraIndex: jumpParaIndex, sentenceIndex: 0 }
      anchorRef.current = target
      return target
    }
    if (pendingTargetRef.current) {
      const pending = pendingTargetRef.current
      pendingTargetRef.current = null
      lastChapterKeyRef.current = chapterKey
      return pending
    }
    if (lastChapterKeyRef.current !== chapterKey) {
      lastChapterKeyRef.current = chapterKey
      if (activeBook && activeChapter && activeBook.lastChapterId === activeChapter.id) {
        const saved = {
          paraIndex: activeBook.lastParaIndex || 1,
          sentenceIndex: activeBook.lastSentenceIndex || 0,
        }
        anchorRef.current = saved
        return saved
      }
      anchorRef.current = { paraIndex: 1, sentenceIndex: 0 }
      return 'start'
    }
    return anchorRef.current
  }, [activeBook, activeChapter, chapterKey, jumpParaIndex, jumpVersion])

  const layoutKey = `${chapterKey}::q${lookupMode}::f${fontScale}::j${jumpVersion ?? 0}::n${navigationVersion}`
  const {
    viewportRef,
    contentRef,
    endMarkerRef,
    size: pageSize,
    columnWidth,
    contentWidth,
    step: pageStep,
    page,
    pageCount,
    animate: pageAnimate,
    goTo: goToPage,
    readAnchor,
    pageOf,
  } = usePagedLayout(isPaged && !!activeChapter, layoutKey, resolveTarget)

  useEffect(() => {
    if (!isPaged || !currentBookId || !activeChapter || !pageSize.width) return
    const currentAnchor = readAnchor(page)
    if (!currentAnchor) return
    anchorRef.current = currentAnchor
    onReadingProgress?.(
      currentBookId,
      activeChapter.id,
      currentAnchor.paraIndex,
      currentAnchor.sentenceIndex
    )
  }, [
    isPaged,
    currentBookId,
    activeChapter,
    page,
    pageCount,
    pageSize.width,
    readAnchor,
    onReadingProgress,
  ])

  useEffect(() => {
    setSelectedSentence(null)
    if (!currentBookId || !activeChapter) {
      setParaTranslations({})
      setParaLoading({})
      setParaErrors({})
      return
    }
    let cancelled = false
    const initialMap: Record<number, string> = {}
    const cKey = `${currentBookId}::${activeChapter.id}`
    activeChapter.paragraphs.forEach((p) => {
      const syncVal = getCachedParagraphTranslation(cKey, p.index)
      if (syncVal) {
        initialMap[p.index] = syncVal
      } else {
        loadCachedParagraphTranslationAsync(cKey, p.index).then((val) => {
          if (!cancelled && val) {
            setParaTranslations((prev) => ({ ...prev, [p.index]: val }))
          }
        })
      }
    })
    setParaTranslations(initialMap)
    setParaLoading({})
    setParaErrors({})
    return () => {
      cancelled = true
    }
  }, [currentBookId, activeChapter])

  useEffect(() => {
    if (jumpParaIndex && activeChapter) {
      setActiveParaHighlight(jumpParaIndex)
      const timer = window.setTimeout(() => {
        if (isPaged) return
        const el = paragraphRefs.current[jumpParaIndex]
        if (el) {
          el.scrollIntoView({ behavior: 'smooth', block: 'center' })
        }
      }, 150)
      const clearTimer = window.setTimeout(() => {
        setActiveParaHighlight((curr) => (curr === jumpParaIndex ? null : curr))
      }, 4500)
      return () => {
        window.clearTimeout(timer)
        window.clearTimeout(clearTimer)
      }
    }
  }, [jumpParaIndex, jumpVersion, activeChapter, isPaged])

  const availableSections = useMemo(() => {
    if (!activeBook) return []
    const set = new Set<string>()
    for (const c of activeBook.chapters) {
      if (c.section) {
        const topSec = c.section.split('·')[0].trim()
        if (topSec) set.add(topSec)
      }
    }
    return Array.from(set)
  }, [activeBook])

  const filteredChapters = useMemo(() => {
    if (!activeBook) return []
    return activeBook.chapters.filter((c) => {
      if (sectionFilter !== 'all') {
        const topSec = c.section ? c.section.split('·')[0].trim() : ''
        if (topSec !== sectionFilter) return false
      }
      if (chapterQuery.trim()) {
        const q = chapterQuery.trim().toLowerCase()
        const inMeta =
          c.title.toLowerCase().includes(q) ||
          (c.byline || '').toLowerCase().includes(q) ||
          (c.subtitle || '').toLowerCase().includes(q) ||
          (c.section || '').toLowerCase().includes(q)
        if (inMeta) return true
        return c.paragraphs.some((p) => p.text.toLowerCase().includes(q))
      }
      return true
    })
  }, [activeBook, sectionFilter, chapterQuery])

  const handleImportFiles = async (files: File[]) => {
    if (files.length === 0 || importing) return
    setImporting(true)
    setImportError('')
    try {
      let lastImported: EbookItem | null = null
      for (const file of files) {
        const parsed = await parseEbookFile(file)
        await dbService.saveBook(parsed)
        lastImported = parsed
      }
      const updatedBooks = await dbService.getAllBooks()
      if (lastImported) {
        onBooksChanged(
          updatedBooks,
          lastImported.id,
          lastImported.chapters[0]?.id || 'ch-1'
        )
        setIsShelfOpen(false)
        showToast(`已导入《${lastImported.title}》(${lastImported.chapters.length} 章)`)
      }
    } catch (err) {
      setImportError(err instanceof Error ? err.message : '导入电子书失败，请检查文件格式')
    } finally {
      setImporting(false)
    }
  }

  const handleDeleteBook = async (book: EbookItem) => {
    await dbService.deleteBook(book.id)
    const remaining = await dbService.getAllBooks()
    const nextBook = remaining[0]
    onBooksChanged(remaining, nextBook?.id || '', nextBook?.chapters[0]?.id || '')
    showToast(`已从书架移除《${book.title}》`)
  }

  const handleTranslateParagraph = async (paraIndex: number, text: string, force = false) => {
    if (!activeBook || !activeChapter || paraLoading[paraIndex]) return
    setParaLoading((prev) => ({ ...prev, [paraIndex]: true }))
    setParaErrors((prev) => ({ ...prev, [paraIndex]: '' }))
    try {
      const prevPara =
        paraIndex > 1
          ? activeChapter.paragraphs.find((p) => p.index === paraIndex - 1)?.text
          : undefined
      const res = await translateBookParagraph(
        {
          chapterKey,
          bookTitle: activeBook.title,
          chapterTitle: activeChapter.title,
          paraIndex,
          text,
          previousParagraph: prevPara,
        },
        force
      )
      setParaTranslations((prev) => ({ ...prev, [paraIndex]: res }))
    } catch (err) {
      setParaErrors((prev) => ({
        ...prev,
        [paraIndex]: err instanceof Error ? err.message : '翻译本段失败',
      }))
    } finally {
      setParaLoading((prev) => ({ ...prev, [paraIndex]: false }))
    }
  }

  const handleTranslateAllParagraphs = async () => {
    if (!activeChapter || translatingAll) return
    setTranslatingAll(true)
    try {
      for (const para of activeChapter.paragraphs) {
        if (paraTranslations[para.index]) continue
        await handleTranslateParagraph(para.index, para.text, false)
      }
    } finally {
      setTranslatingAll(false)
    }
  }

  const currentChapterPos = activeBook
    ? activeBook.chapters.findIndex((c) => c.id === activeChapter?.id)
    : -1
  const prevChapter =
    activeBook && currentChapterPos > 0 ? activeBook.chapters[currentChapterPos - 1] : null
  const nextChapter =
    activeBook &&
    currentChapterPos >= 0 &&
    currentChapterPos + 1 < activeBook.chapters.length
      ? activeBook.chapters[currentChapterPos + 1]
      : null

  const selectChapterWithTarget = useCallback(
    (bookId: string, chapterId: string, target: AnchorTarget = 'start') => {
      pendingTargetRef.current = target
      setNavigationVersion((current) => current + 1)
      setSelectedSentence(null)
      onSelectBookChapter(bookId, chapterId)
      if (!isPaged) window.scrollTo({ top: 0, behavior: 'smooth' })
    },
    [isPaged, onSelectBookChapter]
  )

  const turnPrev = useCallback(() => {
    if (!activeBook) return
    setSelectedSentence(null)
    if (page > 0) {
      goToPage(page - 1, true)
      return
    }
    if (prevChapter) {
      selectChapterWithTarget(activeBook.id, prevChapter.id, 'end')
      showToast(`已翻至上一章《${prevChapter.title}》末页`)
    } else {
      showToast('已是全书第一页')
    }
  }, [activeBook, page, goToPage, prevChapter, selectChapterWithTarget])

  const turnNext = useCallback(() => {
    if (!activeBook) return
    setSelectedSentence(null)
    if (page + 1 < pageCount) {
      goToPage(page + 1, true)
      return
    }
    if (nextChapter) {
      selectChapterWithTarget(activeBook.id, nextChapter.id, 'start')
      showToast(`已进入下一章《${nextChapter.title}》`)
    } else {
      showToast('已读完本章最后一页')
    }
  }, [activeBook, page, pageCount, goToPage, nextChapter, selectChapterWithTarget])

  useEffect(() => {
    if (!isPaged || !activeChapter || isShelfOpen || isCatalogOpen) return
    const onKeyDown = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null
      if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || document.querySelector('dialog[open], [role="dialog"]')) return
      const focusedWord = target?.closest('.reader-word')
      if (focusedWord && (e.key === ' ' || e.key === 'Enter')) return
      if (!focusedWord && target?.closest('input, textarea, select, button, a, [contenteditable="true"]')) return
      if (e.key === 'ArrowRight' || e.key === 'PageDown' || (e.key === ' ' && !e.shiftKey)) {
        e.preventDefault()
        turnNext()
      } else if (e.key === 'ArrowLeft' || e.key === 'PageUp' || (e.key === ' ' && e.shiftKey)) {
        e.preventDefault()
        turnPrev()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [isPaged, activeChapter, isShelfOpen, isCatalogOpen, turnNext, turnPrev])

  const toggleReaderMode = (next: ReaderMode) => {
    if (next === readerMode) return
    setIsMenuOpen(false)
    localStorage.setItem(MODE_KEY, next)
    setReaderMode(next)
    if (next === 'scroll') {
      const targetPara = anchorRef.current.paraIndex
      window.setTimeout(() => {
        paragraphRefs.current[targetPara]?.scrollIntoView({ behavior: 'smooth', block: 'center' })
      }, 80)
    }
  }

  const changeFontScale = (delta: -1 | 1) => {
    setFontScale((curr) => {
      const next = Math.min(3, Math.max(0, curr + delta)) as FontScale
      localStorage.setItem(FONT_KEY, String(next))
      return next
    })
  }

  const handleViewportTouchStart = (e: React.TouchEvent<HTMLDivElement>) => {
    if (e.touches.length !== 1) { touchStartRef.current = null; return }
    if (window.getSelection()?.toString().trim()) return
    const t = e.touches[0]
    touchStartRef.current = { x: t.clientX, y: t.clientY, t: Date.now() }
  }

  const handleViewportTouchEnd = (e: React.TouchEvent<HTMLDivElement>) => {
    const start = touchStartRef.current
    touchStartRef.current = null
    if (!start || e.changedTouches.length !== 1) return
    if (window.getSelection()?.toString().trim()) return
    const t = e.changedTouches[0]
    const dx = t.clientX - start.x
    const dy = t.clientY - start.y
    if (Date.now() - start.t < 650 && Math.abs(dx) >= 44 && Math.abs(dx) > Math.abs(dy) * 1.35) {
      ignoreClickUntilRef.current = Date.now() + 360
      if (dx < 0) turnNext()
      else turnPrev()
    }
  }

  const handleViewportClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (Date.now() < ignoreClickUntilRef.current) return
    if ((window.getSelection()?.toString() || '').trim().length > 0) return
    const target = e.target as HTMLElement
    if (target.closest('[data-s], button, a, input, [data-no-turn]')) return
    const rect = e.currentTarget.getBoundingClientRect()
    const ratio = (e.clientX - rect.left) / Math.max(1, rect.width)
    if (ratio <= 0.28) turnPrev()
    else if (ratio >= 0.72) turnNext()
    else setIsMenuOpen(true)
  }

  const handleSentenceClick = (event: React.MouseEvent<HTMLSpanElement>, text: string,
    paraIndex: number, sentenceIndex: number) => {
    event.stopPropagation()
    if (!activeBook || !activeChapter || Date.now() < ignoreClickUntilRef.current) return
    if (window.getSelection()?.toString().trim()) return
    anchorRef.current = { paraIndex, sentenceIndex }
    if (!isPaged) onReadingProgress?.(activeBook.id, activeChapter.id, paraIndex, sentenceIndex)
    if (lookupMode === 'word') {
      const word = (event.target as HTMLElement).closest<HTMLElement>('[data-word]')?.dataset.word
      if (!word) return
      onSelectWord(word, { bookId: activeBook.id, bookTitle: activeBook.title,
        chapterId: activeChapter.id, chapterTitle: activeChapter.title, paraIndex, sentence: text })
    } else {
      onOpenInInterpret(text, '来自书内点选')
    }
    setSelectedSentence({ paraIndex, sentenceIndex, text })
  }

  const lookupToggle = <button type="button" className="reader-action" data-no-turn
    aria-label={lookupMode === 'word' ? '当前点词查词，切换为点句解读' : '当前点句解读，切换为点词查词'}
    onClick={() => {
      const next = lookupMode === 'word' ? 'sentence' : 'word'
      localStorage.setItem('ebook_lookup_mode', next)
      setLookupMode(next)
      setSelectedSentence(null)
    }}>{lookupMode === 'word' ? '点词查词' : '点句解读'}</button>

  return (
    <div
      className={immersive ? "reader-immersive" : "max-w-5xl mx-auto px-3.5 sm:px-6 py-4 pb-28 space-y-4"}
    >
      {toastMsg && (
        <div className="fixed top-16 left-1/2 -translate-x-1/2 z-50 px-4 py-2 rounded-full bg-[#263c33] text-[#fdfbf7] text-xs font-medium shadow-lg animate-in fade-in">
          {toastMsg}
        </div>
      )}

      <input
        ref={fileInputRef}
        type="file"
        accept=".epub,.mobi,.azw,.azw3,.txt,.md,application/epub+zip,application/x-mobipocket-ebook"
        multiple
        hidden
        onChange={(e) => {
          const files = Array.from(e.target.files || [])
          e.target.value = ''
          void handleImportFiles(files)
        }}
      />

      {!immersive && <button type="button" className="reader-action" onClick={() => setIsMenuOpen(true)}><Menu size={18} />阅读菜单</button>}
      {isMenuOpen && <ReaderDialog title="阅读菜单" onClose={() => setIsMenuOpen(false)}>
      <div className="flex flex-wrap items-center gap-2 mb-3">{lookupToggle}<span className="text-xs">点词查看 AI 词义与词源；也可切换点句解读。</span></div>
      <div className="flex flex-wrap items-center justify-between gap-2.5 p-3.5 rounded-2xl bg-[#fdfbf7] border border-[#ded6c6] shadow-xs">
        <div className="flex flex-wrap items-center gap-2 min-w-0">
          <button
            type="button"
            onClick={() => {
              setIsMenuOpen(false)
              setIsShelfOpen((p) => !p)
              setIsCatalogOpen(false)
            }}
            className="flex items-center gap-1.5 px-3 py-2 rounded-xl bg-[#f8f5ef] border border-[#ded6c6] hover:bg-[#f1ece1] text-xs sm:text-sm font-semibold text-[#996021] cursor-pointer"
          >
            <Library size={15} />
            <span className="truncate max-w-[150px] sm:max-w-[220px]">
              {activeBook ? activeBook.title : '我的书架'}
            </span>
            <ChevronDown size={13} className={isShelfOpen ? 'rotate-180 transition-transform' : ''} />
          </button>

          {activeBook && (
            <button
              type="button"
              onClick={() => {
                setIsMenuOpen(false)
                setIsCatalogOpen((p) => !p)
                setIsShelfOpen(false)
              }}
              className="flex items-center gap-1.5 px-3 py-2 rounded-xl bg-[#f8f5ef] border border-[#ded6c6] hover:bg-[#f1ece1] text-xs sm:text-sm font-medium text-[#2e2a24] cursor-pointer"
            >
              <BookText size={15} className="text-[#996021] shrink-0" />
              <span className="text-[#996021] font-semibold shrink-0">
                第 {activeChapter?.index || 1}/{activeBook.chapters.length} 章
              </span>
              <span className="truncate max-w-[150px] sm:max-w-xs">{activeChapter?.title}</span>
              <ChevronDown
                size={13}
                className={isCatalogOpen ? 'rotate-180 transition-transform' : ''}
              />
            </button>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2 text-xs">
          <div className="inline-flex items-center rounded-xl bg-[#f1ece1] p-0.5 border border-[#ded6c6]">
            <button
              type="button"
              onClick={() => toggleReaderMode('paged')}
              className={`px-2.5 py-1.5 rounded-lg font-semibold inline-flex items-center gap-1 cursor-pointer transition-colors ${
                isPaged ? 'bg-[#996021] text-[#fdfbf7] shadow-2xs' : 'text-[#5c5347] hover:text-[#2e2a24]'
              }`}
              title="电子书智能分栏翻页（长章节自动切页，按左/右翻页）"
            >
              <BookOpenText size={13} />
              <span>书页翻页</span>
            </button>
            <button
              type="button"
              onClick={() => toggleReaderMode('scroll')}
              className={`px-2.5 py-1.5 rounded-lg font-semibold inline-flex items-center gap-1 cursor-pointer transition-colors ${
                !isPaged ? 'bg-[#996021] text-[#fdfbf7] shadow-2xs' : 'text-[#5c5347] hover:text-[#2e2a24]'
              }`}
              title="卡片长卷滚动模式"
            >
              <ScrollText size={13} />
              <span>长卷精读</span>
            </button>
          </div>

          <div className="inline-flex items-center rounded-xl bg-[#f8f5ef] border border-[#ded6c6] px-1 py-0.5 text-[#5c5347]">
            <button
              type="button"
              disabled={fontScale === 0}
              onClick={() => changeFontScale(-1)}
              className="p-1.5 rounded-lg hover:bg-[#efe2cc] disabled:opacity-40 cursor-pointer"
              title="缩小字号（自动重新分页）"
            >
              <AArrowDown size={14} />
            </button>
            <span className="px-1.5 text-[11px] font-medium text-[#72685b] select-none">
              {fontProfile.label}
            </span>
            <button
              type="button"
              disabled={fontScale === 3}
              onClick={() => changeFontScale(1)}
              className="p-1.5 rounded-lg hover:bg-[#efe2cc] disabled:opacity-40 cursor-pointer"
              title="放大字号（自动重新分页）"
            >
              <AArrowUp size={14} />
            </button>
          </div>

          <button
            type="button"
            disabled={importing}
            onClick={() => fileInputRef.current?.click()}
            className="px-3 py-2 rounded-xl bg-[#996021] hover:brightness-95 text-[#fdfbf7] font-semibold inline-flex items-center gap-1.5 cursor-pointer disabled:opacity-50"
          >
            <FileUp size={14} />
            <span>{importing ? '正在解析...' : '+ 导入书籍'}</span>
          </button>

          {activeChapter && (
            <button
              type="button"
              disabled={translatingAll || !activeChapter.paragraphs.length}
              onClick={() => void handleTranslateAllParagraphs()}
              className="px-3 py-2 rounded-xl border border-[#d6b483] bg-[#efe2cc] hover:bg-[#e5d2b2] text-[#7a4b19] font-semibold inline-flex items-center gap-1.5 cursor-pointer disabled:opacity-60"
            >
              <Languages size={14} className="text-[#996021]" />
              <span>{translatingAll ? '逐段译注中...' : '全章译注（长卷精读）'}</span>
            </button>
          )}
        </div>
      </div>

        {activeChapter && <div className="space-y-3 pt-4">
          <label className="block text-sm" htmlFor="reader-page-range">本章进度 · 第 {page + 1} / {pageCount} 页</label>
          <input id="reader-page-range" type="range" min={1} max={pageCount} value={page + 1}
            className="w-full h-11 accent-[#996021]" onChange={(event) => goToPage(Number(event.target.value) - 1, false)} />
          <div className="flex gap-2">
            <button type="button" className="reader-action" disabled={!prevChapter} onClick={() => {
              if (activeBook && prevChapter) selectChapterWithTarget(activeBook.id, prevChapter.id)
              setIsMenuOpen(false)
            }}>上一章</button>
            <button type="button" className="reader-action" disabled={!nextChapter} onClick={() => {
              if (activeBook && nextChapter) selectChapterWithTarget(activeBook.id, nextChapter.id)
              setIsMenuOpen(false)
            }}>下一章</button>
          </div>
        </div>}
        <div className="reader-menu-links">
          <button type="button" className="reader-action" onClick={() => { setIsMenuOpen(false); onOpenStarred() }}><Star size={16} />收藏</button>
          <button type="button" className="reader-action" onClick={() => { setIsMenuOpen(false); onOpenSettings() }}><Settings size={16} />设置</button>
          <button type="button" className="reader-action" onClick={() => onNavigate('notebook')}>笔记与生词</button>
          <button type="button" className="reader-action" onClick={() => onNavigate('interpret')}>句段解读</button>
          <button type="button" className="reader-action" onClick={() => onNavigate('tv')}>美剧台词</button>
        </div>
      </ReaderDialog>}

      {importError && (
        <div className="p-3.5 rounded-2xl bg-[#f8e5e2] border border-[#e4bfb8] text-xs sm:text-sm text-[#94352b] flex items-center justify-between">
          <span>{importError}</span>
          <button type="button" onClick={() => setImportError('')} className="p-1">
            <X size={14} />
          </button>
        </div>
      )}

      {/* Bookshelf Drawer */}
      {isShelfOpen && (<ReaderDialog title="我的书架" onClose={() => setIsShelfOpen(false)}>
        <div className="p-4 sm:p-5 rounded-3xl bg-[#fdfbf7] border border-[#ded6c6] shadow-lg space-y-3 animate-in fade-in">
          <div className="flex items-center justify-between pb-2 border-b border-[#e5decfe6]">
            <div>
              <h3 className="font-semibold text-sm text-[#2e2a24]">
                已导入电子书架 ({books.length} 本)
              </h3>
              <p className="text-[11px] text-[#72685b]">
                支持 EPUB、MOBI、AZW3 或 TXT，点选句子时自动匹配书中上下文。
              </p>
            </div>
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              className="px-3 py-1.5 rounded-xl bg-[#996021] text-[#fdfbf7] text-xs font-medium inline-flex items-center gap-1 cursor-pointer"
            >
              <FileUp size={13} />
              <span>导入新书</span>
            </button>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            {books.map((b) => {
              const isCurrent = b.id === activeBook?.id
              return (
                <div
                  key={b.id}
                  onClick={() => {
                    selectChapterWithTarget(
                      b.id,
                      b.lastChapterId || b.chapters[0]?.id || 'ch-1',
                      {
                        paraIndex: b.lastParaIndex || 1,
                        sentenceIndex: b.lastSentenceIndex || 0,
                      }
                    )
                    setIsShelfOpen(false)
                  }}
                  className={`p-3.5 rounded-2xl border transition-all cursor-pointer flex items-center justify-between gap-3 ${
                    isCurrent
                      ? 'bg-[#efe2cc] border-[#c89d66]'
                      : 'bg-[#f8f5ef] border-[#ded6c6] hover:bg-[#f1ece1]'
                  }`}
                >
                  <div className="flex items-center gap-3 min-w-0">
                    {b.coverDataUrl ? (
                      <img
                        src={b.coverDataUrl}
                        alt={b.title}
                        className="w-11 h-15 object-cover rounded-lg border border-[#ded6c6] shrink-0"
                      />
                    ) : (
                      <div className="w-11 h-15 rounded-lg bg-[#e6dac6] flex items-center justify-center text-[#996021] shrink-0">
                        <BookOpen size={18} />
                      </div>
                    )}
                    <div className="min-w-0 space-y-0.5">
                      <div className="font-semibold text-sm text-[#2e2a24] truncate">{b.title}</div>
                      <div className="text-xs text-[#72685b] truncate">{b.author}</div>
                      <div className="text-[11px] text-[#8a7f70]">
                        {b.chapters.length} 章 · {(b.totalWords / 1000).toFixed(1)}k 词 ·{' '}
                        {(b.format || 'epub').toUpperCase()}
                      </div>
                    </div>
                  </div>

                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation()
                      void handleDeleteBook(b)
                    }}
                    className="p-2 rounded-xl text-[#8a7f70] hover:text-[#94352b] hover:bg-[#f1ece1] shrink-0"
                    title="从书架移除"
                  >
                    <Trash2 size={15} />
                  </button>
                </div>
              )
            })}
          </div>
        </div>
      </ReaderDialog>)}

      {/* Chapter Catalog Drawer */}
      {isCatalogOpen && activeBook && (<ReaderDialog title="章节目录" onClose={() => setIsCatalogOpen(false)}>
        <div className="p-4 sm:p-5 rounded-3xl bg-[#fdfbf7] border border-[#ded6c6] shadow-lg space-y-3 animate-in fade-in">
          <div className="flex flex-wrap items-center justify-between gap-2 pb-2 border-b border-[#e5decfe6]">
            <h3 className="font-semibold text-sm text-[#2e2a24]">
              《{activeBook.title}》目录 ({filteredChapters.length} 章)
            </h3>
            <input
              type="text"
              value={chapterQuery}
              onChange={(e) => setChapterQuery(e.target.value)}
              placeholder="搜章节标题、作者或书中句子..."
              className="w-56 px-3 py-1.5 text-xs rounded-xl bg-[#f8f5ef] border border-[#ded6c6] text-[#2e2a24] placeholder-[#8a7f70] focus:outline-hidden"
            />
          </div>

          {availableSections.length > 0 && (
            <div className="flex gap-1.5 overflow-x-auto pb-1 text-xs">
              <button
                type="button"
                onClick={() => setSectionFilter('all')}
                className={`px-2.5 py-1 rounded-xl shrink-0 cursor-pointer ${
                  sectionFilter === 'all'
                    ? 'bg-[#996021] text-[#fdfbf7] font-medium'
                    : 'bg-[#f1ece1] text-[#72685b]'
                }`}
              >
                全部栏目
              </button>
              {availableSections.map((sec) => (
                <button
                  key={sec}
                  type="button"
                  onClick={() => setSectionFilter(sec)}
                  className={`px-2.5 py-1 rounded-xl shrink-0 cursor-pointer ${
                    sectionFilter === sec
                      ? 'bg-[#996021] text-[#fdfbf7] font-medium'
                      : 'bg-[#f1ece1] text-[#72685b]'
                  }`}
                >
                  {sec}
                </button>
              ))}
            </div>
          )}

          <div className="max-h-80 overflow-y-auto space-y-1.5 pr-1 divide-y divide-[#ede6d8]">
            {filteredChapters.map((chap) => (
              <div
                key={chap.id}
                onClick={() => {
                  selectChapterWithTarget(activeBook.id, chap.id, 'start')
                  setIsCatalogOpen(false)
                }}
                className={`p-2.5 rounded-xl cursor-pointer flex items-center justify-between gap-3 transition-colors ${
                  chap.id === activeChapter?.id
                    ? 'bg-[#efe2cc] border border-[#d6b483]'
                    : 'hover:bg-[#f3ece0]'
                }`}
              >
                <div className="min-w-0 space-y-0.5">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-xs font-bold text-[#996021]">
                      #{chap.index}
                    </span>
                    {chap.section && (
                      <span className="px-2 py-0.5 rounded-md bg-[#f1ece1] text-[#72685b] text-[11px]">
                        {chap.section}
                      </span>
                    )}
                    <span className="font-semibold text-xs sm:text-sm text-[#2e2a24] truncate">
                      {chap.title}
                    </span>
                  </div>
                  {(chap.byline || chap.subtitle) && (
                    <div className="text-[11px] text-[#72685b] truncate">
                      {chap.byline ? `${chap.byline} · ` : ''}
                      {chap.subtitle || ''}
                    </div>
                  )}
                </div>
                <span className="text-[11px] text-[#8a7f70] shrink-0">
                  {chap.paragraphs.length} 段 · {chap.wordCount} 词
                </span>
              </div>
            ))}
          </div>
        </div>
      </ReaderDialog>)}

      {/* Empty Bookshelf State */}
      {!activeBook || !activeChapter ? (
        <div className="p-12 rounded-3xl bg-[#fdfbf7] border border-[#ded6c6] text-center space-y-4">
          <BookOpen size={36} className="mx-auto text-[#996021]" />
          <div className="space-y-1">
            <h2 className="text-lg font-bold text-[#2e2a24]">导入你的第一本英文电子书</h2>
            <p className="text-xs sm:text-sm text-[#72685b]">
              支持 EPUB、MOBI、AZW3 或 TXT。导入后可直接点词查词，或切换为点句解读。
            </p>
          </div>
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            className="px-5 py-2.5 rounded-2xl bg-[#996021] text-[#fdfbf7] text-sm font-semibold inline-flex items-center gap-2 cursor-pointer"
          >
            <FileUp size={16} />
            <span>选择 EPUB / MOBI 电子书导入</span>
          </button>
        </div>
      ) : isPaged ? (
        <div className="reader-book">
          <div className="reader-running-header">
            <button type="button" aria-label="打开目录" onClick={() => setIsCatalogOpen(true)}><BookText size={18} /></button>
            <span>{activeChapter.title}</span>
            {lookupToggle}
            <button type="button" aria-label="阅读菜单" onClick={() => setIsMenuOpen(true)}><Menu size={20} /></button>
          </div>
          <div className="reader-page-area">
            <div ref={viewportRef} className="reader-viewport" onClick={handleViewportClick}
              onTouchStart={handleViewportTouchStart} onTouchEnd={handleViewportTouchEnd}
              onTouchCancel={() => { touchStartRef.current = null }}>
              <div
                ref={contentRef}
                style={{
                  height: pageSize.height ? `${pageSize.height}px` : '62vh',
                  width: contentWidth ? `${contentWidth}px` : '100%',
                  columnWidth: columnWidth ? `${columnWidth}px` : undefined,
                  columnGap: `${PAGE_GAP}px`,
                  columnFill: 'auto',
                  transform: `translate3d(-${page * pageStep}px, 0, 0)`,
                  transition: pageAnimate
                    ? 'transform 260ms cubic-bezier(0.22, 1, 0.36, 1)'
                    : 'none',
                  fontSize: `${fontProfile.sizePx}px`,
                  lineHeight: fontProfile.lineHeight,
                }}
                className="reader-flow font-serif text-[#2e2a24]"
              >
                {/* Chapter Opening Title Block on Page 1 */}
                <div className="mb-5 pb-4 border-b border-[#e5decfe6] break-inside-avoid">
                  <div className="flex flex-wrap items-center gap-2 mb-2 text-xs font-sans text-[#8a7f70]">
                    <span className="px-2 py-0.5 rounded-full bg-[#efe2cc] text-[#7a4b19] font-semibold">
                      第 {activeChapter.index} 章
                    </span>
                    {activeChapter.byline && <span>{activeChapter.byline}</span>}
                    <span>· {activeChapter.wordCount} 词</span>
                  </div>
                  <h2 className="text-xl sm:text-2xl font-bold text-[#2e2a24] leading-snug">
                    {activeChapter.title}
                  </h2>
                  {activeChapter.subtitle && (
                    <p className="mt-1.5 text-sm text-[#5c5347] italic leading-relaxed">
                      {activeChapter.subtitle}
                    </p>
                  )}
                </div>

                {/* Continuous Flowing Paragraphs */}
                {activeChapter.paragraphs.map((para) => {
                  const isHighlightedPara = activeParaHighlight === para.index
                  
                  return (
                    <div
                      key={para.index}
                      data-p={para.index}
                      className={`mb-4 rounded-xl transition-colors ${
                        isHighlightedPara ? 'bg-[#f8efe0] ring-1 ring-[#c89d66] px-2 py-1' : ''
                      }`}
                    >
                      <p className="text-justify">
                        <span className="inline-block mr-1.5 font-sans text-[11px] font-semibold text-[#996021]/80 select-none">
                          ¶{para.index}
                        </span>
                        {para.sentences.map((sent, sIdx) => {
                          const isSelected =
                            selectedSentence?.paraIndex === para.index &&
                            selectedSentence?.sentenceIndex === sIdx
                          const hasHighlightTerm =
                            highlightTerm &&
                            sent.toLowerCase().includes(highlightTerm.toLowerCase())
                          return (
                            <React.Fragment key={sIdx}>
                              <span
                                data-p={para.index}
                                data-s={sIdx}
                                onClick={(e) => handleSentenceClick(e, sent, para.index, sIdx)}
                                role={lookupMode === 'sentence' ? 'button' : undefined}
                                tabIndex={lookupMode === 'sentence' ? 0 : undefined}
                                onFocus={(e) => { goToPage(pageOf(e.target as HTMLElement), false) }}
                                onKeyDown={(e) => {
                                  if (lookupMode === 'sentence' && (e.key === 'Enter' || e.key === ' ')) {
                                    e.preventDefault(); e.stopPropagation(); e.currentTarget.click()
                                  }
                                }}
                                className={`rounded-md px-0.5 transition-colors cursor-pointer ${
                                  isSelected
                                    ? 'bg-[#f2d6a2] text-[#3d2308]'
                                    : hasHighlightTerm
                                    ? 'bg-[#efe2cc] underline decoration-[#996021]'
                                    : 'hover:bg-[#f3ece0]'
                                }`}
                              >
                                {lookupMode === 'word' ? <ReaderWords text={sent} /> : sent}
                              </span>{' '}
                            </React.Fragment>
                          )
                        })}
                      </p>

                    </div>
                  )
                })}
                <span ref={endMarkerRef} aria-hidden="true" className="inline-block w-0 h-0" />
              </div>
            </div>
          </div>

          <div className="reader-page-footer">
            <button type="button" aria-label="上一页" disabled={page === 0 && !prevChapter} onClick={turnPrev}><ChevronLeft size={18} /></button>
            <button type="button" className="reader-page-status" onClick={() => setIsMenuOpen(true)} aria-label="查看阅读进度">
              第 {page + 1} / {pageCount} 页 · 第 {activeChapter.index} 章
            </button>
            <button type="button" aria-label="下一页" disabled={page + 1 >= pageCount && !nextChapter} onClick={turnNext}><ChevronRight size={18} /></button>
          </div>
        </div>
      ) : (
        <div className="space-y-5">
          {/* Chapter Header Card */}
          <div className="p-5 sm:p-7 rounded-3xl bg-[#fdfbf7] border border-[#ded6c6] shadow-xs space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="px-2.5 py-0.5 rounded-full bg-[#efe2cc] text-[#7a4b19] text-xs font-semibold border border-[#dfc6a4]">
                  《{activeBook.title}》 · 第 {activeChapter.index} 章
                </span>
                {activeChapter.section && (
                  <span className="px-2.5 py-0.5 rounded-full bg-[#f1ece1] text-[#5c5347] text-xs border border-[#ded6c6]">
                    {activeChapter.section}
                  </span>
                )}
                {activeChapter.byline && (
                  <span className="text-xs font-medium text-[#72685b]">
                    {activeChapter.byline}
                  </span>
                )}
              </div>
              <span className="text-xs text-[#8a7f70]">
                共 {activeChapter.paragraphs.length} 段 · {activeChapter.wordCount} 词
              </span>
            </div>

            <h2 className="text-2xl sm:text-3xl font-bold text-[#2e2a24] tracking-tight font-serif">
              {activeChapter.title}
            </h2>

            {activeChapter.subtitle && (
              <p className="text-sm sm:text-base text-[#5c5347] font-serif italic leading-relaxed">
                {activeChapter.subtitle}
              </p>
            )}

            <div className="pt-1 text-xs text-[#72685b] flex flex-wrap items-center gap-2">
              <span className="px-2.5 py-1 rounded-xl bg-[#f8f5ef] border border-[#e5decfe6]">
                点词查词或点句解读，可在阅读菜单切换；关闭弹窗继续阅读。
              </span>
            </div>
          </div>

          {/* Paragraphs Workbench */}
          <div className="space-y-4">
            {activeChapter.paragraphs.map((para) => {
              return (
                <div
                  key={para.index}
                  ref={(el) => {
                    paragraphRefs.current[para.index] = el
                  }}
                  className={`p-5 sm:p-7 rounded-3xl border shadow-xs space-y-4 transition-all ${
                    activeParaHighlight === para.index
                      ? 'bg-[#f8efe0] border-[#c89d66] ring-2 ring-[#c89d66]'
                      : 'bg-[#fdfbf7] border-[#ded6c6]'
                  }`}
                >
                  <div className="flex flex-wrap items-center justify-between gap-2 pb-2 border-b border-[#e5decfe6] text-xs text-[#72685b]">
                    <div className="flex items-center gap-2">
                      <span className="font-semibold text-[#996021]">第 {para.index} 段</span>
                      <span className="text-[11px] text-[#8a7f70]">
                        ({para.sentences.length} 句 · 支持点词查词与点句解读)
                      </span>
                    </div>

                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        disabled={!!paraLoading[para.index]}
                        onClick={() =>
                          void handleTranslateParagraph(
                            para.index,
                            para.text,
                            !!paraTranslations[para.index]
                          )
                        }
                        className="inline-flex items-center gap-1 px-2.5 py-1 rounded-full bg-[#efe2cc] hover:bg-[#e5d2b2] border border-[#d6b483] text-[#7a4b19] font-semibold text-xs cursor-pointer disabled:opacity-60"
                      >
                        <Languages size={13} className="text-[#996021]" />
                        <span>
                          {paraLoading[para.index]
                            ? '6-luna 译注中...'
                            : paraTranslations[para.index]
                            ? '重译本段'
                            : '✨ 译注本段'}
                        </span>
                      </button>
                      <AudioButton term={para.text} size="sm" labelText="读本段" />
                    </div>
                  </div>

                  <div
                    style={{
                      fontSize: `${fontProfile.sizePx}px`,
                      lineHeight: fontProfile.lineHeight,
                    }}
                    className="text-[#2e2a24] font-serif space-x-1"
                  >
                    {para.sentences.map((sent, sIdx) => {
                      const isSelected =
                        selectedSentence?.paraIndex === para.index &&
                        selectedSentence?.sentenceIndex === sIdx
                      const hasHighlightTerm =
                        highlightTerm &&
                        sent.toLowerCase().includes(highlightTerm.toLowerCase())
                      return (
                        <span
                          key={sIdx}
                          onClick={(e) => handleSentenceClick(e, sent, para.index, sIdx)}
                          role={lookupMode === 'sentence' ? 'button' : undefined}
                          tabIndex={lookupMode === 'sentence' ? 0 : undefined}
                          onKeyDown={(e) => {
                            if (lookupMode === 'sentence' && (e.key === 'Enter' || e.key === ' ')) {
                              e.preventDefault(); e.stopPropagation(); e.currentTarget.click()
                            }
                          }}
                          className={`rounded-lg px-1 py-0.5 transition-colors cursor-pointer ${
                            isSelected
                              ? 'bg-[#f2d6a2] text-[#3d2308] font-medium shadow-2xs'
                              : hasHighlightTerm
                              ? 'bg-[#efe2cc] underline decoration-[#996021]'
                              : 'hover:bg-[#f3ece0]'
                          }`}
                          title={lookupMode === 'word' ? '点按单词查询词义' : '点按句子解读'}
                        >
                          {lookupMode === 'word' ? <ReaderWords text={sent} /> : sent}
                        </span>
                      )
                    })}
                  </div>

                  {(paraTranslations[para.index] || paraErrors[para.index]) && (
                    <div className="p-4 rounded-2xl bg-[#f8f2e7] border border-[#dfc6a4] space-y-2 animate-in fade-in">
                      <div className="flex items-center justify-between">
                        <span className="text-xs font-semibold text-[#996021] flex items-center gap-1.5">
                          <Languages size={14} />
                          <span>飞牛 6-luna · 第 {para.index} 段全文译注</span>
                        </span>
                        {paraTranslations[para.index] && (
                          <button
                            type="button"
                            disabled={!!paraLoading[para.index]}
                            onClick={() =>
                              void handleTranslateParagraph(para.index, para.text, true)
                            }
                            className="inline-flex items-center gap-1 text-xs text-[#8c5820] hover:underline cursor-pointer"
                          >
                            <RotateCw
                              size={12}
                              className={paraLoading[para.index] ? 'animate-spin' : ''}
                            />
                            重新译注
                          </button>
                        )}
                      </div>
                      {paraErrors[para.index] ? (
                        <p className="text-xs text-[#94352b]">{paraErrors[para.index]}</p>
                      ) : (
                        <MarkdownContent content={paraTranslations[para.index]} />
                      )}
                    </div>
                  )}
                </div>
              )
            })}
          </div>

          <div className="flex items-center justify-between gap-3 pt-2">
            {prevChapter ? (
              <button
                type="button"
                onClick={() => {
                  selectChapterWithTarget(activeBook.id, prevChapter.id, 'start')
                }}
                className="flex-1 p-3.5 rounded-2xl bg-[#fdfbf7] border border-[#ded6c6] hover:bg-[#f1ece1] text-left flex items-center gap-2 cursor-pointer"
              >
                <ChevronLeft size={18} className="text-[#996021] shrink-0" />
                <div className="min-w-0">
                  <div className="text-[11px] text-[#8a7f70]">上一章</div>
                  <div className="text-xs sm:text-sm font-semibold text-[#2e2a24] truncate">
                    {prevChapter.title}
                  </div>
                </div>
              </button>
            ) : (
              <div className="flex-1" />
            )}

            {nextChapter ? (
              <button
                type="button"
                onClick={() => {
                  selectChapterWithTarget(activeBook.id, nextChapter.id, 'start')
                }}
                className="flex-1 p-3.5 rounded-2xl bg-[#fdfbf7] border border-[#ded6c6] hover:bg-[#f1ece1] text-right flex items-center justify-end gap-2 cursor-pointer"
              >
                <div className="min-w-0">
                  <div className="text-[11px] text-[#8a7f70]">下一章</div>
                  <div className="text-xs sm:text-sm font-semibold text-[#2e2a24] truncate">
                    {nextChapter.title}
                  </div>
                </div>
                <ChevronRight size={18} className="text-[#996021] shrink-0" />
              </button>
            ) : (
              <div className="flex-1" />
            )}
          </div>
        </div>
      )}
    </div>
  )
}
