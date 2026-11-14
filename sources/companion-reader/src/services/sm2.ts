import type { MasteryStatus, UserWordRecord } from '../types/ebook'

export type SM2Rating = 1 | 2 | 3 | 4 // 1: Again, 2: Hard, 3: Good, 4: Easy

export interface SM2Result {
  repetitions: number
  interval: number // in days
  easeFactor: number
  dueDate: number
  status: MasteryStatus
}

export function calculateSM2(
  currentRecord: Partial<UserWordRecord> | null,
  rating: SM2Rating,
  now: number = Date.now()
): SM2Result {
  const repetitions = currentRecord?.repetitions ?? 0
  const prevInterval = currentRecord?.interval ?? 0
  const prevEaseFactor = currentRecord?.easeFactor ?? 2.5

  let nextRepetitions = repetitions
  let nextInterval = 1
  let nextEaseFactor = prevEaseFactor
  let nextStatus: MasteryStatus = 'learning'

  if (rating === 1) {
    // Again / 重来: reset repetitions, interval = 1 day, decrease ease factor
    nextRepetitions = 0
    nextInterval = 1
    nextEaseFactor = Math.max(1.3, prevEaseFactor - 0.2)
    nextStatus = 'learning'
  } else if (rating === 2) {
    // Hard / 模糊: maintain repetitions or slight advance, shorter interval
    nextRepetitions = Math.max(1, repetitions)
    nextInterval = Math.max(1, Math.round(prevInterval > 0 ? prevInterval * 1.2 : 1))
    nextEaseFactor = Math.max(1.3, prevEaseFactor - 0.15)
    nextStatus = 'review'
  } else if (rating === 3) {
    // Good / 认识: standard SM-2 intervals
    if (repetitions === 0) {
      nextInterval = 1
    } else if (repetitions === 1) {
      nextInterval = 3
    } else {
      nextInterval = Math.max(2, Math.round(prevInterval * prevEaseFactor))
    }
    nextRepetitions = repetitions + 1
    nextStatus = nextRepetitions >= 4 ? 'mastered' : 'review'
  } else if (rating === 4) {
    // Easy / 熟练: accelerated intervals, increase ease factor
    if (repetitions === 0) {
      nextInterval = 2
    } else if (repetitions === 1) {
      nextInterval = 6
    } else {
      nextInterval = Math.max(3, Math.round(prevInterval * prevEaseFactor * 1.3))
    }
    nextRepetitions = repetitions + 1
    nextEaseFactor = Math.min(3.0, prevEaseFactor + 0.15)
    nextStatus = nextRepetitions >= 3 ? 'mastered' : 'review'
  }

  const dueDate = now + nextInterval * 24 * 60 * 60 * 1000

  return {
    repetitions: nextRepetitions,
    interval: nextInterval,
    easeFactor: Math.round(nextEaseFactor * 100) / 100,
    dueDate,
    status: nextStatus,
  }
}
