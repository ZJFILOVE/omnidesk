const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto').webcrypto;

const plain = value => JSON.parse(JSON.stringify(value));
const payload = (text = 'Hello', id = 's1', target = 'zh-CN') => ({target, title: 'Article', context: '',
  segments: [{id, html: `<t id="0">${text}</t>`}]});
function storage(initial = {}) {
  const data = {token: 'test-token', ...initial};
  return {data, get: async keys => Object.fromEntries((Array.isArray(keys) ? keys : [keys])
    .filter(key => Object.hasOwn(data, key)).map(key => [key, plain(data[key])])),
  set: async values => Object.assign(data, plain(values)), remove: async key => { delete data[key]; }};
}
function background({saved = storage(), automatic = false, limits = {}} = {}) {
  let listener, commandListener;
  let heartbeatId = 0, keepAliveCalls = 0;
  const heartbeats = new Map();
  const calls = [];
  const injections = [], controls = [], badges = [], titles = [];
  const extension = 'moz-extension://test-extension/';
  const browser = {
    runtime: {id: 'qwen@test', getURL: name => extension + name,
      getPlatformInfo: async () => { keepAliveCalls++; return {os: 'linux'}; },
      onMessage: {addListener: fn => { listener = fn; }}},
    storage: {local: saved},
    commands: {onCommand: {addListener: fn => { commandListener = fn; }}},
    scripting: {executeScript: async details => { injections.push(plain(details)); }},
    action: {setBadgeText: async details => { badges.push(plain(details)); },
      setTitle: async details => { titles.push(plain(details)); }},
    tabs: {onRemoved: {addListener() {}}, onUpdated: {addListener() {}},
      query: async () => [{id: 5}],
      sendMessage: async (tabId, message) => { controls.push({tabId, ...plain(message)}); return {ok: true}; }},
  };
  const context = vm.createContext({browser, crypto, TextEncoder, AbortController, AbortSignal, setTimeout, clearTimeout,
    setInterval: (fn, delay) => { const id = ++heartbeatId; heartbeats.set(id, {fn, delay}); return id; },
    clearInterval: id => heartbeats.delete(id),
    fetch: (url, options) => new Promise(resolve => {
      const original = options.body ? JSON.parse(options.body) : null;
      const translated = original ? {segments: original.segments.map(segment => ({id: segment.id,
        slots: Object.fromEntries([...segment.html.matchAll(/<t id="(\d+)">([^<]*)<\/t>/g)]
          .map(match => [match[1], '译：' + match[2]]))}))} : {ok: true};
      const call = {url, options, release: (result = translated) => resolve({ok: true, json: async () => result}),
        fail: (detail = 'provider failed') => resolve({ok: false, json: async () => ({detail})})};
      calls.push(call);
      if (automatic) call.release();
    })});
  for (const file of ['cache.js', 'background.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../firefox-extension', file), 'utf8'), context);
  }
  for (const [key, value] of Object.entries(limits)) vm.runInContext(`translationCache.${key} = ${Number(value)}`, context);
  const send = (type, tabId = 1, extra = {}, url = 'https://example.org/article', incognito = false) =>
    listener({type, payload: payload('Page ' + tabId), ...extra},
      {id: browser.runtime.id, tab: {id: tabId, incognito}, url});
  const ui = (type, extra = {}) => listener({type, ...extra}, {id: browser.runtime.id, url: extension + 'options.html'});
  return {listener, calls, send, ui, extension, saved, browser, command: commandListener,
    heartbeats, keepAliveCalls: () => keepAliveCalls,
    injections, controls, badges, titles};
}

const tick = () => new Promise(resolve => setImmediate(resolve));
async function waitFor(predicate) {
  const end = Date.now() + 3000;
  while (!predicate() && Date.now() < end) await new Promise(resolve => setTimeout(resolve, 2));
  assert.ok(predicate(), 'operation timed out');
}

