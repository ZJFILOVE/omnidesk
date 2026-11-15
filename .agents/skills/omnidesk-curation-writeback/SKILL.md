---
name: omnidesk-curation-writeback
description: Unified AI curation, historical archive rehydration, frontend reverse-write, and Apple Music playlist write-back skill for OmniDesk. Use whenever an internal or external AI agent needs to (1) clone/sync ZJFILOVE/obsidian-vault and ZJFILOVE/music-daily to rehydrate past recommendations into OmniDesk, (2) run the daily non-repeating curation pipelines (news-youtube-reddit, music, english, journal), or (3) generate user-prompted repeatable recommendations and write them back to OmniDesk workbenches and Apple Music playlists.
---

# OmniDesk Unified AI Curation & Write-Back Skill

本 Skill 是 **OmniDesk 内驻 Agent（`plugin-magpie-agent`）与外部入站 Agent（Antigravity / Codex / Claude Code / OpenCode）共用的唯一策展与反写入口**。无需拆分多个零碎 Skill——通过下方**第 1 步意图路由**按需读取对应的 `references/*.md`，即可覆盖「历史归档回灌」「每日不重复精选」「按需可重复推荐」及「Apple Music 歌单写回」全流程。

---

## Step 1: 意图分类与渐进式参考加载（Mode Router）

收到任务后，先将请求归入以下三种执行动作之一，并**仅使用 `view_file` 读取该分支所需的参考文件**：

| 动作分支 | 典型触发场景 | 必读参考文件（按需加载） |
| :--- | :--- | :--- |
| **分支 0：双仓克隆与历史归档回灌**<br>(`archive_rehydrate`) | 「把原来的推荐搞回来」「克隆/同步 obsidian-vault 和 music-daily 到前端」「初始化历史曲库/早报/期刊」 | 1. [`references/archive-rehydration.md`](references/archive-rehydration.md)<br>2. [`references/writeback-rpc-and-apple-music.md`](references/writeback-rpc-and-apple-music.md) |
| **分支 A：每日不重复精选环节**<br>(`mode = "daily_immutable"`) | 指定今日（`Asia/Shanghai` `YYYY-MM-DD`）执行每日例行推荐：`news-youtube-reddit`、`music`（Daily 5）、`english`（精读/翻译/词汇/周六美剧/周日长文）或 `journal`（2–4 篇论文） | 1. [`references/daily-immutable-rules.md`](references/daily-immutable-rules.md)<br>2. [`references/writeback-rpc-and-apple-music.md`](references/writeback-rpc-and-apple-music.md) |
| **分支 B：按需可重复推荐系统**<br>(`mode = "interactive_ephemeral"`) | 用户根据心情、工作场景、当前阅读/播放上下文提出即时推荐需求（如「来 5 首雨夜写代码的器乐，以前推过的也可以」「找几篇和当前论文相关的经典工作」） | 1. [`references/interactive-repeatable-rules.md`](references/interactive-repeatable-rules.md)<br>2. [`references/writeback-rpc-and-apple-music.md`](references/writeback-rpc-and-apple-music.md) |

---

## Step 2: 全局硬防火墙红线（Non-Negotiable Guardrails）

无论走哪一分支，以下五条系统红线在任何情况下不得突破：

1. **双模账本绝对隔离**：
   - `daily_immutable` **必须**先同步本地双仓并读取完整 `.recommendation/policy.json`、永久去重历史（`.recommendation/history/*.jsonl`、`state/tracks.json`）及所有未归档 `PREPARED` 预留单；文件不可读或截断视为 **Unverified（立即中止）**，严禁当作空历史；**严禁自动补推漏日**。
   - `interactive_ephemeral` **明确允许重复推荐历史条目**（严禁把永久历史当作黑名单剔除好内容）；同时强制 `record_permanent_history: false`，**严禁向 `.recommendation/history/*.jsonl`、`state/tracks.json`、`.recommendation/transactions/` 或每日正式笔记目录写入任何内容**。
