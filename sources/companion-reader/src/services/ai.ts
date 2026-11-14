import {
  INTERPRET_PRESETS,
  type InterpretPreset,
  type SentenceContextMatch,
} from '../types/ebook'

export interface ConnectionConfig {
  baseUrl: string
  configured: boolean
  model: string
  ttsReady?: boolean
}

export interface OnlineSubtitleQuote {
  id: string
  source: 'online' | 'local'
  show: string
  episode: string | null
  year?: number | null
  genres?: string[]
  start: number
  end: number
  text: string
  matchedForm: string
  matchStart: number
  matchEnd: number
  previous: string[]
  next: string[]
  variant: boolean
}

declare global {
  interface Window {
    AndroidBridge?: {
      getConnectionConfig: () => string
      saveConnectionConfig: (baseUrl: string, apiKey: string) => string
      testConnection: () => string
      chatCompletion: (systemPrompt: string, userPrompt: string) => string
      searchSubtitles: (query: string, limit: number) => string
      speak: (text: string, accent: string) => boolean
      stopSpeaking: () => void
      isSpeaking?: () => boolean
      copyToClipboard?: (text: string) => boolean
      consumeIncomingText?: () => string
    }
    __onNativeAudioEnded?: () => void
    __onNativeIncomingText?: (payloadJson: string) => void
  }
}

const WEB_URL_KEY = 'toefl_feiniu_base_url'
const WEB_TOKEN_KEY = 'toefl_feiniu_token'
const ETYMOLOGY_CACHE_KEY = 'companion_ai_etymology_v1'
const SUBTITLE_AI_CACHE_KEY = 'companion_ai_subtitle_v1'
const SUBTITLE_QUOTES_CACHE_KEY = 'companion_subtitle_quotes_v1'
const PARA_TRANS_CACHE_KEY = 'companion_ai_para_trans_v1'
const SENTENCE_INTERPRET_CACHE_KEY = 'companion_ai_sentence_v1'
const AI_IDB_NAME = 'companion_ai_cache_db'
const AI_IDB_VERSION = 1

type StoreName =
  | 'etymology'
  | 'subtitle_usage'
  | 'subtitle_quotes'
  | 'paragraph_translation'
  | 'sentence_interpretation'

const STORES: StoreName[] = [
  'etymology',
  'subtitle_usage',
  'subtitle_quotes',
  'paragraph_translation',
  'sentence_interpretation',
]

let aiDbPromise: Promise<IDBDatabase> | null = null

function openAiCacheDB(): Promise<IDBDatabase> {
  if (aiDbPromise) return aiDbPromise
  aiDbPromise = new Promise((resolve, reject) => {
    if (typeof window === 'undefined' || !window.indexedDB) {
      reject(new Error('IndexedDB unavailable'))
      return
    }
    const req = window.indexedDB.open(AI_IDB_NAME, AI_IDB_VERSION)
    req.onupgradeneeded = () => {
      const db = req.result
      for (const store of STORES) {
        if (!db.objectStoreNames.contains(store)) {
          db.createObjectStore(store, { keyPath: 'key' })
        }
      }
    }
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => {
      aiDbPromise = null
      reject(req.error)
    }
  })
  return aiDbPromise
}

async function idbGet<T>(storeName: StoreName, key: string): Promise<T | null> {
  try {
    const db = await openAiCacheDB()
    return await new Promise<T | null>((resolve) => {
      const tx = db.transaction(storeName, 'readonly')
      const req = tx.objectStore(storeName).get(key)
      req.onsuccess = () => resolve(req.result ? (req.result.value as T) : null)
      req.onerror = () => resolve(null)
    })
  } catch {
    return null
  }
}

async function idbSet<T>(storeName: StoreName, key: string, value: T): Promise<void> {
  try {
    const db = await openAiCacheDB()
    await new Promise<void>((resolve) => {
      const tx = db.transaction(storeName, 'readwrite')
      tx.objectStore(storeName).put({ key, value, updatedAt: Date.now() })
      tx.oncomplete = () => resolve()
      tx.onerror = () => resolve()
    })
  } catch {
    // ignore
  }
}