test('partial model output preserves and caches valid paragraphs, retries only failures', async () => {
  const bg = background();
  const page = {target: 'zh-CN', segments: ['bad', 'body', 'abstract'].map(id =>
    ({id, html: `<t id="0">Text for ${id}</t>`}))};
  const first = bg.send('qwen:translate', 1, {payload: page});
  await waitFor(() => bg.calls.length === 1);
  bg.calls[0].release({segments: ['body', 'abstract'].map(id => ({id, slots: {'0': `译文 ${id}`}})),
    failedSegments: ['bad'], failure: 'Structure mismatch'});
  const result = await first;
  assert.deepEqual(plain(result.failedSegments), ['bad']);
  assert.equal(result.segments.length, 2);
  assert.equal(result.saved, true);
  const second = bg.send('qwen:translate', 1, {payload: page});
  await waitFor(() => bg.calls.length === 2);
  assert.deepEqual(JSON.parse(bg.calls[1].options.body).segments.map(item => item.id), ['bad']);
  bg.calls[1].release();
  const completed = await second;
  assert.equal(completed.cacheHits, 2);
  assert.equal(completed.segments.find(item => item.id === 'body').slots['0'], '译文 body');
});

test('slow and queued translations keep the background awake only until the last request finishes', async () => {
  const bg = background();
  assert.equal(bg.heartbeats.size, 0);
  const first = bg.send('qwen:translate', 1);
  const second = bg.send('qwen:translate', 2);
  await waitFor(() => bg.calls.length === 1);
  assert.equal(bg.heartbeats.size, 1, 'all tabs share one heartbeat');
  const heartbeat = [...bg.heartbeats.values()][0];
  assert.ok(heartbeat.delay < 30000);
  heartbeat.fn(); await tick();
  assert.equal(bg.keepAliveCalls(), 1);
  bg.calls[0].release(); await first;
  await waitFor(() => bg.calls.length === 2);
  assert.equal(bg.heartbeats.size, 1);
  bg.calls[1].release(); await second;
  assert.equal(bg.heartbeats.size, 0);
});

test('cancelling all work releases the heartbeat even before fetch settles; failures release it too', async () => {
  const bg = background();
  const first = bg.send('qwen:translate', 1);
  const second = bg.send('qwen:translate', 2);
  await waitFor(() => bg.calls.length === 1);
  await bg.send('qwen:cancel', 1);
  assert.equal(bg.heartbeats.size, 1);
  await bg.send('qwen:cancel', 2);
  assert.equal(bg.heartbeats.size, 0);
  bg.calls[0].release(); await Promise.all([first, second]);
  const failed = bg.send('qwen:translate', 3);
  await waitFor(() => bg.calls.length === 2);
  assert.equal(bg.heartbeats.size, 1);
  bg.calls[1].fail(); await failed;
  assert.equal(bg.heartbeats.size, 0);
});

test('install declares a translation shortcut; first use injects directly with default Chinese', async () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, '../firefox-extension/manifest.json')));
  assert.equal(manifest.commands['translate-page'].suggested_key.default, 'Alt+Shift+T');
  assert.ok(manifest.permissions.includes('activeTab'));
  const bg = background();
  await bg.command('translate-page');
  assert.deepEqual(bg.injections, [{target: {tabId: 5}, files: ['content.js']}]);
  assert.deepEqual(bg.controls, [{tabId: 5, type: 'qwen:mode', mode: 'bilingual', target: 'zh-CN'}]);
  assert.equal(bg.calls.length, 0, 'shortcut delegates to the existing content/cache flow');
});

test('shortcut preserves invoking tab and saved target while preferences load', async () => {
  const bg = background({saved: storage({target: 'ja'})});
  let release;
  const get = bg.saved.get;
  bg.saved.get = async keys => { await new Promise(resolve => { release = resolve; }); return get(keys); };
  bg.browser.tabs.query = async () => [{id: 8}];
  const invoked = bg.command('translate-page', {id: 3});
  await waitFor(() => release);
  bg.browser.tabs.query = async () => [{id: 9}];
  release(); await invoked;
  assert.deepEqual(bg.controls, [{tabId: 3, type: 'qwen:mode', mode: 'bilingual', target: 'ja'}]);
});

test('shortcut failure is visible on toolbar and the next success clears it', async () => {
  const bg = background();
  const inject = bg.browser.scripting.executeScript;
  bg.browser.scripting.executeScript = async () => { throw new Error('restricted URL'); };
  await bg.command('translate-page', {id: 4});
  assert.equal(bg.controls.length, 0);
  assert.deepEqual(bg.badges.at(-1), {tabId: 4, text: '!'});
  assert.match(bg.titles.at(-1).title, /普通网页/);
  bg.browser.scripting.executeScript = inject;
  await bg.command('translate-page', {id: 4});
  assert.deepEqual(bg.badges.at(-1), {tabId: 4, text: ''});
});

