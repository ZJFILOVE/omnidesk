// Keep punctuation and whitespace intact; contractions and hyphenated words stay whole.
export function splitReaderWords(text: string): string[] {
  return text.split(/([A-Za-z]+(?:['’\-‑][A-Za-z]+)*)/g)
}

export function extractReaderWords(text: string): string[] {
  return Array.from(new Set(splitReaderWords(text)
    .filter((_, index) => index % 2 === 1).map((word) => word.toLowerCase())))
}
