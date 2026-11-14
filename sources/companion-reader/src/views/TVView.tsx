import { useEffect, useMemo, useRef, useState } from 'react'
import { BookOpen, ExternalLink, FileUp, Film, RotateCw, Search, Sparkles, Trash2 } from 'lucide-react'
import type { SubtitleEpisode, SubtitleMatch } from '../types/subtitles'
import { dbService } from '../services/db'
import { AudioButton } from '../components/AudioButton'
import { MarkdownContent } from '../components/MarkdownContent'
import {
  createSubtitleMatcher,
  episodeFromFilename,
  findSubtitleMatchesByTerm,
  formatSubtitleTime,
  parseSubtitles,
  phraseSearchUrl,
  showSearchUrl,
  sourceFromFilename,
  type SubtitleWordSeed,
} from '../services/subtitles'
import {
  loadSubtitleLibrary,
  removeSubtitleEpisode,
  saveSubtitleEpisode,
  subtitleIdentity,
} from '../services/subtitleLibrary'
import {
  explainSubtitleUsage,
  explainWordEtymology,
  getCachedEtymology,
  getCachedSubtitleExplanation,
  loadCachedEtymologyAsync,
  loadCachedSubtitleExplanationAsync,
  searchOnlineSubtitles,
  type OnlineSubtitleQuote,
} from '../services/ai'

const control =
  'min-h-11 rounded-2xl border border-[#ded6c6] bg-[#f8f5ef] px-3.5 py-2 text-sm text-[#2e2a24] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#996021] disabled:opacity-50'
const panel = 'rounded-3xl border border-[#ded6c6] bg-[#fdfbf7] p-4 sm:p-6 shadow-xs'

interface QuoteCardProps {
  quote: OnlineSubtitleQuote
  targetTerm: string
  wordInfo?: SubtitleWordSeed
  accent: 'us' | 'uk'
}

