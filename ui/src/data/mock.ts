export type DomainKind = 'media' | 'text' | 'book' | 'core';
export type CurationMode = 'daily_immutable' | 'interactive_ephemeral';

export interface LyricLine { timeSec: number; original: string; translation: string; note?: string; }
export interface ChapterSegment { id: string; startSec: number; endSec: number; label: string; kind: 'content' | 'sponsor' | 'ad'; }

export interface MediaItem {
  id: string;
  sourceId: string;
  kind: 'video' | 'music' | 'nas';
  title: string;
  subtitle: string;
  creator: string;
  durationSec: number;
  durationLabel: string;
  aspect: '16:9' | '1:1';
  badge: string;
  curationMode?: CurationMode;
  businessDate?: string;
  previouslyRecommendedOn?: string;
  verificationLabel?: string;
  thumbGradient: string;
  thumbTag: string;
  playUrl: string;
  audioUrl?: string;
  httpHeaders?: Record<string, string>;
  qualityLabel?: string;
  streamResolver?: StreamResolverDescriptor;
  summary: string;
  listeningCue?: string;
  appleMusicSongId?: string;
  chapters?: ChapterSegment[];
  lyrics?: LyricLine[];
  nasMeta?: { codec: string; resolution: string; path: string; subtitleStatus: string; imdbRating: string; clipQuote?: string };
}

export interface StreamResolverDescriptor {
  capability: string;
  payload: Record<string, unknown>;
  qualityOptions: string[];
  settingsAction?: string;
}

export type TextChannelId = 'all' | 'journal' | 'news' | 'reddit' | 'linuxdo' | 'zhihu';
export interface ArticleBlock { id: string; kind: 'heading' | 'paragraph' | 'quote' | 'code' | 'formula' | 'image'; original: string; translated?: string; language?: string; }
export interface CommentNode { id: string; author: string; roleBadge?: string; timeAgo: string; score: number; original: string; translated?: string; children?: CommentNode[]; }

export interface TextArticle {
  id: string;
  channelId: Exclude<TextChannelId, 'all'>;
  sourceName: string;
  title: string;
  titleZh: string;
  author: string;
  publishedAt: string;
  readTime: string;
  curationMode?: CurationMode;
  businessDate?: string;
  previouslyRecommendedOn?: string;
  doi?: string;
  journalName?: string;
  impactScore?: string;
  tags: string[];
  summaryZh: string;
  blocks: ArticleBlock[];
  comments: CommentNode[];
  commentsError?: string | null;
}

export interface BookSpread {
  page: number;
  chapterTitle: string;
  leftTitle: string;
  leftParagraphs: { id: string; text: string; keyTerms?: string[] }[];
  rightTitle: string;
  rightParagraphs: { id: string; text: string }[];
}

export interface BookItem {
  id: string;
  title: string;
  author: string;
  format: 'pdf' | 'epub' | 'english_daily';
  language: string;
  progressPct: number;
  totalPages: number;
  currentPage: number;
  coverGradient: string;
  badge: string;
  sourcePlugin: string;
  bilingualReady: boolean;
  spreads: BookSpread[];
}

export interface VocabCard {
  id: string;
  word: string;
  phonetic: string;
  pos: string;
  meaningZh: string;
  etymology: string;
  contextSentence: string;
  bookTitle: string;
  sourceDate: string;
  intervalDays: number;
  easeFactor: number;
  dueLabel: string;
  nasClip?: { showTitle: string; episode: string; timestampLabel: string; lineEn: string; lineZh: string };
}

export interface MagpieRouteState {
  gatewayUrl: string;
  gatewayHealthy: boolean;
  activeGroup: string;
  primaryModel: string;
  availableModels: string[];
  effort: 'low' | 'medium' | 'high' | 'xhigh';
  strategy: 'order' | 'smart' | 'usage' | 'pace';
  fallbackChain: string[];
  quotas: { provider: string; model: string; usedPct: number; resetIn: string }[];
}

export interface AiStructuredCard {
  id: string;
  domain: 'media' | 'text' | 'book';
  title: string;
  subtitle: string;
  badge: string;
  previouslyRecommendedOn?: string;
  verifiedCn?: boolean;
}

export interface AiChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  timestamp: string;
  mode?: CurationMode;
  servedModel?: string;
  content: string;
  cards?: AiStructuredCard[];
  isError?: boolean;
  isDemo?: boolean;
}

export interface ConfigFieldSchema {
  key: string;
  label: string;
  type: 'string' | 'secret' | 'bool' | 'int' | 'select';
  default: string | boolean | number;
  description?: string;
  required?: boolean;
  options?: string[];
}

export interface PluginItem {
  id: string;
  name: string;
  version: string;
  kind: 'provider' | 'workspace' | 'hybrid' | 'extension';
  domain: DomainKind;
  author: string;
  description: string;
  installed: boolean;
  enabled: boolean;
  healthOk: boolean;
  hotReloadMs: number;
  verifyStatus: 'passed' | 'idle' | 'failed';
  runtimeState?: string;
  runtimeError?: string;
  reloadCount?: number;
  provides: string[];
  consumes: string[];
  permissions: string[];
  config_schema: ConfigFieldSchema[];
  initialConfig: Record<string, string | boolean | number>;
}

export interface BookCatalogItem {
  id: string;
  title: string;
  author: string;
  format: 'pdf' | 'epub';
  language: string;
  sourceName: string;
  sizeLabel: string;
  coverGradient: string;
  summary: string;
  itemId?: string;
  providerPluginId?: string;
  detailUrl?: string;
  downloadToken?: string;
  downloadable?: boolean;
}

export interface AiPresetItem {
  id: string;
  label: string;
  mode: CurationMode;
  prompt: string;
  targetNote: string;
}

export interface ArchiveSnapshot {
  obsidianRepo: string;
  musicRepo: string;
  sinceDate: string;
  lastSyncAt: string;
  branchStatus: 'clean' | 'dirty_local_notes';
  counts: {
    musicDaily: number;
    youtubeDaily: number;
    journalDaily: number;
    newsAndReddit: number;
    englishVocab: number;
  };
}

