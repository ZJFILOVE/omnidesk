"""Native caption styling studio: live preview, visual presets and grouped controls."""
from PyQt6.QtCore import QEvent, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QCursor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PyQt6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QDialog, QFrame,
                             QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                             QSizePolicy, QStackedWidget, QTabBar, QVBoxLayout, QWidget)

from caption_style import CaptionSurface, STYLES, resolve_appearance
from caption_editor_widgets import PresetCard, ValueSlider
from ui_controls import WheelSafeComboBox, WheelSafeFontComboBox
from ui_tokens import THEMES


class PreviewStage(QWidget):
    def __init__(self, surface, parent=None):
        super().__init__(parent)
        self.dark = False
        self.surface = surface
        self.samples = ('Every word brings us a little closer.', '每一句话，都让我们靠近一点。')
        for label in (surface.source, surface.translation):
            label.setWordWrap(False)
        self.setFixedHeight(164)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 22, 28, 22)
        layout.addStretch()
        layout.addWidget(surface)
        layout.addStretch()

    def fit_surface(self):
        # Keep the actual selected font size. Long preview samples are elided,
        # while height grows to show both lines and their effects completely.
        available = max(40, self.width() - 56 - 60 - 20)
        for label, sample in zip((self.surface.source, self.surface.translation), self.samples):
            label.ensurePolished()
            label.setText(label.fontMetrics().elidedText(sample, Qt.TextElideMode.ElideRight, available))
        self.surface.layout.activate()
        self.setFixedHeight(max(164, self.surface.sizeHint().height() + 44))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_surface()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        gradient = QLinearGradient(0, self.height(), self.width(), 0)
        for stop, value in ((0, '#26322E' if self.dark else '#F9FBEA'),
                            (.55, '#35453E' if self.dark else '#EDF8EC'),
                            (1, '#20332D' if self.dark else '#E9F9F1')):
            gradient.setColorAt(stop, QColor(value))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawRoundedRect(QRectF(self.rect()), 12, 12)


class Toggle(QCheckBox):
    def __init__(self, title):
        super().__init__()
        self.setAccessibleName(title)
        self.setFixedSize(42, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def hitButton(self, position):
        return self.rect().contains(position)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = self.palette().color(QPalette.ColorRole.Highlight if self.isChecked() else QPalette.ColorRole.Mid)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(QRectF(3, 5, 36, 20), 10, 10)
        painter.setBrush(self.palette().color(QPalette.ColorRole.HighlightedText) if self.isChecked() else QColor('#FFFFFF'))
        painter.drawEllipse(QRectF(21 if self.isChecked() else 5, 7, 16, 16))
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(self.palette().color(QPalette.ColorRole.Highlight), 1.5))
            painter.drawRoundedRect(QRectF(1, 3, 40, 24), 12, 12)


