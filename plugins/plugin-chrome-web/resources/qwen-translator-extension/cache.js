/* Only the background owns persistent translations. No URL or source is stored. */
class TranslationCache {
  constructor(storage, {maxEntries = 2000, maxBytes = 4 * 1024 * 1024} = {}) {
    this.storage = storage;
    this.maxEntries = maxEntries;
    this.maxBytes = maxBytes;
    this.queue = Promise.resolve();
    this.generation = 0;
  }

  serial(work) {
    const result = this.queue.then(work);
    this.queue = result.catch(() => {});
    return result;
  }

  async key(url, target, html) {
    const bytes = new TextEncoder().encode(JSON.stringify([url, target, html]));
    return [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))]
      .map(value => value.toString(16).padStart(2, '0')).join('');
  }

  validSlots(slots, expected = null) {
    if (!slots || typeof slots !== 'object' || Array.isArray(slots)) return false;
    const keys = Object.keys(slots);
    return keys.length > 0 && keys.length <= 200 && keys.every(key =>
      /^\d{1,4}$/.test(key) && typeof slots[key] === 'string' && slots[key].length <= 64000) &&
      (!expected || keys.length === expected.length && expected.every(key => Object.hasOwn(slots, key)));
  }

  expected(html) {
    return [...html.matchAll(/<t id="(\d{1,4})">/g)].map(match => match[1]);
  }

  entries(value) {
    if (value === undefined) return [];
    if (value?.version !== 1 || !Array.isArray(value.entries) || value.entries.some(entry =>
      !entry || !/^[a-f0-9]{64}$/.test(entry.key) || !this.validSlots(entry.slots))) {
      throw new Error('已保存的译文记录损坏；本次将重新翻译。');
    }
    return value.entries;
  }

  trim(entries) {
    const sizes = entries.map(entry => JSON.stringify(entry).length * 2);
    let bytes = sizes.reduce((sum, size) => sum + size, 64), start = 0;
    while (entries.length - start > this.maxEntries || bytes > this.maxBytes) bytes -= sizes[start++];
    return entries.slice(start);
  }

  async lookup(url, payload, {force = false, privateWindow = false} = {}) {
    const ticket = {generation: this.generation, enabled: false, hits: new Map(), keys: [], warning: ''};
    if (privateWindow) return ticket;
    ticket.keys = await Promise.all(payload.segments.map(segment => this.key(url, payload.target, segment.html)));
    return this.serial(async () => {
      try {
        const saved = await this.storage.get(['autoSaveTranslations', 'translationCache']);
        if (saved.autoSaveTranslations === false || ticket.generation !== this.generation) return ticket;
        ticket.enabled = true;
        const entries = this.entries(saved.translationCache);
        if (force) return ticket;
        const byKey = new Map(entries.map(entry => [entry.key, entry]));
        payload.segments.forEach((segment, index) => {
          const entry = byKey.get(ticket.keys[index]);
          if (entry && this.validSlots(entry.slots, this.expected(segment.html))) ticket.hits.set(segment.id, entry.slots);
        });
        if (ticket.hits.size) {
          const touched = new Set(payload.segments.flatMap((segment, index) =>
            ticket.hits.has(segment.id) ? [ticket.keys[index]] : []));
          const recent = entries.filter(entry => touched.has(entry.key));
          await this.storage.set({translationCache: {version: 1,
            entries: this.trim([...entries.filter(entry => !touched.has(entry.key)), ...recent])}});
        }
      } catch (error) {
        ticket.warning = error.message?.includes('记录损坏') ? error.message : '无法读取或更新已保存译文；本次仍可翻译。';
      }
      return ticket;
    });
  }

  commit(ticket, segments, results, signal) {
    if (!ticket.enabled) return Promise.resolve({saved: false, warning: ticket.warning});
    return this.serial(async () => {
      if (signal?.aborted || ticket.generation !== this.generation) return {saved: false};
      try {
        const stored = await this.storage.get(['autoSaveTranslations', 'translationCache']);
        if (stored.autoSaveTranslations === false || signal?.aborted || ticket.generation !== this.generation) return {saved: false};
        let entries;
        try { entries = this.entries(stored.translationCache); }
        catch (_) { entries = []; }
        const additions = new Map(segments.map((segment, index) =>
          [ticket.keys[index], {key: ticket.keys[index], slots: results.get(segment.id)}]));
        entries = this.trim([...entries.filter(entry => !additions.has(entry.key)), ...additions.values()]);
        await this.storage.set({translationCache: {version: 1, entries}});
        if (signal?.aborted || ticket.generation !== this.generation) {
          if (stored.translationCache === undefined) await this.storage.remove('translationCache');
          else await this.storage.set({translationCache: stored.translationCache});
          return {saved: false};
        }
        const retained = new Set(entries.map(entry => entry.key));
        const saved = ticket.keys.every(key => retained.has(key));
        return {saved, warning: saved ? ticket.warning : '本页译文超过保存容量，部分段落未保存。'};
      } catch (_) { return {saved: false, warning: '译文已生成，但浏览器存储失败，未能保存。'}; }
    });
  }

  settings() {
    return this.serial(async () => {
      const saved = await this.storage.get(['autoSaveTranslations', 'translationCache']);
      const entries = this.entries(saved.translationCache);
      return {enabled: saved.autoSaveTranslations !== false, count: entries.length,
        bytes: JSON.stringify(entries).length * 2};
    });
  }

  configure(enabled) {
    this.generation++;
    return this.serial(async () => {
      await this.storage.set({autoSaveTranslations: Boolean(enabled)});
      return {ok: true};
    });
  }

  clear() {
    this.generation++;
    return this.serial(async () => {
      await this.storage.remove('translationCache');
      return {ok: true};
    });
  }
}
