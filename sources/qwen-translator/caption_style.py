"""Shared subtitle appearance for the overlay and the visual comparison."""
from math import ceil

from PyQt6.QtCore import QEasingCurve, QObject, QPointF, QPropertyAnimation, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QTextLayout, QTextOption
from PyQt6.QtWidgets import QFrame, QGraphicsBlurEffect, QGraphicsDropShadowEffect, QLabel, QSizePolicy, QVBoxLayout


STYLES = {
    'cinema': ('视频字幕', 'rgba(15, 21, 19, 210)', '#F5F7F4', '#C1CCC6', 'rgba(255,255,255,30)'),
    'paper': ('简洁卡片', 'rgba(245, 247, 244, 242)', '#17201C', '#4D5A54', 'rgba(221,229,224,210)'),
    'clear': ('极简透明', 'transparent', '#FFFFFF', '#E6EEE9', 'transparent'),
    'none': ('陪读蛙 · 无', 'transparent', '#FFFFFF', '#E6EEE9', 'transparent'),
    'blur': ('陪读蛙 · 模糊效果', 'transparent', '#FFFFFF', '#E6EEE9', 'transparent'),
    'quote': ('陪读蛙 · 引用', 'transparent', '#FFFFFF', '#E6EEE9', '#D8A94B'),
    'weaken': ('陪读蛙 · 弱化', 'transparent', '#A1A1A1', '#E6EEE9', 'transparent'),
    'dashed': ('陪读蛙 · 虚线', 'transparent', '#FFFFFF', '#E6EEE9', '#D8A94B'),
    'outline': ('陪读蛙 · 边框', 'transparent', '#FFFFFF', '#E6EEE9', '#D8A94B'),
    'green': ('陪读蛙 · 文字颜色', 'transparent', '#00BB7C', '#E6EEE9', 'transparent'),
    'highlight': ('陪读蛙 · 背景', 'rgba(216,169,75,38)', '#FFFFFF', '#E6EEE9', 'transparent'),
}

PRESETS = {
    'cinema': dict(background='#0F1513', foreground='#F5F7F4', secondary='#C1CCC6',
                   background_opacity=82, shadow=False),
    'paper': dict(background='#F5F7F4', foreground='#17201C', secondary='#4D5A54',
                  background_opacity=95, shadow=False),
    'clear': dict(background='#0F1513', foreground='#FFFFFF', secondary='#E6EEE9',
                  background_opacity=0, shadow=True),
}
for _key in ('none', 'blur', 'quote', 'weaken', 'dashed', 'outline', 'green', 'highlight'):
    PRESETS[_key] = dict(background='#D8A94B' if _key == 'highlight' else '#0F1513',
                         foreground='#00BB7C' if _key == 'green' else '#A1A1A1' if _key == 'weaken' else '#FFFFFF', secondary='#E6EEE9',
                         background_opacity=15 if _key == 'highlight' else 0, shadow=True,
                         radius=4, decoration=_key)
DEFAULTS = dict(style='cinema', size=22, font_family='Noto Sans CJK SC',
                background_opacity=82, text_opacity=100, foreground='', secondary='',
                background='', radius=20, spacing=9, show_source=True,
                translation_first=False, shadow=False, blur_translation=False,
                dashed_translation=False, dim_original=False,
                transparent_background=False, auto_fit_screen=False, click_through=False)
LIMITS = dict(size=(14, 64), background_opacity=(0, 100), text_opacity=(10, 100),
              radius=(0, 40), spacing=(0, 30))


def normalize_appearance(values):
    result = {**DEFAULTS, **{key: value for key, value in values.items() if key in DEFAULTS}}
    if result['style'] not in PRESETS:
        result['style'] = 'cinema'
    for key, (low, high) in LIMITS.items():
        try:
            result[key] = max(low, min(high, int(result[key])))
        except (ValueError, TypeError, OverflowError):
            result[key] = DEFAULTS[key]
    for key in ('show_source', 'translation_first', 'shadow', 'blur_translation', 'dashed_translation', 'dim_original',
                'transparent_background', 'auto_fit_screen', 'click_through'):
        value = result[key]
        result[key] = value.lower() in ('true', '1', 'yes') if isinstance(value, str) else bool(value)
    for key in ('foreground', 'secondary', 'background'):
        color = QColor(str(result[key]))
        result[key] = color.name() if color.isValid() else ''
    # Font names are used in a stylesheet, never accept CSS delimiters from settings.
    family = str(result['font_family'] or '').strip()
    result['font_family'] = ''.join(c for c in family if c not in '\"\'\\;{}\n\r')[:100] or DEFAULTS['font_family']
    return result


