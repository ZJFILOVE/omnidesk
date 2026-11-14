import React, { useEffect, useState } from 'react'
import {
  BookmarkCheck,
  BookOpen,
  Sparkles,
  Trash2,
  ArrowUpRight,
  Star,
  RotateCw,
  Film,
} from 'lucide-react'
import type { SentenceNote, UserWordRecord } from '../types/ebook'
import { dbService } from '../services/db'
import type { SM2Rating } from '../services/sm2'
import { AudioButton } from '../components/AudioButton'
import { MarkdownContent } from '../components/MarkdownContent'

interface NotebookViewProps {
  onJumpToChapter: (
    bookId: string,
    chapterId: string,
    paraIndex: number,
    highlightTerm?: string
  ) => void
  onReopenInInterpret: (sentence: string, sourceKind: string) => void
  onSelectWord: (term: string) => void
  onMatchTV: (term: string) => void
}

export const NotebookView: React.FC<NotebookViewProps> = ({
  onJumpToChapter,
  onReopenInInterpret,
  onSelectWord,
  onMatchTV,
}) => {
  const [subTab, setSubTab] = useState<'notes' | 'vocab' | 'flashcard'>('notes')
  const [notes, setNotes] = useState<SentenceNote[]>([])
  const [words, setWords] = useState<UserWordRecord[]>([])
  const [cardIndex, setCardIndex] = useState(0)
  const [flipped, setFlipped] = useState(false)

  const reloadAll = async () => {
    const [noteList, wordMap] = await Promise.all([
      dbService.getAllSentenceNotes(),
      dbService.getAllWordRecords(),
    ])
    setNotes(noteList)
    const sortedWords = Object.values(wordMap).sort((a, b) => b.lastReviewed - a.lastReviewed)
    setWords(sortedWords)
  }

  useEffect(() => {
    void reloadAll()
  }, [])

  const handleDeleteNote = async (id: string) => {
    await dbService.deleteSentenceNote(id)
    setNotes((prev) => prev.filter((n) => n.id !== id))
  }

  const handleDeleteWord = async (wordId: string) => {
    await dbService.deleteWordRecord(wordId)
    setWords((prev) => prev.filter((w) => w.wordId !== wordId))
  }

  const handleRateCard = async (word: UserWordRecord, rating: SM2Rating) => {
    await dbService.updateWordProgress(word.wordId, word.term, rating)
    setFlipped(false)
    await reloadAll()
    setCardIndex((prev) => (words.length > 1 ? (prev + 1) % words.length : 0))
  }

  const activeCard = words[cardIndex] || words[0]

  return (
    <div className="max-w-5xl mx-auto px-3.5 sm:px-6 py-4 pb-28 space-y-4">
      {/* Sub-navigation bar */}
      <div className="flex flex-wrap items-center justify-between gap-2 p-3 rounded-2xl bg-[#fdfbf7] border border-[#ded6c6]">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setSubTab('notes')}
            className={`px-3.5 py-2 rounded-xl text-xs sm:text-sm font-semibold inline-flex items-center gap-1.5 cursor-pointer transition-colors ${
              subTab === 'notes'
                ? 'bg-[#996021] text-[#fdfbf7]'
                : 'bg-[#f8f5ef] text-[#5c5347] hover:bg-[#f1ece1]'
            }`}
          >
            <BookmarkCheck size={15} />
            <span>句段解读笔记 ({notes.length})</span>
          </button>

          <button
            type="button"
            onClick={() => setSubTab('vocab')}
            className={`px-3.5 py-2 rounded-xl text-xs sm:text-sm font-semibold inline-flex items-center gap-1.5 cursor-pointer transition-colors ${
              subTab === 'vocab'
                ? 'bg-[#996021] text-[#fdfbf7]'
                : 'bg-[#f8f5ef] text-[#5c5347] hover:bg-[#f1ece1]'
            }`}
          >
            <Star size={15} />
            <span>书中生词本 ({words.length})</span>
          </button>

          {words.length > 0 && (
            <button
              type="button"
              onClick={() => {
                setCardIndex(0)
                setFlipped(false)
                setSubTab('flashcard')
              }}
              className={`px-3.5 py-2 rounded-xl text-xs sm:text-sm font-semibold inline-flex items-center gap-1.5 cursor-pointer transition-colors ${
                subTab === 'flashcard'
                  ? 'bg-[#325f50] text-[#fdfbf7]'
                  : 'bg-[#dce9d9] text-[#263c33] hover:brightness-95'
              }`}
            >
              <RotateCw size={14} />
              <span>语境闪卡复习</span>
            </button>
          )}
        </div>
      </div>

      {/* Sub-tab 1: Sentence Context Notes */}
      {subTab === 'notes' && (
        <div className="space-y-4">
          {notes.length === 0 ? (
            <div className="p-10 rounded-3xl bg-[#fdfbf7] border border-[#ded6c6] text-center space-y-2 text-[#72685b]">
              <Sparkles size={28} className="mx-auto text-[#996021]" />
              <p className="font-semibold text-[#2e2a24]">还没有句段解读笔记</p>
              <p className="text-xs">
                在阅读电子书时点选任意句子，或复制句子到「句段解读」让 6-luna 解读后，会自动保存在这里。
              </p>
            </div>
          ) : (
            notes.map((note) => (
              <article
                key={note.id}
                className="p-4 sm:p-6 rounded-3xl bg-[#fdfbf7] border border-[#ded6c6] shadow-xs space-y-3"
              >
                <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-[#72685b]">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="px-2.5 py-0.5 rounded-full bg-[#efe2cc] text-[#7a4b19] font-medium border border-[#dfc6a4]">
                      {note.sourceKind}
                    </span>
                    {note.context && (
                      <span className="font-semibold text-[#996021]">
                        《{note.context.bookTitle}》 · {note.context.chapterTitle} (第{' '}
                        {note.context.paraIndex} 段)
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-1.5">
                    <AudioButton term={note.sentence} size="sm" />
                    {note.context && (
                      <button
                        type="button"
                        onClick={() =>
                          onJumpToChapter(
                            note.context!.bookId,
                            note.context!.chapterId,
                            note.context!.paraIndex
                          )
                        }
                        className="inline-flex items-center gap-1 px-2.5 py-1 rounded-xl bg-[#f8f5ef] border border-[#ded6c6] text-[#5c5347] hover:bg-[#f1ece1] cursor-pointer"
                      >
                        <BookOpen size={12} />
                        <span>回原书段落</span>
                        <ArrowUpRight size={12} />
                      </button>
                    )}
                    <button
                      type="button"
                      onClick={() => onReopenInInterpret(note.sentence, note.sourceKind)}
                      className="px-2.5 py-1 rounded-xl bg-[#dce9d9] text-[#263c33] font-medium cursor-pointer"
                    >
                      继续追问
                    </button>
                    <button
                      type="button"
                      onClick={() => void handleDeleteNote(note.id)}
                      className="p-1.5 rounded-xl text-[#8a7f70] hover:text-[#94352b] cursor-pointer"
                      title="删除笔记"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                </div>

                <blockquote className="p-3.5 rounded-2xl bg-[#f8f5ef] border-l-4 border-[#996021] font-serif text-base sm:text-lg text-[#2e2a24] leading-relaxed">
                  {note.sentence}
                </blockquote>

                {note.followUpQuestion && (
                  <div className="text-xs text-[#325f50] bg-[#dce9d9]/60 px-3 py-1.5 rounded-xl font-medium">
                    追问：{note.followUpQuestion}
                  </div>
                )}

                <div className="p-4 rounded-2xl bg-[#f8f2e7] border border-[#dfc6a4]">
                  <MarkdownContent content={note.aiInterpretation} />
                </div>
              </article>
            ))
          )}
        </div>
      )}

      {/* Sub-tab 2: Book Vocabulary List */}
      {subTab === 'vocab' && (
        <div className="space-y-3">
          {words.length === 0 ? (
            <div className="p-10 rounded-3xl bg-[#fdfbf7] border border-[#ded6c6] text-center space-y-2 text-[#72685b]">
              <Star size={28} className="mx-auto text-[#996021]" />
              <p className="font-semibold text-[#2e2a24]">还没有收录书中生词</p>
              <p className="text-xs">
                在阅读电子书或解读句子时，点按任意英文单词即可查看词源并收入生词本。
              </p>
            </div>
          ) : (
            words.map((w) => (
              <div
                key={w.wordId}
                className="p-4 rounded-3xl bg-[#fdfbf7] border border-[#ded6c6] shadow-xs space-y-2.5"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2.5">
                    <button
                      type="button"
                      onClick={() => onSelectWord(w.term)}
                      className="text-xl font-bold font-serif text-[#2e2a24] hover:text-[#996021] cursor-pointer"
                    >
                      {w.term}
                    </button>
                    <AudioButton term={w.term} size="sm" />
                    {w.bookTitle && (
                      <span className="text-xs text-[#996021] bg-[#efe2cc] px-2.5 py-0.5 rounded-full">
                        《{w.bookTitle}》{w.chapterTitle ? ` · ${w.chapterTitle}` : ''}
                      </span>
                    )}
                  </div>

                  <div className="flex items-center gap-1.5">
                    <button
                      type="button"
                      onClick={() => onMatchTV(w.term)}
                      className="px-2.5 py-1 rounded-xl bg-[#f1ece1] text-xs text-[#5c5347] hover:bg-[#efe2cc] inline-flex items-center gap-1 cursor-pointer"
                    >
                      <Film size={12} />
                      <span>美剧台词</span>
                    </button>
                    {w.bookId && w.chapterId && (
                      <button
                        type="button"
                        onClick={() =>
                          onJumpToChapter(w.bookId!, w.chapterId!, w.paraIndex || 1, w.term)
                        }
                        className="px-2.5 py-1 rounded-xl bg-[#f8f5ef] border border-[#ded6c6] text-xs text-[#5c5347] inline-flex items-center gap-1 cursor-pointer"
                      >
                        <span>原书段落</span>
                        <ArrowUpRight size={12} />
                      </button>
                    )}
                    <button
                      type="button"
                      onClick={() => void handleDeleteWord(w.wordId)}
                      className="p-1.5 rounded-xl text-[#8a7f70] hover:text-[#94352b] cursor-pointer"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                </div>

                {w.contextSentence && (
                  <p className="p-3 rounded-2xl bg-[#f8f5ef] border border-[#e5decfe6] font-serif text-sm text-[#2e2a24]">
                    “{w.contextSentence}”
                  </p>
                )}

                {w.aiNote && (
                  <div className="p-3.5 rounded-2xl bg-[#f8f2e7] border border-[#dfc6a4]">
                    <MarkdownContent content={w.aiNote} />
                  </div>
                )}
              </div>
            ))
          )}
        </div>
      )}

      {/* Sub-tab 3: SM-2 Flashcard Review for Book Words */}
      {subTab === 'flashcard' && activeCard && (
        <div className="max-w-xl mx-auto p-6 rounded-3xl bg-[#fdfbf7] border border-[#ded6c6] shadow-sm space-y-5">
          <div className="flex items-center justify-between text-xs text-[#72685b]">
            <span>
              书中生词闪卡 ({cardIndex + 1} / {words.length})
            </span>
            {activeCard.bookTitle && (
              <span className="text-[#996021] font-medium">出自《{activeCard.bookTitle}》</span>
            )}
          </div>

          <div className="text-center py-4 space-y-3">
            <div className="flex items-center justify-center gap-2.5">
              <h3 className="text-3xl font-bold font-serif text-[#2e2a24]">{activeCard.term}</h3>
              <AudioButton term={activeCard.term} size="md" />
            </div>
            {activeCard.contextSentence && (
              <p className="p-4 rounded-2xl bg-[#f8f5ef] border border-[#e5decfe6] font-serif text-base text-[#2e2a24] leading-relaxed">
                “{activeCard.contextSentence}”
              </p>
            )}
          </div>

          {!flipped ? (
            <button
              type="button"
              onClick={() => setFlipped(true)}
              className="w-full py-3 rounded-2xl bg-[#efe2cc] border border-[#d6b483] text-[#7a4b19] font-semibold text-sm cursor-pointer"
            >
              翻开查看词源与语境解析
            </button>
          ) : (
            <div className="space-y-4 animate-in fade-in">
              {activeCard.aiNote ? (
                <div className="p-4 rounded-2xl bg-[#f8f2e7] border border-[#dfc6a4]">
                  <MarkdownContent content={activeCard.aiNote} />
                </div>
              ) : (
                <p className="text-xs text-center text-[#72685b]">
                  点击词名可让 6-luna 生成完整词源与语境笔记
                </p>
              )}

              <div className="grid grid-cols-4 gap-2 pt-2">
                {(
                  [
                    [1, '重来', 'bg-[#f8e5e2] text-[#94352b]'],
                    [2, '模糊', 'bg-[#f4e8d6] text-[#8c5820]'],
                    [3, '认识', 'bg-[#efe2cc] text-[#7a4b19]'],
                    [4, '熟练', 'bg-[#e7f0e8] text-[#35593d]'],
                  ] as const
                ).map(([rating, label, cls]) => (
                  <button
                    key={rating}
                    type="button"
                    onClick={() => void handleRateCard(activeCard, rating)}
                    className={`py-2.5 rounded-xl font-semibold text-xs cursor-pointer ${cls}`}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
