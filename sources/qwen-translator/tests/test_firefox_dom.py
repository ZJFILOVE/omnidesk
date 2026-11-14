"""Run with QWEN_FIREFOX_TESTS=1 after installing the Playwright Firefox runtime."""
import os
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(ROOT / 'runtime/browsers'))

SHIM = '''() => {
  window.requests = []; window.messages = []; window.commits = []; window.discards = []; window.cancels = 0; window.delay = 0;
  const original = Element.prototype.attachShadow;
  Element.prototype.attachShadow = function(options) { return original.call(this, {...options, mode: 'open'}); };
  window.browser = {runtime: {
    onMessage: {addListener: fn => { window.setMode = (mode, target = 'zh-CN') => fn({type:'qwen:mode', mode, target}); }},
    sendMessage: async message => {
      if (message.type === 'qwen:cancel') { window.cancels++; return {ok:true}; }
      if (message.type === 'qwen:cache-save') { window.commits.push(message); return {saved: window.saved ?? true}; }
      if (message.type === 'qwen:cache-discard') { window.discards.push(message); return {ok:true}; }
      window.messages.push(message); window.requests.push(message.payload);
      const prefix = message.force ? '新：' : '译：';
      const forcedCount = window.messages.filter(item => item.force).length;
      await new Promise(resolve => setTimeout(resolve, message.force ? (window.forceDelay || window.delay) : window.delay));
      if (window.fail || (message.force && window.failForceAt === forcedCount)) return {error:'测试连接失败'};
      const failedSegments = message.payload.segments.filter(segment => window.offlineFragment && segment.html.includes(window.offlineFragment)).map(segment => segment.id);
      return {failedSegments, failure: failedSegments.length ? '模型离线，未缓存段落暂时无法翻译' : undefined, cacheTicket: message.deferSave && window.noTicketAt !== forcedCount ? `ticket${window.requests.length}` : undefined, cacheHits: window.cacheHits || 0, saved: window.saved, warning: window.cacheWarning, segments:message.payload.segments.filter(segment => !failedSegments.includes(segment.id)).map(segment => {
        if (window.badSegment === segment.id) return {id:segment.id, slots:{}};
        const doc = new DOMParser().parseFromString('<root>' + segment.html + '</root>', 'application/xml');
        return {id:segment.id, slots:Object.fromEntries([...doc.querySelectorAll('t')].map(t => [t.id, prefix + t.textContent]))};
      })};
    }
  }};
}'''


