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
- `config_schema[].type`: `"string"` | `"secret"` | `"bool"` | `"int"` | `"select"`
- `id`: must match `^[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}$`

---

## 3. Binary C ABI v1 & C++17 `IPlugin` Interface

Defined in `<omnidesk/plugin_abi.h>` and `<omnidesk/plugin.hpp>`:

- Every `.so` must export three unmangled `extern "C"` symbols via `OMNIDESK_DECLARE_PLUGIN(ClassName, id, name, version, kind, domain)`:
  - `const OmniDeskPluginInfo* omnidesk_plugin_abi_info(void)`
  - `void* omnidesk_plugin_create(void)` -> returns `static_cast<void*>(static_cast<omnidesk::IPlugin*>(new ClassName()))`
  - `void omnidesk_plugin_destroy(void* instance)`
- Before loading into the host process, `PluginInstaller` forks a child verification process (`omnidesk-pm abi-check <so> --expect-id <id>`) to guarantee the `.so` opens cleanly, reports `OMNIDESK_PLUGIN_ABI_VERSION == 1`, matches manifest `id`, and constructs/destructs safely.

### Scoped Cleanup Guarantee
When `onLoad(PluginContext& ctx)` registers capabilities (`ctx.registerCapability(...)`), subscribes to bus events (`ctx.subscribe(...)`), or adds domain sources/tabs/widgets (`ctx.registerMediaSource(...)`, `ctx.registerWorkspaceTab(...)`, etc.), `PluginManager::unloadPlugin()` automatically revokes **every** registration tagged with `plugin_id` on unload, hot-reload, update, or failure.

---

## 4. Standard Cross-Plugin Capability Contracts

Plugins collaborate strictly through `CapabilityRegistry` and `EntityBus`—never by linking directly against one another.

| Capability ID | Intended Provider | Key Operations & Purpose |
| :--- | :--- | :--- |
| `ai.agent.v1` | `plugin-magpie-agent` | `{"action":"chat","domain","context","prompt"}` -> contextual AI assistant response for `F8` Companion Dock |
| `llm.router.v1` | `plugin-magpie-agent` | `{"action":"current_route"}` / `{"action":"list_harnesses"}` / `{"action":"apply_route","harness","group","model"}` — reads local `magpie` state and pushes route choices back to `magpie` |
| `llm.chat.v1` | `plugin-magpie-agent` | General completion / translation / summarization consumed by Text & Book workbenches |
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
- `entity.ai.route_changed` (`harness`, `group`, `model`)
- `plugin.state_changed` (`plugin_id`, `loaded`)

---

## 5. Security, Lockfiles & Rollback

1. **Staging Build Isolation**: Clones and builds inside `<XDG_DATA_HOME>/omnidesk/staging/` with `-DOmniDeskSDK_DIR=...`. Previous active plugins remain untouched if configure/build/test/ABI checks fail.
2. **Cryptographic Pinning**: Activated `.so` binaries are SHA-256 hashed and pinned in `<XDG_DATA_HOME>/omnidesk/upstreams.lock.json` along with git commit SHA.
3. **1-Click Rollback**: Up to 5 previous versions are snapshotted in `<XDG_DATA_HOME>/omnidesk/backups/<plugin_id>/`. `omnidesk-pm rollback <id>` or the UI «回滚» button restores cleanly.
4. **0600 Config & SecretVault**: Plain configuration lives in `<XDG_CONFIG_HOME>/omnidesk/plugins/<id>.json`; secrets marked `"type": "secret"` live separately in `<XDG_CONFIG_HOME>/omnidesk/vault.json` (`0600`) and are never printed in diagnostics.
