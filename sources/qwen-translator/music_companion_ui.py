"""Optional Waybar display preferences and lyric typography for MusicPage."""
from PyQt6.QtCore import QObject, QProcess, QTimer
from PyQt6.QtWidgets import QCheckBox, QFrame, QVBoxLayout, QWidget

from caption_style import resolve_appearance
from desktop_ui import Select, button, label, row
from waybar_theme import sync_theme


class MusicCompanion(QObject):
    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.settings = page.window.settings
        self._observed_process = None
        self._sent = None
        self._appearance = None
        self.section = QWidget(page)
        outer = QVBoxLayout(self.section)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        self.toggle = button('▸  Waybar 显示', role='disclosure')
        self.toggle.setCheckable(True)
        outer.addWidget(self.toggle)
        self.body = QFrame()
        self.body.setProperty('role', 'soft')
        layout = QVBoxLayout(self.body)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        self.display = Select()
        self.display.setAccessibleName('Waybar 歌词显示内容')
        for text, value in (('中文优先', 'translation'), ('只显示原文', 'original'),
                            ('原文与中文（双行）', 'bilingual')):
            self.display.addItem(text, value)
        stored = self.settings.value('music/waybar_display', 'translation')
        self.display.setCurrentIndex(max(0, self.display.findData(stored)))
        layout.addWidget(label('Waybar 左上角显示', 'secondary'))
        layout.addWidget(self.display)
        self.show_title = QCheckBox('显示歌曲名')
        self.show_progress = QCheckBox('显示播放进度')
        self.show_title.setChecked(self.settings.value('music/waybar_show_title', True, type=bool))
        self.show_progress.setChecked(self.settings.value('music/waybar_show_progress', True, type=bool))
        layout.addLayout(row(self.show_title, self.show_progress, None))
        layout.addWidget(label('只调整歌词文字；保留原鼠标操作。双行模式可能增加状态栏高度。', 'muted', True))
        outer.addWidget(self.body)
        self.body.hide()
        parent_layout = page.reading.parentWidget().layout()
        parent_layout.insertWidget(max(0, parent_layout.indexOf(page.reading)), self.section)
        self.toggle.toggled.connect(self.expand)
        self.display.currentIndexChanged.connect(self.preferences_changed)
        self.show_title.toggled.connect(self.preferences_changed)
        self.show_progress.toggled.connect(self.preferences_changed)
        self.worker_timer = QTimer(self)
        self.worker_timer.setInterval(200)
        self.worker_timer.timeout.connect(self.poll_worker)
        self.worker_timer.start()
        self.appearance_timer = QTimer(self)
        self.appearance_timer.setSingleShot(True)
        self.appearance_timer.setInterval(150)
        self.appearance_timer.timeout.connect(self.flush_appearance)
        appearance = getattr(page, 'appearance', None) or getattr(page, 'caption_appearance', None)
        if appearance is not None and hasattr(appearance, 'changed'):
            appearance.changed.connect(self.appearance_changed)
        self.poll_worker()

    def expand(self, expanded):
        self.body.setVisible(expanded)
        self.toggle.setText(('▾' if expanded else '▸') + '  Waybar 显示')

    def preferences(self):
        return {'display': self.display.currentData(), 'show_title': self.show_title.isChecked(),
                'show_progress': self.show_progress.isChecked()}

    def preferences_changed(self):
        options = self.preferences()
        self.settings.setValue('music/waybar_display', options['display'])
        self.settings.setValue('music/waybar_show_title', options['show_title'])
        self.settings.setValue('music/waybar_show_progress', options['show_progress'])
        self.settings.sync()
        self.send_preferences()

    def poll_worker(self):
        process = getattr(self.page, 'process', None)
        if process is not self._observed_process:
            self._observed_process = process
            self._sent = None
            if process is not None:
                process.started.connect(self.send_preferences)
        self.send_preferences()

    def send_preferences(self):
        process = getattr(self.page, 'process', None)
        if process is None or getattr(self.page, 'stopping', False) or getattr(self.page, 'closing', False):
            return
        try:
            if process.state() != QProcess.ProcessState.Running:
                return
        except RuntimeError:
            return
        options = self.preferences()
        sent = (process, options)
        if self._sent != sent:
            self.page.command('update_waybar', options)
            self._sent = sent

    def appearance_changed(self, options):
        if getattr(self.page.window, 'sync_waybar', False):
            self._appearance = dict(options)
            self.appearance_timer.start()

    def flush_appearance(self):
        if not getattr(self.page.window, 'sync_waybar', False) or self._appearance is None:
            return
        try:
            sync_theme(getattr(self.page.window, 'theme', 'dark'),
                       appearance=resolve_appearance(self._appearance))
            QProcess.startDetached('pkill', ['-SIGUSR2', '-x', 'waybar'])
        except (OSError, ValueError):
            if hasattr(self.page, 'error'):
                self.page.error.setText('歌词字体暂未同步到 Waybar，请稍后重试。')


def install_music_companion(page):
    """Install once and retain the controller for the lifetime of MusicPage."""
    existing = getattr(page, '_music_companion', None)
    if isinstance(existing, MusicCompanion):
        return existing
    controller = MusicCompanion(page)
    page._music_companion = controller
    return controller
