/* The pairing token stays in the extension background, never in page scripts. */
const ENDPOINT = 'http://127.0.0.1:8766';
const pending = new Map();
let queue = Promise.resolve();
const translationCache = new TranslationCache(browser.storage.local);
const staged = new Map();
let keepAliveTimer = null;

function updateKeepAlive() {
  // Firefox MV3 can suspend an event page while fetch/onMessage is still pending.
  // A parent-process API call resets its idle timer. Only keep it awake while
  // user-requested work is active or queued; cancellation releases it immediately.
  if (pending.size && keepAliveTimer === null) {
    keepAliveTimer = setInterval(() => {
      browser.runtime.getPlatformInfo().catch(() => {});
    }, 10000);
  } else if (!pending.size && keepAliveTimer !== null) {
    clearInterval(keepAliveTimer);
    keepAliveTimer = null;
  }
}

function validatePayload(payload) {
  if (!payload || typeof payload.target !== 'string' || !Array.isArray(payload.segments) ||
      !payload.segments.length || payload.segments.length > 256) throw new Error('网页翻译请求无效。');
  const ids = new Set();
  let size = 0;
  for (const segment of payload.segments) {
    if (!segment || typeof segment.id !== 'string' || ids.has(segment.id) ||
        typeof segment.html !== 'string' || !segment.html || segment.html.length > 12000) throw new Error('网页翻译段落无效。');
    const slots = translationCache.expected(segment.html);
    if (!slots.length || slots.length > 200 || new Set(slots).size !== slots.length) throw new Error('网页翻译文字结构无效。');
    ids.add(segment.id); size += segment.html.length;
  }
  if (size > 64000) throw new Error('网页翻译批次过大。');
}

function validatedResults(segments, response) {
  if (!Array.isArray(response?.segments) || response.segments.length !== segments.length) throw new Error('译文段落不完整，请重试。');
  const results = new Map();
  for (const result of response.segments) {
    const original = segments.find(segment => segment.id === result?.id);
    if (!original || results.has(result.id) ||
        !translationCache.validSlots(result.slots, translationCache.expected(original.html))) throw new Error('译文结构不匹配，请重试。');
    results.set(result.id, result.slots);
  }
  return results;
}

function pruneStaged() {
  for (const [key, entry] of staged) if (entry.expires < Date.now()) staged.delete(key);
}

async function translate(message, sender, signal) {
  const payload = message.payload;
  validatePayload(payload);
  const ticket = await translationCache.lookup(sender.url, payload,
    {force: message.force === true, privateWindow: sender.tab.incognito === true});
  if (signal.aborted) throw new Error('翻译已取消。');
  const missing = payload.segments.filter(segment => !ticket.hits.has(segment.id));
  const results = new Map(ticket.hits);
  let failedSegments = [], failure = '';
  if (missing.length) {
    try {
      const response = await bridge('/translate', {...payload, segments: missing}, signal);
      if (signal.aborted) throw new Error('翻译已取消。');
      failedSegments = response.failedSegments || [];
      if (!Array.isArray(failedSegments) || new Set(failedSegments).size !== failedSegments.length ||
          failedSegments.some(id => !missing.some(segment => segment.id === id))) throw new Error('译文失败段落编号无效。');
      const successful = missing.filter(segment => !failedSegments.includes(segment.id));
      for (const [id, slots] of validatedResults(successful, response)) results.set(id, slots);
      failure = failedSegments.length ? response.failure || '部分段落翻译失败，请重试。' : '';
    } catch (error) {
      if (signal.aborted || !ticket.hits.size) throw error;
      return {segments: payload.segments.filter(segment => ticket.hits.has(segment.id))
        .map(segment => ({id: segment.id, slots: ticket.hits.get(segment.id)})),
        failedSegments: missing.map(segment => segment.id), failure: error.message,
        cacheHits: ticket.hits.size, saved: true, warning: ticket.warning};
    }
  }
  const completed = payload.segments.filter(segment => results.has(segment.id));
  const response = {segments: completed.map(segment => ({id: segment.id, slots: results.get(segment.id)})),
    failedSegments, failure,
    cacheHits: ticket.hits.size, saved: !missing.length && ticket.enabled, warning: ticket.warning};
  if (!completed.length) return response;
  if (!missing.length) return response;
  if (message.force === true && message.deferSave === true) {
    if (failedSegments.length) return response;
    if (ticket.enabled) {
      pruneStaged();
      const bytes = JSON.stringify(response.segments).length * 2 + JSON.stringify(payload).length * 2;
      if (staged.size >= 32 || [...staged.values()].reduce((sum, entry) => sum + entry.bytes, bytes) > 8 * 1024 * 1024) {
        response.warning = '重译暂存容量已满，本段译文未保存。';
      } else {
        response.cacheTicket = crypto.randomUUID();
        staged.set(response.cacheTicket, {ticket, payload, results, bytes,
          tabId: sender.tab.id, url: sender.url, expires: Date.now() + 300000});
      }
    }
    return response;
  }
  // Cache keys correspond to the original request positions, including failures.
  const completedTicket = {...ticket, keys: ticket.keys.filter((_, index) => results.has(payload.segments[index].id))};
  return {...response, ...await translationCache.commit(completedTicket, completed, results, signal)};
}

