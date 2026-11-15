# 模式 B：按需可重复推荐系统细则（`interactive_ephemeral`）

当用户基于当前输入（心情、工作状态、流派偏好、正在阅读的论文/书籍、正在播放的曲目或特定科学问题）请求个性化推荐时，启用本模式。

---

## 1. 核心设计原则：可重复 + 懂语境 + 零账本污染

1. **明确允许重复推荐（Repeatable by Design）**：
   - 在交互探索场景下，用户往往希望听到最契合当下心境的曲目、重温经典论文或围绕特定主题拉出最佳代表作。
   - **严禁将 `.recommendation/history/*.jsonl` 或 `music-daily/state/tracks.json` 作为过滤黑名单**。
2. **三种重复控制策略（`repeat_policy`）**：
   - `"allow_any"`（默认）：全网新候选与历史已推佳作同台竞优，完全按与用户当前提示词的契合度排序；
   - `"prefer_familiar"`：当用户表达「从以前推过的歌/文章里挑……」「重温一下类似……的老推荐」时，优先调用 `recommend.feed.v1` 的 `search_archive` 从本地已回灌的历史精选库中筛选重组；
   - `"session_dedup_only"`：仅避免在当前单次多轮会话已展示过的条目里重复，仍允许命中过往每日精选。
3. **「经典重温」只读回标（Read-Only Provenance Badge）**：
   - 选定条目后，可以**只读**方式在本地历史索引中查询该 `song_id` / `video_id` / `doi` / `post_id` 是否曾在每日精选中出现；
   - 若命中（例如曾在 `2026-10-18` 推荐过），在条目 `extra.previously_recommended_on` 填入 `"2026-10-18"`，前端将自动展示 `经典重温 · 2026-10-18` 标签，兼顾复听自由与信息透明。

---

## 2. 零污染防火墙检查表（执行前必核）

在构造反写请求时，必须满足以下全部条件：
- [ ] `mode` 固定设为 `"interactive_ephemeral"`；
- [ ] `record_permanent_history` 显式设为 `false`；
- [ ] **不修改、不追加** `obsidian-vault/.recommendation/history/*.jsonl`、`.recommendation/transactions/`、`music-daily/state/tracks.json` 以及任何 `Daily/` 正式笔记文件；
- [ ] 若写回 Apple Music 歌单，**绝不指定 `p.YJXV7dEIeb3GzZX` 或任何以 `"ChatGPT Daily 5"` 开头的歌单名**（一律写入 `"OmniDesk AI 灵感电台"`、用户自定义歌单名，或仅推入 `MPVCore` 即时播放队列）。

---

## 3. 跨工作台组合策展范式

按需推荐不局限于单一媒介，支持根据用户一句话同时反写多个工作台：
- **纯音乐/氛围电台请求**（如「深夜改代码来 6 首冷冽电子与爵士，以前推过的也行，直接放进灵感歌单播起来」）：
  1. 先对候选曲目执行 `media.playlist.v1` -> `preflight_cn_catalog`，必须读取 `verified_ids` 并确认其集合覆盖全部候选；失败或部分不可播时不得写云端歌单；
  2. 调用 `recommend.feed.v1` -> `push_interactive_set`（写入 `media_items[]`，附每首歌契合当前场景的 `why_recommended` 与 `listening_or_reading_cue`）；
  3. 调用 `media.playlist.v1` -> `write_interactive_playlist`（`playlist_name: "OmniDesk AI 灵感电台"`, `enqueue_now: true`）。
- **科研/主题深潜请求**（如「围绕我正在读的蛋白质主动学习论文，给我配 2 篇必读方法论文和 2 个直观讲解视频」）：
  1. 核实论文 DOI/PubMed 与视频链接真实存在；
  2. 一次调用 `recommend.feed.v1` -> `push_interactive_set` 同时填入 `text_items[]`（`category = "journal"`，支持一键转 BabelDOC 双栏 PDF）与 `media_items[]`（`kind = "video"`），让用户在 `TextWorkbench` 与 `MediaWorkbench` 的 `AI 灵感推荐` 栏无缝联动学习。

---

## 4. 真实性与失败关闭（不可跳过）

1. **禁止合成兜底**：网关不可用且归档无命中时，必须返回 `no_verified_candidates`。不得生成占位歌曲、虚构论文/书籍、伪造 DOI/URL，或把无来源条目包装成“精选”。
2. **归档真实性**：模型输出的文本与书籍必须能匹配本地归档中的真实条目；非音乐媒体需有真实归档 URL/ID。模型只能补充推荐理由与导读，不得改写权威标题、作者、DOI 或曲目 ID。
3. **国区可播性**：所有 Apple Music 曲目必须由实时 `preflight_cn_catalog` 返回的 `verified_ids` 证明；历史归档与离线文件只能作为元数据，不能证明当前国区可播。
4. **执行顺序**：先完成 `push_interactive_set` 前端反写，再尝试可选歌单/播放队列；部分失败必须保留前端结果并返回 `partial_success` / `playlist_error`，不得伪报成功。
5. **会话去重**：`session_dedup_only` 只对当前 `session_id` 内已展示条目按歌曲 ID、URL、DOI 或标题身份去重；不得把每日永久历史当作黑名单。
6. **Skill 缺失**：Skill 或必需 references 缺失时以 `skill_unavailable` fail closed。