def resolve_appearance(options):
    result = normalize_appearance(options)
    for key in ('foreground', 'secondary', 'background'):
        result[key] = result[key] or PRESETS[result['style']][key]
    result['decoration'] = PRESETS[result['style']].get('decoration', '')
    if result['transparent_background']:
        result['background_opacity'] = 0
    return result


def color_css(color, opacity=100):
    value = QColor(color)
    return f'rgba({value.red()}, {value.green()}, {value.blue()}, {round(opacity * 255 / 100)})'


class CaptionAppearance(QObject):
    """One observable, persistent profile shared by editor and displayed captions."""
    changed = pyqtSignal(dict)

    def __init__(self, settings=None, prefix='caption_', parent=None):
        super().__init__(parent)
        self.settings, self.prefix = settings, prefix
        self.defaults = {**DEFAULTS, **(dict(size=24, translation_first=True, auto_fit_screen=True)
                                      if prefix == 'music_caption_' else {})}
        style = settings.value(prefix + 'style', 'cinema') if settings is not None else 'cinema'
        defaults = {**self.defaults, **{key: value for key, value in PRESETS.get(style, PRESETS['cinema']).items()
                                 if key not in ('foreground', 'secondary', 'background')}}
        self._options = normalize_appearance({key: settings.value(prefix + key, default)
                                              if settings is not None else default
                                              for key, default in defaults.items()})
        if settings is not None:
            for key, value in self._options.items():
                settings.setValue(prefix + key, value)

    @property
    def options(self):
        return dict(self._options)

    def update(self, **changes):
        if 'style' in changes and changes['style'] != self._options['style']:
            preset = PRESETS.get(changes['style'], PRESETS['cinema'])
            changes = {**{key: '' for key in ('foreground', 'secondary', 'background')},
                       'background_opacity': preset['background_opacity'], 'shadow': preset['shadow'],
                       'radius': preset.get('radius', DEFAULTS['radius']), **changes}
        options = normalize_appearance({**self._options, **changes})
        if options == self._options:
            return
        self._options = options
        if self.settings is not None:
            for key, value in options.items():
                self.settings.setValue(self.prefix + key, value)
            self.settings.sync()
        self.changed.emit(self.options)

    def apply_preset(self, style):
        preset = PRESETS.get(style, PRESETS['cinema'])
        self.update(**{**self.defaults, **{key: value for key, value in preset.items()
                                           if key in DEFAULTS and key not in ('foreground', 'secondary', 'background')},
                       'style': style,
                       **{key: self._options[key] for key in ('transparent_background', 'auto_fit_screen', 'click_through')}})

    def reset(self):
        self.update(**self.defaults)


