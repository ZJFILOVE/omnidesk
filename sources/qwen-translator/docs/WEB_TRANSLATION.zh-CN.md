# Firefox 网页翻译

Qwen 项目自带的 Firefox 扩展，通过本机网页翻译服务调用已登录的 Codex 或 Antigravity。
支持整页翻译、双语阅读、仅译文、译文持久保存、单段重新翻译、恢复原文和动态正文更新。

## 启动与配对

1. 启动桌面应用，打开侧栏 **网页翻译**。也可以执行：

   ```bash
   .venv/bin/python launch_app.py --web
   ```

2. 在 **偏好设置** 选择 Codex 或 Antigravity 翻译服务和模型，然后在网页翻译页点击 **启动网页翻译**。
   网页翻译使用当前选择的 Codex / Antigravity；选择“兼容 API / 仅原文”时，请先切回这两种服务之一。
   修改模型后停止并重新启动网页服务。
3. 本机 Zen 使用常驻安装：运行 `python scripts/package_firefox.py`，在 `about:addons` 的齿轮菜单选择
   **从文件安装附加组件**，选择 `dist/qwen-firefox-0.1.5-unsigned.xpi`。本机已允许本地未签名扩展，
   安装一次后重启会自动载入；更新时直接安装新版 XPI，保留配对与译文缓存。紫色“文 / A”气泡代表 Qwen，
   蓝色时钟代表时序。可以把两个扩展固定到工具栏。标准 Firefox 需要 Mozilla 签名版本；
   开发调试仍可在 `about:debugging#/runtime/this-firefox` 临时载入 `firefox-extension/manifest.json`。
4. 桌面端点击 **复制配对密钥**，然后在扩展 **连接设置** 中粘贴，点击 **保存并测试连接**。
5. 打开普通网页，按 **Alt + Shift + T** 直接启动双语翻译；也可点击扩展图标，选择语言和 **双语翻译** 或 **仅译文**。

快捷键在安装或重新载入新版扩展后注册，无需先点击扩展图标。它沿用上次选择的目标语言，默认简体中文，并复用现有译文缓存。弹窗和连接设置会显示实际分配的按键。修改方式：`about:addons` → 齿轮菜单 → **管理扩展快捷键** → **同声 · Qwen 网页翻译**。已有扩展或系统占用组合键时需换一个按键，参见 [Mozilla 快捷键文档](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/manifest.json/commands)。浏览器受限页面无法翻译时，扩展图标显示 `!`，悬停查看原因；正常网页的翻译进度或连接错误显示在页面右下角。

服务固定监听 `http://127.0.0.1:8766`。连接测试只确认本地服务；首次翻译才验证所选服务的登录和模型可用性。
浏览器刷新或再次打开后，点击翻译即可复用已保存的译文。只有主动启用的当前页面会被处理；不自动注入所有网站。

升级至 0.1.4 后，在桌面端停止并重新启动网页翻译服务；在 `about:debugging#/runtime/this-firefox`
重新载入扩展，然后刷新文章页再翻译。如果临时扩展来自旧 XPI，应载入新的
`dist/qwen-firefox-0.1.4-unsigned.xpi` 或项目中的 `firefox-extension/manifest.json`。

若连接测试成功，但整页翻译等待约半分钟后出现 `Receiving end does not exist`，请更新到 **0.1.2 或以上**。
旧版会在等待模型响应时被 Firefox / Zen 的后台空闲机制关闭。新版只在有翻译或缓存保存任务时
每 10 秒调用一次浏览器 API 保持后台活动，完成、失败或取消后停止；无需修改浏览器的休眠设置。

如果更新后页面仍提示 `Could not establish connection. Receiving end does not exist.` 或“扩展连接已断开”，
请先刷新网页，再点击扩展的双语翻译。仍失败时，在 `about:addons` 中关闭再启用 Qwen 扩展，
然后刷新网页并重新翻译。更新或重新载入扩展后，已打开的页面可能仍保留旧脚本。
新版会暂停后续批次并显示恢复步骤；后台恢复后可点击“重试”，原有译文仍可恢复为原文。
这是页面脚本与扩展后台之间的通信问题，参见 [Mozilla 消息 API](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/API/runtime/sendMessage)。
如果显示“无法连接 Qwen”，则应在桌面端启动网页翻译服务，并到扩展连接设置测试配对。

