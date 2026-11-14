import React from 'react'

interface MarkdownContentProps {
  content: string
  className?: string
}

function renderInlineMarkdown(text: string): React.ReactNode[] {
  if (!text) return []
  // Match `code`, **bold**, __bold__, *italic*, or inline 【section】
  const tokenRegex = /(`[^`\n]+`|\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\n]+\*|【[^】\n]+】)/g
  const parts = text.split(tokenRegex)
  return parts.map((part, idx) => {
    if (!part) return null
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      return (
        <code
          key={idx}
          className="px-1.5 py-0.5 rounded-md bg-[#ede2cf] text-[#6e4214] font-mono text-[0.9em]"
        >
          {part.slice(1, -1)}
        </code>
      )
    }
    if (
      ((part.startsWith('**') && part.endsWith('**')) ||
        (part.startsWith('__') && part.endsWith('__'))) &&
      part.length > 4
    ) {
      return (
        <strong
          key={idx}
          className="font-bold text-[#59340e] bg-[#f2dfc1]/65 px-1 py-0.2 rounded"
        >
          {part.slice(2, -2)}
        </strong>
      )
    }
    if (part.startsWith('*') && part.endsWith('*') && part.length > 2) {
      return (
        <em key={idx} className="italic font-serif text-[#4a3b28] font-medium">
          {part.slice(1, -1)}
        </em>
      )
    }
    if (part.startsWith('【') && part.endsWith('】') && part.length > 2) {
      return (
        <span
          key={idx}
          className="inline-flex items-center px-2 py-0.5 mr-1 rounded-lg bg-[#efe2cc] text-[#7a4b19] text-xs font-bold border border-[#dfc6a4]"
        >
          {part.slice(1, -1)}
        </span>
      )
    }
    return <React.Fragment key={idx}>{part}</React.Fragment>
  })
}

export const MarkdownContent: React.FC<MarkdownContentProps> = ({ content, className = '' }) => {
  if (!content) return null

  // Strip outer markdown fence if the whole response is wrapped
  const cleaned = content
    .trim()
    .replace(/^```(?:markdown|md|text)?\s*\n([\s\S]*?)\n```$/i, '$1')
    .trim()

  const rawLines = cleaned.split(/\r?\n/)
  const blocks: React.ReactNode[] = []
  let listItems: { ordered: boolean; marker: string; text: string }[] = []
  let keyCounter = 0

  const flushList = () => {
    if (listItems.length === 0) return
    const items = listItems
    listItems = []
    blocks.push(
      <ul key={`list-${keyCounter++}`} className="space-y-1.5 pl-1 my-1.5">
        {items.map((item, idx) => (
          <li key={idx} className="flex items-start gap-2 leading-relaxed text-[#2e2a24]">
            <span className="text-[#996021] font-bold shrink-0 mt-0.5 select-none text-xs sm:text-sm">
              {item.ordered ? item.marker : '•'}
            </span>
            <span className="flex-1">{renderInlineMarkdown(item.text)}</span>
          </li>
        ))}
      </ul>
    )
  }

  for (let i = 0; i < rawLines.length; i++) {
    const line = rawLines[i].trim()
    if (!line) {
      flushList()
      continue
    }

    // Horizontal rule
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line)) {
      flushList()
      blocks.push(<hr key={`hr-${keyCounter++}`} className="border-[#e2d6c1] my-2" />)
      continue
    }

    // Bracket section header at start of line: e.g. 【词根词缀拆解】 or ### 【词根词缀拆解】
    const bracketMatch = line.match(/^(?:#{1,4}\s*)?(?:\*\*)?【([^】]+)】(?:\*\*)?\s*(.*)$/)
    if (bracketMatch) {
      flushList()
      const title = bracketMatch[1].trim()
      const rest = bracketMatch[2].trim()
      blocks.push(
        <div key={`sec-${keyCounter++}`} className="pt-1.5 first:pt-0 space-y-1.5">
          <div className="inline-flex items-center px-2.5 py-0.5 rounded-lg bg-[#efe2cc] text-[#7a4b19] text-xs font-bold border border-[#dfc6a4]">
            {title}
          </div>
          {rest && (
            <p className="leading-relaxed text-[#2e2a24]">{renderInlineMarkdown(rest)}</p>
          )}
        </div>
      )
      continue
    }

    // Markdown ATX Heading: # / ## / ### / ####
    const headingMatch = line.match(/^(#{1,4})\s+(.+)$/)
    if (headingMatch) {
      flushList()
      const headingText = headingMatch[2].replace(/^\*\*|\*\*$/g, '').trim()
      blocks.push(
        <div key={`h-${keyCounter++}`} className="pt-1.5 first:pt-0">
          <h4 className="inline-flex items-center px-2.5 py-0.5 rounded-lg bg-[#efe2cc] text-[#7a4b19] text-xs font-bold border border-[#dfc6a4]">
            {renderInlineMarkdown(headingText)}
          </h4>
        </div>
      )
      continue
    }

    // Unordered list item: - / * / •
    const ulMatch = line.match(/^[-*•]\s+(.+)$/)
    if (ulMatch) {
      listItems.push({ ordered: false, marker: '•', text: ulMatch[1].trim() })
      continue
    }

    // Ordered list item: 1. / 1)
    const olMatch = line.match(/^(\d+[.)])\s+(.+)$/)
    if (olMatch) {
      listItems.push({ ordered: true, marker: olMatch[1], text: olMatch[2].trim() })
      continue
    }

    // Blockquote: >
    const quoteMatch = line.match(/^>\s*(.+)$/)
    if (quoteMatch) {
      flushList()
      blocks.push(
        <blockquote
          key={`q-${keyCounter++}`}
          className="border-l-2 border-[#c89d66] bg-[#f4ece0]/75 pl-3 py-1.5 rounded-r-xl text-[#4a3b28] leading-relaxed"
        >
          {renderInlineMarkdown(quoteMatch[1].trim())}
        </blockquote>
      )
      continue
    }

    flushList()
    blocks.push(
      <p key={`p-${keyCounter++}`} className="leading-relaxed text-[#2e2a24]">
        {renderInlineMarkdown(line)}
      </p>
    )
  }

  flushList()

  return <div className={`space-y-2 text-sm text-[#2e2a24] ${className}`}>{blocks}</div>
}
