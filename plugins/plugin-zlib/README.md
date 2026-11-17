# plugin-zlib

OmniDesk v2 `hybrid` (`domain=book`) plugin providing `book.search.v1` for Z-Library search, detail inspection, verified download, and handoff to BabelDOC, EPUB reader, or Feiniu NAS.

## Configuration

- `base_url` / `mirror_url`: Z-Library public endpoint or user-configured mirror/EAPI URL (`mirror_url` takes precedence). Only `http`/`https` are permitted; private/loopback addresses are blocked unless explicitly configured by the user.
- `download_root`: Required local directory for saving downloaded files (validated against path traversal and symlinks).
- `max_download_mb`: Maximum file size in MiB enforced pre-download (`Content-Length`) and during streaming.
- `api_key` / `remix_userid` / `cookie_header`: Stored exclusively in `0600` `SecretVault` (never written to plain config or logs). When `web.cookies.v1` is available, live Chrome cookies take precedence.