export const MEDIA_ITEMS: MediaItem[] = [
  {
    id: 'media-yt-attention', sourceId: 'plugin-youtube', kind: 'video',
    title: 'Mechanistic Interpretability of Sparse Attention Heads',
    subtitle: 'Andrej Karpathy · Why watch：从归纳头到稀疏路由的清晰几何直观',
    creator: 'Andrej Karpathy', durationSec: 1945, durationLabel: '32:25', aspect: '16:9',
    badge: '每日精选 · 2026-11-16', curationMode: 'daily_immutable', businessDate: '2026-11-16',
    verificationLabel: 'youtube_watch_verified · SponsorBlock 3 段',
    thumbGradient: 'linear-gradient(135deg, #2B3A4A 0%, #52414E 58%, #9A3B58 100%)', thumbTag: 'YouTube 4K · 双语预译',
    playUrl: 'https://www.youtube.com/watch?v=kCc8FmEb1nY',
    summary: '拆解多层 Transformer 注意力头在长上下文下的稀疏坍缩现象，附带 SponsorBlock 空降切片与 FunASR 离线字幕检查点。',
    chapters: [
      { id: 'ch-1', startSec: 0, endSec: 210, label: '00:00 引言与符号约定', kind: 'content' },
      { id: 'ch-2', startSec: 210, endSec: 295, label: '03:30 赞助商片段 (SponsorBlock)', kind: 'sponsor' },
      { id: 'ch-3', startSec: 295, endSec: 1180, label: '04:55 Induction Heads 几何推导', kind: 'content' },
      { id: 'ch-4', startSec: 1180, endSec: 1945, label: '19:40 稀疏 KV 缓存实验验证', kind: 'content' },
    ],
  },
  {
    id: 'media-bili-wayland', sourceId: 'plugin-bilibili', kind: 'video',
    title: 'Wayland + libmpv OpenGL 硬解管线零拷贝实践',
    subtitle: 'Linux 图形工作站 · 4K60 HDR · 无边框桌面合成',
    creator: '极客图形社', durationSec: 1120, durationLabel: '18:40', aspect: '16:9',
    badge: 'Bilibili 4K · 硬解', verificationLabel: 'SponsorBlock · bsbsb.top 2 段',
    thumbGradient: 'linear-gradient(135deg, #3A2C38 0%, #7C304E 55%, #BA345A 100%)', thumbTag: 'Bilibili · 60fps',
    playUrl: 'https://www.bilibili.com/video/BV1xK4y1Z7aa',
    summary: '分析 Tauri v2 Wayland 子表面与 libmpv render API 的帧同步时序，消除跨屏拖拽 tearing 与输入法闪烁。',
    chapters: [
      { id: 'ch-b1', startSec: 0, endSec: 480, label: '00:00 DMABUF 零拷贝握手', kind: 'content' },
      { id: 'ch-b2', startSec: 480, endSec: 540, label: '08:00 恰饭推广 (SponsorBlock)', kind: 'ad' },
      { id: 'ch-b3', startSec: 540, endSec: 1120, label: '09:00 Wayland 帧回调压测', kind: 'content' },
    ],
  },
  {
    id: 'media-yt-jazz-live', sourceId: 'plugin-ai-curator', kind: 'video',
    title: 'Brad Mehldau Trio — Live at Village Vanguard (Full Set)',
    subtitle: '深夜器乐现场 · 经典重温推荐 · 钢琴三重奏动态分析',
    creator: 'Jazz Archive', durationSec: 2680, durationLabel: '44:40', aspect: '16:9',
    badge: 'AI 灵感推荐 · 经典重温', curationMode: 'interactive_ephemeral', previouslyRecommendedOn: '2026-10-12',
    verificationLabel: 'creator_official_page',
    thumbGradient: 'linear-gradient(135deg, #27252B 0%, #4E4237 55%, #AC5F0C 100%)', thumbTag: 'Live · 器乐现场',
    playUrl: 'https://www.youtube.com/watch?v=9x5c3pP0M2k',
    summary: '极高密度的复调即兴与低音贝斯对话，适合夜间专注阅读或编码时作为背景声场，27:15 节奏错位切换极具张力。',
  },
  {
    id: 'media-music-daily-1', sourceId: 'plugin-music', kind: 'music',
    title: 'In a Silent Way / It’s About That Time',
    subtitle: 'Miles Davis · In a Silent Way (1969)',
    creator: 'Miles Davis', durationSec: 1192, durationLabel: '19:52', aspect: '1:1',
    badge: '每日 5 首 · 2026-11-16', curationMode: 'daily_immutable', businessDate: '2026-11-16',
    verificationLabel: '国区 Apple Music verified · ALAC 无损',
    thumbGradient: 'linear-gradient(135deg, #2C3531 0%, #4B5B53 50%, #9E7B4F 100%)', thumbTag: 'Daily 5 · #1',
    playUrl: 'https://music.apple.com/cn/song/1463501518',
    summary: 'Teo Macero 将多段录音室即兴剪辑重组为近二十分钟的催眠式融合爵士长卷，电钢琴与电风琴像雾气般悬浮在极简军鼓律动之上。',
    listeningCue: '第一遍听电风琴与电吉他在声场左右两侧的呼吸留白；第二遍追踪 08:40 处从沉静过渡到行走贝斯的剪辑接缝。',
    lyrics: [
      { timeSec: 0, original: '[Instrumental Prelude — Fender Rhodes & Organ Drone]', translation: '【器乐前奏 — 电钢琴与管风琴持续和声铺底】', note: 'Zawinul 与 Corea 双电钢琴织体' },
      { timeSec: 148, original: 'Soft trumpet motif enters over suspended modal chord', translation: '弱音器小号主题在调式和弦上缓慢浮现', note: '调式爵士留白张力' },
      { timeSec: 365, original: 'Acoustic guitar arpeggio anchors the sparse pulse', translation: '原声吉他分解和弦锚定稀疏脉冲' },
      { timeSec: 520, original: '[Transition — It’s About That Time Walking Bass]', translation: '【段落切换 — 行走贝斯驱动的律动加速】', note: 'Dave Holland 贝斯线进入' },
      { timeSec: 790, original: 'Closing theme mirrors the opening silence', translation: '尾声主旋律镜像呼应开篇的静谧空间' },
    ],
  },
  {
    id: 'media-music-daily-2', sourceId: 'plugin-music', kind: 'music',
    title: 'Nardis (Live at The Village Vanguard)',
    subtitle: 'Bill Evans Trio · Waltz for Debby Sessions',
    creator: 'Bill Evans', durationSec: 356, durationLabel: '05:56', aspect: '1:1',
    badge: 'AI 灵感推荐 · 经典重温', curationMode: 'interactive_ephemeral', previouslyRecommendedOn: '2026-09-28',
    verificationLabel: '国区 verified · 灵感电台',
    thumbGradient: 'linear-gradient(135deg, #242B35 0%, #3F4E66 60%, #7C8FA8 100%)', thumbTag: '灵感电台 · 重温',
    playUrl: 'https://music.apple.com/cn/song/1440812492',
    summary: 'Scott LaFaro 的对位贝斯与 Bill Evans 的内声部和弦形成近乎室内乐的等权对话，零侵略感却保持清晰结构感。',
    listeningCue: '重点留意 01:45 之后钢琴左手与低音提琴的交替领奏，几乎听不到传统主伴奏层级。',
    lyrics: [
      { timeSec: 0, original: 'Modal Phrygian-dominant opening figure', translation: '弗里吉亚属调式开篇动机' },
      { timeSec: 105, original: 'Contrapuntal bass dialogue enters mid-phrase', translation: '对位低音提琴在乐句中段切入呼应', note: 'Scott LaFaro 经典交互' },
      { timeSec: 230, original: 'Brushed snare swells beneath block-chord climax', translation: '鼓刷滚奏托起柱式和弦高潮段落' },
    ],
  },
  {
    id: 'media-nas-severance', sourceId: 'plugin-fnos-nas', kind: 'nas',
    title: 'Severance · S02E03 “Who Is Alive?”',
    subtitle: '飞牛 fnOS 原盘库 · 4K DoVi · 中英双语 ASS 已同步',
    creator: 'Apple TV+ / fnOS NAS', durationSec: 3140, durationLabel: '52:20', aspect: '16:9',
    badge: '飞牛原盘 · 4K HDR', verificationLabel: 'media.clip_jump.v1 · 含 482 条台词索引',
    thumbGradient: 'linear-gradient(135deg, #1E282E 0%, #344954 55%, #1E7244 100%)', thumbTag: 'fnOS NAS · MKV',
    playUrl: 'file:///mnt/feiniu-smb/Series/Severance/S02E03.mkv',
    summary: '已通过 L1 本地挂载与 L2 伴随台词索引关联；在 BookWorkbench 点选 ephemeral 等词汇可直接跳播本集原声对白。',
    nasMeta: {
      codec: 'HEVC 10-bit · Dolby Vision', resolution: '3840×2160 · 42.8 Mbps',
      path: '/mnt/feiniu-smb/Series/Severance/S02E03.mkv', subtitleStatus: '双语 ASS 已原子写回 (受保护)',
      imdbRating: 'IMDb 9.1', clipQuote: '14:02 · "The arrangement feels deliberately ephemeral, like chalk on wet slate."',
    },
  },
  {
    id: 'media-nas-oppenheimer', sourceId: 'plugin-fnos-nas', kind: 'nas',
    title: 'Oppenheimer (2023) · IMAX Remux',
    subtitle: '飞牛 fnOS 电影库 · DTS-HD MA 5.1 · 待匹配双语字幕',
    creator: 'Christopher Nolan', durationSec: 10800, durationLabel: '180:00', aspect: '16:9',
    badge: '飞牛原盘 · REMUX', verificationLabel: 'Radarr 已入库 · 需 SubHunter 对齐',
    thumbGradient: 'linear-gradient(135deg, #2D231C 0%, #6A3C1E 55%, #AC5F0C 100%)', thumbTag: 'fnOS · 78.4 GB',
    playUrl: 'file:///mnt/feiniu-smb/Movies/Oppenheimer.2023.mkv',
    summary: '支持调用 plugin-sub-hunter 检索 ASSRT/SubHD 字幕并经 ffsubsync 对齐音频时间轴后原子写回。',
    nasMeta: {
      codec: 'HEVC 10-bit · HDR10', resolution: '3840×2160 · 61.4 Mbps',
      path: '/mnt/feiniu-smb/Movies/Oppenheimer.2023.mkv', subtitleStatus: '仅内置英文字幕 · 可一键运行 SubHunter',
      imdbRating: 'IMDb 8.3',
    },
  },
  {
    id: 'media-music-daily-3', sourceId: 'plugin-music', kind: 'music',
    title: 'Blue in Green (Take 3)',
    subtitle: 'Miles Davis & Bill Evans · Kind of Blue (1959)',
    creator: 'Miles Davis', durationSec: 327, durationLabel: '05:27', aspect: '1:1',
    badge: '每日 5 首 · 2026-11-16', curationMode: 'daily_immutable', businessDate: '2026-11-16',
    verificationLabel: '国区 Apple Music verified · 24-bit/96kHz',
    thumbGradient: 'linear-gradient(135deg, #1E2A38 0%, #344D6B 52%, #6C8EB2 100%)', thumbTag: 'Daily 5 · #2',
    playUrl: 'https://music.apple.com/cn/song/157322651',
    summary: '十小节循环曲式让弱音器小号与三连音钢琴泛音缓缓下沉，是夜读和整理学术笔记时极克制的呼吸感配乐。',
    listeningCue: '第一遍听 Bill Evans 开篇四小节和弦的踏板余韵；第二遍留意 Paul Chambers 弓弦拉奏与鼓刷的极弱动态切换。',
    lyrics: [
      { timeSec: 0, original: 'Rubato piano chordal introduction (Bill Evans)', translation: '自由速度钢琴柱式和弦引子（Bill Evans）', note: 'Kind of Blue 经典冥想序奏' },
      { timeSec: 46, original: 'Harmon-muted trumpet states the 10-bar descending motif', translation: '哈蒙弱音器小号陈述十小节下行主题' },
      { timeSec: 174, original: 'Double-time brushed pulse beneath tenor saxophone solo', translation: '次中音萨克斯独奏下方的倍速鼓刷轻抚律动' },
    ],
  },
  {
    id: 'media-yt-rust-wayland', sourceId: 'plugin-youtube', kind: 'video',
    title: 'Designing Deterministic Spatial Keyboard Focus in Desktop Webviews',
    subtitle: 'Systems UI Conf · 2D 几何焦点导航与 Fcitx5 输入法避让工程实践',
    creator: 'Paper & Graphite Lab', durationSec: 1580, durationLabel: '26:20', aspect: '16:9',
    badge: '每日精选 · 2026-11-16', curationMode: 'daily_immutable', businessDate: '2026-11-16',
    verificationLabel: 'creator_official_page · 双语字幕就绪',
    thumbGradient: 'linear-gradient(135deg, #25303C 0%, #3F5266 55%, #2260AA 100%)', thumbTag: 'YouTube · 1080p60',
    playUrl: 'https://www.youtube.com/watch?v=omni2dFocus01',
    summary: '对比传统 Tab 顺序焦点的跨栏迷失问题，推导基于包围盒重叠惩罚与业务 ID 锚定的 2D 空间跳转算法。',
    chapters: [
      { id: 'ch-rw1', startSec: 0, endSec: 320, label: '00:00 为什么键盘桌面不能只靠 Tab', kind: 'content' },
      { id: 'ch-rw2', startSec: 320, endSec: 410, label: '05:20 赞助片段 (SponsorBlock)', kind: 'sponsor' },
      { id: 'ch-rw3', startSec: 410, endSec: 1580, label: '06:50 Bounding-Box 方向代价函数', kind: 'content' },
    ],
  },
  {
    id: 'media-bili-babeldoc', sourceId: 'plugin-bilibili', kind: 'video',
    title: 'BabelDOC 学术 PDF 版面解析：公式隔离与双栏基线重排',
    subtitle: '开源文档工程 · LaTeX 数学块保护 · 术语锁定词典',
    creator: '文档排版工坊', durationSec: 940, durationLabel: '15:40', aspect: '16:9',
    badge: 'AI 灵感推荐 · 经典重温', curationMode: 'interactive_ephemeral', previouslyRecommendedOn: '2026-10-29',
    verificationLabel: 'bsbsb.top 1 段 · 已挂载词汇索引',
    thumbGradient: 'linear-gradient(135deg, #1E332B 0%, #2B5342 55%, #1E7244 100%)', thumbTag: 'Bilibili · 知识区',
    playUrl: 'https://www.bilibili.com/video/BV1bB4y1N7kp',
    summary: '详解如何通过 PDF 对象流坐标聚类区分双栏正文、脚注、矢量公式与表格，避免译文溢出破坏原文页码基线。',
  },
];

