type AudioAccent = 'us' | 'uk'

class AudioService {
  private currentAudio: HTMLAudioElement | null = null
  private listeners: Set<(isPlaying: boolean, term: string) => void> = new Set()
  private activeTerm: string = ''
  private pollTimer: number | null = null
  private activeResolve: (() => void) | null = null

  public subscribe(fn: (isPlaying: boolean, term: string) => void) {
    this.listeners.add(fn)
    return () => {
      this.listeners.delete(fn)
    }
  }

  private notify(isPlaying: boolean, term: string) {
    this.activeTerm = isPlaying ? term : ''
    this.listeners.forEach((fn) => fn(isPlaying, term))
  }

  public getActiveTerm(): string {
    return this.activeTerm
  }

  public getYoudaoUrl(text: string, accent: AudioAccent = 'us'): string {
    const clean = text.trim()
    // type=1 for UK, type=2 for US
    const type = accent === 'uk' ? '1' : '2'
    return `https://dict.youdao.com/dictvoice?audio=${encodeURIComponent(clean)}&type=${type}`
  }

  public stop(): void {
    if (this.pollTimer !== null && typeof window !== 'undefined') {
      window.clearInterval(this.pollTimer)
      this.pollTimer = null
    }
    if (this.currentAudio) {
      try {
        this.currentAudio.pause()
        this.currentAudio.currentTime = 0
      } catch {
        // ignore
      }
      this.currentAudio = null
    }
    if (typeof window !== 'undefined') {
      try {
        window.AndroidBridge?.stopSpeaking()
      } catch {
        // ignore
      }
      try {
        window.speechSynthesis?.cancel()
      } catch {
        // ignore
      }
    }
    if (this.activeResolve) {
      const res = this.activeResolve
      this.activeResolve = null
      res()
    }
    this.notify(false, '')
  }

  public async playWord(text: string, accent: AudioAccent = 'us'): Promise<void> {
    const clean = text.replace(/\s+/g, ' ').trim()
    if (!clean) return

    if (this.activeTerm === clean) {
      this.stop()
      return
    }

    // Stop previous audio if playing
    this.stop()

    this.notify(true, clean)

    // On Android device, always use the phone's own native TTS / speech synthesis directly
    if (typeof window !== 'undefined' && window.AndroidBridge) {
      await this.speakOnAndroidDevice(clean, accent)
      return
    }

    // In Web browser: only single words use Youdao dictionary voice; sentences use SpeechSynthesis
    if (/\s/.test(clean) || clean.length > 36) {
      await this.fallbackSpeechSynthesis(clean, accent)
      this.notify(false, '')
      return
    }

    const url = this.getYoudaoUrl(clean, accent)

    return new Promise((resolve) => {
      const audio = new Audio(url)
      this.currentAudio = audio
      this.activeResolve = resolve

      const cleanup = () => {
        if (this.activeResolve === resolve) {
          this.activeResolve = null
        }
        this.notify(false, '')
        if (this.currentAudio === audio) {
          this.currentAudio = null
        }
        resolve()
      }

      audio.onended = cleanup
      audio.onerror = () => {
        // Fallback to Web SpeechSynthesis if offline or Youdao fails
        this.fallbackSpeechSynthesis(clean, accent).finally(cleanup)
      }

      audio.play().catch(() => {
        // Fallback to Web SpeechSynthesis
        this.fallbackSpeechSynthesis(clean, accent).finally(cleanup)
      })
    })
  }

  private speakOnAndroidDevice(text: string, accent: AudioAccent): Promise<void> {
    return new Promise((resolve) => {
      this.activeResolve = resolve
      let finished = false

      const finish = () => {
        if (finished) return
        finished = true
        if (this.pollTimer !== null && typeof window !== 'undefined') {
          window.clearInterval(this.pollTimer)
          this.pollTimer = null
        }
        if (this.activeResolve === resolve) {
          this.activeResolve = null
        }
        if (this.activeTerm === text) {
          this.notify(false, '')
        }
        resolve()
      }

      if (typeof window !== 'undefined') {
        window.__onNativeAudioEnded = () => {
          finish()
        }
      }

      try {
        const started = window.AndroidBridge?.speak(text, accent)
        if (!started) {
          this.fallbackSpeechSynthesis(text, accent).finally(finish)
          return
        }
        const maxMs = Math.min(45000, Math.max(2500, text.length * 110))
        const startedAt = Date.now()
        this.pollTimer = window.setInterval(() => {
          if (Date.now() - startedAt > maxMs) {
            finish()
            return
          }
          try {
            if (
              Date.now() - startedAt > 600 &&
              window.AndroidBridge?.isSpeaking &&
              !window.AndroidBridge.isSpeaking()
            ) {
              finish()
            }
          } catch {
            // ignore
          }
        }, 250)
      } catch {
        this.fallbackSpeechSynthesis(text, accent).finally(finish)
      }
    })
  }

  private fallbackSpeechSynthesis(text: string, accent: AudioAccent): Promise<void> {
    return new Promise((resolve) => {
      if (typeof window === 'undefined' || !('speechSynthesis' in window)) {
        resolve()
        return
      }
      try {
        window.speechSynthesis.cancel()
        const utterance = new SpeechSynthesisUtterance(text)
        utterance.lang = accent === 'uk' ? 'en-GB' : 'en-US'
        utterance.rate = 0.95
        utterance.onend = () => resolve()
        utterance.onerror = () => resolve()
        window.speechSynthesis.speak(utterance)
      } catch {
        resolve()
      }
    })
  }
}

export const audioService = new AudioService()
