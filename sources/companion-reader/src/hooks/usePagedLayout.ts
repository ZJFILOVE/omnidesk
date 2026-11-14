import { useCallback, useLayoutEffect, useRef, useState } from 'react'

export interface ReaderAnchor {
  paraIndex: number
  sentenceIndex: number
}

export type AnchorTarget = ReaderAnchor | 'start' | 'end'

export const PAGE_GAP = 56
const SPREAD_MIN_WIDTH = 920

function leftOf(el: HTMLElement, originLeft: number) {
  const rect = el.getClientRects()[0] || el.getBoundingClientRect()
  return rect.left - originLeft
}

export function usePagedLayout(
  enabled: boolean,
  layoutKey: string,
  resolveTarget: () => AnchorTarget
) {
  const viewportRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const endMarkerRef = useRef<HTMLSpanElement>(null)
  const resolveRef = useRef(resolveTarget)
  const [size, setSize] = useState({ width: 0, height: 0 })
  const [page, setPage] = useState(0)
  const [pageCount, setPageCount] = useState(1)
  const [animate, setAnimate] = useState(false)

  const spread = size.width >= SPREAD_MIN_WIDTH ? 2 : 1
  const columnWidth = Math.max(1, Math.floor((size.width - (spread - 1) * PAGE_GAP) / spread))
  const contentWidth = spread * columnWidth + (spread - 1) * PAGE_GAP
  const step = contentWidth + PAGE_GAP

  useLayoutEffect(() => {
    resolveRef.current = resolveTarget
  })

  const measureSize = useCallback(() => {
    const viewport = viewportRef.current
    if (!viewport) return
    const height = Math.max(1, Math.floor(viewport.clientHeight))
    const width = Math.floor(viewport.clientWidth)
    setSize((curr) => (curr.width === width && curr.height === height ? curr : { width, height }))
  }, [])

  useLayoutEffect(() => {
    if (!enabled) return
    measureSize()
    const observer = new ResizeObserver(() => measureSize())
    if (viewportRef.current) observer.observe(viewportRef.current)
    window.addEventListener('resize', measureSize)
    return () => {
      observer.disconnect()
      window.removeEventListener('resize', measureSize)
    }
  }, [enabled, measureSize])

  const pageOf = useCallback(
    (el: HTMLElement) => {
      const content = contentRef.current
      if (!content) return 0
      return Math.max(0, Math.floor((leftOf(el, content.getBoundingClientRect().left) + 1) / step))
    },
    [step]
  )

  const locate = useCallback(
    (target: AnchorTarget, count: number) => {
      const content = contentRef.current
      if (!content || target === 'start') return 0
      if (target === 'end') return count - 1
      const el =
        content.querySelector<HTMLElement>(
          `[data-p="${target.paraIndex}"][data-s="${target.sentenceIndex}"]`
        ) || content.querySelector<HTMLElement>(`[data-p="${target.paraIndex}"]`)
      return el ? Math.min(count - 1, pageOf(el)) : 0
    },
    [pageOf]
  )

  const readAnchor = useCallback(
    (pageIndex: number): ReaderAnchor | null => {
      const content = contentRef.current
      if (!content) return null
      const spans = content.querySelectorAll<HTMLElement>('[data-s]')
      if (!spans.length) return null
      const origin = content.getBoundingClientRect().left
      const start = pageIndex * step - 1
      let chosen = spans[0]
      for (const span of spans) {
        const left = leftOf(span, origin)
        if (left >= start) {
          if (left < start + step) chosen = span
          break
        }
        chosen = span
      }
      return { paraIndex: Number(chosen.dataset.p), sentenceIndex: Number(chosen.dataset.s) }
    },
    [step]
  )

  useLayoutEffect(() => {
    const content = contentRef.current
    if (!enabled || !size.width || !content) return
    const marker = endMarkerRef.current
    const columns = marker
      ? Math.floor((leftOf(marker, content.getBoundingClientRect().left) + 1) / (columnWidth + PAGE_GAP)) + 1
      : Math.round((content.scrollWidth + PAGE_GAP) / (columnWidth + PAGE_GAP))
    const count = Math.max(1, Math.ceil(columns / spread))
    setAnimate(false)
    setPageCount(count)
    setPage(locate(resolveRef.current(), count))
  }, [enabled, layoutKey, size, columnWidth, spread, locate])

  const goTo = useCallback(
    (next: number, withAnimation = true) => {
      setAnimate(withAnimation)
      setPage(Math.min(pageCount - 1, Math.max(0, next)))
    },
    [pageCount]
  )

  return {
    viewportRef,
    contentRef,
    endMarkerRef,
    size,
    spread,
    columnWidth,
    contentWidth,
    step,
    page,
    pageCount,
    animate,
    goTo,
    pageOf,
    readAnchor,
  }
}
