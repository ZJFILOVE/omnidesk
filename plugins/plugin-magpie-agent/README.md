# Magpie AI and translation

The desktop header opens the translation center. Articles, music lyrics,
YouTube captions and book pages have contextual translation buttons. Results
are written back to the matching paragraph/cue IDs in the current reader;
manual translations currently last for the mounted workbench session.

`llm.translate.v1` supports `settings`, `configure`, and text translation:

- `translation_gateway_url`: optional local OpenAI-compatible gateway base URL;
  blank uses the existing Magpie gateway. Credentials are not forwarded to a
  different endpoint. Authenticated custom gateways can be configured through
  Magpie, using its existing gateway key.
- `translation_models`: comma-separated model IDs, independent of the chat
  route. Multiple models are assigned to batches in round-robin order.
- `translation_concurrency`: 1–8 simultaneous batch requests (default 4).
- `translation_batch_size`: 1–32 input segments per batch (default 4).

Requests can override these defaults with `model`/`models`, `concurrency`, and
`batch_size`. Stable segment IDs and input order are preserved even when batches
finish out of order. A terminal error stops queued work and cancels sibling
coroutines; rate limits share a bounded exponential cooldown. This follows the
bounded-pool and cancellation approach in the local
`qwen-translator/translation_pool.py`, with OmniDesk's capability transport.

YouTube displays original subtitles first (`bilingual: false`); the user can
then select a translation model. Lyric translation sends an array of individual
lines instead of flattening all cues into a single string. Missing source
lyrics/subtitles and API failures remain visible, without placeholder content.

翻译中心可切换 `magpie`、`codex`、`agy`。三种后端分别保存模型列表，
CLI 路径留空自动检测；Codex/AGY 直接使用本机登录，不经过 Magpie。
CLI 每批创建独立临时工作目录、通过 stdin 传入文本，禁用工具，
超时/取消通过 SDK 清理进程组。Codex 使用 ephemeral 模式。
并发为 1–8；多模型按批次轮询，保留段落 ID、顺序与字幕时间。
额度用尽中止任务，限流采用共享退避；不会自动换后端。

Agent 选择器新增 AGY 上下文助手，读取 OmniDesk 提供的工作台上下文；
当前不执行 MCP 操作。Codex Agent 保留既有 MCP 工具。
`agy_model` 与 `translation_agy_models` 独立设置。

Codex 参数依据 [官方非交互文档](https://developers.openai.com/codex/noninteractive)
及本机 CLI help；CLI 并发模式参考本地 qwen-translator。