function readCache(storageKey: string): Record<string, string> {
  try {
    const raw = localStorage.getItem(storageKey)
    return raw ? JSON.parse(raw) : {}
  } catch {
    return {}
  }
}

function writeCache(storageKey: string, key: string, value: string): void {
  try {
    const map = readCache(storageKey)
    map[key] = value
    localStorage.setItem(storageKey, JSON.stringify(map))
  } catch {
    // ignore storage quota errors
  }
}

async function cachedValue(storageKey: string, store: StoreName, key: string): Promise<string | null> {
  const syncVal = readCache(storageKey)[key]
  if (syncVal) return syncVal
  const idbVal = await idbGet<string>(store, key)
  if (idbVal) {
    writeCache(storageKey, key, idbVal)
    return idbVal
  }
  return null
}

function storeValue(storageKey: string, store: StoreName, key: string, value: string): void {
  writeCache(storageKey, key, value)
  void idbSet(store, key, value)
}

function readQuotesCache(): Record<string, OnlineSubtitleQuote[]> {
  try {
    const raw = localStorage.getItem(SUBTITLE_QUOTES_CACHE_KEY)
    return raw ? JSON.parse(raw) : {}
  } catch {
    return {}
  }
}

function writeQuotesCache(key: string, quotes: OnlineSubtitleQuote[]): void {
  try {
    const map = readQuotesCache()
    map[key] = quotes
    localStorage.setItem(SUBTITLE_QUOTES_CACHE_KEY, JSON.stringify(map))
  } catch {
    // ignore storage quota errors
  }
}

function etymologyKey(term: string, contextSentence = ''): string {
  const clean = term.trim().toLowerCase()
  return contextSentence.trim() ? JSON.stringify([clean, contextSentence.trim()]) : clean
}

export function getCachedEtymology(term: string, contextSentence = ''): string | null {
  return readCache(ETYMOLOGY_CACHE_KEY)[etymologyKey(term, contextSentence)] || null
}

export function loadCachedEtymologyAsync(term: string, contextSentence = ''): Promise<string | null> {
  return cachedValue(ETYMOLOGY_CACHE_KEY, 'etymology', etymologyKey(term, contextSentence))
}

export function getCachedSubtitleExplanation(matchId: string, term: string): string | null {
  return readCache(SUBTITLE_AI_CACHE_KEY)[`${matchId}::${term.trim().toLowerCase()}`] || null
}

export function loadCachedSubtitleExplanationAsync(matchId: string, term: string): Promise<string | null> {
  return cachedValue(SUBTITLE_AI_CACHE_KEY, 'subtitle_usage', `${matchId}::${term.trim().toLowerCase()}`)
}

export function getCachedSubtitleQuotes(term: string): OnlineSubtitleQuote[] | null {
  const list = readQuotesCache()[term.trim().toLowerCase()]
  return Array.isArray(list) && list.length > 0 ? list : null
}

function paragraphKey(chapterKey: string, paraIndex: number): string {
  return `${chapterKey.trim().toLowerCase()}::p${paraIndex}`
}

export function getCachedParagraphTranslation(chapterKey: string, paraIndex: number): string | null {
  return readCache(PARA_TRANS_CACHE_KEY)[paragraphKey(chapterKey, paraIndex)] || null
}

export function loadCachedParagraphTranslationAsync(chapterKey: string, paraIndex: number): Promise<string | null> {
  return cachedValue(PARA_TRANS_CACHE_KEY, 'paragraph_translation', paragraphKey(chapterKey, paraIndex))
}

export async function copyTextToSystemClipboard(text: string): Promise<boolean> {
  const clean = text.trim()
  if (!clean) return false
  if (typeof window !== 'undefined' && window.AndroidBridge?.copyToClipboard) {
    try {
      return window.AndroidBridge.copyToClipboard(clean)
    } catch {
      // fallback below
    }
  }
  if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(clean)
      return true
    } catch {
      return false
    }
  }
  return false
}

