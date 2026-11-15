# 双仓克隆与历史推荐全量回灌规范（Archive Rehydration）

当用户要求「把原来的推荐搞回来」、首次初始化 OmniDesk AI 策展中心、或在执行每日精选前需要同步最新本地账本时，按本规范操作。

---

## 1. 两座源仓库与标准克隆位置

| 仓库 | 远端地址 | 本地查找优先级 |
| :--- | :--- | :--- |
| **`obsidian-vault`** | `https://github.com/ZJFILOVE/obsidian-vault.git` | 1. 插件配置 `obsidian_vault_dir`<br>2. 工作区同级 `/home/zhu/Projects/obsidian-vault`<br>3. 默认受管目录 `<XDG_DATA_HOME>/omnidesk/curation/repos/obsidian-vault` |
| **`music-daily`** | `https://github.com/ZJFILOVE/music-daily.git` | 1. 插件配置 `music_daily_dir`<br>2. 工作区同级 `/home/zhu/Projects/music-daily`<br>3. 默认受管目录 `<XDG_DATA_HOME>/omnidesk/curation/repos/music-daily` |

### 安全克隆与快进更新守则
1. 目录不存在时执行 `git clone --branch main <repo_url> <target_dir>`。
2. 目录已存在时先检查 `git status --porcelain`：
   - **干净工作树**：执行 `git fetch origin main` 与 `git merge --ff-only origin/main`；
   - **含未提交改动**：严禁 `git reset --hard` 或强制覆盖用户手写笔记，直接就地读取最新文件并在返回状态中标记 `dirty_worktree: true`。

---

## 2. 真实历史文件扫描与结构化提取规则

回灌器遍历双仓自 `2026-09-04` 起的所有日期文件，按 `(rel_path, size, mtime_ns)` 做增量缓存（`<XDG_DATA_HOME>/omnidesk/curation/rehydrate_manifest.json`）：

1. **Apple Music 每日 5 首（`MediaWorkbench`）**：
   - 主数据源：`music-daily/recommendations/YYYY/MM/YYYY-MM-DD.json`（及镜像 `obsidian-vault/Music/Daily/manifests/YYYY-MM-DD.json`）。
   - 补充数据源：`obsidian-vault/Music/Daily/YYYY-MM-DD.md`（提取 `<!-- AUTO:END -->` 之后的 `## Notes` 手写听后感）。
   - 映射字段：`tracks[].apple_music.song_id` -> `item_id`，`apple_music.url` -> 国区播放源，`analysis_summary` -> `extra.why_recommended`，`listening_cue` -> `extra.listening_or_reading_cue`。
2. **YouTube 每日 5 条（`MediaWorkbench`）**：
   - 数据源：`obsidian-vault/Resources/YouTube/Daily/YYYY-MM-DD.md` + `.recommendation/history/youtube.jsonl`。
   - 提取 `## 今日优先级`、顶部核验范围块，以及 `## 1..5. <Title>` 下的 `频道`、`主题`、`链接`（提取 11 位 `video_id`，封面映射为 `https://i.ytimg.com/vi/<video_id>/hqdefault.jpg`）、`### Why watch` 与 `### 核验说明`（含 `verification_method`）。
3. **News 每日早报（`TextWorkbench`）**：
   - 数据源：`obsidian-vault/News/Daily/YYYY-MM-DD.md`。
   - 提取 `## Executive View` 及每条 `## N. <标题>` 的 `事件日期`、`影响等级`、`### 事实摘要`、`### Why it matters`、`### Long-term impact`、`### What to watch next`、`### Sources`。
4. **Reddit 每日 10 帖（`TextWorkbench`）**：
   - 数据源：`obsidian-vault/Resources/Reddit/Daily/YYYY-MM-DD.md` + `.recommendation/history/reddit.jsonl`。
   - 提取子版块 `r/...`、原帖链接、核心价值拆解与 `original_post_verification` 状态。
5. **科学期刊每日精读（`TextWorkbench` · `category == "journal"`）**：
   - 数据源：`obsidian-vault/00-inbox/Journal/YYYY-MM-DD.md`、`00-inbox/Journal/Index.md` + `.recommendation/history/journal.jsonl`。
   - 提取 `## 今日科学问题`、`## ①/②/③ <英文题名>`、作者/年份/期刊、`[DOI]`/`[PubMed]`/`[PMC]` 链接、`问题与观测`、`作者解释`、`证据边界与 reviewer_synthesis`、`更正说明`、`追问`、`阅读范围与待核` 及 `## 建议阅读顺序`。
6. **英语每日精读 / 翻译 / 词汇 / 周刊（`BookWorkbench` + `TextWorkbench`）**：
   - 数据源：`obsidian-vault/50-courses/English/Daily/YYYY-MM-DD.md`、`Translation/YYYY-MM-DD.md`、`Vocabulary/YYYY-MM-DD.md`、`Weekly/YYYY-MM-DD.md`（周日长文）、`Weekly/TV/YYYY-MM-DD.md`（周六美剧口语） + `.recommendation/history/english.jsonl`。
   - 从 `Vocabulary/*.md` 解析核心词条并幂等导入 `vocab.sm2.v1`（仅新增不存在的词元，不重置已有复习间隔）。

---

## 3. 一键触发命令

```bash
omnidesk-pm rpc recommend.feed.v1 '{"action":"bootstrap_and_rehydrate","pull_remote":true}'
```

若还需将本地已回灌但尚未同步进 Apple Music 累计歌单 `p.YJXV7dEIeb3GzZX` 的历史日期做一次幂等差集补齐：

```bash
omnidesk-pm rpc media.playlist.v1 '{
  "action": "sync_daily_authorized",
  "mode": "daily_immutable",
  "qa_verified": true,
  "sync_all_rehydrated": true,
  "cumulative_playlist_id": "p.YJXV7dEIeb3GzZX"
}'
```
