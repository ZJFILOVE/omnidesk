import React, { useEffect, useState } from 'react'
import { Volume2 } from 'lucide-react'
import { audioService } from '../services/audio'

interface AudioButtonProps {
  term: string
  accent?: 'us' | 'uk'
  className?: string
  size?: 'sm' | 'md' | 'lg'
  showLabel?: boolean
  labelText?: string
}

export const AudioButton: React.FC<AudioButtonProps> = ({
  term,
  accent = 'us',
  className = '',
  size = 'md',
  showLabel = false,
  labelText,
}) => {
  const [isPlaying, setIsPlaying] = useState(false)

  useEffect(() => {
    const unsub = audioService.subscribe((playing, currentTerm) => {
      setIsPlaying(playing && currentTerm.toLowerCase() === term.toLowerCase())
    })
    return unsub
  }, [term])

  const handleClick = (e: React.MouseEvent) => {
    e.stopPropagation()
    audioService.playWord(term, accent)
  }

  const sizeClasses = {
    sm: 'px-2.5 py-1.5 text-xs',
    md: 'px-3 py-2 text-sm',
    lg: 'px-3.5 py-2.5 text-base',
  }

  const iconSizes = {
    sm: 14,
    md: 18,
    lg: 22,
  }

  return (
    <button
      type="button"
      onClick={handleClick}
      title={`语音朗读 (${accent.toUpperCase()})`}
      className={`inline-flex items-center gap-1.5 rounded-full font-medium transition-all active:scale-95 cursor-pointer ${
        isPlaying
          ? 'bg-amber-700 text-amber-50 shadow-sm animate-pulse'
          : 'bg-[#f1ece1] text-[#7a5228] hover:bg-[#e6dfd1] border border-[#ded6c6]'
      } ${sizeClasses[size]} ${className}`}
    >
      <Volume2 size={iconSizes[size]} className={isPlaying ? 'animate-bounce' : ''} />
      {(showLabel || labelText) && (
        <span className="text-xs font-medium tracking-normal">
          {labelText || (accent === 'uk' ? '英音' : '美音')}
        </span>
      )}
    </button>
  )
}
