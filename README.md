# OmniDesk

**OmniDesk** 是一个基于 **微内核宿主 + 解耦插件市场** 架构的桌面内容工作台与 AI 协作中心，前端交互与视觉系统（Paper `#F6F5F3` 亮色纸感 & Graphite `#181718` 深色石墨、全键盘 Vim 导航、`F8` 响应式伴读栏）继承自 `native-wiliwili`，并针对以下三大内容生态与插件系统进行了独立 UI 设计：

1. **插件市场 (Plugin Marketplace)**：支持从 GitHub (`owner/repo` 简写 / Git URL) 或本地目录实时 CMake 编译、自检、子进程 ABI v1 握手、SHA-256 锁定、版本回滚、动态参数/密钥表单以及脚手架一键创建独立插件仓库。
2. **影视与音乐 (Media Workbench)**：16:9 影视卡片流、1:1 封面与双语 LRC 歌词对照工作室、飞牛 fnOS 私有影库视图、MPV 播放舞台与章节导航。
3. **文字 · 社区 · 科学期刊 (Text Workbench)**：博客、知乎、Linux.do、Reddit 与 arXiv/Nature/PubMed 专用高密度阅读排版，支持双语段落模式与可折叠嵌套评论树 (`CommentTreeView`)。
4. **书本与 PDF (Book Workbench)**：2:3 立体书架、BabelDOC PDF 版面双栏精译对照、EPUB 长难句/词根伴读（含 `media.clip_jump.v1` 影视原声切片跳转接口）及 SM-2 间隔重复生词记忆卡片。

> 详细插件接入协议、ABI v1 规范、`plugin.json` Schema、标准 Capability（如对接本机 `magpie` AI 路由和用户自配 `fnOS` NAS）见 [PLUGIN_SPEC.md](PLUGIN_SPEC.md)。

---

## 目录结构

- `sdk/`：`libomnidesk_sdk.so` 公共 SDK（导出单一 Borealis UI/字体/主题实例、`CapabilityRegistry`、`EntityBus`、`DomainRegistry`、`ConfigStore`、`SecretVault`、`PluginContext`、`IPlugin` 及 `OmniDesk::SDK` CMake 包）
- `pm/`：`omnidesk_pm` 与 `omnidesk-pm` CLI 插件市场管理器
- `host/`：`omnidesk` 桌面宿主应用与三大领域 UI + 插件市场界面
- `templates/plugin-starter/`：独立 C++17 `.so` 插件仓库起始模板
- `tests/`：SDK、Plugin Marketplace 与端到端跨仓库构建验证套件
- `sources/`：保留的上游参考资产（只读参考）

---

## 构建说明

> 提示：由于工作区所在文件系统空间有限，推荐始终将 CMake 构建目录放在 `/tmp/omnidesk-build`（`tmpfs`）。

```bash
/tmp/omnidesk-toolchain/bin/cmake -S . -B /tmp/omnidesk-build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release
/tmp/omnidesk-toolchain/bin/cmake --build /tmp/omnidesk-build --parallel
```

运行自动化测试：

```bash
ctest --test-dir /tmp/omnidesk-build --output-on-failure
bash tests/run_tests.sh /tmp/omnidesk-build
```

---

## 快速使用

### 启动桌面宿主
```bash
/tmp/omnidesk-build/omnidesk
# 或直接进入指定领域工作台：
/tmp/omnidesk-build/omnidesk --domain media
```

常用键盘快捷键：`Ctrl+1..9` 切换工作台、`Ctrl+Tab` 循环切换、`h/j/k/l` 移动焦点、`f` 全局角标直达、`F8` 开关 AI 伴读栏、`/` 搜索、`t` 切换 Paper/Graphite 主题、`?` 查看帮助。

### 使用 `omnidesk-pm` CLI 管理独立插件
```bash
# 浏览内置策划目录
/tmp/omnidesk-build/omnidesk-pm catalog

# 一键生成独立 C++17 .so 插件仓库骨架（可推送至独立 GitHub 仓库）
/tmp/omnidesk-build/omnidesk-pm init /path/to/omnidesk-plugin-mytool \
  --id plugin-mytool --domain core --kind provider --provides ai.agent.v1

# 从本地源码目录或 GitHub owner/repo 编译、验证并安装插件
/tmp/omnidesk-build/omnidesk-pm install /path/to/omnidesk-plugin-mytool
/tmp/omnidesk-build/omnidesk-pm install ZJFILOVE/omnidesk-plugin-magpie-agent

# 运行期生命周期验证 / 启停 / 更新 / 回滚 / 卸载
/tmp/omnidesk-build/omnidesk-pm verify plugin-mytool
/tmp/omnidesk-build/omnidesk-pm list
/tmp/omnidesk-build/omnidesk-pm rollback plugin-mytool
```
