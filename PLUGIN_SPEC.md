# OmniDesk Plugin Architecture Specification (ABI v1)

OmniDesk adopts a strict **Microkernel + Decoupled Plugin Marketplace** architecture:

- The **Host** (`omnidesk`) provides only core infrastructure:
  1. Design System & Borealis/NanoVG/SDL2 Desktop Shell (Paper `#F6F5F3` & Graphite `#181718`, keyboard-first navigation)
  2. Three dedicated content domain workbenches (`MediaWorkbench`, `TextWorkbench`, `BookWorkbench`) + `F8` Companion Dock
  3. **OmniDesk SDK** (`libomnidesk_sdk.so`, C++17 + C ABI v1)
  4. **Plugin Marketplace Engine** (`omnidesk-pm` & in-app `PluginStoreView`)
- Every concrete integration (**AI Agent / Magpie router**, **Feiniu fnOS NAS**, **Bilibili**, **YouTube**, **Zhihu**, **Linux.do**, **Reddit**, **Journals**, **PDF/BabelDOC**, **EPUB Companion**) lives in its own independent directory and GitHub repository, compiled and hot-loaded dynamically at runtime as a `.so` shared library.

---

## 1. Repository Layout

Every standalone plugin repository MUST provide at its root:

```
omnidesk-plugin-<name>/
  plugin.json
  CMakeLists.txt
  src/
    plugin_main.cpp
  tests/
    test_abi.cpp
  README.md
```

Use `omnidesk-pm init <dir> --id <id> ...` (or the in-app `+ 脚手架` button) to generate this skeleton automatically.

---

## 2. `plugin.json` Manifest Contract

```json
{
  "schema_version": 1,
  "id": "plugin-magpie-agent",
  "name": "Magpie AI Agent Router",
  "version": "0.1.0",
  "abi_version": 1,
  "kind": "provider",
  "domain": "core",
  "author": "OmniDesk",
  "description": "Reads local magpie harnesses/models/quotas and pushes routing selections back into magpie.",
  "homepage": "https://github.com/ZJFILOVE/omnidesk-plugin-magpie-agent",
  "repository": {
    "type": "git",
    "url": "https://github.com/ZJFILOVE/omnidesk-plugin-magpie-agent.git",
    "branch": "main"
  },
  "build": {
    "system": "cmake",
    "entry_target": "plugin_magpie_agent",
    "output_lib": "libplugin_magpie_agent.so",
    "cmake_args": [],
    "verify_cmd": "ctest --output-on-failure"
  },
  "provides": ["ai.agent.v1", "llm.router.v1", "llm.chat.v1"],
  "consumes": [],
  "permissions": ["process:magpie", "network:loopback", "config:read", "config:write"],
  "config_schema": [
    {
      "key": "selected_harness",
      "label": "Active Harness",
      "type": "select",
      "default": "auto",
      "options": ["auto", "claude", "codex", "opencode", "gemini"],
      "description": "Target coding/agent harness managed by magpie"
    },
    {
      "key": "selected_route",
      "label": "Model / Group Route",
      "type": "string",
      "default": "",
      "description": "Selected magpie routing group or provider/model target"
    },
    {
      "key": "api_key",
      "label": "Optional Secret",
      "type": "secret",
      "default": "",
      "description": "Stored in 0600 SecretVault (never emitted in logs or config JSON)"
    }
  ]
}
```

### Valid Enums
- `kind`: `"provider"` | `"workspace"` | `"hybrid"` | `"extension"`
- `domain`: `"core"` | `"media"` | `"text"` | `"book"`
- `isolation` (optional, default `"in_process"`): `"in_process"` | `"subprocess"`
- `worker_transport` (optional, default `"pipe"`): `"pipe"` | `"unix_socket"` (used when `isolation == "subprocess"`)
- `config_schema[].type`: `"string"` | `"secret"` | `"bool"` | `"int"` | `"select"`
- `id`: must match `^[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}$`

---

## 3. Binary C ABI v1, SDK Layout Fingerprint & Fault Isolation

Defined in `<omnidesk/plugin_abi.h>`, `<omnidesk/abi_fingerprint.hpp>`, and `<omnidesk/plugin.hpp>`:

- Every `.so` must export three unmangled `extern "C"` symbols via `OMNIDESK_DECLARE_PLUGIN(ClassName, id, name, version, kind, domain)`:
  - `const OmniDeskPluginInfo* omnidesk_plugin_abi_info(void)`
  - `void* omnidesk_plugin_create(void)` -> returns `static_cast<void*>(static_cast<omnidesk::IPlugin*>(new ClassName()))`
  - `void omnidesk_plugin_destroy(void* instance)`
