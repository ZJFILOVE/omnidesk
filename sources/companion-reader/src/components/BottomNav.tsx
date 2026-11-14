import React from 'react'
import { BookOpen, Sparkles, BookmarkCheck, Film } from 'lucide-react'

export type NavTab = 'reader' | 'interpret' | 'notebook' | 'tv'

interface BottomNavProps {
  activeTab: NavTab
  onTabChange: (tab: NavTab) => void
}

export const BottomNav: React.FC<BottomNavProps> = ({ activeTab, onTabChange }) => {
  const tabs: Array<{ id: NavTab; label: string; icon: React.FC<{ size?: number; className?: string }> }> = [
    { id: 'reader', label: '读电子书', icon: BookOpen },
    { id: 'interpret', label: '句段解读', icon: Sparkles },
    { id: 'notebook', label: '笔记生词', icon: BookmarkCheck },
    { id: 'tv', label: '美剧台词', icon: Film },
  ]

  return (
    <nav
      aria-label="主导航"
      style={{ paddingBottom: 'max(4px, env(safe-area-inset-bottom))' }}
      className="fixed bottom-0 left-0 right-0 z-40 bg-[#f8f5ef]/95 backdrop-blur-md border-t border-[#ded6c6] px-2 pt-1"
    >
      <div className="max-w-md mx-auto flex items-center justify-around">
        {tabs.map((tab) => {
          const Icon = tab.icon
          const isActive = activeTab === tab.id
          return (
            <button
              key={tab.id}
              type="button"
              aria-current={isActive ? 'page' : undefined}
              onClick={() => onTabChange(tab.id)}
              className={`min-h-10 flex-1 py-0.5 px-1 rounded-xl flex flex-col items-center justify-center gap-0.5 transition-all active:scale-95 cursor-pointer ${
                isActive
                  ? 'text-[#996021] font-semibold bg-[#efe2cc]/70'
                  : 'text-[#72685b] hover:text-[#2e2a24]'
              }`}
            >
              <Icon size={17} className={isActive ? 'stroke-[2.2]' : 'stroke-[1.8]'} />
              <span className="text-[10px] leading-tight tracking-tight">{tab.label}</span>
            </button>
          )
        })}
      </div>
    </nav>
  )
}