class CaptionAppearanceEditor(QWidget):
    def __init__(self, appearance, parent=None, *, compact=False):
        super().__init__(parent)
        self.setObjectName('caption_editor')
        self.appearance = appearance
        self.controls, self.colors, self.color_labels, self.sliders = {}, {}, {}, {}
        self.preset_buttons, self.cards, self.scroll_areas = {}, [], []
        self._columns = (0, 0)
        self._theme_signature = None
        self.theme_timer = QTimer(self)
        self.theme_timer.setSingleShot(True)
        self.theme_timer.timeout.connect(self.apply_theme)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)
        heading = QHBoxLayout()
        heading.addWidget(self.text('效果预览', 'caption_section'))
        heading.addStretch()
        self.background_button = QPushButton('切换深色画面')
        self.background_button.setProperty('caption_role', 'quiet')
        self.background_button.clicked.connect(self.toggle_preview_background)
        heading.addWidget(self.background_button)
        self.reset_button = QPushButton('恢复默认外观')
        self.reset_button.setProperty('caption_role', 'quiet')
        self.reset_button.clicked.connect(appearance.reset)
        heading.addWidget(self.reset_button)
        outer.addLayout(heading)
        self.preview = CaptionSurface()
        self.preview.source.setText('Every word brings us a little closer.')
        self.preview.translation.setText('每一句话，都让我们靠近一点。')
        self.preview_stage = PreviewStage(self.preview)
        self.preview_stage.dark = QApplication.palette().color(QPalette.ColorRole.Window).lightness() < 128
        self.background_button.setText('切换浅色画面' if self.preview_stage.dark else '切换深色画面')
        outer.addWidget(self.preview_stage)
        meta = QHBoxLayout()
        self.current_preset_label = self.text('', 'caption_muted')
        meta.addWidget(self.current_preset_label)
        meta.addStretch()
        meta.addWidget(self.text('更改自动保存', 'caption_muted'))
        outer.addLayout(meta)
        self.tabs = QTabBar()
        self.tabs.addTab('调整样式')
        self.tabs.addTab('风格预设 · 11')
        self.tabs.setExpanding(False)
        self.tabs.setDrawBase(False)
        self.tabs.setAccessibleName('字幕外观分类')
        outer.addWidget(self.tabs)
        self.pages = QStackedWidget()
        # Embedded editors live inside a scrolling page and need a useful
        # reading area. A small standalone dialog can devote more room to
        # large preview text and scroll a shorter settings area instead.
        self.pages.setMinimumHeight(80 if compact else 360)
        self.pages.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        outer.addWidget(self.pages, 1)
        settings_page, self.settings_grid = self.make_page()
        presets_page, self.presets_grid = self.make_page()
        self.tabs.currentChanged.connect(self.pages.setCurrentIndex)
        self.tabs.currentChanged.connect(lambda _: QTimer.singleShot(0, self.reflow))
        self.style_combo = WheelSafeComboBox(self)
        for key, (name, *_rest) in STYLES.items():
            self.style_combo.addItem(name, key)
        # Kept as the programmatic selection interface shared with older controls.
        self.style_combo.hide()
        self.controls['style'] = self.style_combo
        self.style_combo.currentIndexChanged.connect(lambda: appearance.apply_preset(self.style_combo.currentData()))
        self.style_combo.setAccessibleName('字幕样式预设')
        for key, (name, *_rest) in STYLES.items():
            tile = PresetCard(key, name.replace('陪读蛙 · ', ''))
            tile.setToolTip(name + ' · 点击套用完整外观')
            tile.clicked.connect(lambda checked=False, style=key: self.select_preset(style))
            self.preset_buttons[key] = tile
        self.font_combo = WheelSafeFontComboBox()
        self.font_combo.setSizeAdjustPolicy(WheelSafeComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.font_combo.setMinimumContentsLength(7)
        self.font_combo.setMinimumWidth(0)
        self.font_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.font_combo.setAccessibleName('字幕字体')
        self.controls['font_family'] = self.font_combo
        self.font_combo.currentFontChanged.connect(lambda font: appearance.update(font_family=font.family()))
        self.numeric('size', '文字大小', 14, 64, ' px')
        self.numeric('background_opacity', '背景不透明度', 0, 100, '%')
        self.numeric('text_opacity', '文字不透明度', 10, 100, '%')
        self.numeric('radius', '背景圆角', 0, 40, ' px')
        self.numeric('spacing', '原译行距', 0, 30, ' px')
        for key, title in (('transparent_background', '背景完全透明'), ('auto_fit_screen', '适配屏幕'),
                           ('click_through', '鼠标穿透'),
                           ('show_source', '显示原文'), ('translation_first', '译文在上'), ('shadow', '文字阴影'),
                           ('blur_translation', '模糊译文'), ('dashed_translation', '虚线译文'), ('dim_original', '弱化原文')):
            switch = Toggle(title)
            switch.toggled.connect(lambda value, field=key: appearance.update(**{field: value}))
            self.controls[key] = switch
        for key, title in (('foreground', '译文颜色'), ('secondary', '原文颜色'), ('background', '背景颜色')):
            control = QPushButton()
            control.setProperty('caption_role', 'color')
            control.setFixedSize(38, 32)
            control.setAccessibleName(title)
            control.clicked.connect(lambda checked=False, field=key: self.choose_color(field))
            self.colors[key] = control
            self.color_labels[key] = self.text('', 'caption_hex')
        self.build_cards()
        appearance.changed.connect(self.refresh)
        self.apply_theme()
        self.refresh(appearance.options)
        QTimer.singleShot(0, self.reflow)

    def select_preset(self, key):
        self.appearance.apply_preset(key)
        # Reselecting an unchanged preset still has to keep its tile checked.
        self.refresh(self.appearance.options)

    def text(self, value, role=None):
        item = QLabel(value)
        item.setWordWrap(True)
        if role:
            item.setProperty('caption_role', role)
        return item

    def make_page(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setObjectName('caption_page_content')
        grid = QGridLayout(content)
        grid.setContentsMargins(0, 4, 8, 12)
        grid.setSpacing(14)
        grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(content)
        self.pages.addWidget(scroll)
        self.scroll_areas.append(scroll)
        return content, grid

    def numeric(self, key, title, low, high, suffix):
        control = ValueSlider(low, high, suffix)
        control.setAccessibleName(title)
        control.valueChanged.connect(lambda value, field=key: self.appearance.update(**{field: value}))
        self.controls[key] = control
        self.sliders[key] = control.slider

    def card(self, title, hint, keys):
        card = QFrame()
        card.setProperty('caption_role', 'settings_card')
        col = QVBoxLayout(card)
        col.setContentsMargins(16, 16, 16, 16)
        col.setSpacing(8)
        heading = QHBoxLayout()
        heading.addWidget(self.text(title, 'caption_section'))
        heading.addStretch()
        reset = QPushButton('↺')
        reset.setProperty('caption_role', 'reset')
        reset.setAccessibleName('恢复' + title + '默认设置')
        reset.setToolTip('恢复这组设置')
        reset.setFixedSize(28, 28)
        reset.clicked.connect(lambda: self.appearance.update(**{key: self.appearance.defaults[key] for key in keys}))
        heading.addWidget(reset)
        col.addLayout(heading)
        col.addWidget(self.text(hint, 'caption_muted'))
        self.cards.append(card)
        return col

    def add_row(self, layout, title, control, *, description=None):
        row = QHBoxLayout()
        row.setSpacing(10)
        labels = QVBoxLayout()
        labels.setSpacing(3)
        caption = self.text(title)
        caption.setMinimumWidth(66)
        labels.addWidget(caption)
        if description:
            labels.addWidget(self.text(description, 'caption_muted'))
        row.addLayout(labels, 1 if isinstance(control, Toggle) else 0)
        row.addWidget(control, 0 if isinstance(control, Toggle) else 1)
        layout.addLayout(row)

    def add_color(self, layout, title, key):
        row = QHBoxLayout()
        row.addWidget(self.text(title))
        row.addStretch()
        row.addWidget(self.color_labels[key])
        row.addWidget(self.colors[key])
        layout.addLayout(row)

    def build_cards(self):
        col = self.card('布局与背景', '让字幕融入当前画面',
                        ('background', 'background_opacity', 'radius', 'spacing', 'show_source', 'translation_first',
                         'transparent_background', 'auto_fit_screen', 'click_through'))
        self.add_row(col, '背景完全透明', self.controls['transparent_background'],
                     description='隐藏底板；Hyprland 下同时关闭窗口模糊和阴影')
        self.add_row(col, '适配屏幕', self.controls['auto_fit_screen'],
                     description='打开时按当前屏幕设置宽度，并靠下居中')
        self.add_row(col, '鼠标穿透', self.controls['click_through'],
                     description='点击和滚轮交给后面的窗口；回主界面关闭后可拖动。模糊译文需关闭穿透才能悬停查看')
        self.add_color(col, '背景颜色', 'background')
        for key, title in (('background_opacity', '不透明度'), ('radius', '圆角'), ('spacing', '行距'),
                           ('show_source', '显示原文'), ('translation_first', '译文在上')):
            self.add_row(col, title, self.controls[key])
        col.addStretch()
        col = self.card('文字样式', '字体、配色与清晰度', ('font_family', 'size', 'foreground', 'secondary', 'text_opacity', 'shadow'))
        self.add_row(col, '字体', self.font_combo)
        self.add_row(col, '文字大小', self.controls['size'])
        self.add_color(col, '译文颜色', 'foreground')
        self.add_color(col, '原文颜色', 'secondary')
        self.add_row(col, '不透明度', self.controls['text_opacity'])
        self.add_row(col, '文字阴影', self.controls['shadow'])
        col.addStretch()
        col = self.card('阅读效果', '陪读蛙的三种效果，可组合使用', ('blur_translation', 'dashed_translation', 'dim_original'))
        for key, title, description in (
            ('blur_translation', '模糊译文', '鼠标移入时显示清晰译文'),
            ('dashed_translation', '虚线译文', '用细虚线标识译文'),
            ('dim_original', '弱化原文', '降低原文亮度，突出译文')):
            self.add_row(col, title, self.controls[key], description=description)
        col.addStretch()

    def reflow(self):
        if not self.scroll_areas:
            return
        width = self.pages.width() - 16
        columns = 3 if width >= 900 else 2 if width >= 600 else 1
        tiles = 4 if width >= 760 else 3 if width >= 540 else 2
        if self._columns == (columns, tiles):
            return
        self._columns = columns, tiles
        for grid in (self.settings_grid, self.presets_grid):
            while grid.count():
                grid.takeAt(0)
            for index in range(4):
                grid.setColumnStretch(index, 0)
        for index, card in enumerate(self.cards):
            self.settings_grid.addWidget(card, index // columns, index % columns)
        for index, card in enumerate(self.preset_buttons.values()):
            self.presets_grid.addWidget(card, index // tiles, index % tiles)
        for index in range(columns):
            self.settings_grid.setColumnStretch(index, 1)
        for index in range(tiles):
            self.presets_grid.setColumnStretch(index, 1)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self.reflow)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange) and hasattr(self, 'theme_timer'):
            # Do not replace child stylesheets while Qt is traversing the
            # widget tree to repolish a global theme (can invalidate that walk).
            self.theme_timer.start(0)

    def apply_theme(self):
        dark = QApplication.palette().color(QPalette.ColorRole.Window).lightness() < 128
        if self._theme_signature == dark:
            return
        self._theme_signature = dark
        c = THEMES['dark' if dark else 'light']
        self.setStyleSheet('''
            QWidget#caption_editor, QWidget#caption_page_content, QStackedWidget, QScrollArea { background: transparent; }
            QLabel { background:transparent; }
            QLabel[caption_role="caption_section"] { color:%(text)s;font-size:14px;font-weight:600; }
            QLabel[caption_role="caption_muted"] { color:%(muted)s;font-size:11px;font-weight:400; }
            QLabel[caption_role="caption_hex"] { color:%(muted)s;font-size:11px;font-family:monospace; }
            QFrame[caption_role="settings_card"] { background:%(surface)s;border:1px solid %(border)s;border-radius:12px; }
            QPushButton[caption_role="quiet"], QPushButton[caption_role="reset"] { background:transparent;border:0;border-radius:6px;padding:4px 8px;min-height:22px;font-size:11px;color:%(muted)s;font-weight:400; }
            QPushButton[caption_role="quiet"]:hover, QPushButton[caption_role="reset"]:hover { background:%(hover)s;color:%(text)s; }
            QPushButton[caption_role="reset"] { font-size:20px;padding:0; }
            QPushButton[caption_role="color"] { background:%(surface)s;border:1px solid %(border)s;border-radius:6px;padding:4px;min-height:0; }
            QPushButton[caption_role="color"]:hover { border-color:%(accent)s; }
            QPushButton:focus { border:1px solid %(accent)s; }
            QComboBox, QSpinBox { background:%(surface)s;border:1px solid %(border)s;border-radius:7px;padding:6px 8px;min-height:20px;font-size:12px; }
            QComboBox { padding-right:20px; }
            QComboBox::drop-down { width:18px;border:0; }
            QSpinBox { padding:4px;min-height:20px; }
            QSpinBox::up-button, QSpinBox::down-button { width:0;height:0;border:0; }
            QSpinBox::up-arrow, QSpinBox::down-arrow { image:none;width:0;height:0; }
            QSpinBox QLineEdit { background:transparent;border:0;border-radius:0;padding:0;min-height:0;min-width:0;font-size:12px; }
            QTabBar::tab { background:transparent;color:%(muted)s;border:0;border-bottom:2px solid transparent;padding:9px 18px;font-size:13px; }
            QTabBar::tab:selected { color:%(accent)s;border-bottom-color:%(accent)s;font-weight:600; }
            QTabBar::tab:hover { background:%(hover)s; }
            QSlider { background:transparent;border:0; }
            QSlider::groove:horizontal { height:4px;background:%(border)s;border:0;border-radius:2px; }
            QSlider::sub-page:horizontal { background:%(accent)s;border-radius:2px; }
            QSlider::handle:horizontal { width:12px;height:12px;margin:-4px 0;background:%(accent)s;border:0;border-radius:6px; }
            QSlider::handle:horizontal:focus { border:2px solid %(text)s; }
        ''' % c)

    def toggle_preview_background(self):
        self.preview_stage.dark = not self.preview_stage.dark
        self.preview_stage.update()
        self.background_button.setText('切换浅色画面' if self.preview_stage.dark else '切换深色画面')

    def choose_color(self, key):
        color = QColorDialog.getColor(QColor(resolve_appearance(self.appearance.options)[key]), self, '选择字幕颜色')
        if color.isValid():
            self.appearance.update(**{key: color.name()})

    def refresh(self, options):
        for key, control in self.controls.items():
            control.blockSignals(True)
            if key == 'style':
                control.setCurrentIndex(control.findData(options[key]))
            elif key == 'font_family':
                control.setCurrentFont(QFont(options[key]))
            elif isinstance(control, QCheckBox):
                control.setChecked(options[key])
            else:
                control.setValue(options[key])
            control.blockSignals(False)
        resolved = resolve_appearance(options)
        for key in ('background_opacity', 'radius'):
            self.controls[key].setEnabled(not options['transparent_background'])
        self.colors['background'].setEnabled(not options['transparent_background'])
        for key, control in self.colors.items():
            color = resolved[key]
            swatch = QPixmap(22, 22)
            swatch.fill(Qt.GlobalColor.transparent)
            painter = QPainter(swatch)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor('#AAB5AE'), 1))
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(QRectF(1, 1, 20, 20), 3, 3)
            painter.end()
            control.setIcon(QIcon(swatch))
            control.setToolTip(color.upper() + ' · 点击选择颜色')
            self.color_labels[key].setText(color.upper())
        for key, card in self.preset_buttons.items():
            card.setChecked(key == options['style'])
        self.current_preset_label.setText('当前风格 · ' + STYLES[options['style']][0])
        self.preview.apply_appearance(options)
        self.preview_stage.fit_surface()


