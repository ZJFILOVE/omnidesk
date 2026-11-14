#!/usr/bin/env python3
"""Build the local verification report from actual test evidence."""
import datetime
import html
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parent
def read(name): return json.loads((ROOT/'validation'/name).read_text())
def esc(value): return html.escape(str(value))
installed=read('install-results.json')
audit=read('audit-results.json')
ui=read('ui-results.json')
online=read('online-ui-results.json')
reader=read('reader-ui-results.json')
runtime=read('runtime-results.json')
transport=read('transport-cancel-results.json')
desktop=read('desktop-results.json')
all_checks=ui+online+reader+runtime+transport+desktop
assert all(row['result']=='pass' for row in all_checks),'A desktop check failed'
units=re.search(r'Ran (\d+) tests', (ROOT/'tests.log').read_text()).group(1)
assert (ROOT/'tests.log').read_text().rstrip().endswith('OK')
stamp=datetime.datetime.now().astimezone().isoformat(timespec='seconds')
def rows(checks):
    return ''.join('<tr><td>'+esc(row['check'])+'</td><td class="pass">通过</td></tr>' for row in checks)
sources=next(row['state'] for row in online if row['check']=='Real video metadata, danmaku and comments collected')
model_sample=next(row['state'] for row in ui if row['check']=='Real GPT-6 Luna detects English sponsor')
answer=reader[0]['state']['messages'][-1]['content']
images=[('online-native-panel.png','未登录真实 B 站视频：原生 AI 面板明确显示无字幕不可用'),
        ('native-chapters.png','测试字幕 + 真实 GPT-6 Luna：原生章节按钮与 libmpv 跳转'),
        ('native-answer-reader.png','真实模型问答；使用键盘 PageDown 阅读回答')]
