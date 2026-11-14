"""Native widgets and page composition; translation stays in native_app."""

from html import escape

from PyQt6.QtCore import Qt, QSize, QTimer
from PyQt6.QtGui import QColor, QIcon, QPainter, QPalette, QPen, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QScrollArea,
    QSizePolicy, QSplitter, QStackedWidget, QTableWidget, QTextEdit,
    QVBoxLayout, QWidget, QApplication, QSplitterHandle,
)

from ui_controls import WheelSafeComboBox, WheelSafeSpinBox

PAGE_TITLES = ("实时翻译", "历史记录", "声音与设备", "偏好设置", "视频字幕", "网页翻译", "NAS 自动字幕", "PDF 翻译", "音乐与歌词")


def label(text, role=None, wrap=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if role:
        widget.setProperty("role", role)
    widget.setWordWrap(wrap)
    return widget


def button(text, callback=None, role=None):
    widget = QPushButton(text)
    if role:
        widget.setProperty("role", role)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    if callback:
        widget.clicked.connect(callback)
    return widget


def infer_card_icon(title):
    text = title or ""
    for kind, words in (
        (9, ("外观",)),
        (8, ("声音风格", "语音同传")),
        (0, ("字幕与文档", "开始翻译", "会议")),
        (2, ("耳机", "音频", "设备", "语音识别")),
        (10, ("在线搜索", "搜索")),
        (11, ("播放与导出",)),
        (4, ("视频", "字幕来源")),
        (5, ("网页", "Firefox", "扩展")),
        (6, ("NAS", "同步", "影视库", "Radarr", "Sonarr", "策略")),
        (7, ("PDF", "文档", "结果")),
        (1, ("时间轴", "历史")),
    ):
        if any(word in text for word in words):
            return kind
    return 3


class CardIcon(QLabel):
    """Mobile-style rounded icon badge (SettingsHeading) that adapts to light/dark themes."""

    def __init__(self, kind=3):
        super().__init__()
        self.kind = kind
        self.setProperty("role", "card_icon")
        self.setFixedSize(40, 40)

    def paintEvent(self, event):
        super().paintEvent(event)
        color = self.palette().color(QPalette.ColorRole.Highlight).name()
        icon = nav_icon(self.kind, color)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        icon.paint(p, 9, 9, 22, 22)
        p.end()


def card(title=None, subtitle=None, role="card", icon=None):
    widget = QFrame()
    widget.setProperty("role", role)
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(24, 22, 24, 22)
    layout.setSpacing(14)
    if title:
        header = QHBoxLayout()
        header.setSpacing(12)
        badge = CardIcon(infer_card_icon(title) if icon is None else icon)
        header.addWidget(badge, alignment=Qt.AlignmentFlag.AlignTop)
        titles = QVBoxLayout()
        titles.setContentsMargins(0, 0, 0, 0)
        titles.setSpacing(3)
        titles.addWidget(label(title, "section"))
        if subtitle:
            titles.addWidget(label(subtitle, "muted", True))
        header.addLayout(titles, 1)
        layout.addLayout(header)
    elif subtitle:
        layout.addWidget(label(subtitle, "muted", True))
    return widget, layout


def advanced_section(title="高级设置", subtitle=None, expanded=False):
    wrapper = QWidget()
    outer = QVBoxLayout(wrapper)
    outer.setContentsMargins(0, 4, 0, 0)
    outer.setSpacing(10)
    caption = f"{title}  ·  {subtitle}" if subtitle else title
    toggle = button(("▾  " if expanded else "▸  ") + caption, role="disclosure")
    toggle.setCheckable(True)
    toggle.setChecked(expanded)
    toggle.setAccessibleName(title)
    body = QFrame()
    body.setProperty("role", "soft")
    content = QVBoxLayout(body)
    content.setContentsMargins(20, 18, 20, 18)
    content.setSpacing(16)
    body.setVisible(expanded)

    def on_toggle(checked):
        body.setVisible(checked)
        toggle.setText(("▾  " if checked else "▸  ") + caption)

    toggle.toggled.connect(on_toggle)
    outer.addWidget(toggle)
    outer.addWidget(body)
    return wrapper, content


def field(layout, title, control, hint=None):
    control.setAccessibleName(title)
    layout.addWidget(label(title, "secondary"))
    layout.addWidget(control)
    if hint:
        layout.addWidget(label(hint, "muted", True))


def row(*widgets):
    layout = QHBoxLayout()
    layout.setSpacing(10)
    for widget in widgets:
        if widget is None:
            layout.addStretch()
        else:
            layout.addWidget(widget)
    return layout


class Select(WheelSafeComboBox):
    """Keep the disclosure arrow legible with both palettes and Qt styles."""

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        group = QPalette.ColorGroup.Active if self.isEnabled() else QPalette.ColorGroup.Disabled
        p.setPen(QPen(self.palette().color(group, QPalette.ColorRole.Text), 1.4))
        x, y = self.width() - 18, self.height() // 2
        p.drawLine(x - 4, y - 2, x, y + 2)
        p.drawLine(x, y + 2, x + 4, y - 2)
        p.end()


def scroll_page():
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    content = QWidget()
    content.setObjectName("root")
    layout = QVBoxLayout(content)
    layout.setContentsMargins(32, 26, 32, 28)
    layout.setSpacing(20)
    scroll.setWidget(content)
    return scroll, layout


def nav_icon(kind, color):
    pix = QPixmap(40, 40)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(2, 2)
    p.setPen(QPen(QColor(color), 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    if kind == 0:
        p.drawRoundedRect(2, 3, 16, 12, 3, 3)
        p.drawLine(6, 15, 6, 18)
        p.drawLine(6, 18, 10, 15)
        p.drawLine(6, 7, 14, 7)
        p.drawLine(6, 11, 11, 11)
    elif kind == 1:
        p.drawEllipse(2, 2, 16, 16)
        p.drawLine(10, 5, 10, 10)
        p.drawLine(10, 10, 14, 12)
    elif kind == 2:
        p.drawArc(3, 2, 14, 16, 0, 180 * 16)
        p.drawRoundedRect(2, 9, 4, 8, 2, 2)
        p.drawRoundedRect(14, 9, 4, 8, 2, 2)
    elif kind == 4:
        p.drawRoundedRect(2, 3, 16, 14, 2, 2)
        p.drawLine(8, 7, 13, 10)
        p.drawLine(13, 10, 8, 13)
        p.drawLine(8, 13, 8, 7)
    elif kind == 5:
        p.drawEllipse(2, 2, 16, 16)
        p.drawEllipse(6, 2, 8, 16)
        p.drawLine(2, 10, 18, 10)
    elif kind == 6:
        for y in (3, 11):
            p.drawRoundedRect(2, y, 16, 6, 2, 2)
            p.drawPoint(5, y + 3)
            p.drawLine(10, y + 3, 15, y + 3)
    elif kind == 7:
        p.drawRoundedRect(4, 2, 12, 16, 1, 1)
        for y in (7, 10, 13):
            p.drawLine(7, y, 13, y)
    elif kind == 8:
        for x, h in ((4, 6), (8, 12), (12, 10), (16, 6)):
            p.drawLine(x, 10 - h // 2, x, 10 + h // 2)
    elif kind == 9:
        p.drawEllipse(5, 5, 10, 10)
        for x1, y1, x2, y2 in ((10, 1, 10, 3), (10, 17, 10, 19), (1, 10, 3, 10), (17, 10, 19, 10)):
            p.drawLine(x1, y1, x2, y2)
    elif kind == 10:
        p.drawEllipse(3, 3, 11, 11)
        p.drawLine(12, 12, 17, 17)
    elif kind == 11:
        p.drawEllipse(2, 2, 16, 16)
        p.drawLine(8, 7, 14, 10)
        p.drawLine(14, 10, 8, 13)
        p.drawLine(8, 13, 8, 7)
    else:
        for y, x in ((5, 7), (10, 14), (15, 9)):
            p.drawLine(2, y, 18, y)
            p.setBrush(QColor(color))
            p.drawEllipse(x - 2, y - 2, 4, 4)
    p.end()
    return QIcon(pix)


class TranscriptPanel(QFrame):
    def __init__(self, title, languages, empty_title, empty_hint):
        super().__init__()
        self.setProperty("role", "card")
        self.setMinimumWidth(210)
        self.setMinimumHeight(260)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 16)
        layout.setSpacing(16)
        layout.addLayout(row(label(title, "section"), None, label(languages, "badge")))
        self.stack = QStackedWidget()
        self.stack.setObjectName("transcript_stack")
        empty = QWidget()
        empty_layout = QVBoxLayout(empty)
        empty_layout.addStretch()
        symbol = label("Aa", "empty_mark")
        symbol.setFixedSize(54, 54)
        symbol.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(symbol, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.empty_title = label(empty_title, "section", True)
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addSpacing(8)
        empty_layout.addWidget(self.empty_title)
        hint = label(empty_hint, "muted", True)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(hint)
        empty_layout.addStretch()
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setAccessibleName(title + "字幕")
        self.text.document().setMaximumBlockCount(600)
        self.stack.addWidget(empty)
        self.stack.addWidget(self.text)
        layout.addWidget(self.stack, 1)
        self.footer = label("原文与译文将按顺序显示", "muted")
        self.copy_button = button("复制", self.copy_text, "quiet")
        self.copy_button.setAccessibleName(title + " · 复制字幕")
        self.copy_button.setToolTip("复制本面板的原文与译文")
        self.copy_button.setEnabled(False)
        self.copy_feedback_timer = QTimer(self)
        self.copy_feedback_timer.setSingleShot(True)
        self.copy_feedback_timer.timeout.connect(self.reset_copy_feedback)
        self.clear_button = button("清空", self.clear, "quiet")
        self.clear_button.setAccessibleName(title + " · 清空字幕")
        self.clear_button.setEnabled(False)
        layout.addWidget(self.footer)
        layout.addLayout(row(None, self.copy_button, self.clear_button))
        self.entries = []
        self.entry_ids = []
        self.colors = None

    def append_entry(self, kind, text, timestamp, segment_id=None):
        position = len(self.entries)
        if kind == "译文" and segment_id is not None and segment_id in self.entry_ids:
            position = self.entry_ids.index(segment_id) + 1
        self.entries.insert(position, (kind, text, timestamp))
        self.entry_ids.insert(position, segment_id)
        self.entries = self.entries[-150:]
        self.entry_ids = self.entry_ids[-150:]
        self.render()

    def render(self):
        if not self.colors:
            return
        bar = self.text.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 12
        old_value = bar.value()
        c = self.colors
        html = []
        for kind, text, timestamp in self.entries:
            color = c["text"] if kind == "译文" else c["secondary"]
            html.append(f'<p style="color:{c["muted"]};font-size:11px;margin-top:18px;margin-bottom:7px">'
                        f'{escape(timestamp)} &nbsp; · &nbsp; {kind}</p>'
                        f'<p style="color:{color};font-size:16px;line-height:150%;margin-bottom:18px">'
                        f'{escape(text).replace(chr(10), "<br>")}</p>')
        self.text.setHtml("".join(html))
        self.stack.setCurrentIndex(bool(self.entries))
        self.copy_button.setEnabled(bool(self.entries))
        self.clear_button.setEnabled(bool(self.entries))
        bar.setValue(bar.maximum() if follow else old_value)
        count = sum(kind == "译文" for kind, _, _ in self.entries)
        self.footer.setText(f"{count} 段译文 · 仅保留在本次窗口中" if self.entries else "原文与译文将按顺序显示")

    def copy_text(self):
        QApplication.clipboard().setText("\n\n".join(
            f"{kind} · {timestamp}\n{text}" for kind, text, timestamp in self.entries))
        self.copy_button.setText("已复制")
        self.copy_feedback_timer.start(1600)

    def reset_copy_feedback(self):
        self.copy_button.setText("复制")

    def clear(self):
        self.copy_feedback_timer.stop()
        self.reset_copy_feedback()
        self.entries.clear()
        self.entry_ids.clear()
        self.render()


class ReadingHandle(QSplitterHandle):
    """A visible, focusable splitter with mouse and arrow-key resizing."""
    def __init__(self, orientation, splitter):
        super().__init__(orientation, splitter)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("调整字幕分栏，方向键调整，Home 恢复均分")
        self.setToolTip("拖动调整分栏 · 双击恢复均分 · 支持方向键")

    def keyPressEvent(self, event):
        sizes = self.splitter().sizes()
        if len(sizes) != 2 or not all(sizes):
            return super().keyPressEvent(event)
        horizontal = self.orientation() == Qt.Orientation.Horizontal
        previous, following = ((Qt.Key.Key_Left, Qt.Key.Key_Right) if horizontal
                               else (Qt.Key.Key_Up, Qt.Key.Key_Down))
        if event.key() == Qt.Key.Key_Home:
            self.splitter().setSizes([sum(sizes) // 2] * 2)
        elif event.key() in (previous, following):
            delta = -32 if event.key() == previous else 32
            self.splitter().setSizes([max(1, sizes[0] + delta), max(1, sizes[1] - delta)])
        else:
            return super().keyPressEvent(event)
        self.splitter().splitterMoved.emit(self.pos().x() if horizontal else self.pos().y(), 1)
        event.accept()

    def mouseDoubleClickEvent(self, event):
        sizes = self.splitter().sizes()
        self.splitter().setSizes([sum(sizes) // 2] * 2)
        self.splitter().splitterMoved.emit(0, 1)
        event.accept()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(self.palette().color(QPalette.ColorRole.Highlight if self.hasFocus()
                                                 else QPalette.ColorRole.Mid), 3,
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        x, y = self.width() // 2, self.height() // 2
        if self.orientation() == Qt.Orientation.Horizontal:
            painter.drawLine(x, y - 16, x, y + 16)
        else:
            painter.drawLine(x - 16, y, x + 16, y)
        painter.end()


class ReadingSplitter(QSplitter):
    def createHandle(self):
        return ReadingHandle(self.orientation(), self)


def build_window(w, modes):
    root = QWidget()
    root.setObjectName("root")
    w.setCentralWidget(root)
    shell = QHBoxLayout(root)
    shell.setContentsMargins(0, 0, 0, 0)
    shell.setSpacing(0)
    w.sidebar = QWidget()
    w.sidebar.setObjectName("sidebar")
    w.sidebar.setFixedWidth(218)
    side = QVBoxLayout(w.sidebar)
    side.setContentsMargins(16, 22, 16, 16)
    side.setSpacing(6)
    mark = label("译", "mark")
    mark.setFixedSize(40, 40)
    mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
    w.brand_text = QWidget()
    brand = QVBoxLayout(w.brand_text)
    brand.setContentsMargins(0, 0, 0, 0)
    brand.setSpacing(2)
    brand.addWidget(label("同声", "brand"))
    brand.addWidget(label("智能翻译与字幕助手", "eyebrow"))
    side.addLayout(row(mark, w.brand_text))
    side.addSpacing(20)
    w.nav_caption = label("日常交流", "eyebrow")
    side.addWidget(w.nav_caption)
    w.nav_buttons = []
    group = QButtonGroup(w)
    group.setExclusive(True)
    for index, title in enumerate(PAGE_TITLES):
        if index == 4:
            side.addSpacing(10)
            w.media_nav_caption = label("视频与文档", "eyebrow")
            side.addWidget(w.media_nav_caption)
        b = button(title, lambda checked=False, i=index: w.navigate(i), "nav")
        b.setCheckable(True)
        b.setIconSize(QSize(20, 20))
        b.setToolTip(title)
        b.setAccessibleName(title)
        b.setMinimumHeight(40)
        group.addButton(b)
        side.addWidget(b)
        w.nav_buttons.append(b)
    w.nav_buttons[0].setChecked(True)
    side.addStretch()
    w.sidebar_note = label("让语言退后一步。\n让交流更近一步。", "muted", True)
    side.addWidget(w.sidebar_note)
    side.addSpacing(8)
    w.theme_button = button("切换深色外观", w.toggle_theme, "quiet")
    side.addWidget(w.theme_button)
    shell.addWidget(w.sidebar)
    body = QVBoxLayout()
    body.setSpacing(0)
    body.setContentsMargins(0, 0, 0, 0)
    top = QWidget()
    top.setObjectName("topbar")
    top.setFixedHeight(64)
    top_layout = QHBoxLayout(top)
    top_layout.setContentsMargins(32, 0, 32, 0)
    w.breadcrumb = label("同声  ·  实时翻译", "secondary")
    top_layout.addWidget(w.breadcrumb)
    top_layout.addStretch()
    w.badge = label("准备中", "badge")
    w.badge.setFixedHeight(30)
    top_layout.addWidget(w.badge)
    body.addWidget(top)
    w.pages = QStackedWidget()
    body.addWidget(w.pages, 1)
    shell.addLayout(body, 1)
    w.config_widgets = []
    w.fields = {}

    # Live page: reading space first, configuration lives on separate pages.
    live = QWidget()
    live.setObjectName("root")
    w.live_layout = QVBoxLayout(live)
    layout = w.live_layout
    layout.setContentsMargins(32, 24, 32, 22)
    layout.setSpacing(18)
    title = QVBoxLayout()
    title.setSpacing(5)
    title.addWidget(label("实时翻译", "title"))
    title.addWidget(label("无论是外语会议、追剧看直播，还是面对面交谈，都能实时听懂与回应。", "muted"))
    heading = QHBoxLayout()
    heading.addLayout(title)
    heading.addStretch()
    w.caption_button = button("悬浮字幕", w.toggle_captions)
    heading.addWidget(w.caption_button)
    layout.addLayout(heading)

    w.onboarding, intro = card(role="soft")
    intro.setContentsMargins(20, 16, 20, 16)
    intro.addWidget(label("三步开启第一次翻译", "section"))
    intro.addWidget(label("首次使用只需确认服务与耳机设备，然后说一句中文即可试听英文译音。", "muted", True))
    w.setup_buttons = [button("1  配置翻译服务", lambda: w.navigate(3)),
                       button("2  选择耳机与麦克风", lambda: w.navigate(2)),
                       button("3  说句话试试", w.prepare_test)]
    intro.addLayout(row(*w.setup_buttons))
    intro.addWidget(button("先收起引导", w.dismiss_onboarding, "quiet"), alignment=Qt.AlignmentFlag.AlignRight)
    layout.addWidget(w.onboarding)
    w.onboarding.setVisible(not w.settings.value("onboarded", False, type=bool))

    w.error_card, error_layout = card(role="error")
    w.error_title = label("暂时无法开始翻译", "section", True)
    w.error_detail = label("", "secondary", True)
    error_layout.addWidget(w.error_title)
    error_layout.addWidget(w.error_detail)
    w.recovery_button = button("检查设置", w.recover)
    error_layout.addWidget(w.recovery_button, alignment=Qt.AlignmentFlag.AlignLeft)
    w.error_card.hide()
    layout.addWidget(w.error_card)

    session_card, session_layout = card()
    session_layout.setContentsMargins(22, 18, 22, 18)
    w.session_actions = QHBoxLayout()
    mode = Select()
    mode.setAccessibleName("翻译模式")
    mode.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    mode.setMinimumContentsLength(12)
    for name, value in modes:
        mode.addItem(name, value)
    w.restore(mode, "mode")
    w.fields["mode"] = mode
    w.config_widgets.append(mode)
    w.session_actions.addWidget(mode, 1)
    w.start_button = button("开始翻译", w.start, "primary")
    w.start_button.setEnabled(False)
    w.stop_button = button("结束翻译", w.stop, "stop")
    w.stop_button.hide()
    w.session_actions.addWidget(w.start_button)
    w.session_actions.addWidget(w.stop_button)
    session_layout.addLayout(w.session_actions)
    w.subtitle_options = QWidget()
    subtitle_options = QVBoxLayout(w.subtitle_options)
    subtitle_options.setContentsMargins(0, 0, 0, 0)
    subtitle_inputs = QHBoxLayout()
    source_column = QVBoxLayout()
    language_column = QVBoxLayout()
    source = Select()
    source.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    source.setMinimumContentsLength(15)
    w.fields["subtitle_source"] = source
    field(source_column, "收音来源", source)
    language = Select()
    language.addItem("听英文 → 看中文字幕", "en-zh")
    language.addItem("说中文 → 看英文字幕", "zh-en")
    w.restore(language, "subtitle_language")
    w.fields["subtitle_language"] = language
    field(language_column, "翻译方向", language)
    subtitle_inputs.addLayout(source_column, 2)
    subtitle_inputs.addLayout(language_column, 1)
    subtitle_options.addLayout(subtitle_inputs)
    from subtitle_settings import SUBTITLE_PROFILES
    pacing = Select()
    for key, profile in SUBTITLE_PROFILES.items():
        pacing.addItem(profile['title'], key)
    w.restore(pacing, "subtitle_profile")
    w.fields["subtitle_profile"] = pacing
    field(subtitle_options, "出字速度", pacing)
    w.subtitle_profile_hint = label("", "muted", True)
    subtitle_options.addWidget(w.subtitle_profile_hint)
    w.config_widgets.extend([source, language, pacing])
    subtitle_options.addLayout(row(label("默认跟随电脑当前播放的声音，视频或会议原声照常播放。", "muted", True),
                                   button("切换翻译服务", lambda: w.navigate(3), "quiet")))
    session_layout.addWidget(w.subtitle_options)
    w.subtitle_options.hide()
    w.route_label = label("正在准备音频设备…", "secondary", True)
    session_layout.addWidget(w.route_label)
    w.route_hint = label("", "muted", True)
    session_layout.addWidget(w.route_hint)
    layout.addWidget(session_card)

    w.metrics = QHBoxLayout()
    w.metrics.setSpacing(12)
    w.metric_labels = []
    w.metric_titles = []
    for name, value in (("播放设备", "等待设备"), ("本次时长", "00:00"), ("响应速度", "暂未测量"), ("隐私保护", "不存录音与字幕")):
        box = QFrame()
        box.setProperty("role", "metric")
        col = QVBoxLayout(box)
        col.setContentsMargins(14, 10, 14, 10)
        col.setSpacing(4)
        metric_title = label(name, "eyebrow")
        col.addWidget(metric_title)
        w.metric_titles.append(metric_title)
        value_label = label(value, "secondary")
        value_label.setMinimumWidth(0)
        value_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        col.addWidget(value_label)
        w.metric_labels.append(value_label)
        w.metrics.addWidget(box, 1)
    w.metric_labels[2].setToolTip("当前翻译服务未提供可靠的端到端延迟数据。")
    layout.addLayout(w.metrics)
    w.transcript_split = ReadingSplitter()
    w.transcript_split.setHandleWidth(12)
    w.transcript_split.setChildrenCollapsible(False)
    w.transcripts = {}
    for direction, title, language, empty, hint in (
        ("inbound", "听懂对方", "英 → 中", "对方说的话会实时显示在这里", "开启英译中后，这里会按顺序展示外语原文与中文翻译。"),
        ("outbound", "表达自己", "中 → 英", "你说的话会实时翻译成英文", "开启中译英后，这里会按顺序展示中文原文与英文翻译。"),
    ):
        panel = TranscriptPanel(title, language, empty, hint)
        w.transcripts[direction] = panel
        w.transcript_split.addWidget(panel)
    layout.addWidget(w.transcript_split, 1)
    w.status = label("正在准备音频设备…", "muted", True)
    layout.addWidget(w.status)
    w.privacy_note = label("开始后将实时处理语音；停止或关闭软件即可随时结束。", "muted", True)
    layout.addWidget(w.privacy_note)
    live_adv, live_adv_col = advanced_section("高级运行详情", "查看每段识别耗时、排队状态与模型用量")
    w.token_usage = label("", "muted", True)
    w.pipeline_status = label("", "muted", True)
    live_adv_col.addWidget(w.token_usage)
    live_adv_col.addWidget(w.pipeline_status)
    live_adv_col.addWidget(label("仅在「实时字幕」模式下统计本地语音识别与文本翻译的分段耗时。", "muted", True))
    layout.addWidget(live_adv)
    live_scroll = QScrollArea()
    live_scroll.setWidgetResizable(True)
    live_scroll.setWidget(live)
    w.pages.addWidget(live_scroll)

    history, history_layout = scroll_page()
    history_layout.addWidget(label("历史记录", "title"))
    history_layout.addWidget(label("查看最近的使用时间、时长和播放设备，对话内容不会留存。", "muted"))
    w.history_search = QLineEdit()
    w.history_search.setPlaceholderText("搜索翻译模式、设备或状态")
    w.history_search.setAccessibleName("搜索会话记录")
    w.history_filter = Select()
    w.history_filter.setAccessibleName("记录时间范围")
    for name, days in (("全部时间", 0), ("最近 7 天", 7), ("最近 30 天", 30)):
        w.history_filter.addItem(name, days)
    history_layout.addLayout(row(w.history_search, w.history_filter))
    hcard, hcol = card()
    w.history_table = QTableWidget(0, 5)
    w.history_table.setHorizontalHeaderLabels(["开始时间", "翻译模式", "时长", "输出设备", "状态"])
    w.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    w.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    w.history_table.setShowGrid(False)
    w.history_table.verticalHeader().hide()
    w.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    w.history_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
    w.history_table.setMinimumHeight(280)
    w.history_empty = label("还没有会话记录\n结束一次翻译后，时间、时长和设备会显示在这里。", "muted", True)
    w.history_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
    w.history_empty.setMinimumHeight(200)
    hcol.addWidget(w.history_table)
    hcol.addWidget(w.history_empty)
    history_layout.addWidget(hcard, 1)
    history_layout.addWidget(label("仅在本机保存最近 100 次使用记录的时间与时长，不记录任何录音、字幕或密钥。", "muted", True))
    w.history_search.textChanged.connect(w.render_history)
    w.history_filter.currentIndexChanged.connect(w.render_history)
    w.pages.addWidget(history)

    devices, device_layout = scroll_page()
    device_layout.addWidget(label("声音与设备", "title"))
    device_layout.addWidget(label("选择你正在使用的耳机（或扬声器）和麦克风，默认自动匹配系统当前设备。", "muted", True))
    device_card, device_col = card("耳机与麦克风", "翻译后的语音和视频声音会播放到所选耳机或扬声器。")
    voice_card, voice_col = card("翻译声音风格", "选择自然亲切的预设人声，也可以让声音模仿当前说话人。")
    for name, title in (("headphones", "耳机 / 扬声器"), ("microphone", "麦克风"),
                        ("inbound_voice", "中文译音人声"), ("outbound_voice", "英文译音人声")):
        combo = Select()
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(15)
        w.fields[name] = combo
        w.config_widgets.append(combo)
        field(device_col if name in {"headphones", "microphone"} else voice_col, title, combo)
    w.refresh_button = button("刷新设备", w.refresh)
    w.test_button = button("说句话试听", w.prepare_test)
    device_col.addLayout(row(w.refresh_button, w.test_button, None))
    device_col.addWidget(label("点击「说句话试听」会切回实时翻译页：点击开始后说一句中文，即可在耳机里听英文译音。", "muted", True))
    w.device_status = label("正在准备音频设备…", "muted", True)
    device_col.addWidget(w.device_status)
    w.config_widgets.extend([w.refresh_button, w.test_button])
    device_layout.addWidget(device_card)
    device_layout.addWidget(voice_card)
    device_adv, device_adv_col = advanced_section("高级设置 · 本地字幕识别与会议软件连接",
                                                  "硬件加速方式、收音灵敏度调节，以及线上会议软件连接说明")
    asr_card, asr_layout = card("本地语音识别设置", "用于「实时字幕」和「视频字幕」的本机离线语音识别。")
    w.asr_status = label("正在检查本地识别组件…", "muted", True)
    asr_layout.addWidget(w.asr_status)
    backend = Select()
    backend.addItem("显卡加速（推荐，更快更省电）", "vulkan")
    backend.addItem("处理器模式（兼容备用）", "cpu")
    w.restore(backend, "asr_backend")
    w.fields["asr_backend"] = backend
    field(asr_layout, "加速方式", backend)
    w.vad_threshold = WheelSafeSpinBox()
    w.vad_threshold.setRange(50, 3000)
    w.vad_threshold.setValue(w.settings.value("vad_threshold", 220, type=int))
    field(asr_layout, "收音灵敏度（防杂音阈值）", w.vad_threshold, "周围环境较吵、容易误触发时调高；轻声说话漏识别时调低。")
    w.config_widgets.extend([backend, w.vad_threshold])
    device_adv_col.addWidget(asr_card)
    routing, routing_col = card("线上会议软件连接指南")
    routing_col.addWidget(label("• 平时看视频：电脑和浏览器保持你自己的耳机或扬声器即可。\n• 听懂会议里的外语：把会议软件或浏览器的「扬声器/输出」设为 meeting-remote，再开启「只听对方」或「双向同传」。\n• 让对方听到英文译音：把会议软件的「麦克风/输入」设为 Monitor of meeting-translator。", "secondary", True))
    routing_col.addWidget(label("提示：会议结束后若电脑没声音，只需把系统声音输出切回你平时的耳机或扬声器。", "muted", True))
    device_adv_col.addWidget(routing)
    device_layout.addWidget(device_adv)
    device_layout.addStretch()
    w.pages.addWidget(devices)

    settings, settings_layout = scroll_page()
    settings_layout.addWidget(label("偏好设置", "title"))
    settings_layout.addWidget(label("管理界面外观、翻译服务账号与高级选项。", "muted", True))
    appearance, acol = card("界面外观", "在明亮柔和的浅色与舒适护眼的深色之间切换。")
    w.theme_combo = Select()
    w.theme_combo.addItem("浅色 · 清新柔和", "light")
    w.theme_combo.addItem("深色 · 护眼深色", "dark")
    field(acol, "界面主题", w.theme_combo)
    settings_layout.addWidget(appearance)
    caption_card, caption_col = advanced_section('实时字幕外观', '预设、字体、颜色、透明度与双语顺序')
    from caption_settings import CaptionAppearanceEditor, OverlayDesktopHelp
    w.caption_appearance_editor = CaptionAppearanceEditor(w.overlay.appearance)
    caption_col.addWidget(w.caption_appearance_editor)
    caption_col.addLayout(row(button('显示 / 隐藏字幕窗口', w.toggle_captions), None))
    caption_col.addWidget(OverlayDesktopHelp())
    settings_layout.addWidget(caption_card)
    text_card, text_col = card("字幕与文档翻译服务", "用于「实时字幕、视频字幕、网页翻译、PDF 翻译」，推荐直接使用电脑上已登录的账号。")
    w.text_fields = {}
    provider = Select()
    provider.addItem("本机 Codex 账号（免填密钥 · 推荐）", "codex")
    provider.addItem("本机 Antigravity 账号（免填密钥）", "antigravity")
    provider.addItem("自定义在线服务 (兼容 API)", "api")
    provider.addItem("仅显示原文（不进行翻译）", "none")
    w.text_fields["text_provider"] = provider
    field(text_col, "翻译方式", provider)
    parallel = Select()
    for count in range(1, 9):
        parallel.addItem(f"{count} 个任务" + ("（推荐）" if count == 4 else ""), count)
    parallel.setCurrentIndex(3)
    w.text_fields["translation_concurrency"] = parallel
    w.restore(parallel, "translation_concurrency")
    parallel.currentIndexChanged.connect(lambda: w.settings.setValue("translation_concurrency", parallel.currentData()))
    w.codex_options = QWidget()
    codex_col = QVBoxLayout(w.codex_options)
    codex_col.setContentsMargins(0, 0, 0, 0)
    codex_model = Select()
    saved_model = w.settings.value("codex_model", "gpt-5.6-luna") or "gpt-5.6-luna"
    codex_model.addItem(saved_model + " · 待确认", saved_model)
    w.text_fields["codex_model"] = codex_model
    field(codex_col, "选择模型", codex_model, "直接使用电脑已登录的 Codex 账号，无需填写密钥。")
    codex_effort = Select()
    codex_effort.addItem("模型默认", "default")
    w.text_fields["codex_reasoning_effort"] = codex_effort
    w.codex_refresh_button = button("刷新模型列表", w.refresh_codex_models)
    w.codex_model_status = label("自动读取本机可用模型，也可点击手动刷新。", "muted", True)
    codex_col.addWidget(w.codex_refresh_button, alignment=Qt.AlignmentFlag.AlignLeft)
    codex_col.addWidget(w.codex_model_status)
    w.config_widgets.append(w.codex_refresh_button)
    text_col.addWidget(w.codex_options)
    w.antigravity_options = QWidget()
    agy_col = QVBoxLayout(w.antigravity_options)
    agy_col.setContentsMargins(0, 0, 0, 0)
    agy_model = Select()
    saved_agy = w.settings.value("antigravity_model", "") or ""
    agy_model.addItem(saved_agy + " · 待确认" if saved_agy else "请刷新模型列表", saved_agy)
    w.text_fields["antigravity_model"] = agy_model
    field(agy_col, "选择模型", agy_model,
          "直接使用电脑已登录的 Antigravity 账号，无需填写密钥。")
    agy_model.currentIndexChanged.connect(lambda: w.settings.setValue("antigravity_model", agy_model.currentData()))
    w.antigravity_refresh_button = button("刷新模型列表", w.refresh_antigravity_models)
    w.antigravity_model_status = label("自动读取本机 Antigravity 可用模型。", "muted", True)
    agy_col.addWidget(w.antigravity_refresh_button, alignment=Qt.AlignmentFlag.AlignLeft)
    agy_col.addWidget(w.antigravity_model_status)
    w.config_widgets.append(w.antigravity_refresh_button)
    text_col.addWidget(w.antigravity_options)
    w.api_options = QWidget()
    api_col = QVBoxLayout(w.api_options)
    api_col.setContentsMargins(0, 0, 0, 0)
    for name, title, placeholder in (("text_endpoint", "服务地址", "例如 http://127.0.0.1:1234/v1"),
                                      ("text_model", "模型名称", "填写服务商提供的模型名称")):
        control = QLineEdit()
        control.setPlaceholderText(placeholder)
        control.setText(w.settings.value(name, ""))
        w.text_fields[name] = control
        field(api_col, title, control)
    protocol = Select()
    protocol.addItem("Chat Completions", "chat")
    protocol.addItem("Responses", "responses")
    w.text_fields["api_format"] = protocol
    w.text_key = QLineEdit()
    w.text_key.setEchoMode(QLineEdit.EchoMode.Password)
    w.text_key.setPlaceholderText("本地免密码服务可留空")
    field(api_col, "服务密钥 (API Key)", w.text_key)
    w.text_key_buttons = [button("保存密钥", lambda: w.text_key_action("store")),
                          button("读取密钥", lambda: w.text_key_action("lookup")),
                          button("清除密钥", lambda: w.text_key_action("clear"), "quiet")]
    api_col.addLayout(row(*w.text_key_buttons, None))
    api_col.addWidget(label("密钥按服务地址安全保存在系统密码箱中。", "muted", True))
    text_col.addWidget(w.api_options)
    w.text_test_button = button("测试翻译连接", w.test_translation)
    text_col.addWidget(w.text_test_button, alignment=Qt.AlignmentFlag.AlignLeft)
    w.text_status = label("点击可发送一句简短英文测试翻译是否畅通。", "muted", True)
    text_col.addWidget(w.text_status)
    text_adv, text_adv_col = advanced_section("高级翻译选项", "同时翻译任务数、模型思考深度、接口协议与内置翻译规则")
    field(text_adv_col, "视频 / 网页 / PDF 同时翻译任务数", parallel,
          "默认 4 个即可兼顾速度与稳定，最多 8 个。")
    field(text_adv_col, "Codex 模型思考深度", codex_effort,
          "实时字幕建议选「低 (low)」反应更快；复杂文档可选「中 (medium)」。")
    field(text_adv_col, "自定义接口协议格式", protocol)
    prompt_toggle = button("查看内置翻译规则（提示词）", role="quiet")
    prompt_toggle.setCheckable(True)
    prompt_view = QTextEdit()
    prompt_view.setReadOnly(True)
    prompt_view.setAccessibleName("翻译提示词预览")
    prompt_view.setMaximumHeight(170)
    from text_translation import translation_prompt
    prompt_view.setPlainText(translation_prompt("待翻译的字幕", "en-zh"))
    prompt_view.hide()
    prompt_toggle.toggled.connect(prompt_view.setVisible)
    text_adv_col.addWidget(prompt_toggle, alignment=Qt.AlignmentFlag.AlignLeft)
    text_adv_col.addWidget(prompt_view)
    text_adv_col.addWidget(label("同一场对话中完全相同的句子会自动复用译文，加快显示并节省用量。", "muted", True))
    text_col.addWidget(text_adv)
    for name in ("text_provider", "api_format"):
        w.restore(w.text_fields[name], name)
    w.config_widgets.extend([*w.text_fields.values(), w.text_key, *w.text_key_buttons, w.text_test_button])
    settings_layout.addWidget(text_card)
    connection, connection_col = card("实时语音同传服务（百炼）", "当你需要实时播放中文或英文翻译语音时使用。若只看字幕可无需填写。")
    w.region = Select()
    w.region.addItem("中国内地（北京）", "mainland")
    w.region.addItem("国际（新加坡）", "intl")
    w.restore(w.region, "region")
    field(connection_col, "服务区域", w.region)
    w.key = QLineEdit()
    w.key.setEchoMode(QLineEdit.EchoMode.Password)
    w.key.setPlaceholderText("输入阿里云百炼 API Key")
    field(connection_col, "服务密钥 (API Key)", w.key)
    w.save_button = button("保存密钥", w.save_key)
    w.forget_button = button("清除密钥", w.forget_key, "quiet")
    connection_col.addLayout(row(w.save_button, w.forget_button, None))
    w.remember = QCheckBox("开始翻译时自动记住密钥，下次免输入")
    w.remember.setChecked(w.settings.value("remember", True, type=bool))
    connection_col.addWidget(w.remember)
    w.connection_status = label("正在读取已保存的密钥…", "muted", True)
    connection_col.addWidget(w.connection_status)
    settings_layout.addWidget(connection)
    w.config_widgets.extend([w.region, w.key, w.save_button, w.forget_button, w.remember])
    privacy, pcol = advanced_section("隐私说明与新手指引", "了解语音、字幕与本地缓存数据的处理方式")
    pcol.addWidget(label("• 语音同传模式：语音将发送至百炼服务进行实时同传。\n• 实时字幕模式：声音仅在电脑本地识别为文字，只将文字发送给所选翻译服务。\n• 实时对话不保存任何录音或字幕文本，历史记录仅保存开始时间与时长。\n• 视频与 PDF 翻译生成的缓存文件保存在本机，随时可在对应页面清理，绝不修改你的原始文件。", "secondary", True))
    pcol.addWidget(button("重新打开三步新手指引", w.show_onboarding, "quiet"), alignment=Qt.AlignmentFlag.AlignLeft)
    settings_layout.addWidget(privacy)
    settings_layout.addStretch()
    w.pages.addWidget(settings)
    from video_ui import VideoPage
    w.video_page = VideoPage(w)
    w.pages.addWidget(w.video_page)
    from web_ui import WebPage
    w.web_page = WebPage(w)
    w.pages.addWidget(w.web_page)
    from nas_ui import NasPage
    w.nas_page = NasPage(w)
    w.pages.addWidget(w.nas_page)
    from pdf_ui import PdfPage
    w.pdf_page = PdfPage(w)
    w.pages.addWidget(w.pdf_page)
    from music_ui import MusicPage
    w.music_page = MusicPage(w)
    w.pages.addWidget(w.music_page)
    for combo in w.fields.values():
        combo.currentIndexChanged.connect(w.route)
    pacing.currentIndexChanged.connect(lambda: w.settings.setValue("subtitle_profile", pacing.currentData()))
    w.region.currentIndexChanged.connect(w.load_key)
    w.key.textChanged.connect(w.update_onboarding)
    provider.currentIndexChanged.connect(w.update_text_provider)
    w.text_fields["text_endpoint"].textChanged.connect(w.text_endpoint_changed)
    codex_model.currentIndexChanged.connect(w.save_codex_model)
    codex_effort.currentIndexChanged.connect(w.save_codex_effort)
    w.update_text_provider()
    w.theme_combo.currentIndexChanged.connect(lambda: w.set_theme(w.theme_combo.currentData()))