2. **Apple Music 歌单目标硬隔离**：
   - 固定累计歌单 **`p.YJXV7dEIeb3GzZX`（`ChatGPT Daily 5`）** 及 `ChatGPT Daily 5 · YYYY-MM-DD` **仅限** `daily_immutable` 模式在完成双仓发布与 QA 门禁后以「仅缺失追加（Missing-only Append）+ 读回校验」方式写入。
   - `interactive_ephemeral` 模式**严禁触碰 `p.YJXV7dEIeb3GzZX`**，只能写入 `MPVCore` 即时播放队列或独立的可重复歌单（如 `"OmniDesk AI 灵感电台"` 或用户指定歌单名）。
3. **手写笔记与瞬时饮食段落边界**：
   - 更新已有 Obsidian Markdown 文件时，严格保留 `<!-- AUTO:START -->` 之前与 `<!-- AUTO:END -->` 之后的全部用户手写内容（如 `## Notes`）。
   - `english` 每日流程末尾的 **`今日饮食`** 建议仅作为会话文本 / `ephemeral_chat_note` 输出，**严禁写入任何仓库文件或历史账本**。
4. **中文深度编按质量底线**：
   - 无论是每日精选还是按需交互推荐，反写到 OmniDesk 前端的条目必须附带具体、有证据支撑的中文导读（`why_recommended`、`listening_or_reading_cue`、证据边界与核验方式说明），拒绝空洞的一句话套话。
5. **真实性、发布证据与失败关闭**：
   - 禁止合成兜底：网关不可用且归档无命中时返回 `no_verified_candidates`，不得生成占位歌曲、虚构论文/书籍或伪造 DOI/URL；文本/书籍必须匹配真实归档条目，音乐必须由实时 `preflight_cn_catalog` 的 `verified_ids` 覆盖。
   - `qa_verified` 不能替代上游发布证明：每日发布必须携带匹配的 `publication_evidence`（`PUBLISHED`、`publication_verified: true`、`operation_id`、`project`、`business_date`、`receipt`），否则返回 `needs_verified_bundle`。
   - 缺少本 Skill 或必需 references 时以 `skill_unavailable` fail closed。

---

## Step 3: 标准端到端执行闭环

1. **前置检查（Ensure Local Mirror Ready）**：
   - 若本地尚未克隆 `ZJFILOVE/obsidian-vault` 与 `ZJFILOVE/music-daily`，先按 [`references/archive-rehydration.md`](references/archive-rehydration.md) 调用 `bootstrap_and_rehydrate` 拉取双仓并回灌历史归档。
2. **选题、实证核验与文案生成**：
   - 按所选分支参考文档完成信源核验（如 Apple Music 国区 `/v1/catalog/cn/songs` 预检、DOI/PubMed 核验、YouTube 发布方页面核验或历史库匹配）。
3. **持久化与前端反写（`recommend.feed.v1`）**：
   - 通过 `omnidesk-pm rpc recommend.feed.v1 --file <payload.json>`（或内驻 `ctx.capabilities().call("recommend.feed.v1", ...)`）将结构化条目反写进 `MediaWorkbench`、`TextWorkbench`、`BookWorkbench`（含 `vocab.sm2.v1` 生词闪卡），触发 `"entity.recommend.updated"`。
4. **Apple Music 歌单/播放队列写回（`media.playlist.v1`，当涉及音乐时）**：
   - 必须在第 3 步前端反写成功后执行；按模式调用 `sync_daily_authorized`（每日 5 首）或 `write_interactive_playlist` / `enqueue_playback`（按需可重复推荐），并核对 `verified_ids` / `verified_count` 与读回状态。部分失败如实返回 `partial_success` / `playlist_error`，不得伪报全链路成功。
5. **输出简明执行回执**：
   - 向用户汇报本次反写的工作台栏目、条目摘要、核验范围及歌单同步状态（若为 `english` 每日管线，在消息末尾附上仅聊天可见的 `今日饮食`）。
