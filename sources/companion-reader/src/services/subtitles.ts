import type { SubtitleCue, SubtitleEpisode, SubtitleHit, SubtitleMatch } from '../types/subtitles'

export interface SubtitleWordSeed {
  id: string
  term: string
  pos?: string
  meaning?: string
}

export function phraseSearchUrl(term: string): string {
  return `https://www.playphrase.me/#/search?${new URLSearchParams({ q: term.trim(), language: 'en' })}`
}

export function showSearchUrl(term: string, show = ''): string {
  const query = show.trim() ? `"${show.trim()}" "${term.trim()}" transcript subtitles`
    : `"${term.trim()}" television series transcript subtitles`
  return `https://www.bing.com/search?${new URLSearchParams({ q: query })}`
}

export function sourceFromFilename(filename: string): string {
  const stem = filename.replace(/\.(srt|vtt)$/i, '')
  const title = stem.split(/(?:^|[^a-z0-9])(?:S\d{1,2}[ ._-]*E\d{1,3}(?:E\d{1,3})?|\d{1,2}x\d{1,3})(?=$|[^a-z0-9])/i)[0]
    .replace(/[._]+/g, ' ').replace(/\s+/g, ' ').replace(/[\s-]+$/g, '').trim()
  return (title || stem || filename).slice(0, 100)
}

export function formatSubtitleTime(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds))
  return [Math.floor(total / 3600), Math.floor(total / 60) % 60, total % 60]
    .map((part) => String(part).padStart(2, '0')).join(':')
}

export function episodeFromFilename(filename: string): string | null {
  const matched = filename.match(/(?:^|[^a-z0-9])S(\d{1,2})[ ._-]*E(\d{1,3})(?:E(\d{1,3}))?(?=$|[^a-z0-9])/i)
    ?? filename.match(/(?:^|[^a-z0-9])(\d{1,2})x(\d{1,3})(?=$|[^a-z0-9])/i)
  if (!matched) return null
  const base = `S${matched[1].padStart(2, '0')}E${matched[2].padStart(2, '0')}`
  return matched[3] ? `${base}E${matched[3].padStart(2, '0')}` : base
}

function timestamp(value: string): number {
  const matched = value.match(/^(?:(\d{1,3}):)?(\d{2}):(\d{2})[,.](\d{3})$/)
  if (!matched || Number(matched[2]) > 59 || Number(matched[3]) > 59) return NaN
  return Number(matched[1] ?? 0) * 3600 + Number(matched[2]) * 60
    + Number(matched[3]) + Number(matched[4]) / 1000
}

function cleanSubtitleText(value: string): string {
  const entities: Record<string, string> = {
    amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ',
  }
  return value.replace(/<[^>]*>/g, '').replace(/\{\\[^}]*\}/g, '')
    .replace(/&(#x[\da-f]+|#\d+|amp|lt|gt|quot|apos|nbsp);/gi, (whole, entity: string) => {
      if (!entity.startsWith('#')) return entities[entity.toLowerCase()] ?? whole
      const number = entity.toLowerCase().startsWith('#x')
        ? parseInt(entity.slice(2), 16) : Number(entity.slice(1))
      return number > 0 && number <= 0x10ffff && !(number >= 0xd800 && number <= 0xdfff)
        ? String.fromCodePoint(number) : ''
    }).replace(/[ \t]+/g, ' ').trim()
}

export function parseSubtitles(source: string): { cues: SubtitleCue[]; skippedCues: number } {
  if (source.length > 8 * 1024 * 1024) throw new Error('单个字幕文件请控制在 8 MB 以内。')
  const blocks = source.replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n').split(/\n[ \t]*\n/)
  const cues: SubtitleCue[] = []
  let skippedCues = 0
  for (const block of blocks) {
    if (/^\s*(?:WEBVTT|NOTE|STYLE|REGION)(?:\s|$)/.test(block)) continue
    const lines = block.trim().split('\n')
    const timingIndex = lines.findIndex((line) => line.includes('-->'))
    if (timingIndex < 0) {
      if (block.trim()) skippedCues++
      continue
    }
    const timing = lines[timingIndex].match(/^\s*(\S+)\s+-->\s+(\S+)/)
    const start = timestamp(timing?.[1] ?? '')
    const end = timestamp(timing?.[2] ?? '')
    const text = cleanSubtitleText(lines.slice(timingIndex + 1).join('\n'))
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start || !text) {
      skippedCues++
      continue
    }
    cues.push({ start, end, text })
  }
  if (!cues.length) throw new Error('没有找到有效字幕。请选择带时间轴的 UTF-8 SRT 或 VTT 文件。')
  if (cues.length > 30000) throw new Error('单个文件字幕条数过多，请按集拆分后导入。')
  cues.sort((first, second) => first.start - second.start || first.end - second.end)
  return { cues, skippedCues }
}

interface Token {
  value: string
  start: number
  end: number
}

function tokens(text: string): Token[] {
  const normalized = text.toLowerCase().replace(/[‘’]/g, "'")
  return Array.from(normalized.matchAll(/[a-z]+(?:'[a-z]+)?/g), (match) => ({
    value: match[0], start: match.index, end: match.index + match[0].length,
  }))
}

