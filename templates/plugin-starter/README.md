# OmniDesk Plugin Starter

Minimal standalone C++17 `.so` plugin template for OmniDesk Marketplace.

## Quick Local Install

```bash
omnidesk-pm install .
omnidesk-pm verify plugin-starter
```

For a headless scraper/parser, add `"isolation": "subprocess"` and
`"worker_transport": "pipe"` to `plugin.json`. Its capabilities and domain
sources then run in `omnidesk-plugin-worker`. Custom UI views must stay in a
trusted in-process shell that delegates IO using `PluginContext::registerWorker`
and the worker capability/source helpers.

Build with `find_package(OmniDeskSDK REQUIRED)` and `OmniDesk::SDK` so the
generated SDK header signature is embedded in the plugin. After an SDK update,
use `omnidesk-pm abi-status --strict` and `omnidesk-pm rebuild --stale` to detect
and rebuild incompatible binaries.

## Publish as Standalone GitHub Repository

Each OmniDesk plugin lives in its own directory and GitHub repository. After pushing to GitHub, any user can install it directly inside the in-app **Plugin Marketplace** or via CLI:

```bash
omnidesk-pm install YOUR_GITHUB_USER/plugin-starter
```
