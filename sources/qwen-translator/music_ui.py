"""Apple Music companion page; player and model work stay in a worker process."""
from html import escape
from collections import OrderedDict
import json
from pathlib import Path
import shutil

from PyQt6.QtCore import QEvent, QPointF, QProcess, QRect, QRectF, Qt, QTimer, QUrl
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QFontMetrics, QImage, QPainter, QPalette, QPen, QPixmap, QTextDocument, QTextLayout, QTextOption
from PyQt6.QtWidgets import QCheckBox, QGraphicsBlurEffect, QGraphicsScene, QHBoxLayout, QSlider, QTextBrowser, QVBoxLayout, QWidget

from desktop_ui import Select, button, card, label, row, scroll_page
from file_dialogs import choose_file
from caption_settings import appearance_dialog
from caption_style import CaptionAppearance, color_css, resolve_appearance
from music_caption_window import LyricsCaptionWindow, lyric_pair

ROOT = Path(__file__).resolve().parent


def clock_text(seconds):
    seconds = max(0, int(seconds or 0))
    return f'{seconds // 60}:{seconds % 60:02}'


class MusicPage(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.process = None
        self.buffer = b''
        self.state = {}
        self.render_key = None
        self.stopping = False
        self.restarting = False
        self.closing = False
        self.caption_window = None
        self._lyric_hover_index = -1
        self._lyric_images = OrderedDict()
        self._lyric_image_sizes = OrderedDict()
        self._lyric_image_bytes = 0
        self._lyric_resource_count = 0
        self._rendered_image_lines = None
        self._rendering_lyrics = False
        self._image_refresh_timer = QTimer(self)
        self._image_refresh_timer.setSingleShot(True)
        self._image_refresh_timer.timeout.connect(lambda: self.render_lyrics(force=True, keep_scroll=True))
        if not window.settings.contains('music_caption_size'):
            window.settings.setValue('music_caption_size', 24)
        if not window.settings.contains('music_caption_translation_first'):
            window.settings.setValue('music_caption_translation_first', True)
        self.caption_appearance = CaptionAppearance(window.settings, prefix='music_caption_')
        self._last_appearance = self.caption_appearance.options
        self._reading_preset = window.settings.value('music_caption_reading_preset', False, type=bool)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll, layout = scroll_page()
        outer.addWidget(scroll)
        layout.addWidget(label('音乐与歌词', 'title'))
        layout.addWidget(label('Apple Music 开始播放后，自动同步歌曲和双语歌词；中文翻译可选自动或手动。', 'muted', True))
        now, col = card('正在播放', '音乐继续由 Apple Music 播放；这里只保存歌词。', icon=8)
        self.title = label('等待 Apple Music 播放', 'section', True)
        self.artist = label('打开已安装的 Apple Music（Adam），播放任意歌曲即可连接。', 'muted', True)
        col.addWidget(self.title)
        col.addWidget(self.artist)
        self.previous = button('上一首', lambda: self.command('previous'))
        self.play = button('播放', lambda: self.command('play_pause'), 'primary')
        self.next = button('下一首', lambda: self.command('next'))
        col.addLayout(row(self.previous, self.play, self.next, None, button('打开 Apple Music', self.open_player)))
        self.progress = QSlider(Qt.Orientation.Horizontal)
        self.progress.setSingleStep(5000)
        self.progress.setPageStep(15000)
        self.progress.setAccessibleName('Apple Music 播放进度')
        self.progress.sliderReleased.connect(lambda: self.command('seek', self.progress.value() / 1000))
        self.progress.actionTriggered.connect(lambda _: QTimer.singleShot(0, self.keyboard_seek))
        col.addWidget(self.progress)
        self.time = label('0:00 / 0:00', 'muted')
        col.addWidget(self.time)
        audio = QHBoxLayout()
        audio.addWidget(label('音量', 'secondary'))
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setMaximumWidth(180)
        self.volume.setAccessibleName('Apple Music 音量')
        self.volume.sliderReleased.connect(lambda: self.command('volume', self.volume.value() / 100))
        self.volume.actionTriggered.connect(lambda _: QTimer.singleShot(0, self.keyboard_volume))
        audio.addWidget(self.volume)
        audio.addWidget(label('播放设备', 'secondary'))
        self.devices = Select()
        self.devices.setMinimumWidth(150)
        self.devices.setAccessibleName('Apple Music 播放设备')
        self.devices.activated.connect(lambda _: self.command('device', self.devices.currentData()))
        audio.addWidget(self.devices, 1)
        col.addLayout(audio)
        self.status = label('连接尚未启动', 'muted', True)
        self.error = label('', 'secondary', True)
        col.addWidget(self.status)
        col.addWidget(self.error)
        layout.addWidget(now)
        lyrics, col = card('双语歌词', '优先 Apple Music 自带中文与本地缓存，缺少的部分只翻译一次。', icon=8)
        self.lyric_status = label('播放后自动读取', 'muted', True)
        col.addWidget(self.lyric_status)
        self.reading = QTextBrowser()
        self.reading.setOpenLinks(False)
        self.reading.setMinimumHeight(270)
        self.reading.setAccessibleName('原文与中文歌词，点击有时间轴的行可跳转播放')
        self.reading.anchorClicked.connect(self.lyric_clicked)
        self.reading.viewport().setMouseTracking(True)
        self.reading.viewport().installEventFilter(self)
        self.reading.verticalScrollBar().valueChanged.connect(self.schedule_lyric_image_refresh)
        col.addWidget(self.reading)
        self.follow = QCheckBox('跟随当前歌词')
        self.follow.setChecked(True)
        self.follow.toggled.connect(lambda _: self.render_lyrics(force=True))
        col.addWidget(self.follow)
        self.translate_button = button('翻译中文', lambda: self.command('translate_lyrics'), 'primary')
        self.retry_button = button('重新读取', lambda: self.command('retry_lyrics'))
        self.import_button = button('导入歌词', self.import_lyrics)
        col.addLayout(row(self.translate_button, self.retry_button, self.import_button, None))
        self.caption_button = button('打开悬浮歌词', self.toggle_caption_window)
        col.addLayout(row(button('打开歌词文件', self.open_cache), self.caption_button, None))
        self.click_through = QCheckBox('悬浮歌词鼠标穿透（操作后面的窗口）')
        self.click_through.setChecked(self.caption_appearance.options['click_through'])
        self.click_through.setToolTip('开启后隐藏悬浮工具栏；在这里取消勾选，即可恢复拖动与外观操作。')
        self.click_through.toggled.connect(lambda value: self.caption_appearance.update(click_through=value))
        col.addWidget(self.click_through)
        layout.addWidget(lyrics)
        appearance_card, col = card('歌词外观', '页内歌词与悬浮歌词共用设置；实时翻译字幕使用独立设置。', icon=9)
        self.appearance_toggle = button('外观与预设…')
        self.appearance_toggle.setCheckable(True)
        self.reset_appearance_button = button('恢复默认', self.caption_appearance.reset)
        self.reset_appearance_button.setToolTip('恢复默认字号 24、预设与显示选项；不清除歌曲、歌词或翻译设置。')
        col.addLayout(row(self.appearance_toggle, self.reset_appearance_button, None))
        self.appearance_dialog = appearance_dialog(window, self.caption_appearance)
        self.appearance_editor = self.appearance_dialog.editor
        self.appearance_dialog.finished.connect(lambda _: self.appearance_toggle.setChecked(False))
        self.appearance_toggle.toggled.connect(self.toggle_appearance_editor)
        layout.addWidget(appearance_card)
        settings, col = card('自动连接', 'Qwen 运行时同步歌曲；Waybar 同步显示当前中文歌词。')
        self.enabled = QCheckBox('自动连接 Apple Music')
        self.enabled.setChecked(window.settings.value('music/enabled', True, type=bool))
        self.translation_mode = Select()
        self.translation_mode.setAccessibleName('歌词翻译方式')
        self.translation_mode.addItem('自动翻译缺少的中文', 'automatic')
        self.translation_mode.addItem('手动翻译（点击“翻译中文”）', 'manual')
        mode = window.settings.value('music/translation_mode', '')
        if mode not in ('automatic', 'manual'):
            mode = 'automatic' if window.settings.value('music/translate', True, type=bool) else 'manual'
        self.translation_mode.setCurrentIndex(self.translation_mode.findData(mode))
        window.settings.setValue('music/translation_mode', mode)
        self.translation_hint = label('', 'muted', True)
        col.addWidget(self.enabled)
        col.addWidget(label('歌词翻译方式', 'secondary'))
        col.addWidget(self.translation_mode)
        col.addWidget(self.translation_hint)
        self.connect_button = button('连接 / 重试', self.activate)
        col.addLayout(row(self.connect_button, button('使用说明', self.open_help), None))
        col.addWidget(label('中文歌词保存在本机，可导入手机。播放设备选择控制这台电脑的音频输出；AirPlay 请在 Apple Music 内选择。', 'muted', True))
        layout.addWidget(settings)
        layout.addStretch()
        self.enabled.toggled.connect(self.configure)
        self.translation_mode.currentIndexChanged.connect(self.configure)
        self.update_mode_hint()
        self.caption_appearance.changed.connect(self.lyrics_appearance_changed)
        self.refresh_lyrics_palette()
        self.apply_state({'available': False, 'track': {}, 'capabilities': {}, 'lyrics': {}})

    def configure(self):
        self.window.settings.setValue('music/enabled', self.enabled.isChecked())
        mode = self.translation_mode.currentData()
        self.window.settings.setValue('music/translation_mode', mode)
        self.window.settings.setValue('music/translate', mode == 'automatic')
        self.window.settings.sync()
        self.update_mode_hint()
        self.restarting = self.enabled.isChecked()
        if self.process:
            self.stop()
        elif self.restarting:
            self.activate()

    def update_mode_hint(self):
        self.translation_hint.setText(
            '自动：缺少中文时使用本机 Codex gpt-6-luna 翻译，并保存到本机。'
            if self.translation_mode.currentData() == 'automatic' else
            '手动：先显示原歌词；点击“翻译中文”后使用本机 Codex gpt-6-luna 翻译。')

    def activate(self):
        if self.process or self.closing or not self.enabled.isChecked():
            return
        self.stopping = self.restarting = False
        self.buffer = b''
        self.error.clear()
        self.status.setText('正在连接 Apple Music…')
        process = QProcess(self)
        self.process = process
        process.setWorkingDirectory(str(ROOT))
        process.setProgram(str(ROOT / '.venv/bin/python'))
        process.setArguments([str(ROOT / 'music_service.py')])
        config = {'music_translation_enabled': self.translation_mode.currentData() == 'automatic',
                  'music_translation_mode': self.translation_mode.currentData()}
        process.started.connect(lambda: self.process_started(process, config))
        process.readyReadStandardOutput.connect(self.read_output)
        process.finished.connect(lambda code, status: self.finished(process, code))
        process.errorOccurred.connect(lambda error: self.process_error(process, error))
        self.connect_button.setEnabled(False)
        process.start()

    def process_started(self, process, config):
        if self.process is process and not self.stopping and not self.closing:
            process.write((json.dumps(config) + '\n').encode())
        else:
            process.closeWriteChannel()

    def process_error(self, process, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.finished(process, 1)

    def finished(self, process, code):
        if self.process is not process:
            return
        self.process = None
        process.deleteLater()
        restart = self.restarting and not self.closing
        self.restarting = False
        self.connect_button.setEnabled(True)
        self.status.setText('自动连接已停止' if self.stopping or code == 0 else '连接未能启动，请点击重试。')
        # Do not leave old song/lyrics or active controls after a worker exits.
        self.apply_state({'available': False, 'track': {}, 'capabilities': {}, 'lyrics': {}})
        if restart:
            QTimer.singleShot(0, self.activate)

    def read_output(self):
        if not self.process:
            return
        self.buffer += bytes(self.process.readAllStandardOutput())
        if len(self.buffer) > 8 * 1024 * 1024:
            self.buffer = b''
            self.error.setText('歌词内容过大，暂时无法显示。')
            return
        while b'\n' in self.buffer:
            line, self.buffer = self.buffer.split(b'\n', 1)
            try:
                data = json.loads(line)
                if not isinstance(data, dict):
                    continue
                if isinstance(data.get('state'), dict) and not self.stopping and not self.closing:
                    self.apply_state(data['state'])
                elif data.get('error'):
                    self.error.setText(str(data['error']))
            except (ValueError, TypeError):
                pass

    def command(self, action, value=None, **extras):
        if not self.process or self.stopping:
            self.error.setText('请先连接 Apple Music。')
            return
        data = {'action': action, 'value': value, **extras}
        if self.state.get('track', {}).get('id'):
            data['track_id'] = self.state['track']['id']
        self.error.clear()
        self.process.write((json.dumps(data, ensure_ascii=False) + '\n').encode())

    def keyboard_seek(self):
        if not self.progress.isSliderDown():
            self.command('seek', self.progress.value() / 1000)

    def keyboard_volume(self):
        if not self.volume.isSliderDown():
            self.command('volume', self.volume.value() / 100)

    def apply_state(self, state):
        if not isinstance(state, dict):
            return
        self.state = state
        track, capabilities = state.get('track') or {}, state.get('capabilities') or {}
        available = bool(state.get('available'))
        self.title.setText(track.get('title') or '等待 Apple Music 播放')
        self.artist.setText(' · '.join(filter(None, [track.get('artist'), track.get('album')])) or
                            '打开已安装的 Apple Music（Adam），播放任意歌曲即可连接。')
        self.play.setText('暂停' if state.get('playing') else '播放')
        for widget, action in ((self.play, 'play_pause'), (self.previous, 'previous'),
                               (self.next, 'next'), (self.progress, 'seek'),
                               (self.volume, 'volume'), (self.devices, 'device')):
            widget.setEnabled(available and not self.stopping and not self.closing and bool(capabilities.get(action)))
        lyric_actions_enabled = available and not self.stopping and not self.closing
        self.retry_button.setEnabled(lyric_actions_enabled)
        self.import_button.setEnabled(lyric_actions_enabled)
        self.translate_button.setEnabled(lyric_actions_enabled and
            (state.get('lyrics') or {}).get('translation_status') not in ('loading', 'translating'))
        duration, position = track.get('duration') or 0, state.get('position') or 0
        self.time.setText(clock_text(position) + ' / ' + clock_text(duration))
        if not self.progress.isSliderDown():
            self.progress.setRange(0, min(2147483647, max(0, int(duration * 1000))))
            self.progress.setValue(int(position * 1000))
        if not self.volume.isSliderDown():
            self.volume.setValue(round((state.get('volume') or 0) * 100))
        self.devices.blockSignals(True)
        self.devices.clear()
        for device in state.get('devices') or []:
            self.devices.addItem(device.get('description') or device.get('name') or str(device['id']), device['id'])
            if device.get('selected'):
                self.devices.setCurrentIndex(self.devices.count() - 1)
        if not self.devices.count():
            self.devices.addItem('等待音频输出', None)
        self.devices.blockSignals(False)
        if available:
            self.status.setText(('正在播放' if state.get('playing') else '已暂停') + ' · ' + state.get('identity', 'Apple Music'))
        elif self.process and not self.stopping:
            self.status.setText(state.get('error') or '已连接，等待 Apple Music 开始播放。')
        self.render_lyrics()
        if self.caption_window is not None:
            self.caption_window.apply_state(state if not self.stopping and not self.closing else {})

    def toggle_appearance_editor(self, checked):
        self.appearance_dialog.setVisible(checked)
        if checked:
            self.appearance_dialog.raise_()
            self.appearance_dialog.activateWindow()

    def open_appearance_editor(self):
        self.appearance_toggle.setChecked(True)
        self.appearance_dialog.raise_()
        self.appearance_dialog.activateWindow()

    def lyrics_appearance_changed(self, options):
        self.click_through.blockSignals(True)
        self.click_through.setChecked(options['click_through'])
        self.click_through.blockSignals(False)
        if options.get('style') != self._last_appearance.get('style'):
            self._reading_preset = True
            self.window.settings.setValue('music_caption_reading_preset', True)
        self._last_appearance = dict(options)
        self.render_lyrics(force=True)

    def toggle_caption_window(self):
        if self.closing:
            return
        if self.caption_window is None:
            self.caption_window = LyricsCaptionWindow(self.window.settings, appearance=self.caption_appearance)
            self.caption_window.visibilityChanged.connect(self.caption_visibility_changed)
            self.caption_window.appearanceRequested.connect(self.open_appearance_editor)
        if self.caption_window.isVisible():
            self.caption_window.hide()
        else:
            self.caption_window.apply_state(self.state if not self.stopping else {})
            self.caption_window.show()
            self.caption_window.raise_()

    def caption_visibility_changed(self, visible):
        self.caption_button.setText('隐藏悬浮歌词' if visible else '打开悬浮歌词')

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange,
                            QEvent.Type.StyleChange) and hasattr(self, 'reading'):
            QTimer.singleShot(0, self.refresh_lyrics_palette)

    def refresh_lyrics_palette(self):
        accent = self.palette().color(QPalette.ColorRole.Highlight).name()
        secondary = self.palette().color(QPalette.ColorRole.PlaceholderText).name()
        slider_style = (
            f'QSlider::groove:horizontal {{height:4px; background:{secondary}; border-radius:2px;}}'
            f'QSlider::sub-page:horizontal {{background:{accent}; border-radius:2px;}}'
            f'QSlider::handle:horizontal {{width:12px; margin:-5px 0; border-radius:6px; background:{accent};}}'
            f'QSlider::handle:horizontal:disabled {{background:{secondary};}}')
        self.progress.setStyleSheet(slider_style)
        self.volume.setStyleSheet(slider_style)
        self.render_lyrics(force=True)

    def eventFilter(self, watched, event):
        if hasattr(self, 'reading') and watched is self.reading.viewport():
            if event.type() in (QEvent.Type.MouseMove, QEvent.Type.Leave):
                anchor = self.reading.anchorAt(event.position().toPoint()) if event.type() == QEvent.Type.MouseMove else ''
                try:
                    hovered = int(anchor.split(':', 1)[1]) if anchor.startswith('lyric:') else -1
                except ValueError:
                    hovered = -1
                if hovered != self._lyric_hover_index:
                    self._lyric_hover_index = hovered
                    self.render_lyrics(force=True, keep_scroll=True)
            elif event.type() == QEvent.Type.Resize:
                options = self.caption_appearance.options
                if options.get('style') == 'blur' or options.get('blur_translation') or options.get('dashed_translation'):
                    self.schedule_lyric_image_refresh()
        return super().eventFilter(watched, event)

    def schedule_lyric_image_refresh(self):
        options = self.caption_appearance.options
        if not self._rendering_lyrics and (
                options['style'] == 'blur' or options.get('blur_translation') or options.get('dashed_translation')):
            self._image_refresh_timer.start(0)

    def translation_image_size(self, text, size, bold, family):
        width = max(100, self.reading.viewport().width() - 32)
        key = text, width, size, bold, family
        if key not in self._lyric_image_sizes:
            font = QFont(family)
            font.setPixelSize(size)
            font.setBold(bold)
            flags = Qt.TextFlag.TextWordWrap | Qt.TextFlag.TextExpandTabs
            bounds = QFontMetrics(font).boundingRect(QRect(0, 0, width - 16, 4096), flags, text)
            self._lyric_image_sizes[key] = width, min(4096, max(size + 16, bounds.height() + 18))
        self._lyric_image_sizes.move_to_end(key)
        result = self._lyric_image_sizes[key]
        while len(self._lyric_image_sizes) > 2048:
            self._lyric_image_sizes.popitem(last=False)
        return result

    def translation_image(self, text, *, color, opacity, size, bold, blur, dashed, family):
        """Render only the translation so native text around it remains sharp."""
        width = max(100, self.reading.viewport().width() - 32)
        key = (text, width, color, opacity, size, bold, blur, dashed, family)
        if key in self._lyric_images:
            self._lyric_images.move_to_end(key)
            return self._lyric_images[key]
        font = QFont(family)
        font.setPixelSize(size)
        font.setBold(bold)
        flags = Qt.TextFlag.TextWordWrap | Qt.TextFlag.TextExpandTabs
        width, height = self.translation_image_size(text, size, bold, family)
        image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setFont(font)
        ink = QColor(color)
        ink.setAlphaF(opacity / 100)
        painter.setPen(ink)
        if dashed:
            layout = QTextLayout(text.replace('\r\n', '\n').replace('\n', '\u2028'), font)
            option = QTextOption()
            option.setWrapMode(QTextOption.WrapMode.WordWrap)
            layout.setTextOption(option)
            layout.beginLayout()
            wrapped, top = [], 0
            while True:
                line = layout.createLine()
                if not line.isValid():
                    break
                line.setLineWidth(width - 16)
                line.setPosition(QPointF(0, top))
                top += line.height()
                wrapped.append(line)
            layout.endLayout()
            layout.draw(painter, QPointF(8, 6))
            pen = QPen(ink, 1, Qt.PenStyle.DashLine)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            painter.setPen(pen)
            for line in wrapped:
                bounds = line.naturalTextRect()
                y = round(6 + line.y() + line.ascent() + size * .25) + .5
                painter.drawLine(QPointF(8 + bounds.left(), y), QPointF(8 + bounds.right(), y))
        else:
            painter.drawText(QRect(8, 6, width - 16, height - 14), flags, text)
        painter.end()
        if blur:
            scene = QGraphicsScene()
            scene.setSceneRect(QRectF(0, 0, width, height))
            item = scene.addPixmap(QPixmap.fromImage(image))
            effect = QGraphicsBlurEffect()
            effect.setBlurRadius(blur)
            item.setGraphicsEffect(effect)
            softened = QImage(image.size(), image.format())
            softened.fill(Qt.GlobalColor.transparent)
            painter = QPainter(softened)
            scene.render(painter)
            painter.end()
            image = softened
        self._lyric_images[key] = image
        self._lyric_image_bytes += image.sizeInBytes()
        while self._lyric_image_bytes > 64 * 1024 * 1024 or len(self._lyric_images) > 1024:
            _, expired = self._lyric_images.popitem(last=False)
            self._lyric_image_bytes -= expired.sizeInBytes()
        return image

    def visible_image_rows(self, line_count, index, lines_key, keep_scroll):
        if self._rendered_image_lines != lines_key or (self.follow.isChecked() and not keep_scroll):
            center = max(0, index) if self.follow.isChecked() else 0
            return set(range(max(0, center - 6), min(line_count, center + 12)))
        document = self.reading.document()
        height = self.reading.viewport().height()
        # Qt updates scrollbar limits after layout; a font/preset change may
        # otherwise briefly leave the saved scroll position beyond the document.
        total_height = document.documentLayout().documentSize().height()
        top = min(self.reading.verticalScrollBar().value(), max(0, total_height - height))
        visible = set()
        for number in range(line_count):
            block = document.findBlockByNumber(number)
            if not block.isValid():
                break
            bounds = document.documentLayout().blockBoundingRect(block)
            if bounds.bottom() >= top - height and bounds.top() <= top + height * 2:
                visible.add(number)
        return visible

    def render_lyrics(self, force=False, keep_scroll=False):
        if self._rendering_lyrics:
            return
        lyrics = self.state.get('lyrics') or {}
        lines = lyrics.get('lines') or []
        index = lyrics.get('current_index', -1)
        options = self.caption_appearance.options
        lines_key = json.dumps(lines, ensure_ascii=False)
        key = (lines_key, index, self.palette().color(QPalette.ColorRole.Text).name(),
               json.dumps(options, sort_keys=True), self._reading_preset, self._lyric_hover_index)
        self.lyric_status.setText(lyrics.get('status_text') or lyrics.get('error') or
                                  ('纯音乐' if lyrics.get('instrumental') else '播放后自动读取歌词'))
        if not force and key == self.render_key:
            return
        self._rendering_lyrics = True
        self.render_key = key
        old_scroll = self.reading.verticalScrollBar().value()
        accent = self.palette().color(QPalette.ColorRole.Highlight).name()
        resolved = resolve_appearance(options)
        preset = self._reading_preset or options['style'] != 'cinema'
        foreground = resolved['foreground'] if preset or options['foreground'] else self.palette().color(QPalette.ColorRole.Text).name()
        secondary = resolved['secondary'] if preset or options['secondary'] else self.palette().color(QPalette.ColorRole.PlaceholderText).name()
        background = resolved['background'] if preset or options['background'] else self.palette().color(QPalette.ColorRole.Base).name()
        if options['style'] == 'weaken' and not options['foreground']:
            foreground = '#737373' if self.palette().color(QPalette.ColorRole.Window).lightness() > 128 else '#A1A1A1'
        # Transparent overlay presets sit over other apps; the reading pane sits
        # on the app's theme. Adapt only preset inks, never explicit user colors.
        if preset and options['background_opacity'] < 40 and QColor(background).lightness() < 200:
            if not options['foreground'] and QColor(foreground).lightness() > 200:
                foreground = self.palette().color(QPalette.ColorRole.Text).name()
            if not options['secondary'] and QColor(secondary).lightness() > 180:
                secondary = self.palette().color(QPalette.ColorRole.PlaceholderText).name()
        decoration = {
            'quote': 'border-left:4px solid #D8A94B;',
            'dashed': 'border-bottom:1px dashed #D8A94B;',
            'outline': 'border:1px solid #D8A94B;',
        }.get(resolved.get('decoration'), '')
        self.reading.setStyleSheet(
            f'QTextBrowser {{background:{color_css(background, options["background_opacity"])};'
            f'border:0; border-radius:{options["radius"]}px; padding:12px;{decoration}}}')
        self.reading.document().setDefaultFont(QFont(options['font_family']))
        opacity = options['text_opacity']
        source_opacity = opacity * (.6 if options.get('dim_original') else 1)
        size, source_size = options['size'], max(12, round(options['size'] * .8))
        image_primary = options['style'] == 'blur' or options.get('blur_translation') or options.get('dashed_translation')
        image_rows = self.visible_image_rows(len(lines), index, lines_key, keep_scroll) if image_primary else set()
        placeholder = QImage(1, 1, QImage.Format.Format_ARGB32_Premultiplied)
        placeholder.fill(Qt.GlobalColor.transparent)
        # Stable row URLs keep the document cache bounded across seeks, songs,
        # hover states and resizes; offscreen rows retain only a tiny placeholder.
        for number in range(self._lyric_resource_count):
            self.reading.document().addResource(QTextDocument.ResourceType.ImageResource,
                                                QUrl(f'lyric-image:{number}'), placeholder)
        self._lyric_resource_count = len(lines) if image_primary else 0
        html = []
        for i, line in enumerate(lines):
            source, translation = lyric_pair(line)
            text = escape(source).replace('\n', '<br>')
            translated = escape(translation).replace('\n', '<br>')
            color = accent if i == index and not preset and not options['foreground'] else foreground
            primary = (f'<span style="color:{color_css(color, opacity)};font-size:{size}px;'
                       f'font-weight:{700 if i == index else 400}">{translated}</span>')
            original = (f'<span style="font-size:{source_size}px;color:{color_css(secondary, source_opacity)}">{text}</span>')
            line_image_primary = image_primary and bool(str(line.get('translation') or '').strip())
            if line_image_primary:
                blurred = i != self._lyric_hover_index
                blur = (6 if options.get('blur_translation') else 4 if options['style'] == 'blur' else 0) if blurred else 0
                image_opacity = opacity * (.75 if options['style'] == 'blur' and not options.get('blur_translation') and blurred else 1)
                width, height = self.translation_image_size(translation, size, i == index, options['font_family'])
                image = self.translation_image(
                    translation, color=color, opacity=image_opacity, size=size, bold=i == index, blur=blur,
                    dashed=options.get('dashed_translation', False), family=options['font_family']) if i in image_rows else placeholder
                resource = QUrl(f'lyric-image:{i}')
                self.reading.document().addResource(QTextDocument.ResourceType.ImageResource, resource, image)
                primary = (f'<a href="lyric:{i}"><img src="{resource.toString()}" '
                           f'width="{width}" height="{height}" alt="{escape(translation, quote=True)}"></a>')
                if line.get('start') is not None:
                    original = f'<a href="seek:{line["start"]}" style="text-decoration:none">{original}</a>'
            contents = primary
            if text and options['show_source']:
                pair = (primary, original) if options['translation_first'] else (original, primary)
                contents = '<br>'.join(pair)
            if line.get('start') is not None and not line_image_primary:
                contents = f'<a href="seek:{line["start"]}" style="text-decoration:none">{contents}</a>'
            html.append(f'<p style="margin:0 0 {options["spacing"] + 11}px"><a name="line-{i}"></a>{contents}</p>')
        self.reading.setHtml(''.join(html) or '<p>等待歌词。若没有在线歌词，可导入本地 LRC 或文本歌词。</p>')
        self.reading.setAccessibleDescription('\n'.join('\n'.join(filter(None, lyric_pair(line))) for line in lines))
        if self.follow.isChecked() and index >= 0 and not keep_scroll:
            self.reading.scrollToAnchor(f'line-{index}')
        else:
            self.reading.verticalScrollBar().setValue(old_scroll)
        self._rendered_image_lines = lines_key if image_primary else None
        self._rendering_lyrics = False
        if image_primary and not keep_scroll:
            self.schedule_lyric_image_refresh()

    def lyric_clicked(self, url):
        if url.scheme() == 'lyric':
            try:
                index = int(url.path())
                lines = (self.state.get('lyrics') or {}).get('lines') or []
                if 0 <= index < len(lines) and lines[index].get('start') is not None:
                    self.command('seek', float(lines[index]['start']))
            except (ValueError, TypeError):
                pass
            return
        if url.scheme() == 'seek':
            try:
                self.command('seek', float(url.path()))
            except ValueError:
                pass

    def import_lyrics(self):
        if not self.state.get('available'):
            self.error.setText('请先在 Apple Music 播放要导入歌词的歌曲。')
            return
        track_id = self.state.get('track', {}).get('id')
        path = choose_file(self, '为当前歌曲导入歌词', str(Path.home()), '歌词 (*.lrc *.txt)')
        if not path:
            return
        if not self.state.get('available') or self.state.get('track', {}).get('id') != track_id:
            self.error.setText('歌曲已切换，请为当前歌曲重新选择歌词。')
            return
        try:
            with open(path, 'rb') as source:
                raw = source.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError()
            self.command('import_lyrics', lyrics=raw.decode('utf-8-sig'))
        except (OSError, ValueError):
            self.error.setText('无法读取歌词，请选择小于 1 MB 的 UTF-8 LRC 或文本文件。')

    def open_cache(self):
        path = (self.state.get('lyrics') or {}).get('paths', {}).get('bilingual')
        if path and Path(path).is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))
        else:
            self.error.setText('当前歌曲尚未保存歌词。')

    def open_player(self):
        launcher = shutil.which('adam-online') or Path.home() / '.local/bin/adam-online'
        if Path(launcher).is_file():
            QProcess.startDetached(str(launcher), [])
        else:
            QDesktopServices.openUrl(QUrl('https://music.apple.com/'))

    def open_help(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(ROOT / 'docs/MUSIC.zh-CN.md')))

    def stop(self):
        if self.process and not self.stopping:
            self.stopping = True
            self.apply_state(self.state)
            process = self.process
            process.closeWriteChannel()
            QTimer.singleShot(3500, lambda: process.kill() if self.process is process else None)

    def shutdown(self):
        self.closing = True
        self.restarting = False
        self.appearance_dialog.close()
        if self.caption_window is not None:
            self.caption_window.close()
            self.caption_window.deleteLater()
            self.caption_window = None
        self.apply_state(self.state)
        if self.process is None:
            return True
        self.stop()
        return False