async function saveStaged(message, sender, signal) {
  pruneStaged();
  if (!Array.isArray(message.tickets) || !message.tickets.length) return {saved: false};
  if (message.tickets.length > 32 || new Set(message.tickets).size !== message.tickets.length) return {saved: false, warning: '重译保存请求无效。'};
  const entries = message.tickets.map(key => staged.get(key));
  if (entries.some(entry => !entry || entry.tabId !== sender.tab.id || entry.url !== sender.url ||
      entry.ticket.generation !== translationCache.generation || entry.payload.target !== entries[0]?.payload.target)) {
    return {saved: false, warning: '重译保存已过期或被清除，本段译文未保存。'};
  }
  message.tickets.forEach(key => staged.delete(key));
  const segments = entries.flatMap(entry => entry.payload.segments);
  if (new Set(segments.map(segment => segment.id)).size !== segments.length) return {saved: false, warning: '重译保存段落重复。'};
  const ticket = {...entries[0].ticket, keys: entries.flatMap(entry => entry.ticket.keys)};
  return translationCache.commit(ticket, segments, new Map(entries.flatMap(entry => [...entry.results])), signal);
}

async function bridge(path, payload, signal) {
  const {token} = await browser.storage.local.get('token');
  if (!token) throw new Error('请先在连接设置中粘贴 Qwen 配对密钥。');
  let response;
  try {
    response = await fetch(ENDPOINT + path, {
      method: payload ? 'POST' : 'GET',
      headers: {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
      body: payload ? JSON.stringify(payload) : undefined,
      signal, credentials: 'omit', redirect: 'error', cache: 'no-store',
    });
  } catch (error) {
    if (signal?.aborted) throw new Error('翻译已取消或等待超时。');
    throw new Error('无法连接 Qwen，请在桌面端启动“网页翻译”服务。');
  }
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '翻译服务返回错误。');
  return result;
}

function cancelTab(tabId) {
  for (const [key, entry] of pending) {
    if (entry.tabId === tabId) { entry.controller.abort(); pending.delete(key); }
  }
  for (const [key, entry] of staged) if (entry.tabId === tabId) staged.delete(key);
  updateKeepAlive();
}

async function controlTab(tab, mode, target) {
  if (!Number.isInteger(tab?.id) || tab.id < 0) return {error: '没有可翻译的网页。'};
  try {
    await browser.scripting.executeScript({target: {tabId: tab.id}, files: ['content.js']});
    return await browser.tabs.sendMessage(tab.id, {type: 'qwen:mode', mode, target});
  } catch (_) {
    return {error: '此页面无法注入翻译扩展，请打开普通网页（浏览器设置、扩展商店及内置 PDF 页面不支持）。'};
  }
}