export const TEXT_CHANNELS: { id: TextChannelId; label: string; count: number }[] = [
  { id: 'all', label: '全部来源', count: 8 },
  { id: 'journal', label: '科学期刊', count: 2 },
  { id: 'news', label: '每日早报', count: 2 },
  { id: 'reddit', label: 'Reddit 精选', count: 2 },
  { id: 'linuxdo', label: 'Linux.do', count: 1 },
  { id: 'zhihu', label: '知乎 / 博客', count: 1 },
];

export const TEXT_ARTICLES: TextArticle[] = [
  {
    id: 'text-journal-kv-compression', channelId: 'journal', sourceName: 'Nature Machine Intelligence · Journal Daily',
    title: 'Entropy-Guided Key-Value Cache Eviction for Long-Horizon Reasoning',
    titleZh: '面向长程推理的熵引导 KV 缓存淘汰边界研究',
    author: 'L. Chen, M. Alvarez, K. Sato', publishedAt: '2026-11-16', readTime: '9 min',
    curationMode: 'daily_immutable', businessDate: '2026-11-16', doi: '10.1038/s42256-026-00941-2',
    journalName: 'Nature Machine Intelligence', impactScore: '推荐优先级 #1 · 审稿级拆解',
    tags: ['KV Cache', 'Long Context', 'Information Theory'],
    summaryZh: '揭示均匀窗口淘汰在多跳推理中破坏稀疏桥接 token 的原因，提出基于条件注意力熵的保留下界。',
    blocks: [
      { id: 'blk-j1', kind: 'heading', original: '1. Problem & Empirical Observation', translated: '一、问题与观测：固定滑动窗口为何在 64k 上下文后骤降' },
      { id: 'blk-j2', kind: 'paragraph', original: 'Standard sliding-window KV eviction preserves sink tokens and recent context, yet silently drops low-norm bridge tokens that encode variable bindings across reasoning hops.', translated: '常规滑动窗口策略虽保留了起始 sink token 与近期窗口，却会静默丢弃范数较低但承载跨跳变量绑定的“桥接词元”。' },
      { id: 'blk-j3', kind: 'formula', original: 'L_{\\text{evict}}(\\theta) = \\mathbb{E}_{x \\sim p}[\\log q_\\theta(x)] - \\beta \\cdot D_{\\mathrm{KL}}(H(\\alpha_t) \\parallel \\pi_{\\text{ref}})', translated: '条件注意力熵约束下的 KL 正则化保留目标：当注意力分布熵 $H(\\alpha_t)$ 低于阈值时优先保留对应键向量。' },
      { id: 'blk-j4', kind: 'quote', original: 'Reviewer Synthesis: Strong theoretical framing on synthetic needle tasks; real multi-file coding traces remain sensitive to head-wise temperature calibration.', translated: '审稿级证据边界：合成多跳基准提升显著，但真实长程代码库轨迹仍依赖分头温度校准，外推至 MoE 模型尚缺直接消融。' },
      {
        id: 'blk-j5',
        kind: 'paragraph',
        original: 'Key takeaways across 64k–128k evaluation sweeps ([Nature MI Appendix](https://www.nature.com/natmachintell)):\n\n- Preserves low-entropy bridge tokens at $\\beta = 0.25$ with `< 1.8%` latency delta.\n\n| Method | 64k Needle Acc | KV Budget |\n| --- | --- | --- |\n| Sliding Window | 74.2% | 20% |\n| Entropy-Guided | **96.8%** | 20% |',
        translated: '64k–128k 消融扫描核心结论（见 [Nature MI 附录](https://www.nature.com/natmachintell)）：\n\n- 在 $\\beta = 0.25$ 下保留低熵桥接词元，延迟增幅低于 `1.8%`。\n\n| 淘汰策略 | 64k 检索准确率 | KV 预算 |\n| --- | --- | --- |\n| 固定滑动窗口 | 74.2% | 20% |\n| 熵引导保留 | **96.8%** | 20% |',
      },
    ],
    comments: [
      {
        id: 'cmt-j1', author: 'dr_haowang', roleBadge: 'ML Systems', timeAgo: '2h ago', score: 48,
        original: 'Measuring per-head attention entropy adds almost zero memory overhead if computed right inside the FlashAttention epilogue.',
        translated: '如果在 FlashAttention epilogue 阶段顺带归约每个头的注意力熵，几乎不引入额外显存开销。',
        children: [{
          id: 'cmt-j1-1', author: 'k_sato_author', roleBadge: 'Author', timeAgo: '1h ago', score: 31,
          original: 'Confirmed — we fused the running log-sum-exp entropy into our Triton kernel with `< 1.8%` latency delta.',
          translated: '确实如此——我们将 running log-sum-exp 熵计算融合进 Triton kernel，端到端延迟增幅低于 `1.8%`。',
          children: [{
            id: 'cmt-j1-1-1', author: 'triton_kernel_dev', roleBadge: 'Perf', timeAgo: '38m ago', score: 17,
            original: 'Wait — did you keep the entropy accumulator in FP32 to avoid underflow on sharp heads?',
            translated: '请教一下——熵累加器是否保持在 FP32 以免尖锐注意力头下溢？',
          }],
        }],
      },
    ],
  },
  {
    id: 'text-news-daily', channelId: 'news', sourceName: '每日早报 · Executive View',
    title: 'Linux 6.18 Lands Sub-Millisecond PipeWire & Wayland Explicit Sync Improvements',
    titleZh: 'Linux 6.18 合入亚毫秒级 PipeWire 与 Wayland 显式同步补丁（影响 4/5）',
    author: 'OmniDesk Daily News', publishedAt: '2026-11-16', readTime: '4 min',
    curationMode: 'daily_immutable', businessDate: '2026-11-16', impactScore: '影响等级 4/5',
    tags: ['Wayland', 'Kernel', 'Desktop'],
    summaryZh: '显式同步时间线信号量落地主线内核与合成器协议，直接降低混频播放与 GPU 硬解框架下的微卡顿。',
    blocks: [
      { id: 'blk-n1', kind: 'heading', original: 'Executive View & Why It Matters', translated: '核心事实与长期影响' },
      { id: 'blk-n2', kind: 'paragraph', original: 'The explicit drm_syncobj timeline handoff removes implicit buffer stalls between hardware video decoders and Wayland compositors on hybrid laptops.', translated: '基于 drm_syncobj 的显式时间线交接消除了硬件解码器与 Wayland 合成器之间的隐式缓冲阻塞，直接改善多工作台硬解切换流畅度。' },
    ],
    comments: [{ id: 'cmt-n1', author: 'hypr_maintainer', roleBadge: 'Compositor', timeAgo: '3h ago', score: 64, original: 'Paired with libmpv gpu-next, frame pacing jitter drops below 0.3ms at 120Hz.', translated: '搭配 libmpv gpu-next 后，120Hz 下帧调度抖动已压低至 0.3ms 以内。' }],
  },
  {
    id: 'text-linuxdo-uv', channelId: 'linuxdo', sourceName: 'Linux.do · 开发调优',
    title: 'Zero-ABI Python Plugin Hot Reloading via Isolated Importlib Namespaces',
    titleZh: '基于 importlib 隔离命名空间的纯 Python 插件秒级热重载实践',
    author: 'arch_zhu', publishedAt: '2026-11-15', readTime: '6 min', tags: ['Python 3.14', 'uv', 'Plugin Architecture'],
    summaryZh: '用 180 行 Pydantic 契约替代 22 个 CMake 动态库，实现保存即重载且不丢失核心状态。',
    blocks: [
      { id: 'blk-l1', kind: 'paragraph', original: 'By scoping every capability, event subscription, and background asyncio task to a PluginContext lease, unloading a faulty plugin leaves zero dangling callbacks.', translated: '将所有能力注册、事件订阅和后台 asyncio 任务统一绑定到 PluginContext 租约，热卸载故障插件时零残留回调。' },
      { id: 'blk-l2', kind: 'code', language: 'python', original: 'async def on_load(self, ctx: PluginContext) -> None:\n    ctx.register_capability("llm.router.v1", self.handle_route)\n    ctx.subscribe("entity.ai.route_changed", self.on_route)', translated: '所有声明式注册统一经由 ctx 托管，重载时内核按 plugin_id 原子回收。' },
    ],
    comments: [{
      id: 'cmt-l1', author: 'py_craft', timeAgo: '5h ago', score: 29,
      original: 'Keeping subprocess tools in start_new_session=True also prevents zombie ffmpeg trees on reload.',
      translated: '外部进程统一用 start_new_session=True 拉起，热重载时就不会留下僵尸 ffmpeg 子进程树。',
      children: [{ id: 'cmt-l1-1', author: 'arch_zhu', roleBadge: 'OP', timeAgo: '4h ago', score: 14, original: 'Exactly, SIGTERM -> SIGKILL cascades cleanly across the entire process group.', translated: '没错，SIGTERM 到 SIGKILL 沿整个进程组级联回收最稳妥。' }],
    }],
  },
  {
    id: 'text-reddit-localllama', channelId: 'reddit', sourceName: 'r/LocalLLaMA · Reddit Daily',
    title: 'Benchmarking Local Loopback Model Routers with Quota-Aware Fallbacks',
    titleZh: '本地回环多模型网关的配额感知路由与自动回退实测',
    author: 'u/token_minimalist', publishedAt: '2026-11-16', readTime: '5 min',
    curationMode: 'daily_immutable', businessDate: '2026-11-16', tags: ['Magpie', 'Router', 'Local Gateway'],
    summaryZh: '实测表明专属路由组隔离终端会话与桌面应用配置，可避免多端争抢与额度突耗。',
    blocks: [{ id: 'blk-r1', kind: 'paragraph', original: 'Pinning desktop apps to a dedicated routing group like group/omnidesk-active keeps CLI coding harnesses untouched while preserving fallback resilience.', translated: '将桌面端绑定到独立路由组 group/omnidesk-active，可在保持故障回退韧性的同时完全不扰动终端编码环境。' }],
    comments: [{ id: 'cmt-r1', author: 'u/eval_first', timeAgo: '6h ago', score: 37, original: 'Readback verification after group updates is underrated—catches stale cache immediately.', translated: '修改路由组后做一次读回校验非常关键，能立刻抓出陈旧缓存。' }],
  },
  {
    id: 'text-zhihu-typography', channelId: 'zhihu', sourceName: '知乎专栏 · 界面工程',
    title: 'Paper & Graphite: Designing High-Density Keyboard-First Desktop Typography',
    titleZh: '纸感亮色与石墨深色：高信息密度桌面端的键盘空间焦点设计',
    author: '陆离 · 桌面交互笔记', publishedAt: '2026-11-14', readTime: '7 min',
    previouslyRecommendedOn: '2026-11-02', tags: ['Paper Theme', 'Spatial Focus', 'Typography'],
    summaryZh: '探讨暖纸色 #F6F5F3 与 1px 细线边框如何在多栏工作台中降低长时阅读的视觉疲劳。',
    blocks: [{ id: 'blk-z1', kind: 'paragraph', original: 'Unlike stark #FFFFFF canvases, warm paper #F6F5F3 paired with 1px structural hairlines separates multi-column panes without heavy drop shadows.', translated: '相比刺眼的纯白画布，暖纸色 #F6F5F3 搭配 1px 细线可在无需厚重阴影的前提下清晰区隔多栏信息密度。' }],
    comments: [],
  },
  {
    id: 'text-journal-world-model', channelId: 'journal', sourceName: 'PNAS · Journal Daily',
    title: 'Predictive Coding Hierarchies Stabilize Episodic Memory Rehydration',
    titleZh: '预测编码层级如何稳定情景记忆的表征回灌',
    author: 'E. Takahashi, R. Lindqvist', publishedAt: '2026-11-16', readTime: '11 min',
    curationMode: 'daily_immutable', businessDate: '2026-11-16', doi: '10.1073/pnas.2604182123',
    journalName: 'PNAS', impactScore: '推荐优先级 #2 · 神经计算',
    tags: ['Predictive Coding', 'Memory Rehydration', 'Hippocampus'],
    summaryZh: '证明皮层-海马闭环中的预测误差门控能在检索阶段防止新旧情景记忆相互覆盖。',
    blocks: [
      { id: 'blk-jw1', kind: 'heading', original: '1. Author Mechanism & Error Gating', translated: '一、作者解释：预测误差门控与双通道记忆索引' },
      { id: 'blk-jw2', kind: 'paragraph', original: 'When replay operates over an immutable episodic ledger while allowing ephemeral contextual recombination, catastrophic interference drops by 63%.', translated: '当重放机制建立在不可变的情景账本之上、同时允许瞬时上下文重组时，灾难性记忆干扰可下降 63%。' },
      { id: 'blk-jw3', kind: 'quote', original: 'Verification Boundary: Human intracranial EEG supports theta-phase separation; causal perturbation across multi-day intervals remains unverified.', translated: '核验边界：人类颅内脑电支持 θ 相位分离假说，但跨多日尺度的因果微刺激实验尚待补全。' },
    ],
    comments: [
      {
        id: 'cmt-jw1', author: 'neuro_comp', roleBadge: 'Researcher', timeAgo: '4h ago', score: 22,
        original: 'The separation between immutable consolidation and ephemeral working retrieval mirrors software event sourcing.',
        translated: '不可变巩固账本与瞬时检索工作区的分离，和软件里的事件溯源架构简直如出一辙。',
      },
    ],
  },
  {
    id: 'text-news-uv-07', channelId: 'news', sourceName: '每日早报 · Executive View',
    title: 'Python 3.14 Free-Threaded Subinterpreters Gain Native uv Workspace Locking',
    titleZh: 'Python 3.14 无锁子解释器获得 uv 原生工作区锁文件直连支持（影响 3/5）',
    author: 'OmniDesk Daily News', publishedAt: '2026-11-16', readTime: '3 min',
    curationMode: 'daily_immutable', businessDate: '2026-11-16', impactScore: '影响等级 3/5',
    tags: ['Python 3.14', 'uv', 'Concurrency'],
    summaryZh: '插件微内核可在同一进程内以隔离命名空间并行执行多源抓取与本地 OCR/ASR 调度。',
    blocks: [
      { id: 'blk-nu1', kind: 'paragraph', original: 'uv 0.8 resolves deterministic lockfiles directly into per-plugin virtual environments without spawning redundant build backends.', translated: 'uv 0.8 可将确定性锁文件直接解析到独立插件虚拟环境，大幅缩短纯 Python 插件冷启动与依赖巡检耗时。' },
    ],
    comments: [],
  },
  {
    id: 'text-reddit-wayland-hdr', channelId: 'reddit', sourceName: 'r/hyprland · Reddit Daily',
    title: 'PSA: Color-Managed Wayland Subsurfaces + libmpv gpu-next ICC Profiles',
    titleZh: 'r/hyprland 热议：Wayland 色彩管理协议与 libmpv gpu-next 实测笔记',
    author: 'u/wayland_tinker', publishedAt: '2026-11-16', readTime: '5 min',
    curationMode: 'daily_immutable', businessDate: '2026-11-16', tags: ['Hyprland', 'Wayland', 'mpv'],
    summaryZh: '总结 Hyprland 0.48 下嵌入 libmpv 窗口开启 wp_color_manager_v1 的正确参数组合。',
    blocks: [
      { id: 'blk-rh1', kind: 'code', language: 'ini', original: 'vo=gpu-next\nhwdec=auto-safe\ntarget-colorspace-hint=yes', translated: '在 Tauri 子表面嵌入 libmpv 时开启色彩空间提示即可保留 HDR 映射。' },
    ],
    comments: [
      {
        id: 'cmt-rh1', author: 'u/hdr_enjoyer', timeAgo: '5h ago', score: 19,
        original: 'Verified on Fedora 42 — remember to pair with Fcitx5 wayland_im_v2 so floating docks stay rock solid.',
        translated: '实测有效——记得配合 Fcitx5 wayland_im_v2，伴读浮窗输入中文时候选框也不会漂移。',
      },
    ],
  },
];