- `OMNIDESK_DECLARE_PLUGIN` embeds a compile-time `sdk_abi_signature`. CMake hashes the public SDK and Borealis headers with SHA-256 and regenerates the signature header when their contents change; the final fingerprint also includes type sizes/alignment, interface revisions, compiler/stdlib ABI traits, and nlohmann JSON version/options. Same-size field edits and virtual-method changes therefore invalidate old binaries even when the manual ABI version is unchanged. This is conservative: header-only documentation edits can also require rebuilding. The C handshake returns static POD/string storage without constructing SDK objects or allocating memory.
- Before calling `omnidesk_plugin_create()`, `PluginInstaller` (`omnidesk-pm abi-check <so> --expect-id <id>`) and `PluginManager` verify `abi_version == OMNIDESK_PLUGIN_ABI_VERSION`, `struct_size >= sizeof(OmniDeskPluginInfo)`, and `sdk_abi_signature == omnidesk::sdkAbiSignature()` so older/stale `.so` binaries are cleanly rejected without segfaulting.
- **Process & Exception Fault Isolation**:
  - **Subprocess Isolation (`"isolation": "subprocess"`)**: Third-party or untrusted `.so` plugins load exclusively inside `omnidesk-plugin-worker`, communicating with the host via pure-JSON NDJSON RPC over `"pipe"` or `"unix_socket"` (`WorkerSupervisor` + `WorkerMediaSource`/`WorkerTextSource`/`WorkerBookSource`). Worker crashes (`SIGSEGV`/`SIGABRT`) are isolated from the host UI, reported via `worker.fault`, and automatically restarted (with crash-loop quarantine).
  - **In-Process Exception Barriers**: `CapabilityRegistry`, `EntityBus`, `DomainRegistry`, and `PluginManager` catch C++ exceptions and report `"plugin.fault"`. In-process native code, including static initializers run by `dlopen`, can still crash or deadlock the host. Reserve this mode for trusted UI code.
  - **Bounded Worker IO**: Requests have interruptible queue waits, nonblocking writes, and read deadlines. Timeouts and malformed responses produce fault events and participate in restart/quarantine limits. Frames and accumulated stream text are limited to 8 MiB, and stderr retains at most 100 chunks of 4096 bytes. Stopping a worker also terminates descendants in its process group.
  - **Request Semantics**: Plugin workers restart after a fault, but do not replay the failed capability call automatically. Explicit `WorkerSpec::retry_on_crash` is for idempotent operations only; streams that already delivered a chunk are never replayed. Unregistering a worker prevents an in-flight request from restarting it.

For a plugin with custom NanoVG/Borealis views, keep its small UI shell in-process and register a separate worker with `PluginContext::registerWorker`, `registerWorkerCapability`, and the `registerWorker*Source` helpers. Whole-plugin subprocess mode exposes capabilities and media/text/book data sources; it rejects UI registrations rather than silently dropping them. Over the `--duplex` multiplexed channel (`WorkerHostBridge` + worker-side `RemoteBridge`), the worker's `CapabilityRegistry` (`has`, `providersFor`, `listAll`, sync/streaming `call*`, and cancellation propagation) and `EntityBus` (`subscribe`, `unsubscribe`, echo-free `publish`, and host event push) transparently forward cross-plugin capability calls and host event subscriptions—including calls made inside `onLoad` and re-entrant nested calls. This is process fault isolation, not a security sandbox or a permanent C++ ABI guarantee.

Host article feeds, articles, book libraries/pages, and media lyrics/chapters perform provider IO on background jobs and apply results on the UI thread. Replaced requests and destroyed views discard late results.

### Scoped Cleanup Guarantee
When `onLoad(PluginContext& ctx)` registers capabilities (`ctx.registerCapability(...)`), subscribes to bus events (`ctx.subscribe(...)`), or adds domain sources/tabs/widgets (`ctx.registerMediaSource(...)`, `ctx.registerWorkspaceTab(...)`, etc.), `PluginManager::unloadPlugin()` automatically revokes **every** registration tagged with `plugin_id` (and stops its isolated worker if running in `subprocess` mode) on unload, hot-reload, update, or failure.

---

## 4. Standard Cross-Plugin Capability Contracts & Inbound Agent Gateway

Plugins collaborate strictly through `CapabilityRegistry` and `EntityBus`—never by linking directly against one another. In addition to in-app plugins, the Host exposes a `0600` local Unix Domain Socket (`$XDG_RUNTIME_DIR/omnidesk/control.sock`, accessible via `omnidesk-pm rpc <cap> '<json>'`) so external coding/scheduled AI Agents can invoke capabilities and push recommendations into the live GUI (with offline persistence fallback in `<XDG_DATA_HOME>/omnidesk/curation/`). Full specification: [docs/AI_AGENT_RECOMMENDATION_PROTOCOL.md](docs/AI_AGENT_RECOMMENDATION_PROTOCOL.md).

