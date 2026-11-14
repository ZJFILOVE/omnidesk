import { splitReaderWords } from '../services/readerWords'

export function ReaderWords({ text }: { text: string }) {
  return splitReaderWords(text).map((part, index) => index % 2 === 1
    ? <button type="button" className="reader-word" data-word={part}
        aria-label={`查询单词 ${part}`} key={index}>{part}</button>
    : part)
}
