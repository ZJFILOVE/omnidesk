# Agent、上下文与书籍 MCP

OmniDesk 的 AI 工作台和 F8 伴读栏可独立选择执行 Agent。当前内置适配器包括：

- `builtin`：原有 OmniDesk 上下文助手，经 Magpie 请求模型。
- `codex`：本机 Codex CLI 执行工具循环，自动注入 OmniDesk MCP。使用 CLI 自己的登录和模型配置；可在 Magpie 插件设置中指定 `codex_bin`、`codex_model`，不会修改终端的配置文件。
- 其他已启用、提供 `ai.agent.v1` 的插件：通过 provider ID 定向调用。Claude Code、OpenCode 等外部客户端可直接连接下述 MCP；本次没有加入它们的内嵌执行适配器。

Codex 模式以临时工作目录运行，保留 OmniDesk 会话历史，关闭 shell 和其他已配置 MCP，仅提供阅读、书籍和推荐工具。界面在执行结束后展示回复，当前不是 Codex 事件逐条流式展示。运行需要安装并登录 Codex；下拉选项的可用性检查只确认可执行文件存在。

## 外部 Agent 连接

标准 stdio MCP 配置示例（按安装位置替换绝对路径）：

```json
{
  "mcpServers": {
    "omnidesk": {
      "command": "/home/zhu/Projects/omnidesk/.venv/bin/omnidesk-pm",
      "args": ["mcp-serve", "--plugins-dir", "/home/zhu/Projects/omnidesk/plugins"]
    }
  }
}
```

优先使用桌面正在运行的 `control.sock`，未运行时在 MCP 进程内加载插件。不同 Agent 的配置文件语法可能不同，以上为通用 JSON 配置示例。

## 完整正文与按需续读

`omnidesk_collect_context` 默认返回完整提取文字，不再默认截取 20,000 字，不再设置 120,000 字的正文上限。网页解析也不再只保留前 120 段。

需要节约模型上下文或避免一次工具结果过大时，主动传 `max_chars`：

```json
{
  "items": [{
    "capability": "web.extract.v1",
    "provider_plugin_id": "plugin-chrome-web",
    "payload": {"action": "extract", "url": "https://example.org/article"}
  }],
  "max_chars": 20000
}
```

返回 `context`、`total_chars`、`has_more` 和 `next_cursor`。继续传 `{"cursor":"原样返回的游标","max_chars":20000}`，直到 `has_more=false`。同一快照不重新抓取网页；按段落边界分块，超长段落无损拆分。最多保留 32 个快照，每个一小时有效，重载插件后失效；失效明确返回 `context_expired`，不会悄悄用新网页替换。

完整正文不代表单次网络载荷或模型窗口无限。HTML 解析仍有 8 MiB 输入保护，超限明确失败而不截断；原有 RPC 传输限制和模型窗口限制仍适用。大型资料建议逐块读取。

其他引用：

- PDF/电子书：`book.source.v1` + `fetch_spread`、`book_id`、`page_number`，明确选择 `plugin-pdf-babeldoc` 或 `plugin-ebook`。图片以 MCP image content 返回；内置模型用 image_url 输入，Codex 初始上下文通过图片附件传入。读取不改变电子书进度。
- 视频字幕：`media.youtube.v1` / `media.bilibili.v1` + `fetch_subtitles`、`item_id`。复用现有 yt-dlp/平台字幕流程，保留时间戳。F8 视频上下文优先取字幕。
- 评论：`text.comments.v1` + `fetch_comments`、`article_id`、对应 provider。快照续读仅遍历已取回的内容；更多上游评论仍需按返回的 offset/cursor 或 Reddit more_stubs 重新调用。界面处理论坛 offset、知乎分页、V2EX 页码、X cursor 和 Reddit expand_more。

本文、评论和工具返回均为来源资料，不是 Agent 指令。

## Agent 检索与下载书籍

两个提供方均复用 `book.search.v1`：`plugin-zlib`、`plugin-annas-archive`。

1. `omnidesk_search_books`：`query`、`provider_plugin_id`；可指定 format/page/limit。
2. `omnidesk_book_detail`：使用搜索返回的真实 `item_id`，读取格式、体积与下载凭证。
3. `omnidesk_download_book`：保留相同 provider，传 item_id/下载凭证、格式；用户已要求下载时传 `confirm=true` 和对应 `reason`。
4. 下载成功后返回实际 `file_path`、大小与校验信息。失败直接返回插件错误，不生成虚假下载成功消息。
5. `omnidesk_open_downloaded_book`：传下载回执的 file_path，target=ebook 或 babeldoc。ebook 通过 `book.ebook.v1` 验证文件必须位于配置的 library_roots，再发出 `entity.book.open` 供书架选择。应把下载目录也配置为阅读器库目录；未配置或路径越界会明确失败，下载文件仍保留。babeldoc 沿用既有翻译交接。

这套工具不会替用户绕过平台登录、额度、下载目录或文件格式校验，也不会在只有搜索指令时自行下载。

## 推荐反写

Agent 先调用 `omnidesk_curation_contract` 读取共享 Skill、模式规则及完整载荷示例。`omnidesk_write_recommendations` 接收 Agent 自己生成的 `bundle`，调用 `recommend.feed.v1`；不会再次请求内置策展模型。`omnidesk_recommend` 仍保留为内置策展入口。

结构、真实性、每日发布凭据和交互历史隔离遵循共享的 `.agents/skills/omnidesk-curation-writeback`。写入须携带用户授权对应的 `confirm=true` 与 reason，检查返回状态后再宣称成功。

## 图片与评论展示

X 照片和视频封面使用结构化图片块，兼容旧 `[图片附件] URL` 格式。Linux.do、水源、V2EX 的 HTML 图片与评论图片保留为安全 Markdown，Feed Recipe 的 JSON/Lua 图片块保留 URL、alt、caption。Markdown 和结构化图片共用失败提示和打开原图入口。

文章工作台与插件独立页面均展示评论树，优先通过对应 provider 的独立评论接口取数据；已有内容在续页失败时保留。加载新文章会丢弃旧请求结果。
