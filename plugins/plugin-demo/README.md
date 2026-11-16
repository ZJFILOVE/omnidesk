# plugin-demo

OmniDesk v2 Zero-ABI pure-Python reference plugin (`schema_version = 2`).

## Provided Surface

- **Capability**: `demo.echo.v1`
  - Request: `{"message": "hello"}` (also accepts `text` or `value`)
  - Response `data`: `{"echo": "echo: hello", "message": "hello", "payload": {...}, "count": 1, "plugin_id": "plugin-demo"}`
- **Declarative Workspace Tab**: `demo-echo` (`domain = "core"`)
  - Exposes `Section`, `Markdown`, `KeyValue`, `Form`, `ActionRow`, and `List` descriptors via `WorkspaceTabSpec` and handles `echo` / `reset` actions.
- **Configuration (`config_schema`)**:
  - `prefix` (`string`, default `"echo"`)
  - `uppercase` (`bool`, default `false`)

## Offline Tests

Run from the repository root (with `PYTHONPATH=.`):

```bash
PYTHONPATH=. uv run --offline --with pytest --with pytest-asyncio --with pydantic pytest -q plugins/plugin-demo/tests/test_plugin.py
```
