"""Pydantic v2 capability envelopes, declarative UI specs, and domain model exports."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from pydantic.json_schema import SkipJsonSchema

from .domain_models import (
    BookBlockKind,
    BookFormat,
    BookItem,
    BookPageBlock,
    BookSpread,
    ChapterKind,
    ChapterSegment,
    CommentNode,
    ContentDomain,
    CurationMode,
    IsoDateStr,
    LyricLine,
    MediaItem,
    MediaKind,
    NonEmptyStr,
    PluginKind,
    TextArticle,
    TextChannel,
    TextFeedItem,
    TextParagraph,
    TextParagraphKind,
    VocabCard,
)


def _coerce_payload_mapping(
    data: Mapping[str, Any] | BaseModel | None,
    extra_data: dict[str, Any],
) -> dict[str, Any]:
    if data is None:
        payload: dict[str, Any] = {}
    elif isinstance(data, BaseModel):
        payload = data.model_dump()
    elif isinstance(data, Mapping):
        payload = dict(data)
    else:
        raise TypeError("CapabilityResult payload must be a mapping, BaseModel, or None")
    if extra_data:
        payload.update(extra_data)
    return payload


class _OkFactoryDescriptor:
    """Allows `CapabilityResult.ok(...)` as a class factory and `res.ok` as a bool field."""

    def __get__(self, instance: Any, owner: type[Any] | None = None) -> Any:
        if instance is None:
            cls = owner or CapabilityResult

            def _ok_factory(
                data: Mapping[str, Any] | BaseModel | None = None,
                *,
                provider_plugin_id: str = "",
                error: str = "",
                **extra_data: Any,
            ) -> CapabilityResult:
                payload = _coerce_payload_mapping(data, extra_data)
                return cls(
                    ok=True,
                    data=payload,
                    error=error,
                    provider_plugin_id=provider_plugin_id,
                )

            return _ok_factory
        return bool(instance.__dict__.get("ok", False))


class CapabilityResult(BaseModel):
    """Standard CapabilityRegistry invocation response envelope."""

    model_config = ConfigDict(
        validate_assignment=True,
        populate_by_name=True,
        from_attributes=True,
        ser_json_bytes="base64",
    )

    ok: bool = False
    data: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    provider_plugin_id: str = ""

    @classmethod
    def fail(
        cls,
        error: str,
        data: Mapping[str, Any] | BaseModel | None = None,
        *,
        error_code: str | None = None,
        provider_plugin_id: str = "",
        **extra_data: Any,
    ) -> CapabilityResult:
        """Build a failed CapabilityResult without mutating caller-supplied mappings."""
        payload = _coerce_payload_mapping(data, extra_data)
        if error_code:
            payload["error_code"] = str(error_code)
        return cls(
            ok=False,
            data=payload,
            error=error,
            provider_plugin_id=provider_plugin_id,
        )

    def with_provider(self, plugin_id: str) -> CapabilityResult:
        """Return a copy tagged with `plugin_id` when `provider_plugin_id` is unset."""
        if self.provider_plugin_id or not plugin_id:
            return self
        return self.model_copy(update={"provider_plugin_id": plugin_id})

    def to_dict(self) -> dict[str, Any]:
        """Serialize the envelope into a plain dictionary."""
        return self.model_dump()


CapabilityResult.ok = _OkFactoryDescriptor()  # type: ignore[assignment]


class WorkspaceTabSpec(BaseModel):
    """Declarative workspace tab registered by a plugin."""

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    tab_id: NonEmptyStr
    plugin_id: str = ""
    title: NonEmptyStr
    domain: ContentDomain = "core"
    badge: str = ""
    ui_schema: dict[str, Any] | list[dict[str, Any]] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("ui_schema", "schema", "descriptor"),
    )
    render: SkipJsonSchema[Callable[[], Any] | None] = Field(default=None, exclude=True)
    on_action: SkipJsonSchema[Callable[[str, dict[str, Any]], Any] | None] = Field(default=None, exclude=True)


class DockWidgetSpec(BaseModel):
    """Declarative F8 Companion Dock widget registered by a plugin."""

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    widget_id: NonEmptyStr
    plugin_id: str = ""
    title: NonEmptyStr
    supported_domains: list[ContentDomain] = Field(
        default_factory=lambda: ["core", "media", "text", "book"],
    )
    ui_schema: dict[str, Any] | list[dict[str, Any]] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("ui_schema", "schema", "descriptor"),
    )
    render: SkipJsonSchema[Callable[[], Any] | None] = Field(default=None, exclude=True)
    on_action: SkipJsonSchema[Callable[[str, dict[str, Any]], Any] | None] = Field(default=None, exclude=True)


class SubprocessResult(BaseModel):
    """Result returned by PluginContext.exec_subprocess."""

    model_config = ConfigDict(ser_json_bytes="base64", val_json_bytes="base64")

    returncode: int
    stdout: str = ""
    stderr: str = ""
    stdout_bytes: bytes = b""
    stderr_bytes: bytes = b""
    timed_out: bool = False
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.cancelled


__all__ = [
    "BookBlockKind",
    "BookFormat",
    "BookItem",
    "BookPageBlock",
    "BookSpread",
    "CapabilityResult",
    "ChapterKind",
    "ChapterSegment",
    "CommentNode",
    "ContentDomain",
    "CurationMode",
    "DockWidgetSpec",
    "IsoDateStr",
    "LyricLine",
    "MediaItem",
    "MediaKind",
    "NonEmptyStr",
    "PluginKind",
    "SubprocessResult",
    "TextArticle",
    "TextChannel",
    "TextFeedItem",
    "TextParagraph",
    "TextParagraphKind",
    "VocabCard",
    "WorkspaceTabSpec",
]
