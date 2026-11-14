import React, { useEffect, useState } from 'react'
import { BookOpen, Search, Star, BarChart3, Settings, Sparkles } from 'lucide-react'
import { getConnectionConfig } from '../services/ai'

interface HeaderProps {
  onOpenStarred: () => void
  onOpenStats: () => void
  onOpenSettings: () => void
  onSelectWord: (term: string) => void
}

export const Header: React.FC<HeaderProps> = ({
  onOpenStarred,
  onOpenStats,
  onOpenSettings,
  onSelectWord,
}) => {
  const [query, setQuery] = useState('')
  const [showMobileSearch, setShowMobileSearch] = useState(false)
  const [aiConfigured, setAiConfigured] = useState(false)

  useEffect(() => {
    setAiConfigured(getConnectionConfig().configured)
    const timer = window.setInterval(() => {
      setAiConfigured(getConnectionConfig().configured)
    }, 2500)
    return () => window.clearInterval(timer)
  }, [])

  const submitSearch = (e: React.FormEvent) => {
    e.preventDefault()
    const clean = query.trim()
    if (!clean) return
    onSelectWord(clean)
    setQuery('')
    setShowMobileSearch(false)
  }

  return (
    <header
      style={{ paddingTop: 'max(10px, env(safe-area-inset-top))' }}
      className="sticky top-0 z-40 bg-[#f8f5ef]/95 backdrop-blur-md border-b border-[#e2d9c8] px-3.5 sm:px-6 pb-2.5"
    >
      <div className="max-w-6xl mx-auto flex items-center justify-between gap-2.5">
        <div className="flex items-center gap-2.5 shrink-0">
          <div className="w-9 h-9 rounded-2xl bg-[#efe3ce] border border-[#dec9a8] flex items-center justify-center text-[#996021]">
            <BookOpen size={19} />
          </div>
          <div className="min-w-0">
            <h1 className="text-sm sm:text-lg font-semibold text-[#2e2a24] tracking-tight leading-none flex items-center gap-2 whitespace-nowrap">
              <span>伴读 · 书与语境</span>
            </h1>
            <p className="hidden sm:block text-[11px] text-[#72685b] leading-tight mt-1">
              阅读 · 查词 · 语境解读
            </p>
          </div>
        </div>

        <form onSubmit={submitSearch} className="relative flex-1 max-w-sm hidden sm:block">
          <Search className="absolute left-3 top-2.5 text-[#8a7f70]" size={15} />
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="输入英文单词，在已导入书中查原句与词源..."
            className="w-full pl-9 pr-3 py-1.5 text-xs rounded-xl bg-[#f1ece1] border border-[#ded6c6] text-[#2e2a24] placeholder-[#8a7f70] focus:outline-hidden focus:border-[#996021] focus:bg-[#fdfbf7]"
          />
        </form>

        <div className="flex items-center gap-1 shrink-0 whitespace-nowrap">
          <button
            type="button"
            onClick={onOpenSettings}
            className="inline-flex items-center gap-1 px-2 py-1 rounded-full border border-[#ded6c6] bg-[#fdfbf7] text-[11px] text-[#5c5347] hover:bg-[#f1ece1] transition-colors cursor-pointer whitespace-nowrap shrink-0"
            title="飞牛 6-luna 连接状态"
          >
            <span className={`w-2 h-2 rounded-full ${aiConfigured ? 'bg-[#496247]' : 'bg-[#b59c7b]'}`} />
            <Sparkles size={12} className="text-[#996021]" />
            <span className="hidden md:inline">{aiConfigured ? '6-luna 已连' : '连接 6-luna'}</span>
          </button>

          <button
            type="button"
            onClick={() => setShowMobileSearch((prev) => !prev)}
            className="sm:hidden p-2 rounded-xl text-[#72685b] hover:text-[#2e2a24] hover:bg-[#f1ece1] transition-colors"
            title="查书中词"
          >
            <Search size={18} />
          </button>
          <button
            type="button"
            onClick={onOpenStarred}
            className="p-2 rounded-xl text-[#72685b] hover:text-[#996021] hover:bg-[#f1ece1] transition-colors"
            title="收藏的生词"
          >
            <Star size={18} />
          </button>
          <button
            type="button"
            onClick={onOpenStats}
            className="p-2 rounded-xl text-[#72685b] hover:text-[#996021] hover:bg-[#f1ece1] transition-colors"
            title="阅读概览"
          >
            <BarChart3 size={18} />
          </button>
          <button
            type="button"
            onClick={onOpenSettings}
            className="p-2 rounded-xl text-[#72685b] hover:text-[#2e2a24] hover:bg-[#f1ece1] transition-colors"
            title="连接与设置"
          >
            <Settings size={18} />
          </button>
        </div>
      </div>

      {showMobileSearch && (
        <form onSubmit={submitSearch} className="sm:hidden mt-2.5 flex gap-2">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-2.5 text-[#8a7f70]" size={15} />
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="输入英文单词，查书中例句与词源..."
              className="w-full pl-9 pr-3 py-2 text-sm rounded-xl bg-[#f1ece1] border border-[#ded6c6] text-[#2e2a24] placeholder-[#8a7f70] focus:outline-hidden focus:border-[#996021] focus:bg-[#fdfbf7]"
            />
          </div>
          <button
            type="submit"
            className="px-3.5 py-2 rounded-xl bg-[#996021] text-[#fdfbf7] text-xs font-medium"
          >
            查词
          </button>
        </form>
      )}
    </header>
  )
}