function QuoteMatchCard({ quote, targetTerm, wordInfo, accent }: QuoteCardProps) {
  const [aiExplanation, setAiExplanation] = useState<string>(
    () => getCachedSubtitleExplanation(quote.id, targetTerm) || ''
  )
  const [aiBusy, setAiBusy] = useState(false)
  const [aiErr, setAiErr] = useState('')

  useEffect(() => {
    const syncVal = getCachedSubtitleExplanation(quote.id, targetTerm) || ''
    setAiExplanation(syncVal)
    if (!syncVal) {
      loadCachedSubtitleExplanationAsync(quote.id, targetTerm).then((val) => {
        if (val) setAiExplanation(val)
      })
    }
    setAiErr('')
    setAiBusy(false)
  }, [quote.id, targetTerm])

  const handleExplainUsage = async (force = false) => {
    if (aiBusy) return
    setAiBusy(true)
    setAiErr('')
    try {
      const res = await explainSubtitleUsage(
        {
          matchId: quote.id,
          term: targetTerm,
          matchedForm: quote.matchedForm || targetTerm,
          show: quote.show,
          episode: quote.episode,
          year: quote.year,
          text: quote.text,
          previous: quote.previous,
          next: quote.next,
          wordInfo,
        },
        force
      )
      setAiExplanation(res)
    } catch (err) {
      setAiErr(err instanceof Error ? err.message : '调用飞牛 6-luna 失败')
    } finally {
      setAiBusy(false)
    }
  }

  const safeStart = Math.max(0, Math.min(quote.matchStart, quote.text.length))
  const safeEnd = Math.max(safeStart, Math.min(quote.matchEnd, quote.text.length))

  return (
    <article className={`${panel} space-y-3`}>
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs sm:text-sm">
        <div className="flex items-center gap-2">
          <span className="px-2.5 py-0.5 rounded-full bg-[#efe2cc] text-[#7a4b19] font-medium text-xs border border-[#dfc6a4]">
            {quote.source === 'online' ? '影视台词库' : '本机字幕'}
          </span>
          <h3 className="font-semibold text-base text-[#2e2a24]">《{quote.show}》</h3>
        </div>
        <div className="flex items-center gap-2">
          <span className="rounded-xl bg-[#f1ece1] px-2.5 py-1 text-xs text-[#72685b] border border-[#ded6c6]">
            {quote.episode ? `${quote.episode} · ` : ''}
            {formatSubtitleTime(quote.start)}
          </span>
          <AudioButton term={quote.text} accent={accent} size="sm" labelText="听台词" />
        </div>
      </div>

      <div className="rounded-2xl bg-[#f8f5ef] border border-[#e5decfe6] p-3.5 sm:p-4 space-y-2">
        {quote.previous && quote.previous.length > 0 && (
          <div className="space-y-1 text-xs sm:text-sm text-[#72685b] border-l-2 border-[#d5cbb8] pl-2.5">
            {quote.previous.map((line, idx) => (
              <p key={idx} className="leading-relaxed">
                {line}
              </p>
            ))}
          </div>
        )}

        <p className="whitespace-pre-line font-serif text-base sm:text-lg leading-relaxed text-[#2e2a24] font-medium">
          {quote.text.slice(0, safeStart)}
          <mark className="rounded-md bg-[#f2d6a2] px-1.5 py-0.5 text-[#59340e] font-bold">
            {quote.text.slice(safeStart, safeEnd) || quote.matchedForm}
          </mark>
          {quote.text.slice(safeEnd)}
        </p>

        {quote.next && quote.next.length > 0 && (
          <div className="space-y-1 text-xs sm:text-sm text-[#72685b] border-l-2 border-[#d5cbb8] pl-2.5">
            {quote.next.map((line, idx) => (
              <p key={idx} className="leading-relaxed">
                {line}
              </p>
            ))}
          </div>
        )}
      </div>

      <div className="pt-1">
        <button
          type="button"
          disabled={aiBusy}
          onClick={() => void handleExplainUsage(false)}
          className="w-full min-h-11 rounded-2xl bg-[#efe2cc] hover:bg-[#e7d5b7] border border-[#d6b483] px-4 py-2.5 text-xs sm:text-sm font-semibold text-[#7a4b19] flex items-center justify-center gap-2 transition-colors disabled:opacity-60 cursor-pointer"
        >
          <Sparkles size={16} className="text-[#996021] shrink-0" />
          <span>
            {aiBusy
              ? `6-luna 正在结合《${quote.show}》剧情分析...`
              : aiExplanation
              ? `已生成《${quote.show}》剧情与语境用法解读`
              : `✨ 让 6-luna 结合《${quote.show}》剧情与上下文讲用法`}
          </span>
        </button>

        {(aiExplanation || aiErr) && (
          <div className="mt-3 p-4 rounded-2xl bg-[#f8f2e7] border border-[#dfc6a4] space-y-2 animate-in fade-in">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-[#996021] flex items-center gap-1.5">
                <Sparkles size={14} />
                <span>飞牛 6-luna · 《{quote.show}》剧情与台词用法解析</span>
              </span>
              {aiExplanation && (
                <button
                  type="button"
                  disabled={aiBusy}
                  onClick={() => void handleExplainUsage(true)}
                  className="inline-flex items-center gap-1 text-xs text-[#8c5820] hover:underline cursor-pointer"
                >
                  <RotateCw size={12} className={aiBusy ? 'animate-spin' : ''} />
                  重新解读
                </button>
              )}
            </div>
            {aiErr ? (
              <p className="text-xs text-[#94352b]">{aiErr}</p>
            ) : (
              <MarkdownContent content={aiExplanation} />
            )}
          </div>
        )}
      </div>
    </article>
  )
}

function localMatchToQuote(match: SubtitleMatch): OnlineSubtitleQuote {
  const { episode, cue, hit } = match
  const prev: string[] = []
  if (episode.cues[hit.cueIndex - 2]) prev.push(episode.cues[hit.cueIndex - 2].text)
  if (episode.cues[hit.cueIndex - 1]) prev.push(episode.cues[hit.cueIndex - 1].text)
  const next: string[] = []
  if (episode.cues[hit.cueIndex + 1]) next.push(episode.cues[hit.cueIndex + 1].text)
  if (episode.cues[hit.cueIndex + 2]) next.push(episode.cues[hit.cueIndex + 2].text)
  return {
    id: `local:${episode.id}:${hit.cueIndex}`,
    source: 'local',
    show: episode.show,
    episode: episode.episode ?? '本机字幕',
    start: cue.start,
    end: cue.end,
    text: cue.text,
    matchedForm: cue.text.slice(hit.start, hit.end),
    matchStart: hit.start,
    matchEnd: hit.end,
    previous: prev,
    next,
    variant: hit.variant,
  }
}

interface TVViewProps {
  initialTerm?: string
  onSelectWord: (term: string) => void
}

export function TVView({ initialTerm = '', onSelectWord }: TVViewProps) {
  const [words, setWords] = useState<SubtitleWordSeed[]>([])
  const [episodes, setEpisodes] = useState<SubtitleEpisode[]>([])
  const [query, setQuery] = useState(initialTerm || 'sanctuary')
  const [selectedTerm, setSelectedTerm] = useState(initialTerm || 'sanctuary')
  const [show] = useState('')
  const [includeVariants] = useState(true)
  const [importSource, setImportSource] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [reports, setReports] = useState<string[]>([])
  const [accent, setAccent] = useState<'us' | 'uk'>('us')
  const [pendingRemoval, setPendingRemoval] = useState<string | null>(null)

  const [onlineQuotes, setOnlineQuotes] = useState<OnlineSubtitleQuote[]>([])
  const [onlineSearching, setOnlineSearching] = useState(false)
  const [onlineError, setOnlineError] = useState('')

  const [wordEtymology, setWordEtymology] = useState('')
  const [etymologyLoading, setEtymologyLoading] = useState(false)
  const [etymologyError, setEtymologyError] = useState('')

  const fileInput = useRef<HTMLInputElement>(null)

  useEffect(() => {
    let active = true
    Promise.allSettled([
      dbService.getAllWordRecords(),
      loadSubtitleLibrary(),
      dbService.getSettings(),
    ]).then(([wordMapRes, libraryRes, settingsRes]) => {
      if (!active) return
      if (wordMapRes.status === 'fulfilled') {
        const seeds: SubtitleWordSeed[] = Object.values(wordMapRes.value).map((w) => ({
          id: w.wordId,
          term: w.term,
          pos: w.pos || 'n. v.',
          meaning: w.meaning || '',
        }))
        setWords(seeds)
      }
      if (libraryRes.status === 'fulfilled') setEpisodes(libraryRes.value)
      if (settingsRes.status === 'fulfilled') setAccent(settingsRes.value.voiceAccent)
    })
    return () => {
      active = false
    }
  }, [])

  useEffect(() => {
    if (initialTerm.trim()) {
      setQuery(initialTerm.trim())
      setSelectedTerm(initialTerm.trim())
    }
  }, [initialTerm])

  const selectedWord = useMemo(() => {
    const clean = selectedTerm.trim().toLowerCase()
    return words.find((w) => w.term.toLowerCase() === clean)
  }, [words, selectedTerm])

  useEffect(() => {
    const clean = selectedTerm.trim()
    if (!clean) {
      setOnlineQuotes([])
      return
    }
    const syncEty = getCachedEtymology(clean) || ''
    setWordEtymology(syncEty)
    if (!syncEty) {
      loadCachedEtymologyAsync(clean).then((val) => {
        if (val) setWordEtymology(val)
      })
    }
    setEtymologyError('')
    setOnlineSearching(true)
    setOnlineError('')
    searchOnlineSubtitles(clean, 10, false)
      .then(setOnlineQuotes)
      .catch((err) => setOnlineError(err instanceof Error ? err.message : '检索台词失败'))
      .finally(() => setOnlineSearching(false))
  }, [selectedTerm])

  const localMatches = useMemo(() => {
    if (!selectedTerm.trim()) return []
    return findSubtitleMatchesByTerm(episodes, selectedTerm, selectedWord, show, includeVariants)
  }, [episodes, selectedTerm, selectedWord, show, includeVariants])

  const handleAskEtymology = async (force = false) => {
    if (!selectedTerm.trim() || etymologyLoading) return
    setEtymologyLoading(true)
    setEtymologyError('')
    try {
      const res = await explainWordEtymology(
        {
          term: selectedTerm.trim(),
          meaning: selectedWord?.meaning,
        },
        force
      )
      setWordEtymology(res)
    } catch (err) {
      setEtymologyError(err instanceof Error ? err.message : '拆解词源失败')
    } finally {
      setEtymologyLoading(false)
    }
  }

  const importFiles = async (files: File[]) => {
    if (!files.length || busy) return
    setBusy(true)
    setError('')
    setReports([])
    const matcher = createSubtitleMatcher(
      words.length > 0 ? words : [{ id: 'sanctuary', term: 'sanctuary', pos: 'n. v.' }]
    )
    const nextReports: string[] = []
    try {
      for (const file of files) {
        const content = await file.text()
        const { cues, skippedCues } = parseSubtitles(content)
        const showTitle = importSource.trim() || sourceFromFilename(file.name)
        const id = await subtitleIdentity(showTitle, content)
        const episode: SubtitleEpisode = {
          id,
          show: showTitle,
          filename: file.name,
          episode: episodeFromFilename(file.name),
          importedAt: Date.now(),
          cues,
          hits: matcher(cues),
          skippedCues,
        }
        await saveSubtitleEpisode(episode)
        nextReports.push(`已导入 ${file.name}（${cues.length} 条字幕）`)
      }
      setEpisodes(await loadSubtitleLibrary())
      setReports(nextReports)
    } catch (err) {
      setError(err instanceof Error ? err.message : '字幕导入失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="max-w-5xl mx-auto px-3.5 sm:px-6 py-4 pb-28 space-y-5">
      <div className="flex items-start gap-3">
        <div className="w-10 h-10 rounded-2xl bg-[#efe2cc] border border-[#dfc6a4] flex items-center justify-center text-[#996021] shrink-0 mt-0.5">
          <Film size={20} />
        </div>
        <div>
          <h2 className="text-xl sm:text-2xl font-semibold text-[#2e2a24]">
            在美剧与电影台词里，听见鲜活语境
          </h2>
          <p className="mt-1 text-xs sm:text-sm leading-relaxed text-[#72685b]">
            读电子书遇到某个词或短语时，输入即可检索真实美剧对白，让飞牛 6-luna 结合剧情和上下文讲透口语用法。
          </p>
        </div>
      </div>

      {error && (
        <p className="rounded-2xl border border-[#e4bfb8] bg-[#f8e5e2] p-3.5 text-sm text-[#94352b]">
          {error}
        </p>
      )}

      <section className={panel}>
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            if (query.trim()) setSelectedTerm(query.trim())
          }}
        >
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="输入在书中遇到的单词，例如 sanctuary、resonance、prodigy..."
            className={`${control} min-w-0 flex-1 text-base`}
          />
          <button
            type="submit"
            className="min-h-11 rounded-2xl bg-[#996021] px-4 py-2 text-sm font-medium text-[#fdfbf7] flex shrink-0 items-center gap-1.5 hover:brightness-95 cursor-pointer"
          >
            <Search size={17} />
            找台词
          </button>
        </form>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <span className="text-xs text-[#8a7f70]">示例词汇：</span>
          {['sanctuary', 'resonance', 'prodigy', 'inevitable', 'catapult', 'estrange'].map(
            (term) => (
              <button
                key={term}
                type="button"
                onClick={() => {
                  setQuery(term)
                  setSelectedTerm(term)
                }}
                className={`px-3 py-1 rounded-full text-xs border transition-colors cursor-pointer ${
                  selectedTerm.toLowerCase() === term
                    ? 'bg-[#efe2cc] border-[#c89d66] text-[#7a4b19] font-semibold'
                    : 'bg-[#f8f5ef] border-[#ded6c6] text-[#5c5347] hover:bg-[#f1ece1]'
                }`}
              >
                {term}
              </button>
            )
          )}
        </div>

        {selectedTerm && (
          <div className="mt-4 border-t border-[#e5decfe6] pt-4 space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex flex-wrap items-center gap-2.5">
                <h3 className="font-serif text-2xl sm:text-3xl font-bold text-[#2e2a24]">
                  {selectedTerm}
                </h3>
                <AudioButton term={selectedTerm} accent={accent} size="md" showLabel />
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  disabled={etymologyLoading}
                  onClick={() => void handleAskEtymology(false)}
                  className="inline-flex items-center gap-1.5 rounded-2xl bg-[#efe2cc] hover:bg-[#e6d4b6] border border-[#d6b483] px-3.5 py-2 text-xs font-semibold text-[#7a4b19] cursor-pointer"
                >
                  <Sparkles size={14} className="text-[#996021]" />
                  <span>{etymologyLoading ? '拆解中...' : '✨ AI 讲词源与词根词缀'}</span>
                </button>

                <button
                  type="button"
                  onClick={() => onSelectWord(selectedTerm)}
                  className="inline-flex items-center gap-1.5 rounded-2xl border border-[#ded6c6] bg-[#f8f5ef] hover:bg-[#f1ece1] px-3.5 py-2 text-xs font-medium text-[#5c5347] cursor-pointer"
                >
                  <BookOpen size={14} />
                  查看书中原句与词卡
                </button>
              </div>
            </div>

            {(wordEtymology || etymologyError) && (
              <div className="p-4 rounded-2xl bg-[#f8f2e7] border border-[#dfc6a4] space-y-2">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-semibold text-[#996021] flex items-center gap-1.5">
                    <Sparkles size={14} />
                    <span>飞牛 6-luna · 「{selectedTerm}」词源与词根词缀</span>
                  </span>
                  {wordEtymology && (
                    <button
                      type="button"
                      disabled={etymologyLoading}
                      onClick={() => void handleAskEtymology(true)}
                      className="inline-flex items-center gap-1 text-xs text-[#8c5820] hover:underline cursor-pointer"
                    >
                      <RotateCw size={12} className={etymologyLoading ? 'animate-spin' : ''} />
                      重新生成
                    </button>
                  )}
                </div>
                {etymologyError ? (
                  <p className="text-xs text-[#94352b]">{etymologyError}</p>
                ) : (
                  <MarkdownContent content={wordEtymology} />
                )}
              </div>
            )}
          </div>
        )}
      </section>

      <section className="space-y-3">
        <div className="flex items-center justify-between px-1">
          <h3 className="text-base sm:text-lg font-semibold text-[#2e2a24]">
            匹配的影视台词与上下文{' '}
            {!onlineSearching && `(${onlineQuotes.length + localMatches.length} 条)`}
          </h3>
          {selectedTerm && (
            <button
              type="button"
              disabled={onlineSearching}
              onClick={() => {
                setOnlineSearching(true)
                setOnlineError('')
                searchOnlineSubtitles(selectedTerm, 10, true)
                  .then(setOnlineQuotes)
                  .catch((err) =>
                    setOnlineError(err instanceof Error ? err.message : '刷新台词失败')
                  )
                  .finally(() => setOnlineSearching(false))
              }}
              className="inline-flex items-center gap-1 text-xs text-[#996021] hover:underline cursor-pointer"
            >
              <RotateCw size={13} className={onlineSearching ? 'animate-spin' : ''} />
              重新检索台词接口
            </button>
          )}
        </div>

        {onlineSearching ? (
          <div className={`${panel} text-center py-10 text-sm text-[#72685b]`}>
            正在读取美剧与电影台词检索接口，匹配「{selectedTerm}」的前后文对白...
          </div>
        ) : onlineError && onlineQuotes.length === 0 && localMatches.length === 0 ? (
          <div className={`${panel} text-sm text-[#94352b] space-y-2`}>
            <p>{onlineError}</p>
          </div>
        ) : onlineQuotes.length === 0 && localMatches.length === 0 ? (
          <div className={`${panel} text-sm text-[#72685b]`}>
            暂未在当前台词库中命中「{selectedTerm}」，可试试其他常用词或通过下方外链查找。
          </div>
        ) : (
          <div className="space-y-4">
            {onlineQuotes.map((quote) => (
              <QuoteMatchCard
                key={quote.id}
                quote={quote}
                targetTerm={selectedTerm}
                wordInfo={selectedWord}
                accent={accent}
              />
            ))}
            {localMatches.slice(0, 20).map((match) => {
              const q = localMatchToQuote(match)
              return (
                <QuoteMatchCard
                  key={q.id}
                  quote={q}
                  targetTerm={selectedTerm}
                  wordInfo={selectedWord}
                  accent={accent}
                />
              )
            })}
          </div>
        )}
      </section>

      <details className={`${panel} space-y-4`}>
        <summary className="cursor-pointer py-1 font-semibold text-sm text-[#5c5347]">
          更多工具：原片视频片段外链 · 导入本机 SRT / VTT 字幕（已导 {episodes.length} 个）
        </summary>

        {selectedTerm && (
          <div className="grid gap-2.5 sm:grid-cols-2 pt-2">
            <a
              href={phraseSearchUrl(selectedTerm)}
              target="_blank"
              rel="noopener noreferrer"
              className={`${control} inline-flex items-center justify-between gap-2 bg-[#efe2cc] border-[#d6b483] text-[#7a4b19] font-medium`}
            >
              去 PlayPhrase 看原片视频片段
              <ExternalLink size={15} />
            </a>
            <a
              href={showSearchUrl(selectedTerm)}
              target="_blank"
              rel="noopener noreferrer"
              className={`${control} inline-flex items-center justify-between gap-2`}
            >
              网页搜索更多美剧剧本台词
              <ExternalLink size={15} />
            </a>
          </div>
        )}

        <div className="flex flex-col gap-3 sm:flex-row sm:items-end border-t border-[#e5decfe6] pt-4">
          <label className="min-w-0 flex-1 text-xs text-[#5c5347]">
            导入新字幕的剧名（可选）
            <input
              value={importSource}
              disabled={busy}
              onChange={(e) => setImportSource(e.target.value)}
              placeholder="例如 Friends、The Bear"
              className={`${control} mt-1.5 w-full`}
            />
          </label>
          <button
            type="button"
            className="min-h-11 rounded-2xl bg-[#996021] text-[#fdfbf7] px-4 py-2 text-sm font-medium inline-flex items-center justify-center gap-2 cursor-pointer disabled:opacity-50"
            disabled={busy}
            onClick={() => fileInput.current?.click()}
          >
            <FileUp size={16} />
            {busy ? '正在解析…' : '导入 SRT / VTT'}
          </button>
          <input
            ref={fileInput}
            type="file"
            accept=".srt,.vtt"
            multiple
            hidden
            onChange={(e) => {
              const files = Array.from(e.target.files ?? [])
              e.target.value = ''
              void importFiles(files)
            }}
          />
        </div>

        {reports.length > 0 && (
          <div className="space-y-1 text-xs text-[#5c5347]">
            {reports.map((r, i) => (
              <p key={i}>{r}</p>
            ))}
          </div>
        )}

        {episodes.length > 0 && (
          <div className="space-y-2 pt-2">
            {episodes.map((ep) => (
              <div
                key={ep.id}
                className="rounded-2xl border border-[#ded6c6] bg-[#f8f5ef] p-3 flex items-center justify-between gap-2 text-xs"
              >
                <div>
                  <p className="font-semibold text-[#2e2a24]">
                    {ep.show} · {ep.episode ?? '单集字幕'}
                  </p>
                  <p className="text-[#72685b]">{ep.filename}</p>
                </div>
                {pendingRemoval === ep.id ? (
                  <div className="flex items-center gap-1.5">
                    <button
                      type="button"
                      onClick={async () => {
                        await removeSubtitleEpisode(ep.id)
                        setEpisodes(await loadSubtitleLibrary())
                        setPendingRemoval(null)
                      }}
                      className="px-2.5 py-1 rounded-xl bg-[#94352b] text-white"
                    >
                      确认
                    </button>
                    <button
                      type="button"
                      onClick={() => setPendingRemoval(null)}
                      className="px-2.5 py-1 rounded-xl bg-[#e6dfd1]"
                    >
                      取消
                    </button>
                  </div>
                ) : (
                  <button
                    type="button"
                    onClick={() => setPendingRemoval(ep.id)}
                    className="p-2 rounded-xl text-[#8a7f70] hover:text-[#94352b]"
                  >
                    <Trash2 size={15} />
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </details>
    </div>
  )
}