export async function exportAllAiCache() {
  return {
    etymology: readCache(ETYMOLOGY_CACHE_KEY),
    subtitleUsage: readCache(SUBTITLE_AI_CACHE_KEY),
    subtitleQuotes: readQuotesCache(),
    paragraphTranslation: readCache(PARA_TRANS_CACHE_KEY),
    sentenceInterpretation: readCache(SENTENCE_INTERPRET_CACHE_KEY),
  }
}

export function getConnectionConfig(): ConnectionConfig {
  if (typeof window !== 'undefined' && window.AndroidBridge) {
    try {
      return JSON.parse(window.AndroidBridge.getConnectionConfig())
    } catch {
      // fallback below
    }
  }
  const baseUrl = localStorage.getItem(WEB_URL_KEY) || 'http://100.90.158.48:8766/v1'
  const token = localStorage.getItem(WEB_TOKEN_KEY) || ''
  return { baseUrl, configured: token.trim().length > 0, model: 'gpt-6-luna' }
}

export function saveConnectionConfig(
  baseUrl: string,
  apiKey: string
): { ok: boolean; error?: string; config?: ConnectionConfig } {
  const cleanUrl = baseUrl.trim().replace(/\/+$/, '')
  if (!cleanUrl) return { ok: false, error: '请输入飞牛服务地址' }
  if (typeof window !== 'undefined' && window.AndroidBridge) {
    try {
      const res = JSON.parse(window.AndroidBridge.saveConnectionConfig(cleanUrl, apiKey))
      if (!res.ok) return { ok: false, error: res.error || '保存失败' }
      return { ok: true, config: getConnectionConfig() }
    } catch {
      return { ok: false, error: '保存连接配置失败' }
    }
  }
  localStorage.setItem(WEB_URL_KEY, cleanUrl.endsWith('/v1') ? cleanUrl : `${cleanUrl}/v1`)
  if (apiKey.trim()) localStorage.setItem(WEB_TOKEN_KEY, apiKey.trim())
  return { ok: true, config: getConnectionConfig() }
}

export async function testFeiniuConnection(): Promise<{ ok: boolean; message: string }> {
  if (typeof window !== 'undefined' && window.AndroidBridge) {
    return new Promise((resolve) => {
      window.setTimeout(() => {
        try {
          const res = JSON.parse(window.AndroidBridge!.testConnection())
          resolve(
            res.ok
              ? { ok: true, message: `已连接飞牛 (${res.model || 'gpt-6-luna'} · 端口 ${res.port || 8766})` }
              : { ok: false, message: res.error || '连接失败' }
          )
        } catch {
          resolve({ ok: false, message: '连接测试异常' })
        }
      }, 10)
    })
  }
  const cfg = getConnectionConfig()
  const token = localStorage.getItem(WEB_TOKEN_KEY) || ''
  if (!token) return { ok: false, message: '请先填写飞牛 8766 访问令牌' }
  try {
    const resp = await fetch(`${cfg.baseUrl}/health`, { headers: { Authorization: `Bearer ${token}` } })
    if (!resp.ok) return { ok: false, message: `连接返回 HTTP ${resp.status}` }
    const data = await resp.json()
    return { ok: true, message: `已连接飞牛 (${data.model || 'gpt-6-luna'} · 端口 8766)` }
  } catch {
    return { ok: false, message: '无法连接飞牛 8766，请检查网络或 Tailscale' }
  }
}

