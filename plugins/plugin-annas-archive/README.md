# plugin-annas-archive

OmniDesk v2 `hybrid` (`domain=book`) plugin providing `book.search.v1` for Anna's Archive search, MD5 metadata detail inspection, verified download (fast API or mirror link), and handoff to BabelDOC, EPUB reader, or Feiniu NAS.

## Configuration

- `base_url` / `mirror_url`: Anna's Archive public endpoint or user-configured mirror (`mirror_url` takes precedence). Only `http`/`https` are permitted; private/loopback addresses are blocked unless explicitly configured by the user.
  The default is `https://annas-archive.gl`, listed by the [official FAQ](https://annas-archive.gl/faq) on 2026-11-17. Existing installations can set `mirror_url` to replace an unavailable `.org` endpoint. Browser verification may still be required.
- `download_root`: Required local directory for saving downloaded files (validated against path traversal and symlinks).
- `max_download_mb`: Maximum file size in MiB enforced pre-download (`Content-Length`) and during streaming.
- `api_key` / `cookie_header`: Stored exclusively in `0600` `SecretVault` (never written to plain config or logs). When `web.cookies.v1` is available, live Chrome cookies take precedence.

Search requests retry interrupted connections and timeouts through the headless
Chrome bridge. Certificate verification failures and unsafe redirects remain
terminal failures; TLS verification is never disabled.

书籍工作台支持 `.gl`、`.pk`、`.gd` 镜像选择与自定义地址。
用户提供的 `.is` 作为可手动选择的未核实入口保留；
截至 2026-11-17，[官方 FAQ](https://annas-archive.gl/faq) 未将其列入镜像清单。
切换域名会清空旧缓存，并阻止旧站点 Vault 凭据被转发到新域名。
