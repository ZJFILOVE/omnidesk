import React, { useEffect, useState } from 'react'
import { X, Star, Trash2, Search } from 'lucide-react'
import type { UserWordRecord } from '../types/ebook'
import { dbService } from '../services/db'
import { AudioButton } from './AudioButton'

interface StarredDrawerProps {
  isOpen: boolean
  onClose: () => void
  onSelectWord: (term: string) => void
}

export const StarredDrawer: React.FC<StarredDrawerProps> = ({
  isOpen,
  onClose,
  onSelectWord,
}) => {
  const [starredWords, setStarredWords] = useState<UserWordRecord[]>([])
  const [filterQuery, setFilterQuery] = useState('')

  useEffect(() => {
    if (isOpen) {
      dbService.getAllWordRecords().then((map) => {
        const list = Object.values(map)
          .filter((w) => w.isStarred)
          .sort((a, b) => b.lastReviewed - a.lastReviewed)
        setStarredWords(list)
      })
    }
  }, [isOpen])

  if (!isOpen) return null

  const filtered = starredWords.filter(
    (w) =>
      w.term.toLowerCase().includes(filterQuery.toLowerCase()) ||
      (w.meaning || '').toLowerCase().includes(filterQuery.toLowerCase()) ||
      (w.contextSentence || '').toLowerCase().includes(filterQuery.toLowerCase())
  )

  return (
    <div
      onClick={onClose}
      className="fixed inset-0 z-50 flex justify-end bg-[#2e2a24]/40 backdrop-blur-xs animate-in fade-in"
    >
      <div
        className="w-full max-w-md h-full bg-[#fdfbf7] border-l border-[#ded6c6] flex flex-col shadow-xl text-[#2e2a24]"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="p-4 border-b border-[#e5decfe6] bg-[#f8f5ef] flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Star className="text-[#996021] fill-[#996021]" size={20} />
            <h2 className="font-semibold text-lg text-[#2e2a24]">书中生词本</h2>
            <span className="px-2.5 py-0.5 rounded-full bg-[#efe2cc] text-[#7a4b19] text-xs border border-[#dfc6a4]">
              {starredWords.length}
            </span>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="p-2 rounded-xl bg-[#f1ece1] text-[#72685b] hover:text-[#2e2a24]"
          >
            <X size={18} />
          </button>
        </div>

        <div className="p-3 border-b border-[#e5decfe6] bg-[#fdfbf7]">
          <div className="relative">
            <Search className="absolute left-3 top-2.5 text-[#8a7f70]" size={16} />
            <input
              type="text"
              placeholder="筛选生词或原句..."
              value={filterQuery}
              onChange={(e) => setFilterQuery(e.target.value)}
              className="w-full pl-9 pr-3 py-2 rounded-xl bg-[#f8f5ef] border border-[#ded6c6] text-sm text-[#2e2a24]"
            />
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-3 space-y-2">
          {filtered.length === 0 ? (
            <div className="py-12 text-center text-[#8a7f70] text-sm space-y-2">
              <Star size={32} className="mx-auto text-[#c8bda8] stroke-[1.5]" />
              <p>生词本还是空的</p>
              <p className="text-xs">在读电子书或解读句子时，点击任意单词即可收入生词本</p>
            </div>
          ) : (
            filtered.map((word) => (
              <div
                key={word.wordId}
                onClick={() => {
                  onSelectWord(word.term)
                  onClose()
                }}
                className="p-3.5 rounded-2xl bg-[#f8f5ef] hover:bg-[#f1ece1] border border-[#ded6c6] transition-all cursor-pointer flex items-center justify-between gap-2"
              >
                <div className="space-y-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-bold text-[#2e2a24] text-base font-serif">{word.term}</span>
                    {word.bookTitle && (
                      <span className="text-[11px] text-[#996021] truncate">《{word.bookTitle}》</span>
                    )}
                  </div>
                  {word.meaning && <div className="text-xs text-[#5c5347] line-clamp-1">{word.meaning}</div>}
                  {word.contextSentence && (
                    <div className="text-[11px] text-[#8a7f70] font-serif italic line-clamp-1">
                      “{word.contextSentence}”
                    </div>
                  )}
                </div>
                <div className="flex items-center gap-1 shrink-0">
                  <AudioButton term={word.term} size="sm" />
                  <button
                    type="button"
                    onClick={async (e) => {
                      e.stopPropagation()
                      await dbService.deleteWordRecord(word.wordId)
                      setStarredWords((prev) => prev.filter((w) => w.wordId !== word.wordId))
                    }}
                    className="p-1.5 rounded-xl text-[#8a7f70] hover:text-[#94352b]"
                  >
                    <Trash2 size={15} />
                  </button>
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  )
}