export async function callLunaChat(systemPrompt: string, userPrompt: string): Promise<string> {
  if (typeof window !== 'undefined' && window.AndroidBridge) {
    return new Promise((resolve, reject) => {
      window.setTimeout(() => {
        try {
          const res = JSON.parse(window.AndroidBridge!.chatCompletion(systemPrompt, userPrompt))
          if (res.ok && res.content) resolve(String(res.content).trim())
          else reject(new Error(res.error || '生成失败，请检查飞牛连接设置'))
        } catch (err) {
          reject(err instanceof Error ? err : new Error('调用飞牛 6-luna 失败'))
        }
      }, 15)
    })
  }
  const cfg = getConnectionConfig()
  const token = localStorage.getItem(WEB_TOKEN_KEY) || ''
  if (!token) throw new Error('请先在右上角「设置」中填入飞牛 8766 访问令牌')
  const resp = await fetch(`${cfg.baseUrl}/chat/completions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({
      model: 'gpt-6-luna',
      reasoning_effort: 'low',
      stream: false,
      messages: [
        { role: 'system', content: systemPrompt },
        { role: 'user', content: userPrompt },
      ],
    }),
  })
  if (!resp.ok) {
    throw new Error(resp.status === 401 ? '飞牛访问令牌无效，请检查设置' : `请求失败 (HTTP ${resp.status})`)
  }
  const data = await resp.json()
  const text = data?.choices?.[0]?.message?.content?.trim()
  if (!text) throw new Error('飞牛 6-luna 返回了空结果，请重试')
  return text
}

export async function searchOnlineSubtitles(
  query: string,
  limit = 8,
  forceRefresh = false
): Promise<OnlineSubtitleQuote[]> {
  const clean = query.trim()
  if (!clean) return []
  const cacheKey = clean.toLowerCase()
  if (!forceRefresh) {
    const syncCached = getCachedSubtitleQuotes(cacheKey)
    if (syncCached) return syncCached.slice(0, limit)
    const idbCached = await idbGet<OnlineSubtitleQuote[]>('subtitle_quotes', cacheKey)
    if (idbCached && idbCached.length > 0) {
      writeQuotesCache(cacheKey, idbCached)
      return idbCached.slice(0, limit)
    }
  }
  const remember = (matches: OnlineSubtitleQuote[]) => {
    if (matches.length > 0) {
      writeQuotesCache(cacheKey, matches)
      void idbSet('subtitle_quotes', cacheKey, matches)
    }
    return matches
  }
  if (typeof window !== 'undefined' && window.AndroidBridge) {
    return new Promise((resolve, reject) => {
      window.setTimeout(() => {
        try {
          const res = JSON.parse(window.AndroidBridge!.searchSubtitles(clean, limit))
          if (res.ok && Array.isArray(res.matches)) resolve(remember(res.matches))
          else reject(new Error(res.error || '未检索到匹配台词'))
        } catch (err) {
          reject(err instanceof Error ? err : new Error('字幕接口读取失败'))
        }
      }, 15)
    })
  }
  const cfg = getConnectionConfig()
  const token = localStorage.getItem(WEB_TOKEN_KEY) || ''
  const resp = await fetch(`${cfg.baseUrl}/subtitles/search?q=${encodeURIComponent(clean)}&limit=${limit}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (!resp.ok) throw new Error(`读取字幕检索接口失败 (HTTP ${resp.status})`)
  const data = await resp.json()
  return remember(Array.isArray(data?.matches) ? data.matches : [])
}

export async function explainWordEtymology(
  word: {
    term: string
    pos?: string
    meaning?: string
    phonetic?: string
    contextSentence?: string
    bookTitle?: string
    chapterTitle?: string
  },
  forceRefresh = false
): Promise<string> {
  const key = etymologyKey(word.term, word.contextSentence)
  if (!forceRefresh) {
    const cached = await loadCachedEtymologyAsync(word.term, word.contextSentence)
    if (cached) return cached
  }
  const systemPrompt = [
    '你是一位温和、生活化、擅长结合原书语境与词源画面帮人记单词的英语伴读朋友。',
    '请用亲切自然的中文，拆解目标单词在当前阅读语境中的含义、词根词缀与词源故事。',
    '不要加寒暄或代码围栏，直接按以下三个小节输出：',
    '【语境词义与词根拆解】先一句话点明它在书中原句里的意思与词性；再拆开前缀、词根、后缀，讲清每块的本义。',
    '【词源画面故事】讲这个词最早来自什么具体动作、画面或历史场景，又如何演变成今天的意思。',
    '【顺手串记一串词】列出 2～4 个同根常见词，一句话点出它们的区别。',
  ].join('\n')
  const notes = [
    `单词：${word.term}`,
    word.phonetic ? `音标：${word.phonetic}` : '',
    word.pos ? `词性：${word.pos}` : '',
    word.meaning ? `已知释义：${word.meaning}` : '',
    word.bookTitle ? `所在书籍：《${word.bookTitle}》${word.chapterTitle ? ` · ${word.chapterTitle}` : ''}` : '',
    word.contextSentence ? `书中原句：${word.contextSentence}` : '',
  ]
    .filter(Boolean)
    .join('\n')
  const result = await callLunaChat(systemPrompt, notes)
  storeValue(ETYMOLOGY_CACHE_KEY, 'etymology', key, result)
  return result
}

export async function explainSubtitleUsage(
  params: {
    matchId: string
    term: string
    matchedForm: string
    show: string
    episode?: string | null
    year?: number | null
    text: string
    previous?: string[]
    next?: string[]
    wordInfo?: { meaning?: string } | null
  },
  forceRefresh = false
): Promise<string> {
  const cacheId = `${params.matchId}::${params.term.trim().toLowerCase()}`
  if (!forceRefresh) {
    const cached = await loadCachedSubtitleExplanationAsync(params.matchId, params.term)
    if (cached) return cached
  }
  const systemPrompt = [
    '你是一位熟悉经典美剧与英美电影、说话亲切自然的英语伴读朋友。',
    '用户命中了一段真实影视台词。请结合作品背景与前后句，讲透目标词在这里的地道用法；不确定的细节不要捏造。',
    '不要输出前言或代码围栏，直接按以下三个小节输出：',
    '【剧集情节与现场气氛】',
    '【前后台词在聊什么】并给出当前句贴合语境的中文翻译。',
    '【这里的用法怎么记】',
  ].join('\n')
  const userPrompt = [
    `目标单词：${params.term}（台词中出现形式：${params.matchedForm || params.term}）`,
    params.wordInfo?.meaning ? `已知释义：${params.wordInfo.meaning}` : '',
    `影视作品：《${params.show}》${params.episode ? `（${params.episode}）` : params.year ? `（${params.year}）` : ''}`,
    `前文台词：${params.previous?.length ? params.previous.join(' / ') : '（无前句）'}`,
    `当前命中台词：${params.text}`,
    `后文台词：${params.next?.length ? params.next.join(' / ') : '（无后句）'}`,
  ]
    .filter(Boolean)
    .join('\n')
  const result = await callLunaChat(systemPrompt, userPrompt)
  storeValue(SUBTITLE_AI_CACHE_KEY, 'subtitle_usage', cacheId, result)
  return result
}

export async function translateBookParagraph(
  params: {
    chapterKey: string
    bookTitle: string
    chapterTitle: string
    paraIndex: number
    text: string
    previousParagraph?: string
  },
  forceRefresh = false
): Promise<string> {
  const cacheId = paragraphKey(params.chapterKey, params.paraIndex)
  if (!forceRefresh) {
    const cached = await loadCachedParagraphTranslationAsync(params.chapterKey, params.paraIndex)
    if (cached) return cached
  }
  const systemPrompt = [
    '你是一位精通英文原版书刊阅读、文笔自然流畅的 AI 伴读朋友。',
    '请结合书籍章节上下文翻译当前英文段落，并点拨重点表达与叙事脉络。',
    '不要输出寒暄或代码围栏，直接按以下两个小节输出：',
    '【段落全文译文】完整、准确、自然地译成中文，保留作者语气与节奏，拒绝机翻腔。',
    '【语境脉络与重点表达】一句话说明本段在上下文中的作用，再挑 2～3 个地道词汇、短语或复杂句式点明含义。',
  ].join('\n')
  const userPrompt = [
    `书籍：《${params.bookTitle}》`,
    `章节：《${params.chapterTitle}》 第 ${params.paraIndex} 段`,
    params.previousParagraph ? `前一段参考：${params.previousParagraph.slice(0, 450)}` : '',
    '英文原文：',
    params.text,
  ]
    .filter(Boolean)
    .join('\n')
  const result = await callLunaChat(systemPrompt, userPrompt)
  storeValue(PARA_TRANS_CACHE_KEY, 'paragraph_translation', cacheId, result)
  return result
}

export async function interpretSentenceWithContext(
  params: {
    sentence: string
    presets: InterpretPreset[]
    followUpQuestion?: string
    context?: SentenceContextMatch | null
    customContextText?: string
  },
  forceRefresh = false
): Promise<string> {
  const activePresets: InterpretPreset[] = params.presets.length > 0 ? params.presets : ['context', 'expressions']
  const questionClean = (params.followUpQuestion || '').trim()
  const ctx = params.context
  const ctxKey = ctx ? `${ctx.bookId}:${ctx.chapterId}:p${ctx.paraIndex}` : (params.customContextText || '').slice(0, 60)
  const cacheId = [
    params.sentence.trim().toLowerCase().slice(0, 160),
    [...activePresets].sort().join(','),
    questionClean,
    ctxKey,
  ].join('::')

  if (!forceRefresh) {
    const cached = await cachedValue(SENTENCE_INTERPRET_CACHE_KEY, 'sentence_interpretation', cacheId)
    if (cached) return cached
  }

  const systemPrompt = [
    '你是一位博学、敏锐、说话自然亲切的英文原版书伴读伙伴。',
    '用户在读英文电子书或外刊时复制/选中了一句话，希望你结合它所在的上下文段落深入解读。',
    '不要寒暄、不要代码围栏。先输出【贴合语境的中文精译】，再按顺序输出以下小节：',
    ...(questionClean ? ['【回应你的追问】结合原句与前后文直接、透彻地回答用户的问题。'] : []),
    ...INTERPRET_PRESETS.filter((p) => activePresets.includes(p.id)).map((p) => p.promptInstruction),
    '如果上下文不足以确定某个指代或背景，请明确说明不确定，不要编造。',
  ].join('\n')

  const contextBlock = ctx
    ? [
        '【原书上下文】',
        `书名：《${ctx.bookTitle}》${ctx.bookAuthor ? `（${ctx.bookAuthor}）` : ''}`,
        `章节：《${ctx.chapterTitle}》${ctx.chapterSection ? ` [${ctx.chapterSection}]` : ''}${ctx.chapterByline ? ` · ${ctx.chapterByline}` : ''}（第 ${ctx.paraIndex} 段）`,
        ctx.previousParagraph ? `前一段：${ctx.previousParagraph}` : '',
        `所在段落：${ctx.currentParagraph}`,
        ctx.nextParagraph ? `后一段：${ctx.nextParagraph}` : '',
      ]
        .filter(Boolean)
        .join('\n')
    : params.customContextText?.trim()
    ? `【用户补充的上下文】\n${params.customContextText.trim()}`
    : '【上下文说明】没有找到原书段落，请仅基于句子本身解读，并提醒用户导入原书可以得到更准确的解读。'

  const userPrompt = [
    '【目标句子】',
    params.sentence.trim(),
    '',
    contextBlock,
    questionClean ? `\n【用户追问】\n${questionClean}` : '',
  ]
    .filter(Boolean)
    .join('\n')

  const result = await callLunaChat(systemPrompt, userPrompt)
  storeValue(SENTENCE_INTERPRET_CACHE_KEY, 'sentence_interpretation', cacheId, result)
  return result
}