export const BOOK_ITEMS: BookItem[] = [
  {
    id: 'book-babeldoc-attention', title: 'Attention Is All You Need (BabelDOC 双栏对照版)',
    author: 'Vaswani et al. · 术语锁定已校对', format: 'pdf', language: 'EN ↔ ZH',
    progressPct: 68, totalPages: 15, currentPage: 3,
    coverGradient: 'linear-gradient(155deg, #1E3A2F 0%, #245742 58%, #1E7244 100%)',
    badge: 'BabelDOC 双栏', sourcePlugin: 'plugin-pdf-babeldoc', bilingualReady: true,
    spreads: [
      {
        page: 3, chapterTitle: 'Section 3.2 · Scaled Dot-Product & Multi-Head Attention',
        leftTitle: 'Original PDF Column',
        leftParagraphs: [
          { id: 'bp-1', text: 'An attention function maps a query and key-value pairs to an output, maintaining strict provenance across tokens.', keyTerms: ['provenance'] },
          { id: 'bp-2', text: 'With a single head, averaging makes intermediate bindings ephemeral rather than idempotent across layers.', keyTerms: ['ephemeral', 'idempotent'] },
        ],
        rightTitle: 'BabelDOC 译文基线对照',
        rightParagraphs: [
          { id: 'bp-1-zh', text: '注意力函数将查询与键值对映射到输出，并在词元之间保持严格的溯源（provenance）关系。' },
          { id: 'bp-2-zh', text: '若仅使用单一注意力头，平均效应会令中间绑定变得转瞬即逝（ephemeral），而非跨层幂等（idempotent）。' },
        ],
      },
      {
        page: 4, chapterTitle: 'Section 3.3 · Position-wise Feed-Forward Networks',
        leftTitle: 'Original PDF Column',
        leftParagraphs: [{ id: 'bp-3', text: 'Each layer applies an idempotent feed-forward projection separately to every position after archive rehydration.', keyTerms: ['idempotent', 'rehydration'] }],
        rightTitle: 'BabelDOC 译文基线对照',
        rightParagraphs: [{ id: 'bp-3-zh', text: '每一层在完成状态回灌（rehydration）后，都会分别对每个位置施加幂等（idempotent）前馈映射。' }],
      },
    ],
  },
  {
    id: 'book-ddia-epub', title: 'Designing Data-Intensive Applications',
    author: 'Martin Kleppmann · EPUB 划词伴读', format: 'epub', language: 'EN ↔ ZH',
    progressPct: 42, totalPages: 560, currentPage: 214,
    coverGradient: 'linear-gradient(155deg, #273244 0%, #374F6F 58%, #2260AA 100%)',
    badge: 'EPUB 伴读', sourcePlugin: 'plugin-ebook', bilingualReady: true,
    spreads: [
      {
        page: 214, chapterTitle: 'Chapter 7 · Transactions & Idempotency',
        leftTitle: 'EPUB Original Text',
        leftParagraphs: [{ id: 'ddia-1', text: 'An idempotent operation can run repeatedly without altering provenance during archive rehydration.', keyTerms: ['idempotent', 'provenance', 'rehydration'] }],
        rightTitle: '伴读精讲对照',
        rightParagraphs: [{ id: 'ddia-1-zh', text: '幂等（idempotent）操作即使重复执行，也不会在归档回灌（rehydration）期间篡改数据溯源（provenance）。' }],
      },
      {
        page: 215, chapterTitle: 'Chapter 7 · Linearizability & Ephemeral Leases',
        leftTitle: 'EPUB Original Text',
        leftParagraphs: [{ id: 'ddia-2', text: 'Ephemeral session state stays decoupled from durable write-ahead ledgers via explicit fencing tokens.', keyTerms: ['ephemeral', 'epistemic'] }],
        rightTitle: '伴读精讲对照',
        rightParagraphs: [{ id: 'ddia-2-zh', text: '瞬时（ephemeral）会话状态通过显式栅栏令牌与持久化预写账本保持解耦。' }],
      },
    ],
  },
  {
    id: 'book-remains-day', title: 'The Remains of the Day',
    author: 'Kazuo Ishiguro · 原版文学精读', format: 'epub', language: 'EN ↔ ZH',
    progressPct: 81, totalPages: 258, currentPage: 209,
    coverGradient: 'linear-gradient(155deg, #3A2C26 0%, #6A4837 58%, #9A6E4E 100%)',
    badge: '文学精读', sourcePlugin: 'plugin-ebook', bilingualReady: true,
    spreads: [
      {
        page: 209, chapterTitle: 'Day Six — Evening · Weymouth',
        leftTitle: 'Original Prose',
        leftParagraphs: [{ id: 'rem-1', text: 'The evening light felt almost ephemeral along the pier, settling quietly into the sea mist.', keyTerms: ['ephemeral'] }],
        rightTitle: '译文赏析',
        rightParagraphs: [{ id: 'rem-1-zh', text: '码头上的暮光显得近乎转瞬即逝（ephemeral），悄然沉入海雾之中。' }],
      },
      {
        page: 210, chapterTitle: 'Day Six — Dusk Reflections',
        leftTitle: 'Original Prose',
        leftParagraphs: [{ id: 'rem-2', text: 'Memory preserves a quiet provenance of unspoken loyalty long after the grand house falls silent.', keyTerms: ['provenance'] }],
        rightTitle: '译文赏析',
        rightParagraphs: [{ id: 'rem-2-zh', text: '记忆在深宅归于沉寂许久之后，依然保留着未曾言明之忠诚的静默出处（provenance）。' }],
      },
    ],
  },
  {
    id: 'book-english-daily', title: '英语每日精读与翻译归档 · 2026-11-16',
    author: 'obsidian-vault / 50-courses/English', format: 'english_daily', language: 'Daily 3-Pack',
    progressPct: 100, totalPages: 3, currentPage: 1,
    coverGradient: 'linear-gradient(155deg, #2D2638 0%, #583457 58%, #BA345A 100%)',
    badge: '每日英语 · 归档', sourcePlugin: 'plugin-ai-curator', bilingualReady: true,
    spreads: [
      {
        page: 1, chapterTitle: 'Daily Reading & Translation Drill (2026-11-16)',
        leftTitle: 'Passage & Core Expressions',
        leftParagraphs: [{ id: 'eng-1', text: 'Strict provenance guarantees ensure ephemeral recommendations never contaminate daily rehydration.', keyTerms: ['provenance', 'ephemeral', 'rehydration'] }],
        rightTitle: '逐句精译与语法拆解',
        rightParagraphs: [{ id: 'eng-1-zh', text: '严格的溯源保障确保瞬时推荐在本地回灌期间绝不会污染不可变的每日账本。' }],
      },
      {
        page: 2, chapterTitle: 'Weekly TV Idioms · Severance S02E03',
        leftTitle: 'Dialogue & Epistemic Nuance',
        leftParagraphs: [{ id: 'eng-2', text: 'Telemetry from the floor sounded reassuring, yet left an epistemic gap between protocol and reality.', keyTerms: ['telemetry', 'epistemic'] }],
        rightTitle: '地道表达与美剧原声切片',
        rightParagraphs: [{ id: 'eng-2-zh', text: '楼层传来的遥测指标（telemetry）听起来令人安心，却在规程与现实之间留下了认知（epistemic）裂隙。' }],
      },
    ],
  },
];

