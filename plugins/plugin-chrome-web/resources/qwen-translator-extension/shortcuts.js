/* Read the actual binding, including user changes and unassigned shortcuts. */
var browser = globalThis.browser || globalThis.chrome;
browser.commands.getAll().then(commands => {
  const shortcut = commands.find(command => command.name === 'translate-page')?.shortcut;
  document.getElementById('shortcut').textContent = shortcut
    ? `快捷键：${shortcut}` : '尚未设置快捷键，可在 Chrome 扩展快捷键设置（chrome://extensions/shortcuts）中添加。';
}).catch(() => {
  document.getElementById('shortcut').textContent = '可在 Chrome 扩展快捷键设置中查看。';
});
