import type { AppSettings, EbookItem, SentenceNote, UserWordRecord } from '../types/ebook'
import { calculateSM2, type SM2Rating } from './sm2'

const DB_NAME = 'ebook_companion_db'
const DB_VERSION = 1
const STARTER_DISMISSED_KEY = 'companion_starter_book_dismissed'

const DEFAULT_SETTINGS: AppSettings = {
  voiceAccent: 'us',
  autoPlayAudio: false,
  fontSize: 'md',
}

// Purge any legacy TOEFL databases & localStorage keys on boot
export function purgeLegacyToeflData(): void {
  if (typeof window === 'undefined') return
  try {
    const keysToRemove: string[] = []
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i)
      if (k && k.startsWith('toefl_') && k !== 'toefl_feiniu_base_url' && k !== 'toefl_feiniu_token') {
        keysToRemove.push(k)
      }
    }
    keysToRemove.forEach((k) => localStorage.removeItem(k))
  } catch {
    // ignore
  }
  try {
    window.indexedDB?.deleteDatabase('toefl_vocab_db')
    window.indexedDB?.deleteDatabase('toefl_ai_persistent_cache')
    window.indexedDB?.deleteDatabase('toefl_subtitle_library')
  } catch {
    // ignore
  }
}

purgeLegacyToeflData()

class DatabaseService {
  private dbPromise: Promise<IDBDatabase> | null = null
  private starterLoadPromise: Promise<EbookItem[]> | null = null

  private getDB(): Promise<IDBDatabase> {
    if (this.dbPromise) return this.dbPromise

    this.dbPromise = new Promise((resolve, reject) => {
      if (typeof window === 'undefined' || !window.indexedDB) {
        reject(new Error('IndexedDB is not supported'))
        return
      }

      const request = indexedDB.open(DB_NAME, DB_VERSION)

      request.onupgradeneeded = (event) => {
        const db = (event.target as IDBOpenDBRequest).result

        if (!db.objectStoreNames.contains('books')) {
          const bookStore = db.createObjectStore('books', { keyPath: 'id' })
          bookStore.createIndex('importedAt', 'importedAt', { unique: false })
        }

        if (!db.objectStoreNames.contains('sentence_notes')) {
          const noteStore = db.createObjectStore('sentence_notes', { keyPath: 'id' })
          noteStore.createIndex('updatedAt', 'updatedAt', { unique: false })
        }

        if (!db.objectStoreNames.contains('user_words')) {
          const wordStore = db.createObjectStore('user_words', { keyPath: 'wordId' })
          wordStore.createIndex('status', 'status', { unique: false })
          wordStore.createIndex('dueDate', 'dueDate', { unique: false })
          wordStore.createIndex('isStarred', 'isStarred', { unique: false })
        }

        if (!db.objectStoreNames.contains('settings')) {
          db.createObjectStore('settings', { keyPath: 'key' })
        }
      }

      request.onsuccess = () => resolve(request.result)
      request.onerror = () => {
        this.dbPromise = null
        reject(request.error)
      }
    })

    return this.dbPromise
  }

  // --- Ebook Bookshelf Management ---

  public async getAllBooks(): Promise<EbookItem[]> {
    const db = await this.getDB()
    const list = await new Promise<EbookItem[]>((resolve, reject) => {
      const tx = db.transaction('books', 'readonly')
      const req = tx.objectStore('books').getAll()
      req.onsuccess = () => resolve((req.result as EbookItem[]) || [])
      req.onerror = () => reject(req.error)
    })

    if (list.length > 0) {
      return list.sort((a, b) => b.importedAt - a.importedAt)
    }

    // Auto-seed starter English EPUB (The New Yorker) if not dismissed
    if (localStorage.getItem(STARTER_DISMISSED_KEY) === '1') {
      return []
    }
    if (!this.starterLoadPromise) {
      this.starterLoadPromise = (async () => {
        try {
          const res = await fetch('/books/starter-new-yorker.json')
          if (!res.ok) return []
          const starterBook = (await res.json()) as EbookItem
          starterBook.lastChapterId = starterBook.chapters[8]?.id || starterBook.chapters[0]?.id
          starterBook.lastParaIndex = 1
          await this.saveBook(starterBook)
          return [starterBook]
        } catch {
          return []
        } finally {
          this.starterLoadPromise = null
        }
      })()
    }
    return this.starterLoadPromise
  }