export const INITIAL_VOCAB_CARDS: VocabCard[] = [
  {
    id: 'vocab-ephemeral', word: 'ephemeral', phonetic: '/ɪˈfem.ər.əl/', pos: 'adj.',
    meaningZh: '短暂的，瞬息的，非持久化的',
    etymology: 'epi- (on) + hemera (day) → 朝生暮死的；在系统中指“按需瞬时会话”',
    contextSentence: 'The arrangement feels deliberately ephemeral, like chalk on wet slate.',
    bookTitle: 'Attention Is All You Need / Severance', sourceDate: '2026-11-16',
    intervalDays: 1, easeFactor: 2.5, dueLabel: '今日待复习',
    nasClip: { showTitle: 'Severance', episode: 'S02E03', timestampLabel: '14:02', lineEn: 'The arrangement feels deliberately ephemeral, like chalk on wet slate.', lineZh: '这种安排刻意给人转瞬即逝之感，就像湿石板上的粉笔字。' },
  },
  {
    id: 'vocab-idempotent', word: 'idempotent', phonetic: '/ˌaɪ.dɪmˈpoʊ.tənt/', pos: 'adj.',
    meaningZh: '幂等的（多次执行结果与单次执行相同）',
    etymology: 'idem (same) + potens (powerful) → 同样效力',
    contextSentence: 'An idempotent operation can be executed multiple times without changing the result.',
    bookTitle: 'Designing Data-Intensive Applications', sourceDate: '2026-11-15',
    intervalDays: 4, easeFactor: 2.6, dueLabel: '今日待复习',
  },
  {
    id: 'vocab-provenance', word: 'provenance', phonetic: '/ˈprɒv.ən.əns/', pos: 'n.',
    meaningZh: '起源，出处，数据溯源记录',
    etymology: 'pro- (forth) + venire (to come) → 从何而来',
    contextSentence: 'Read-only provenance lookup surfaces prior daily feature dates.',
    bookTitle: '英语每日精读与翻译归档', sourceDate: '2026-11-14',
    intervalDays: 6, easeFactor: 2.5, dueLabel: '2 天后到期',
  },
  {
    id: 'vocab-rehydration', word: 'rehydration', phonetic: '/ˌriː.haɪˈdreɪ.ʃən/', pos: 'n.',
    meaningZh: '再水化；归档数据全量回灌恢复',
    etymology: 're- (again) + hydr- (water) → 让干涸的归档重新充盈',
    contextSentence: 'Archive rehydration parses historical markdown and JSON manifests locally.',
    bookTitle: 'Designing Data-Intensive Applications', sourceDate: '2026-11-13',
    intervalDays: 3, easeFactor: 2.4, dueLabel: '今日待复习',
  },
  {
    id: 'vocab-epistemic', word: 'epistemic', phonetic: '/ˌep.ɪˈstiː.mɪk/', pos: 'adj.',
    meaningZh: '认知的，认识论的，关于知识边界的',
    etymology: 'Gk. episteme (knowledge, understanding) → 审视“知道什么与不知什么”',
    contextSentence: 'Reviewer synthesis draws a strict epistemic boundary between observed gains and extrapolation.',
    bookTitle: '英语每日精读与翻译归档', sourceDate: '2026-11-16',
    intervalDays: 2, easeFactor: 2.5, dueLabel: '今日待复习',
  },
  {
    id: 'vocab-telemetry', word: 'telemetry', phonetic: '/təˈlem.ə.tri/', pos: 'n.',
    meaningZh: '遥测数据；运行状态远距指标采集',
    etymology: 'tele- (far) + -metry (measurement) → 远端实时度量',
    contextSentence: 'Telemetry from the floor sounded reassuring, yet left an epistemic gap.',
    bookTitle: 'Severance / English Weekly TV', sourceDate: '2026-11-16',
    intervalDays: 5, easeFactor: 2.5, dueLabel: '3 天后到期',
    nasClip: { showTitle: 'Severance', episode: 'S02E03', timestampLabel: '28:19', lineEn: 'Telemetry from the floor sounded reassuring.', lineZh: '楼层传来的遥测指标听起来让人安心。' },
  },
];

