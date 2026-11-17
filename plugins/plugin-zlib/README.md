# plugin-zlib

OmniDesk v2 `hybrid` (`domain=book`) plugin providing `book.search.v1` for Z-Library search, detail inspection, verified download, and handoff to BabelDOC, EPUB reader, or Feiniu NAS.

## Configuration

- `base_url` / `mirror_url`: Z-Library public endpoint or user-configured mirror/EAPI URL (`mirror_url` takes precedence). Only `http`/`https` are permitted; private/loopback addresses are blocked unless explicitly configured by the user.
- `download_root`: Required local directory for saving downloaded files (validated against path traversal and symlinks).
- `max_download_mb`: Maximum file size in MiB enforced pre-download (`Content-Length`) and during streaming.
- `api_key` / `remix_userid` / `cookie_header`: Stored exclusively in `0600` `SecretVault` (never written to plain config or logs). When `web.cookies.v1` is available, live Chrome cookies take precedence.

### 镜像切换

在书籍工作台「搜书 → 镜像与自定义地址」选择或输入镜像，应用后重新搜索。
`book.search.v1` 的 `mirrors` 动作读取候选列表与当前地址，
`configure_mirror` 动作保存 `mirror_url`，清除旧搜索/详情缓存。
切换域名时固定原 Vault 凭据所属 origin，不向新域名转发；Chrome Cookie 按域名获取。
EAPI 搜索使用 POST 表单和 `remix-userkey` 请求头。

2026-11-17 本机检测 `/eapi/info/domains`：`.gd`、`.gl`、`.website`、`.qa`
返回有效 JSON，`.sk` 入口重定向循环，`.fm` 证书过期。
这些是当次检测结果，不保证之后可访问或无需登录。
候选来源：[官方账号公告](https://mastodon.social/@Z_Lib_official/115982676684123606)、
[站点论坛公告](https://lib-talk.me/viewtopic.php?t=113)，并参考本地 zlib 项目 EAPI 实现。
