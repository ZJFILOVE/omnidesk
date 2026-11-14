# Antigravity 翻译

## 使用

1. 安装官方 Antigravity CLI，在终端运行 `agy` 完成登录。
2. 打开应用 **偏好设置 → 字幕文本翻译 → 翻译服务 → 本机 Antigravity**。
3. 点击 **刷新模型列表**，选择模型；思考等级包含在 CLI 返回的模型名称中。首次读取优先选择列表中的 Flash Low。
4. 点击 **测试翻译连接**，或开始视频 / 网页翻译。网页服务启动后修改模型或并发数，需要停止后重新启动。

模型来自本机 `agy models`，不写死模型清单。沿用 CLI 登录，不填写翻译 API Key，不修改用户的 CLI 配置。若 CLI 使用 Gemini API Key 提供方，请先在 CLI 中切回账号登录模式。

## 并发和生命周期

- 视频、网页共用现有 `TranslationPool`，默认 4 路，可选 1–8 路。
- 每个池成员独立创建 CLI 进程、临时工作目录和新会话，不使用 `--continue` 或 `--conversation`。同一成员内串行，不同成员可同时工作。
- 不积累前一批翻译的上下文；输入经 stdin NDJSON 传入，不放入进程参数。
- 普通字幕使用 `text` 字段对象、视频使用 `subtitles` 数组包装对象、网页使用精确节点 schema；读取 `structured_output`，避免将模型的说明或工具输出当成译文。对象包装兼容本机 CLI 的 schema 要求，交回视频模块时还原字幕数组。
- 视频结果仍按原字幕 ID / 时间轴回填；网页沿用原有结构校验。视频缓存包括 Antigravity 模型 ID，切换模型会使用不同缓存。
- 取消 / 超时会终止并回收私有进程组；后续请求重新创建会话。网页与视频复用共享限流退避，额度耗尽停止该池后续请求。
- 实时音频仍使用原有识别与顺序翻译队列；并发设置适用于视频与网页。

## 数据与工具

应用创建专用翻译 agent，仅声明 `finish` 完成工具，禁止调用业务工具的指令同时传入提示词，并禁用斜杠命令展开。不使用自动批准权限参数。CLI 初始化事件可能列出完整工具注册表，不能把该列表当作已经执行的工具记录。

Antigravity CLI 没有这里使用的 Codex `ephemeral` 接口。临时工作目录会被清理，但 **CLI 可能在自身目录保留会话历史**；这不属于应用实时字幕的内存缓存，需要按 Antigravity 自身方式管理。云端文字处理遵循当前登录服务规则。

## 验证

离线测试：`python3 scripts/test.py`。覆盖模型发现、字幕与网页适配、4 / 8 路独立工作目录、限流、额度、超时、取消、UI 选择保存与原有回归测试。

2026-09-15 本机真实验证：读取 14 个模型，使用 `gemini-3.8-flash-low` 同时翻译两份固定网页小样本和两份固定字幕小样本，4 个独立 conversation ID，约 52.8 秒全部完成；网页节点与字幕 ID 校验通过。8 路为离线并发测试，未额外消耗账号额度做 8 路真实调用。耗时仅代表这次小样本，随模型和账号负载变化。

参考：[官方 Headless Mode](https://antigravity.google/docs/cli/headless/)、[自定义 Agent](https://antigravity.google/docs/cli/commands/agents/)、[Agent 配置](https://antigravity.google/docs/subagents/)。本机接入使用 `agy --help` 和 `agy models` 核对的实际参数及模型 ID。
