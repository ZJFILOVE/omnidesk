# 飞牛 NAS 影视自动化（已部署）

部署时间：2026-09-28。NAS：192.168.1.100（PN40）。

## 服务入口

| 服务 | 地址 | 用途 |
| --- | --- | --- |
| Transmission（原有原生应用） | http://192.168.1.100:9091 | 下载和做种 |
| Radarr | http://192.168.1.100:7878 | 添加、订阅电影 |
| Sonarr | http://192.168.1.100:8989 | 添加、追更电视剧 |
| Prowlarr | http://192.168.1.100:9696 | 管理和同步资源站 |
| 飞牛影视 | http://192.168.1.100:5666/v/ | 观看和海报墙 |

三个自动化服务的账号见同目录 `登录信息.txt`（权限 600）。Transmission 保留原账号密码。

## 已配置

- 重用原生 Transmission 4.0.5，无重复下载器，也未删除原有 qBittorrent、迅雷或其他容器。
- Radarr、Sonarr 对接 Transmission，类别分别为 `radarr`、`sonarr`。
- Prowlarr 对两个应用开启 Full Sync，自动同步索引器。
- 已测试的基础来源：YTS（电影）、The Pirate Bay、LimeTorrents。Knaben 因反复超时已停用；EZTV 因 Cloudflare 拦截未启用。
- 完成下载自动导入、电影/剧集重命名、硬链接入库。
- 三个 Web 服务开启登录验证；Docker 开机自启、日志轮转。
- 尚未添加具体影片或剧集订阅，需要用户指定片名。

## 目录

NAS 部署目录：`/vol1/1000/docker/media-stack`，在“我的文件 / docker / media-stack”中。

Transmission 共享目录真实位置：`/vol1/@appshare/transmission`。

| 内容 | NAS 实际目录 |
| --- | --- |
| 电影下载 | `/vol1/@appshare/transmission/complete/radarr` |
| 剧集下载 | `/vol1/@appshare/transmission/complete/sonarr` |
| 电影媒体库 | `/vol1/@appshare/transmission/media/movies` |
| 剧集媒体库 | `/vol1/@appshare/transmission/media/tv` |

Radarr/Sonarr 容器里统一映射为 `/var/apps/transmission/shares/transmission`，与原生 Transmission 返回的路径一致，不需要远程路径转换。

下载与媒体库共用同一文件系统、同一挂载，可硬链接，不占用两份数据。保持下载文件做种，媒体库中使用规范名称。请勿在入库后手工移动媒体库目录。

## 开始订阅

1. 电影：Radarr → Add New → 搜索片名 → 选媒体库 `.../media/movies`，质量可选 `HD-1080p` → Add & Search。
2. 美剧：Sonarr → Add New → 搜索剧名 → 选媒体库 `.../media/tv`，质量可选 `HD-1080p` → 选择要监控的季度/集数 → Add & Search。
3. 下载进度在各应用 Activity 或 Transmission 查看。索引器是否可用在 Prowlarr 查看。
4. 订阅只跟踪已添加作品；RSS 负责检测新发布资源，历史缺集需要执行搜索。

## 飞牛影视（已接入）

通过 NAS 账号单点登录完成接入，无需创建影视独立密码。

- **自动电影**：`/vol1/@appshare/transmission/media/movies/`
- **自动美剧**：`/vol1/@appshare/transmission/media/tv/`
- 新目录的影视应用读写权限已授权，状态正常。
- 文件变动自动扫描已启用；两个新媒体库的扫描请求均已成功执行。
- 原有“美剧”媒体库及 `/vol1/1000/serial/` 路径保持原样。
- 还未添加具体影片订阅，因此新媒体库目前为空。

在 Radarr 添加电影、Sonarr 添加剧集后，下载完成会自动整理到上述目录，飞牛影视随后扫描入库。

## 验证记录

- Radarr、Sonarr 的 Transmission 连接测试：通过。
- 电影、剧集元数据查询：通过。
- LimeTorrents 的美剧搜索返回 40 条结果。
- 三个服务的网页登录：均成功，未登录 API 返回 401。
- 三个自动化应用健康检查：无错误/警告（部署时）。
- 原生 Transmission 用户写入文件，Radarr 容器用户创建硬链接：成功；inode 相同，链接数 2。
- Ubuntu 官方种子传输：连接 24 个节点，下载 2,588,672 字节后结束；测试任务及测试文件已清理。
- Transmission 创建文件 → 硬链接入库 → 飞牛影视账号读取：通过。
- 飞牛影视目录授权、NAS 单点登录及两个媒体库扫描请求：通过。
- 尚未进行完整电影下载、真实影片刮削或播放测试。

## 维护

在 NAS 部署目录执行：

```bash
sudo docker compose ps
sudo docker compose logs --tail 100
```

更新前可先从三个应用的 System → Backup 创建配置备份，再执行：

```bash
sudo docker compose pull
sudo docker compose up -d
```

临时部署凭据文件已删除。运行所需的下载客户端凭据仍保存在 Radarr/Sonarr 配置库中。分享配置包时不包含登录信息、API 密钥或 appdata。


## qBittorrent 接入（2026-09-30）

- Web：<http://100.90.158.48:8085/>，用户名 `admin`，密码使用重装时设置的密码。
- 应用中心标记 4.6.11，运行 API 实测版本 v4.6.4。
- Sonarr、Radarr 均已添加 `qBittorrent (fnOS)`，主机 `192.168.1.100`，端口 `8085`，优先级 1。
- 原 Transmission 连接保留，优先级调整为 50，已有任务仍由原下载器处理。
- 分类 `sonarr`、`radarr` 分别下载到 `/var/apps/transmission/shares/transmission/complete/sonarr` 和 `.../radarr`，与现有容器挂载及媒体库兼容。
- 给 qBittorrent 系统用户 UID 989 添加了下载目录 ACL；修改前记录见 `qb-acl-backup.json`。
- 两个应用的下载器连接测试通过，健康检查为空。
- Ubuntu 官方种子测试收到 4,754,016 字节，瞬时速度约 501 KB/s，连接 17 个种子节点；测试任务和文件已删除。
- The Bear 原任务未迁移：Transmission 凭据在 API 中被掩码，无法自动暂停原任务，因此没有创建重复下载。
- 尚未验证完整影片下载后自动入库。
