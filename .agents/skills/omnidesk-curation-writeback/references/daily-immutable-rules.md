# 模式 A：每日不重复精选环节细则（`daily_immutable`）

本文件规定每日例行策展（永久去重模式）的强制执行步骤。执行前请确保本地双仓（`obsidian-vault` 与 `music-daily`）已完成同步。

---

## 1. 强制前置核验与两阶段可恢复协议

1. **业务日锁定**：取 `Asia/Shanghai` 当前日期 `YYYY-MM-DD`。**严禁自动补推漏掉的历史日期**。
2. **先读全量策略与账本（Unreadable != Empty）**：
   - 必须完整读取本地 `obsidian-vault` 中的 `.recommendation/policy.json`、`.recommendation/PUBLISHING.md`、`scripts/publication_bundle.py`、`scripts/publication_recovery.py`；
   - 读取对应项目的完整永久历史（`.recommendation/history/{youtube,reddit,english,journal}.jsonl`、`music-daily/state/tracks.json`）、近期 Daily 笔记，以及 `.recommendation/transactions/**` 下所有未归档的 `PREPARED` 预留记录；
   - **任何文件不可读、截断或解析报错均视为 Unverified，必须立即中止并报错，绝不当作空历史继续选品**。
3. **操作幂等与 `PREPARED` 恢复**：
   - 固定 `operation_id = "<project>:<YYYY-MM-DD>"`；
   - 若已存在 `PUBLISHED` 记录，禁止重写历史推荐，仅将其同步刷新至 OmniDesk 前端；
   - 若已存在 `PREPARED` 预留捆绑包，必须按原锁定的条目集合与正文继续完成发布（**严禁丢弃已 `PREPARED` 条目重新随机选题**）。
4. **手写内容保护**：
   - 目标 Markdown 若已存在，仅替换 `<!-- AUTO:START -->` 与 `<!-- AUTO:END -->` 之间的内容，完整保留锚点之外的 YAML Frontmatter 自定义字段与底部 `## Notes` 手写笔记。
5. **真实发布证据（不可用 QA 代替）**：
   - 调用 `publish_daily_bundle` / `run_daily_pipeline` 时必须提供 `publication_evidence`：`status = "PUBLISHED"`、`publication_verified = true`、匹配的 `operation_id` / `project` / `business_date`，以及可定位已提交产物的 `receipt`；
   - 仅有 `qa_verified = true` 时服务端返回 `needs_verified_bundle`；
   - 音乐项目必须在写前端前用 `media.playlist.v1 sync_daily_authorized(dry_run: true)` 校验真实双仓 `PUBLISHED` 事务与 5 首曲目。

---

## 2. 四大每日子管线规格与真实路径

### (1) `news-youtube-reddit:YYYY-MM-DD`
- **输出路径（`obsidian-vault`）**：
  - `News/Daily/YYYY-MM-DD.md`（3–6 条重要新闻，含 `Executive View`、`事实摘要`、`Why it matters`、`Long-term impact`、`What to watch next`、`Sources`，区分政策倡议、统计口径与研究证据的确定性层级）
  - `Resources/YouTube/Daily/YYYY-MM-DD.md`（严格 5 条视频，永久 `video_id` 去重 + 频道冷却；若无法直接验证完整播放页而采用发布方官网原始嵌入核验，须记录 `verification_method: creator_official_page` 并在笔记中诚实写明核验范围）
  - `Resources/Reddit/Daily/YYYY-MM-DD.md`（严格 10 条帖子，永久 `post_id` 去重 + 子版块多样性；在用户豁免逐帖打开原页时，记录 `original_post_verification: not_performed_user_waived` 并在笔记与前端卡片中如实披露）
- **历史账本更新**：`.recommendation/history/youtube.jsonl`、`.recommendation/history/reddit.jsonl`。

### (2) `music:YYYY-MM-DD`
- **选品与核验**：
  - 严格 5 首 Apple Music 中国区（`storefront: "cn"`）实证可播曲目（先通过 `media.playlist.v1` 的 `preflight_cn_catalog` 或 Apple Music CN Catalog API 核实数字 `song_id`、歌名、艺人、专辑与国区 URL）；
  - 遵守 `state/tracks.json` 永久曲目与录音/作品级（`canonical_id` / `recording_history.py`）不重复约束及艺人/专辑冷却窗口；
  - 保持深度中文导听文风：每首提供多段式 `analysis_summary`（听感尺度、编配细节、声部关系）与具体可操作的 `listening_cue`（第一遍/第二遍怎么听）。
- **双仓落盘路径**：
  - `music-daily`：`recommendations/YYYY/MM/YYYY-MM-DD.json` + `state/tracks.json`（通过 `scripts/validate.py` 与 `scripts/publication_gate.py` 校验）；
  - `obsidian-vault`：`Music/Daily/YYYY-MM-DD.md` + `Music/Daily/manifests/YYYY-MM-DD.json` + `Music/Daily/state/tracks.json`。
- **歌单反写门禁**：双仓一致性与 `local_sync_gate.py` 校验通过后，调用 `media.playlist.v1` (`sync_daily_authorized`) 同步至累计歌单 **`p.YJXV7dEIeb3GzZX`（`ChatGPT Daily 5`）** 及当日歌单 `ChatGPT Daily 5 · YYYY-MM-DD`。
  - **已知历史异常**：`2026-11-06` 的提交清单仅有 4 首。该日期必须作为 `history_anomalies` 显式报告；不得静默跳过，也不得伪造成 5 首或自动补一首以凑数。

### (3) `english:YYYY-MM-DD`
- **按星期路由输出路径（`obsidian-vault/50-courses/English/`）**：
  - **周一至周五**：`Daily/YYYY-MM-DD.md` + `Translation/YYYY-MM-DD.md` + `Vocabulary/YYYY-MM-DD.md`；
  - **周六**：`Weekly/TV/YYYY-MM-DD.md`（影视地道表达精讲）；
  - **周日**：`Weekly/YYYY-MM-DD.md`（深度长文伴读）；
  - 永久账本追加：`.recommendation/history/english.jsonl`。
- **前端联动与饮食红线**：
  - 将当日核心词条同步放入 `publish_daily_bundle.vocab_cards[]` 以注入 `BookWorkbench` SM-2 生词本；
  - **`今日饮食` 强制隔离**：在对话回复末尾（及 `ephemeral_chat_note`）提供简明实用的 `今日饮食` 建议，**严禁将任何饮食字样写入上述 Markdown 文件或 `english.jsonl`**。

### (4) `journal:YYYY-MM-DD`
- **选品与质量红线**：
  - 每日 2–4 篇同行评审原始研究或关键预印本（默认目标 3 篇，**质量优先、宁缺毋滥**，绝不凑数；不把同一工作的预印本与正式发表版重复计数）；
  - 通过 PubMed / PMC / Crossref / 出版商页面核实英文原题、作者、年份、期刊、DOI、更正/撤稿状态，诚实标注实际阅读范围（如是否读完 PDF 主文、是否未读 SI）。
- **输出路径（`obsidian-vault`）**：
  - `00-inbox/Journal/YYYY-MM-DD.md`（包含 `## 今日科学问题`、`今日最值得精读`、`①/②/③` 分篇深度拆解与 `## 建议阅读顺序`）
  - `00-inbox/Journal/Index.md`
  - `.recommendation/history/journal.jsonl`