test('unknown shortcut and missing tab do not inject or leak rejections', async () => {
  const bg = background();
  await bg.command('unrelated-command');
  bg.browser.tabs.query = async () => [];
  await bg.command('translate-page');
  await bg.command('translate-page', {id: -1});
  assert.equal(bg.injections.length, 0);
  assert.equal(bg.controls.length, 0);
});

test('popup translation and shortcut share page controls; pages cannot invoke controls', async () => {
  const bg = background();
  assert.equal(bg.send('qwen:control', 1, {mode: 'bilingual'}), undefined);
  await bg.ui('qwen:control', {mode: 'translation', target: 'en'});
  assert.deepEqual(bg.controls, [{tabId: 5, type: 'qwen:mode', mode: 'translation', target: 'en'}]);
});

test('extension settings can test bridge and manage cache; page scripts cannot', async () => {
  const {listener, calls, extension, ui} = background();
  const response = listener({type: 'qwen:health'}, {id: 'qwen@test', tab: {id: 1}, url: extension + 'options.html'});
  await tick();
  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /\/health$/);
  assert.equal(calls[0].options.headers.Authorization, 'Bearer test-token');
  calls[0].release();
  assert.equal((await response).ok, true);
  for (const type of ['qwen:health', 'qwen:cache-clear', 'qwen:cache-configure']) {
    assert.equal(listener({type}, {id: 'qwen@test', tab: {id: 2}, url: 'https://example.org'}), undefined);
  }
  assert.equal((await ui('qwen:cache-settings')).enabled, true);
});

test('network requests remain serial; cancelled queued pages never reach model', async () => {
  const {calls, send} = background();
  const first = send('qwen:translate', 1);
  const cancelled = send('qwen:translate', 2);
  const third = send('qwen:translate', 3);
  await waitFor(() => calls.length === 1);
  await send('qwen:cancel', 2);
  calls[0].release();
  assert.equal((await first).segments.length, 1);
  assert.match((await cancelled).error, /取消/);
  await waitFor(() => calls.length === 2);
  calls[1].release();
  assert.equal((await third).segments.length, 1);
});

test('persistent entries survive background restart, remap ids and work without bridge', async () => {
  const saved = storage();
  const first = background({saved, automatic: true});
  assert.equal((await first.send('qwen:translate', 1, {payload: payload()})).saved, true);
  delete saved.data.token;
  const next = background({saved});
  const result = await next.send('qwen:translate', 9, {payload: payload('Hello', 'new-id')});
  assert.equal(result.cacheHits, 1);
  assert.equal(result.segments[0].id, 'new-id');
  assert.equal(result.segments[0].slots['0'], '译：Hello');
  assert.equal(next.calls.length, 0);
  const stored = JSON.stringify(saved.data.translationCache);
  assert.equal(stored.includes('https://'), false);
  assert.equal(stored.includes('<t '), false);
});

test('full trusted URL including query/hash, language and exact source isolate entries', async () => {
  const bg = background({automatic: true});
  const base = 'https://example.org/article';
  const requests = [[payload(), base], [payload(), base + '?page=2'], [payload(), base + '#part'],
    [payload('Hello', 's1', 'en'), base], [payload('Hello!'), base]];
  for (const [data, url] of requests) {
    const result = await bg.send('qwen:translate', 1, {payload: {...data, url: base}}, url);
    assert.equal(result.cacheHits, 0);
  }
  assert.equal(bg.calls.length, requests.length);
});

test('partial hits request only changed segments and preserve original order', async () => {
  const bg = background({automatic: true});
  await bg.send('qwen:translate', 1, {payload: payload()});
  const data = payload('Changed', 'second');
  data.segments.unshift(payload('Hello', 'first').segments[0]);
  const result = await bg.send('qwen:translate', 1, {payload: data});
  assert.equal(result.cacheHits, 1);
  assert.deepEqual(JSON.parse(bg.calls[1].options.body).segments.map(segment => segment.id), ['second']);
  assert.deepEqual(plain(result.segments).map(segment => segment.id), ['first', 'second']);
});