@unittest.skipUnless(os.environ.get('QWEN_FIREFOX_TESTS') == '1', 'Optional real Firefox DOM tests')
class FirefoxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        self.engine = await async_playwright().start()
        self.browser = await self.engine.firefox.launch(headless=True, timeout=15000)
        self.page = await self.browser.new_page(viewport={'width': 1200, 'height': 800})
        await self.page.set_content('''<!doctype html><html><head><title>Test article</title></head><body>
            <h1>A page title</h1><p id="article">Read <a id="link" href="https://example.org"><strong>the article</strong></a> today.</p>
            <div id="mixed">Loose introduction to the article<p>Nested paragraph with meaningful content</p>Loose conclusion of the article</div>
            <pre id="code">Do not translate this code</pre><div contenteditable="true">My draft</div>
            <p translate="no">Keep original</p><input value="private input"><p hidden>Hidden text</p>
            <div style="height:2000px"></div><p id="bottom">Below the fold is more article content.</p></body></html>''')
        await self.page.evaluate(SHIM)
        await self.page.add_script_tag(path=ROOT / 'firefox-extension/content.js')

    async def asyncTearDown(self):
        await self.browser.close()
        await self.engine.stop()

    async def done(self, selector='#article'):
        await self.page.locator(selector + ' [data-qwen-owned="translation"]').wait_for(timeout=10000)

    async def test_filters_widgets_and_short_labels_but_preserves_article_structure(self):
        await self.page.evaluate('''() => {
          document.body.innerHTML = `<nav><h2>Site navigation</h2><p>Browse all our latest research articles</p></nav>
            <div role="toolbar"><a href="#">Download this paper in PDF format</a></div>
            <div role="button">Sign in with your university account</div>
            <form><p>Enter your email address to subscribe</p></form>
            <div><a href="#">Share</a><span>PDF</span><span>Tools</span></div>
            <p><a href="#">Read more.</a></p><div>相关推荐</div><div>12345</div>
            <article><header><h1>Light sensing</h1></header><h2>Results</h2>
              <p id="article">Read <a href="#"><strong>this</strong></a> paper to understand how plants sense light.</p>
              <p id="short">It works.</p><p id="cjk">结果很明确。</p>
              <div id="cjk-long">这是一段没有空格的研究正文内容</div>
              <figcaption>Light response</figcaption><div id="growing">More</div></article>`;
          setMode('bilingual');
        }''')
        await self.done()
        await self.done('#short')
        await self.done('#cjk')
        await self.done('#cjk-long')
        requests = await self.page.evaluate('requests')
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(requests[0]['segments']), 7)
        text = str(requests)
        for excluded in ['Site navigation', 'Browse all', 'Download this', 'Sign in', 'Enter your email',
                         'Share', 'PDF', 'Tools', 'Read more', '相关推荐', '12345', '>More<']:
            self.assertNotIn(excluded, text)
        for included in ['Light sensing', 'Results', 'Light response', '>this<', 'It works.', '结果很明确']:
            self.assertIn(included, text)
        await self.page.evaluate('document.querySelector("#growing").textContent = "New research content loaded by the website."')
        await self.done('#growing')
        self.assertEqual(await self.page.evaluate('requests[1].segments.length'), 1)
        await self.page.evaluate('setMode("translation")')
        self.assertEqual(await self.page.locator('[role="button"]').text_content(), 'Sign in with your university account')
        await self.page.evaluate('setMode("off")')
        self.assertEqual(await self.page.locator('#article').text_content(), 'Read this paper to understand how plants sense light.')

    async def test_page_with_only_short_controls_does_not_call_model(self):
        await self.page.evaluate('''() => {
          document.body.innerHTML='<nav>Home</nav><div>PDF</div><span>Share</span><label>Search</label>';
          setMode('bilingual');
        }''')
        await self.page.get_by_text('未找到需要翻译的内容', exact=False).wait_for()
        self.assertEqual(await self.page.evaluate('requests.length'), 0)

    async def test_publisher_article_keeps_paragraphs_with_inline_block_citations(self):
        await self.page.evaluate('''() => {
          document.body.innerHTML = `<h1>Chemical mechanism of light sensing</h1>
            <div><h2>Article Metrics</h2><p>Sign up to follow this journal by email.</p></div>
            <aside><h2>Cited By</h2><p>These other articles may also interest you.</p></aside>
            <section class="abstract"><h2>Abstract</h2><p id="abstract">Phytochromes are light-sensitive proteins
              found in plants, fungi and bacteria. The chromophore switches between two states in darkness.</p></section>
            <div class="article-body"><h2>Introduction</h2>
              ${Array.from({length:15}, (_, i) => `<p id="p${i}">The chromophore in this protein
                <span style="display:inline-block"><em>Pr</em></span> changes its conformation in darkness
                <sup style="display:inline-block"><a href="#ref1">(1)</a></sup>.
                These measurements explain how the protein controls the reaction.</p>`).join('')}
              <section class="references"><h2>References</h2>${'<p>Author. Title of another research paper. Journal. 2025.</p>'.repeat(240)}</section>
            </div><footer><p>Subscribe to receive all our latest email updates.</p></footer>`;
          setMode('bilingual');
        }''')
        await self.done('#abstract')
        await self.done('#p14')
        requests = await self.page.evaluate('requests')
        segments = [segment for request in requests for segment in request['segments']]
        self.assertEqual(len(segments), 19)
        for excluded in ['Article Metrics', 'Cited By', 'Sign up', 'References', 'Author.', 'Subscribe']:
            self.assertNotIn(excluded, str(segments))
        paragraphs = [segment for segment in segments if 'The chromophore in this protein' in segment['html']]
        self.assertEqual(len(paragraphs), 15)
        for segment in paragraphs:
            self.assertIn('<em>', segment['html'])
            self.assertIn('(1)', segment['html'])
            self.assertIn('These measurements', segment['html'])
        self.assertEqual(await self.page.locator('#p0 > [data-qwen-owned]').count(), 1)
        await self.page.evaluate('setMode("translation"); setMode("off")')
        self.assertEqual(await self.page.locator('#p0 sup a').text_content(), '(1)')

    async def test_disconnected_background_pauses_batches_and_retry_recovers(self):
        await self.page.evaluate('''() => {
          const send = browser.runtime.sendMessage;
          window.transportCalls = 0;
          window.disconnected = true;
          browser.runtime.sendMessage = message => {
            if (message.type === 'qwen:translate') {
              transportCalls++;
              if (disconnected) return Promise.reject(new Error('Could not establish connection. Receiving end does not exist.'));
            }
            return send(message);
          };
          const long = document.createElement('p'); long.textContent = 'Long sentence. '.repeat(10000);
          document.body.append(long);
          setMode('bilingual');
        }''')
        status = self.page.locator('[data-qwen-owned="status"] [role="status"]')
        await self.page.wait_for_function('document.querySelector("[data-qwen-owned=status]")?.shadowRoot.textContent.includes("扩展连接已断开")')
        self.assertIn('请刷新网页', await status.text_content())
        self.assertNotIn('段失败', await status.text_content())
        await self.page.evaluate('window.dispatchEvent(new Event("scroll"))')
        await self.page.wait_for_timeout(700)
        self.assertEqual(await self.page.evaluate('transportCalls'), 1)
        await self.page.evaluate('disconnected = false')
        await self.page.locator('[data-qwen-owned="status"]').get_by_role('button', name='重试', exact=True).click()
        await self.done()
        await self.done('#bottom')

    async def test_restore_works_when_invalidated_runtime_throws_synchronously(self):
        original = await self.page.locator('#article').inner_html()
        await self.page.evaluate('setMode("translation")')
        await self.done()
        errors = []
        self.page.on('pageerror', lambda error: errors.append(str(error)))
        await self.page.evaluate('''() => {
          browser.runtime.sendMessage = () => { throw new Error('Extension context invalidated.'); };
        }''')
        await self.page.locator('[data-qwen-owned="status"]').get_by_role('button', name='恢复原文').click()
        self.assertEqual(await self.page.locator('#article').inner_html(), original)
        self.assertEqual(await self.page.locator('[data-qwen-owned]').count(), 0)
        self.assertEqual(errors, [])

    async def test_bilingual_html_modes_and_exact_restore(self):
        original = await self.page.locator('#article').inner_html()
        await self.page.evaluate('setMode("bilingual")')
        await self.done()
        self.assertEqual(await self.page.locator('#link strong').text_content(), 'the article')
        self.assertEqual(await self.page.locator('#article [data-qwen-owned] a').get_attribute('href'), 'https://example.org/')
        self.assertIn('译：the article', await self.page.locator('#article [data-qwen-owned] strong').text_content())
        await self.page.evaluate('setMode("translation")')
        self.assertEqual(await self.page.locator('#link strong').text_content(), '译：the article')
        await self.page.evaluate('setMode("off")')
        self.assertEqual(await self.page.locator('#article').inner_html(), original)
        self.assertEqual(await self.page.locator('[data-qwen-owned]').count(), 0)

    async def test_whole_page_in_one_request_with_exclusions_and_mixed_html(self):
        await self.page.evaluate('setMode("bilingual")')
        await self.done()
        await self.page.wait_for_timeout(1500)
        text = str(await self.page.evaluate('requests'))
        for excluded in ['Do not translate', 'My draft', 'Keep original', 'private input', 'Hidden text']:
            self.assertNotIn(excluded, text)
        for included in ['Loose introduction', 'Nested paragraph', 'Loose conclusion', 'Below the fold']:
            self.assertIn(included, text)
        self.assertEqual(await self.page.evaluate('requests.length'), 1)
        self.assertGreater(await self.page.evaluate('requests[0].segments.length'), 4)
        await self.done('#bottom')

    async def test_dynamic_updates_and_late_cancel(self):
        await self.page.evaluate('setMode("translation")')
        await self.page.wait_for_function('document.querySelector("#link strong").textContent.startsWith("译：")')
        await self.page.evaluate('document.querySelector("#link strong").textContent = "New article"')
        await self.page.wait_for_function('document.querySelector("#link strong").textContent === "译：New article"')
        await self.page.evaluate('setMode("off")')
        self.assertEqual(await self.page.locator('#link strong').text_content(), 'New article')
        await self.page.evaluate('delay = 800; setMode("bilingual")')
        await self.page.wait_for_timeout(350)
        await self.page.evaluate('setMode("off")')
        await self.page.wait_for_timeout(1000)
        self.assertEqual(await self.page.locator('[data-qwen-owned]').count(), 0)

    async def test_oversized_paragraphs_split_and_restore_inline_structure(self):
        await self.page.evaluate("""() => {
          const huge = document.createElement('p'); huge.id = 'huge';
          const link = document.createElement('a'); link.href = 'https://example.org/long';
          link.textContent = 'Long sentence & <symbols> 😀. '.repeat(5000);
          huge.append(link); document.body.prepend(huge);
          const many = document.createElement('p'); many.id = 'many';
          for (let i = 0; i < 260; i++) {
            const span = document.createElement('strong'); span.textContent = `Word ${i} `; many.append(span);
          }
          huge.after(many);
          window.walks = 0;
          const walk = document.createTreeWalker.bind(document);
          document.createTreeWalker = (...args) => { window.walks++; return walk(...args); };
        }""")
        original = await self.page.locator('#huge').inner_html()
        many_original = await self.page.locator('#many').inner_html()
        await self.page.evaluate('setMode("bilingual")')
        await self.done('#huge')
        await self.done('#many')
        await self.done('#bottom')
        requests = await self.page.evaluate('requests')
        self.assertGreater(len(requests), 1)
        for request in requests:
            self.assertLessEqual(sum(len(item['html']) for item in request['segments']), 64000)
            for item in request['segments']:
                self.assertLessEqual(len(item['html']), 12000)
                self.assertLessEqual(item['html'].count('<t id='), 200)
        self.assertEqual(await self.page.locator('#huge [data-qwen-owned] a').get_attribute('href'),
                         'https://example.org/long')
        self.assertEqual(await self.page.locator('#many [data-qwen-owned] strong').count(), 260)
        await self.page.evaluate('setMode("translation")')
        translated = await self.page.locator('#huge > a').text_content()
        self.assertEqual(translated.replace('译：', ''), 'Long sentence & <symbols> 😀. ' * 5000)
        await self.page.evaluate('window.dispatchEvent(new Event("scroll"))')
        await self.page.wait_for_timeout(400)
        self.assertEqual(await self.page.evaluate('walks'), 1)
        await self.page.evaluate('setMode("off")')
        self.assertEqual(await self.page.locator('#huge').inner_html(), original)
        self.assertEqual(await self.page.locator('#many').inner_html(), many_original)

    async def test_split_paragraph_retry_preserves_successful_parts(self):
        await self.page.evaluate("""() => {
          document.querySelector('#article').textContent = 'A long sentence. '.repeat(2000);
          delay = 350; setMode('bilingual');
        }""")
        await self.page.wait_for_function('requests.length > 0')
        await self.page.evaluate('badSegment = requests[0].segments[1].id')
        await self.page.get_by_text('译文文字编号不完整', exact=False).wait_for()
        await self.done('#bottom')
        await self.page.evaluate('badSegment = null; delay = 0')
        await self.page.get_by_role('button', name='重试', exact=True).click()
        await self.done()
        self.assertEqual(await self.page.evaluate('requests.at(-1).segments.length'), 1)
        await self.page.evaluate('setMode("translation"); setMode("off")')
        self.assertEqual(await self.page.locator('#article').text_content(), 'A long sentence. ' * 2000)

    async def test_retranslate_one_paragraph_in_both_modes_preserves_old_on_failure(self):
        original = await self.page.locator('#article').inner_html()
        for mode in ['bilingual', 'translation']:
            with self.subTest(mode=mode):
                await self.page.evaluate(f'setMode("{mode}")')
                await self.done()
                before = await self.page.evaluate('requests.length')
                other = await self.page.locator('#bottom').inner_html()
                await self.page.evaluate('fail = true; forceDelay = 300')
                button = self.page.locator('#article').get_by_role('button', name='重新翻译本段')
                await button.click()
                self.assertTrue(await button.is_disabled())
                self.assertIn('译：the article', await self.page.locator('#article strong').all_text_contents())
                await self.page.locator('#article').get_by_text('重译失败，已保留原译文', exact=True).wait_for()
                self.assertIn('译：the article', await self.page.locator('#article strong').all_text_contents())
                self.assertNotIn('新：', await self.page.locator('#article').inner_html())
                await self.page.evaluate('fail = false; forceDelay = 0')
                await button.click()
                await self.page.wait_for_function("document.querySelector('#article [data-qwen-owned]').shadowRoot.textContent.includes('新：the article') || document.querySelector('#link strong').textContent === '新：the article'")
                self.assertEqual(await self.page.evaluate('requests.length'), before + 2)
                forced = await self.page.evaluate('messages.filter(message => message.force).slice(-2)')
                for message in forced:
                    self.assertEqual(len(message['payload']['segments']), 1)
                    self.assertIn('the article', message['payload']['segments'][0]['html'])
                    self.assertNotIn('译：', message['payload']['segments'][0]['html'])
                self.assertEqual(await self.page.locator('#bottom').inner_html(), other)
                await self.page.evaluate('setMode("off")')
                self.assertEqual(await self.page.locator('#article').inner_html(), original)

    async def test_retranslate_late_result_cannot_override_restore_or_site_update(self):
        original = await self.page.locator('#article').inner_html()
        await self.page.evaluate('setMode("translation")')
        await self.done()
        await self.page.evaluate('forceDelay = 600')
        await self.page.locator('#article').get_by_role('button', name='重新翻译本段').click()
        await self.page.evaluate('setMode("off")')
        await self.page.wait_for_timeout(800)
        self.assertEqual(await self.page.locator('#article').inner_html(), original)
        self.assertEqual(await self.page.locator('[data-qwen-owned]').count(), 0)
        await self.page.evaluate('setMode("translation")')
        await self.done()
        await self.page.locator('#article').get_by_role('button', name='重新翻译本段').click()
        await self.page.evaluate('document.querySelector("#link strong").textContent = "Updated by website"')
        await self.page.wait_for_function('document.querySelector("#link strong").textContent === "译：Updated by website"')
        await self.page.wait_for_timeout(800)
        self.assertEqual(await self.page.locator('#link strong').text_content(), '译：Updated by website')
        await self.page.evaluate('setMode("off")')
        self.assertEqual(await self.page.locator('#link strong').text_content(), 'Updated by website')

    async def test_retranslate_mode_switch_and_target_switch(self):
        await self.page.evaluate('setMode("bilingual")')
        await self.done()
        await self.page.evaluate('forceDelay = 600')
        await self.page.locator('#article').get_by_role('button', name='重新翻译本段').click()
        await self.page.evaluate('setMode("translation")')
        await self.page.wait_for_function('document.querySelector("#link strong").textContent === "新：the article"')
        await self.page.locator('#article').get_by_role('button', name='重新翻译本段').click()
        await self.page.evaluate('setMode("translation", "ja")')
        await self.page.wait_for_function('document.querySelector("#link strong").textContent === "译：the article"')
        await self.page.wait_for_timeout(800)
        self.assertEqual(await self.page.locator('#link strong').text_content(), '译：the article')
        self.assertEqual(await self.page.evaluate('requests.at(-1).target'), 'ja')

    async def test_long_retranslation_commits_all_parts_only_after_success(self):
        await self.page.evaluate("""() => {
          document.querySelector('#article').textContent = 'Long sentence & symbols. '.repeat(5000);
          setMode('translation');
        }""")
        await self.done()
        await self.done('#bottom')
        old = await self.page.locator('#article').text_content()
        other = await self.page.locator('#bottom').inner_html()
        await self.page.evaluate('failForceAt = 2')
        button = self.page.locator('#article').get_by_role('button', name='重新翻译本段')
        await button.click()
        await self.page.locator('#article').get_by_text('重译失败，已保留原译文', exact=True).wait_for()
        self.assertEqual(await self.page.locator('#article').text_content(), old)
        self.assertEqual(await self.page.evaluate('commits.length'), 0)
        self.assertEqual(await self.page.evaluate('discards.length'), 1)
        await self.page.evaluate('failForceAt = 0')
        await button.click()
        await self.page.wait_for_function('document.querySelector("#article").textContent.startsWith("新：")')
        self.assertEqual(await self.page.evaluate('commits.length'), 1)
        self.assertGreater(await self.page.evaluate('commits[0].tickets.length'), 1)
        forced = await self.page.evaluate('messages.filter(message => message.force)')
        for message in forced:
            self.assertTrue(message['deferSave'])
            self.assertLessEqual(sum(len(item['html']) for item in message['payload']['segments']), 64000)
            self.assertNotIn('Below the fold', str(message['payload']))
        self.assertEqual(await self.page.locator('#bottom').inner_html(), other)
        # One unstaged batch must prevent committing all the other new fragments.
        await self.page.evaluate('noTicketAt = messages.filter(message => message.force).length + 2')
        await button.click()
        await self.page.get_by_text('部分译文未保存', exact=False).wait_for()
        self.assertEqual(await self.page.evaluate('commits.length'), 1)
        self.assertEqual(await self.page.evaluate('discards.length'), 2)
        await self.page.evaluate('setMode("off")')
        self.assertEqual(await self.page.locator('#article').text_content(), 'Long sentence & symbols. ' * 5000)

    async def test_partial_cache_survives_offline_model_and_retries_only_missing(self):
        await self.page.evaluate('cacheHits = 3; offlineFragment = "Below the fold"; setMode("bilingual")')
        await self.done()
        await self.page.get_by_text('模型离线，未缓存段落暂时无法翻译', exact=False).wait_for()
        await self.page.get_by_text('已复用 3 段缓存', exact=False).wait_for()
        self.assertIn('译：the article', await self.page.locator('#article strong').all_text_contents())
        self.assertEqual(await self.page.locator('#bottom').text_content(), 'Below the fold is more article content.')
        self.assertEqual(await self.page.locator('#bottom [data-qwen-owned]').count(), 0)
        self.assertEqual(await self.page.evaluate('requests.length'), 1)
        await self.page.evaluate('offlineFragment = null; cacheHits = 0')
        await self.page.get_by_role('button', name='重试', exact=True).click()
        await self.done('#bottom')
        self.assertEqual(await self.page.evaluate('requests.length'), 2)
        self.assertEqual(await self.page.evaluate('requests[1].segments.length'), 1)
        self.assertIn('Below the fold', await self.page.evaluate('requests[1].segments[0].html'))

    async def test_cache_reuse_and_unsaved_status(self):
        await self.page.evaluate('cacheHits = 3; saved = false; cacheWarning = "测试缓存写入失败"; setMode("bilingual")')
        await self.done()
        await self.page.get_by_text('已复用 3 段缓存', exact=False).wait_for()
        await self.page.get_by_text('部分译文未保存', exact=False).wait_for()
        await self.page.get_by_text('测试缓存写入失败', exact=False).wait_for()

    async def test_failure_is_visible_and_can_be_retried(self):
        await self.page.evaluate('fail = true; setMode("bilingual")')
        await self.page.get_by_text('测试连接失败', exact=False).wait_for()
        await self.page.evaluate('fail = false')
        await self.page.get_by_role('button', name='重试', exact=True).click()
        await self.done()


if __name__ == '__main__':
    unittest.main()
