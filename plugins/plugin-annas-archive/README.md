# Anna’s Archive 文件索引适配器（0.3）

实现依据 Anna 官方站点及开源模板，独立于 Z-Library 的 EAPI 和账号模型。

- **搜索**：`/search?q=…&ext=…&lang=…&content=…&sort=…&page=…`。
  解析列表卡片的封面/标题/作者等相邻元素，兼容旧卡片和注释中的延迟结果。
  只将 32 位 MD5 文件记录作为可下载结果；MD5 查询直接读取文件详情。
  分页来自页面链接，不将“有结果”视为必然存在下一页。
  验证页、未知结构和无效 JSON 返回错误，不冒充空结果。
- **详情**：`/md5/<32位MD5>`。一个 MD5 是一个确定文件，不是多格式图书集合。
  `download_options` 分别列出 `fast`、`slow`、`external`、`direct` 来源。
  地址必须来自真实详情；不拼接猜测的 `/slow_download/<md5>/0/0`。
- **会员 API**：`/dyn/api/fast_download.json` 使用 `md5`、`key`，以及所选
  `path_index` / `domain_index`。Key 只存 SecretVault，只发送给此接口；
  不用于搜索请求头，不通过 Chrome 打开带 Key 的 URL，不跟随 API 重定向。
  解析 HTTP 400/401/403/429 中的 JSON 错误，包括会员与额度错误。
- **慢速下载**：识别页面的浏览器验证、`waitSeconds` 倒计时、普通或复制型文件链接。
  不跳过排队、不执行页面脚本；需要验证时可从界面打开 Chrome，完成后手动重试。
- **文件交付**：只接受详情列出的来源。外部来源由浏览器继续；合作下载主机
  不接收 Anna Cookie。原有大小上限、路径、重定向、魔数、压缩包检查保留，
  支持 Anna 常见扫描格式 DjVu；新增 MD5 一致性检查，全部通过后才原子保存文件。失败清除临时文件。

## 界面与能力

书籍工作台的 Anna 结果显示「查看文件与来源」，选择文件后显示元数据、MD5、
来源选项、验证入口和排队倒计时。下载校验成功后显示实际保存路径；选择阅读、翻译或送 NAS 时才调用对应插件，
不再创建虚构页数和双语内容的预览书籍。
「镜像与自定义地址」可打开当前 Anna 搜索页验证；插件独立页支持语言、格式、排序。

`book.search.v1` 支持 `search`、`detail`、`download`、`open_browser`、`mirrors`、
`configure_mirror`、`status` 和既有文件交付动作。`download` 要求 `item_id`（MD5）；
非会员需要选择详情返回的 `download_token`；会员可省略以使用自动服务器。
详情使用 `member_api_available` 表示 Key 已配置（不代表会员或额度验证成功）。

保留自定义 JSON 镜像的显式文件列表兼容，但它不是 Anna 官方搜索 API。
官方仅保证会员快速下载 JSON API；其他 JSON 格式不自动猜测为有效搜索结果。

## 配置与镜像

`base_url` / `mirror_url`：当前入口；`download_root`：下载目录；
`max_download_mb`：体积上限；`api_key`：会员 Secret Key；`cookie_header`：会话 Cookie。
Chrome Cookie 按域名读取；切换镜像固定原 Vault 凭据 origin，避免跨域转发。

2026-11-17 官方镜像清单为 `.gl`、`.pk`、`.gd`。
用户提供的 `.is` 保留为未核实入口；结构不符会明确报错。
本机官方镜像搜索仍可能返回 403，软件不能保证站点可访问。

## 实现依据

- [官方 FAQ：API、慢速下载与验证](https://annas-archive.gl/faq)
- [会员 API 自描述文档](https://annas-archive.gl/dyn/api/fast_download.json)
- [搜索卡片宏](https://software.annas-archive.gl/AnnaArchivist/annas-archive/-/blob/main/allthethings/templates/macros/aarecord_list.html)
- [文件详情模板](https://software.annas-archive.gl/AnnaArchivist/annas-archive/-/blob/main/allthethings/page/templates/page/aarecord.html)
- [合作下载模板](https://software.annas-archive.gl/AnnaArchivist/annas-archive/-/blob/main/allthethings/page/templates/page/partner_download.html)

测试用合成的官方结构片段验证协议，不依赖被拦截的线上搜索或会员账号。
MD5 下载测试使用实际测试文件的摘要，不把任意测试字节假装成正确文件。
