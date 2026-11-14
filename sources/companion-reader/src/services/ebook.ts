import type {
  EbookChapter,
  EbookFormat,
  EbookItem,
  EbookParagraph,
  SentenceContextMatch,
  WordOccurrence,
} from '../types/ebook'

const ABBREV_REGEX =
  /\b(?:Mr|Mrs|Ms|Dr|Prof|St|Jr|Sr|vs|etc|Fig|No|Vol|Dept|Inc|Ltd|Co|Jan|Feb|Mar|Apr|Aug|Sept|Oct|Nov|Dec|U\.S|U\.K|N\.Y\.C|F\.B\.I|C\.I\.A|C\.E\.O|Ph\.D|M\.D|B\.A|M\.A|a\.m|p\.m)\.$/i

export function splitEnglishSentences(raw: string): string[] {
  const text = raw.replace(/\s+/g, ' ').trim()
  if (!text) return []
  const parts = text.split(/(?<=[.!?।…"”’'])\s+(?=[A-Z“"‘'(\[0-9])/g)
  const merged: string[] = []
  for (const rawPart of parts) {
    const p = rawPart.trim()
    if (!p) continue
    const prev = merged[merged.length - 1]
    if (prev && (ABBREV_REGEX.test(prev) || /\b[A-Z]\.$/.test(prev))) {
      merged[merged.length - 1] = `${prev} ${p}`
    } else {
      merged.push(p)
    }
  }
  return merged.length > 0 ? merged : [text]
}

export function countEnglishWords(text: string): number {
  const matches = text.match(/[A-Za-z'’-]+/g)
  return matches ? matches.length : 0
}

function decodeHtmlEntities(input: string): string {
  if (!input) return ''
  const named: Record<string, string> = {
    amp: '&',
    lt: '<',
    gt: '>',
    quot: '"',
    apos: "'",
    nbsp: ' ',
    mdash: '—',
    ndash: '–',
    hellip: '…',
    lsquo: '‘',
    rsquo: '’',
    ldquo: '“',
    rdquo: '”',
  }
  return input
    .replace(/&(#x[\da-fA-F]+|#\d+|[a-zA-Z]+);/g, (full, entity: string) => {
      const lower = entity.toLowerCase()
      if (named[lower]) return named[lower]
      if (lower.startsWith('#x')) {
        const code = parseInt(lower.slice(2), 16)
        return code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : ''
      }
      if (lower.startsWith('#')) {
        const code = parseInt(lower.slice(1), 10)
        return code > 0 && code <= 0x10ffff ? String.fromCodePoint(code) : ''
      }
      return full
    })
    .replace(/\u00a0/g, ' ')
    .replace(/♦/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}

export function extractParagraphsFromHtml(
  htmlSource: string,
  chapterTitle = '',
  sectionTitle = ''
): {
  paragraphs: EbookParagraph[]
  byline: string
  subtitle: string
  detectedTitle: string
  detectedSection: string
  wordCount: number
} {
  const cleanedHtml = htmlSource
    .replace(/<script[\s\S]*?<\/script>/gi, '')
    .replace(/<style[\s\S]*?<\/style>/gi, '')
    .replace(/<br\s*\/?>/gi, '\n')

  let detectedTitle = chapterTitle.trim()
  if (!detectedTitle) {
    const hMatch = cleanedHtml.match(/<h[1-3][^>]*>([\s\S]*?)<\/h[1-3]>/i)
    if (hMatch) {
      detectedTitle = decodeHtmlEntities(hMatch[1].replace(/<[^>]+>/g, ' '))
    }
  }

  const blockRegex = /<(?:p|h[1-6]|blockquote|li)[^>]*>([\s\S]*?)<\/(?:p|h[1-6]|blockquote|li)>/gi
  const rawBlocks: string[] = []
  let match: RegExpExecArray | null
  while ((match = blockRegex.exec(cleanedHtml)) !== null) {
    const text = decodeHtmlEntities(match[1].replace(/<[^>]+>/g, ' '))
    if (text) rawBlocks.push(text)
  }

  if (rawBlocks.length === 0) {
    const fallbackLines = decodeHtmlEntities(cleanedHtml.replace(/<[^>]+>/g, '\n'))
      .split(/\n+/)
      .map((s) => s.trim())
      .filter((s) => s.length > 0)
    rawBlocks.push(...fallbackLines)
  }

  let byline = ''
  let subtitle = ''
  let detectedSection = sectionTitle
  const filtered: string[] = []

  for (let i = 0; i < rawBlocks.length; i++) {
    const p = rawBlocks[i]
    if (i < 5) {
      if (
        (detectedTitle && p.toLowerCase() === detectedTitle.toLowerCase()) ||
        (detectedSection && p.toLowerCase() === detectedSection.toLowerCase())
      ) {
        continue
      }
      if (
        /^(?:profiles|comment|the pictures|books|musical events|the art world|fiction|poems|shouts & murmurs)$/i.test(
          p
        ) ||
        p.endsWith('Dept.') ||
        p.startsWith('Dept. of')
      ) {
        detectedSection =
          detectedSection && !detectedSection.toLowerCase().includes(p.toLowerCase())
            ? `${detectedSection} · ${p}`
            : p
        continue
      }
      if (/^By\s+[A-Z]/i.test(p) && p.length <= 95 && !byline) {
        byline = p
        continue
      }
      if (
        /^(?:Photograph by|Photo illustration by|Illustration by)/i.test(p) ||
        (p.includes('Photograph by ') && p.length <= 220)
      ) {
        continue
      }
      if (
        p.includes('Make sense of the midterm results.') ||
        p.includes('Catch up on our real-time reporting')
      ) {
        continue
      }
      if (!byline && filtered.length === 0 && p.length <= 240 && i <= 3 && rawBlocks.length > 3) {
        subtitle = p
        continue
      }
    }
    if (/^(?:Photograph by|Illustration by)/i.test(p) && p.length <= 180) {
      continue
    }
    filtered.push(p)
  }

  let wordCount = 0
  const paragraphs: EbookParagraph[] = filtered.map((text, idx) => {
    wordCount += countEnglishWords(text)
    return {
      index: idx + 1,
      text,
      sentences: splitEnglishSentences(text),
    }
  })

  return {
    paragraphs,
    byline,
    subtitle,
    detectedTitle: detectedTitle || 'Chapter',
    detectedSection,
    wordCount,
  }
}

interface ZipEntryInfo {
  filename: string
  compressionMethod: number
  compressedSize: number
  uncompressedSize: number
  localHeaderOffset: number
}

async function inflateRawBytes(compressed: Uint8Array): Promise<Uint8Array> {
  if (typeof DecompressionStream === 'undefined') {
    throw new Error('当前环境不支持 DecompressionStream 解压 EPUB')
  }
  const ds = new DecompressionStream('deflate-raw')
  const writer = ds.writable.getWriter()
  writer.write(compressed as unknown as BufferSource).catch(() => {})
  writer.close().catch(() => {})

  const reader = ds.readable.getReader()
  const chunks: Uint8Array[] = []
  let total = 0
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    if (value) {
      chunks.push(value)
      total += value.byteLength
    }
  }
  const out = new Uint8Array(total)
  let offset = 0
  for (const c of chunks) {
    out.set(c, offset)
    offset += c.byteLength
  }
  return out
}

function parseZipDirectory(buffer: ArrayBuffer): Map<string, ZipEntryInfo> {
  const view = new DataView(buffer)
  const bytes = new Uint8Array(buffer)
  const len = buffer.byteLength
  let eocdOffset = -1
  const minEocdOffset = Math.max(0, len - 65557)
  for (let i = len - 22; i >= minEocdOffset; i--) {
    if (view.getUint32(i, true) === 0x06054b50) {
      eocdOffset = i
      break
    }
  }
  if (eocdOffset < 0) {
    throw new Error('无法识别的 EPUB (ZIP) 文件结构')
  }

  const cdCount = view.getUint16(eocdOffset + 10, true)
  const cdOffset = view.getUint32(eocdOffset + 16, true)
  const decoder = new TextDecoder('utf-8')
  const entries = new Map<string, ZipEntryInfo>()

  let ptr = cdOffset
  for (let i = 0; i < cdCount && ptr + 46 <= len; i++) {
    if (view.getUint32(ptr, true) !== 0x02014b50) break
    const compressionMethod = view.getUint16(ptr + 10, true)
    const compressedSize = view.getUint32(ptr + 20, true)
    const uncompressedSize = view.getUint32(ptr + 24, true)
    const nameLen = view.getUint16(ptr + 28, true)
    const extraLen = view.getUint16(ptr + 30, true)
    const commentLen = view.getUint16(ptr + 32, true)
    const localHeaderOffset = view.getUint32(ptr + 42, true)
    const filename = decoder.decode(bytes.subarray(ptr + 46, ptr + 46 + nameLen))
    entries.set(filename, {
      filename,
      compressionMethod,
      compressedSize,
      uncompressedSize,
      localHeaderOffset,
    })
    ptr += 46 + nameLen + extraLen + commentLen
  }
  return entries
}

async function readZipEntryBytes(
  buffer: ArrayBuffer,
  entries: Map<string, ZipEntryInfo>,
  path: string
): Promise<Uint8Array | null> {
  const normalized = path.replace(/^\/+/, '')
  const info = entries.get(normalized) || entries.get(decodeURIComponent(normalized))
  if (!info) return null
  const view = new DataView(buffer)
  const bytes = new Uint8Array(buffer)
  const lh = info.localHeaderOffset
  if (lh + 30 > buffer.byteLength || view.getUint32(lh, true) !== 0x04034b50) {
    return null
  }
  const nameLen = view.getUint16(lh + 26, true)
  const extraLen = view.getUint16(lh + 28, true)
  const dataStart = lh + 30 + nameLen + extraLen
  const slice = bytes.subarray(dataStart, dataStart + info.compressedSize)
  if (info.compressionMethod === 0) {
    return slice
  }
  if (info.compressionMethod === 8) {
    return await inflateRawBytes(slice)
  }
  return null
}

async function readZipEntryText(
  buffer: ArrayBuffer,
  entries: Map<string, ZipEntryInfo>,
  path: string
): Promise<string | null> {
  const raw = await readZipEntryBytes(buffer, entries, path)
  if (!raw) return null
  return new TextDecoder('utf-8').decode(raw)
}

function resolveRelativePath(baseFile: string, relativeHref: string): string {
  const cleanHref = relativeHref.split('#')[0].trim()
  if (!cleanHref) return ''
  const baseParts = baseFile.split('/')
  baseParts.pop()
  for (const seg of cleanHref.split('/')) {
    if (!seg || seg === '.') continue
    if (seg === '..') baseParts.pop()
    else baseParts.push(seg)
  }
  return baseParts.join('/')
}

function uint8ToBase64(bytes: Uint8Array): string {
  let binary = ''
  const chunkSize = 0x8000
  for (let i = 0; i < bytes.length; i += chunkSize) {
    const sub = bytes.subarray(i, i + chunkSize)
    binary += String.fromCharCode(...sub)
  }
  return btoa(binary)
}

export async function parseEpubBuffer(buffer: ArrayBuffer, filename: string): Promise<EbookItem> {
  const entries = parseZipDirectory(buffer)
  const containerXml = await readZipEntryText(buffer, entries, 'META-INF/container.xml')
  let opfPath = 'content.opf'
  if (containerXml) {
    const m = containerXml.match(/full-path=["']([^"']+\.opf)["']/i)
    if (m) opfPath = m[1]
  }
  if (!entries.has(opfPath)) {
    for (const k of entries.keys()) {
      if (k.toLowerCase().endsWith('.opf')) {
        opfPath = k
        break
      }
    }
  }

  const opfText = (await readZipEntryText(buffer, entries, opfPath)) || ''
  const parser = new DOMParser()
  const opfDoc = parser.parseFromString(opfText, 'application/xml')

  const getMetaText = (tag: string): string => {
    const el =
      opfDoc.getElementsByTagName(`dc:${tag}`)[0] || opfDoc.getElementsByTagName(tag)[0]
    return el?.textContent?.trim() || ''
  }

  const rawTitle = getMetaText('title') || filename.replace(/\.epub$/i, '')
  const rawAuthor = getMetaText('creator') || 'Unknown Author'
  const rawDesc = decodeHtmlEntities((getMetaText('description') || '').replace(/<[^>]+>/g, ' '))

  const manifest = new Map<string, { href: string; mediaType: string; properties: string }>()
  const itemEls = Array.from(opfDoc.getElementsByTagName('item'))
  for (const el of itemEls) {
    const id = el.getAttribute('id') || ''
    const href = el.getAttribute('href') || ''
    const mediaType = el.getAttribute('media-type') || ''
    const properties = el.getAttribute('properties') || ''
    if (id && href) {
      manifest.set(id, {
        href: resolveRelativePath(opfPath, href),
        mediaType,
        properties,
      })
    }
  }

  let coverDataUrl = ''
  let coverHref = ''
  for (const info of manifest.values()) {
    if (
      info.properties.includes('cover-image') ||
      /cover\.(?:jpe?g|png|webp)$/i.test(info.href)
    ) {
      coverHref = info.href
      break
    }
  }
  if (coverHref) {
    const coverBytes = await readZipEntryBytes(buffer, entries, coverHref)
    if (coverBytes && coverBytes.byteLength < 1500000) {
      const mime = coverHref.toLowerCase().endsWith('.png') ? 'image/png' : 'image/jpeg'
      coverDataUrl = `data:${mime};base64,${uint8ToBase64(coverBytes)}`
    }
  }

  const spineHrefs: string[] = []
  const itemrefEls = Array.from(opfDoc.getElementsByTagName('itemref'))
  for (const el of itemrefEls) {
    const idref = el.getAttribute('idref') || ''
    const mItem = manifest.get(idref)
    if (mItem && mItem.href) {
      spineHrefs.push(mItem.href)
    }
  }

  const tocEntries: Array<{ section: string; title: string; href: string }> = []
  let ncxPath = ''
  for (const info of manifest.values()) {
    if (info.mediaType.includes('dtbncx') || info.href.toLowerCase().endsWith('.ncx')) {
      ncxPath = info.href
      break
    }
  }
  if (ncxPath) {
    const ncxText = await readZipEntryText(buffer, entries, ncxPath)
    if (ncxText) {
      const ncxDoc = parser.parseFromString(ncxText, 'application/xml')
      const walkNavPoints = (parent: Element, currentSection: string) => {
        const children = Array.from(parent.children).filter(
          (c) => c.localName === 'navPoint' || c.tagName.endsWith('navPoint')
        )
        for (const np of children) {
          const labelEl = Array.from(np.children).find(
            (c) => c.localName === 'navLabel' || c.tagName.endsWith('navLabel')
          )
          const contentEl = Array.from(np.children).find(
            (c) => c.localName === 'content' || c.tagName.endsWith('content')
          )
          const label = labelEl?.textContent?.trim() || ''
          const srcRaw = contentEl?.getAttribute('src') || ''
          const resolvedHref = resolveRelativePath(ncxPath, srcRaw)
          const subPoints = Array.from(np.children).filter(
            (c) => c.localName === 'navPoint' || c.tagName.endsWith('navPoint')
          )
          if (subPoints.length > 0) {
            const nextSec = label && label !== rawTitle ? label : currentSection
            walkNavPoints(np, nextSec)
          } else if (resolvedHref) {
            tocEntries.push({ section: currentSection, title: label, href: resolvedHref })
          }
        }
      }
      const navMap =
        ncxDoc.getElementsByTagName('navMap')[0] ||
        Array.from(ncxDoc.getElementsByTagName('*')).find((el) => el.localName === 'navMap')
      if (navMap) walkNavPoints(navMap, '')
    }
  }

  if (tocEntries.length === 0) {
    for (const info of manifest.values()) {
      if (info.properties.includes('nav')) {
        const navText = await readZipEntryText(buffer, entries, info.href)
        if (navText) {
          const navDoc = parser.parseFromString(navText, 'text/html')
          const links = Array.from(navDoc.querySelectorAll('nav a[href]'))
          for (const a of links) {
            const title = a.textContent?.trim() || ''
            const href = resolveRelativePath(info.href, a.getAttribute('href') || '')
            if (title && href) {
              tocEntries.push({ section: '', title, href })
            }
          }
        }
        break
      }
    }
  }

  const chapters: EbookChapter[] = []
  const processedHrefs = new Set<string>()
  const tocHrefSet = new Set(tocEntries.map((t) => t.href).filter(Boolean))

  const candidateList =
    tocEntries.length > 0
      ? tocEntries
      : spineHrefs.map((href, i) => ({ section: '', title: `Chapter ${i + 1}`, href }))

  for (const item of candidateList) {
    if (!item.href || processedHrefs.has(item.href)) continue
    processedHrefs.add(item.href)
    const htmlContent = await readZipEntryText(buffer, entries, item.href)
    if (!htmlContent) continue

    const extracted = extractParagraphsFromHtml(htmlContent, item.title, item.section)
    const mergedParagraphs = [...extracted.paragraphs]
    let mergedWordCount = extracted.wordCount

    // Merge continuation spine documents (e.g. Calibre/novel multi-part chapter splits not in TOC)
    const spineIdx = spineHrefs.indexOf(item.href)
    if (spineIdx >= 0 && tocEntries.length > 0) {
      for (let nextIdx = spineIdx + 1; nextIdx < spineHrefs.length; nextIdx++) {
        const nextHref = spineHrefs[nextIdx]
        if (tocHrefSet.has(nextHref) || processedHrefs.has(nextHref)) break
        processedHrefs.add(nextHref)
        const nextHtml = await readZipEntryText(buffer, entries, nextHref)
        if (!nextHtml) continue
        const nextExt = extractParagraphsFromHtml(nextHtml, '', '')
        // Skip short Calibre section divider / TOC pages
        if (nextExt.wordCount < 45) continue
        for (const p of nextExt.paragraphs) {
          mergedParagraphs.push({
            index: mergedParagraphs.length + 1,
            text: p.text,
            sentences: p.sentences,
          })
        }
        mergedWordCount += nextExt.wordCount
      }
    }

    if (mergedWordCount < 30) continue

    chapters.push({
      id: `ch-${chapters.length + 1}`,
      index: chapters.length + 1,
      title: extracted.detectedTitle,
      section: extracted.detectedSection || undefined,
      subtitle: extracted.subtitle || undefined,
      byline: extracted.byline || undefined,
      href: item.href,
      wordCount: mergedWordCount,
      paragraphs: mergedParagraphs,
    })
  }

  if (chapters.length <= 1 && spineHrefs.length > 1) {
    chapters.length = 0
    for (const href of spineHrefs) {
      const htmlContent = await readZipEntryText(buffer, entries, href)
      if (!htmlContent) continue
      const extracted = extractParagraphsFromHtml(htmlContent, '', '')
      if (extracted.wordCount < 45) continue
      chapters.push({
        id: `ch-${chapters.length + 1}`,
        index: chapters.length + 1,
        title:
          extracted.detectedTitle !== 'Chapter'
            ? extracted.detectedTitle
            : `Chapter ${chapters.length + 1}`,
        byline: extracted.byline || undefined,
        subtitle: extracted.subtitle || undefined,
        href,
        wordCount: extracted.wordCount,
        paragraphs: extracted.paragraphs,
      })
    }
  }

  if (chapters.length === 0) {
    throw new Error('未能在该 EPUB 中提取到有效的英文正文段落')
  }

  const totalWords = chapters.reduce((acc, c) => acc + c.wordCount, 0)
  const bookId = `epub-${rawTitle
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 40)}-${Date.now().toString(36)}`

  return {
    id: bookId,
    title: rawTitle,
    author: rawAuthor === 'calibre' ? 'Calibre Edition' : rawAuthor,
    description: rawDesc,
    format: 'epub',
    coverDataUrl: coverDataUrl || undefined,
    importedAt: Date.now(),
    totalWords,
    lastChapterId: chapters[0].id,
    lastParaIndex: 1,
    chapters,
  }
}

function decompressPalmDocLz77(input: Uint8Array): Uint8Array {
  const out: number[] = []
  let i = 0
  const len = input.length
  while (i < len) {
    const b = input[i++]
    if (b === 0x00) {
      out.push(0x00)
    } else if (b >= 0x01 && b <= 0x08) {
      for (let k = 0; k < b && i < len; k++) {
        out.push(input[i++])
      }
    } else if (b >= 0x09 && b <= 0x7f) {
      out.push(b)
    } else if (b >= 0x80 && b <= 0xbf) {
      if (i >= len) break
      const b2 = input[i++]
      const pair = (b << 8) | b2
      const distance = (pair >> 3) & 0x07ff
      const copyLen = (pair & 0x07) + 3
      const start = out.length - distance
      for (let k = 0; k < copyLen; k++) {
        const idx = start + k
        out.push(idx >= 0 && idx < out.length ? out[idx] : 0x20)
      }
    } else {
      out.push(0x20)
      out.push(b ^ 0x80)
    }
  }
  return new Uint8Array(out)
}

function trimMobiRecordTrailingData(record: Uint8Array, extraDataFlags: number): Uint8Array {
  let end = record.length
  for (let bit = 15; bit >= 1; bit--) {
    if ((extraDataFlags & (1 << bit)) !== 0) {
      let size = 0
      let shift = 0
      for (let pos = end - 1; pos >= Math.max(0, end - 4); pos--) {
        const b = record[pos]
        size |= (b & 0x7f) << shift
        shift += 7
        if ((b & 0x80) !== 0) break
      }
      if (size > 0 && size <= end) {
        end -= size
      }
    }
  }
  if ((extraDataFlags & 1) !== 0 && end > 0) {
    const pad = (record[end - 1] & 0x03) + 1
    if (pad <= end) end -= pad
  }
  return record.subarray(0, end)
}

export async function parseMobiBuffer(
  buffer: ArrayBuffer,
  filename: string,
  format: EbookFormat = 'mobi'
): Promise<EbookItem> {
  const view = new DataView(buffer)
  const bytes = new Uint8Array(buffer)
  if (buffer.byteLength < 86) {
    throw new Error('MOBI/AZW 文件过小或已损坏')
  }

  const asciiDecoder = new TextDecoder('latin1')
  const magic = asciiDecoder.decode(bytes.subarray(60, 68))
  if (magic !== 'BOOKMOBI' && magic !== 'TEXtREAd') {
    throw new Error('不是有效的 MOBI / AZW / PalmDOC 电子书文件')
  }

  const numRecords = view.getUint16(76, false)
  const recordOffsets: number[] = []
  for (let i = 0; i < numRecords; i++) {
    const recOffset = view.getUint32(78 + i * 8, false)
    recordOffsets.push(recOffset)
  }

  const getRecord = (idx: number): Uint8Array => {
    const start = recordOffsets[idx]
    const end = idx + 1 < recordOffsets.length ? recordOffsets[idx + 1] : buffer.byteLength
    if (start >= buffer.byteLength || end <= start) return new Uint8Array(0)
    return bytes.subarray(start, end)
  }

  const rec0 = getRecord(0)
  const rec0View = new DataView(rec0.buffer, rec0.byteOffset, rec0.byteLength)
  const compression = rec0View.getUint16(0, false)
  const textRecordCount = rec0View.getUint16(8, false)

  if (compression === 17480) {
    throw new Error('该 MOBI 使用了专有 Huff/CDIC 高阶字典压缩，建议先用 Calibre 转为 EPUB 或标准 MOBI')
  }
  if (textRecordCount === 0) {
    throw new Error('该文件是纯 KF8/AZW3 骨骼容器（不含 MOBI 正文层），请用 Calibre 转为 EPUB 后导入')
  }

  let encoding = 'utf-8'
  let title = filename.replace(/\.(?:mobi|azw3?|prc)$/i, '')
  let author = 'Unknown Author'
  let description = ''
  let extraDataFlags = 0

  if (rec0.byteLength >= 132 && asciiDecoder.decode(rec0.subarray(16, 20)) === 'MOBI') {
    const mobiHeaderLen = rec0View.getUint32(20, false)
    const textEncodingCode = rec0View.getUint32(28, false)
    if (textEncodingCode === 1252) encoding = 'windows-1252'
    const fullNameOffset = rec0View.getUint32(84, false)
    const fullNameLen = rec0View.getUint32(88, false)
    if (fullNameOffset + fullNameLen <= rec0.byteLength && fullNameLen > 0) {
      title = new TextDecoder(encoding).decode(
        rec0.subarray(fullNameOffset, fullNameOffset + fullNameLen)
      )
    }
    if (mobiHeaderLen >= 228 && 16 + 228 <= rec0.byteLength) {
      extraDataFlags = rec0View.getUint16(242, false)
    }
    const exthFlags = rec0View.getUint32(128, false)
    const exthStart = 16 + mobiHeaderLen
    if (
      (exthFlags & 0x40) !== 0 &&
      exthStart + 12 <= rec0.byteLength &&
      asciiDecoder.decode(rec0.subarray(exthStart, exthStart + 4)) === 'EXTH'
    ) {
      const exthCount = rec0View.getUint32(exthStart + 8, false)
      let ptr = exthStart + 12
      const textDec = new TextDecoder(encoding)
      for (let i = 0; i < exthCount && ptr + 8 <= rec0.byteLength; i++) {
        const recType = rec0View.getUint32(ptr, false)
        const recLen = rec0View.getUint32(ptr + 4, false)
        if (recLen < 8 || ptr + recLen > rec0.byteLength) break
        const payload = rec0.subarray(ptr + 8, ptr + recLen)
        if (recType === 100) author = textDec.decode(payload).trim() || author
        else if (recType === 103) description = decodeHtmlEntities(textDec.decode(payload))
        else if (recType === 503) title = textDec.decode(payload).trim() || title
        ptr += recLen
      }
    }
  }

  const chunks: Uint8Array[] = []
  let totalBytes = 0
  const maxRec = Math.min(textRecordCount, numRecords - 1)
  for (let r = 1; r <= maxRec; r++) {
    const rawRec = trimMobiRecordTrailingData(getRecord(r), extraDataFlags)
    const decompressed =
      compression === 2 ? decompressPalmDocLz77(rawRec) : rawRec
    chunks.push(decompressed)
    totalBytes += decompressed.byteLength
  }

  const fullBytes = new Uint8Array(totalBytes)
  let offset = 0
  for (const c of chunks) {
    fullBytes.set(c, offset)
    offset += c.byteLength
  }

  const htmlString = new TextDecoder(encoding).decode(fullBytes)
  const rawPages = htmlString.split(
    /<mbp:pagebreak\s*\/?>|<div[^>]+class=["'][^"']*mbp_pagebreak[^"']*["'][^>]*>\s*<\/div>|(?=<h[12]\b)/gi
  )

  const chapters: EbookChapter[] = []
  for (const pageHtml of rawPages) {
    const extracted = extractParagraphsFromHtml(pageHtml, '', '')
    if (extracted.wordCount < 35) continue
    chapters.push({
      id: `ch-${chapters.length + 1}`,
      index: chapters.length + 1,
      title:
        extracted.detectedTitle !== 'Chapter'
          ? extracted.detectedTitle
          : `Chapter ${chapters.length + 1}`,
      byline: extracted.byline || undefined,
      subtitle: extracted.subtitle || undefined,
      wordCount: extracted.wordCount,
      paragraphs: extracted.paragraphs,
    })
  }

  if (chapters.length === 0) {
    const fallback = extractParagraphsFromHtml(htmlString, title, '')
    if (fallback.paragraphs.length === 0) {
      throw new Error('未能在该 MOBI 文件中解析出有效正文内容')
    }
    chapters.push({
      id: 'ch-1',
      index: 1,
      title,
      wordCount: fallback.wordCount,
      paragraphs: fallback.paragraphs,
    })
  }

  const totalWords = chapters.reduce((acc, c) => acc + c.wordCount, 0)
  const bookId = `${format}-${title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 40)}-${Date.now().toString(36)}`

  return {
    id: bookId,
    title,
    author,
    description: description || undefined,
    format,
    importedAt: Date.now(),
    totalWords,
    lastChapterId: chapters[0].id,
    lastParaIndex: 1,
    chapters,
  }
}

export function parsePlainTextBook(content: string, filename: string): EbookItem {
  const title = filename.replace(/\.(?:txt|md)$/i, '')
  const normalized = content.replace(/\r\n?/g, '\n').trim()
  const sections = normalized.split(/\n(?=(?:CHAPTER|Chapter|SECTION|Part)\s+[0-9IVXLC]+|^#{1,2}\s+\S)/m)
  const chapters: EbookChapter[] = []

  for (const sec of sections) {
    const rawParas = sec
      .split(/\n\s*\n/)
      .map((p) => p.replace(/\s+/g, ' ').trim())
      .filter(Boolean)
    if (rawParas.length === 0) continue
    let chapTitle = `Section ${chapters.length + 1}`
    let bodyParas = rawParas
    if (rawParas[0].length <= 100 && rawParas.length > 1) {
      chapTitle = rawParas[0].replace(/^#{1,3}\s*/, '')
      bodyParas = rawParas.slice(1)
    }
    let wordCount = 0
    const paragraphs: EbookParagraph[] = bodyParas.map((text, idx) => {
      wordCount += countEnglishWords(text)
      return {
        index: idx + 1,
        text,
        sentences: splitEnglishSentences(text),
      }
    })
    if (wordCount < 15) continue
    chapters.push({
      id: `ch-${chapters.length + 1}`,
      index: chapters.length + 1,
      title: chapTitle,
      wordCount,
      paragraphs,
    })
  }

  if (chapters.length === 0) {
    throw new Error('文本内容为空或过短')
  }

  return {
    id: `txt-${Date.now().toString(36)}`,
    title,
    author: 'Imported Text',
    format: 'txt',
    importedAt: Date.now(),
    totalWords: chapters.reduce((a, c) => a + c.wordCount, 0),
    lastChapterId: chapters[0].id,
    lastParaIndex: 1,
    chapters,
  }
}

export async function parseEbookFile(file: File): Promise<EbookItem> {
  const lower = file.name.toLowerCase()
  if (lower.endsWith('.txt') || lower.endsWith('.md')) {
    const text = await file.text()
    return parsePlainTextBook(text, file.name)
  }
  const buffer = await file.arrayBuffer()
  const bytes = new Uint8Array(buffer)
  if (bytes.length >= 4 && bytes[0] === 0x50 && bytes[1] === 0x4b) {
    return await parseEpubBuffer(buffer, file.name)
  }
  if (
    lower.endsWith('.mobi') ||
    lower.endsWith('.azw') ||
    lower.endsWith('.azw3') ||
    lower.endsWith('.prc') ||
    (bytes.length >= 68 &&
      new TextDecoder('latin1').decode(bytes.subarray(60, 68)) === 'BOOKMOBI')
  ) {
    const fmt: EbookFormat = lower.endsWith('.azw3') ? 'azw3' : 'mobi'
    return await parseMobiBuffer(buffer, file.name, fmt)
  }
  return await parseEpubBuffer(buffer, file.name)
}

function normalizeForMatch(text: string): string {
  return text
    .toLowerCase()
    .replace(/[‘’`´]/g, "'")
    .replace(/[“”«»]/g, '"')
    .replace(/[—–―]/g, ' ')
    .replace(/[^a-z0-9'\s]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

function extractContentTokens(text: string): string[] {
  const stop = new Set([
    'the',
    'and',
    'that',
    'this',
    'with',
    'from',
    'have',
    'had',
    'has',
    'were',
    'was',
    'are',
    'for',
    'not',
    'but',
    'his',
    'her',
    'they',
    'their',
    'been',
    'which',
    'when',
    'who',
    'what',
    'into',
    'more',
    'than',
    'some',
    'would',
    'could',
    'about',
    'there',
  ])
  return normalizeForMatch(text)
    .split(' ')
    .filter((w) => w.length >= 3 && !stop.has(w))
}

export function findSentenceContextInBooks(
  rawSentence: string,
  books: EbookItem[],
  preferredBookId?: string,
  preferredChapterId?: string
): SentenceContextMatch | null {
  const cleanQuery = rawSentence.replace(/\s+/g, ' ').trim()
  if (cleanQuery.length < 6 || books.length === 0) return null
  const normQuery = normalizeForMatch(cleanQuery)
  if (!normQuery) return null

  const orderedBooks = [...books].sort((a, b) => {
    if (a.id === preferredBookId) return -1
    if (b.id === preferredBookId) return 1
    return b.importedAt - a.importedAt
  })

  for (const book of orderedBooks) {
    const orderedChapters = [...book.chapters].sort((a, b) => {
      if (a.id === preferredChapterId) return -1
      if (b.id === preferredChapterId) return 1
      return a.index - b.index
    })

    for (const chapter of orderedChapters) {
      for (let i = 0; i < chapter.paragraphs.length; i++) {
        const para = chapter.paragraphs[i]
        const normPara = normalizeForMatch(para.text)
        if (
          normPara.includes(normQuery) ||
          (normQuery.length >= 30 && normPara.includes(normQuery.slice(0, 36)))
        ) {
          const matchedSent =
            para.sentences.find((s) => {
              const ns = normalizeForMatch(s)
              return ns.includes(normQuery) || normQuery.includes(ns)
            }) || cleanQuery
          return {
            bookId: book.id,
            bookTitle: book.title,
            bookAuthor: book.author,
            chapterId: chapter.id,
            chapterIndex: chapter.index,
            chapterTitle: chapter.title,
            chapterSection: chapter.section,
            chapterByline: chapter.byline,
            paraIndex: para.index,
            targetSentence: matchedSent,
            currentParagraph: para.text,
            previousParagraph: i > 0 ? chapter.paragraphs[i - 1].text : '',
            nextParagraph:
              i + 1 < chapter.paragraphs.length ? chapter.paragraphs[i + 1].text : '',
            confidence: 'exact',
          }
        }
      }
    }
  }

  const queryTokens = extractContentTokens(cleanQuery)
  if (queryTokens.length < 3) return null

  let bestScore = 0
  let bestMatch: SentenceContextMatch | null = null

  for (const book of orderedBooks) {
    for (const chapter of book.chapters) {
      for (let i = 0; i < chapter.paragraphs.length; i++) {
        const para = chapter.paragraphs[i]
        const paraTokenSet = new Set(extractContentTokens(para.text))
        let hits = 0
        for (const qt of queryTokens) {
          if (paraTokenSet.has(qt)) hits++
        }
        const ratio = hits / queryTokens.length
        if (ratio > bestScore && ratio >= 0.62 && hits >= 3) {
          bestScore = ratio
          bestMatch = {
            bookId: book.id,
            bookTitle: book.title,
            bookAuthor: book.author,
            chapterId: chapter.id,
            chapterIndex: chapter.index,
            chapterTitle: chapter.title,
            chapterSection: chapter.section,
            chapterByline: chapter.byline,
            paraIndex: para.index,
            targetSentence: cleanQuery,
            currentParagraph: para.text,
            previousParagraph: i > 0 ? chapter.paragraphs[i - 1].text : '',
            nextParagraph:
              i + 1 < chapter.paragraphs.length ? chapter.paragraphs[i + 1].text : '',
            confidence: 'fuzzy',
          }
        }
      }
    }
  }

  return bestMatch
}

export function findWordOccurrencesInBooks(
  term: string,
  books: EbookItem[],
  preferredBookId?: string,
  limit = 10
): WordOccurrence[] {
  const clean = term.trim().toLowerCase().replace(/[^a-z'-]/g, '')
  if (!clean || clean.length < 2) return []

  const variants = new Set<string>([clean])
  if (clean.length >= 3) {
    variants.add(`${clean}s`)
    variants.add(`${clean}es`)
    variants.add(`${clean}ed`)
    variants.add(`${clean}d`)
    variants.add(`${clean}ing`)
    if (clean.endsWith('y')) {
      variants.add(`${clean.slice(0, -1)}ies`)
      variants.add(`${clean.slice(0, -1)}ied`)
    }
    if (clean.endsWith('e')) {
      variants.add(`${clean.slice(0, -1)}ing`)
    }
  }

  const escaped = Array.from(variants).map((v) => v.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
  const wordRegex = new RegExp(`\\b(${escaped.join('|')})\\b`, 'i')

  const orderedBooks = [...books].sort((a, b) => {
    if (a.id === preferredBookId) return -1
    if (b.id === preferredBookId) return 1
    return b.importedAt - a.importedAt
  })

  const results: WordOccurrence[] = []
  for (const book of orderedBooks) {
    for (const chapter of book.chapters) {
      for (const para of chapter.paragraphs) {
        if (!wordRegex.test(para.text)) continue
        const matchedSentence =
          para.sentences.find((s) => wordRegex.test(s)) || para.text
        const m = matchedSentence.match(wordRegex)
        results.push({
          bookId: book.id,
          bookTitle: book.title,
          chapterId: chapter.id,
          chapterTitle: chapter.title,
          paraIndex: para.index,
          sentence: matchedSentence,
          paragraphText: para.text,
          matchedForm: m ? m[1] : clean,
        })
        if (results.length >= limit) return results
      }
    }
  }
  return results
}
