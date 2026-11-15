# OmniDesk AI Agent 进驻、历史推荐回灌、双模策展与前端/歌单反写协议（v1）

> **核心目标**：
> 1. **AI Agent 真正进驻 OmniDesk**：既作为**内驻策展大脑**直接在桌面端（`F8` 伴读栏与三大工作台）主动检索、核验并主导内容推荐，也通过本地控制网关接纳**外部终端/定时 Agent**（Antigravity、Codex、Claude Code、OpenCode 等）直连入站。
> 2. **克隆双仓并把原来的全部历史推荐搞回来（Git Mirror & Full Archive Rehydration）**：自动在本地克隆并同步 [`ZJFILOVE/obsidian-vault`](https://github.com/ZJFILOVE/obsidian-vault) 与 [`ZJFILOVE/music-daily`](https://github.com/ZJFILOVE/music-daily)，将自 `2026-09-04` 以来积累的全部历史每日新闻、YouTube、Reddit、Apple Music 每日 5 首深度乐评、英语精读/翻译/词汇及科学期刊论文解析回灌进三大工作台（支持按日期时间轴回溯、全文检索与一键连播）。
> 3. **单一渐进式 Skill 驱动反写**：统一使用仓库内**唯一入口 Skill**（[`.agents/skills/omnidesk-curation-writeback/SKILL.md`](../.agents/skills/omnidesk-curation-writeback/SKILL.md)）驱动前端反写与 **Apple Music 歌单写回**，并在底层用硬防火墙隔离**「每日不重复精选（`daily_immutable`）」**与**「按需可重复推荐（`interactive_ephemeral`）」**两套推荐系统。

---

## 一、总体架构：AI Agent 进驻 + 双仓历史回灌 + 全栈反写闭环

通过**「内驻/入站双通道 Agent + 双仓 Git 本地镜像回灌引擎 + 本地双模策展存储」**，彻底打通历史沉淀、每日例行策展与即时灵感交互：

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                       统一策展 Skill (单一入口 + 渐进式披露)                      │
│          .agents/skills/omnidesk-curation-writeback/SKILL.md + references/      │
└───────────────────────┬─────────────────────────────────────┬───────────────────┘
                        │ 同一套规则与 JSON Schema             │ 同一套规则与 JSON Schema
                        ▼                                     ▼
┌───────────────────────────────────────┐   ┌─────────────────────────────────────┐
│ 路径 A：内驻主控 Agent (In-App)        │   │ 路径 B：外部/定时 Agent 入站直连     │
│ plugin-magpie-agent (ai.curation.v1)  │   │ Antigravity / Codex / Claude Code   │
│ 复用本机 Magpie 127.0.0.1:3425 网关    │   │ 通过 omnidesk-pm rpc / mcp-serve    │
│ 在 F8 伴读栏 / 工作台内直接对话与触发  │   │ 连接 $XDG_RUNTIME_DIR/omnidesk/...  │
└───────────────────────┬───────────────┘   └─────────────────┬───────────────────┘
                        │                                     │
                        └──────────────────┬──────────────────┘
                                           ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                HostKernel 入站控制桥 (ControlServer · control.sock)              │
│          CapabilityRegistry::call(...)  +  EntityBus::publish(...)              │
└──────────────┬───────────────────────────────────────────┬──────────────────────┘
               │ recommend.feed.v1                         │ media.playlist.v1
               ▼                                           ▼
┌──────────────────────────────────────────┐ ┌────────────────────────────────────┐
│ plugin-ai-curator (回灌与跨三域反写中心)  │ │ plugin-music (Apple Music 写回底座) │
│ 1. 受管本地克隆 repos/ (或工作区同级目录):│ │ Adam 引擎 + Apple Music Library API │
│    • ZJFILOVE/obsidian-vault             │ │ • daily_authorized -> p.YJXV7d...  │
│    • ZJFILOVE/music-daily                │ │ • interactive_ephemeral -> 灵感歌单│
│ 2. 本地索引 <XDG_DATA_HOME>/omnidesk/    │ └──────────────────┬─────────────────┘
│    • curation/daily/       (历史+今日)    │                    │
│    • curation/interactive/ (可重复灵感池) │                    │
└──────┬───────────────┬──────────────┬────┘                    │
       ▼               ▼              ▼                         ▼
┌─────────────┐ ┌─────────────┐ ┌─────────────┐        ┌──────────────────────────┐
│MediaWorkbench│ │TextWorkbench│ │BookWorkbench│        │ Apple Music 云端资料库   │
│YouTube/音乐 │ │News/Reddit/ │ │英语双语精读/│        │ + MPVCore 本地无损播放队 │
│今日+历史归档│ │期刊今日+归档│ │SM-2 生词闪卡│        │   列实时同步             │
└─────────────┘ └─────────────┘ └─────────────┘        └──────────────────────────┘
```

### 1.1 两种 Agent 进驻形态（共享同一套 Capability 接口）

1. **内驻主控 Agent（In-App Embedded Agent）**：
   - 由 `plugin-magpie-agent`（提供 `ai.curation.v1`）联合 `plugin-ai-curator`（提供 `recommend.feed.v1`）在宿主后台 `JobCenter` 中运行。
   - 模型推理直接走本机 Magpie 网关 `http://127.0.0.1:3425/v1/chat/completions`（绑定专属组 `group/omnidesk-active`，零改动终端 Harness 默认配置）。
   - 用户可在 `F8` 伴读栏（[`AiCompanionDock`](../host/src/companion_dock.cpp)）输入自然语言需求实时生成推荐并反写前端/歌单，或点击工作台顶部的「同步历史归档 / 生成今日精选」按钮触发。
2. **外部 / 定时 Agent 入站直连（Inbound External Agent）**：
   - 宿主 [`HostKernel`](../host/src/kernel.cpp) 启动时监听权限为 `0600` 的本地 Unix Domain Socket：`$XDG_RUNTIME_DIR/omnidesk/control.sock`（若未设置 `XDG_RUNTIME_DIR`，回退至 `<XDG_DATA_HOME>/omnidesk/run/control.sock`）。
   - `omnidesk-pm` CLI 提供入站命令（及可选 stdio MCP 桥接 `omnidesk-pm mcp-serve`）：
     - `omnidesk-pm rpc <capability_id> '<json_payload>'`：向运行中的 `omnidesk` 宿主转发 Capability 调用；
     - `omnidesk-pm emit <topic> '<json_payload>'`：向宿主 `EntityBus` 广播事件驱动 `brls::sync` 实时刷新；
     - **离线兜底（Offline Fallback）**：当桌面端 GUI 未启动时，`omnidesk-pm curate ...` / `omnidesk-pm rehydrate` 直接操作本地克隆仓与 `<XDG_DATA_HOME>/omnidesk/curation/`，下次打开桌面端即刻呈现全部历史与最新推荐。

---

## 二、双仓自动克隆与历史推荐全量回灌协议（「把原来的推荐搞回来」）

为了让桌面端一启动就完整找回此前积累的全部高质量推荐，并使本机 Agent 拥有毫秒级可校验的本地永久去重账本，`plugin-ai-curator` 内置 **Git 镜像管理与历史归档回灌器（Archive Rehydrator）**。

### 2.1 本地克隆路径发现与增量同步规则

按以下优先级定位两座上游仓库（支持用户在插件配置表单中自定义路径）：
1. **显式配置路径**：`ConfigStore` 中的 `obsidian_vault_dir` 与 `music_daily_dir`；
2. **工作区同级目录探测**：若 `/home/zhu/Projects/obsidian-vault` 或 `/home/zhu/Projects/music-daily` 已存在且包含 `.git`，直接复用；
3. **受管默认克隆目录**：`<XDG_DATA_HOME>/omnidesk/curation/repos/obsidian-vault` 与 `<XDG_DATA_HOME>/omnidesk/curation/repos/music-daily`。

**同步安全守卫**：
- 首次缺失时自动执行无 Shell 参数数组调用：`git clone --branch main https://github.com/ZJFILOVE/obsidian-vault.git <dir>` 与 `https://github.com/ZJFILOVE/music-daily.git <dir>`；
- 已存在时先检查 `git status --porcelain`：**仅在工作树干净时**执行 `git fetch origin main` 与 `git merge --ff-only origin/main`，绝不覆盖用户本地尚未提交的 Obsidian 手写笔记；若工作树有本地未提交改动，照常回灌本地最新文件并在状态栏提示 `本地有未推送笔记修改`。

### 2.2 真实仓库目录到三大工作台的精确回灌映射表

回灌器基于文件 `(rel_path, size, mtime_ns / git_blob_sha)` 维护增量清单 `<XDG_DATA_HOME>/omnidesk/curation/rehydrate_manifest.json`（首次全量扫描解析 `2026-09-04` 至今全部归档，后续启动仅重解析变更文件，耗时 `< 30 ms`）：

| 源仓库与真实相对路径 | 结构化特征与解析规则 | 回灌后的前端落点与交互能力 |
| :--- | :--- | :--- |
| **`music-daily`**:<br>`recommendations/YYYY/MM/YYYY-MM-DD.json`<br>`state/tracks.json`<br>**`obsidian-vault` 镜像**:<br>`Music/Daily/manifests/YYYY-MM-DD.json`<br>`Music/Daily/YYYY-MM-DD.md` | • 直接读取 `daily5.schema.json` 结构：`date`、`tracks[0..4]`（含 `position`、`title`、`artist`、`album`、`canonical_id`、`apple_music.{song_id, url, storefront}`、`analysis_summary`、`listening_cue`）<br>• 合并 `Music/Daily/YYYY-MM-DD.md` 中 `<!-- AUTO:END -->` 之后的 `## Notes` 用户手写批注 | **`MediaWorkbench`**：<br>• 子栏目 `每日 5 首 · 最新`、`每日 5 首 · 往期日历 (YYYY-MM-DD)`、`每日 5 首 · 全量曲库连播`<br>• 点击任意历史曲目直接通过 `plugin-music` 无损播放，右侧展示完整多段 `analysis_summary` 编曲解析与 `listening_cue` 两遍听法指南 |
| **`obsidian-vault`**:<br>`Resources/YouTube/Daily/YYYY-MM-DD.md`<br>`.recommendation/history/youtube.jsonl` | • 解析 YAML Frontmatter（`type: youtube-daily`）与 `<!-- AUTO:START/END -->` 区块：<br>• 提取 `## 今日优先级`、核验声明引用块、以及 `## 1..5. <Title>` 下的 `频道`、`主题`、`链接`（提取 11 位 `video_id` 构造 `hqdefault.jpg` 封面）、`### Why watch` 与 `### 核验说明`（提取 `verification_method`） | **`MediaWorkbench`**：<br>• 子栏目 `YouTube 每日精选 · 最新` 与 `YouTube · 往期归档`<br>• 卡片底部展示主题徽章（含 `wildcard` 标记）与核验范围说明，点击调用 `plugin-youtube` / `MPVCore` 播放并挂载 `Why watch` 至 `F8` 伴读栏 |
| **`obsidian-vault`**:<br>`News/Daily/YYYY-MM-DD.md` | • 解析 `type: daily-news-daily`：<br>• 提取 `## Executive View` 总纲，以及每条 `## N. <标题>` 的 `事件日期`、`影响等级`（如 `4/5`）、`### 事实摘要`、`### Why it matters`、`### Long-term impact`、`### What to watch next` 与 `### Sources` 原始链接 | **`TextWorkbench`**：<br>• 子栏目 `每日早报 (News)`（按期聚合或拆条浏览）<br>• 阅读器以结构化分节卡片呈现事实与推演边界，支持一键在托管阅读窗打开原始信源 |
| **`obsidian-vault`**:<br>`Resources/Reddit/Daily/YYYY-MM-DD.md`<br>`.recommendation/history/reddit.jsonl` | • 解析每日 10 条精选社区帖：提取子版块 `r/...`、标题、原帖链接、核心讨论价值、`Why read` 以及 `original_post_verification` 诚实核验状态 | **`TextWorkbench`**：<br>• 子栏目 `Reddit 每日精选`（支持按日期与子版块过滤）<br>• 点击可在 `plugin-reddit` 中实时拉取最新可折叠嵌套评论树（`CommentTreeView`）对照阅读 |
| **`obsidian-vault`**:<br>`00-inbox/Journal/YYYY-MM-DD.md`<br>`00-inbox/Journal/Index.md`<br>`.recommendation/history/journal.jsonl` | • 解析 `type: journal-daily`：<br>• 提取 `## 今日科学问题`（含「今日最值得精读」高亮提示）、`## ①/②/③ <英文原题>` 下的中文译题、作者/年份/期刊、`[DOI]`/`[PubMed]`/`[PMC]` 链接，以及 `问题与观测`、`作者解释`、`证据边界与 reviewer_synthesis`、`更正说明`、`为何推荐`、`追问`、`阅读范围与待核`、`## 建议阅读顺序` | **`TextWorkbench`（`category = "journal"` 专属排版）**：<br>• 子栏目 `科学期刊 · 每日精读` 与 `Journal 专题索引`<br>• 完整渲染论文元数据卡与审稿级证据边界拆解，顶部提供一键调用 `book.bilingual_pdf.v1` 转交 BabelDOC 生成双栏 PDF 按钮 |
| **`obsidian-vault`**:<br>`50-courses/English/Daily/YYYY-MM-DD.md`<br>`50-courses/English/Translation/YYYY-MM-DD.md`<br>`50-courses/English/Vocabulary/YYYY-MM-DD.md`<br>`50-courses/English/Weekly/YYYY-MM-DD.md`<br>`50-courses/English/Weekly/TV/YYYY-MM-DD.md`<br>`.recommendation/history/english.jsonl` | • 按日期聚合工作日三件套（`Daily` 精读、`Translation` 翻译实训、`Vocabulary` 核心词表）、周六 `Weekly/TV` 影视地道表达与周日 `Weekly` 深度长文<br>• 从历史 `Vocabulary/*.md` 提取词元、释义、词源与语境例句 | **`BookWorkbench` + `TextWorkbench`**：<br>• `BookWorkbench` 书架挂载 `英语每日精读与翻译归档册`（双语对照伴读）<br>• 历史 `Vocabulary/*.md` 幂等导入 `SM-2 闪卡复习台`（`vocab.sm2.v1`，不覆盖已有复习进度），支持联动 `media.clip_jump.v1` 跳播飞牛美剧原声切片 |

---

## 三、双模推荐隔离契约：每日不重复 vs. 按需可重复

在完整找回历史归档的基础上，新发起的所有 AI 推荐在协议层、存储层、去重账本与歌单目标四个维度上设置**硬防火墙**，强制每次策展请求显式声明 `mode`（仅限 `"daily_immutable"` 或 `"interactive_ephemeral"`）：

| 维度 | 模式 A：`daily_immutable`（每日不重复环节） | 模式 B：`interactive_ephemeral`（按需可重复推荐系统） |
| :--- | :--- | :--- |
| **触发方式** | 每日定时任务、工作台「生成今日精选」按钮、或指定 `Asia/Shanghai` 业务日 `YYYY-MM-DD` 的每日策展指令 | 用户在 `F8` 伴读栏、搜索框或 Agent 会话中输入心情/主题/种子内容/上下文实时触发 |
| **是否允许重复推荐** | **严格禁止重复**（受永久历史账本、作品级归一化与创作者/歌手冷却期硬约束） | **明确允许重复**（既可全网检索新老内容，也可直接从已回灌的本地历史佳作库中按当下心情二次编排重温；严禁将历史账本作为黑名单剔除） |
| **永久历史账本读写** | **必读 + 门禁后追加写**：读取本地克隆仓 `.recommendation/policy.json`、`.recommendation/history/*.jsonl`、`state/tracks.json` 与全部 `PREPARED` 事务单；仅在校验通过后追加记录并推送到远端 GitHub | **只读可选、严禁写入**：`record_permanent_history` 恒为 `false`；仅可只读匹配本地回灌索引以标注 `曾于 YYYY-MM-DD 推荐 · 经典重温` 徽章，**绝不修改本地克隆仓内的任何文件、历史账本或事务目录** |
| **Git / Obsidian 副作用** | 按两阶段可恢复协议更新本地 `obsidian-vault` / `music-daily` 克隆仓（严格保留 `<!-- AUTO:START/END -->` 之外的 `## Notes` 手写内容），校验无误后 `git commit && git push` | **零仓库污染**：完全不触碰 `obsidian-vault` 与 `music-daily` 的每日正式目录与历史账本 |
| **本地前端持久化目录** | `<XDG_DATA_HOME>/omnidesk/curation/daily/<YYYY-MM-DD>/<project>.json`（与 Git 克隆仓双向同步） | `<XDG_DATA_HOME>/omnidesk/curation/interactive/sessions/<session_id>.json` 及 `pinned.json` |
| **Apple Music 歌单目标** | 经 `local_sync_gate.py` / QA 门禁后同步至固定累计歌单 **`p.YJXV7dEIeb3GzZX`（`ChatGPT Daily 5`）** 及可选当日歌单 `ChatGPT Daily 5 · YYYY-MM-DD` | **硬编码禁止修改 `p.YJXV7dEIeb3GzZX`**；仅允许写入 `MPVCore` 即时播放队列、本地会话歌单或专用可重复歌单（如 `OmniDesk AI 灵感电台` 或用户命名歌单） |
| **前端视觉徽章** | `每日精选 · YYYY-MM-DD`（使用 `status/ok_{bg,fg}` 语义色） | `AI 灵感推荐 · 可重温`（若命中历史归档则附 `经典重温 · YYYY-MM-DD` 副标，使用 `domain/core` 语义色） |

---

## 四、模式 A 详规：每日不重复推荐环节（`daily_immutable`）

模式 A 直接在本地双仓克隆上运行，完整继承 `ZJFILOVE/obsidian-vault`（`.recommendation/policy.json`、`.recommendation/PUBLISHING.md`、`scripts/publication_bundle.py`、`scripts/publication_recovery.py`）与 `ZJFILOVE/music-daily`（`scripts/publication_gate.py`、`scripts/local_sync_gate.py`、`scripts/apple_music_sync.py`）的全部生产级不变量：

1. **业务日与防补推**：统一采用 `Asia/Shanghai` 当前日期 `YYYY-MM-DD`；**禁止自动补推漏掉的历史日期**。
2. **先同步、先读全量账本后选题（Unreadable != Empty）**：
   - 先对本地双仓执行安全快进拉取（`git pull --ff-only`）；
   - 完整读取 `.recommendation/policy.json`、目标项目全部永久历史（`.recommendation/history/{youtube,reddit,english,journal}.jsonl`、`music-daily/state/tracks.json`）、近期 Daily 笔记及 `.recommendation/transactions/**` 下所有 `PREPARED` 预留记录；任何输入不可读或截断均视为**未核验（Unverified）**并中止。
3. **两阶段事务恢复与手写批注保护**：
   - 锁定 `operation_id`（`news-youtube-reddit:YYYY-MM-DD`、`music:YYYY-MM-DD`、`english:YYYY-MM-DD`、`journal:YYYY-MM-DD`）；若存在未完结的 `PREPARED` 捆绑包，必须按原预留集恢复发布，严禁丢弃重选；
   - 更新已有 Markdown 笔记时严格保留 `<!-- AUTO:START -->` 之前与 `<!-- AUTO:END -->` 之后的全部内容（如底部 `## Notes`）。
4. **四大子管线特殊红线**：
   - **`news-youtube-reddit`**：News 3–6 条多源交叉核验；YouTube 5 条（允许退回发布方官方页面核验 `verification_method: creator_official_page` 并诚实声明核验边界）；Reddit 10 条（豁免原帖逐页抓取时记录 `original_post_verification: not_performed_user_waived` 并诚实标注）。
   - **`music`**：严格 5 首 Apple Music 国区（`storefront: "cn"`）实证可播歌曲，同步写入 `music-daily/recommendations/YYYY/MM/YYYY-MM-DD.json`、`state/tracks.json` 以及 `obsidian-vault/Music/Daily/YYYY-MM-DD.md`、`Music/Daily/manifests/YYYY-MM-DD.json`、`Music/Daily/state/tracks.json`，QA 通过后同步歌单 `p.YJXV7dEIeb3GzZX`。
   - **`english`**：周一至周五写入 `50-courses/English/{Daily,Translation,Vocabulary}/YYYY-MM-DD.md`，周六写入 `50-courses/English/Weekly/TV/YYYY-MM-DD.md`，周日写入 `50-courses/English/Weekly/YYYY-MM-DD.md`，追加 `english.jsonl`；**`今日饮食` 建议仅输出到当次聊天/伴读栏气泡，严禁写入任何仓库文件或历史记录**。
   - **`journal`**：2–4 篇（默认目标 3 篇，质量优先、宁缺毋滥），写入 `00-inbox/Journal/YYYY-MM-DD.md`、`00-inbox/Journal/Index.md` 与 `journal.jsonl`，保持高规格审稿式中文拆解（`问题与观测`、`作者解释`、`证据边界与 reviewer_synthesis`、`追问`、`阅读范围与待核`）。

---

## 五、模式 B 详规：按需可重复推荐系统（`interactive_ephemeral`）

模式 B 面向用户日常在桌面端的即时探索需求，核心原则是**「懂上下文、允许重温、即时反写、绝不脏账」**：

1. **多源输入信号**：支持自然语言口令（心情、工作流场景、流派、科研问题）叠加当前焦点工作台上下文（正在播放的曲目、正在读的论文或刚查过的生词）。
2. **三种可重复策略（`repeat_policy`）**：
   - `"allow_any"`（默认）：全网新内容与历史已推佳作自由混排，只看当下契合度；
   - `"prefer_familiar"`：优先从本地已回灌的历史精选库（如过去推过的几百首 Daily 5 曲目、往期 YouTube 视频或 Journal 论文）中按用户当前主题重新策展组合（「从以前推过的歌里挑 5 首适合静心改论文的纯音乐」）；
   - `"session_dedup_only"`：仅在当前单次多轮对话内避免重复，仍允许推荐历史每日精选条目。
3. **只读回标与零污染守卫**：
   - 若候选条目命中本地历史索引，在 `extra.previously_recommended_on` 填入首次推荐日期 `"YYYY-MM-DD"`，前端展示 `经典重温 · YYYY-MM-DD` 徽章；
   - 强制 `record_permanent_history = false`，绝不写入 `.recommendation/` 或 `music-daily/state/`；
   - 歌单反写仅允许进入 `MPVCore` 播放队列或独立可重复歌单（如 `"OmniDesk AI 灵感电台"`），服务端硬拦截对 `p.YJXV7dEIeb3GzZX` 的任何修改。

---

## 六、核心接口契约（Host Control Socket + 三大标准 Capability）

### 6.1 宿主入站控制网关协议（`control.sock` NDJSON RPC）

宿主 `ControlServer` 监听 `$XDG_RUNTIME_DIR/omnidesk/control.sock`（`0600`），采用与 `WorkerSupervisor` 一致的单行 UTF-8 NDJSON 帧格式（上限 `8 MiB`）：

- **请求帧**：
  ```json
  {
    "id": "req-001",
    "op": "capability.call",
    "capability": "recommend.feed.v1",
    "caller": "external-agent:antigravity",
    "payload": {
      "action": "bootstrap_and_rehydrate",
      "pull_remote": true
    }
  }
  ```
- **响应帧**：
  ```json
  {
    "id": "req-001",
    "ok": true,
    "provider_plugin_id": "plugin-ai-curator",
    "data": {
      "obsidian_vault_dir": "/home/zhu/.local/share/omnidesk/curation/repos/obsidian-vault",
      "music_daily_dir": "/home/zhu/.local/share/omnidesk/curation/repos/music-daily",
      "dates_indexed": 68,
      "music_tracks_hydrated": 340,
      "youtube_videos_hydrated": 335,
      "news_days_hydrated": 64,
      "reddit_days_hydrated": 64,
      "journal_days_hydrated": 13,
      "english_notes_hydrated": 162
    },
    "error": ""
  }
  ```
- **CLI 快捷命令**：
  ```bash
  # 一键克隆/拉取双仓并将全部历史推荐回灌进 OmniDesk 本地策展库
  omnidesk-pm rpc recommend.feed.v1 '{"action":"bootstrap_and_rehydrate","pull_remote":true}'

  # 推送单日正式精选或交互式可重复推荐集
  omnidesk-pm rpc recommend.feed.v1 --file /path/to/curation_payload.json

  # 写回 Apple Music 歌单
  omnidesk-pm rpc media.playlist.v1 --file /path/to/playlist_sync.json
  ```

### 6.2 `recommend.feed.v1` —— 历史回灌与三域前端反写接口（提供者：`plugin-ai-curator`）

`plugin-ai-curator`（`kind = "hybrid"`，`domain = "core"`）向 `DomainRegistry` 注册 `IMediaSource`、`ITextSource`（`category() == "multi"`，原生适配 [`TextWorkbench`](../host/src/text_workbench.cpp) 动态多子栏目过滤）与 `IBookSource`：

| `action` | 适用模式 | 核心职责 |
| :--- | :--- | :--- |
| `"bootstrap_and_rehydrate"` | 初始化 / 同步 | 自动 `git clone` 或快进更新 `obsidian-vault` 与 `music-daily`，增量解析全部历史归档并回灌至三大工作台与 SM-2 词库，广播 `"entity.recommend.updated"` |
| `"list_archive_dates"` | 只读查询 | 返回已回灌的全部历史业务日列表（降序）及每日各板块条目计数，驱动工作台顶部「往期日历选择器」 |
| `"search_archive"` | 只读查询 | 在本地已回灌的全量历史推荐（乐评、导看、早报、Reddit、期刊、英语）中按关键词/标签/艺人检索，供前端搜索或模式 B `prefer_familiar` 选品使用 |
| `"publish_daily_bundle"` | `daily_immutable` | 在 `qa_verified == true` **且** `publication_evidence` 证明上游已提交 `PUBLISHED` 事务后，将当日新推荐写入本地缓存与双仓镜像（处理 `ephemeral_chat_note` 瞬时饮食气泡而不落盘），刷新前端并触发生词入库；只有 QA 标记时返回 `needs_verified_bundle` |
| `"push_interactive_set"` | `interactive_ephemeral` | 将用户按需触发的可重复推荐集写入 `curation/interactive/sessions/`（强校验 `record_permanent_history == false`），实时推送至前端 `AI 灵感推荐` 栏目 |
| `"pin_item" \| "dismiss_item" \| "clear_interactive" \| "status"` | 狀態管理 | 收藏固定单条灵感推荐、隐藏条目、一键清空临时会话流或查询双仓同步健康度 |

### 6.3 `media.playlist.v1` —— Apple Music 歌单写回接口（提供者：`plugin-music`）

在 [`omnidesk-plugin-music`](https://github.com/ZJFILOVE/omnidesk-plugin-music) 现有 `AdamClient` 基础上新增 Apple Music Library 写回动作：

1. **`"preflight_cn_catalog"`**：调用 `/v1/catalog/cn/songs?ids=...` 批量实证核验国区曲目存在性与可播状态。
2. **`"sync_daily_authorized"`**（仅限 `mode == "daily_immutable"`、`qa_verified == true`，且能读取到与 `operation_id` / `business_date` 完全匹配的已提交 `PUBLISHED` 事务与 QA 收据）：
   - 持 `0600` 文件锁与检查点（兼容 `~/.local/state/daily5/apple-music-playlist.json`），向固定累计歌单 **`p.YJXV7dEIeb3GzZX`（`ChatGPT Daily 5`）** 执行仅缺失追加（Missing-only Append，不删不重排），可选同步创建当日歌单 `ChatGPT Daily 5 · YYYY-MM-DD`，完成后强制读回校验（Readback Verification）并广播 `"entity.music.playlist_synced"`；
   - 亦支持传入 `sync_all_rehydrated: true` 对本地刚克隆回灌的历史日期执行幂等差集补齐（自动跳过已在 `p.YJXV7dEIeb3GzZX` 中的曲目）；
   - `2026-11-06` 是已确认的历史异常：提交清单只有 4 首而非 5 首。系统会显式校验该清单并在 `history_anomalies` 中报告，**不会静默跳过或伪造成完整 5 首**。
3. **`"write_interactive_playlist" | "enqueue_playback"`**（`mode == "interactive_ephemeral"`）：
   - **硬拦截保护**：任何试图指向 `p.YJXV7dEIeb3GzZX` 或 `"ChatGPT Daily 5*"` 名称的调用一律返回 `error_code = "protected_daily_playlist"`；
   - 云端写回前必须用 `preflight_cn_catalog` 返回的 `verified_ids` 做严格集合校验；预检失败、缺少 `verified_ids` 或部分不可播时一律禁止云端歌单写入；
   - 支持写入独立可重复歌单（默认 `"OmniDesk AI 灵感电台"` 或用户指定歌单名，支持包含历史已推曲目）及一键推入 `MPVCore` 当前无损播放队列；`local_checkpoint_only` 仅做本地暂存，不得标记为云端已同步。

### 6.4 `ai.curation.v1` —— 内驻 Agent 策展编排接口（提供者：`plugin-magpie-agent`）

- **`"curate_interactive"`**：供 `F8` 伴读栏与工作台直接输入口令调用内驻 Agent（走 Magpie `group/omnidesk-active`），自动串联「本地历史归档检索 -> 真实来源/国区曲库核验 -> `recommend.feed.v1` 前端反写 -> `media.playlist.v1` 灵感歌单/播放队列写回」。
  - **禁止合成兜底**：网关不可用且本地归档无命中时返回 `no_verified_candidates`；不得生成占位歌曲、虚构论文/书籍或伪造 DOI/URL。
  - 模型返回的文本/书籍必须能匹配本地归档中的真实条目；音乐必须通过 `preflight_cn_catalog` 的 `verified_ids` 核验，未通过项不得进入正式推荐。
  - 必须先完成前端反写，再执行可选歌单写回；歌单失败时保留已写入的前端结果，并通过 `partial_success` / `playlist_error` 如实返回。
  - `repeat_policy == "session_dedup_only"` 会在当前会话内按 `song_id` / URL / DOI / 标题身份去重，并返回 `skipped_session_duplicates`。
  - 缺少 `omnidesk-curation-writeback` Skill 或必需 references 时以 `skill_unavailable` fail closed。
- **`"run_daily_pipeline"`**：在宿主后台 `JobCenter` 执行指定项目的 `daily_immutable` 全链路（拉取最新双仓 -> 去重门禁 -> 撰写深度中文导读 -> 校验真实 `PUBLISHED` 事务/收据 -> 前端回灌 -> Apple Music 累计歌单同步）。
  - `qa_verified` 不能单独证明上游已发布；缺少 `publication_evidence` 时返回 `needs_verified_bundle`。
  - 音乐项目在写前端前先用 `media.playlist.v1` 的 `dry_run` 校验真实双仓 `PUBLISHED` 事务与 5 首曲目；授权失败时不得先发布前端。
  - 前端发布成功但云端歌单失败时返回 `ok = false` 且携带 `feed_published = true` / `partial_success = true`，不得伪报全链路成功。

---

## 七、单一渐进式 Skill 结构（`omnidesk-curation-writeback`）

采用单一入口 Skill + 模块化参考文档（Progressive Disclosure），内驻 Agent 与外部编码 Agent 共享同一份协议包：

```text
.agents/skills/omnidesk-curation-writeback/
├── SKILL.md                                     # 唯一入口：意图路由（回灌 / 每日不重复 / 按需可重复）、红线守卫与标准工作流
└── references/
    ├── archive-rehydration.md                   # 双仓克隆同步与 2026-09-04 以来全量历史推荐解析回灌规范
    ├── daily-immutable-rules.md                 # 模式 A 细则：四大每日管线真实路径、去重账本、PREPARED 事务与诚实标注规范
    ├── interactive-repeatable-rules.md          # 模式 B 细则：按需可重复排序、「经典重温」只读回标与零污染守卫
    └── writeback-rpc-and-apple-music.md         # 反写接口手册：omnidesk-pm rpc 调用模板、recommend.feed.v1 与 Apple Music 写回载荷
```

## 八、工作台栏目与消费入口

每日推荐和普通来源在列表层分开，消费入口保持共用：

| 工作台 | 普通来源 | 策展入口 | 共用能力 |
| --- | --- | --- | --- |
| 影视与音乐 | 实时视频、个人曲库、NAS | 每日策展；音乐中的每日音乐/交互推荐 | MPV、双语歌词、F8 伴读 |
| 文字与期刊 | 社区、期刊、订阅配方 | 每日精选及 News/Journal/Reddit/English/交互栏目 | 双语阅读器、原页、PDF 精读 |
| 书本与 PDF | 电子书和 PDF 书架 | 每日英语 | 伴读阅读器、统一 SM-2 生词库 |

- 每日栏目默认显示最新可用业务日；选定日期或全部归档由用户明确切换，不自动补推缺失日期。交互推荐与每日内容分别显示。
- 音乐列表、播放及上一曲/下一曲使用相同的来源、日期与搜索范围，避免把每日曲目和个人曲库、全部历史混入同一播放队列。
- 全局搜索跨来源保留归档。搜索载荷保留 `source_id`、媒体 `extra.curation_mode`、`extra.business_date`，使历史命中能打开正确消费入口。
- 示例布局不得与同一列表的真实数据混合；真实来源加载失败不能用虚构正文或歌词代替。

### 只读来源回标

`recommend.feed.v1` 新增 `lookup_provenance`：

```json
{"action":"lookup_provenance","items":[{"id":"live:123","source_id":"plugin-music","kind":"music","play_url":"https://music.apple.com/cn/song/1463501518"}]}
```

返回 `data.read_only = true` 和 `data.matches`，每个命中包含原请求 `id`、`source_id`、`previously_recommended_on`。每批最多 4096 项；普通来源以此展示“曾入选每日精选 · 日期”。匹配范围为已有每日发布与历史归档，交互会话本身不构成每日入选记录。支持歌曲/视频/Reddit ID、DOI、URL、规范化作品身份；歌曲不使用裸标题猜测。查询不会同步仓库、写入账本、保存本地状态或发布推荐事件。
