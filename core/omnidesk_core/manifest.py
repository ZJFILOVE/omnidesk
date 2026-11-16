"""Validation and loading for OmniDesk v2 plugin.json manifests (schema_version = 2)."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .errors import ErrorCode, PathSafetyError, PluginManifestError
from .paths import validate_plugin_id
from .registry import CAPABILITY_ID_RE

SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$")
ENTRY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")

PluginKind = Literal["provider", "workspace", "hybrid", "extension"]
PluginDomain = Literal["core", "media", "text", "book"]
ConfigFieldType = Literal["string", "secret", "bool", "int", "select"]


class RepositorySpec(BaseModel):
    """Optional external Git repository metadata for a plugin."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["git"] = "git"
    url: str = Field(min_length=1)
    branch: str = "main"

    @field_validator("url", "branch")
    @classmethod
    def _validate_non_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must be a non-empty string")
        return stripped


class ConfigFieldSpec(BaseModel):
    """Declarative config_schema field rendered in the UI and routed to ConfigStore/SecretVault."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    type: ConfigFieldType
    default: str | bool | int | float | None = None
    description: str = ""
    required: bool = False
    options: list[str] | None = None

    @field_validator("key", "label")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must be a non-empty string")
        return stripped

    @model_validator(mode="after")
    def _validate_select_options(self) -> ConfigFieldSpec:
        if self.type == "select":
            if not self.options or len(self.options) < 1:
                raise ValueError("config_schema field with type='select' requires non-empty options")
            if any(not isinstance(opt, str) or not opt.strip() for opt in self.options):
                raise ValueError("config_schema select options must be non-empty strings")
        return self


class PluginManifest(BaseModel):
    """Strict schema_version = 2 model matching docs/schemas/plugin-manifest.schema.json."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2]
    id: str = Field(min_length=1, max_length=96)
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    runtime: Literal["python"] = "python"
    entry: str = "plugin:create_plugin"
    kind: PluginKind
    domain: PluginDomain
    author: str = ""
    description: str = ""
    homepage: str | None = None
    repository: RepositorySpec | None = None
    python_dependencies: list[str] = Field(default_factory=list)
    verify_cmd: str = "uv run pytest -q"
    provides: list[str] = Field(default_factory=list)
    consumes: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    config_schema: list[ConfigFieldSpec] = Field(default_factory=list)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _strict_schema_version(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("schema_version must be integer 2")
        if value != 2:
            raise ValueError(f"Unsupported plugin.json schema_version={value!r}; expected 2")
        return value

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        try:
            return validate_plugin_id(value)
        except PathSafetyError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must be a non-empty string")
        return value

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        if not SEMVER_RE.match(value):
            raise ValueError(f"Invalid SemVer version {value!r}")
        return value

    @field_validator("entry")
    @classmethod
    def _validate_entry(cls, value: str) -> str:
        if not ENTRY_RE.match(value):
            raise ValueError(f"Invalid entry {value!r}: expected '<module>:<callable>'")
        return value

    @field_validator("homepage")
    @classmethod
    def _validate_homepage(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if not parsed.scheme or not (parsed.netloc or parsed.path):
            raise ValueError(f"Invalid homepage URI {value!r}")
        return value

    @field_validator("provides", "consumes")
    @classmethod
    def _validate_capability_list(cls, items: list[str]) -> list[str]:
        for cap_id in items:
            if not CAPABILITY_ID_RE.match(cap_id):
                raise ValueError(
                    f"Invalid versioned capability ID {cap_id!r}: expected '<ns>.<name>.v<n>'"
                )
        return items

    @field_validator("permissions")
    @classmethod
    def _validate_permissions(cls, items: list[str]) -> list[str]:
        for perm in items:
            if not perm.strip():
                raise ValueError("permission labels must be non-empty strings")
        return items

    @model_validator(mode="after")
    def _validate_unique_config_keys(self) -> PluginManifest:
        seen: set[str] = set()
        for field in self.config_schema:
            if field.key in seen:
                raise ValueError(f"Duplicate config_schema key {field.key!r}")
            seen.add(field.key)
        return self

    @property
    def entry_module(self) -> str:
        return self.entry.split(":", 1)[0]

    @property
    def entry_callable(self) -> str:
        return self.entry.split(":", 1)[1]

    def secret_keys(self) -> set[str]:
        """Return config_schema field keys declared with type='secret'."""
        return {field.key for field in self.config_schema if field.type == "secret"}

    def default_config(self) -> dict[str, Any]:
        """Return plain config defaults (never including secret fields)."""
        return {
            field.key: field.default
            for field in self.config_schema
            if field.type != "secret" and field.default is not None
        }


def resolve_manifest_path(path_or_dir: Path | str) -> Path:
    """Resolve a plugin directory or explicit plugin.json file path."""
    target = Path(path_or_dir).expanduser()
    return target / "plugin.json" if target.is_dir() else target


def validate_manifest(data: Mapping[str, Any] | PluginManifest) -> PluginManifest:
    """Validate a manifest mapping against schema_version = 2."""
    if isinstance(data, PluginManifest):
        return data
    if not isinstance(data, Mapping):
        raise PluginManifestError("Plugin manifest must be a JSON object")
    raw_id = data.get("id") if isinstance(data.get("id"), str) else ""
    try:
        return PluginManifest.model_validate(dict(data))
    except ValidationError as exc:
        raise PluginManifestError(
            f"Invalid plugin.json manifest: {exc}",
            plugin_id=raw_id,
            code=ErrorCode.VALIDATION_ERROR,
            details={"errors": exc.errors(include_url=False, include_context=False)},
            cause=exc,
        ) from exc


def load_manifest(path_or_dir: Path | str) -> PluginManifest:
    """Read and validate a plugin.json file from a directory or file path."""
    manifest_path = resolve_manifest_path(path_or_dir)
    if manifest_path.is_symlink():
        raise PluginManifestError(
            f"Refusing symlinked manifest: {manifest_path}",
            code=ErrorCode.PATH_SAFETY,
        )
    if not manifest_path.is_file():
        raise PluginManifestError(
            f"Missing plugin.json at {manifest_path}",
            code=ErrorCode.NOT_FOUND,
        )
    try:
        raw_text = manifest_path.read_text(encoding="utf-8")
        payload = json.loads(raw_text)
    except (OSError, json.JSONDecodeError) as exc:
        raise PluginManifestError(
            f"Failed to parse {manifest_path}: {exc}",
            code=ErrorCode.VALIDATION_ERROR,
            cause=exc,
        ) from exc
    return validate_manifest(payload)


def build_manifest_dict(
    *,
    plugin_id: str,
    name: str | None = None,
    version: str = "0.1.0",
    kind: PluginKind = "provider",
    domain: PluginDomain = "core",
    author: str = "OmniDesk",
    description: str = "",
    provides: Sequence[str] = (),
    consumes: Sequence[str] = (),
    permissions: Sequence[str] = (),
) -> dict[str, Any]:
    """Build and validate a canonical schema_version = 2 plugin.json dictionary."""
    display_name = name or plugin_id.replace("-", " ").replace("_", " ").title()
    manifest = validate_manifest(
        {
            "schema_version": 2,
            "id": plugin_id,
            "name": display_name,
            "version": version,
            "runtime": "python",
            "entry": "plugin:create_plugin",
            "kind": kind,
            "domain": domain,
            "author": author,
            "description": description or f"OmniDesk {domain} {kind} plugin.",
            "provides": list(provides),
            "consumes": list(consumes),
            "permissions": list(permissions),
            "verify_cmd": "uv run pytest -q",
            "config_schema": [],
        }
    )
    return manifest.model_dump(exclude_none=True)


__all__ = [
    "ConfigFieldSpec",
    "ConfigFieldType",
    "ENTRY_RE",
    "PluginDomain",
    "PluginKind",
    "PluginManifest",
    "RepositorySpec",
    "SEMVER_RE",
    "build_manifest_dict",
    "load_manifest",
    "resolve_manifest_path",
    "scaffold_plugin",
    "validate_manifest",
]


def scaffold_plugin(
    target_dir: Path | str,
    *,
    plugin_id: str | None = None,
    name: str | None = None,
    version: str = "0.1.0",
    kind: PluginKind = "provider",
    domain: PluginDomain = "core",
    author: str = "OmniDesk",
    description: str = "",
    provides: Sequence[str] = (),
    consumes: Sequence[str] = (),
    permissions: Sequence[str] = (),
    force: bool = False,
) -> Path:
    """Scaffold a valid schema_version = 2 pure Python plugin directory."""
    target = Path(target_dir).expanduser()
    pid = plugin_id or target.name
    manifest_data = build_manifest_dict(
        plugin_id=pid,
        name=name,
        version=version,
        kind=kind,
        domain=domain,
        author=author,
        description=description,
        provides=provides,
        consumes=consumes,
        permissions=permissions,
    )
    if (target / "plugin.json").exists() and not force:
        raise FileExistsError(f"Plugin already exists at {target}; pass force=True to overwrite")
    target.mkdir(parents=True, exist_ok=True)
    (target / "tests").mkdir(parents=True, exist_ok=True)
    (target / "plugin.json").write_text(json.dumps(manifest_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    class_name = "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", pid) if part) + "Plugin"
    cap_lines = "\n".join(
        f'        ctx.register_capability("{cap}", self._handle_default)' for cap in manifest_data["provides"]
    ) or "        pass"
    plugin_py = (
        f'"""OmniDesk v2 plugin: {manifest_data["name"]}."""\n\n'
        "from __future__ import annotations\n\nfrom typing import Any\n\n"
        "from omnidesk_sdk import BasePlugin, CapabilityResult, PluginContext\n\n\n"
        f"class {class_name}(BasePlugin):\n"
        "    async def on_load(self, ctx: PluginContext) -> None:\n"
        "        await super().on_load(ctx)\n"
        f"{cap_lines}\n\n"
        "    async def _handle_default(self, req: dict[str, Any]) -> CapabilityResult:\n"
        f'        return CapabilityResult.ok({{"plugin_id": "{pid}", "echo": req}})\n\n'
        "    async def on_health_check(self) -> dict[str, Any]:\n"
        f'        return {{"ok": True, "plugin_id": "{pid}"}}\n\n\n'
        f"def create_plugin() -> BasePlugin:\n    return {class_name}()\n"
    )
    (target / "plugin.py").write_text(plugin_py, encoding="utf-8")

    test_py = (
        "from __future__ import annotations\n\nimport asyncio\nfrom pathlib import Path\n\n"
        "from omnidesk_sdk import PluginContext\nfrom plugin import create_plugin\n\n\n"
        "def test_plugin_lifecycle() -> None:\n"
        "    async def _run() -> None:\n"
        "        plugin = create_plugin()\n"
        f'        ctx = PluginContext("{pid}", plugin_dir=Path(__file__).resolve().parents[1])\n'
        "        await plugin.on_load(ctx)\n"
        '        assert (await plugin.on_health_check()).get("ok") is True\n'
        "        await plugin.on_unload()\n"
        "        await ctx.acleanup()\n"
        "    asyncio.run(_run())\n"
    )
    (target / "tests" / "test_plugin.py").write_text(test_py, encoding="utf-8")
    (target / "README.md").write_text(f"# {manifest_data['name']}\n\n{manifest_data['description']}\n", encoding="utf-8")
    return target
