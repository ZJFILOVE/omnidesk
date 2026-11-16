# OmniDesk Plugin Architecture Specification (Protocol v2 · Zero-ABI Python)

OmniDesk v2 adopts a **Lean Three-Layer Shell + Zero-ABI Pure Python Plugin** architecture:

- **Native Shell (`shell-tauri/`)**: Pure Wayland/Hyprland window (`app_id="omnidesk"`) + `libmpv` OpenGL hardware video surface + MPRIS2 media keys (`~500` LOC Rust, zero business logic).
- **Keyboard-First Frontend (`ui/`)**: Paper `#F6F5F3` & Graphite `#181718` theme, 2D Spatial Keyboard Focus Engine (`hjkl`/arrows, `<Keycap>` badges, `Ctrl+1..5`, `Alt+<Key>`, `f` hints, `Ctrl+K`, `F8` Companion Dock), and four domain workbenches (`MediaWorkbench`, `TextWorkbench`, `BookWorkbench`, `AiWorkbench`) + `PluginStoreView`.
- **Python Core & SDK (`core/`)**: `omnidesk_core` + `omnidesk_sdk` (`Python 3.14+`, managed by `uv`), providing `CapabilityRegistry`, `EntityBus`, `DomainRegistry`, `JobCenter`, `0600` `ConfigStore` & `SecretVault`, SQLite FTS5 `SearchIndex`, and the `$XDG_RUNTIME_DIR/omnidesk/control.sock` inbound gateway.
- **Pure Python Plugins (`plugins/<plugin-id>/`)**: Every integration (**Magpie AI Router**, **AI Curator**, **Feiniu fnOS NAS**, **Music**, **Bilibili**, **YouTube**, **Zhihu**, **Linux.do**, **Shuiyuan**, **V2EX**, **Reddit**, **X**, **Journals**, **PDF/BabelDOC**, **EPUB Companion**, **Z-Library**, **Anna's Archive**, **Chrome Web Bridge**, **FunASR**, **SubHunter**, **SponsorBlock**) is a zero-compile Python package (`150~350` LOC) hot-loaded via `importlib` and Pydantic/JSON contracts.

> **Hard Anti-Bloat Rule**: C++ `.so` plugins, `CMake`, `dlopen`, `sdk_abi_signature`, and `DsoViewLease` are permanently retired. Every source file in `core/`, `plugins/`, `ui/`, and `shell-tauri/` MUST stay `<= 400` lines.

---

## 1. Plugin Directory Layout

Built-in and marketplace plugins share the exact same self-contained directory structure (either inside `plugins/<plugin-id>/` in the main monorepo or in an external Git repository installed via `omnidesk-pm install`):

```text
plugins/plugin-<slug>/
├── plugin.json          # Required: manifest, permissions, and config_schema (schema_version = 2)
├── plugin.py            # Required: BasePlugin subclass & create_plugin() entrypoint (<= 400 lines)
├── sources.py           # Optional: split MediaSource / TextSource / BookSource implementations
├── tests/
│   └── test_plugin.py   # Required: offline pytest contract & parser smoke tests (< 2s)
└── README.md            # Optional: concise plugin usage notes
```

Scaffold a new plugin in 1 second:
```bash
omnidesk-pm init plugins/plugin-mytool --id plugin-mytool --domain core --kind provider --provides ai.agent.v1
```

---

## 2. `plugin.json` Manifest Contract (`schema_version = 2`)

```json
{
  "schema_version": 2,
  "id": "plugin-magpie-agent",
  "name": "Magpie AI Agent Router",
  "version": "0.2.0",
  "runtime": "python",
  "entry": "plugin:create_plugin",
  "kind": "provider",
  "domain": "core",
  "author": "OmniDesk",
  "description": "Reads local magpie harnesses/models/quotas and routes requests through group/omnidesk-active.",
  "provides": ["ai.agent.v1", "ai.curation.v1", "llm.router.v1", "llm.chat.v1", "llm.translate.v1"],
  "consumes": [],
  "permissions": ["process:magpie", "network:loopback", "config:read", "config:write", "secret:vault"],
  "verify_cmd": "uv run pytest -q",
  "config_schema": [
    {
      "key": "magpie_bin",
      "label": "Magpie CLI 路径",
      "type": "string",
      "default": "magpie",
      "description": "Executable name on PATH or absolute path to magpie"
    },
    {
      "key": "routing_strategy",
      "label": "路由调度策略",
      "type": "select",
      "default": "order",
      "options": ["manual", "order", "smart", "usage", "pace"],
      "description": "Magpie group routing policy for group/omnidesk-active"
    },
    {
      "key": "gateway_key",
      "label": "Magpie Gateway Key（可选）",
      "type": "secret",
      "default": "",
      "description": "Stored exclusively in 0600 SecretVault (never written to plain config or logs)"
    }
  ]
}
```

### Valid Enums & Constraints
- `runtime`: `"python"` (default)
- `entry`: `"<module>:<factory_or_class>"` relative to the plugin directory (default `"plugin:create_plugin"`)
- `kind`: `"provider"` | `"workspace"` | `"hybrid"` | `"extension"`
- `domain`: `"core"` | `"media"` | `"text"` | `"book"`
- `config_schema[].type`: `"string"` | `"secret"` | `"bool"` | `"int"` | `"select"`
- `id`: must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$` (no `/`, `\`, or `..`)

---

## 3. Pure Python SDK (`omnidesk_sdk`), Declarative UI & Fault Isolation

### 3.1 Minimal Plugin Example (`plugin.py`)

```python
from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext


class MagpieAgentPlugin(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        ctx.register_capability("llm.router.v1", self.handle_router)
        ctx.register_streaming_capability("llm.chat.v1", self.handle_chat_stream)

    async def handle_router(self, req: dict) -> CapabilityResult:
        return CapabilityResult.ok({"gateway_running": True, "group_id": "omnidesk-active"})

    async def handle_chat_stream(self, req: dict, stream) -> CapabilityResult:
        await stream.emit_delta("Hello from Magpie")
        return CapabilityResult.ok({"reply": "Hello from Magpie"})


def create_plugin() -> BasePlugin:
    return MagpieAgentPlugin()
```

### 3.2 Scoped Lifecycle, Hot-Reload & Fault Isolation
1. **Zero Binary Fragility**: Plugins exchange validated Python `Pydantic` models / JSON dicts (`MediaItem`, `TextFeedItem`, `TextArticle`, `BookItem`, `BookSpread`, `VocabCard`, `CapabilityResult`). Updating `omnidesk_core` or `ui/` never invalidates existing plugins.
2. **Automatic Scoped Cleanup**: Every capability, `EntityBus` subscription, domain data source (`register_media_source` / `register_text_source` / `register_book_source`), background `asyncio.Task`, and declarative panel registered via `PluginContext` is automatically tagged with `ctx.plugin_id` and revoked atomically on `unload` or hot-reload.
3. **Exception & Process-Group Barriers**:
   - Uncaught Python exceptions inside capability handlers, data sources, or event subscribers are caught by `omnidesk_core`, logged without leaking secrets, and published as `"plugin.fault"`—never crashing the core or freezing the UI.
   - Heavy external native tools (`ffmpeg`, `ffprobe`, `llama-funasr-sensevoice`, `yt-dlp`, `magpie`, `google-chrome`) MUST be spawned via `ctx.exec_subprocess(...)` (arg-list only, `shell=False`, dedicated process group `start_new_session=True` so cancellation sends `SIGTERM -> SIGKILL` cleanly to the entire process tree).
4. **Declarative Schema Panels (Zero Imperative Widget Trees in Plugins)**:
   - Settings UI is rendered automatically in React from `plugin.json` `config_schema`.
   - Custom plugin tabs (`ctx.register_workspace_tab(...)`) and `F8` dock cards (`ctx.register_dock_widget(...)`) return lightweight JSON Schema descriptors (`Section`, `Form`, `KeyValue`, `List`, `ActionRow`, `Markdown`) and receive `on_action(action_id, state)` callbacks—keeping 100% of keyboard spatial focus and Paper/Graphite styling consistent in `ui/`.

---

## 4. Standard Cross-Plugin Capability Contracts & Inbound Agent Gateway

Plugins collaborate strictly through `CapabilityRegistry` and `EntityBus`—never by importing another plugin's private directory. In addition, `omnidesk_core` listens on a `0600` Unix Domain Socket (`$XDG_RUNTIME_DIR/omnidesk/control.sock`, accessible via `omnidesk-pm rpc <cap> '<json>'`) so external coding/scheduled AI Agents can invoke capabilities and push recommendations into the live GUI (with offline persistence fallback in `<XDG_DATA_HOME>/omnidesk/curation/`). Full specifications: [docs/PLUGIN_INTERFACE_PROTOCOL.md](docs/PLUGIN_INTERFACE_PROTOCOL.md) and [docs/AI_AGENT_RECOMMENDATION_PROTOCOL.md](docs/AI_AGENT_RECOMMENDATION_PROTOCOL.md).

| Capability ID | Intended Provider | Key Operations & Purpose |
| :--- | :--- | :--- |
| `ai.agent.v1` | `plugin-magpie-agent` | `{"action":"chat","domain","context","prompt"}` -> contextual AI assistant response for `F8` Companion Dock |
| `ai.curation.v1` | `plugin-magpie-agent` | `{"action":"curate_interactive" \| "run_daily_pipeline", ...}` -> embedded AI Agent orchestrating skill-driven curation & write-back |
| `llm.router.v1` | `plugin-magpie-agent` | `{"action":"current_route" \| "list_harnesses" \| "list_models" \| "list_quotas" \| "set_route"}` -> reads local `magpie` state and updates `group/omnidesk-active` |
| `llm.chat.v1` / `llm.translate.v1` | `plugin-magpie-agent` | Streaming/sync completion & glossary-aware bilingual translation consumed across all workbenches |
| `recommend.feed.v1` | `plugin-ai-curator` | Clone & rehydrate historical archives (`obsidian-vault` + `music-daily`), provenance lookup, and `daily_immutable` vs `interactive_ephemeral` workbench write-back |
| `media.playlist.v1` | `plugin-music` | Apple Music CN catalog preflight (`verified_ids`), gated sync to cumulative playlist `p.YJXV7dEIeb3GzZX` (`ChatGPT Daily 5`), and isolated interactive playlist / `libmpv` queue write-back |
| `nas.connection.v1` | `plugin-fnos-nas` | User-configured fnOS NAS session (`status`, `probe`) and safe atomic subtitle write-back |
| `media.local_match.v1` | `plugin-fnos-nas` | Match cloud video/IMDb metadata against local/NAS media files |
| `media.clip_jump.v1` | `plugin-fnos-nas` | Given a vocabulary word or quote (`{"query"}`), return matching subtitle timestamped clips in NAS media and jump `libmpv` |
| `book.sync.v1` / `vocab.sm2.v1` | `plugin-fnos-nas` / `plugin-ebook` | Cross-device reading progress sync and SM-2 spaced-repetition vocabulary cards |
| `web.embed.v1` / `web.cookies.v1` / `web.extract.v1` / `web.fetch.v1` | `plugin-chrome-web` | Managed Chrome `--app` auth/reader window, MV3 cookie sync, rendered DOM extraction, and same-origin `fetch` proxy |

### Standard `EntityBus` Topics
- `entity.media.open` (`item_id`, `title`, `source_id`, `play_url`, `start_sec`, `end_sec`)
- `entity.text.open` (`article_id`, `source_id`, `url`, `doi`)
- `entity.book.open` (`book_id`, `file_path`, `format`, `page`)
- `entity.vocab.add` (`word`, `context_sentence`, `source_title`)
- `entity.recommend.updated` (`mode`, `set_id`, `operation_id`, `domains`, `written_count`, `auto_focus`)
- `entity.music.playlist_synced` (`mode`, `playlist_id`, `playlist_name`, `added_count`, `verified_count`)
- `curation.job.progress` (`job_id`, `mode`, `stage`, `message`)
- `asr.job.progress` / `asr.job.completed` (`job_id`, `media_path`, `progress`, `output_srt_path`)
- `entity.ai.route_changed` (`summary`, `group_id`, `primary_member`)
- `plugin.state_changed` (`plugin_id`, `state`)
- `plugin.fault` (`plugin_id`, `component`, `error`)

---

## 5. Security, Verification & Rollback

1. **Pre-Activation Smoke Gate**: External plugins installed or updated via `omnidesk-pm` are staged in `/tmp/omnidesk-staging/`, verified with `verify_cmd` (`uv run pytest -q`) plus manifest/lifecycle checks (`on_load -> on_health_check -> on_unload`), and switched atomically only when green.
2. **Source Pinning & Instant Rollback**: External Git plugins record exact commit SHA and tree hash in `<XDG_DATA_HOME>/omnidesk/upstreams.lock.json`, keeping up to 5 snapshots in `<XDG_DATA_HOME>/omnidesk/backups/<plugin_id>/` for instant `omnidesk-pm rollback <id>`.
3. **`0600` Config & SecretVault**: Plain configuration lives in `<XDG_CONFIG_HOME>/omnidesk/plugins/<id>.json` (`0600`); fields marked `"type": "secret"` live exclusively in `<XDG_CONFIG_HOME>/omnidesk/vault.json` (`0600`) and are stripped from all logs, UI snapshots, and error payloads.
4. **Network Hygiene**: Loopback (`127.0.0.1`) and LAN (`plugin-fnos-nas`, Radarr/Sonarr) HTTP clients enforce `trust_env=False` (bypassing system proxies so local tokens never leak), and cross-origin HTTP redirects automatically strip `Cookie` and `Authorization` headers.