test('failed force and invalid model structures retain previous good translation', async () => {
  const bg = background();
  const first = bg.send('qwen:translate', 1);
  await waitFor(() => bg.calls.length === 1); bg.calls[0].release(); await first;
  for (const invalid of [null, {segments: [{id: 's1', slots: {'1': 'wrong'}}]}]) {
    const forced = bg.send('qwen:translate', 1, {force: true});
    await waitFor(() => bg.calls.length === (invalid ? 3 : 2));
    const call = bg.calls.at(-1);
    if (invalid) call.release(invalid); else call.fail();
    assert.ok((await forced).error);
  }
  const cached = await bg.send('qwen:translate', 1);
  assert.equal(cached.cacheHits, 1);
  assert.equal(cached.segments[0].slots['0'], '译：Page 1');
});

test('successful force replaces prior persisted result', async () => {
  const bg = background();
  let request = bg.send('qwen:translate', 1);
  await waitFor(() => bg.calls.length === 1); bg.calls[0].release(); await request;
  request = bg.send('qwen:translate', 1, {force: true});
  await waitFor(() => bg.calls.length === 2);
  bg.calls[1].release({segments: [{id: 's1', slots: {'0': '更好的译文'}}]});
  assert.equal((await request).saved, true);
  assert.equal((await bg.send('qwen:translate', 1)).segments[0].slots['0'], '更好的译文');
});

test('LRU count and byte budgets evict old entries', async () => {
  const bg = background({automatic: true, limits: {maxEntries: 2}});
  for (const text of ['A', 'B', 'A', 'C']) await bg.send('qwen:translate', 1, {payload: payload(text)});
  assert.equal((await bg.send('qwen:translate', 1, {payload: payload('A')})).cacheHits, 1);
  assert.equal((await bg.send('qwen:translate', 1, {payload: payload('B')})).cacheHits, 0);
  assert.equal(bg.saved.data.translationCache.entries.length, 2);
  const tiny = background({automatic: true, limits: {maxBytes: 100}});
  const result = await tiny.send('qwen:translate', 1);
  assert.equal(result.saved, false);
  assert.match(result.warning, /容量/);
  assert.equal(tiny.saved.data.translationCache.entries.length, 0);
});

test('disabled saving and private windows do not read or persist translations', async () => {
  const bg = background({automatic: true});
  await bg.send('qwen:translate', 1);
  const before = JSON.stringify(bg.saved.data.translationCache);
  const privateResult = await bg.send('qwen:translate', 1, {}, undefined, true);
  assert.equal(privateResult.cacheHits, 0); assert.equal(privateResult.saved, false);
  assert.equal(JSON.stringify(bg.saved.data.translationCache), before);
  await bg.ui('qwen:cache-configure', {enabled: false});
  const disabled = await bg.send('qwen:translate', 1);
  assert.equal(disabled.cacheHits, 0); assert.equal(disabled.saved, false);
  assert.equal(JSON.stringify(bg.saved.data.translationCache), before);
});

test('clearing does not remove settings or permit an in-flight result to repopulate cache', async () => {
  const bg = background({saved: storage({target: 'ja'})});
  const request = bg.send('qwen:translate', 1);
  await waitFor(() => bg.calls.length === 1);
  await bg.ui('qwen:cache-clear');
  bg.calls[0].release();
  assert.equal((await request).saved, false);
  assert.equal(bg.saved.data.translationCache, undefined);
  assert.equal(bg.saved.data.token, 'test-token'); assert.equal(bg.saved.data.target, 'ja');
});

test('cancelled active response never overwrites cache', async () => {
  const bg = background();
  const request = bg.send('qwen:translate', 1);
  await waitFor(() => bg.calls.length === 1);
  await bg.send('qwen:cancel', 1); bg.calls[0].release();
  assert.match((await request).error, /取消/);
  assert.equal(bg.saved.data.translationCache, undefined);
});

test('storage write failure still returns translation with unsaved warning', async () => {
  const saved = storage();
  saved.set = async () => { throw new Error('quota exceeded'); };
  const bg = background({saved, automatic: true});
  const result = await bg.send('qwen:translate', 1);
  assert.equal(result.segments.length, 1); assert.equal(result.saved, false);
  assert.match(result.warning, /未能保存/);
});

test('concurrent tabs preserve both cache writes', async () => {
  const bg = background({automatic: true});
  await Promise.all([bg.send('qwen:translate', 1), bg.send('qwen:translate', 2)]);
  assert.equal(bg.saved.data.translationCache.entries.length, 2);
});

