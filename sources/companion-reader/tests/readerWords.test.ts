import { test } from 'node:test'
import assert from 'node:assert/strict'
import { splitReaderWords, extractReaderWords } from '../src/services/readerWords.ts'

test('extracts contractions and hyphenated words without changing the reading text', () => {
  const text = "“I can't,” she said.\nIt's a well-known, reader’s choice; non‑fiction too."
  assert.equal(splitReaderWords(text).join(''), text)
  assert.deepEqual(extractReaderWords(text), [
    'i', "can't", 'she', 'said', "it's", 'a', 'well-known', 'reader’s', 'choice', 'non‑fiction', 'too',
  ])
})

test('retains short words and words beyond twenty; only deduplicates case', () => {
  const words = 'I a to be of in on at by an as or if it we he me my no so do go up us'.split(' ')
  assert.deepEqual(extractReaderWords(words.join(' ') + ' I TO'), words.map(w => w.toLowerCase()))
  assert.deepEqual(extractReaderWords('123，。—'), [])
})
