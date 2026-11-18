var browser = globalThis.browser || globalThis.chrome;
const target = document.getElementById('target');
const status = document.getElementById('status');
const autoSave = document.getElementById('auto-save');
const show = (text, tone = '') => { status.textContent = text; status.dataset.tone = tone; };
browser.runtime.sendMessage({type: 'qwen:cache-settings'}).then(result => {
  if (result.error) show(result.error, 'error');
  else autoSave.checked = result.enabled;
}).catch(error => show(error.message, 'error'));
autoSave.addEventListener('change', async () => {
  try {
    const result = await browser.runtime.sendMessage({type: 'qwen:cache-configure', enabled: autoSave.checked});
    if (result.error) throw new Error(result.error);
    show(autoSave.checked ? '已开启：会记住翻译结果。' : '已关闭：之前保存的译文可在设置中清除。', 'success');
  } catch (error) { autoSave.checked = !autoSave.checked; show(error.message, 'error'); }
});
browser.storage.local.get('target').then(saved => { if (saved.target) target.value = saved.target; });
document.querySelectorAll('[data-mode]').forEach(button => button.addEventListener('click', async () => {
  try {
    await browser.storage.local.set({target: target.value});
    const result = await browser.runtime.sendMessage({type: 'qwen:control', mode: button.dataset.mode, target: target.value});
    if (result.error) show(result.error, 'error');
    else show(button.dataset.mode === 'off' ? '已显示原文。' : '开始翻译啦，进度会显示在网页右下角。', 'success');
  } catch (error) { show(error.message, 'error'); }
}));
document.getElementById('settings').onclick = () => browser.runtime.openOptionsPage();