test('deferred multipart force only replaces cache after atomic ticket commit', async () => {
  const bg = background();
  async function translated(text, prefix, extra = {}) {
    const count = bg.calls.length;
    const request = bg.send('qwen:translate', 1, {payload: payload(text, text), ...extra});
    await waitFor(() => bg.calls.length === count + 1);
    bg.calls.at(-1).release({segments: [{id: text, slots: {'0': prefix + text}}]});
    return request;
  }
  for (const text of ['A', 'B']) await translated(text, '旧：');
  const before = JSON.stringify(bg.saved.data.translationCache);
  const tickets = [];
  for (const text of ['A', 'B']) {
    const result = await translated(text, '新：', {force: true, deferSave: true});
    tickets.push(result.cacheTicket);
    assert.equal(result.saved, false);
    assert.equal(JSON.stringify(bg.saved.data.translationCache), before);
  }
  assert.equal((await bg.send('qwen:cache-save', 2, {tickets})).saved, false);
  assert.equal((await bg.send('qwen:cache-save', 1, {tickets})).saved, true);
  assert.equal(bg.saved.data.translationCache.entries.length, 2);
  for (const text of ['A', 'B']) {
    assert.equal((await bg.send('qwen:translate', 1, {payload: payload(text, text)})).segments[0].slots['0'], '新：' + text);
  }
});

test('discarded and cleared deferred results cannot overwrite existing cache', async () => {
  const bg = background({automatic: true});
  await bg.send('qwen:translate', 1);
  const before = JSON.stringify(bg.saved.data.translationCache);
  let result = await bg.send('qwen:translate', 1, {force: true, deferSave: true});
  await bg.send('qwen:cache-discard', 1, {tickets: [result.cacheTicket]});
  assert.equal((await bg.send('qwen:cache-save', 1, {tickets: [result.cacheTicket]})).saved, false);
  assert.equal(JSON.stringify(bg.saved.data.translationCache), before);
  result = await bg.send('qwen:translate', 1, {force: true, deferSave: true});
  await bg.ui('qwen:cache-clear');
  assert.equal((await bg.send('qwen:cache-save', 1, {tickets: [result.cacheTicket]})).saved, false);
  assert.equal(bg.saved.data.translationCache, undefined);
});

test('partial offline failure still returns persisted segments for reading', async () => {
  const bg = background();
  let request = bg.send('qwen:translate', 1, {payload: payload()});
  await waitFor(() => bg.calls.length === 1); bg.calls[0].release(); await request;
  const data = payload('New text', 'new');
  data.segments.unshift(payload('Hello', 'cached').segments[0]);
  request = bg.send('qwen:translate', 1, {payload: data});
  await waitFor(() => bg.calls.length === 2); bg.calls[1].fail('本地服务已关闭');
  const result = await request;
  assert.equal(result.cacheHits, 1);
  assert.deepEqual(plain(result.segments).map(segment => segment.id), ['cached']);
  assert.deepEqual(plain(result.failedSegments), ['new']);
  assert.equal(result.failure, '本地服务已关闭');
  assert.equal(result.saved, true);
});

test('cache read failure permits live translation and reports persistence problem', async () => {
  const saved = storage(), get = saved.get;
  saved.get = async keys => {
    if (Array.isArray(keys)) throw new Error('cache inaccessible');
    return get(keys);
  };
  const bg = background({saved, automatic: true});
  const result = await bg.send('qwen:translate', 1);
  assert.equal(result.segments.length, 1);
  assert.equal(result.saved, false);
  assert.match(result.warning, /无法读取/);
});

test('cancellation during a pending storage write restores the prior translation', async () => {
  const bg = background({automatic: true});
  await bg.send('qwen:translate', 1);
  const before = JSON.stringify(bg.saved.data.translationCache);
  const set = bg.saved.set;
  let release, entered = false;
  const pause = new Promise(resolve => { release = resolve; });
  bg.saved.set = async values => { entered = true; await pause; return set(values); };
  const request = bg.send('qwen:translate', 1, {force: true});
  await waitFor(() => entered);
  await bg.send('qwen:cancel', 1);
  release();
  assert.equal((await request).saved, false);
  assert.equal(JSON.stringify(bg.saved.data.translationCache), before);
});
