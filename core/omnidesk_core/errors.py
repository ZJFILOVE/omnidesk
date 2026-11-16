"""Core error hierarchy, standard error codes, and secret redaction helpers."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any

_BEARER_RE = re.compile(r"(?i)((?:Bearer|Authorization\s*:\s*(?:Basic|Token))\s+)[A-Za-z0-9._~+/=-]{6,}")
_SECRET_KEY_PATTERN = (
    r"[A-Za-z0-9_.-]*(?:api[_-]?(?:key|secret|token)|access[_-]?token|refresh[_-]?token|auth[_-]?token|"
    r"bearer[_-]?token|client[_-]?secret|session[_-]?secret|private[_-]?key|gateway[_-]?key|"
    r"cookie(?:_header)?|secret|token|password|passwd|passphrase|authorization)"
)
_JSON_KEY_VAL_RE = re.compile(
    rf"""(?i)(["']{_SECRET_KEY_PATTERN}["']\s*:\s*)(?:"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')"""
)
_KEY_VAL_RE = re.compile(
    rf"(?i)\b({_SECRET_KEY_PATTERN})\s*([=:])\s*"
    r"""(?!Bearer\s+\[REDACTED\]|Basic\s+\[REDACTED\]|Token\s+\[REDACTED\])"""
    r"""(?:"([^"\n]*)"|'([^'\n]*)'|([^\s,;'\"]+))"""
)
_KNOWN_PREFIX_RE = re.compile(r"\b(?:(?:sk|gh[pousr]|glpat|xox[baprs])-|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{8,}\b")
_SECRET_KEY_NAME_RE = re.compile(rf"(?i)\A{_SECRET_KEY_PATTERN}\Z")


class ErrorCode(StrEnum):
    """Canonical machine-readable error codes shared across core and plugins."""

    NOT_FOUND = "not_found"
    CAPABILITY_NOT_FOUND = "capability_not_found"
    INVALID_ARGUMENT = "invalid_argument"
    VALIDATION_ERROR = "validation_error"
    CONFLICT = "conflict"
    NOT_CONFIGURED = "not_configured"
    SERVICE_OFFLINE = "service_offline"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXCEEDED = "quota_exceeded"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    PERMISSION_DENIED = "permission_denied"
    PATH_SAFETY = "path_safety"
    PLUGIN_FAULT = "plugin_fault"
    INTERNAL_ERROR = "internal_error"


def _replace_kv_secret(match: re.Match[str]) -> str:
    key, sep, dq, sq, _ = match.groups()
    if dq is not None:
        return f'{key}{sep}"[REDACTED]"'
    if sq is not None:
        return f"{key}{sep}'[REDACTED]'"
    return f"{key}{sep}[REDACTED]"


