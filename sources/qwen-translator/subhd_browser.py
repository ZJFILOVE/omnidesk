"""Follow SubHD's public two-click download flow in a fresh browser session."""
import asyncio
import os
from pathlib import Path
import tempfile


async def fetch(sid, root):
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(Path(__file__).resolve().parent / 'runtime/browsers'))
    try:
        from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout
    except ImportError:
        raise RuntimeError('SubHD 自动下载需要浏览器组件，请运行 scripts/setup_video.py。') from None
    from subtitle_sources import LIMIT, subtitle_files
    proxy = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
    async with async_playwright() as engine:
        try:
            browser = await engine.chromium.launch(headless=True, proxy={'server': proxy} if proxy else None)
        except Exception:
            raise RuntimeError('SubHD 浏览器无法启动，请运行 scripts/setup_video.py 安装组件，或打开网页下载后导入。') from None
        try:
            async with asyncio.timeout(100):
                context = await browser.new_context(accept_downloads=True)
                messages = []
                page = await context.new_page()
                async def dialog(d):
                    messages.append(d.message)
                    await d.dismiss()
                page.on('dialog', dialog)
                await page.goto('https://subhd.tv/a/' + sid, wait_until='domcontentloaded', timeout=45000)
                async with page.expect_popup(timeout=20000) as popup:
                    await page.locator('.subtitle-prepare-download').click()
                tab = await popup.value
                tab.on('dialog', dialog)
                await tab.wait_for_url('**/down/**', timeout=25000)
                await tab.wait_for_load_state('domcontentloaded')
                async with tab.expect_download(timeout=30000) as transfer:
                    await tab.locator('.download-submit').click()
                download = await transfer.value
                with tempfile.TemporaryDirectory(dir=root, prefix='subhd-') as tmp:
                    target = Path(tmp) / 'download'
                    await download.save_as(target)
                    if target.stat().st_size > LIMIT:
                        raise ValueError('字幕文件超过 16 MB，请选择单集字幕。')
                    files = []
                    for i, (name, data) in enumerate(subtitle_files(target.read_bytes(), download.suggested_filename)):
                        path = Path(root) / f'{i:03}-{Path(name).name}'
                        path.write_bytes(data)
                        files.append({'path': str(path), 'title': name})
                    return files
        except (BrowserTimeout, TimeoutError):
            raise RuntimeError('SubHD 下载未完成，可能需要登录或人工验证。请打开条目网页处理，再导入下载的字幕。') from None
        finally:
            await browser.close()