browser.commands.onCommand.addListener(async (command, commandTab) => {
  if (command !== 'translate-page') return;
  let tab = commandTab;
  try {
    // Capture the invoking tab before reading preferences: switching tabs while
    // storage is pending must not translate a different page.
    if (!tab) [tab] = await browser.tabs.query({active: true, currentWindow: true});
    const {target} = await browser.storage.local.get('target');
    const result = await controlTab(tab, 'bilingual', target || 'zh-CN');
    if (result?.error) throw new Error(result.error);
    await browser.action.setBadgeText({tabId: tab.id, text: ''});
    await browser.action.setTitle({tabId: tab.id, title: 'Qwen 网页翻译'});
  } catch (error) {
    // Restricted pages cannot show our in-page status; use the toolbar instead.
    if (Number.isInteger(tab?.id) && tab.id >= 0) {
      await Promise.allSettled([
        browser.action.setBadgeText({tabId: tab.id, text: '!'}),
        browser.action.setTitle({tabId: tab.id, title: '翻译未启动：' + error.message}),
      ]);
    }
  }
});

browser.runtime.onMessage.addListener((message, sender) => {
  if (sender.id !== browser.runtime.id) return;
  const isUI = sender.url?.startsWith(browser.runtime.getURL(''));
  if (message.type === 'qwen:translate' && sender.tab && !isUI) {
    return (async () => {
      if (pending.size >= 6) return {error: '正在翻译的页面较多，请稍后重试。'};
      const key = Symbol();
      const controller = new AbortController();
      pending.set(key, {controller, tabId: sender.tab.id});
      updateKeepAlive();
      // Codex processes one whole page at a time across tabs. Waiting does not
      // consume the model timeout, and cancelled queue entries never call Codex.
      const run = queue.then(async () => {
        if (controller.signal.aborted) return {error: '翻译已取消。'};
        const timeout = setTimeout(() => controller.abort(), 255000);
        try { return await translate(message, sender, controller.signal); }
        catch (error) { return {error: error.message}; }
        finally { clearTimeout(timeout); }
      });
      queue = run.catch(() => {});
      try { return await run; }
      finally { pending.delete(key); updateKeepAlive(); }
    })();
  }
  if (message.type === 'qwen:cancel' && sender.tab && !isUI) {
    cancelTab(sender.tab.id);
    return Promise.resolve({ok: true});
  }
  if (message.type === 'qwen:cache-save' && sender.tab && !isUI) {
    const key = Symbol(), controller = new AbortController();
    pending.set(key, {controller, tabId: sender.tab.id});
    updateKeepAlive();
    return saveStaged(message, sender, controller.signal)
      .catch(() => ({saved: false, warning: '译文未能保存，请检查浏览器存储。'}))
      .finally(() => { pending.delete(key); updateKeepAlive(); });
  }
  if (message.type === 'qwen:cache-discard' && sender.tab && !isUI) {
    for (const key of Array.isArray(message.tickets) ? message.tickets : []) {
      const entry = staged.get(key);
      if (entry?.tabId === sender.tab.id && entry.url === sender.url) staged.delete(key);
    }
    return Promise.resolve({ok: true});
  }
  // Only extension UI can test the service or inject a script into a tab.
  if (!isUI) return;
  if (message.type === 'qwen:cache-settings') {
    return translationCache.settings().catch(() => ({error: '无法读取已保存译文，请尝试清除后重新翻译。'}));
  }
  if (message.type === 'qwen:cache-configure') {
    staged.clear();
    return translationCache.configure(message.enabled).catch(() => ({error: '无法保存设置，请检查浏览器存储。'}));
  }
  if (message.type === 'qwen:cache-clear') {
    staged.clear();
    return translationCache.clear().catch(() => ({error: '无法清除译文，请检查浏览器存储。'}));
  }
  if (message.type === 'qwen:health') {
    return bridge('/health', null, AbortSignal.timeout(5000)).catch(error => ({error: error.message}));
  }
  if (message.type === 'qwen:control') {
    return (async () => {
      const [tab] = await browser.tabs.query({active: true, currentWindow: true});
      return controlTab(tab, message.mode, message.target);
    })();
  }
});
browser.tabs.onRemoved.addListener(cancelTab);
browser.tabs.onUpdated.addListener((tabId, change) => { if (change.status === 'loading') cancelTab(tabId); });
