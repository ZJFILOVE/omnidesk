# 本机 Codex 接入

当前试用构建通过 `127.0.0.1:43188` 调用已登录的本机 Codex，指定模型为 `gpt-6-luna`。模型联网运行，并使用 Codex 账号的额度。无需创建 OpenAI API Key。

接口只接受带本机访问令牌的文本请求。Codex 登录凭据始终由 Codex 自己管理；每次请求关闭工具、文件读取与网页搜索，不保存会话文件。接口不会记录字幕、问题或回复。助手的总结和提问通过现有 OpenAI 兼容客户端调用。

流式连接在等待期间保活，Codex 完成后一次返回答案。Codex 不提供向量接口，广告识别沿用项目现有的词库与文本模型降级流程。

Firefox 内容脚本通过后台端口接收回复，使用 JavaScript 分块队列读取，避开原生 `ReadableStream` 跨上下文构造的权限错误（[Mozilla 1757836](https://bugzilla.mozilla.org/show_bug.cgi?id=1757836)）。

当前安装的配置在 `/home/zhu/.config/bili-helper-codex/config.json`（仅当前用户可读写），服务为 `bili-helper-codex.service`，已随用户登录启动。本机专用构建的 AI 设置与连接方案使用浏览器本地存储。

维护命令：

```sh
systemctl --user status bili-helper-codex.service
systemctl --user restart bili-helper-codex.service
systemctl --user disable --now bili-helper-codex.service
```

重新构建：

```sh
BILI_HELPER_CODEX_CONFIG=/home/zhu/.config/bili-helper-codex/config.json pnpm exec wxt build -b firefox
```

含本机配置的构建包仅用于本机，不要发布。公开源码不包含访问令牌。CLI 路径在本机配置里；Codex 更新后如果旧路径失效，更新该路径并重启服务。

协议与取消测试：`python3 -m unittest discover -s local-codex-bridge -p 'test_server.py'`。
完整真实推理测试：`BILI_HELPER_CODEX_LIVE_CONFIG=/home/zhu/.config/bili-helper-codex/config.json pnpm exec vitest run tests/local-codex-live.test.ts`。
