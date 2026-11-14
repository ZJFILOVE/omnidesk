"""Exercise the real content/background/cache scripts together in Firefox.

Only the extension storage transport and model response are simulated.
"""
import json
import os
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(ROOT / 'runtime/browsers'))

BACKGROUND = r'''() => {
  window.networkCalls = []; window.modelOnline = true; window.translationVersion = '译一：';
  window.browser = {
    runtime: {id: 'qwen-web-translator@local.zhu', getURL: p => 'moz-extension://audit/' + p,
      onMessage: {addListener: listener => { window.backgroundMessage = listener; }}},
    storage: {local: {
      get: async keys => {
        const all = await storageRead();
        if (keys == null) return all;
        if (typeof keys === 'string') return {[keys]: all[keys]};
        if (Array.isArray(keys)) return Object.fromEntries(keys.map(k => [k, all[k]]));
        return {...keys, ...Object.fromEntries(Object.keys(keys).filter(k => k in all).map(k => [k, all[k]]))};
      },
      set: items => storageWrite(items), remove: keys => storageRemove(keys),
    }, onChanged: {addListener() {}}},
    tabs: {onRemoved: {addListener() {}}, onUpdated: {addListener() {}}},
    commands: {onCommand: {addListener() {}}},
  };
  window.fetch = async (url, options) => {
    const payload = JSON.parse(options.body);
    networkCalls.push(payload);
    if (!modelOnline) throw new Error('Fixture model offline');
    return new Response(JSON.stringify({segments: payload.segments.map(segment => {
      const doc = new DOMParser().parseFromString('<root>' + segment.html + '</root>', 'application/xml');
      return {id: segment.id, slots: Object.fromEntries([...doc.querySelectorAll('t')].map(t =>
        [t.getAttribute('id'), translationVersion + t.textContent]))};
    })}), {status: 200, headers: {'Content-Type': 'application/json'}});
  };
}'''

CONTENT = r'''() => {
  const attach = Element.prototype.attachShadow;
  Element.prototype.attachShadow = function(options) { return attach.call(this, {...options, mode: 'open'}); };
  window.browser = {runtime: {
    onMessage: {addListener: listener => { window.setMode = mode => listener({type:'qwen:mode', mode, target:'zh-CN'}); }},
    sendMessage: message => forwardMessage(message),
  }};
}'''