def redact_secrets(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Scrub likely credentials and explicit secret strings from diagnostic text."""
    if not text:
        return ""
    scrubbed = text
    secrets = sorted(
        {secret for secret in extra_secrets if isinstance(secret, str) and len(secret) >= 4},
        key=len,
        reverse=True,
    )
    for secret in secrets:
        scrubbed = scrubbed.replace(secret, "[REDACTED]")
    scrubbed = _BEARER_RE.sub(r"\1[REDACTED]", scrubbed)
    scrubbed = _JSON_KEY_VAL_RE.sub(r'\1"[REDACTED]"', scrubbed)
    scrubbed = _KEY_VAL_RE.sub(_replace_kv_secret, scrubbed)
    scrubbed = _KNOWN_PREFIX_RE.sub("[REDACTED]", scrubbed)
    return scrubbed


def _redact_details(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, Mapping):
        return {
            k: ("[REDACTED]" if isinstance(k, str) and _SECRET_KEY_NAME_RE.match(k) and v is not None else _redact_details(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_details(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact_details(v) for v in value)
    return value


class OmniDeskError(Exception):
    """Base exception for all OmniDesk core runtime errors."""

    default_code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(
        self,
        message: str,
        *,
        code: str | ErrorCode | None = None,
        details: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.code: str = str(code if code is not None else self.default_code)
        self.message: str = message
        self.details: dict[str, Any] = dict(details) if details else {}
        self.cause: BaseException | None = cause
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        """Serialize error metadata without leaking sensitive values."""
        return {
            "code": self.code,
            "message": redact_secrets(self.message),
            "details": _redact_details(self.details),
        }


class ValidationError(OmniDeskError):
    """Raised when input data or schema validation fails."""

    default_code = ErrorCode.VALIDATION_ERROR


class InvalidArgumentError(ValidationError):
    """Raised when a caller supplies an invalid argument."""

    default_code = ErrorCode.INVALID_ARGUMENT


class PathSafetyError(OmniDeskError):
    """Raised when a path or identifier violates containment or traversal rules."""

    default_code = ErrorCode.PATH_SAFETY


class CapabilityError(OmniDeskError):
    """Base class for capability registration and invocation errors."""


class InvalidCapabilityIdError(CapabilityError):
    """Raised when a capability ID does not match the versioned `<ns>.<name>.v<n>` format."""

    default_code = ErrorCode.INVALID_ARGUMENT

    def __init__(self, capability_id: str, reason: str = "") -> None:
        self.capability_id = capability_id
        msg = f"Invalid versioned capability ID {capability_id!r}"
        if reason:
            msg = f"{msg}: {reason}"
        super().__init__(msg, details={"capability_id": capability_id})


class CapabilityNotFoundError(CapabilityError):
    """Raised when invoking or querying an unregistered capability."""

    default_code = ErrorCode.CAPABILITY_NOT_FOUND

    def __init__(self, capability_id: str) -> None:
        self.capability_id = capability_id
        super().__init__(
            f"Capability not found: {capability_id}",
            details={"capability_id": capability_id},
        )


class CapabilityConflictError(CapabilityError):
    """Raised when registering or revoking a capability owned by another entry."""

    default_code = ErrorCode.CONFLICT

    def __init__(
        self,
        capability_id: str,
        *,
        plugin_id: str = "",
        existing_plugin_id: str = "",
        message: str | None = None,
    ) -> None:
        self.capability_id = capability_id
        self.plugin_id = plugin_id
        self.existing_plugin_id = existing_plugin_id
        msg = message or (
            f"Capability {capability_id!r} is already registered"
            + (f" by plugin {existing_plugin_id!r}" if existing_plugin_id else "")
        )
        super().__init__(
            msg,
            details={
                "capability_id": capability_id,
                "plugin_id": plugin_id,
                "existing_plugin_id": existing_plugin_id,
            },
        )


class CapabilityInvocationError(CapabilityError):
    """Raised when a capability invocation fails due to an unhandled provider error."""

    default_code = ErrorCode.PLUGIN_FAULT

    def __init__(
        self,
        capability_id: str,
        plugin_id: str,
        message: str,
        *,
        code: str | ErrorCode = ErrorCode.PLUGIN_FAULT,
        cause: BaseException | None = None,
    ) -> None:
        self.capability_id = capability_id
        self.plugin_id = plugin_id
        super().__init__(
            message,
            code=code,
            details={"capability_id": capability_id, "plugin_id": plugin_id},
            cause=cause,
        )


class PluginError(OmniDeskError):
    """Base error associated with a specific plugin ID."""

    def __init__(
        self,
        message: str,
        *,
        plugin_id: str = "",
        code: str | ErrorCode | None = None,
        details: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.plugin_id = plugin_id
        merged = dict(details) if details else {}
        if plugin_id:
            merged.setdefault("plugin_id", plugin_id)
        super().__init__(message, code=code, details=merged, cause=cause)


class PluginNotFoundError(PluginError):
    """Raised when an operation references an unknown plugin."""

    default_code = ErrorCode.NOT_FOUND


class PluginManifestError(PluginError):
    """Raised when a plugin manifest fails schema or contract checks."""

    default_code = ErrorCode.VALIDATION_ERROR


class PluginFaultError(PluginError):
    """Raised when a plugin triggers an isolated runtime fault."""

    default_code = ErrorCode.PLUGIN_FAULT


class DomainSourceError(OmniDeskError):
    """Base error for domain data source registration and lookup."""

    def __init__(
        self,
        message: str,
        *,
        domain: str = "",
        source_id: str = "",
        code: str | ErrorCode | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.domain = domain
        self.source_id = source_id
        merged = dict(details) if details else {}
        if domain:
            merged.setdefault("domain", domain)
        if source_id:
            merged.setdefault("source_id", source_id)
        super().__init__(message, code=code, details=merged)


class DomainSourceNotFoundError(DomainSourceError):
    """Raised when a requested domain source is not registered."""

    default_code = ErrorCode.NOT_FOUND


class DomainSourceConflictError(DomainSourceError):
    """Raised when a domain source ID conflicts with an existing registration."""

    default_code = ErrorCode.CONFLICT


class JobError(OmniDeskError):
    """Base error for background job lifecycle operations."""

    def __init__(
        self,
        message: str,
        *,
        job_id: str = "",
        code: str | ErrorCode | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.job_id = job_id
        merged = dict(details) if details else {}
        if job_id:
            merged.setdefault("job_id", job_id)
        super().__init__(message, code=code, details=merged)


class JobNotFoundError(JobError):
    """Raised when referencing an unknown job ID."""

    default_code = ErrorCode.NOT_FOUND


class JobStateError(JobError):
    """Raised on an illegal job state transition."""

    default_code = ErrorCode.CONFLICT


class JobCancelledError(JobError):
    """Raised when an operation is aborted because its job was cancelled."""

    default_code = ErrorCode.CANCELLED


class ConfigError(OmniDeskError):
    """Raised on configuration store errors."""

    default_code = ErrorCode.VALIDATION_ERROR


class SecretVaultError(OmniDeskError):
    """Raised on secret vault access or permission errors."""

    default_code = ErrorCode.PERMISSION_DENIED