class OverlayDesktopHelp(QWidget):
    """Inspect the desktop and copy explicit rules without modifying configuration."""
    def __init__(self, parent=None):
        super().__init__(parent)
        from overlay_platform import detect_desktop, desktop_help, preferred_rule_syntax
        self.info = detect_desktop(QApplication.platformName())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        note = QLabel(desktop_help(self.info))
        note.setWordWrap(True)
        layout.addWidget(note)
        bar = QHBoxLayout()
        self.syntax = WheelSafeComboBox()
        for title, value in (('Hyprland 0.55+ · Lua', 'lua'), ('Hyprland 0.53–0.54 · conf', 'conf'),
                             ('Hyprland 旧版 · windowrulev2', 'legacy')):
            self.syntax.addItem(title, value)
        self.syntax.setCurrentIndex(max(0, self.syntax.findData(preferred_rule_syntax(self.info))))
        self.syntax.setAccessibleName('Hyprland 配置格式')
        bar.addWidget(self.syntax)
        copy = QPushButton('复制悬浮窗口规则')
        copy.clicked.connect(self.copy_rules)
        bar.addWidget(copy)
        layout.addLayout(bar)
        self.feedback = QLabel('')
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)

    def copy_rules(self):
        from overlay_platform import hyprland_rules
        QApplication.clipboard().setText(hyprland_rules(syntax=self.syntax.currentData()))
        self.feedback.setText('已复制实时字幕和歌词规则。将其加入对应 Hyprland 配置后重新加载；应用没有修改桌面配置。')


