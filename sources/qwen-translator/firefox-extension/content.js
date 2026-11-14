/* Original implementation; see SOURCES.md for the Read Frog design references. */
(() => {
  if (globalThis.__qwenWebTranslator) return;
  globalThis.__qwenWebTranslator = true;
  const SKIP = 'script,style,noscript,template,pre,code,kbd,samp,textarea,input,select,button,svg,math,canvas,video,audio,iframe,nav,form,label,[role="navigation"],[role="menu"],[role="menubar"],[role="menuitem"],[role="toolbar"],[role="button"],[role="tab"],[role="search"],[contenteditable]:not([contenteditable="false"]),[translate="no"],.notranslate,[data-qwen-owned]';
  const INLINE = new Set('a b strong i em u s small sub sup span mark abbr cite q bdi bdo del ins br'.split(' '));
  const CHROME = 'aside,footer,[role="complementary"],.article-metrics,.articleMetrics,.related-articles,.recommended-articles,.ref-list,.references,[role="doc-bibliography"],.article-sidebar,.site-header';
  const PROSE = 'p,h1,h2,h3,h4,h5,h6,li,dt,dd,td,th,blockquote,figcaption,[role="paragraph"],.NLM_p,.para';
  const BATCH_HTML = 24000, BATCH_SEGMENTS = 32;
  const BLOCKS = new Set('P DIV SECTION ARTICLE MAIN ASIDE NAV HEADER FOOTER LI DT DD TD TH H1 H2 H3 H4 H5 H6 BLOCKQUOTE FIGCAPTION LABEL SUMMARY'.split(' '));
  let mode = 'off', target = 'zh-CN', generation = 0, sequence = 0;
  let working = false, scanTimer, badge, badgeText, observer, pageContext = '';
  let needsDiscovery = true, hasHiddenCandidates = false, orderedRecords = null;
  let cacheHits = 0, cacheSaved = null, cacheWarning = '';
  let connectionLost = false;
  const CONNECTION_HELP = '扩展连接已断开。请刷新网页后再点一次翻译；如果仍不行，请在 Firefox 扩展管理中将本扩展关闭后重新开启。';
  const records = new Map();
  const dirty = new Set();
  const escape = text => text.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
  const excluded = node => !!node.parentElement?.closest(SKIP + ',' + CHROME + ',[hidden],[aria-hidden="true"]');

  async function sendMessage(message) {
    const epoch = generation;
    try {
      const result = await browser.runtime.sendMessage(message);
      if (result == null) throw new Error('Receiving end does not exist');
      return result;
    } catch (error) {
      if (/receiving end does not exist|could not establish connection|extension context invalidated/i.test(error.message || '')) {
        if (epoch === generation && mode !== 'off') connectionLost = true;
        throw new Error(CONNECTION_HELP);
      }
      throw error;
    }
  }

  function snapshot(element, roots = [...element.childNodes], anchor = element) {
    const nodes = [], originals = [], index = new Map();
    function walk(node, depth = 0) {
      if (depth > 35) return '';
      if (node.nodeType === Node.TEXT_NODE) {
        if (!node.data.trim() || excluded(node)) return '';
        const id = nodes.length;
        nodes.push(node); originals.push(node.data); index.set(node, id);
        return `<t id="${id}">${escape(node.data)}</t>`;
      }
      if (node.nodeType !== Node.ELEMENT_NODE || node.matches(SKIP + ',' + CHROME) || node.hidden) return '';
      const content = [...node.childNodes].map(child => walk(child, depth + 1)).join('');
      const tag = node.localName;
      return INLINE.has(tag) ? `<${tag}>${content}</${tag}>` : content;
    }
    const html = roots.map(node => walk(node)).join('');
    return {element, roots, anchor, nodes, originals, index, html, parts: translationParts(html, originals), state: 'pending', host: null, slots: null};
  }

  // Keep the DOM intact: oversized paragraphs use virtual text slots and are
  // reassembled before either display mode renders the original inline structure.
  function translationParts(html, originals) {
    const part = (html, slots) => ({id: `s${++sequence}`, html, slots, state: 'pending', translated: null});
    if (html.length <= 12000 && originals.length <= 200) {
      return [part(html, originals.map((_, index) => index))];
    }
    const pieces = [];
    originals.forEach((text, index) => {
      while (text.length) {
        // 1000 code points fit comfortably even after XML escaping. Prefer a
        // sentence/word boundary without dropping spaces or splitting surrogates.
        let chunk = [...text.slice(0, 2002)].slice(0, 1000).join('');
        if (chunk.length < text.length) {
          const boundary = [...chunk.matchAll(/[。！？.!?]\s*|\s+/gu)].pop();
          if (boundary && boundary.index > chunk.length / 2) chunk = chunk.slice(0, boundary.index + boundary[0].length);
        }
        pieces.push({index, text: chunk});
        text = text.slice(chunk.length);
      }
    });
    const parts = [];
    let htmlPart = '', slots = [];
    for (const piece of pieces) {
      let slot = `<t id="${slots.length}">${escape(piece.text)}</t>`;
      if (htmlPart.length + slot.length > 10000 || slots.length === 180) {
        parts.push(part(htmlPart, slots)); htmlPart = ''; slots = [];
        slot = `<t id="0">${escape(piece.text)}</t>`;
      }
      htmlPart += slot; slots.push(piece.index);
    }
    if (slots.length) parts.push(part(htmlPart, slots));
    return parts;
  }

  function unchanged(record) {
    return !dirty.has(record) && record.anchor.isConnected && record.nodes.every((node, index) =>
      record.element.contains(node) && node.data === (
        mode === 'translation' && record.state === 'done' ? record.slots[String(index)] : record.originals[index]));
  }

  function restoreRecord(record) {
    record.host?.remove(); record.host = null;
    if (record.slots) record.nodes.forEach((node, index) => {
      // Do not overwrite content the website has updated since translation.
      if (node.data === record.slots[String(index)]) node.data = record.originals[index];
    });
  }

  function translatedMirror(record) {
    function clone(node) {
      if (node.nodeType === Node.TEXT_NODE) {
        const id = record.index.get(node);
        return document.createTextNode(id === undefined ? node.data : record.slots[String(id)]);
      }
      if (node.nodeType !== Node.ELEMENT_NODE || node.matches('[data-qwen-owned],script,style,input,textarea,button,select,iframe,svg,math,canvas,video,audio')) return document.createTextNode('');
      const copy = document.createElement(INLINE.has(node.localName) ? node.localName : 'span');
      if (node.localName === 'a') {
        try {
          const url = new URL(node.getAttribute('href'), location.href);
          if (['https:', 'http:', 'mailto:'].includes(url.protocol)) copy.href = url.href;
        } catch (_) { /* Broken links remain text. */ }
        copy.rel = 'noopener noreferrer';
      }
      [...node.childNodes].forEach(child => copy.append(clone(child)));
      return copy;
    }
    const content = document.createDocumentFragment();
    record.roots.forEach(child => content.append(clone(child)));
    return content;
  }

  function render(record) {
    if (!record.slots) return;
    restoreRecord(record);
    if (mode === 'off') return;
    if (mode === 'translation') {
      record.nodes.forEach((node, index) => { node.data = record.slots[String(index)]; });
    }
    const host = document.createElement('span');
    host.dataset.qwenOwned = 'translation';
    host.style.setProperty('display', mode === 'bilingual' ? 'block' : 'inline-block', 'important');
    const shadow = host.attachShadow({mode: 'closed'});
    const style = document.createElement('style');
    style.textContent = ':host{margin:.45em 0 .75em;font-weight:normal}div{border-inline-start:3px solid #3fa592;border-radius:2px;padding:.1em 0 .1em .75em;line-height:1.7;white-space:normal}a{color:inherit;text-decoration:underline;text-underline-offset:2px}button{font:12px/1.4 system-ui,sans-serif;border:0;border-radius:999px;background:#3fa5921a;color:#1c7a69;padding:3px 10px;margin:4px 6px 0 0;cursor:pointer;opacity:.75;transition:opacity .15s,background .15s}button:hover{opacity:1;background:#3fa59233}button:focus-visible{outline:2px solid #3fa592;outline-offset:1px;opacity:1}button:disabled{cursor:wait;opacity:.6}small{font:12px system-ui,sans-serif;color:#b5432c}@media(prefers-color-scheme:dark){button{color:#7fd3c1}small{color:#f3a28c}}';
    shadow.append(style);
    if (mode === 'bilingual') {
      const content = document.createElement('div');
      content.lang = target;
      content.append(translatedMirror(record)); shadow.append(content);
    }
    const retry = document.createElement('button');
    retry.type = 'button'; retry.setAttribute('aria-label', '重新翻译本段');
    retry.textContent = record.refreshing ? '正在重新翻译…' : '↻ 重新翻译';
    retry.disabled = !!record.refreshing;
    retry.onclick = () => retranslate(record);
    shadow.append(retry); record.refreshButton = retry;
    if (record.refreshError) {
      const error = document.createElement('small'); error.setAttribute('role', 'status');
      error.textContent = '重译失败，已保留原译文'; error.title = record.refreshError;
      shadow.append(error);
    }
    if (record.anchor === record.element) record.element.append(host);
    else record.anchor.after(host);
    record.host = host;
  }

  function rememberCache(result) {
    if (Number.isFinite(result.cacheHits)) cacheHits += result.cacheHits;
    if (result.saved === false) cacheSaved = false;
    else if (result.saved === true && cacheSaved !== false) cacheSaved = true;
    if (result.warning) cacheWarning = String(result.warning);
  }

  function validSlots(part, slots) {
    return slots && Object.keys(slots).length === part.slots.length &&
      part.slots.every((_, index) => typeof slots[String(index)] === 'string');
  }

  async function retranslate(record) {
    if (mode === 'off' || record.refreshing || record.state !== 'done' || !unchanged(record)) return;
    const epoch = generation, refreshToken = Symbol('refresh');
    record.refreshToken = refreshToken; record.refreshing = true; record.refreshError = '';
    const current = () => epoch === generation && records.get(record.anchor) === record &&
      record.refreshToken === refreshToken && unchanged(record);
    disconnectObserver(() => render(record));
    notify('正在重新翻译这一段，完成前保留当前译文');
    const tickets = [];
    let canSave = true, saveWarning = '';
    try {
      const translated = new Map();
      for (let offset = 0; offset < record.parts.length;) {
        const batch = []; let size = 0;
        while (offset < record.parts.length && batch.length < BATCH_SEGMENTS &&
               size + record.parts[offset].html.length <= BATCH_HTML) {
          const part = record.parts[offset++]; batch.push(part); size += part.html.length;
        }
        const result = await sendMessage({type: 'qwen:translate', force: true, deferSave: true,
          requestId: `r${++sequence}`, payload: {target, title: document.title, context: pageContext,
            segments: batch.map(({id, html}) => ({id, html}))}});
        if (result.cacheTicket) tickets.push(result.cacheTicket);
        else canSave = false;
        if (result.warning) saveWarning = String(result.warning);
        if (!current()) return;
        if (result.error) throw new Error(result.error);
        const byId = new Map(result.segments.map(item => [item.id, item.slots]));
        for (const part of batch) {
          const slots = byId.get(part.id);
          if (!validSlots(part, slots)) throw new Error('译文文字编号不完整');
          translated.set(part, slots);
        }
      }
      if (!current()) return;
      const slots = Object.fromEntries(record.originals.map((_, index) => [String(index), '']));
      for (const part of record.parts) part.slots.forEach((index, i) => {
        slots[String(index)] += translated.get(part)[String(i)];
      });
      // Publish every fragment together so a later failure cannot leave a
      // long paragraph with a mixture of old and new cached translations.
      let saved;
      try {
        saved = canSave ? await browser.runtime.sendMessage({type: 'qwen:cache-save', tickets: [...tickets]})
          : {saved: false, warning: saveWarning};
      }
      catch (_) { saved = {saved: false, warning: '缓存保存失败，本次译文仍可阅读'}; }
      if (!current()) return;
      rememberCache(saved || {saved: false});
      if (saved?.saved === true) tickets.length = 0;
      disconnectObserver(() => {
        // Restore with the OLD slots before replacing them, especially in translation-only mode.
        restoreRecord(record); record.slots = slots;
        for (const part of record.parts) part.translated = translated.get(part);
        record.refreshing = false; render(record);
      });
      notify('这一段已重新翻译');
    } catch (error) {
      if (current()) {
        record.refreshing = false; record.refreshError = error.message;
        disconnectObserver(() => render(record));
        notify(`这一段重译失败，已保留原译文 · ${error.message}`);
      }
    } finally {
      if (tickets.length) browser.runtime.sendMessage({type: 'qwen:cache-discard', tickets}).catch(() => {});
      if (epoch === generation) {
        record.refreshing = false;
        if (!current()) schedule();
      }
    }
  }

  function notify(message) {
    if (!badge) {
      badge = document.createElement('div'); badge.dataset.qwenOwned = 'status';
      badge.style.cssText = 'position:fixed!important;bottom:18px!important;right:18px!important;z-index:2147483647!important';
      const shadow = badge.attachShadow({mode: 'closed'});
      const style = document.createElement('style');
      style.textContent = ':host{font:13px/1.5 system-ui,sans-serif}section{display:flex;align-items:center;gap:10px;max-width:440px;padding:10px 10px 10px 14px;background:#fff;color:#1f2d2b;border:1px solid #e1e6e3;border-radius:16px;box-shadow:0 10px 30px #1f2d2b26;animation:in .25s ease both}i{flex:none;width:8px;height:8px;border-radius:50%;background:#2fa58f;box-shadow:0 0 0 4px #2fa58f26}span{flex:1;min-width:0}button{flex-shrink:0;white-space:nowrap;border:1px solid #e1e6e3;border-radius:999px;background:#f6f8f7;color:inherit;font:600 12px system-ui,sans-serif;padding:6px 12px;cursor:pointer}button:hover{background:#e1f3ef;border-color:#9fd3c7}button:focus-visible{outline:2px solid #2fa58f;outline-offset:1px}@keyframes in{from{opacity:0;transform:translateY(8px)}}@media(prefers-color-scheme:dark){section{background:#1c2726;color:#e8efed;border-color:#30413e}button{background:#223130;border-color:#30413e}button:hover{background:#1f3f39}}@media(prefers-reduced-motion:reduce){section{animation:none}}';
      const box = document.createElement('section');
      badgeText = document.createElement('span'); badgeText.setAttribute('role', 'status');
      const retry = document.createElement('button'); retry.textContent = '重试';
      retry.onclick = () => {
        connectionLost = false;
        for (const record of records.values()) if (record.refreshError) retranslate(record);
        for (const record of records.values()) if (record.state === 'error') {
          record.state = 'pending';
          for (const part of record.parts) if (part.state === 'error') part.state = 'pending';
        }
        schedule();
      };
      const stop = document.createElement('button'); stop.textContent = '恢复原文'; stop.onclick = reset;
      const dot = document.createElement('i'); dot.setAttribute('aria-hidden', 'true');
      box.append(dot, badgeText, retry, stop); shadow.append(style, box); document.documentElement.append(badge);
    }
    const cacheStatus = [cacheHits ? `${cacheHits} 段使用了之前保存的译文` : '',
      cacheSaved === false ? '部分译文未保存' : cacheSaved === true ? '译文已保存' : '', cacheWarning].filter(Boolean);
    badgeText.textContent = connectionLost ? CONNECTION_HELP : [message, ...cacheStatus].join(' · ');
  }

  function disconnectObserver(action) {
    observer?.disconnect();
    action();
    if (mode !== 'off') observe();
  }

  function reset() {
    mode = 'off'; generation++;
    observer?.disconnect(); clearTimeout(scanTimer);
    // An invalidated extension can throw synchronously; restoring the page must still work.
    sendMessage({type: 'qwen:cancel'}).catch(() => {});
    connectionLost = false;
    records.forEach(restoreRecord); records.clear(); dirty.clear(); pageContext = '';
    cacheHits = 0; cacheSaved = null; cacheWarning = '';
    needsDiscovery = true; orderedRecords = null;
    badge?.remove(); badge = null;
  }

  function visible(element) {
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 &&
      getComputedStyle(element).visibility !== 'hidden';
  }

  function worthTranslating(record) {
    // Measure the whole paragraph, including inline links/emphasis. Filtering
    // individual text nodes would lose short words inside meaningful sentences.
    const text = record.originals.join('').replace(/\s+/gu, ' ').trim();
    if (!/\p{L}/u.test(text)) return false;
    if (record.element.matches('h1,h2,h3,h4,h5,h6,[role="heading"],figcaption')) return true;
    const letters = (text.match(/[\p{L}\p{N}]/gu) || []).length;
    const words = (text.match(/\p{L}+(?:['’\-]\p{L}+)*/gu) || []).length;
    const cjk = (text.match(/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/gu) || []).length;
    if (letters >= 20 || words >= 4 || cjk >= 10) return true;
    // Keep short prose sentences; isolated links such as "Read more." are UI.
    return record.element.matches('p,li,blockquote,dd,td,th') &&
      !record.nodes.every(node => node.parentElement.closest('a,[role="link"]')) &&
      /[.!?。！？]["'”’）)]?$/u.test(text) && (words >= 2 || cjk >= 4);
  }

  function discover() {
    hasHiddenCandidates = false;
    // Prefer publisher body/abstract regions, then a substantial semantic article.
    // Keep the title even when the publisher places it outside the body region.
    const publisher = [...document.querySelectorAll('[itemprop="articleBody"],.article-body,.articleBody,#articleBody,.article-fulltext,.hlFld-Fulltext')]
      .filter(element => visible(element) && !element.closest(SKIP + ',' + CHROME) && element.textContent.trim().length >= 200);
    const articles = [...document.querySelectorAll('article')].filter(element =>
      visible(element) && !element.closest(SKIP + ',' + CHROME) && element.querySelector('p') && element.textContent.trim().length >= 200);
    const scopes = publisher.length ? [...publisher, ...document.querySelectorAll('.abstract,.abstractSection,.abstractInFull,[role="doc-abstract"],[itemprop="abstract"]')]
      : articles.length === 1 ? articles : [];
    const inScope = node => !scopes.length || scopes.some(element => element.contains(node)) ||
      !!node.parentElement.closest('h1,.article-title,.hlFld-Title');
    // Text-node discovery covers semantic HTML and paragraph-like div/span layouts.
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode(node) { return node.data.trim() && /\p{L}/u.test(node.data) && !excluded(node) && inScope(node)
        ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT; },
    });
    const candidates = new Map();
    while (walker.nextNode()) {
      const node = walker.currentNode;
      // Citations/superscripts are often inline-block. They still belong to the
      // enclosing paragraph, not a separate translation record for each text run.
      let element = node.parentElement.closest(PROSE) || node.parentElement;
      while (element && element !== document.body && !BLOCKS.has(element.tagName) &&
             ['inline', 'contents'].includes(getComputedStyle(element).display)) element = element.parentElement;
      if (element) {
        if (!candidates.has(element)) candidates.set(element, []);
        candidates.get(element).push(node);
      }
    }
    const containers = new Set();
    for (const element of candidates.keys()) {
      for (let parent = element.parentElement; parent; parent = parent.parentElement) {
        if (candidates.has(parent)) containers.add(parent);
      }
    }
    function add(element, roots, anchor) {
      if (records.has(anchor)) return;
      const record = snapshot(element, roots, anchor);
      if (!record.nodes.length || !worthTranslating(record)) return;
      records.set(anchor, record); orderedRecords = null;
    }
    for (const [element, textNodes] of candidates) {
      if (!visible(element)) { hasHiddenCandidates = true; continue; }
      if (containers.has(element) || element === document.body) {
        // Preserve loose text around nested paragraphs without translating their children twice.
        for (const node of textNodes) add(element, [node], node);
      } else add(element, [...element.childNodes].filter(n => !(n.nodeType === 1 && n.matches('[data-qwen-owned]'))), element);
    }
  }

  async function pump() {
    if (working || mode === 'off') return;
    if (connectionLost) { notify(CONNECTION_HELP); return; }
    working = true;
    const epoch = generation;
    try {
      disconnectObserver(() => {
        for (const [element, record] of records) if (!unchanged(record)) {
          restoreRecord(record); records.delete(element); dirty.delete(record);
          needsDiscovery = true; orderedRecords = null;
        }
      });
      if (needsDiscovery) { discover(); needsDiscovery = false; }
      const ordered = orderedRecords ||= [...records.values()].sort((a, b) =>
        a.anchor.compareDocumentPosition(b.anchor) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1);
      if (!pageContext) pageContext = ordered.map(record => record.originals.join('')).join('\n').slice(0, 6000);
      const batch = []; let size = 0;
      outer: for (const record of ordered) {
        if (record.state === 'done' || !visible(record.element)) continue;
        for (const part of record.parts) {
          if (part.state !== 'pending') continue;
          if (size + part.html.length > BATCH_HTML) break outer;
          batch.push({record, part}); size += part.html.length;
          if (batch.length === BATCH_SEGMENTS) break outer;
        }
      }
      if (!batch.length) {
        const errors = [...records.values()].filter(record => record.state === 'error' || record.refreshError);
        const count = [...records.values()].filter(record => record.state === 'done').length;
        notify(errors.length ? `已翻译 ${count} 段，${errors.length} 段失败 · ${errors[0].refreshError ? '重译失败，已保留原译文' : errors[0].error || '点击重试'}` : count ? `翻译完成，共 ${count} 段` : '未找到需要翻译的内容，菜单和按钮等已自动跳过');
        return;
      }
      batch.forEach(({record, part}) => { record.state = 'working'; part.state = 'working'; });
      notify(`正在翻译… 本批 ${new Set(batch.map(item => item.record)).size} 段，共 ${ordered.length} 段`);
      try {
        const result = await sendMessage({type: 'qwen:translate', requestId: `r${++sequence}`,
          payload: {target, title: document.title,
            context: batch.length < ordered.reduce((count, record) => count + record.parts.length, 0) ? pageContext : '',
            segments: batch.map(({part: {id, html}}) => ({id, html}))}});
        if (epoch !== generation) return;
        if (result.error) throw new Error(result.error);
        rememberCache(result);
        const byId = new Map(result.segments.map(item => [item.id, item.slots]));
        const failed = new Set(result.failedSegments || []);
        disconnectObserver(() => {
          for (const {record, part} of batch) {
            if (records.get(record.anchor) !== record) continue;
            if (!unchanged(record)) {
              records.delete(record.anchor); dirty.delete(record);
              needsDiscovery = true; orderedRecords = null; continue;
            }
            const slots = byId.get(part.id);
            if (!validSlots(part, slots)) {
              part.state = 'error'; record.state = 'error';
              record.error = failed.has(part.id) ? result.failure || '未缓存段落翻译失败，请重试' : '译文文字编号不完整';
              continue;
            }
            part.translated = slots; part.state = 'done';
            if (record.parts.every(item => item.state === 'done')) {
              record.slots = Object.fromEntries(record.originals.map((_, i) => [String(i), '']));
              for (const item of record.parts) item.slots.forEach((index, i) => {
                record.slots[String(index)] += item.translated[String(i)];
              });
              record.state = 'done'; render(record);
            }
          }
          for (const record of new Set(batch.map(item => item.record))) {
            if (record.parts.some(part => part.state === 'error')) record.state = 'error';
          }
        });
      } catch (error) {
        if (epoch === generation) batch.forEach(({record, part}) => {
          part.state = connectionLost ? 'pending' : 'error';
          record.state = connectionLost ? 'pending' : 'error'; record.error = error.message;
        });
      }
      if (epoch === generation) schedule();
    } finally {
      working = false;
      if (epoch !== generation && mode !== 'off') schedule();
    }
  }

  function schedule() {
    clearTimeout(scanTimer);
    if (mode !== 'off') scanTimer = setTimeout(() => { pump().catch(error => { working = false; notify(error.message); }); }, 250);
  }

  function observe() {
    observer ||= new MutationObserver(mutations => {
      const relevant = mutations.filter(m => !m.target.parentElement?.closest('[data-qwen-owned]') &&
        !(m.target.nodeType === 1 && m.target.matches('[data-qwen-owned]')));
      if (!relevant.length) return;
      const targets = [...new Set(relevant.map(mutation => mutation.target))];
      for (const record of records.values()) {
        if (!record.anchor.isConnected || targets.some(target => target === record.anchor ||
            record.roots.some(root => root === target || root.contains(target)))) dirty.add(record);
      }
      needsDiscovery = true; orderedRecords = null; schedule();
    });
    if (document.body) observer.observe(document.body, {childList: true, subtree: true, characterData: true});
  }
  window.addEventListener('scroll', () => { needsDiscovery ||= hasHiddenCandidates; schedule(); }, {passive: true});
  window.addEventListener('resize', () => { needsDiscovery = true; schedule(); }, {passive: true});
  window.addEventListener('pagehide', reset);
  browser.runtime.onMessage.addListener(message => {
    if (message.type !== 'qwen:mode') return;
    if (!['off', 'bilingual', 'translation'].includes(message.mode)) return Promise.resolve({error: '显示模式无效。'});
    if (message.mode === 'off') { reset(); return Promise.resolve({ok: true}); }
    if (!document.body) return Promise.resolve({error: '页面尚未加载完成。'});
    if (message.target !== target) reset();
    target = message.target;
    connectionLost = false;
    disconnectObserver(() => {
      for (const [key, record] of records) if (!unchanged(record)) {
        restoreRecord(record); records.delete(key); dirty.delete(record);
        needsDiscovery = true; orderedRecords = null;
      }
      records.forEach(restoreRecord);
      mode = message.mode;
      records.forEach(record => { if (record.state === 'done') render(record); });
    });
    observe(); notify('准备翻译这个网页…'); schedule();
    return Promise.resolve({ok: true});
  });
})();
