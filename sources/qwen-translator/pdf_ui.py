"""Native PDF translation workspace, sharing the user's text model preferences."""
import json
from pathlib import Path
import shutil
import sys

from PyQt6.QtCore import QProcess, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QCheckBox, QLineEdit, QProgressBar, QVBoxLayout, QWidget

from desktop_ui import advanced_section, button, card, field, label, row, scroll_page
from file_dialogs import choose_file
from pdf_translation import CACHE, LANGUAGES, PDF_PYTHON, ROOT, validate_config
from ui_controls import WheelSafeComboBox
from worker_shutdown import stop_worker


class PdfPage(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.process = None
        self.buffer = b''
        self.result = None
        self.cancel_pending = False
        self.last_error = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll, layout = scroll_page()
        outer.addWidget(scroll)
        layout.addWidget(label('PDF 翻译', 'title'))
        layout.addWidget(label('保留原文档排版、公式与图表位置，一键生成双语对照或纯中文 PDF。', 'muted', True))
        source_card, col = card('1. 选择 PDF 文档', '在电脑本地解析版面，只将文字交给「偏好设置」中所选的翻译服务。扫描件请先进行文字识别 (OCR)。')
        self.source = QLineEdit(window.settings.value('pdf/source', ''))
        self.source.setPlaceholderText('选择或粘贴本地 PDF 文件路径')
        self.source.setAccessibleName('PDF 文件路径')
        select = button('选择 PDF 文件', self.choose_source)
        col.addLayout(row(self.source, select))
        self.lang_in, self.lang_out = WheelSafeComboBox(), WheelSafeComboBox()
        for code, title in LANGUAGES.items():
            self.lang_in.addItem(title, code)
            self.lang_out.addItem(title, code)
        self.lang_in.setCurrentIndex(self.lang_in.findData(window.settings.value('pdf/lang_in', 'en')))
        self.lang_out.setCurrentIndex(self.lang_out.findData(window.settings.value('pdf/lang_out', 'zh')))
        self.lang_in.setAccessibleName('PDF 源语言')
        self.lang_out.setAccessibleName('PDF 目标语言')
        col.addLayout(row(label('原文语言'), self.lang_in, label('翻译为'), self.lang_out))
        pdf_adv, pdf_adv_col = advanced_section('高级排版与页码设置', '指定翻译页码范围、切换交替页版式或开启术语统一')
        self.pages = QLineEdit()
        self.pages.setPlaceholderText('留空翻译全文，例如 1,3-5')
        field(pdf_adv_col, '页码范围', self.pages, '留空默认翻译整份文档；也可填如 1, 3-5。')
        self.alternating = QCheckBox('双语 PDF 使用原文、译文交替页（默认左右对照）')
        self.glossary = QCheckBox('自动提取并统一全文专业术语（适合长篇论文，会增加耗时）')
        pdf_adv_col.addWidget(self.alternating)
        pdf_adv_col.addWidget(self.glossary)
        col.addWidget(pdf_adv)
        layout.addWidget(source_card)
        task_card, col = card('2. 开始翻译', '自动使用「偏好设置」中选好的翻译服务与模型。')
        self.model = label('', 'secondary', True)
        col.addWidget(self.model)
        self.start_button = button('开始翻译 PDF', self.start, 'primary')
        self.cancel_button = button('取消任务', self.cancel)
        self.cancel_button.setEnabled(False)
        col.addLayout(row(self.start_button, self.cancel_button, button('切换翻译模型', lambda: window.navigate(3)), None))
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setAccessibleName('PDF 翻译进度')
        col.addWidget(self.progress)
        self.status = label('等待选择 PDF。', 'secondary', True)
        col.addWidget(self.status)
        col.addWidget(label('原文件保持原样，翻译完成的 PDF 与段落缓存保存在本机。', 'muted', True))
        layout.addWidget(task_card)
        results, col = card('3. 查看与保存结果')
        self.output = WheelSafeComboBox()
        self.output.setAccessibleName('PDF 输出版本')
        col.addWidget(self.output)
        self.open_button = button('打开 PDF', self.open_result, 'primary')
        self.save_button = button('另存 PDF…', self.save_result)
        col.addLayout(row(self.open_button, self.save_button,
                          button('打开结果目录', lambda: self.open_path(CACHE)), None))
        col.addWidget(label('生成包含「双语对照」和「纯译文」两个版本，可随时预览或另存。', 'muted', True))
        layout.addWidget(results)
        layout.addStretch()
        self.controls = [self.source, select, self.lang_in, self.lang_out, self.pages,
                         self.alternating, self.glossary, self.start_button]
        self.show_result(None)
        self.refresh_model()
        previous = window.settings.value('pdf/last_result', '')
        if previous:
            try:
                saved = json.loads(previous)
                if all(Path(saved[key]).is_file() for key in ('mono', 'dual')):
                    self.show_result(saved)
            except (ValueError, TypeError, KeyError, OSError):
                pass

    def refresh_model(self):
        config = self.window.translation_config()
        provider = config.get('text_provider', 'codex')
        key = {'codex': 'codex_model', 'antigravity': 'antigravity_model', 'api': 'text_model'}.get(provider)
        self.model.setText(f'当前模型：{config.get(key) or "尚未选择翻译模型"}')

    def showEvent(self, event):
        super().showEvent(event)
        if not self.process:
            self.refresh_model()

    def choose_source(self):
        path = choose_file(self, '选择 PDF', str(Path.home()), 'PDF 文档 (*.pdf)')
        if path:
            self.source.setText(path)

    def start(self):
        if self.process:
            return
        try:
            if not PDF_PYTHON.is_file():
                raise ValueError('请先安装 PDF 引擎：python3 scripts/setup_pdf.py --warmup')
            config = validate_config({**self.window.translation_config(), 'source': self.source.text().strip(),
                'lang_in': self.lang_in.currentData(), 'lang_out': self.lang_out.currentData(),
                'pages': self.pages.text(), 'alternating': self.alternating.isChecked(), 'glossary': self.glossary.isChecked()})
        except (ValueError, OSError) as error:
            self.status.setText(str(error))
            return
        for key in ('source', 'lang_in', 'lang_out'):
            self.window.settings.setValue('pdf/' + key, config[key])
        self.window.settings.sync()
        self.refresh_model()
        self.last_error = self.cancel_pending = False
        self.buffer = b''
        self.show_result(None)
        process = QProcess(self)
        self.process = process
        process.setProgram(str(PDF_PYTHON))
        process.setArguments([str(ROOT / 'pdf_worker.py')])
        process.setWorkingDirectory(str(ROOT))
        process.started.connect(lambda: process.write((json.dumps(config, ensure_ascii=False) + '\n').encode()))
        process.readyReadStandardOutput.connect(self.events)
        process.readyReadStandardError.connect(lambda: process.readAllStandardError())
        process.finished.connect(lambda code, status: self.finished(process, code))
        process.errorOccurred.connect(lambda error: self.failed(process, error))
        for control in self.controls:
            control.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setRange(0, 0)
        self.status.setText('正在启动 PDF 翻译…')
        process.start()

    def events(self):
        if not self.process:
            return
        self.buffer += bytes(self.process.readAllStandardOutput())
        while b'\n' in self.buffer:
            line, self.buffer = self.buffer.split(b'\n', 1)
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get('error'):
                self.last_error = True
                self.status.setText(event['error'])
            elif not self.cancel_pending:
                if event.get('message'):
                    self.status.setText(event['message'])
                if 'progress' in event:
                    self.progress.setRange(0, 100)
                    self.progress.setValue(int(event['progress']))
                if event.get('result'):
                    self.show_result(event['result'])
                    self.window.settings.setValue('pdf/last_result', json.dumps(self.result, ensure_ascii=False))
                    self.window.settings.sync()

    def failed(self, process, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.last_error = True
            self.status.setText('无法启动 PDF 引擎，请运行 python3 scripts/setup_pdf.py 重新安装。')
            self.finished(process, 1)

    def finished(self, process, code):
        if self.process is not process:
            return
        self.events()
        self.process = None
        self.cancel_button.setEnabled(False)
        self.progress.setRange(0, 100)
        for control in self.controls:
            control.setEnabled(True)
        if self.cancel_pending:
            self.status.setText('已取消 PDF 翻译，可重新开始并复用成功的段落缓存。')
        elif (code or not self.result) and not self.last_error:
            self.status.setText('PDF 任务未完成，请检查文件、模型和引擎依赖后重试。')
        if self.cancel_pending or self.last_error or code:
            self.show_result(None)
            self.progress.setValue(0)
        process.deleteLater()

    def cancel(self):
        if self.process and not self.cancel_pending:
            self.cancel_pending = True
            self.cancel_button.setEnabled(False)
            self.status.setText('正在取消并关闭翻译会话…')
            process = self.process
            stop_worker(process, lambda: self.process is process)

    def show_result(self, result):
        self.result = result
        self.output.clear()
        if result:
            self.output.addItem('双语对照 · ' + result['title'], result['dual'])
            self.output.addItem('纯译文 · ' + result['title'], result['mono'])
        self.open_button.setEnabled(bool(result))
        self.save_button.setEnabled(bool(result))

    def open_path(self, path):
        path = Path(path).expanduser().resolve()
        if sys.platform == 'linux' and path.suffix.lower() == '.pdf' and path.is_file():
            viewer = shutil.which('papers')
            if viewer:
                started, _ = QProcess.startDetached(viewer, [str(path)])
                if not started:
                    self.status.setText('无法启动 Papers 阅读器，请检查安装后重试。')
                return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def open_result(self):
        if self.output.currentData():
            self.open_path(self.output.currentData())

    def save_result(self):
        if not self.result:
            return
        kind = 'bilingual' if self.output.currentData() == self.result['dual'] else self.result['lang_out']
        suggested = Path.home() / 'Downloads' / (self.result['title'] + '.' + kind + '.pdf')
        path = choose_file(self, '另存 PDF', str(suggested), 'PDF 文档 (*.pdf)', save=True, suffix='pdf')
        if path:
            try:
                target, original = Path(path), Path(self.result['source'])
                if target.resolve() == original.resolve() or (target.exists() and original.exists() and target.samefile(original)):
                    raise ValueError('请选择其他文件名，不能覆盖原始 PDF。')
                shutil.copyfile(self.output.currentData(), path)
                self.status.setText('已保存：' + path)
            except (OSError, ValueError) as error:
                self.status.setText('无法保存 PDF：' + str(error))

    def shutdown(self):
        if self.process:
            self.cancel()
            return False
        return True
