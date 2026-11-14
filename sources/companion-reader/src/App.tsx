import { useCallback, useEffect, useState } from 'react'
import type { NavTab } from './components/BottomNav'
import { BottomNav } from './components/BottomNav'
import { Header } from './components/Header'
import { ReaderView } from './views/ReaderView'
import { InterpretView } from './views/InterpretView'
import { NotebookView } from './views/NotebookView'
import { TVView } from './views/TVView'
import { WordModal } from './components/WordModal'
import { StarredDrawer } from './components/StarredDrawer'
import { StatsModal } from './components/StatsModal'
import { SettingsModal } from './components/SettingsModal'
import type { EbookItem } from './types/ebook'
import { dbService } from './services/db'
import { SentenceQueryModal } from './components/SentenceQueryModal'
import './reader.css'

export function App() {
  const [activeTab, setActiveTab] = useState<NavTab>('reader')
  const [books, setBooks] = useState<EbookItem[]>([])
  const [activeBookId, setActiveBookId] = useState<string>('')
  const [activeChapterId, setActiveChapterId] = useState<string>('')
  const [jumpState, setJumpState] = useState<{
    paraIndex: number | null
    highlightTerm: string
    version: number
  }>({ paraIndex: null, highlightTerm: '', version: 0 })

  // Sentence interpretation workbench state
  const [interpretInput, setInterpretInput] = useState<{
    sentence: string
    sourceKind: string
    version: number
  }>({ sentence: '', sourceKind: '手动输入', version: 0 })

  const [queryInput, setQueryInput] = useState<{ sentence: string; sourceKind: string; version: number } | null>(null)
  const [readerImmersive, setReaderImmersive] = useState(false)
  const isImmersive = activeTab === 'reader' && readerImmersive

  // TV selection
  const [tvSelection, setTvSelection] = useState({ term: '', version: 0 })

  // Modals & Drawers
  const [activeWordModal, setActiveWordModal] = useState<{
    term: string
    hint?: {
      bookId?: string
      bookTitle?: string
      chapterId?: string
      chapterTitle?: string
      paraIndex?: number
      sentence?: string
    }
  } | null>(null)
  const [isStarredOpen, setIsStarredOpen] = useState<boolean>(false)
  const [isStatsOpen, setIsStatsOpen] = useState<boolean>(false)
  const [isSettingsOpen, setIsSettingsOpen] = useState<boolean>(false)

  // Load bookshelf on mount
  useEffect(() => {
    dbService.getAllBooks().then((loaded) => {
      setBooks(loaded)
      if (loaded.length > 0) {
        const first = loaded[0]
        setActiveBookId(first.id)
        setActiveChapterId(first.lastChapterId || first.chapters[0]?.id || 'ch-1')
      }
    })
  }, [])

  const openInInterpret = useCallback((sentence: string, sourceKind = '来自选词') => {
    const clean = sentence.trim()
    if (!clean) return
    setActiveWordModal(null)
    setQueryInput((current) => ({ sentence: clean, sourceKind, version: (current?.version || 0) + 1 }))
  }, [])

  useEffect(() => {
    const handleNativeIncoming = (rawJson: string) => {
      try {
        const parsed = JSON.parse(rawJson)
        if (typeof parsed?.text === 'string' && parsed.text.trim()) {
          openInInterpret(parsed.text, parsed.source || '来自系统选词')
        }
      } catch { return }
    }
    window.__onNativeIncomingText = handleNativeIncoming
    const pending = window.AndroidBridge?.consumeIncomingText?.()
    if (pending) handleNativeIncoming(pending)
    return () => { window.__onNativeIncomingText = undefined }
  }, [openInInterpret])

  const handleReadingProgress = useCallback(
    (bookId: string, chapterId: string, paraIndex: number, sentenceIndex = 0) => {
      setBooks((curr) =>
        curr.map((b) =>
          b.id === bookId && (b.lastChapterId !== chapterId || b.lastParaIndex !== paraIndex || b.lastSentenceIndex !== sentenceIndex)
            ? { ...b, lastChapterId: chapterId, lastParaIndex: paraIndex, lastSentenceIndex: sentenceIndex }
            : b
        )
      )
      void dbService.updateBookProgress(bookId, chapterId, paraIndex, sentenceIndex)
    },
    []
  )

  const handleSelectBookChapter = useCallback(
    (bookId: string, chapterId: string) => {
      setActiveBookId(bookId)
      setActiveChapterId(chapterId)
      const book = books.find((b) => b.id === bookId)
      if (book?.lastChapterId !== chapterId) handleReadingProgress(bookId, chapterId, 1, 0)
    },
    [books, handleReadingProgress]
  )

  const handleBooksChanged = useCallback(
    (updatedBooks: EbookItem[], selectedBookId?: string, selectedChapterId?: string) => {
      setBooks(updatedBooks)
      if (selectedBookId !== undefined) setActiveBookId(selectedBookId)
      if (selectedChapterId !== undefined) setActiveChapterId(selectedChapterId)
    },
    []
  )

  const handleJumpToChapter = useCallback(
    (bookId: string, chapterId: string, paraIndex: number, highlightTerm = '') => {
      setActiveBookId(bookId)
      setActiveChapterId(chapterId)
      setJumpState((curr) => ({
        paraIndex,
        highlightTerm,
        version: curr.version + 1,
      }))
      setActiveTab('reader')
      handleReadingProgress(bookId, chapterId, paraIndex, 0)
    },
    [handleReadingProgress]
  )

  const handleMatchTV = useCallback((term: string) => {
    setTvSelection((curr) => ({ term, version: curr.version + 1 }))
    setActiveWordModal(null)
    setActiveTab('tv')
    window.scrollTo({ top: 0, behavior: 'instant' })
  }, [])

  return (
    <div className="min-h-screen bg-[#f8f5ef] text-[#2e2a24] flex flex-col font-sans antialiased">
      {!isImmersive && (
        <Header
          onOpenStarred={() => setIsStarredOpen(true)}
          onOpenStats={() => setIsStatsOpen(true)}
          onOpenSettings={() => setIsSettingsOpen(true)}
          onSelectWord={(term) => setActiveWordModal({ term })}
        />
      )}

      <main className="flex-1 w-full max-w-6xl mx-auto">
        {activeTab === 'reader' && (
          <ReaderView
            books={books}
            activeBookId={activeBookId}
            activeChapterId={activeChapterId}
            jumpParaIndex={jumpState.paraIndex}
            highlightTerm={jumpState.highlightTerm}
            jumpVersion={jumpState.version}
            onSelectBookChapter={handleSelectBookChapter}
            onReadingProgress={handleReadingProgress}
            onBooksChanged={handleBooksChanged}
            onSelectWord={(term, hint) => setActiveWordModal({ term, hint })}
            onOpenInInterpret={openInInterpret}
            onOpenStarred={() => setIsStarredOpen(true)}
            onOpenSettings={() => setIsSettingsOpen(true)}
            onImmersiveChange={setReaderImmersive}
            onNavigate={setActiveTab}
          />
        )}

        {activeTab === 'interpret' && (
          <InterpretView
            books={books}
            activeBookId={activeBookId}
            activeChapterId={activeChapterId}
            initialSentence={interpretInput.sentence}
            initialSourceKind={interpretInput.sourceKind}
            incomingVersion={interpretInput.version}
            onSelectWord={(term, hint) => setActiveWordModal({ term, hint })}
            onJumpToChapter={handleJumpToChapter}
          />
        )}

        {activeTab === 'notebook' && (
          <NotebookView
            onJumpToChapter={handleJumpToChapter}
            onReopenInInterpret={openInInterpret}
            onSelectWord={(term) => setActiveWordModal({ term })}
            onMatchTV={handleMatchTV}
          />
        )}

        {activeTab === 'tv' && (
          <TVView
            key={tvSelection.version}
            initialTerm={tvSelection.term}
            onSelectWord={(term) => setActiveWordModal({ term })}
          />
        )}
      </main>

      {!isImmersive && <BottomNav activeTab={activeTab} onTabChange={setActiveTab} />}

      {queryInput && <SentenceQueryModal
        key={queryInput.version}
        sentence={queryInput.sentence}
        sourceKind={queryInput.sourceKind}
        books={books}
        activeBookId={activeBookId}
        activeChapterId={activeChapterId}
        onClose={() => setQueryInput(null)}
        onSelectWord={(term, hint) => setActiveWordModal({ term, hint })}
        onFollowUp={() => {
          setInterpretInput(queryInput)
          setQueryInput(null)
          setActiveTab('interpret')
        }}
      />}

      {activeWordModal && <WordModal
        key={JSON.stringify(activeWordModal)}
        term={activeWordModal?.term || null}
        books={books}
        activeBookId={activeBookId}
        contextHint={activeWordModal?.hint}
        onClose={() => setActiveWordModal(null)}
        onJumpToChapter={handleJumpToChapter}
        onMatchTV={handleMatchTV}
        onInterpretSentence={openInInterpret}
      />}

      <StarredDrawer
        isOpen={isStarredOpen}
        onClose={() => setIsStarredOpen(false)}
        onSelectWord={(term) => setActiveWordModal({ term })}
      />

      <StatsModal isOpen={isStatsOpen} onClose={() => setIsStatsOpen(false)} />

      <SettingsModal isOpen={isSettingsOpen} onClose={() => setIsSettingsOpen(false)} />
    </div>
  )
}

export default App