@unittest.skipUnless(os.environ.get('QWEN_FIREFOX_TESTS') == '1', 'Optional real Firefox integration tests')
class SavedPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        self.engine = await async_playwright().start()
        self.browser = await self.engine.firefox.launch(headless=True, timeout=15000)
        self.context = await self.browser.new_context()
        await self.context.route('https://saved-translation.test/**',
                                 lambda route: route.fulfill(content_type='text/html', body='<html><body></body></html>'))
        self.saved = {'token': 'fixture-pairing-token-not-a-real-secret'}
        self.background = None
        await self.start_background()
        await self.open_article()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.engine.stop()

    def write_storage(self, items):
        self.saved.update(items)

    def remove_storage(self, keys):
        for key in [keys] if isinstance(keys, str) else keys:
            self.saved.pop(key, None)

    async def start_background(self):
        if self.background:
            await self.background.close()
        self.background = await self.context.new_page()
        await self.background.goto('https://saved-translation.test/background')
        await self.background.expose_function('storageRead', lambda: self.saved)
        await self.background.expose_function('storageWrite', self.write_storage)
        await self.background.expose_function('storageRemove', self.remove_storage)
        await self.background.evaluate(BACKGROUND)
        manifest = json.loads((ROOT / 'firefox-extension/manifest.json').read_text())
        for name in manifest['background']['scripts']:
            await self.background.add_script_tag(path=ROOT / 'firefox-extension' / name)

    async def open_article(self, second='Second paragraph.'):
        self.page = await self.context.new_page()
        await self.page.goto('https://saved-translation.test/article?edition=1#intro')
        await self.page.set_content('<html><head><title>Saved article</title></head><body>'
                                    '<p id="first">Read <a href="https://example.org">the article</a>.</p>'
                                    '<p id="second"></p></body></html>')
        await self.page.locator('#second').evaluate('(element, text) => { element.textContent = text; }', second)
        page = self.page
        async def forward(message):
            return await self.background.evaluate('''({message, url}) => backgroundMessage(message, {
                id: browser.runtime.id, tab: {id: 7, incognito: false}, url})''', {'message': message, 'url': page.url})
        await page.expose_function('forwardMessage', forward)
        await page.evaluate(CONTENT)
        await page.add_script_tag(path=ROOT / 'firefox-extension/content.js')

    async def translated(self):
        await self.page.locator('#first [data-qwen-owned="translation"]').wait_for(timeout=10000)
        await self.page.locator('#second [data-qwen-owned="translation"]').wait_for(timeout=10000)

    async def test_saved_page_restores_without_model_after_background_restart(self):
        await self.page.evaluate('setMode("bilingual")')
        await self.translated()
        self.assertEqual(await self.background.evaluate('networkCalls.length'), 1)
        await self.page.evaluate('setMode("off")')
        await self.start_background()
        await self.background.evaluate('modelOnline = false')
        await self.open_article()
        await self.page.evaluate('setMode("bilingual")')
        await self.translated()
        self.assertEqual(await self.background.evaluate('networkCalls.length'), 0)
        self.assertIn('译一：', await self.page.locator('#first [data-qwen-owned="translation"] div').inner_text())

    async def test_changed_paragraph_only_calls_model_for_changed_source(self):
        await self.page.evaluate('setMode("bilingual")')
        await self.translated()
        await self.page.evaluate('setMode("off")')
        await self.open_article(second='Updated paragraph.')
        await self.background.evaluate('networkCalls = []; translationVersion = "译二："')
        await self.page.evaluate('setMode("bilingual")')
        await self.translated()
        calls = await self.background.evaluate('networkCalls')
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]['segments']), 1)
        self.assertIn('Updated paragraph.', calls[0]['segments'][0]['html'])
        self.assertIn('译一：', await self.page.locator('#first [data-qwen-owned="translation"] div').inner_text())
        self.assertIn('译二：', await self.page.locator('#second [data-qwen-owned="translation"] div').inner_text())

    async def test_partial_saved_page_is_readable_while_new_content_is_offline(self):
        await self.page.evaluate('setMode("bilingual")')
        await self.translated()
        await self.page.evaluate('setMode("off")')
        await self.open_article(second='New uncached paragraph.')
        await self.background.evaluate('modelOnline = false; networkCalls = []')
        await self.page.evaluate('setMode("bilingual")')
        await self.page.locator('#first [data-qwen-owned="translation"]').wait_for(timeout=10000)
        await self.page.get_by_text('无法连接 Qwen', exact=False).wait_for()
        self.assertEqual(await self.page.locator('#second [data-qwen-owned="translation"]').count(), 0)
        self.assertEqual(await self.page.locator('#second').inner_text(), 'New uncached paragraph.')
        await self.background.evaluate('modelOnline = true; networkCalls = []')
        await self.page.get_by_role('button', name='重试', exact=True).click()
        await self.translated()
        calls = await self.background.evaluate('networkCalls')
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]['segments']), 1)
        self.assertIn('New uncached paragraph.', calls[0]['segments'][0]['html'])

    async def test_paragraph_revision_replaces_saved_version_only_after_success(self):
        await self.page.evaluate('setMode("bilingual")')
        await self.translated()
        await self.background.evaluate('modelOnline = false; networkCalls = []')
        button = self.page.locator('#first').get_by_role('button', name='重新翻译本段')
        await button.click()
        await self.page.locator('#first').get_by_text('重译失败，已保留原译文', exact=True).wait_for()
        self.assertIn('译一：', await self.page.locator('#first [data-qwen-owned="translation"] div').inner_text())
        await self.background.evaluate('modelOnline = true; translationVersion = "新译："; networkCalls = []')
        await button.click()
        await self.page.wait_for_function('''() => document.querySelector('#first [data-qwen-owned="translation"]')
            .shadowRoot.querySelector('div').textContent.includes('新译：')''')
        calls = await self.background.evaluate('networkCalls')
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]['segments']), 1)
        await self.page.evaluate('setMode("off")')
        await self.start_background()
        await self.background.evaluate('modelOnline = false')
        await self.open_article()
        await self.page.evaluate('setMode("bilingual")')
        await self.translated()
        self.assertIn('新译：', await self.page.locator('#first [data-qwen-owned="translation"] div').inner_text())
        self.assertIn('译一：', await self.page.locator('#second [data-qwen-owned="translation"] div').inner_text())
        self.assertEqual(await self.background.evaluate('networkCalls.length'), 0)
