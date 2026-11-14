"""Desktop ownership and pairing for the Firefox page translator."""
import json
from pathlib import Path
import secrets

from PyQt6.QtCore import QProcess, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QApplication, QLineEdit, QVBoxLayout, QWidget

from desktop_ui import advanced_section, button, card, field, label, row, scroll_page

ROOT = Path(__file__).resolve().parent


class WebPage(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.process = None
        self.buffer = b''
        self.running_model = ''
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll, layout = scroll_page()
        outer.addWidget(scroll)
        layout.addWidget(label('网页翻译', 'title'))
        layout.addWidget(label('配合 Firefox 浏览器扩展，浏览外文网页时一键开启清晰的双语对照阅读。', 'muted', True))
        service, col = card('网页翻译服务', '开启后即可在浏览器中使用网页翻译，自动复用「偏好设置」里选好的模型。')
        self.status = label('服务未启动', 'secondary', True)
        col.addWidget(self.status)
        self.start_button = button('启动网页翻译', self.start, 'primary')
        self.stop_button = button('停止服务', self.stop)
        self.stop_button.setEnabled(False)
        col.addLayout(row(self.start_button, self.stop_button, button('切换翻译模型', lambda: window.navigate(3)), None))
        layout.addWidget(service)
        pairing, col = card('连接 Firefox 浏览器扩展', '首次使用时，复制下方连接口令并粘贴到浏览器扩展的设置中即可完成配对。')
        self.token = window.settings.value('web/token', '') or secrets.token_urlsafe(32)
        window.settings.setValue('web/token', self.token)
        window.settings.sync()
        self.token_field = QLineEdit(self.token)
        self.token_field.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_field.setReadOnly(True)
        field(col, '连接口令', self.token_field)
        col.addLayout(row(button('复制连接口令', self.copy_token), button('更换口令', self.rotate_token), None))
        layout.addWidget(pairing)
        install, col = advanced_section('扩展安装指南与隐私说明', '查看如何在 Firefox 中加载扩展及管理网页译文缓存')
        col.addWidget(label('1. 在 Firefox 打开 about:debugging#/runtime/this-firefox。\n'
            '2. 点击“临时载入附加组件”，选择扩展目录内的 manifest.json。\n'
            '3. 打开扩展设置，粘贴上方的连接口令并测试连接。\n'
            '4. 打开任意外文网页，点击扩展图标即可选择“双语翻译”或“仅译文”。', 'secondary', True))
        col.addLayout(row(button('打开扩展目录', lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(ROOT / 'firefox-extension')))), button('打开使用说明', lambda:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(ROOT / 'docs/WEB_TRANSLATION.zh-CN.md')))), None))
        col.addWidget(label('已翻译的网页段落默认缓存在浏览器本地，再次打开同一网页可直接读取；你也可以随时在扩展设置中清理缓存。', 'muted', True))
        layout.addWidget(install)
        layout.addStretch()

    def copy_token(self):
        QApplication.clipboard().setText(self.token)

    def rotate_token(self):
        self.stop()
        self.token = secrets.token_urlsafe(32)
        self.token_field.setText(self.token)
        self.window.settings.setValue('web/token', self.token)
        self.window.settings.sync()
        self.status.setText('已更换配对密钥，请重新复制到扩展并启动服务。')

    def start(self):
        if self.process is not None:
            return
        config = self.window.translation_config()
        provider = config.get('text_provider', 'codex')
        if provider not in {'codex', 'antigravity'}:
            self.status.setText('请在偏好设置中选择 Codex 或 Antigravity 后启动网页翻译。')
            return
        if provider == 'antigravity' and not config.get('antigravity_model'):
            self.status.setText('请在偏好设置中刷新并选择 Antigravity 模型。')
            return
        config = {key: config.get(key) for key in ('text_provider', 'codex_model', 'codex_reasoning_effort', 'antigravity_model', 'translation_concurrency')}
        config['token'] = self.token
        self.running_model = config['antigravity_model'] if provider == 'antigravity' else config.get('codex_model') or 'gpt-5.6-luna'
        process = QProcess(self)
        self.process = process
        self.buffer = b''
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.status.setText('正在启动网页翻译服务…')
        process.setProgram(str(ROOT / '.venv/bin/python'))
        process.setArguments([str(ROOT / 'web_translation.py')])
        process.setWorkingDirectory(str(ROOT))
        process.started.connect(lambda: process.write((json.dumps(config) + '\n').encode()))
        process.readyReadStandardOutput.connect(self.read_output)
        process.finished.connect(lambda code, status: self.finished(process, code))
        process.errorOccurred.connect(lambda error: self.failed(process, error))
        process.start()

    def read_output(self):
        if not self.process:
            return
        self.buffer += bytes(self.process.readAllStandardOutput())
        while b'\n' in self.buffer:
            line, self.buffer = self.buffer.split(b'\n', 1)
            try:
                if json.loads(line).get('ready'):
                    self.status.setText('网页翻译服务已启动 · ' + self.running_model + '\n正文分批翻译 · 127.0.0.1:8766')
            except (ValueError, AttributeError):
                pass

    def failed(self, process, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.finished(process, 1)

    def finished(self, process, code):
        if self.process is not process:
            return
        self.process = None
        process.deleteLater()
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.status.setText('服务已停止' if code == 0 else '服务未能运行，请检查依赖或 8766 端口是否被占用。')

    def stop(self):
        if self.process:
            self.stop_button.setEnabled(False)
            process = self.process
            process.closeWriteChannel()
            QTimer.singleShot(5000, lambda: self.force_stop(process))

    def force_stop(self, process):
        if self.process is process:
            process.kill()

    def shutdown(self):
        if self.process is None:
            return True
        self.stop()
        return False