  public async saveBook(book: EbookItem): Promise<void> {
    const db = await this.getDB()
    return new Promise((resolve, reject) => {
      const tx = db.transaction('books', 'readwrite')
      const req = tx.objectStore('books').put(book)
      req.onsuccess = () => resolve()
      req.onerror = () => reject(req.error)
    })
  }

  public async updateBookProgress(
    bookId: string,
    lastChapterId: string,
    lastParaIndex = 1,
    lastSentenceIndex = 0
  ): Promise<void> {
    const db = await this.getDB()
    return new Promise((resolve, reject) => {
      const tx = db.transaction('books', 'readwrite')
      const store = tx.objectStore('books')
      const getReq = store.get(bookId)
      getReq.onsuccess = () => {
        const existing = getReq.result as EbookItem | undefined
        if (existing) {
          existing.lastChapterId = lastChapterId
          existing.lastParaIndex = lastParaIndex
          existing.lastSentenceIndex = lastSentenceIndex
          store.put(existing)
        }
        resolve()
      }
      getReq.onerror = () => reject(getReq.error)
    })
  }

  public async deleteBook(bookId: string): Promise<void> {
    if (bookId === 'epub-new-yorker-2026-11-09') {
      localStorage.setItem(STARTER_DISMISSED_KEY, '1')
    }
    const db = await this.getDB()
    return new Promise((resolve, reject) => {
      const tx = db.transaction('books', 'readwrite')
      const req = tx.objectStore('books').delete(bookId)
      req.onsuccess = () => resolve()
      req.onerror = () => reject(req.error)
    })
  }

  // --- Sentence Context Interpretation Notes ---

  public async getAllSentenceNotes(): Promise<SentenceNote[]> {
    const db = await this.getDB()
    return new Promise((resolve, reject) => {
      const tx = db.transaction('sentence_notes', 'readonly')
      const req = tx.objectStore('sentence_notes').getAll()
      req.onsuccess = () => {
        const list = ((req.result as SentenceNote[]) || []).sort(
          (a, b) => b.updatedAt - a.updatedAt
        )
        resolve(list)
      }
      req.onerror = () => reject(req.error)
    })
  }

  public async saveSentenceNote(note: SentenceNote): Promise<void> {
    const db = await this.getDB()
    return new Promise((resolve, reject) => {
      const tx = db.transaction('sentence_notes', 'readwrite')
      const req = tx.objectStore('sentence_notes').put(note)
      req.onsuccess = () => resolve()
      req.onerror = () => reject(req.error)
    })
  }

  public async deleteSentenceNote(noteId: string): Promise<void> {
    const db = await this.getDB()
    return new Promise((resolve, reject) => {
      const tx = db.transaction('sentence_notes', 'readwrite')
      const req = tx.objectStore('sentence_notes').delete(noteId)
      req.onsuccess = () => resolve()
      req.onerror = () => reject(req.error)
    })
  }

  // --- Book Vocabulary Records ---

  public async getWordRecord(wordId: string): Promise<UserWordRecord | null> {
    const key = wordId.trim().toLowerCase()
    const db = await this.getDB()
    return new Promise((resolve, reject) => {
      const tx = db.transaction('user_words', 'readonly')
      const req = tx.objectStore('user_words').get(key)
      req.onsuccess = () => resolve(req.result || null)
      req.onerror = () => reject(req.error)
    })
  }

  public async getAllWordRecords(): Promise<Record<string, UserWordRecord>> {
    const db = await this.getDB()
    return new Promise((resolve, reject) => {
      const tx = db.transaction('user_words', 'readonly')
      const req = tx.objectStore('user_words').getAll()
      req.onsuccess = () => {
        const map: Record<string, UserWordRecord> = {}
        const list: UserWordRecord[] = req.result || []
        for (const item of list) {
          map[item.wordId] = item
        }
        resolve(map)
      }
      req.onerror = () => reject(req.error)
    })
  }