const IRREGULAR: Record<string, string[]> = {
  arise: ['arose', 'arisen'], become: ['became'], begin: ['began', 'begun'],
  bind: ['bound'], break: ['broke', 'broken'], bring: ['brought'], build: ['built'],
  catch: ['caught'], choose: ['chose', 'chosen'], draw: ['drew', 'drawn'],
  drive: ['drove', 'driven'], fall: ['fell', 'fallen'], feed: ['fed'],
  find: ['found'], give: ['gave', 'given'], grow: ['grew', 'grown'],
  hold: ['held'], keep: ['kept'], lead: ['led'], leave: ['left'],
  lose: ['lost'], make: ['made'], mean: ['meant'], meet: ['met'],
  occur: ['occurred', 'occurring'], prefer: ['preferred', 'preferring'],
  refer: ['referred', 'referring'], run: ['ran', 'running'], see: ['saw', 'seen'],
  seek: ['sought'], send: ['sent'], show: ['showed', 'shown'],
  take: ['took', 'taken'], teach: ['taught'], think: ['thought'],
  undergo: ['underwent', 'undergone'], write: ['wrote', 'written'],
  analysis: ['analyses'], basis: ['bases'], crisis: ['crises'],
  hypothesis: ['hypotheses'], phenomenon: ['phenomena'], criterion: ['criteria'],
  species: ['species'], child: ['children'], person: ['people'],
}

function wordForms(word: SubtitleWordSeed): string[][] {
  const base = tokens(word.term).map((token) => token.value)
  if (!base.length) return []
  const forms = [base]
  if (base.length !== 1 || base[0].length < 3) return forms
  const term = base[0]
  const pos = word.pos || 'n. v.'
  const noun = /(?:^|[\s/])n\./.test(pos)
  const verb = /(?:^|[\s/])v(?:t|i)?\./.test(pos)
  if (!noun && !verb) return forms
  const variants = new Set<string>(IRREGULAR[term] ?? [])
  if (noun || verb) {
    const plural = /[^aeiou]y$/.test(term) ? `${term.slice(0, -1)}ies`
      : /(?:s|x|z|ch|sh)$/.test(term) ? `${term}es` : `${term}s`
    if (!noun || !IRREGULAR[term]) variants.add(plural)
  }
  if (verb && !IRREGULAR[term]) {
    variants.add(/e$/.test(term) ? `${term}d`
      : /[^aeiou]y$/.test(term) ? `${term.slice(0, -1)}ied` : `${term}ed`)
    variants.add(/ie$/.test(term) ? `${term.slice(0, -2)}ying`
      : /[^e]e$/.test(term) ? `${term.slice(0, -1)}ing` : `${term}ing`)
  }
  if (noun) variants.add(`${term}'s`)
  for (const variant of variants) {
    if (variant !== term) forms.push([variant])
  }
  return forms
}

interface Candidate {
  wordId: string
  sequence: string[]
  variant: boolean
}

export function createSubtitleMatcher(words: SubtitleWordSeed[]) {
  const firstTokenIndex = new Map<string, Candidate[]>()
  for (const word of words) {
    wordForms(word).forEach((sequence, position) => {
      const bucket = firstTokenIndex.get(sequence[0]) ?? []
      bucket.push({ wordId: word.id, sequence, variant: position > 0 })
      firstTokenIndex.set(sequence[0], bucket)
    })
  }
  return (cues: SubtitleCue[]): SubtitleHit[] => {
    const hits: SubtitleHit[] = []
    cues.forEach((cue, cueIndex) => {
      const parts = tokens(cue.text)
      const cueHits = new Map<string, SubtitleHit>()
      parts.forEach((token, tokenIndex) => {
        for (const candidate of firstTokenIndex.get(token.value) ?? []) {
          const matched = candidate.sequence.every((value, offset) => parts[tokenIndex + offset]?.value === value)
          const existing = cueHits.get(candidate.wordId)
          if (!matched || (existing && (!existing.variant || candidate.variant))) continue
          const lastToken = parts[tokenIndex + candidate.sequence.length - 1]
          if (candidate.sequence.length > 1) {
            const span = cue.text.slice(token.start, lastToken.end)
            if (/[.!?;。！？]/.test(span)) continue
          }
          cueHits.set(candidate.wordId, { wordId: candidate.wordId, cueIndex, start: token.start, end: lastToken.end, variant: candidate.variant })
        }
      })
      hits.push(...cueHits.values())
    })
    return hits
  }
}

export function findSubtitleMatches(episodes: SubtitleEpisode[], wordId: string, show: string, includeVariants: boolean): SubtitleMatch[] {
  return episodes.filter((episode) => !show || episode.show === show).flatMap((episode) =>
    episode.hits.filter((hit) => hit.wordId === wordId && (includeVariants || !hit.variant))
      .map((hit) => ({ episode, hit, cue: episode.cues[hit.cueIndex] })),
  ).sort((first, second) => Number(first.hit.variant) - Number(second.hit.variant)
    || first.episode.show.localeCompare(second.episode.show)
    || (first.episode.episode ?? first.episode.filename).localeCompare(second.episode.episode ?? second.episode.filename)
    || first.cue.start - second.cue.start)
}

export function findSubtitleMatchesByTerm(
  episodes: SubtitleEpisode[],
  term: string,
  wordItem: SubtitleWordSeed | undefined,
  show: string,
  includeVariants: boolean
): SubtitleMatch[] {
  if (wordItem) {
    const indexed = findSubtitleMatches(episodes, wordItem.id, show, includeVariants)
    if (indexed.length > 0) return indexed
  }
  const clean = term.trim()
  if (!clean) return []
  const matcher = createSubtitleMatcher([
    wordItem || {
      id: `__custom__:${clean.toLowerCase()}`,
      term: clean,
      pos: 'n. v.',
      meaning: '',
    },
  ])
  return episodes
    .filter((episode) => !show || episode.show === show)
    .flatMap((episode) =>
      matcher(episode.cues)
        .filter((hit) => includeVariants || !hit.variant)
        .map((hit) => ({ episode, hit, cue: episode.cues[hit.cueIndex] }))
    )
}
