"""Error hierarchy and safety size/bounds constants for plugin-ebook."""

from __future__ import annotations

MAX_BOOK_FILE_BYTES = 128 * 1024 * 1024
MAX_TXT_FILE_BYTES = 32 * 1024 * 1024
MAX_SIDECAR_BYTES = 1 * 1024 * 1024
MAX_EPUB_ENTRIES = 2000
MAX_EPUB_ENTRY_BYTES = 16 * 1024 * 1024
MAX_EPUB_TOTAL_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_EPUB_COMPRESSION_RATIO = 100.0


class EbookPluginError(Exception):
    """Base error carrying a machine-readable error_code."""

    code: str = "internal_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        if code:
            self.code = code
        self.error_code = self.code
        super().__init__(message)


class EbookPathSafetyError(EbookPluginError):
    """Raised on path traversal, symlink escape, or Zip Slip violation."""

    code = "path_safety"


class BookNotFoundError(EbookPluginError, LookupError):
    """Raised when a requested book or vocabulary card does not exist."""

    code = "not_found"


class FormatUnavailableError(EbookPluginError):
    """Raised when a format requires an unavailable external converter (e.g. MOBI/AZW3)."""

    code = "format_unavailable"


class BookParseError(EbookPluginError):
    """Raised on malformed, oversized, or zip-bomb ebook files."""

    code = "parse_error"


class InvalidEbookArgError(EbookPluginError, ValueError):
    """Raised on invalid request parameters."""

    code = "invalid_argument"
