"""A lyrics-only caption view of the music page's existing player state."""
import math

from PyQt6.QtCore import Qt, pyqtSignal

from caption_window import CaptionWindow


def lyric_pair(line):
    """Keep original-only and already-Chinese lyrics readable as one line."""
    source = str(line.get('text') or '').strip()
    translated = str(line.get('translation') or '').strip()
    if not translated or ''.join(source.split()) == ''.join(translated.split()):
        return '', translated or source
    return source, translated


def timed_line(line):
    start = line.get('start')
    return isinstance(start, (int, float)) and not isinstance(start, bool) and math.isfinite(start)


class LyricsCaptionWindow(CaptionWindow):
    visibilityChanged = pyqtSignal(bool)

    def __init__(self, settings=None, *, appearance=None):
        super().__init__(settings, appearance=appearance, kind='lyrics', directions=('lyrics',))
        self.setWindowTitle('Qwen 悬浮歌词')
        self.caption_title.setText('⠿  同步歌词')
        self.music_state = {}
        self.appearance.changed.connect(lambda _: self.apply_state(self.music_state))
        self.apply_state({})

    def apply_state(self, state):
        """Only the worker selects the current line; this view never advances time."""
        self.music_state = state
        track = state.get('track') or {}
        lyrics = state.get('lyrics') or {}
        lines = lyrics.get('lines') or []
        index = lyrics.get('current_index', -1)
        surface = self.surfaces['lyrics']
        source, translated, notice = '', '', ''
        primary_is_source = True
        song = ' · '.join(filter(None, (track.get('title'), track.get('artist'))))
        self.caption_title.setToolTip(song or '拖动歌词移动窗口；拖动右下角调整大小')
        if not state.get('available'):
            translated = '等待 Apple Music 播放…'
        elif lyrics.get('instrumental'):
            translated, notice = '纯音乐', song
        elif not lines:
            translated = lyrics.get('status_text') or lyrics.get('error') or '等待歌词…'
            notice = song
        elif not any(timed_line(line) for line in lines):
            translated = '当前歌词没有时间轴'
            notice = '请在音乐页阅读完整歌词，或导入带时间轴的 LRC。'
        elif type(index) is int and 0 <= index < len(lines) and timed_line(lines[index]):
            source, translated = lyric_pair(lines[index])
            primary_is_source = not bool(str(lines[index].get('translation') or '').strip())
            notice = song
        else:
            translated, notice = '等待下一句歌词…', song
        if state.get('available') and not state.get('playing'):
            notice = '已暂停' + (' · ' + notice if notice else '')
        surface.source.setText(source)
        surface.set_primary_is_source(primary_is_source)
        surface.source.setVisible(bool(source) and self.appearance.options['show_source'])
        surface.translation.setText(translated)
        surface.notice.setToolTip(notice)
        surface.notice.setText(surface.notice.fontMetrics().elidedText(
            notice, Qt.TextElideMode.ElideRight, max(200, self.width() - 90)))
        surface.notice.setVisible(bool(notice))
        surface.show()
        self.fit_height()

    def showEvent(self, event):
        super().showEvent(event)
        self.visibilityChanged.emit(True)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.visibilityChanged.emit(False)