export const BOOK_SEARCH_CATALOG: BookCatalogItem[] = [
  {
    id: 'cat-sicp',
    title: 'Structure and Interpretation of Computer Programs (2nd Ed.)',
    author: 'Harold Abelson & Gerald Jay Sussman',
    format: 'pdf',
    language: 'EN · 可转 BabelDOC',
    sourceName: 'Anna’s Archive · book.search.v1',
    sizeLabel: '14.2 MB PDF',
    coverGradient: 'linear-gradient(155deg, #2B2435 0%, #4E3B63 58%, #7E599A 100%)',
    summary: '计算机科学经典元编程教材，含完整公式与 Scheme 代码块，适合 BabelDOC 术语锁定双栏翻译。',
  },
  {
    id: 'cat-crafting-interpreters',
    title: 'Crafting Interpreters',
    author: 'Robert Nystrom',
    format: 'epub',
    language: 'EN · EPUB 伴读',
    sourceName: 'Z-Library · book.search.v1',
    sizeLabel: '8.6 MB EPUB',
    coverGradient: 'linear-gradient(155deg, #2F261D 0%, #6A4929 58%, #AC5F0C 100%)',
    summary: '从手写扫描器、字节码虚拟机到垃圾回收器的工程导览，自带清晰插图与章节练习。',
  },
];

export const INITIAL_MAGPIE_ROUTE: MagpieRouteState = {
  gatewayUrl: 'http://127.0.0.1:3425', gatewayHealthy: true, activeGroup: 'group/omnidesk-active',
  primaryModel: 'antigravity/gemini-4-carbon',
  availableModels: ['antigravity/gemini-4-carbon', 'claude/claude-sonnet-4', 'deepseek/deepseek-r1', 'openai/gpt-5.2'],
  effort: 'high', strategy: 'order', fallbackChain: ['claude/claude-sonnet-4', 'deepseek/deepseek-r1'],
  quotas: [
    { provider: 'Antigravity', model: 'gemini-4-carbon', usedPct: 24, resetIn: '3h 40m' },
    { provider: 'Claude Plan', model: 'claude-sonnet-4', usedPct: 48, resetIn: '1h 15m' },
    { provider: 'DeepSeek API', model: 'deepseek-r1', usedPct: 12, resetIn: '按量余额 ¥46.8' },
  ],
};

export const INITIAL_AI_MESSAGES: AiChatMessage[] = [
  { id: 'ai-msg-1', role: 'user', timestamp: '22:40', mode: 'interactive_ephemeral', content: '来几首适合深夜写代码的器乐爵士，以前每日推过的也可以，帮我推到灵感电台。' },
  {
    id: 'ai-msg-2', role: 'assistant', timestamp: '22:40', mode: 'interactive_ephemeral',
    servedModel: 'group/omnidesk-active → gemini-4-carbon:high',
    content: '已按 interactive_ephemeral（prefer_familiar）检索本地回灌历史库并完成 Apple Music 国区曲库实时 preflight 校验。结果仅写入「OmniDesk AI 灵感电台」，绝不触碰 p.YJXV7dEIeb3GzZX。',
    cards: [
      { id: 'rec-track-1', domain: 'media', title: 'In a Silent Way / It’s About That Time — Miles Davis', subtitle: '电钢琴持续声场 · 零侵略专注感', badge: 'AI 灵感推荐 · 经典重温 2026-11-16', previouslyRecommendedOn: '2026-11-16', verifiedCn: true },
      { id: 'rec-track-2', domain: 'media', title: 'Nardis (Live) — Bill Evans Trio', subtitle: '室内乐式钢琴与低音提琴对位对话', badge: 'AI 灵感推荐 · 经典重温 2026-09-28', previouslyRecommendedOn: '2026-09-28', verifiedCn: true },
    ],
  },
];