  public async upsertWordFromBook(params: {
    term: string
    meaning?: string
    bookId?: string
    bookTitle?: string
    chapterId?: string
    chapterTitle?: string
    paraIndex?: number
    contextSentence?: string
    aiNote?: string
    isStarred?: boolean
  }): Promise<UserWordRecord> {
    const wordId = params.term.trim().toLowerCase()
    const current = await this.getWordRecord(wordId)
    const updated: UserWordRecord = {
      wordId,
      term: current?.term || params.term.trim(),
      meaning: params.meaning ?? current?.meaning ?? '',
      bookId: params.bookId ?? current?.bookId,
      bookTitle: params.bookTitle ?? current?.bookTitle,
      chapterId: params.chapterId ?? current?.chapterId,
      chapterTitle: params.chapterTitle ?? current?.chapterTitle,
      paraIndex: params.paraIndex ?? current?.paraIndex,
      contextSentence: params.contextSentence ?? current?.contextSentence,
      aiNote: params.aiNote ?? current?.aiNote,
      status: current?.status ?? 'new',
      repetitions: current?.repetitions ?? 0,
      interval: current?.interval ?? 0,
      easeFactor: current?.easeFactor ?? 2.5,
      dueDate: current?.dueDate ?? Date.now(),
      lastReviewed: current?.lastReviewed ?? Date.now(),
      isStarred: params.isStarred !== undefined ? params.isStarred : (current?.isStarred ?? true),
      reviewCount: current?.reviewCount ?? 0,
      wrongCount: current?.wrongCount ?? 0,
    }

    const db = await this.getDB()
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction('user_words', 'readwrite')
      const req = tx.objectStore('user_words').put(updated)
      req.onsuccess = () => resolve()
      req.onerror = () => reject(req.error)
    })
    return updated
  }

  public async updateWordProgress(wordId: string, term: string, rating: SM2Rating): Promise<UserWordRecord> {
    const key = wordId.trim().toLowerCase()
    const current = await this.getWordRecord(key)
    const sm2 = calculateSM2(current, rating)
    const updated: UserWordRecord = {
      wordId: key,
      term: current?.term || term,
      meaning: current?.meaning,
      bookId: current?.bookId,
      bookTitle: current?.bookTitle,
      chapterId: current?.chapterId,
      chapterTitle: current?.chapterTitle,
      paraIndex: current?.paraIndex,
      contextSentence: current?.contextSentence,
      aiNote: current?.aiNote,
      status: sm2.status,
      repetitions: sm2.repetitions,
      interval: sm2.interval,
      easeFactor: sm2.easeFactor,
      dueDate: sm2.dueDate,
      lastReviewed: Date.now(),
      isStarred: current?.isStarred ?? true,
      reviewCount: (current?.reviewCount ?? 0) + 1,
      wrongCount: rating === 1 ? (current?.wrongCount ?? 0) + 1 : (current?.wrongCount ?? 0),
    }
    const db = await this.getDB()
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction('user_words', 'readwrite')
      const req = tx.objectStore('user_words').put(updated)
      req.onsuccess = () => resolve()
      req.onerror = () => reject(req.error)
    })
    return updated
  }

  public async toggleStar(
    term: string,
    context?: {
      bookId?: string
      bookTitle?: string
      chapterId?: string
      chapterTitle?: string
      paraIndex?: number
      contextSentence?: string
      aiNote?: string
    }
  ): Promise<boolean> {
    const wordId = term.trim().toLowerCase()
    const current = await this.getWordRecord(wordId)
    const nextStarred = !current?.isStarred
    await this.upsertWordFromBook({
      term,
      ...context,
      isStarred: nextStarred,
    })
    return nextStarred
  }

  public async deleteWordRecord(wordId: string): Promise<void> {
    const db = await this.getDB()
    return new Promise((resolve, reject) => {
      const tx = db.transaction('user_words', 'readwrite')
      const req = tx.objectStore('user_words').delete(wordId.trim().toLowerCase())
      req.onsuccess = () => resolve()
      req.onerror = () => reject(req.error)
    })
  }

  // --- Settings ---

  public async getSettings(): Promise<AppSettings> {
    const db = await this.getDB()
    return new Promise((resolve, reject) => {
      const tx = db.transaction('settings', 'readonly')
      const req = tx.objectStore('settings').getAll()
      req.onsuccess = () => {
        const map: Record<string, unknown> = {}
        const items = req.result || []
        for (const it of items) {
          map[it.key] = it.value
        }
        resolve({ ...DEFAULT_SETTINGS, ...map })
      }
      req.onerror = () => reject(req.error)
    })
  }

  public async updateSetting<K extends keyof AppSettings>(key: K, value: AppSettings[K]): Promise<void> {
    const db = await this.getDB()
    return new Promise((resolve, reject) => {
      const tx = db.transaction('settings', 'readwrite')
      const req = tx.objectStore('settings').put({ key, value })
      req.onsuccess = () => resolve()
      req.onerror = () => reject(req.error)
    })
  }
}

export const dbService = new DatabaseService()
