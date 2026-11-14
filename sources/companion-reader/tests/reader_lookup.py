"""Browser regression: run against a local dev server, with a synthetic book and mocked AI."""
import json
import os
import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = os.environ.get('READER_TEST_URL')
if not BASE:
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(Path(__file__).resolve().parents[1] / 'dist'))
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    BASE = f'http://127.0.0.1:{server.server_port}' 
OUT = Path('/tmp/toefl-reader-test')
OUT.mkdir(exist_ok=True)
paragraphs = []
for i in range(1, 31):
    sentence = f"I can't forget the well-known river bank where we observed remarkable adaptations in nature and studied many unusual organisms with careful attention to every tiny detail on expedition {i}."
    paragraphs.append({'index': i, 'text': sentence, 'sentences': [sentence]})
book = {'id': 'test-book', 'title': 'Reader lookup test', 'author': 'Test', 'importedAt': 1,
        'totalWords': 1000, 'chapters': [{'id': 'chapter-1', 'index': 1, 'title': 'Field notes',
        'wordCount': 1000, 'paragraphs': paragraphs}]}

def visible_word(page):
    index = page.locator('.reader-word').evaluate_all('''els => els.findIndex(el => {
      const r = el.getBoundingClientRect(); const v = document.querySelector('.reader-viewport').getBoundingClientRect();
      return el.textContent === 'bank' && r.left >= v.left && r.right <= v.right + 1 && r.top >= v.top && r.bottom <= v.bottom;
    })''')
    if index < 0:
        page.screenshot(path=str(OUT / 'failure.png'))
        print(page.locator('.reader-page-status').inner_text(), page.locator('.reader-word').count())
        print(page.locator('.reader-word[data-word="bank"]').evaluate_all('els => els.slice(0,8).map(el => el.getBoundingClientRect().toJSON())'))
    assert index >= 0
    return page.locator('.reader-word').nth(index)

with sync_playwright() as p:
    browser = p.chromium.launch()
    for width, height in [(390, 844), (1280, 900)]:
        context = browser.new_context(viewport={'width': width, 'height': height}, has_touch=True)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        calls = []
        failed = [False]
        def reply(route):
            payload = route.request.post_data_json
            calls.append(payload)
            if failed[0]:
                route.fulfill(status=503, content_type='application/json', body='{}')
            else:
                route.fulfill(json={'choices': [{'message': {'content': '【语境词义与词根拆解】\nbank 在本句指河岸。\n【词源画面故事】\n测试解释。'}}]})
        page.route('**/books/starter-new-yorker.json', lambda route: route.fulfill(json=book))
        page.route('**/v1/chat/completions', reply)
        page.add_init_script("localStorage.setItem('toefl_feiniu_token', 'test-only'); localStorage.setItem('toefl_feiniu_base_url', location.origin + '/v1')")
        page.goto(BASE)
        page.get_by_role('button', name='下一页', exact=True).wait_for()
        page.get_by_role('button', name='下一页', exact=True).click()
        page.wait_for_timeout(300)
        status = page.get_by_role('button', name='查看阅读进度').inner_text()
        word = visible_word(page)
        sentence = word.evaluate('el => el.closest("[data-s]").textContent')
        word.tap()
        dialog = page.get_by_role('dialog', name='单词查询 · bank')
        dialog.wait_for()
        assert not calls, 'Clicking a word must not start sentence interpretation'
        assert dialog.locator('blockquote').inner_text() == sentence
        page.keyboard.press('PageDown')
        assert page.get_by_role('button', name='查看阅读进度').inner_text() == status
        dialog.get_by_role('button', name='AI 查询词义与词源', exact=True).click()
        dialog.get_by_text('bank 在本句指河岸。', exact=False).last.wait_for()
        assert len(calls) == 1 and '单词：bank' in calls[0]['messages'][1]['content']
        assert sentence in calls[0]['messages'][1]['content']
        page.screenshot(path=str(OUT / f'word-{width}.png'))
        dialog.get_by_role('button', name='继续阅读', exact=True).click()
        assert page.get_by_role('button', name='查看阅读进度').inner_text() == status
        page.keyboard.press('PageDown')
        assert page.get_by_role('button', name='查看阅读进度').inner_text() != status
        page.keyboard.press('PageUp')
        assert page.get_by_role('button', name='查看阅读进度').inner_text() == status
        word.click()
        page.get_by_role('dialog').get_by_text('bank 在本句指河岸。', exact=False).last.wait_for()
        assert len(calls) == 1
        page.keyboard.press('Escape')
        assert page.locator('dialog[open]').count() == 0
        # Same word on the next page has a different sentence: no stale cache.
        page.get_by_role('button', name='下一页', exact=True).click()
        page.wait_for_timeout(300)
        visible_word(page).click()
        failed[0] = True
        page.get_by_role('button', name='AI 查询词义与词源', exact=True).click()
        page.get_by_role('alert').wait_for()
        failed[0] = False
        page.get_by_role('button', name='AI 查询词义与词源', exact=True).click()
        page.get_by_text('bank 在本句指河岸。', exact=False).last.wait_for()
        page.get_by_role('button', name='查看 AI 词义与词源', exact=True).wait_for()
        assert len(calls) == 3
        assert calls[0]['messages'][1]['content'] != calls[-1]['messages'][1]['content']
        page.keyboard.press('Escape')
        # Swipe still turns pages without opening a word.
        viewport = page.locator('.reader-viewport')
        before = page.get_by_role('button', name='查看阅读进度').inner_text()
        viewport.evaluate("""el => {
          const start = new Touch({ identifier: 1, target: el, clientX: 120, clientY: 300 });
          const end = new Touch({ identifier: 1, target: el, clientX: 250, clientY: 305 });
          el.dispatchEvent(new TouchEvent('touchstart', {bubbles: true, touches: [start]}));
          el.dispatchEvent(new TouchEvent('touchend', {bubbles: true, changedTouches: [end]}));
        }""")
        page.wait_for_timeout(400)
        assert page.get_by_role('button', name='查看阅读进度').inner_text() != before
        assert page.locator('dialog[open]').count() == 0
        # Original sentence path and full extraction, including I and late words.
        page.get_by_role('button', name='当前点词查词，切换为点句解读').click()
        page.locator('[data-s]').evaluate_all('''els => {
          const v = document.querySelector('.reader-viewport').getBoundingClientRect();
          const el = els.find(el => Array.from(el.getClientRects()).some(r => r.left >= v.left && r.right <= v.right + 1));
          el.click();
        }''')
        dialog = page.get_by_role('dialog', name='句子解读', exact=True)
        dialog.wait_for()
        dialog.get_by_role('button', name='i', exact=True).wait_for()
        dialog.get_by_role('button', name='expedition', exact=True).wait_for()
        dialog.get_by_role('button', name='expedition', exact=True).click()
        page.get_by_role('dialog', name='单词查询 · expedition').wait_for()
        page.keyboard.press('Escape')
        page.reload()
        page.get_by_role('button', name='当前点句解读，切换为点词查词').wait_for()
        page.get_by_role('button', name='当前点句解读，切换为点词查词').click()
        page.screenshot(path=str(OUT / f'reader-{width}.png'))
        page.get_by_role('button', name='阅读菜单', exact=True).click()
        page.get_by_role('button', name='长卷精读', exact=True).click()
        page.locator('.reader-word').first.click()
        page.get_by_role('dialog', name='单词查询 · I').wait_for()
        page.keyboard.press('Escape')
        assert not errors, errors
        print(f'PASS {width}x{height}: word/context/cache/retry/modal/page/swipe/extraction/preference')
        context.close()
    browser.close()
