import type { SubtitleEpisode } from '../types/subtitles'

let connection: Promise<IDBDatabase> | undefined

function getDatabase(): Promise<IDBDatabase> {
  if (connection) return connection
  connection = new Promise((resolve, reject) => {
    const request = indexedDB.open('companion_subtitle_library', 1)
    request.onupgradeneeded = () => {
      request.result.createObjectStore('episodes', { keyPath: 'id' })
    }
    request.onsuccess = () => {
      request.result.onversionchange = () => {
        request.result.close()
        connection = undefined
      }
      resolve(request.result)
    }
    request.onerror = () => { connection = undefined; reject(request.error) }
    request.onblocked = () => { connection = undefined; reject(new Error('请关闭其他应用窗口后重试。')) }
  })
  return connection
}

export async function loadSubtitleLibrary(): Promise<SubtitleEpisode[]> {
  const database = await getDatabase()
  return new Promise((resolve, reject) => {
    const transaction = database.transaction('episodes', 'readonly')
    const request = transaction.objectStore('episodes').getAll()
    transaction.oncomplete = () => resolve(request.result as SubtitleEpisode[])
    transaction.onabort = () => reject(transaction.error)
    transaction.onerror = () => reject(transaction.error)
  })
}

export async function saveSubtitleEpisode(episode: SubtitleEpisode): Promise<void> {
  const database = await getDatabase()
  return new Promise((resolve, reject) => {
    const transaction = database.transaction('episodes', 'readwrite')
    transaction.objectStore('episodes').put(episode)
    transaction.oncomplete = () => resolve()
    transaction.onabort = () => reject(transaction.error ?? new Error('字幕保存失败，请检查存储空间。'))
    transaction.onerror = () => reject(transaction.error)
  })
}

export async function removeSubtitleEpisode(id: string): Promise<void> {
  const database = await getDatabase()
  return new Promise((resolve, reject) => {
    const transaction = database.transaction('episodes', 'readwrite')
    transaction.objectStore('episodes').delete(id)
    transaction.oncomplete = () => resolve()
    transaction.onabort = () => reject(transaction.error)
    transaction.onerror = () => reject(transaction.error)
  })
}

export async function subtitleIdentity(show: string, content: string): Promise<string> {
  const input = new TextEncoder().encode(`${show.trim().toLowerCase()}\n${content.replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n')}`)
  const digest = await crypto.subtle.digest('SHA-256', input)
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
}
