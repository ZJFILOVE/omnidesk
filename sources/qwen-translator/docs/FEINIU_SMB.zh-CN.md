# 飞牛 Tailscale + SMB（2026-09-28 已配置）

电脑通过 Tailscale 地址 `100.90.158.48` 访问飞牛 PN40 的 SMB 3.1.1 服务。局域网和外网使用相同路径；外网性能仍取决于带宽、延迟和 Tailscale 能否直连。部署时实际为直连，Tailscale ping 约 1 ms。未新增公网端口转发。

## 日常入口

文件管理器书签：**飞牛（Tailscale SMB）**，路径 `/mnt/feiniu-smb`。

| 内容 | 本机路径 | NAS 实际路径 |
| --- | --- | --- |
| 旧剧集 | `/mnt/feiniu-smb/serial` | `/vol1/1000/serial` |
| 新电影库 | `/mnt/feiniu-smb/media-library/movies` | `/vol1/@appshare/transmission/media/movies` |
| 新剧集库 | `/mnt/feiniu-smb/media-library/tv` | `/vol1/@appshare/transmission/media/tv` |
| 共享文件 | `/mnt/feiniu-smb/共享` | `/vol1/1000/共享` |
| 部署配置 | `/mnt/feiniu-smb/docker (存储空间1)/media-stack` | `/vol1/1000/docker/media-stack` |

现有 14 个个人共享和新增 `media-library` 共 15 个均可访问，包括含中文和空格的共享名。原 `/mnt/feiniu` WebDAV 入口保留；新操作优先使用 SMB 入口。Daily5 归档脚本已切换到同一远端音乐目录的 SMB 路径，脚本和定时器名称保持兼容。

## 挂载与凭据

- `/etc/fstab` 的 `feiniu-tailscale-smb` 标记块管理 15 个共享。
- systemd 按需自动挂载，依赖 `tailscaled.service`；闲置 10 分钟卸载，再次访问自动挂载。`nofail` 避免 NAS 离线阻断启动，挂载超时 30 秒。
- 挂载使用 `vers=3.1.1`、`sec=ntlmssp`、`cache=strict`，本机映射为 `zhu` 的 UID/GID 1000。
- NAS 挂载凭据保存在 `/etc/feiniu-smb/credentials`，父目录 root 专用，文件 root 所有、权限 600。文档、命令行、日志不保存密码。
- 排障发现 NAS 的系统密码与 SMB 密码不一致。经用户明确同意，已将 `admin` 的 SMB 密码同步为当前飞牛系统密码；未修改飞牛系统密码。其他使用旧 SMB 密码的设备需更新登录凭据。

NAS 新共享定义位于 `/etc/samba/feiniu-media-library.conf`，由 `/etc/samba/smb.custom.conf` 引入。共享仅允许 `admin` 登录，客户端来源限定为 Tailscale IPv4 地址段和本机回环，禁用访客和符号链接跟随。只暴露既有 `media` 目录，没有移动视频或下载目录，也没有更改做种 / 硬链接布局。修改共享协议设置或升级 fnOS 后，可检查该自定义 include 是否仍在。

## 字幕程序

两项已验证映射及挂载身份已保存到 `QwenLiveTranslate/Desktop` 的 NAS 设置。服务地址使用 Tailscale：

- Radarr：`http://100.90.158.48:7878`，连接通过。
- Sonarr：`http://100.90.158.48:8989`，连接通过。

API Key 已保存到系统密钥库并回读核对，未写入普通配置文件。后台同步与自动处理仍关闭；当前两个服务的已入库文件数量均为 0。

## 已验证与边界

- 15 个共享的目录读取全部通过。
- 以普通用户身份，对 `serial`、新 `movies` / `tv` 目录完成创建、回读、防覆盖重命名、已有文件碰撞保护及替换测试。
- 在 SMB 写入后，通过 SSH 从 NAS 真实目录独立回读核对；测试文件已清理。
- direct CIFS 测试后已卸载，切换为 fstab/systemd 自动挂载，再次写回测试通过；已检查对 Tailscale 服务的启动依赖。
- 尚未实际重启电脑或切换到另一条外网复测；未进行真实影片翻译与飞牛影视播放验收。

WebDAV 的普通写入本身正常，但当前 davfs2/FUSE 不支持程序所需的 `renameat2(RENAME_NOREPLACE)`；此前“只读”的判断混入了运行沙箱限制。SMB 已通过该操作及同名文件保护测试。

## 备份与维护

本机：

- SMB 配置前的 fstab：`/etc/fstab.before-feiniu-smb-20260928-022155`
- 增加媒体共享前的 fstab：`/etc/fstab.before-media-library-20260928-022507`
- GTK 书签、Daily5 脚本和字幕设置文件旁均有带时间戳的 `before-…` 备份。

NAS：

- 同步密码前的认证数据库：`/var/lib/samba/private/passdb.tdb.before-feiniu-smb-20260928-022153`（权限 600）。
- 新增媒体共享前的配置：`/etc/samba/smb.custom.conf.before-media-library-20260928-022507`。

这些是部署时快照。以后若新增其他共享、改密码或改挂载，应按差异回退相关条目，避免直接覆盖后续配置。

在宿主机终端检查当前连接与挂载：

```bash
tailscale ping --c 2 100.90.158.48
ls /mnt/feiniu-smb/serial
findmnt -t cifs -o TARGET,SOURCE,FSTYPE
```

若在沙箱中诊断，先区分沙箱只读限制与宿主机挂载状态。参考：[Tailscale NAS 访问文档](https://tailscale.com/docs/use-cases/personal-or-at-home-use/access-nas-media-file-servers)、[Linux 防覆盖重命名说明](https://man7.org/linux/man-pages/man2/rename.2.html)。
