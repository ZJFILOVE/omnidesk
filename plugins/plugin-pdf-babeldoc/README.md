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

## 扫描 PDF / MinerU

PDF 阅读区提供「上传当前页至 MinerU 解析」和「MinerU 设置」。复用学案工作台的
`mineru_api.py`（standard / vlm 的 submit → poll → download 协议），仅上传单页 PDF，
不在翻页或 Agent 读取时自动上传。失败后保留私有任务回执，重试继续已有任务。

设置支持 API Token（SecretVault，不回显）、已有 Token 文件路径、适配器和 Python
路径。默认使用 `~/Documents/GLM-OCR/.agent/skills/mineru-document-parser/scripts/mineru_api.py`
与 `~/Projects/qwen-translator/runtime/pdf-venv/bin/python`（需 PyMuPDF）。
本实现复用适配器协议，不修改学案工作台账号池或自动轮换其账号。

优先读取 `*_content_list.json`，支持平铺列表和按页嵌套列表；保存页码、段落 ID、
公式、图注和 bbox。JSON 格式未知或没有正文时回退单页 `full.md`，支持内联、引用式
及 HTML img 图片。图片只允许结果目录内 PNG/JPEG/WebP，读取后转为图片块；图注作为
独立文字块参与翻译，原图保留在两侧，不把图片 URL 交给模型重写。

解析按源 PDF SHA-256 + 页码缓存到插件 data_dir，`book.source.v1/fetch_spread`
直接返回解析正文和图片，因此现有 Agent 当前页上下文与翻译按钮均能读取。
源 PDF 改变后不会沿用旧缓存。此功能提供应用内双语阅读，不等同于从 MinerU JSON
直接重排导出 BabelDOC 双语 PDF；BabelDOC 原有导出任务保持独立。

能力动作：`book.bilingual_pdf.v1` 的 `mineru_settings`、`configure_mineru`、
`parse_page`（`book_id`、`page_number`、`upload_current_page: true`）。配置动作不上传 PDF。
