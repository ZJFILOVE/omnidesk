# PDF 翻译

Qwen LiveTranslate 的“PDF 翻译”页面使用 BabelDOC 本地引擎。源 PDF 不会作为文件上传到陪读蛙或沉浸式翻译；解析、版面识别和重新排版在本机进行，文档文字、段落上下文和可选术语提取请求会发送给偏好设置中的翻译服务。

## 安装与使用

```bash
python3 scripts/setup_pdf.py --warmup
```

安装使用 `runtime/pdf-venv` 中独立的 Python 3.13 环境，避免影响现有语音、字幕和 Qt 依赖。`--warmup` 下载并由 BabelDOC 校验版面模型及字体，不调用翻译大模型。离线资源准备完成后，本地排版不需要在线服务；云端翻译仍需要网络。

1. 在“偏好设置”选择翻译服务、模型和并发数。例如选择已登录的 Codex 与 `gpt-6-luna`，无需另填 API Key；思考等级也沿用该设置。
2. 打开“PDF 翻译”（`Ctrl+8`），选择本地 PDF，设置源语言、目标语言与可选页码。页码按实际 PDF 页序从 1 开始，例如 `1,3-5`；未选中的页保留原文。
3. 按需启用自动术语提取，或把双语输出切换为原文／译文交替页。默认双语输出在同一张页面上左右对照。
4. 点击“翻译 PDF”。完成后可打开纯译文或双语 PDF，或另存到其他目录。原始 PDF 不能被“另存”覆盖。

Linux 下“打开 PDF”优先使用 Papers 阅读器（Poppler 后端），避免当前 Zathura／MuPDF 在部分论文图表的斜排字体上出现黑块。未安装 Papers 时使用系统默认 PDF 阅读器；其他系统沿用默认打开方式。“打开结果目录”仍使用系统文件管理器。

正文和术语提取均使用选定的同一个模型。翻译并发默认 4、最多 8；Codex 与 Antigravity 使用现有独立会话池、限流等待和取消机制。Codex 使用 ephemeral 会话，Antigravity CLI 可能保留自己的会话历史。兼容 API 支持现有 Chat Completions 和 Responses 两种配置；文档提示词与语音字幕提示词独立。

## 本地文件与取消

- `runtime/pdf-assets`：版面模型、字体、分词资源以及 BabelDOC 的段落译文缓存，可能包含文档原文和译文。清除此目录后首次使用需要重新下载资源。
- `pdf-cache/<任务 ID>`：完整的纯译文 `mono.pdf`、双语 `dual.pdf` 与结果元数据 `result.json`，不保存 API Key。
- 每次任务使用独立临时目录；正常失败或取消会清理未完成的结果，完成的段落缓存可供同一模型重试复用。异常断电或强制结束可能留下临时目录，可通过“打开结果目录”手动清理。
- 取消任务或关闭应用会停止该 PDF 的工作进程及模型子进程。成功生成的 PDF 会保留；重启应用会恢复最后一次成功的结果入口。

## 范围与限制

支持带可提取文字层的 PDF。纯扫描件需先做 OCR，加密文件需先解锁另存。公式、插图与版面由 BabelDOC 处理；复杂多栏、表格、混合扫描页仍应对照原文检查，不能保证所有页面完全一致。

模型请求失败、输出缺失或页数不完整不会显示为成功。上游引擎本身仍可能回退单个段落的格式处理，因此“完成”不代表人工级排版验收。

## 上游与版本

采用 [BabelDOC 0.6.4](https://github.com/funstory-ai/BabelDOC)，直接依赖见 `requirements-pdf.txt`，完整安装版本锁定于 `requirements-pdf.lock`。BabelDOC 提醒其 Python API 属于内部接口，所以升级时必须一起验证适配器、取消行为与真实 PDF 输出。

[PDFMathTranslate-next](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next) 是工作组维护的应用层和官方参考实现，核心同样调用 BabelDOC。2026-10-31 检查时其最新发布版为 [2.9.0](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/releases/tag/v2.9.0)，主要新增 DeepSeek V4 思考模式控制和 WebUI／限流参数改进。本项目复用自己的 Qt 界面和模型登录通道，直接适配固定版本引擎，不启动额外 WebUI。

BabelDOC 使用 AGPL-3.0，PyMuPDF 的许可证见其发行包。本项目没有复制上游实现，运行时依赖安装在独立环境；对外分发含这些依赖的产品时需保留对应许可并遵循其要求。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -p test_pdf_translation.py
QT_QPA_PLATFORM=offscreen /usr/bin/python3 -m unittest discover -s tests -p test_pdf_ui.py
```

常规测试使用模拟模型，不产生真实请求。可选的引擎集成测试需要已安装依赖和预热资源：

```bash
QWEN_PDF_ENGINE_TESTS=1 runtime/pdf-venv/bin/python -m unittest discover -s tests -p test_pdf_engine.py
```

2026-10-31 使用已登录的 Codex `gpt-6-luna`、low 思考等级，对含正文、表格、公式的一页合成文档完成真实验收（启用术语提取，共 3 次请求）。纯译文及左右双语 PDF 均生成成功；提取文本和渲染图检查了中文、表格单元格及公式保留。这是小型样例验证，不代表复杂论文或扫描件都能正确处理。
