export type EbookFormat = 'epub' | 'mobi' | 'azw3' | 'txt'

export interface EbookParagraph {
  index: number
  text: string
  sentences: string[]
}

export interface EbookChapter {
  id: string
  index: number
  title: string
  section?: string
  subtitle?: string
  byline?: string
  href?: string
  wordCount: number
  paragraphs: EbookParagraph[]
}

export interface EbookItem {
  id: string
  title: string
  author: string
  description?: string
  format?: EbookFormat
  coverDataUrl?: string
  importedAt: number
  totalWords: number
  lastChapterId?: string
  lastParaIndex?: number
  lastSentenceIndex?: number
  chapters: EbookChapter[]
}

export interface SentenceContextMatch {
  bookId: string
  bookTitle: string
  bookAuthor: string
  chapterId: string
  chapterIndex: number
  chapterTitle: string
  chapterSection?: string
  chapterByline?: string
  paraIndex: number
  targetSentence: string
  currentParagraph: string
  previousParagraph: string
  nextParagraph: string
  confidence: 'exact' | 'fuzzy' | 'manual'
}

export interface WordOccurrence {
  bookId: string
  bookTitle: string
  chapterId: string
  chapterTitle: string
  paraIndex: number
  sentence: string
  paragraphText: string
  matchedForm: string
}

export type InterpretPreset = 'context' | 'syntax' | 'expressions' | 'cultural'

export interface InterpretPresetMeta {
  id: InterpretPreset
  label: string
  shortDesc: string
  promptInstruction: string
}

export const INTERPRET_PRESETS: InterpretPresetMeta[] = [
  {
    id: 'context',
    label: '结合上下文深读',
    shortDesc: '还原句意、前后文衔接、指代关系与作者弦外之音',
    promptInstruction:
      '【上下文语境深读】结合前文段落、当前段落与后文段落，用自然鲜活的中文讲清楚这句话在当前语境里真正想表达什么、代词或暗指指向谁、起到什么承上启下或转折讽刺的作用，以及作者的语气与潜台词。',
  },
  {
    id: 'syntax',
    label: '长难句拆解',
    shortDesc: '抓出主干，理清从句、分词与插入语层级',
    promptInstruction:
      '【句子主干与结构拆解】提炼句子的核心主谓宾主干，按阅读语序拆解定语从句、同位语、分词结构、倒装或破折号插入语等修饰层次，帮读者一眼看清句法骨架。',
  },
  {
    id: 'expressions',
    label: '地道表达与词源',
    shortDesc: '提炼句中鲜活词块、隐喻用法与词根词源串记',
    promptInstruction:
      '【地道表达与词源拆解】挑出句中 2～4 个最值得积累的关键词、地道短语或精妙动词，讲清它们在这句话里的鲜活含义，并顺带拆解核心词的词根词缀或词源画面，帮读者顺手串记同根词。',
  },
  {
    id: 'cultural',
    label: '人文与背景典故',
    shortDesc: '补充句中涉及的人物、历史文化梗或学科背景',
    promptInstruction:
      '【人物与文化背景点拨】如果句中涉及特定人物、欧美社会文化常识、历史事件、文学典故或学科概念，请做简明生动的背景科普，帮读者彻底读懂背后的梗。',
  },
]

export interface SentenceNote {
  id: string
  createdAt: number
  updatedAt: number
  sentence: string
  sourceKind: string
  presets: InterpretPreset[]
  followUpQuestion?: string
  aiInterpretation: string
  context?: SentenceContextMatch | null
}

export type MasteryStatus = 'new' | 'learning' | 'review' | 'mastered'

export interface UserWordRecord {
  wordId: string
  term: string
  phonetic?: string
  pos?: string
  meaning?: string
  bookId?: string
  bookTitle?: string
  chapterId?: string
  chapterTitle?: string
  paraIndex?: number
  contextSentence?: string
  aiNote?: string
  status: MasteryStatus
  repetitions: number
  interval: number
  easeFactor: number
  dueDate: number
  lastReviewed: number
  isStarred: boolean
  reviewCount: number
  wrongCount: number
}

export interface AppSettings {
  voiceAccent: 'us' | 'uk'
  autoPlayAudio: boolean
  fontSize: 'sm' | 'md' | 'lg'
}
export interface WordContextHint {
  bookId?: string
  bookTitle?: string
  chapterId?: string
  chapterTitle?: string
  paraIndex?: number
  sentence?: string
}