class CaptionSettingsDialog(QDialog):
    """Keep the editor out of the overlay and within the current work area."""
    def showEvent(self, event):
        screen = QApplication.screenAt(QCursor.pos()) or self.screen()
        if screen:
            area = screen.availableGeometry().adjusted(16, 24, -16, -24)
            self.setMaximumSize(area.size())
            self.resize(min(self.width(), area.width()), min(self.height(), area.height()))
            if self.windowHandle():
                self.windowHandle().setScreen(screen)
            if not QApplication.platformName().startswith('wayland'):
                self.move(area.center() - self.rect().center())
        super().showEvent(event)


def appearance_dialog(owner, appearance):
    dialog = CaptionSettingsDialog(owner)
    title = '歌词外观' if appearance.prefix.startswith('music') else '实时字幕外观'
    dialog.setWindowTitle(title)
    dialog.resize(1040, 800)
    dialog.setMinimumSize(320, 240)
    outer = QVBoxLayout(dialog)
    outer.setContentsMargins(24, 20, 24, 16)
    outer.setSpacing(16)
    heading = QLabel(title)
    heading.setProperty('role', 'title')
    outer.addWidget(heading)
    editor = CaptionAppearanceEditor(appearance, compact=True)
    dialog.editor = editor
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setWidget(editor)
    dialog.editor_scroll = scroll
    outer.addWidget(scroll, 1)
    footer = QHBoxLayout()
    help_button = QPushButton('桌面悬浮设置')
    help_button.setProperty('role', 'quiet')
    def show_desktop_help():
        if not hasattr(dialog, 'desktop_help_dialog'):
            help_dialog = QDialog(dialog)
            help_dialog.setWindowTitle('桌面悬浮设置')
            help_dialog.resize(620, 340)
            help_layout = QVBoxLayout(help_dialog)
            help_layout.setContentsMargins(20, 20, 20, 16)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidget(OverlayDesktopHelp())
            help_layout.addWidget(scroll, 1)
            done = QPushButton('完成')
            done.clicked.connect(help_dialog.accept)
            help_layout.addWidget(done, 0, Qt.AlignmentFlag.AlignRight)
            dialog.desktop_help_dialog = help_dialog
        dialog.desktop_help_dialog.show()
        dialog.desktop_help_dialog.raise_()
        dialog.desktop_help_dialog.activateWindow()
    help_button.clicked.connect(show_desktop_help)
    footer.addWidget(help_button)
    footer.addStretch()
    close = QPushButton('完成')
    close.setProperty('role', 'primary')
    close.setFixedWidth(100)
    close.clicked.connect(dialog.accept)
    footer.addWidget(close)
    outer.addLayout(footer)
    return dialog
