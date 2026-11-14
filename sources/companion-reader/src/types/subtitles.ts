export interface SubtitleCue {
  start: number
  end: number
  text: string
}

export interface SubtitleHit {
  wordId: string
  cueIndex: number
  start: number
  end: number
  variant: boolean
}

export interface SubtitleEpisode {
  id: string
  show: string
  filename: string
  episode: string | null
  importedAt: number
  cues: SubtitleCue[]
  hits: SubtitleHit[]
  skippedCues: number
}

export interface SubtitleMatch {
  episode: SubtitleEpisode
  cue: SubtitleCue
  hit: SubtitleHit
}
