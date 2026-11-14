const token = document.getElementById('token');
const status = document.getElementById('status');
const autoSave = document.getElementById('auto-save');
const cacheStatus = document.getElementById('cache-status');
const show = (text, tone = '') => { status.textContent = text; status.dataset.tone = tone; };

async function refreshCache() {
  try {
    const result = await browser.runtime.sendMessage({type: 'qwen:cache-settings'});
    if (result.error) throw new Error(result.error);
    autoSave.checked = result.enabled;
    cacheStatus.textContent = `已保存 ${result.count} 段译文，约占 ${(result.bytes / 1024).toFixed(0)} KB`;
  } catch (error) { cacheStatus.textContent = error.message; }
}
autoSave.addEventListener('change', async () => {
  try {
    const result = await browser.runtime.sendMessage({type: 'qwen:cache-configure', enabled: autoSave.checked});
    if (result.error) throw new Error(result.error);
    await refreshCache();
  } catch (error) { autoSave.checked = !autoSave.checked; cacheStatus.textContent = error.message; }
});
document.getElementById('clear-cache').addEventListener('click', async () => {
  try {
    const result = await browser.runtime.sendMessage({type: 'qwen:cache-clear'});
    if (result.error) throw new Error(result.error);
    await refreshCache();
    cacheStatus.textContent = '已清除保存的译文，连接码和语言设置仍保留。';
  } catch (error) { cacheStatus.textContent = error.message; }
});
refreshCache();
browser.storage.local.get('token').then(saved => { token.value = saved.token || ''; });
document.getElementById('connection').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    await browser.storage.local.set({token: token.value.trim()});
    show('正在检查连接…');
    const result = await browser.runtime.sendMessage({type: 'qwen:health'});
    if (result.error) show(result.error, 'error');
    else show(`连接成功，可以开始翻译了。当前模型：${result.model}`, 'success');
  } catch (error) { show(error.message, 'error'); }
});