| Capability ID | Intended Provider | Key Operations & Purpose |
| :--- | :--- | :--- |
| `ai.agent.v1` | `plugin-magpie-agent` | `{"action":"chat","domain","context","prompt"}` -> contextual AI assistant response for `F8` Companion Dock |
| `ai.curation.v1` | `plugin-magpie-agent` | `{"action":"curate_interactive" \| "run_daily_pipeline", ...}` -> embedded AI Agent orchestrating skill-driven curation & write-back |
| `llm.router.v1` | `plugin-magpie-agent` | `{"action":"current_route"}` / `{"action":"list_harnesses"}` / `{"action":"apply_route","harness","group","model"}` — reads local `magpie` state and pushes route choices back to `magpie` |
| `llm.chat.v1` | `plugin-magpie-agent` | General completion / translation / summarization consumed by Text & Book workbenches |
| `recommend.feed.v1` | `plugin-ai-curator` | Clone & rehydrate historical archives (`obsidian-vault` + `music-daily`), and reverse-write `daily_immutable` (non-repeating) or `interactive_ephemeral` (repeatable) recommendations across Media, Text, and Book workbenches |
| `media.playlist.v1` | `plugin-music` | Apple Music CN catalog preflight, gated sync to cumulative playlist `p.YJXV7dEIeb3GzZX` (`ChatGPT Daily 5`), and isolated repeatable interactive playlist / MPV playback queue write-back |
| `nas.connection.v1` | `plugin-fnos-nas` | User-configured fnOS NAS session (`status`, `list_dir`, `stream_url`) |
| `media.local_match.v1` | `plugin-fnos-nas` | Match cloud video/music metadata against local/NAS media files |
| `media.clip_jump.v1` | `plugin-fnos-nas` | Given a vocabulary word or quote (`{"query","book"}`), return matching subtitle timestamped clip in NAS media |
| `book.sync.v1` | `plugin-fnos-nas` | Reading progress & vocabulary sync across desktop and NAS |
| `web.embed.v1` | `plugin-chrome-web` | Interactive web authentication / session cookie bridge for community sites |

### Standard `EntityBus` Topics
- `entity.media.open` (`id`, `title`, `source_id`)
- `entity.text.open` (`id`, `title`)
- `entity.book.open` (`id`, `title`, `page`)
- `entity.vocab.add` (`word`, `book`)
- `entity.recommend.updated` (`mode`, `set_id`, `operation_id`, `domains`, `written_count`, `auto_focus`)
- `entity.music.playlist_synced` (`mode`, `playlist_id`, `playlist_name`, `added_count`, `verified_count`)
- `curation.job.progress` (`job_id`, `mode`, `stage`, `message`)
- `entity.ai.route_changed` (`harness`, `group`, `model`)
- `plugin.state_changed` (`plugin_id`, `loaded`)
- `plugin.fault` (`plugin_id`, `component`, `error`)
- `worker.fault` (`worker_id`, `plugin_id`, `signal_name`, `crash_count`, `auto_restarted`, `restart_succeeded`, `quarantined`, `error`)
- `plugin.abi_drift` (`count`, `stale_plugins`, `entries`)

---

## 5. Security, Lockfiles & Rollback

1. **Staging Build Isolation**: Clones and builds inside `<XDG_DATA_HOME>/omnidesk/staging/` with `-DOmniDeskSDK_DIR=...`. Previous active plugins remain untouched if configure/build/test/ABI checks fail.
2. **Cryptographic Pinning & ABI Drift Recovery**: Activated `.so` binaries are SHA-256 hashed and pinned in `<XDG_DATA_HOME>/omnidesk/upstreams.lock.json` along with git commit SHA and `sdk_abi_signature`. Run `omnidesk-pm abi-status [--strict]` to audit compatibility, and `omnidesk-pm rebuild [--stale | --all | <id>]` (or the UI «一键重编过期») to batch-rebuild plugins from source against the current SDK.
   Installation and full ABI audits always probe binaries in a separate, time-limited `omnidesk-pm abi-check` process, including disabled plugins. A missing checker fails verification; it never falls back to host-process `dlopen`. UI refresh uses recorded build signatures to stay responsive; actual loading and explicit CLI audits validate the binary. Rebuilding preserves enabled/disabled state.
3. **1-Click Rollback**: Up to 5 previous versions are snapshotted in `<XDG_DATA_HOME>/omnidesk/backups/<plugin_id>/`. `omnidesk-pm rollback <id>` or the UI «回滚» button restores cleanly.
4. **0600 Config & SecretVault**: Plain configuration lives in `<XDG_CONFIG_HOME>/omnidesk/plugins/<id>.json`; secrets marked `"type": "secret"` live separately in `<XDG_CONFIG_HOME>/omnidesk/vault.json` (`0600`) and are never printed in diagnostics.