export const AI_ARCHIVE_SNAPSHOT: ArchiveSnapshot = {
  obsidianRepo: 'ZJFILOVE/obsidian-vault',
  musicRepo: 'ZJFILOVE/music-daily',
  sinceDate: '2026-09-04',
  lastSyncAt: '2026-11-16 22:35',
  branchStatus: 'clean',
  counts: {
    musicDaily: 365,
    youtubeDaily: 340,
    journalDaily: 198,
    newsAndReddit: 720,
    englishVocab: 412,
  },
};

export const AI_QUICK_PRESETS: AiPresetItem[] = [
  {
    id: 'preset-jazz-familiar',
    label: '重温 · 深夜纯音乐 3 首',
    mode: 'interactive_ephemeral',
    prompt: '从往期每日 5 首历史库里重温 3 首适合深夜编码的冷静器乐爵士，写到灵感电台。',
    targetNote: '仅写 OmniDesk AI 灵感电台 · 只读回标',
  },
  {
    id: 'preset-paper-chain',
    label: '灵感 · 长程推理论文延展',
    mode: 'interactive_ephemeral',
    prompt: '结合今天精读的 KV Cache 淘汰边界论文，推荐 2 篇同主题实证工作和对应 YouTube 讲解。',
    targetNote: '允许重温 · 不触碰 journal.jsonl',
  },
  {
    id: 'preset-daily-music',
    label: 'Daily 5 · 今日音乐管线',
    mode: 'daily_immutable',
    prompt: '执行 Asia/Shanghai 2026-11-16 music 每日 5 首不可重复精选（国区 verified 后同步 p.YJXV7dEIeb3GzZX）。',
    targetNote: '受永久账本去重与两阶段事务保护',
  },
  {
    id: 'preset-daily-journal',
    label: 'Daily · 科学期刊审稿拆解',
    mode: 'daily_immutable',
    prompt: '运行今日 journal 每日精选（2-3 篇），生成中文审稿级证据边界拆解并追加 journal.jsonl。',
    targetNote: '严格校验 DOI 与未读中止守卫',
  },
];

