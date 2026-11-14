"""Frameless bilingual overlay with paired captions and unobtrusive controls."""
from collections import OrderedDict

from PyQt6.QtCore import QEvent, QProcess, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor, QGuiApplication
from PyQt6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QPushButton,
                             QSizeGrip, QVBoxLayout, QWidget)

from caption_style import CaptionAppearance, CaptionSurface, STYLES, resolve_appearance
from ui_controls import WheelSafeComboBox, WheelSafeSpinBox


def fitted_overlay_geometry(available, height):
    """Use logical screen coordinates, including work-area insets and scaling."""
    margin = min(24, max(0, available.width() // 20))
    width = min(max(420, round(available.width() * .6)), 1600, available.width() - 2 * margin)
    height = min(max(155, height), max(1, int(available.height() * .65)))
    bottom = min(64, max(16, round(available.height() * .04)))
    return QRect(available.x() + (available.width() - width) // 2,
                 max(available.y(), available.y() + available.height() - height - bottom), width, height)


class CaptionWindow(QWidget):
    appearanceRequested = pyqtSignal()

    def __init__(self, settings=None, *, appearance=None, kind='captions', directions=('inbound', 'outbound')):
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint)
        self.settings = settings
        self.kind = kind
        self.appearance = appearance or CaptionAppearance(settings, 'music_caption_' if kind == 'lyrics' else 'caption_', self)
        self.geometry_key = 'music_caption_geometry' if kind == 'lyrics' else 'caption_geometry'
        self.settings_dialog = None
        self.desktop_process = None
        self._pending_geometry = None
        self._observed_screen = None
        self._last_auto_fit = self.appearance.options['auto_fit_screen']
        self._last_transparent = None
        self._last_click_through = None
        self.policy_timer = QTimer(self)
        self.policy_timer.setSingleShot(True)
        self.policy_timer.setInterval(150)
        self.policy_timer.timeout.connect(self.apply_desktop_policy)
        self.setWindowTitle('Qwen 悬浮歌词' if kind == 'lyrics' else 'Qwen 悬浮字幕')
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMinimumSize(420, 155)
        self.resize(960, 240)
        self.setObjectName('caption_overlay')
        self.setStyleSheet('QWidget#caption_overlay { background:transparent; border:0; } '
                          '* { font-family:"Noto Sans CJK SC"; }')
        self.states = {}
        self.responsive = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)
        self.toolbar = QWidget()
        self.toolbar.setObjectName('caption_toolbar')
        self.toolbar.setStyleSheet('''
            QWidget#caption_toolbar { background:rgba(15,21,19,235); border-radius:14px; }
            QLabel, QCheckBox { color:#E6EEE9; background:transparent; }
            QPushButton, QComboBox, QSpinBox { background:#212B27; color:#F5F7F4;
                border:0; border-radius:9px; padding:5px 10px; font-size:12px; }
            QPushButton:hover { background:#2B3631; }
            QComboBox QAbstractItemView { background:#18201D; color:#F5F7F4; selection-background-color:#184C42; }
        ''')
        bar = QHBoxLayout(self.toolbar)
        bar.setContentsMargins(12, 6, 8, 6)
        self.caption_title = QLabel('⠿  双语歌词' if kind == 'lyrics' else '⠿  同声字幕')
        self.caption_title.setToolTip('拖动字幕移动窗口；拖动右下角调整大小')
        bar.addWidget(self.caption_title)
        bar.addStretch()
        self.compact_button = QPushButton('设置')
        self.compact_button.setCheckable(True)
        self.compact_button.setAccessibleName('字幕显示设置')
        self.compact_button.toggled.connect(self.toggle_compact)
        bar.addWidget(self.compact_button)
        hide = QPushButton('×')
        hide.setAccessibleName('隐藏悬浮字幕')
        hide.setToolTip('隐藏字幕窗口；翻译继续')
        hide.clicked.connect(self.hide)
        bar.addWidget(hide)
        retain = self.toolbar.sizePolicy()
        retain.setRetainSizeWhenHidden(True)
        self.toolbar.setSizePolicy(retain)
        layout.addWidget(self.toolbar)
        self.options = QWidget(self.toolbar)
        options = QHBoxLayout(self.options)
        options.setContentsMargins(0, 0, 0, 0)
        self.style_combo = WheelSafeComboBox()
        for key, (title, *_rest) in STYLES.items():
            self.style_combo.addItem(title, key)
        style = self.appearance.options['style']
        self.style_combo.setCurrentIndex(max(0, self.style_combo.findData(style)))
        self.style_combo.setAccessibleName('字幕外观')
        options.addWidget(self.style_combo)
        self.size_control = WheelSafeSpinBox()
        self.size_control.setRange(14, 64)
        self.size_control.setValue(self.appearance.options['size'])
        self.size_control.setSuffix(' px')
        self.size_control.setAccessibleName('悬浮字幕字号')
        options.addWidget(self.size_control)
        self.appearance_button = QPushButton('外观…')
        self.appearance_button.clicked.connect(self.edit_appearance)
        options.addWidget(self.appearance_button)
        self.pin = QCheckBox('置顶')
        self.pin.setChecked(True)
        self.pin.toggled.connect(self.set_pinned)
        options.addWidget(self.pin)
        clear = QPushButton('清空')
        clear.clicked.connect(self.clear)
        options.addWidget(clear)
        bar.insertWidget(1, self.options)
        self.options.hide()
        self.surfaces = {}
        self.lines = {}
        self.source_lines = {}
        for direction in directions:
            surface = CaptionSurface(self.style_combo.currentData(), self.size_control.value())
            self.surfaces[direction] = surface
            self.lines[direction] = surface.translation
            self.source_lines[direction] = surface.source
            layout.addWidget(surface)
            for target in (surface, surface.source, surface.translation):
                target.installEventFilter(self)
        self.caption_title.installEventFilter(self)
        self.grip = QSizeGrip(self)
        self.grip.setFixedSize(18, 12)
        layout.addWidget(self.grip, alignment=Qt.AlignmentFlag.AlignRight)
        self.style_combo.currentIndexChanged.connect(self.apply_appearance)
        self.size_control.valueChanged.connect(self.apply_appearance)
        self.appearance.changed.connect(self.refresh_appearance)
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.setInterval(1500)
        self.hide_timer.timeout.connect(self.hide_controls)
        self.clear()
        self.refresh_appearance(self.appearance.options)
        if not self.appearance.options['auto_fit_screen'] and self.settings is not None and self.settings.value(self.geometry_key):
            self.restoreGeometry(self.settings.value(self.geometry_key))

    def value(self, key, default):
        return self.settings.value(key, default) if self.settings else default

    def apply_appearance(self):
        style, size = self.style_combo.currentData(), self.size_control.value()
        if style != self.appearance.options['style']:
            self.appearance.apply_preset(style)
        else:
            self.appearance.update(size=size)

    def refresh_appearance(self, options):
        self.style_combo.blockSignals(True)
        self.size_control.blockSignals(True)
        self.style_combo.setCurrentIndex(max(0, self.style_combo.findData(options['style'])))
        self.size_control.setValue(options['size'])
        self.style_combo.blockSignals(False)
        self.size_control.blockSignals(False)
        for surface in self.surfaces.values():
            surface.apply_appearance(options)
        self.fit_height()
        auto_fit = options['auto_fit_screen']
        transparent = resolve_appearance(options)['background_opacity'] == 0
        if auto_fit != self._last_auto_fit:
            self._last_auto_fit = auto_fit
            self._pending_geometry = None
            if auto_fit and self.isVisible():
                self.fit_to_screen()
        if transparent != self._last_transparent:
            self._last_transparent = transparent
            if self.isVisible():
                self.policy_timer.start()
        self.apply_input_mode()

    def apply_input_mode(self):
        enabled = self.appearance.options['click_through']
        changed = enabled != self._last_click_through
        self._last_click_through = enabled
        handle = self.windowHandle()
        if handle is not None:
            # Set the native input region without remapping the QWidget or
            # sending misleading hide/show signals to the music page.
            flags = handle.flags()
            input_flags = (Qt.WindowType.WindowTransparentForInput |
                           Qt.WindowType.WindowDoesNotAcceptFocus)
            handle.setFlags(flags | input_flags if enabled else flags & ~input_flags)
            # Wayland input regions are double-buffered surface state. A flag
            # change alone need not produce a frame when the lyrics are idle.
            self.update()
        if enabled:
            self.hide_timer.stop()
            self.toolbar.hide()
            self.grip.hide()
        elif self.isVisible():
            self.toolbar.show()
            self.grip.show()
            self.hide_timer.start()
        if changed and self.isVisible():
            self.policy_timer.start()

    def watch_screen(self, screen):
        if self._observed_screen is screen:
            return
        if self._observed_screen is not None:
            try:
                self._observed_screen.availableGeometryChanged.disconnect(self.screen_geometry_changed)
            except (TypeError, RuntimeError):
                pass
        self._observed_screen = screen
        if screen is not None:
            screen.availableGeometryChanged.connect(self.screen_geometry_changed)

    def screen_geometry_changed(self, *_args):
        if self.isVisible() and self.appearance.options['auto_fit_screen']:
            self.fit_to_screen()

    def screen_changed(self, screen):
        self.watch_screen(screen)
        self.screen_geometry_changed()

    def fit_to_screen(self, screen=None):
        screen = screen or self.screen()
        if screen is None:
            return
        self.watch_screen(screen)
        area = screen.availableGeometry()
        rect = fitted_overlay_geometry(area, self.height())
        self.setMinimumWidth(min(420, rect.width()))
        self.resize(rect.width(), rect.height())
        self.fit_height()
        rect = fitted_overlay_geometry(area, self.height())
        self._pending_geometry = (rect.x(), rect.y(), rect.width(), rect.height())
        # Wayland clients cannot place themselves. Hyprland gets the same
        # logical work-area coordinates through the scoped runtime request.
        if not QGuiApplication.platformName().startswith('wayland'):
            self.move(rect.topLeft())
        if self.isVisible():
            self.policy_timer.start()

    def edit_appearance(self):
        if self.receivers(self.appearanceRequested):
            self.appearanceRequested.emit()
            return
        if self.settings_dialog is None:
            from caption_settings import appearance_dialog
            self.settings_dialog = appearance_dialog(None, self.appearance)
            self.destroyed.connect(self.settings_dialog.deleteLater)
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    def fit_height(self):
        if not hasattr(self, 'surfaces'):
            return
        height = self.toolbar.sizeHint().height() + 34
        for surface in self.surfaces.values():
            if not surface.isHidden():
                height += surface.layout.totalHeightForWidth(max(300, self.width()-16))
        available = self.screen().availableGeometry().height() if self.screen() else 900
        old_height = self.height()
        self.resize(self.width(), min(max(155, height), int(available*.65)))
        if self.height() != old_height and self.isVisible() and self.appearance.options['auto_fit_screen']:
            # Keep growing lyrics above the bottom margin, not below the
            # physical screen. Debounce compositor requests during layout.
            area = self.screen().availableGeometry()
            rect = fitted_overlay_geometry(area, self.height())
            rect.setWidth(self.width())
            rect.moveLeft(area.x() + (area.width() - self.width()) // 2)
            self._pending_geometry = rect.getRect()
            if not QGuiApplication.platformName().startswith('wayland'):
                self.move(rect.topLeft())
            self.policy_timer.start()

    def clear(self):
        self.states = {direction: {'sources': OrderedDict(), 'translated': None, 'has_translation': False,
                                  'pending': '', 'pending_id': None} for direction in self.surfaces}
        for surface in self.surfaces.values():
            surface.set_primary_is_source(True)
            surface.source.clear()
            surface.translation.clear()
            surface.notice.hide()
            surface.hide()
        first = next(iter(self.surfaces))
        self.surfaces[first].show()
        self.source_lines[first].setText('原文与译文同步显示')
        self.lines[first].setText('等待歌词…' if self.kind == 'lyrics' else '等待字幕…')
        self.fit_height()

    def pending(self, direction, text, segment_id=None):
        if direction not in self.states:
            return
        state = self.states[direction]
        state['pending'] = text
        state['pending_id'] = segment_id
        if segment_id is not None:
            state['sources'][segment_id] = text
            while len(state['sources']) > 16:
                state['sources'].popitem(last=False)
        if not state['has_translation']:
            self.reveal(direction)
            self.surfaces[direction].set_primary_is_source(True)
            self.source_lines[direction].setText(text)
            self.lines[direction].setText('正在翻译…')
            self.fit_height()
        elif self.responsive:
            self.source_lines[direction].setText(text)
            self.surfaces[direction].notice.setText('上句译文 · 新句翻译中')
            self.surfaces[direction].notice.show()
            self.fit_height()

    def reveal(self, direction):
        for other, state in self.states.items():
            if other != direction and not state['pending'] and not state['has_translation']:
                self.surfaces[other].hide()
        self.surfaces[direction].show()

    def caption(self, direction, text, segment_id=None):
        if direction not in self.states:
            return
        state = self.states[direction]
        if segment_id is not None and state['translated'] is not None and segment_id < state['translated']:
            return
        source = state['sources'].get(segment_id, '') if segment_id is not None else state['pending']
        state['translated'] = segment_id
        state['has_translation'] = True
        self.reveal(direction)
        self.surfaces[direction].set_primary_is_source(False)
        pending = self.responsive and state['pending_id'] is not None and segment_id != state['pending_id']
        self.source_lines[direction].setText(state['pending'] if pending else source)
        self.surfaces[direction].notice.setText('上句译文 · 新句翻译中' if pending else '')
        self.surfaces[direction].notice.setVisible(pending)
        self.lines[direction].setText(text)
        self.fit_height()

    def original(self, direction, text):
        self.reveal(direction)
        self.surfaces[direction].set_primary_is_source(True)
        self.source_lines[direction].clear()
        self.surfaces[direction].notice.hide()
        self.lines[direction].setText(text)
        self.fit_height()

    def toggle_compact(self, checked):
        self.options.setVisible(checked)
        self.compact_button.setText('收起' if checked else '设置')
        self.fit_height()

    def set_pinned(self, checked):
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, checked)
        self.pin.setToolTip('已请求窗口置顶。Hyprland 下同时跨工作区显示；实际层级由桌面决定。'
                            if checked else '已请求取消置顶；Hyprland 下同时解除跨工作区固定。')
        if visible:
            self.show()
            QTimer.singleShot(200, self.apply_desktop_policy)

    def apply_desktop_policy(self):
        if not self.isVisible():
            return
        if self.desktop_process is not None:
            self._desktop_policy_pending = True
            return
        from dataclasses import replace
        from overlay_platform import (detect_desktop, desktop_help, preferred_rule_syntax,
                                      hyprland_runtime_command, parse_hyprland_version)
        from PyQt6.QtWidgets import QApplication
        if not hasattr(self, 'desktop_info'):
            # Even the version probe is asynchronous: a stalled IPC socket
            # must not pause dragging, subtitles or settings controls.
            self.desktop_info = detect_desktop(QApplication.platformName(), probe=False)
        info = self.desktop_info
        if not info.hyprland:
            self.pin.setToolTip(desktop_help(info))
            return
        if not info.hyprctl_available:
            self.pin.setToolTip('未找到 hyprctl，无法自动设置悬浮窗口。可在外观设置中复制 Hyprland 规则。')
            return
        probe_version = not info.hyprland_version and not getattr(self, '_desktop_version_probed', False)
        if not probe_version and not info.hyprland_version:
            self.pin.setToolTip('未能识别 Hyprland 版本，未执行自动窗口设置。可在外观设置中选择并复制规则。')
            return
        if info.hyprland_version and preferred_rule_syntax(info) != 'lua':
            self.pin.setToolTip('此 Hyprland 版本需要桌面规则。请在外观设置中复制相应规则并重新打开窗口。')
            return
        requested_pin = self.pin.isChecked()
        requested_geometry = self._pending_geometry
        requested_transparent = resolve_appearance(self.appearance.options)['background_opacity'] == 0
        requested_click_through = self.appearance.options['click_through']
        if probe_version:
            self._desktop_version_probed = True
            command = ['hyprctl', '-j', 'version']
        else:
            command = hyprland_runtime_command(self.kind, pinned=requested_pin,
                                               transparent=requested_transparent, geometry=requested_geometry,
                                               click_through=requested_click_through)
        # Keep the request alive long enough to reap it if the window itself
        # is deleted; the destroyed handler kills it without a GUI-thread wait.
        process = QProcess()
        self.desktop_process = process
        self._desktop_policy_pending = False
        deadline = QTimer(process)
        deadline.setSingleShot(True)
        # Owned timer and one completion guard cover timeout/error/finished
        # arriving together, without a delayed callback to a deleted process.
        status = {'done': False, 'timed_out': False, 'owner_destroyed': False, 'error': ''}

        def done(exit_code=-1, exit_status=QProcess.ExitStatus.CrashExit):
            if status['done']:
                return
            status['done'] = True
            deadline.stop()
            stdout = bytes(process.readAllStandardOutput()).decode('utf-8', errors='replace').strip()
            stderr = bytes(process.readAllStandardError()).decode('utf-8', errors='replace').strip()
            success = exit_code == 0 and exit_status == QProcess.ExitStatus.NormalExit and not status['timed_out']
            process.deleteLater()
            if status['owner_destroyed']:
                return
            self.destroyed.disconnect(owner_destroyed)
            self.desktop_process = None
            if not success:
                reason = ('桌面响应超时' if status['timed_out'] else
                          ' '.join((stderr or stdout or status['error'] or '桌面请求未完成').split())[:240])
                self.pin.setToolTip(f'悬浮窗口设置未完成：{reason}。可在外观设置中复制 Hyprland 规则。')
            elif probe_version:
                version = parse_hyprland_version(stdout)
                self.desktop_info = replace(self.desktop_info, hyprland_version=version)
                if not version:
                    self.pin.setToolTip('未能识别 Hyprland 版本，未执行自动窗口设置。可在外观设置中选择并复制规则。')
            else:
                if self._pending_geometry == requested_geometry:
                    self._pending_geometry = None
                self.pin.setToolTip('已发送浮动与跨工作区固定请求，并提升一次窗口层级；最终显示顺序由 Hyprland 决定。'
                                    if requested_pin else '已发送解除跨工作区固定请求，窗口继续保持浮动。')
            # A click during a pending request must not leave the compositor
            # using the old checkbox value when that request finally finishes.
            repeat = (self._desktop_policy_pending or requested_pin != self.pin.isChecked() or
                      requested_click_through != self.appearance.options['click_through'])
            self._desktop_policy_pending = False
            if self.isVisible() and ((success and probe_version and self.desktop_info.hyprland_version) or
                                     (repeat and not (probe_version and not success))):
                QTimer.singleShot(0, self.apply_desktop_policy)

        def failed(error):
            status['error'] = process.errorString()
            if error == QProcess.ProcessError.FailedToStart:
                done()

        def timed_out():
            if not status['done']:
                status['timed_out'] = True
                process.kill()

        def owner_destroyed():
            status['owner_destroyed'] = True
            if process.state() == QProcess.ProcessState.NotRunning:
                done()
            else:
                process.kill()

        self.destroyed.connect(owner_destroyed)
        process.finished.connect(done)
        process.errorOccurred.connect(failed)
        deadline.timeout.connect(timed_out)
        deadline.start(750 if probe_version else 1500)
        process.start(command[0], command[1:])

    def hide_controls(self):
        if not self.underMouse() and not self.compact_button.isChecked():
            self.toolbar.hide()
            self.grip.hide()

    def enterEvent(self, event):
        if not self.appearance.options['click_through']:
            self.hide_timer.stop()
            self.toolbar.show()
            self.grip.show()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.hide_timer.start()
        super().leaveEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        handle = self.windowHandle()
        if handle and not getattr(self, '_screen_signal_connected', False):
            handle.screenChanged.connect(self.screen_changed)
            self._screen_signal_connected = True
        if self.appearance.options['auto_fit_screen']:
            screen = QGuiApplication.screenAt(QCursor.pos()) or self.screen()
            if handle and screen:
                handle.setScreen(screen)
            self.fit_to_screen(screen)
        self.toolbar.show()
        self.grip.show()
        self.hide_timer.start()
        self.policy_timer.start()
        self.apply_input_mode()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            QTimer.singleShot(0, self.fit_height)

    def eventFilter(self, watched, event):
        if (not self.appearance.options['click_through'] and
                event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton):
            handle = self.windowHandle()
            if handle and handle.startSystemMove():
                return True
        return super().eventFilter(watched, event)

    def hideEvent(self, event):
        self.policy_timer.stop()
        if self.settings is not None:
            self.settings.setValue(self.geometry_key, self.saveGeometry())
            self.settings.sync()
        if self.settings_dialog is not None:
            self.settings_dialog.hide()
        super().hideEvent(event)