gallery=''.join('<figure><a href="validation/'+file+'"><img src="validation/'+file+'" loading="lazy"></a><figcaption>'+esc(caption)+'</figcaption></figure>' for file,caption in images)
report=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>wiliwili AI 安装与验证报告</title>
<style>body{{margin:0;background:#f1f4f8;color:#1e293b;font:16px/1.75 system-ui,sans-serif}}main{{max-width:1060px;margin:36px auto;padding:30px;background:white;border-radius:14px}}h1{{font-size:30px;margin-top:0}}h2{{margin-top:36px;font-size:22px}}.badge{{color:#08663d;background:#e5f6ee;border-radius:8px;padding:10px 16px;display:inline-block}}.note{{background:#fff5dd;padding:18px;border-radius:8px}}code,pre{{background:#eef2f6;border-radius:5px;padding:3px 6px;overflow:auto}}pre{{padding:16px;white-space:pre-wrap}}table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #dce3eb;padding:9px 10px;text-align:left}}.pass{{color:#087146;white-space:nowrap}}img{{width:100%;border-radius:8px}}figure{{margin:24px 0}}figcaption{{color:#596579}}a{{color:#086cba}}.muted{{color:#64748b}}</style>
<main><h1>wiliwili AI 已构建、安装并验证</h1><p class="muted">记录时间：{esc(stamp)} · Linux 原生客户端 · wiliwili 1.6.0 / bili-helper 客户端流程 2.11.0.5</p>
<p class="badge">{units} 项规则测试通过 · {len(all_checks)} 项原生桌面检查通过 · 安装文件与已构建版本一致</p>
<p>已安装原生 Borealis / NanoVG 面板，使用 libmpv 播放与跳转；本机服务承接视频资料采集、广告识别、总结、章节与多轮提问。模型使用现有已登录的本机 Codex <code>gpt-6-luna</code>，对话接口为 <code>http://127.0.0.1:43188/v1</code>。令牌只在运行时读取，未写入报告或日志；没有 embeddings 请求或全局模型配置修改。</p>
<h2>启动与键盘</h2><pre>~/.local/bin/wiliwili-ai</pre><p>应用菜单：<b>wiliwili AI</b>。F8 打开面板；Tab / Shift+Tab、方向键导航，Enter 操作，Esc 返回，PageUp / PageDown 阅读。F6 总结、F7 提问、F9 跳过、F10 想看、Ctrl+F10 不是广告、Ctrl+M 补录区间、Ctrl+Shift+M 取消补录、Ctrl+G 自动跳过、Ctrl+Shift+A 功能开关。Ctrl+L 打开 BV / 链接；Ctrl+PageDown / Ctrl+PageUp 换 P。完整说明见 <a href="README.md">README</a>。</p>
<h2>真实与模拟的验证范围</h2><table><tr><th>方式</th><th>实际执行</th><th>资料与限制</th></tr>
<tr><td>原生桌面 + 真实模型</td><td>已安装启动器、真实 Wayland 键盘事件、原生输入框、GPT-6 Luna、libmpv 播放 / 章节跳转 / 自动跳过 / 撤回；人工纠错持久化；请求中换视频、同 BV 换 cid / P、关闭与退出。</td><td>视频是本地 200 秒测试媒体，字幕、弹幕、评论与分 P 身份为明确的测试样例。真实广告语义识别只验证已知英文赞助样例，没有测真实视频广告召回率。</td></tr>
<tr><td>真实 B 站网络与播放器</td><td>公共视频播放、暂停、5 秒跳转、Ctrl+L 切换到第二个真实视频并播放；元数据、简介、UP、标签、弹幕与评论采集；无字幕状态展示。</td><td>保留独立登录，未导入 Zen。此次字幕接口均未返回可用字幕；已登录 B 站字幕采集与真实视频字幕总结 / 广告判断尚未实测。</td></tr>
<tr><td>规则单元测试</td><td>{units} 项：连续分批、未知品牌、弹幕仅旁证、180 秒门槛、独立置信度、章节补区间、人工优先、缓存身份、失败不缓存、迟到结果、重试与独立登录资料重新读取等。</td><td>模型返回与部分网络数据使用模拟样例。低置信度及错误返回等边界属于模拟验证。</td></tr></table>
<div class="note"><b>原版兼容范围：</b>沿用已有验证的客户端流程和广告规则；原版私有服务器的核心识别算法没有还原，不能宣称完全复刻。没有可用字幕时不会缓存为确认无广告，也不会依据弹幕自动跳过；仍可用简介、弹幕、评论提问或人工补录。</div>
<h2>模型与真实资料证据</h2><p>真实模型识别测试结果：{esc(json.dumps(model_sample['ads'],ensure_ascii=False))}</p>
<p>原生真实视频：<code>{esc(sources['video']['bvid'])}</code> / cid <code>{esc(sources['video']['cid'])}</code>；本次原生采集数：{esc(json.dumps(sources['sources'],ensure_ascii=False))}。直接公共接口复测另见 <a href="validation/online-results.json">网络采集记录</a>。</p>
<p>真实模型的原生输入框问答：</p><pre>{esc(answer)}</pre>
<h2>原生桌面回归</h2><table><tr><th>检查</th><th>结果</th></tr>{rows(ui)}</table>
<h2>真实视频回归</h2><table><tr><th>检查</th><th>结果</th></tr>{rows(online)}</table>
<h2>长内容阅读</h2><table><tr><th>检查</th><th>结果</th></tr>{rows(reader)}</table>{gallery}
<h2>实际模型子进程取消</h2><p>通过本机桥接进程的子进程状态确认：请求取消到达真实 Codex CLI，旧子进程在 5 秒内退出。此处使用测试视频资料，CLI、HTTP 连接和原生客户端是真实运行。</p><table><tr><th>检查</th><th>结果</th></tr>{rows(transport)}</table>
<h2>备用与服务回收</h2><table><tr><th>检查</th><th>结果</th></tr>{rows(runtime+desktop)}</table>
<h2>安装与备用客户端</h2><pre>{esc(json.dumps(installed,ensure_ascii=False,indent=2))}</pre>
<p>任务开始时未检索到旧 wiliwili 可执行程序、包、Flatpak、源码或登录配置，因此没有可备份的旧安装。此次先编译未改动的官方 1.6.0 并保存为备用，可执行 <code>~/.local/bin/wiliwili-original</code>。后续安装修复时也已备份每个被覆盖的改造版目录、启动器和桌面文件。</p>
<h2>源码与完整性</h2><p>官方上游提交 <code>88e5876bea9502d06f46a8656e3530684d3aaf7d</code>，C++17，Borealis / NanoVG，SDL2，libmpv。来源：<a href="https://github.com/xfangfang/wiliwili">官方仓库</a>。完整检出在 <code>native-wiliwili/upstream</code>；可重放补丁为 <a href="integration.patch">integration.patch</a> 与 <a href="borealis-keyboard.patch">borealis-keyboard.patch</a>，AI 原生组件为 <a href="ai_helper.cpp">ai_helper.cpp</a> / <a href="ai_helper.hpp">ai_helper.hpp</a>，本机服务为 <a href="service.py">service.py</a>。</p>
<pre>{esc(json.dumps(audit,ensure_ascii=False,indent=2))}</pre><p>抽查原助手 {audit['preexisting_files_checked']} 个重点文件哈希不变；实现写入新建的 native-wiliwili 目录及独立安装 / 配置目录。未修改 Zen 扩展或网页屏蔽设置。上游新增代码的空白检查通过（按上游 CRLF 文件约定），运行库全部解析成功。</p>
<p class="muted">原始证据：<a href="validation/ui-results.json">原生回归 JSON</a> · <a href="validation/online-ui-results.json">真实播放 JSON</a> · <a href="validation/reader-ui-results.json">阅读 JSON</a> · <a href="validation/transport-cancel-results.json">真实模型取消</a> · <a href="tests.log">单元测试日志</a> · <a href="validation/audit-results.json">安装核验</a></p></main></html>'''
(ROOT/'report.html').write_text(report)
print(str(ROOT/'report.html'))