export const INITIAL_PLUGINS: PluginItem[] = [
  {
    id: 'plugin-magpie-agent', name: 'Magpie AI Agent Router', version: '0.2.0', kind: 'provider', domain: 'core', author: 'OmniDesk',
    description: '读取本机 127.0.0.1:3425 Magpie 网关与配额，通过专属组 group/omnidesk-active 反向打穿路由，绝不污染终端 Harness。',
    installed: true, enabled: true, healthOk: true, hotReloadMs: 14, verifyStatus: 'passed',
    provides: ['llm.router.v1', 'ai.agent.v1', 'ai.curation.v1', 'llm.chat.v1', 'llm.translate.v1'], consumes: [],
    permissions: ['process:magpie', 'network:loopback', 'config:read', 'config:write', 'secret:vault'],
    config_schema: [
      { key: 'magpie_bin', label: 'Magpie CLI 路径', type: 'string', default: 'magpie', description: 'PATH 命令名或绝对路径' },
      { key: 'routing_strategy', label: '路由调度策略', type: 'select', default: 'order', options: ['manual', 'order', 'smart', 'usage', 'pace'], description: 'group/omnidesk-active 组策略' },
      { key: 'gateway_key', label: 'Magpie Gateway Key', type: 'secret', default: '', description: '仅存入 0600 SecretVault，不写入明文配置' },
      { key: 'stream_tokens', label: '启用 SSE 流式输出', type: 'bool', default: true },
    ],
    initialConfig: { magpie_bin: 'magpie', routing_strategy: 'order', gateway_key: '', stream_tokens: true },
  },
  {
    id: 'plugin-ai-curator', name: 'AI Curator & Archive Rehydrator', version: '0.2.0', kind: 'hybrid', domain: 'core', author: 'OmniDesk',
    description: '克隆同步 obsidian-vault 与 music-daily 回灌全量历史推荐，硬隔离 daily_immutable 与 interactive_ephemeral 双模反写。',
    installed: true, enabled: true, healthOk: true, hotReloadMs: 22, verifyStatus: 'passed',
    provides: ['recommend.feed.v1', 'media.source.v1', 'text.source.v1', 'book.source.v1'], consumes: ['llm.chat.v1', 'media.playlist.v1'],
    permissions: ['fs:user_mount', 'network:wan', 'config:read', 'config:write'],
    config_schema: [
      { key: 'obsidian_vault_dir', label: 'Obsidian Vault 本地目录', type: 'string', default: '~/Projects/obsidian-vault' },
      { key: 'music_daily_dir', label: 'Music Daily 本地目录', type: 'string', default: '~/Projects/music-daily' },
      { key: 'auto_pull_remote', label: '启动时仅快进拉取双仓', type: 'bool', default: true },
    ],
    initialConfig: { obsidian_vault_dir: '~/Projects/obsidian-vault', music_daily_dir: '~/Projects/music-daily', auto_pull_remote: true },
  },
  {
    id: 'plugin-fnos-nas', name: 'Feiniu fnOS NAS Connector', version: '0.2.0', kind: 'hybrid', domain: 'media', author: 'OmniDesk',
    description: '用户自配置挂载目录原盘硬解、受保护双语字幕原子写回、美剧台词切片跳播（media.clip_jump.v1）与进度同步。',
    installed: true, enabled: true, healthOk: true, hotReloadMs: 18, verifyStatus: 'passed',
    provides: ['nas.connection.v1', 'media.local_match.v1', 'media.clip_jump.v1', 'book.sync.v1'], consumes: ['subtitle.fetch.v1', 'subtitle.sync.v1'],
    permissions: ['fs:user_mount', 'network:lan', 'secret:vault', 'config:write'],
    config_schema: [
      { key: 'mount_root', label: 'NAS 本地挂载根路径', type: 'string', default: '/mnt/feiniu-smb', required: true },
      { key: 'media_service_url', label: '伴随台词服务地址', type: 'string', default: 'http://192.168.50.12:8765' },
      { key: 'arr_api_key', label: 'Radarr / Sonarr API Key', type: 'secret', default: '', description: '0600 SecretVault 隔离存储' },
      { key: 'protect_manual_srt', label: '禁止覆盖人工精校字幕', type: 'bool', default: true },
    ],
    initialConfig: { mount_root: '/mnt/feiniu-smb', media_service_url: 'http://192.168.50.12:8765', arr_api_key: '', protect_manual_srt: true },
  },
  {
    id: 'plugin-music', name: 'Apple Music & Bilingual Lyrics', version: '0.2.0', kind: 'hybrid', domain: 'media', author: 'OmniDesk',
    description: '复用 qwen-translator 歌词与 Adam 引擎，执行国区曲库实证预检与 ChatGPT Daily 5 门禁写回。',
    installed: true, enabled: true, healthOk: true, hotReloadMs: 16, verifyStatus: 'passed',
    provides: ['media.source.v1', 'media.playlist.v1'], consumes: ['llm.translate.v1'],
    permissions: ['network:wan', 'secret:vault', 'config:read'],
    config_schema: [
      { key: 'storefront', label: 'Apple Music 区域', type: 'select', default: 'cn', options: ['cn', 'us', 'jp', 'hk'] },
      { key: 'daily_playlist_id', label: '每日累计歌单 ID', type: 'string', default: 'p.YJXV7dEIeb3GzZX' },
      { key: 'media_user_token', label: 'Media User Token', type: 'secret', default: '' },
    ],
    initialConfig: { storefront: 'cn', daily_playlist_id: 'p.YJXV7dEIeb3GzZX', media_user_token: '' },
  },
  {
    id: 'plugin-pdf-babeldoc', name: 'BabelDOC Dual-Column PDF', version: '0.2.0', kind: 'hybrid', domain: 'book', author: 'OmniDesk',
    description: '基于 BabelDOC 的学术 PDF 版面识别、公式与术语锁定及左右基线对齐双栏对照生成。',
    installed: true, enabled: true, healthOk: true, hotReloadMs: 19, verifyStatus: 'passed',
    provides: ['book.source.v1', 'book.bilingual_pdf.v1'], consumes: ['llm.translate.v1'],
    permissions: ['fs:user_mount', 'config:read', 'config:write'],
    config_schema: [
      { key: 'output_layout', label: '默认排版模式', type: 'select', default: 'dual_side_by_side', options: ['dual_side_by_side', 'mono_zh', 'interleaved'] },
      { key: 'max_concurrency', label: '并发翻译线程数', type: 'int', default: 4 },
      { key: 'lock_glossary', label: '强制锁定专业术语表', type: 'bool', default: true },
    ],
    initialConfig: { output_layout: 'dual_side_by_side', max_concurrency: 4, lock_glossary: true },
  },
  {
    id: 'plugin-chrome-web', name: 'Managed Chrome --app & MV3 Bridge', version: '0.2.0', kind: 'hybrid', domain: 'text', author: 'OmniDesk',
    description: '托管无地址栏 Chrome --app 窗并自动挂载 MV3 双语翻译扩展，经 web.cookies.v1 / web.fetch.v1 反哺社区与期刊。',
    installed: true, enabled: true, healthOk: true, hotReloadMs: 11, verifyStatus: 'passed',
    provides: ['web.embed.v1', 'web.cookies.v1', 'web.extract.v1', 'web.fetch.v1'], consumes: ['llm.translate.v1'],
    permissions: ['process:chrome', 'network:loopback', 'config:read'],
    config_schema: [
      { key: 'chrome_bin', label: 'Chrome 可执行文件', type: 'string', default: 'google-chrome' },
      { key: 'auto_inject_mv3', label: '自动加载 MV3 双语扩展', type: 'bool', default: true },
    ],
    initialConfig: { chrome_bin: 'google-chrome', auto_inject_mv3: true },
  },
  {
    id: 'plugin-asr-funasr', name: 'FunASR SenseVoiceSmall Offline', version: '0.1.8', kind: 'provider', domain: 'core', author: 'OmniDesk',
    description: '独立进程组 Vulkan/CPU 离线音视频分块转写与断点续传引擎（speech.asr.v1）。',
    installed: true, enabled: false, healthOk: true, hotReloadMs: 9, verifyStatus: 'idle',
    provides: ['speech.asr.v1'], consumes: [],
    permissions: ['process:funasr', 'process:ffmpeg', 'gpu:vulkan', 'fs:user_mount'],
    config_schema: [
      { key: 'model_dir', label: 'FunASR 模型目录', type: 'string', default: '~/.local/share/omnidesk/models/sensevoice' },
      { key: 'chunk_seconds', label: '分块切片时长（秒）', type: 'int', default: 120 },
      { key: 'use_vulkan', label: '启用 Vulkan GPU 加速', type: 'bool', default: true },
    ],
    initialConfig: { model_dir: '~/.local/share/omnidesk/models/sensevoice', chunk_seconds: 120, use_vulkan: true },
  },
  {
    id: 'plugin-arxiv-digest', name: 'arXiv & PubMed Deep Reader', version: '0.1.4', kind: 'workspace', domain: 'text', author: 'ZJFILOVE (Git)',
    description: '外部 Git 市场插件：支持按分类订阅 arXiv / PubMed 每日预印本并一键推送至 BabelDOC 双栏翻译。',
    installed: false, enabled: false, healthOk: true, hotReloadMs: 0, verifyStatus: 'idle',
    provides: ['text.source.v1'], consumes: ['book.bilingual_pdf.v1', 'llm.translate.v1'],
    permissions: ['network:wan', 'config:read'],
    config_schema: [
      { key: 'categories', label: '订阅分类', type: 'string', default: 'cs.CL,cs.LG,q-bio.NC' },
      { key: 'auto_fetch_pdf', label: '自动缓存 PDF 全文', type: 'bool', default: false },
    ],
    initialConfig: { categories: 'cs.CL,cs.LG,q-bio.NC', auto_fetch_pdf: false },
  },
  {
    id: 'plugin-sub-hunter', name: 'SubHunter & ffsubsync Aligner', version: '0.3.0', kind: 'provider', domain: 'media', author: 'OmniDesk',
    description: 'SubHD / ASSRT 多源字幕检索、16 MB 压缩包路径逃逸安全解压、ffsubsync VAD 音轨对齐与 llm.translate.v1 双语翻译。',
    installed: true, enabled: true, healthOk: true, hotReloadMs: 12, verifyStatus: 'passed',
    provides: ['subtitle.fetch.v1', 'subtitle.sync.v1', 'subtitle.translate.v1'], consumes: ['web.cookies.v1', 'llm.translate.v1'],
    permissions: ['network:wan', 'fs:user_mount', 'process:ffmpeg'],
    config_schema: [
      { key: 'assrt_token', label: 'ASSRT API Token', type: 'secret', default: '', description: '0600 SecretVault 隔离存储' },
      { key: 'preferred_langs', label: '首选语言优先级', type: 'string', default: 'chs,eng,cht' },
      { key: 'translation_concurrency', label: '字幕翻译并发数', type: 'int', default: 4, description: 'llm.translate.v1 同时翻译的分段数，范围 1-8（默认 4）' },
      { key: 'subtitle_output_mode', label: '字幕输出模式', type: 'select', default: 'bilingual', options: ['bilingual', 'translated', 'original'], description: '双语 / 仅译文 / 仅原文' },
      { key: 'max_offset_ms', label: '自动对齐最大偏移阈值 (ms)', type: 'int', default: 12000 },
    ],
    initialConfig: { assrt_token: '', preferred_langs: 'chs,eng,cht', max_offset_ms: 12000 },
  },
  {
    id: 'plugin-sponsorblock', name: 'SponsorBlock K-Anon Timeline', version: '0.1.9', kind: 'extension', domain: 'media', author: 'OmniDesk',
    description: 'YouTube (sponsor.ajay.app) 与 B 站 (bsbsb.top) K-匿名哈希前缀片段查询，向播放器注入空降章节。',
    installed: true, enabled: true, healthOk: true, hotReloadMs: 8, verifyStatus: 'passed',
    provides: ['player.timeline.v1'], consumes: [],
    permissions: ['network:wan', 'config:read'],
    config_schema: [
      { key: 'auto_skip_sponsor', label: '自动跳过赞助与恰饭片段', type: 'bool', default: true },
      { key: 'categories', label: '启用片段分类', type: 'string', default: 'sponsor,selfpromo,intro' },
    ],
    initialConfig: { auto_skip_sponsor: true, categories: 'sponsor,selfpromo,intro' },
  },
  {
    id: 'plugin-ebook', name: 'EPUB & SM-2 Vocabulary Studio', version: '0.2.0', kind: 'hybrid', domain: 'book', author: 'OmniDesk',
    description: 'EPUB/AZW3/TXT 分页伴读、点词词源拆解、SM-2 间隔重复生词卡及 NAS 美剧台词跳播联动。',
    installed: true, enabled: true, healthOk: true, hotReloadMs: 15, verifyStatus: 'passed',
    provides: ['book.source.v1', 'vocab.sm2.v1'], consumes: ['media.clip_jump.v1', 'book.sync.v1'],
    permissions: ['fs:user_mount', 'config:read', 'config:write'],
    config_schema: [
      { key: 'daily_review_limit', label: '每日 SM-2 新词复习上限', type: 'int', default: 40 },
      { key: 'auto_jump_nas', label: '查词自动匹配飞牛 NAS 台词切片', type: 'bool', default: true },
    ],
    initialConfig: { daily_review_limit: 40, auto_jump_nas: true },
  },
  {
    id: 'plugin-zlib', name: 'Z-Library & Anna’s Book Search', version: '0.1.6', kind: 'hybrid', domain: 'book', author: 'OmniDesk Community',
    description: '检索多镜像书库并一键下载至本地书架、BabelDOC 双栏队列或飞牛 NAS 归档目录。',
    installed: false, enabled: false, healthOk: true, hotReloadMs: 0, verifyStatus: 'idle',
    provides: ['book.search.v1'], consumes: ['book.bilingual_pdf.v1', 'web.cookies.v1'],
    permissions: ['network:wan', 'fs:user_mount', 'secret:vault'],
    config_schema: [
      { key: 'mirror_endpoint', label: '主镜像地址', type: 'string', default: 'https://annas-archive.org', required: true },
      { key: 'remix_key', label: 'Account Secret Key', type: 'secret', default: '' },
    ],
    initialConfig: { mirror_endpoint: 'https://annas-archive.org', remix_key: '' },
  },
];