class CaptionText(QLabel):
    """Per-line text decoration and blur confined to this text label."""
    def __init__(self):
        super().__init__()
        self.blur_radius = 0
        self.blur_animation = None
        self.base_css = ''
        self.text_color = '#FFFFFF'
        self.text_opacity = 100
        self.web_blur = False
        self.revealed = False
        self.underline_color = None
        self.underline_offset = 0
        self.underline_padding = 0

    def configure_effect(self, *, css, color, opacity, blur_radius=0, web_blur=False, shadow=False,
                         underline_color=None, underline_offset=0, underline_padding=0):
        if self.blur_animation is not None:
            self.blur_animation.stop()
            self.blur_animation.deleteLater()
            self.blur_animation = None
        self.blur_radius, self.base_css = blur_radius, css
        self.text_color, self.text_opacity = color, opacity
        self.web_blur = web_blur
        self.underline_color = underline_color
        self.underline_offset = underline_offset
        self.underline_padding = underline_padding
        # A preset starts hidden even if selected while the cursor is elsewhere.
        self.revealed = False
        if blur_radius:
            effect = QGraphicsBlurEffect(self)
            effect.setBlurHints(QGraphicsBlurEffect.BlurHint.QualityHint)
            effect.setBlurRadius(blur_radius)
            self.setGraphicsEffect(effect)
            self.blur_animation = QPropertyAnimation(effect, b'blurRadius', self)
            self.blur_animation.setDuration(100 if web_blur else 150)
            curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
            first, second = ((.42, 0), (.58, 1)) if web_blur else ((.25, .1), (.25, 1))
            curve.addCubicBezierSegment(QPointF(*first), QPointF(*second), QPointF(1, 1))
            self.blur_animation.setEasingCurve(curve)
            if web_blur:
                # The CSS preset fades its ink with the same curve as its blur.
                # Deriving opacity from that animation avoids a second effect.
                self.blur_animation.valueChanged.connect(self.refresh_ink)
            self.setToolTip('鼠标移到译文上可显示清晰文字')
        elif shadow:
            effect = QGraphicsDropShadowEffect(self)
            effect.setBlurRadius(9)
            effect.setOffset(0, 2)
            effect.setColor(QColor(0, 0, 0, 245))
            self.setGraphicsEffect(effect)
            self.setToolTip('')
        else:
            self.setGraphicsEffect(None)
            self.setToolTip('')
        self.refresh_ink()

    def ink_opacity(self):
        factor = 1
        if self.web_blur and self.blur_radius:
            factor -= .25 * self.graphicsEffect().blurRadius() / self.blur_radius
        return self.text_opacity * factor

    def refresh_ink(self, _value=None):
        self.setStyleSheet(self.base_css + f'color:{color_css(self.text_color, self.ink_opacity())};')

    def paintEvent(self, event):
        if not self.underline_color or not self.text():
            super().paintEvent(event)
            return
        # Draw the stylesheet's frame, then use one layout for both the text
        # and the lines underneath it. A bottom border cannot follow wrapping.
        QFrame.paintEvent(self, event)
        rect = QRectF(self.contentsRect())
        margin = self.margin()
        rect.adjust(margin, margin, -margin, -margin)
        if rect.width() <= 0:
            return
        option = QTextOption()
        option.setAlignment(self.alignment() & Qt.AlignmentFlag.AlignHorizontal_Mask)
        option.setWrapMode(QTextOption.WrapMode.WordWrap if self.wordWrap()
                           else QTextOption.WrapMode.ManualWrap)
        option.setTextDirection(Qt.LayoutDirection.LayoutDirectionAuto)
        text = self.text().replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\u2028')
        layout = QTextLayout(text, self.font())
        layout.setTextOption(option)
        layout.beginLayout()
        lines = []
        height = 0.0
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(rect.width())
            line.setPosition(QPointF(0, height))
            height += line.height()
            lines.append(line)
        layout.endLayout()
        top = rect.top()
        if self.alignment() & Qt.AlignmentFlag.AlignVCenter:
            top += (rect.height() - height) / 2
        elif self.alignment() & Qt.AlignmentFlag.AlignBottom:
            top += rect.height() - height
        origin = QPointF(rect.left(), top)
        painter = QPainter(self)
        painter.setClipRect(rect.adjusted(0, 0, 0, self.underline_padding))
        ink = QColor(self.text_color)
        ink.setAlpha(round(self.ink_opacity() * 255 / 100))
        painter.setPen(ink)
        layout.draw(painter, origin)
        underline = QColor(self.underline_color)
        underline.setAlpha(ink.alpha())
        pen = QPen(underline, 1, Qt.PenStyle.DashLine)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        painter.setPen(pen)
        for line in lines:
            bounds = line.naturalTextRect()
            if bounds.width() <= 0:
                continue
            y = round(top + line.y() + line.ascent() + self.underline_offset) + .5
            painter.drawLine(QPointF(rect.left() + bounds.left(), y),
                             QPointF(rect.left() + bounds.right(), y))

    def reveal(self, visible):
        if not self.blur_radius:
            return
        self.revealed = visible
        self.refresh_ink()
        self.blur_animation.stop()
        self.blur_animation.setStartValue(self.graphicsEffect().blurRadius())
        self.blur_animation.setEndValue(0.0 if visible else float(self.blur_radius))
        self.blur_animation.start()

    def enterEvent(self, event):
        self.reveal(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.reveal(False)
        super().leaveEvent(event)


class CaptionSurface(QFrame):
    def __init__(self, style='cinema', size=22, parent=None):
        super().__init__(parent)
        self.primary_is_source = False
        self._appearance = {}
        self.setObjectName('caption_surface')
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(30, 18, 30, 22)
        self.layout.setSpacing(9)
        self.source = CaptionText()
        self.translation = CaptionText()
        self.notice = QLabel()
        for label in (self.source, self.translation):
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            self.layout.addWidget(label)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.layout.addWidget(self.notice)
        self.notice.hide()
        self.apply_style(style, size)

    def apply_style(self, style, size):
        preset = PRESETS.get(style, PRESETS['cinema'])
        self.apply_appearance({**DEFAULTS, **preset, 'style': style, 'size': size})

    def apply_appearance(self, options):
        self._appearance = dict(options)
        opts = resolve_appearance(options)
        self.style = opts['style']
        size = opts['size']
        background = color_css(opts['background'], opts['background_opacity'])
        secondary = color_css(opts['secondary'], opts['text_opacity'] * (.6 if opts['dim_original'] else 1))
        self.setStyleSheet(f'QFrame#caption_surface {{ background: {background}; border: 0; border-radius: {opts["radius"]}px; }}')
        common = f'background:transparent;border:0;font-family:"{opts["font_family"]}";'
        decoration = {
            'quote': 'border-left:4px solid #D8A94B;padding:4px 0 4px 8px;',
            'outline': 'border:1px solid #D8A94B;border-radius:4px;padding:2px 4px;',
        }.get(opts['decoration'], '')
        underline_color = '#D8A94B' if opts['decoration'] == 'dashed' else None
        underline_offset = 5 if underline_color else 0
        if opts['dashed_translation'] and not self.primary_is_source:
            underline_color, underline_offset = opts['foreground'], size * .25
        underline_padding = ceil(underline_offset) + 2 if underline_color else 0
        if underline_padding:
            base_padding = 4 if opts['decoration'] == 'quote' else 2 if opts['decoration'] == 'outline' else 0
            decoration += f'padding-bottom:{base_padding + underline_padding}px;'
        self.source.configure_effect(css=f'{common}font-size:{max(12, round(size*.8))}px;font-weight:400;',
                                     color=opts['secondary'], opacity=opts['text_opacity'] * (.6 if opts['dim_original'] else 1),
                                     shadow=opts['shadow'])
        self.translation.configure_effect(css=f'{common}font-size:{size}px;font-weight:600;{decoration}',
                                          color=opts['foreground'], opacity=opts['text_opacity'],
                                          blur_radius=0 if self.primary_is_source else 6 if opts['blur_translation'] else 4 if opts['style'] == 'blur' else 0,
                                          web_blur=opts['style'] == 'blur' and not opts['blur_translation'] and not self.primary_is_source,
                                          shadow=opts['shadow'], underline_color=underline_color,
                                          underline_offset=underline_offset, underline_padding=underline_padding)
        self.notice.setStyleSheet(f'{common}color:{secondary};font-size:11px;')
        self.layout.setSpacing(opts['spacing'])
        self.layout.removeWidget(self.source)
        self.layout.removeWidget(self.translation)
        labels = (self.translation, self.source) if opts['translation_first'] else (self.source, self.translation)
        for index, label in enumerate(labels):
            self.layout.insertWidget(index, label)
        self.source.setVisible(opts['show_source'])

    def set_primary_is_source(self, value):
        value = bool(value)
        if self.primary_is_source != value:
            self.primary_is_source = value
            self.apply_appearance(self._appearance)