临时扩展在 Firefox 重启后需要重新载入。本机 Zen 已配置允许未签名扩展，可通过扩展管理器常驻安装 XPI；
此本机设置不能推广为标准 Firefox 的安装方式。正式分发或长期安装到标准 Firefox 需要 Mozilla 签名。
参见 [Mozilla 临时安装文档](https://extensionworkshop.com/documentation/develop/temporary-installation-in-firefox/)。

## HTML 如何处理

扩展根据文字节点和段落边界提取正文，保留链接、加粗等行内元素的简化 HTML，给每个文字节点加上编号。
服务将较大的请求按段落分组并行翻译，附带页面标题和开头正文作为上下文以帮助统一术语，结果按文档顺序合并。偏好设置中的 **视频 / 网页翻译并发** 支持 1–8，默认 4，修改后重启网页服务。多个网页请求共用此上限；视频任务使用独立上限。
0.1.4 起，模型只返回按段落编号、文字编号对应的译文字符串，原有 HTML 由扩展保留；
不再要求模型重写整段 HTML。响应按段校验，遗漏文字编号只影响对应段落，其他成功译文照常显示和保存。
可在网页右下角点击 **重试**，仅请求失败部分。

- **双语模式**：保留原节点，在段落下插入带独立样式的译文。译文中的普通网页链接可点击。
- **仅译文**：只更新原有文字节点，保留原元素及事件绑定。
- **恢复原文**：取消本页请求，移除译文并恢复仍由扩展管理的文字；不会把网站刚更新的正文覆盖回旧内容。
- 模型输出只作为文字写入 DOM，不直接执行或注入模型返回的 HTML。
- 跳过表单控件、可编辑内容、隐藏区域、代码块、`translate="no"` 与 `.notranslate` 区域。
- 0.1.3 起，导航、菜单、工具栏、按钮、标签页和表单区域不提交翻译；其余内容按整段筛选，通常至少有 20 个字母/数字、4 个词或 10 个中日韩文字之一才翻译。标题与图注保留，正文中至少两个词（或四个中日韩文字）且带句末标点的短句也保留。短链接、加粗文字按所在整段判断，不会从正文中截掉。此规则用于减少零碎标签请求，不保证识别所有网站组件。
- 0.1.4 起，优先选择可识别的出版商正文和摘要区域，否则选择唯一且有足够正文的 article 区域；保留文章标题，排除侧栏、页脚以及带已知标记的指标、推荐和参考文献区域。上标、引用和 inline-block 行内元素按所属段落翻译，避免把正文拆成大量碎片。不能识别文章区域时仍按通用规则提取，未标注的网页组件可能需要网站适配。
- 超长段落或超过 200 个文字节点的段落按虚拟文字片段分批请求，完整拼回后显示，原网页节点和链接保持原样。失败只重试失败片段，已完成片段在本页内存中复用。

首次收集整页已经加载的正文，包括尚未滚动到的段落；不会自动替网站加载尚不存在的内容。
每批最多 24,000 个简化 HTML 字符或 32 个请求片段，扩展依次发送批次，服务将批次内的段落并行处理，
并附带最多 6,000 字符的开头原文作为上下文。这里的阈值是应用的请求边界，不代表模型上下文上限。
滚动触发网站加载更多正文时，新段落会合并补译。多个标签页在扩展后台排队，每批在服务内最多同时调用配置数量的独立翻译会话。
扩展等待队列不消耗单次模型请求的 240 秒超时额度。限流时同一服务内统一等待并有限重试；额度耗尽后暂停新调用，额度恢复后重启网页服务。失败分组保留其他分组的译文。

## 保存与单段重译

译文默认保存在 Firefox 的本机扩展存储中。再次打开同一网址并选择双语或仅译文时，目标语言、原段落内容及结构一致的部分直接读取已存译文，不调用模型。未命中或原文已变化的段落才会请求翻译。完全命中缓存时无需启动 Qwen 桌面服务；部分命中时，即使新内容翻译失败，已存段落仍可显示。

每段末尾都有 **重新翻译** 按钮，两种显示模式均可使用。点击后仅请求该段，等待期间保留旧译文；失败也保留旧译文，成功后才整体替换并保存。超长段落的多个片段全部成功后才一起提交保存，避免半新半旧。切换模型后已有译文仍可复用，需要新模型处理的段落可手动重新翻译。

在扩展设置中可以关闭自动保存或清除已保存译文。恢复原文只撤销当前页面显示，不会删除持久译文。关闭保存时不读写持久缓存，已有记录保留至用户清除；隐私窗口不读写持久译文。清除译文不会删除配对密钥或目标语言设置。

缓存按完整网址（含查询参数与片段）、目标语言和原段落结构的 SHA-256 指纹匹配；持久记录包含指纹和译文，不保存原文、原始网址或标题。缓存最多保存 2,000 个请求片段、约 4 MiB（按保守大小估计），达到上限时淘汰最久未使用的条目。长段落可能占用多个片段。容量不足或浏览器写入失败时会提示未保存，当前译文仍可阅读。

持久化使用浏览器扩展的 `storage.local`，与页面内存、网页自己的 localStorage 分开；正式扩展被卸载时其本地存储通常会删除，参见 [Mozilla storage.local 文档](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/API/storage/local)。临时扩展每次重启仍需重新载入，保持同一扩展 ID 与 Firefox 配置文件。
每批完成后回填成功段落；超长段落仍须等待自身的全部片段完成才显示。

## 范围与限制

- 适用于普通 HTML 文章、列表、表格文字以及以 div/span 排版的正文。
- 暂不支持浏览器内部页面、扩展商店、内置 PDF 阅读器、iframe、网页自身 Shadow DOM、图片 OCR、canvas 和数学公式翻译。
- 文字节点的顺序保持不变。跨链接或强调标签需要大幅调整语序的句子，译文流畅度可能受到影响。
- 超长段落会分批处理，拆分后跨片段的语境可能减弱；嵌套过深的结构仍可能遗漏。不会为了拆分请求而拆开原网页节点。
- 某些持续重绘 DOM 的网页可能使译文失效并重新请求。关闭翻译可立即停止本页处理。
- 翻译延迟和可用额度由实际翻译模型、登录方式和账户状态决定。

连续批次和普通滚动复用文字发现与排序结果；网页 DOM 内容变化后仍会重新扫描正文，以保证动态更新和恢复原文正确。

## 数据与进程

发送内容为页面标题、简化 HTML 和待译文字；不读取 Cookie、浏览历史或输入框内容。
网页文字经本机服务发送给所选的 Codex / Antigravity，云端处理遵循所登录服务的规则。
配对密钥保存在本机 QSettings 和 Firefox 扩展本地存储中，后台发请求时携带，不传入网页正文脚本。
桥接服务校验 Host 和配对密钥，仅绑定 IPv4 回环地址；不提供任意 URL 转发接口。

本地 Codex 桥接会话使用临时工作目录、只读沙箱和禁用工具的配置，不持久保存网页正文和译文；用户需要的持久译文由 Firefox 扩展独立保存和管理。
Antigravity 使用 `agy` 当前登录和独立工作目录，每批建立新会话；CLI 可能保存自身会话历史，详见 [Antigravity 接入说明](ANTIGRAVITY.zh-CN.md)。

关闭网页或恢复原文会取消对应请求；停止服务或退出桌面应用会清理服务和翻译子进程。

## 源码与打包

```text
firefox-extension/        Firefox Manifest V3 扩展与本地译文缓存，无打包依赖
web_translation.py       鉴权、HTML 校验、队列和 Codex 调用
web_ui.py                桌面服务管理与配对页面
codex_session.py         复用字幕模块的 Codex App Server 通道
scripts/package_firefox.py  生成未签名 XPI，不打包本地设置或密钥
```

```bash
python scripts/package_firefox.py
# 输出 dist/qwen-firefox-0.1.5-unsigned.xpi
```

扩展为独立实现，参考了陪读蛙的分段、批量与 HTML 保护思路，没有复制其代码、提示词或资源。
参考版本和来源见 [SOURCES.md](../firefox-extension/SOURCES.md)。
Firefox 后台采用官方支持的 [background.scripts](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/manifest.json/background)，
Codex 通道使用官方 [App Server 协议](https://learn.chatgpt.com/docs/app-server)。

## 验证

可统一运行 `python3 scripts/test.py --with-firefox`，自动选择后台和 Qt 的解释器。首次安装浏览器测试依赖可运行 `python3 scripts/setup.py --firefox-tests`。以下命令仍可用于逐模块验证：

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_web_translation.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_codex_session.py' -v
node --test tests/test_firefox_background.cjs
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -p 'test_desktop_ui.py' -v

# 可选：首次下载 Firefox 测试运行时，使用现有 Playwright 依赖。
PLAYWRIGHT_BROWSERS_PATH="$PWD/runtime/browsers" .venv/bin/python -m playwright install firefox
QWEN_FIREFOX_TESTS=1 .venv/bin/python -m unittest discover -s tests -p 'test_firefox_dom.py' -v
```

DOM 测试使用真实 Firefox 与模拟翻译响应，覆盖整页请求、格式保留、模式切换、动态更新、取消、单段重译与失败保留旧译文。额外的集成测试将真实 content/background/cache 脚本串联，模拟浏览器存储传输和翻译服务，验证后台重启后缓存复用、原文变化只翻译变化段落；Node 测试覆盖缓存容量、清除、隐私窗口和重译保存事务。
生命周期测试在独立 Firefox 配置中实际安装扩展，以缩短后的空闲周期验证慢响应期间消息不丢失、完成后后台仍可休眠。2026-09-14 修复版还在 Zen 1.21.13b 的默认空闲设置下通过了 40 秒模拟翻译响应验证。
不依赖账户额度。桌面测试使用系统 Python 的 PyQt6，服务端测试使用项目虚拟环境的 FastAPI。

2026-09-14 另做了真实集成验证：隔离 Firefox 153 临时安装生成的扩展，配对本地服务，
一次提交测试页的六个段落给已登录的 Codex Luna，全部回填中文，链接与加粗结构保留。
测试结束后本地服务退出码为 0；没有修改日常浏览器配置。

### 0.1.5 图标更新

新增 SVG 与 16–256 像素 PNG 图标，覆盖工具栏、附加组件列表、弹窗与设置页。
扩展 ID 保持不变，使用常驻 XPI 覆盖更新；不需要卸载旧版，也不需要清除配对或译文缓存。
