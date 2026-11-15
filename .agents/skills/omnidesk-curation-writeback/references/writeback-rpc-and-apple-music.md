# OmniDesk 前端与 Apple Music 反写 RPC 手册

本手册提供外部 Agent（通过 `omnidesk-pm rpc` CLI / `$XDG_RUNTIME_DIR/omnidesk/control.sock`）与内驻 Agent（通过 `PluginContext::capabilities().call(...)`）共用的标准 JSON 载荷模板。

---

## 1. CLI 与 Socket 调用方式

- **推荐 CLI 调用（将复杂 JSON 写入临时文件避免引号转义问题）**：
  ```bash
  omnidesk-pm rpc recommend.feed.v1 --file /tmp/omnidesk_feed_payload.json
  omnidesk-pm rpc media.playlist.v1 --file /tmp/omnidesk_playlist_payload.json
  ```
- **桌面端未启动时的离线行为**：`omnidesk-pm rpc` 若探测到 `$XDG_RUNTIME_DIR/omnidesk/control.sock` 未监听，会自动回退至本地直写模式（原子更新 `<XDG_DATA_HOME>/omnidesk/curation/`，或拉起临时 `omnidesk-plugin-worker` 完成歌单同步），无需人工干预。

---

## 2. `recommend.feed.v1` 标准反写载荷模板

### 2.1 模式 A：发布每日不重复精选（`publish_daily_bundle`）

```json
{
  "action": "publish_daily_bundle",
  "mode": "daily_immutable",
  "operation_id": "music:2026-11-14",
  "business_date": "2026-11-14",
  "qa_verified": true,
  "publication_evidence": {
    "status": "PUBLISHED",
    "publication_verified": true,
    "operation_id": "music:2026-11-14",
    "project": "music",
    "business_date": "2026-11-14",
    "receipt": {
      "path": ".recommendation/transactions/music/2026-11-14.json",
      "bundle_sha256": "<committed-bundle-sha256>"
    }
  },
  "media_items": [
    {
      "id": "am-cn-1643009587",
      "title": "Muerte",
      "subtitle": "Natalia Lafourcade · De Todas las Flores",
      "source_id": "plugin-ai-curator",
      "kind": "music",
      "play_url": "https://music.apple.com/cn/song/1643009587",
      "quality_label": "Apple Music CN · ALAC",
      "has_bilingual_sub": true,
      "extra": {
        "curation_mode": "daily_immutable",
        "business_date": "2026-11-14",
        "priority_rank": 1,
        "apple_music_song_id": "1643009587",
        "storefront": "cn",
        "canonical_id": "Natalia Lafourcade::Muerte",
        "why_recommended": "把沉重命题放进呼吸可闻的近距离原声编配尺度里……",
        "listening_or_reading_cue": "第一遍跟主唱句尾气息，第二遍改听木吉他拨弦与低频托底。",
        "verification_method": "apple_music_cn_catalog"
      }
    }
  ],
  "text_items": [],
  "book_items": [],
  "vocab_cards": []
}
```

> `qa_verified` 只表示本地 QA 结果，不能证明上游事务已提交。`publish_daily_bundle` 与 `run_daily_pipeline` 必须同时提供真实 `PUBLISHED` 事务/收据；缺失时服务端返回 `needs_verified_bundle`，不得继续发布或同步歌单。

> 若为 `english:YYYY-MM-DD`，可将仅限会话展示的 `今日饮食` 放入顶层 `"ephemeral_chat_note": "今日饮食：..."`——服务端仅将其推送至前端瞬时气泡，绝不落盘保存。

### 2.2 模式 B：推送按需可重复推荐集（`push_interactive_set`）

```json
{
  "action": "push_interactive_set",
  "mode": "interactive_ephemeral",
  "session_id": "ix-rainy-coding",
  "title": "雨夜 C++ 编码器乐与环境电子（可重温）",
  "user_prompt": "来 5 首适合雨夜写 C++ 的安静电子与木吉他，以前推过的也可以",
  "repeat_policy": "allow_any",
  "record_permanent_history": false,
  "replace_session_items": false,
  "auto_focus": true,
  "media_items": [
    {
      "id": "ix-am-1833283533",
      "title": "Tobiume (2025 Remaster)",
      "subtitle": "Susumu Yokota · Sakura (2025 Remaster)",
      "source_id": "plugin-ai-curator",
      "kind": "music",
      "play_url": "https://music.apple.com/cn/song/1833283533",
      "quality_label": "Apple Music CN · ALAC",
      "extra": {
        "curation_mode": "interactive_ephemeral",
        "priority_rank": 1,
        "apple_music_song_id": "1833283533",
        "storefront": "cn",
        "previously_recommended_on": "2026-11-13",
        "why_recommended": "不靠明确副歌抢占工作记忆，短采样循环与柔和拖尾非常适合长时编码背景。",
        "listening_or_reading_cue": "用偏低音量播放，留意边缘短促音粒的景深变化。"
      }
    }
  ],
  "text_items": []
}
```

---

## 3. `media.playlist.v1` Apple Music 写回载荷模板

### 3.1 国区可播性预检（选品定稿前必调）

```json
{
  "action": "preflight_cn_catalog",
  "storefront": "cn",
  "track_ids": ["1643009587", "1607100336", "1833283533", "3735675", "1032611724"]
}
```

### 3.2 每日不重复正式歌单同步（仅限 `daily_immutable` QA 通过后）

同步至固定累计歌单 **`p.YJXV7dEIeb3GzZX`（`ChatGPT Daily 5`）** 及当日歌单：

```json
{
  "action": "sync_daily_authorized",
  "mode": "daily_immutable",
  "business_date": "2026-11-14",
  "operation_id": "music:2026-11-14",
  "qa_verified": true,
  "cumulative_playlist_id": "p.YJXV7dEIeb3GzZX",
  "create_dated_playlist": true,
  "dated_playlist_prefix": "ChatGPT Daily 5 · ",
  "track_ids": ["1643009587", "1607100336", "1833283533", "3735675", "1032611724"]
}
```

### 3.3 按需可重复灵感歌单写回 / 即时入队播放（仅限 `interactive_ephemeral`）

**严禁传入 `p.YJXV7dEIeb3GzZX`**：

```json
{
  "action": "write_interactive_playlist",
  "mode": "interactive_ephemeral",
  "playlist_name": "OmniDesk AI 灵感电台",
  "write_strategy": "append",
  "enqueue_now": true,
  "track_ids": ["1833283533", "1643009587", "3735675"]
}
```
