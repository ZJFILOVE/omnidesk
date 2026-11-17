# plugin-pdf-babeldoc

OmniDesk v2 Stage 3 hybrid plugin (`domain="book"`, `kind="hybrid"`) providing:

- `book.source.v1` (`BookSource` + capability RPC): scans user-configured `pdf_library_roots` for real PDF files (`%PDF-` magic verified), extracts title/author/page count/file size, detects generated dual-column outputs, and serves bilingual `BookSpread` pages from sidecar JSON or page pairing (`needs_bilingual_pdf` when untranslated; honest placeholders when sidecar is missing).
- `book.bilingual_pdf.v1`: manages local BabelDOC CLI execution (`status`, `submit`, `job_status`, `cancel`, `list_jobs`) with strict path containment, `0600` atomic job files, `fcntl.flock` `operation_id` deduplication, process-group cancellation, glossary parameter checking (`unsupported_option` when unsupported), and `entity.book.open` bus emission on success.

## Configuration (`plugin.json`)

- `babeldoc_bin`: executable name on `PATH` or absolute path to `babeldoc`
- `pdf_library_roots`: allowed PDF library root directories
- `output_root`: allowed root directory for generated dual-column PDFs and sidecars
- `target_language`: default `--lang-out` (default `"zh"`)
- `timeout_sec`: subprocess timeout in seconds (default `600`)
