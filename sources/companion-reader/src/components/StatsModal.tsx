import React, { useEffect, useState } from 'react'
import { X, BarChart3, BookOpen, Sparkles, Star } from 'lucide-react'
import type { EbookItem, SentenceNote, UserWordRecord } from '../types/ebook'
import { dbService } from '../services/db'

interface StatsModalProps {
  isOpen: boolean
  onClose: () => void
}

export const StatsModal: React.FC<StatsModalProps> = ({ isOpen, onClose }) => {
  const [books, setBooks] = useState<EbookItem[]>([])
  const [notes, setNotes] = useState<SentenceNote[]>([])
  const [words, setWords] = useState<UserWordRecord[]>([])

  useEffect(() => {
    if (isOpen) {
      dbService.getAllBooks().then(setBooks)
      dbService.getAllSentenceNotes().then(setNotes)
      dbService.getAllWordRecords().then((m) => setWords(Object.values(m)))
    }
  }, [isOpen])

  if (!isOpen) return null

  const totalChapters = books.reduce((a, b) => a + b.chapters.length, 0)
  const totalBookWords = books.reduce((a, b) => a + b.totalWords, 0)

  return (
    <div
      onClick={onClose}
      className="fixed inset-0 z-50 flex items-center justify-center p-3 sm:p-4 bg-[#2e2a24]/45 backdrop-blur-xs animate-in fade-in"
    >
      <div
        className="w-full max-w-xl bg-[#fdfbf7] border border-[#ded6c6] rounded-3xl shadow-xl overflow-hidden flex flex-col text-[#2e2a24]"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="p-4 sm:p-5 border-b border-[#e5decfe6] bg-[#f8f5ef] flex items-center justify-between">
          <div className="flex items-center gap-2">
            <BarChart3 className="text-[#996021]" size={20} />
            <h2 className="text-lg sm:text-xl font-semibold text-[#2e2a24]">我的阅读与伴读足迹</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="p-2 rounded-xl bg-[#f1ece1] text-[#72685b] hover:text-[#2e2a24]"
          >
            <X size={18} />
          </button>
        </div>

        <div className="p-4 sm:p-6 space-y-5 overflow-y-auto max-h-[75vh] text-sm">
          <div className="grid grid-cols-3 gap-3">
            <div className="p-3.5 rounded-2xl bg-[#f8f5ef] border border-[#ded6c6] flex flex-col">
              <span className="text-xs text-[#72685b] flex items-center gap-1">
                <BookOpen size={13} className="text-[#996021]" />
                已导电子书
              </span>
              <span className="text-2xl font-bold text-[#2e2a24] mt-1">{books.length}</span>
              <span className="text-[11px] text-[#8a7f70] mt-0.5">{totalChapters} 章 · {(totalBookWords / 1000).toFixed(1)}k 词</span>
            </div>

            <div className="p-3.5 rounded-2xl bg-[#f4e8d6] border border-[#dfc6a4] flex flex-col">
              <span className="text-xs text-[#7a4b19] flex items-center gap-1">
                <Sparkles size={13} />
                句段解读笔记
              </span>
              <span className="text-2xl font-bold text-[#7a4b19] mt-1">{notes.length}</span>
              <span className="text-[11px] text-[#8c5820] mt-0.5">结合上下文精读</span>
            </div>

            <div className="p-3.5 rounded-2xl bg-[#e7f0e8] border border-[#c5dbc8] flex flex-col">
              <span className="text-xs text-[#35593d] flex items-center gap-1">
                <Star size={13} />
                书中生词本
              </span>
              <span className="text-2xl font-bold text-[#35593d] mt-1">{words.length}</span>
              <span className="text-[11px] text-[#496247] mt-0.5">来自原书真实语境</span>
            </div>
          </div>

          <div className="space-y-2.5">
            <h3 className="text-xs font-semibold text-[#72685b]">当前书架概览</h3>
            {books.map((b) => (
              <div
                key={b.id}
                className="p-3.5 rounded-2xl bg-[#f8f5ef] border border-[#ded6c6] flex items-center justify-between"
              >
                <div>
                  <div className="font-semibold text-[#2e2a24]">{b.title}</div>
                  <div className="text-xs text-[#72685b]">
                    {b.author} · {b.chapters.length} 篇/章 · {b.totalWords.toLocaleString()} 词
                  </div>
                </div>
                <span className="px-2.5 py-1 rounded-full bg-[#efe2cc] text-[#7a4b19] text-xs font-medium uppercase">
                  {b.format || 'epub'}
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
