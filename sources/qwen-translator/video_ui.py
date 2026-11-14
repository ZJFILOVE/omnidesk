"""Video workspace: soft subtitles, website search, alignment and player handoff."""
from collections import Counter
import json
from pathlib import Path
import shutil
import sys
import urllib.parse

from PyQt6.QtCore import QProcess, Qt, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtNetwork import QLocalSocket
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QMenu, QMessageBox,
                             QLineEdit, QProgressBar, QTextEdit, QVBoxLayout, QWidget)

from desktop_ui import advanced_section, button, card, field, label, row, scroll_page
from file_dialogs import choose_file
from ui_controls import WheelSafeComboBox, WheelSafeDoubleSpinBox
from video_player import VideoPlayer
from worker_shutdown import stop_worker
from video_cache import cached_videos, delete_cache

ROOT = Path(__file__).resolve().parent
SUBTITLE_MODES = (
    ('自动选择现有字幕；没有则本地识别', {}),
    ('使用本地识别（继续上次进度）', {'force_asr': True}),
    ('从头重新识别（忽略上次识别进度）', {'force_asr': True, 'reset_asr_cache': True}),
)


def subtitle_save_path(result, selected):
    """Suggest a sidecar filename without assigning Chinese to source subtitles."""
    if result.get('loaded'):
        suffix = result.get('subtitle_suffix', 'original')
    elif result.get('original_only'):
        origin = result.get('origin')
        suffix = f'{origin}.original' if origin in {'youtube_manual', 'youtube_auto', 'asr'} else 'original'
    elif selected == result.get('bilingual'):
        suffix = 'bilingual'
    elif selected == result.get('original'):
        origin = result.get('origin')
        suffix = f'{origin}.original' if origin in {'youtube_manual', 'youtube_auto', 'asr'} else 'original'
    elif selected == result.get('timed_source'):
        suffix = 'source'
    else:
        suffix = result.get('subtitle_suffix', 'zh' if Path(selected).name == 'zh.srt' else 'source')
    source = result.get('source', '')
    if source and not source.startswith(('https://', 'http://')):
        video = Path(source).expanduser()
        return video.with_name(video.stem + f'.{suffix}.srt')
    return Path.home() / 'Downloads' / (Path(result['title']).stem + f'.{suffix}.srt')


class VideoPage(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.process = None
        self.buffer = b''
        self.result = None
        self.inspected_source = None
        self.controls = []
        self.last_error = False
        self.cancel_pending = False
        self.nas_context = None
        self.nas_job_id = None
        self.nas_pending = False
        self.player = VideoPlayer(self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll, layout = scroll_page()
        outer.addWidget(scroll)
        layout.addWidget(label('视频字幕', 'title'))
        layout.addWidget(label('为本地视频、家庭网盘影片或 YouTube 视频自动生成中英双语字幕，并直接调用播放器观看。', 'muted', True))

        source_card, source_layout = card('1. 选择视频并准备字幕', '粘贴链接或选择本地视频，一键准备中英双语字幕；也可从最近记录中直接打开。')
        self.source = QLineEdit()
        self.source.setPlaceholderText('粘贴 YouTube 链接，或选择电脑 / 网盘目录中的视频文件')
        self.source.setText(window.settings.value('video/source', ''))
        self.source.setAccessibleName('视频路径或 YouTube 链接')
        select = button('选择视频文件', self.choose_video)
        inspect = button('读取视频信息', self.inspect)
        source_layout.addWidget(self.source)
        source_layout.addLayout(row(select, inspect, None))
        self.info = label('原视频文件保持原样，生成的字幕与缓存保存在本机。', 'muted', True)
        source_layout.addWidget(self.info)
        self.chinese = QCheckBox('生成中文与原文对照字幕（英文视频为中英双语）')
        self.chinese.setChecked(True)
        source_layout.addWidget(self.chinese)
        self.prepare_button = button('准备 / 翻译双语字幕', self.prepare, 'primary')
        self.chinese.toggled.connect(lambda checked: self.prepare_button.setText(
            '准备 / 翻译双语字幕' if checked else '准备原字幕'))
        self.obtain_button = button('仅提取原字幕（不翻译）', self.obtain_original)
        self.cancel_button = button('取消任务', self.cancel)
        self.cancel_button.setEnabled(False)
        source_layout.addLayout(row(self.prepare_button, self.obtain_button, self.cancel_button, None))
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setAccessibleName('视频任务进度')
        source_layout.addWidget(self.progress)
        self.status = label('等待选择视频。', 'secondary', True)
        source_layout.addWidget(self.status)
        self.cached_entries = []
        self.cache_search = QLineEdit()
        self.cache_search.setPlaceholderText('按视频标题搜索最近记录…')
        self.cache_search.setAccessibleName('搜索已缓存视频标题')
        self.cache_search.setClearButtonEnabled(True)
        refresh_cache = button('刷新列表', self.refresh_cached_videos, 'quiet')
        source_layout.addWidget(label('最近处理过的视频', 'secondary'))
        source_layout.addLayout(row(self.cache_search, refresh_cache))
        self.cached = WheelSafeComboBox()
        self.cached.setAccessibleName('按标题选择已缓存视频')
        self.cached.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.cached.setMinimumContentsLength(24)
        self.open_cached_button = button('打开所选视频', self.open_cached_video)
        cached_row = row(self.cached, self.open_cached_button)
        cached_row.setStretch(0, 1)
        source_layout.addLayout(cached_row)
        layout.addWidget(source_card)

        play_card, play_layout = card('2. 播放与导出字幕', '字幕准备完成后可直接调用播放器观看，或导出为独立 SRT 字幕文件。')
        self.players = WheelSafeComboBox()
        for name, value in [('mpv · 推荐（支持进度联动）', 'mpv'), ('VLC', 'vlc'), ('SMPlayer', 'smplayer')]:
            self.players.addItem(name + ('' if shutil.which(value) else '（未安装）'), value)
        self.output = WheelSafeComboBox()
        self.output.setAccessibleName('要播放或导出的字幕版本')
        self.play_button = button('播放视频', self.play, 'primary')
        self.export_button = button('导出字幕 SRT…', self.export)
        play_layout.addLayout(row(self.players, self.output, self.play_button, self.export_button))
        self.play_button.setEnabled(False)
        self.export_button.setEnabled(False)
        pause = button('暂停 / 继续', lambda: self.player.send(['cycle', 'pause']))
        back = button('后退 10 秒', lambda: self.player.send(['seek', -10, 'relative']))
        forward = button('前进 10 秒', lambda: self.player.send(['seek', 10, 'relative']))
        self.speed = WheelSafeComboBox()
        for speed in (.75, 1, 1.25, 1.5, 2):
            self.speed.addItem(f'{speed:g}×', speed)
        self.speed.setCurrentIndex(1)
        self.speed.currentIndexChanged.connect(lambda: self.player.send(['set_property', 'speed', self.speed.currentData()]))
        self.position = label('00:00', 'secondary')
        play_layout.addLayout(row(back, pause, forward, self.speed, self.position, None))
        self.player_status = label('mpv 播放声音默认使用「声音与设备」中选定的耳机或扬声器。', 'muted', True)
        self.player.status.connect(self.player_status.setText)
        self.player.position.connect(lambda t: self.position.setText(f'{int(t)//60:02}:{int(t)%60:02}'))
        play_layout.addWidget(self.player_status)
        layout.addWidget(play_card)

        adv_widget, adv_layout = advanced_section(
            '高级字幕设置 · 在线搜字幕、时间轴微调与缓存管理',
            '手动导入字幕、搜索 SubHD / 射手网、校准字幕快慢偏移或回传到家庭影视库',
        )
        prepare_card, prepare_layout = card('字幕来源与音轨选项')
        self.subtitles = WheelSafeComboBox()
        self.subtitles.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.subtitles.setMinimumContentsLength(24)
        for title, options in SUBTITLE_MODES:
            self.subtitles.addItem(title, options)
        self.subtitles.setAccessibleName('用于准备的字幕')
        import_button = button('导入本地字幕 / 压缩包', self.import_subtitle)
        self.load_button = button('直接加载所选字幕', self.load_selected_subtitle)
        prepare_layout.addWidget(self.subtitles)
        prepare_layout.addLayout(row(import_button, self.load_button, None))
        prepare_layout.addWidget(label('已有现成字幕时可直接导入并加载播放；若是外语字幕，点击上方「准备 / 翻译双语字幕」即可译为双语。', 'muted', True))
        self.content = WheelSafeComboBox()
        self.content.addItem('自动判断是否已有中文', 'auto')
        self.content.addItem('已有中文，保留原文', 'zh')
        self.content.addItem('没有中文，翻译为中文', 'other')
        field(prepare_layout, '字幕语言判断', self.content)
        self.audio = WheelSafeComboBox()
        self.audio.addItem('自动选择英文音轨', None)
        field(prepare_layout, '语音识别 / 对齐音轨', self.audio)
        self.offline = QCheckBox('YouTube 视频先下载到本机再播放（关闭则直接在线播放）')
        self.offline.setChecked(True)
        prepare_layout.addWidget(self.offline)
        adv_layout.addWidget(prepare_card)
        self.controls += [self.source, select, inspect, self.audio, self.offline,
                          self.cache_search, refresh_cache, self.cached, self.open_cached_button]

        search_card, search_layout = card('在线搜索外挂字幕', '从 SubHD 或射手网 (ASSRT) 查找现成的电影与美剧字幕。')
        self.provider = WheelSafeComboBox()
        self.provider.addItem('SubHD', 'subhd')
        self.provider.addItem('射手网 (ASSRT)', 'assrt')
        self.query = QLineEdit()
        self.query.setPlaceholderText('例如 Stranger Things S02E01，可修改后重新搜索')
        self.query.setAccessibleName('字幕搜索关键词')
        search_button = button('搜索字幕', self.search)
        search_layout.addLayout(row(self.provider, self.query, search_button))
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText('ASSRT Token（不写入偏好文件）')
        self.token.setAccessibleName('ASSRT Token')
        token_save = button('存入密钥库', lambda: self.start_job('token_store'))
        token_load = button('读取密钥库', lambda: self.start_job('token_lookup'))
        token_clear = button('清除密钥', lambda: self.start_job('token_clear'))
        self.token_row = QWidget()
        self.token_row.setLayout(row(self.token, token_save, token_load, token_clear))
        self.token_row.layout().setContentsMargins(0, 0, 0, 0)
        self.token_row.hide()
        self.provider.currentIndexChanged.connect(lambda: self.token_row.setVisible(self.provider.currentData() == 'assrt'))
        search_layout.addWidget(self.token_row)
        self.candidates = WheelSafeComboBox()
        self.candidates.setMinimumWidth(0)
        self.candidates.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.candidates.setMinimumContentsLength(24)
        self.candidates.setAccessibleName('在线字幕搜索结果')
        search_layout.addWidget(self.candidates)
        fetch = button('下载所选字幕', self.fetch)
        web = button('打开条目网页', self.open_candidate)
        web_search = button('在网站搜索', self.open_search)
        search_layout.addLayout(row(fetch, web, web_search, None))
        search_layout.addWidget(label('字幕来源：subhd.tv / assrt.net。若网站要求登录或验证码，请在网页完成下载，再导入字幕文件。', 'muted', True))
        adv_layout.addWidget(search_card)
        self.controls += [self.provider, self.query, search_button, self.token, token_save, token_load,
                          token_clear, self.candidates, fetch, web, web_search]

        sync_card, sync_layout = card('时间轴对齐与微调', '当外挂字幕比声音偏快或偏慢时，可自动对齐或手动调整秒数。')
        self.sync = WheelSafeComboBox()
        self.sync.addItem('自动对齐外挂字幕：优先内嵌英文字幕，否则分析音轨', 'auto')
        self.sync.addItem('分段对齐：尝试修复片头 / 广告 / 剪辑差异', 'split')
        self.sync.addItem('保留现有时间轴 / 仅使用手动校准', 'none')
        field(sync_layout, '时间轴对齐方式', self.sync)
        self.reference = QLineEdit()
        self.reference.setPlaceholderText('可选：与视频已同步的参考 SRT')
        self.reference.setAccessibleName('时间轴参考字幕')
        reference_button = button('选择参考', self.choose_reference)
        sync_layout.addLayout(row(self.reference, reference_button))
        self.offset = WheelSafeDoubleSpinBox()
        self.offset.setRange(-3600, 3600)
        self.offset.setDecimals(3)
        self.offset.setSingleStep(.1)
        self.offset.setSuffix(' 秒')
        self.offset.setAccessibleName('字幕整体偏移，正值延后')
        self.scale = WheelSafeDoubleSpinBox()
        self.scale.setRange(.5, 2)
        self.scale.setDecimals(6)
        self.scale.setSingleStep(.001)
        self.scale.setValue(1)
        self.scale.setAccessibleName('时间轴倍率')
        sync_layout.addLayout(row(label('整体偏移（正值延后）'), self.offset, label('时间倍率'), self.scale))
        sync_layout.addWidget(label('自动对齐会生成校准版本，原始字幕始终保留。', 'muted', True))
        adv_layout.addWidget(sync_card)
        self.controls += [self.subtitles, import_button, self.load_button, self.content, self.chinese, self.sync,
                          self.reference, reference_button, self.offset, self.scale, self.prepare_button, self.obtain_button]

        manage_card, manage_layout = card('家庭影视库回传与缓存管理')
        self.nas_auto = QCheckBox('字幕完成后自动回传到 NAS（仅限已同步的 NAS 媒体库影片）')
        self.nas_auto.setChecked(window.settings.value('video/nas_auto', False, type=bool))
        self.nas_auto.toggled.connect(lambda value: window.settings.setValue('video/nas_auto', value))
        manage_layout.addWidget(self.nas_auto)
        self.nas_button = button('回传当前成品到 NAS', self.writeback_nas)
        self.nas_button.setEnabled(False)
        manage_layout.addLayout(row(self.nas_button, None))
        self.nas_status = label('回传会保存中文与对照字幕；写回进度和冲突也可在 NAS 自动字幕页查看。', 'muted', True)
        manage_layout.addWidget(self.nas_status)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(135)
        self.log.document().setMaximumBlockCount(150)
        self.log.setAccessibleName('视频任务进度')
        manage_layout.addWidget(self.log)
        self.cache_button = button('删除缓存…', None, 'quiet')
        cache_menu = QMenu(self.cache_button)
        for title, all_sources in [('当前视频', False), ('全部视频', True)]:
            menu = cache_menu.addMenu(title)
            for text, kind in [('删除字幕缓存', 'subtitles'), ('删除已下载的 YouTube 视频', 'video'),
                               ('删除识别音轨缓存', 'audio')]:
                action = menu.addAction(text)
                action.triggered.connect(lambda checked=False, k=kind, a=all_sources: self.clear_cache(k, a))
        self.cache_button.setMenu(cache_menu)
        self.controls.append(self.cache_button)
        manage_layout.addLayout(row(button('打开字幕缓存目录', self.open_cache, 'quiet'), self.cache_button, None))
        adv_layout.addWidget(manage_card)
        layout.addWidget(adv_widget)
        layout.addStretch()
        self.source.textChanged.connect(self.source_changed)
        self.subtitles.currentIndexChanged.connect(lambda: self.invalidate_result())
        self.output.currentIndexChanged.connect(self.reload_subtitle)
        self.cache_search.textChanged.connect(self.filter_cached_videos)
        self.refresh_cached_videos()

    def refresh_cached_videos(self):
        self.cached_entries = cached_videos(ROOT / 'video-cache')
        self.filter_cached_videos()

    def filter_cached_videos(self):
        previous = self.cached.currentData() or {}
        selected_source = previous.get('source', self.source.text().strip())
        query = self.cache_search.text().strip().casefold()
        entries = [entry for entry in self.cached_entries if query in entry['title'].casefold()]
        titles = Counter(entry['title'] for entry in entries)
        self.cached.clear()
        for entry in entries:
            kind = 'YouTube' if entry['source'].startswith(('https://', 'http://')) else '本地视频'
            title = entry['title'] + ' · ' + kind
            if titles[entry['title']] > 1:
                title += ' · ' + entry['source']
            self.cached.addItem(title, entry)
            self.cached.setItemData(self.cached.count() - 1, entry['source'], Qt.ItemDataRole.ToolTipRole)
            if entry['source'] == selected_source:
                self.cached.setCurrentIndex(self.cached.count() - 1)
        if not entries:
            self.cached.addItem('没有匹配标题的视频' if query else '暂无已缓存视频')
        self.cached.setEnabled(bool(entries) and not self.process)
        self.open_cached_button.setEnabled(bool(entries) and not self.process)

    def open_cached_video(self):
        entry = self.cached.currentData()
        if self.process or not entry:
            return
        if self.source.text() == entry['source']:
            self.source_changed()
        else:
            self.source.setText(entry['source'])
        self.info.setText(entry['title'])
        self.query.setText(entry['title'])
        self.start_job('open_cached')

    def report(self, text):
        self.status.setText(text)
        self.log.append('')
        cursor = self.log.textCursor()
        cursor.insertText(text)
        self.log.setTextCursor(cursor)

    def invalidate_result(self):
        self.result = None
        self.nas_job_id = None
        self.output.clear()
        self.play_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.nas_button.setEnabled(False)

    def source_changed(self):
        self.invalidate_result()
        self.nas_context = None
        self.nas_job_id = None
        self.nas_status.setText('准备完成后可回传到已配置的 NAS 媒体库。')
        self.inspected_source = None
        self.audio.clear()
        self.audio.addItem('自动选择英文音轨', None)
        while self.subtitles.count() > len(SUBTITLE_MODES):
            self.subtitles.removeItem(len(SUBTITLE_MODES))
        self.subtitles.setCurrentIndex(0)
        self.candidates.clear()
        self.reference.clear()
        self.offset.setValue(0)
        self.scale.setValue(1)

    def source_value(self):
        value = self.source.text().strip()
        if not value:
            raise ValueError('请先选择视频或粘贴 YouTube 链接。')
        return value

    def choose_video(self):
        initial = self.window.settings.value('video/directory', str(Path.home()))
        path = choose_file(self, '选择视频（支持网盘挂载）', initial,
                    '视频 (*.mkv *.mp4 *.avi *.mov *.webm *.m4v *.ts);;所有文件 (*)')
        if path:
            self.source.setText(path)
            self.window.settings.setValue('video/directory', str(Path(path).parent))
            self.inspect()

    def inspect(self):
        self.start_job('inspect')

    def search(self):
        if len(self.query.text().strip()) < 3:
            self.report('请先检查视频，或填写至少 3 个字符的剧名 / 季集。')
            return
        self.start_job('search', provider=self.provider.currentData(), query=self.query.text().strip())

    def fetch(self):
        candidate = self.candidates.currentData()
        if candidate:
            self.start_job('download', candidate=candidate)
        else:
            self.report('请先搜索并选择字幕条目。')

    def open_candidate(self):
        candidate = self.candidates.currentData()
        if candidate:
            QDesktopServices.openUrl(QUrl(candidate['url']))

    def open_search(self):
        query = urllib.parse.quote(self.query.text().strip(), safe='')
        url = 'https://subhd.tv/search/' + query if self.provider.currentData() == 'subhd' else 'https://assrt.net/sub/?searchword=' + query
        QDesktopServices.openUrl(QUrl(url))

    def import_subtitle(self):
        path = choose_file(self, '导入外挂字幕', str(Path.home() / 'Downloads'),
                    '字幕 (*.srt *.ass *.ssa *.vtt *.zip *.gz)')
        if path:
            if Path(path).suffix.lower() in {'.zip', '.gz'}:
                self.start_job('import_archive', path=path)
            else:
                self.add_files([{'path': path, 'title': Path(path).name}])
                if self.source.text().strip() and not self.source.text().strip().startswith(('https://', 'http://')):
                    self.load_selected_subtitle()

    def add_files(self, files):
        for file in files:
            self.subtitles.addItem(file['title'], {'subtitle_path': file['path']})
        if files:
            self.subtitles.setCurrentIndex(self.subtitles.count() - len(files))
        self.report(f'已加入 {len(files)} 个文本字幕文件；压缩包有多集时请选择对应文件。')

    def choose_reference(self):
        path = choose_file(self, '选择与视频同步的参考字幕', str(Path.home()), 'SRT (*.srt)')
        if path:
            self.reference.setText(path)

    def prepare(self):
        self.invalidate_result()
        selected = self.subtitles.currentData() or {}
        self.start_job('prepare', **selected, audio_stream=self.audio.currentData(),
            make_chinese=self.chinese.isChecked(), subtitle_content=self.content.currentData(),
            sync_mode=self.sync.currentData(), reference_path=self.reference.text().strip(),
            offset_ms=round(self.offset.value() * 1000), scale=self.scale.value(),
            download_video=self.offline.isChecked())

    def load_selected_subtitle(self):
        selected = self.subtitles.currentData() or {}
        if not selected.get('subtitle_path') and selected.get('subtitle_stream') is None:
            self.report('请先在列表选择已有字幕，或导入字幕文件；没有字幕时可使用准备字幕。')
            return
        self.start_job('load_subtitle', **selected)

    def obtain_original(self):
        selected = self.subtitles.currentData() or {}
        self.start_job('obtain_original', **selected, audio_stream=self.audio.currentData(),
                       asr_backend=self.window.fields['asr_backend'].currentData(),
                       download_video=self.offline.isChecked())

    def start_job(self, action, **extra):
        if self.process:
            return
        try:
            source = '' if action.startswith('token_') or action == 'search' else self.source_value()
            config = {'action': action, 'source': source, 'assrt_token': self.token.text().strip(), **extra}
            if action == 'prepare' and extra.get('make_chinese', True):
                config.update(self.window.translation_config())
                config['subtitle_language'] = 'en-zh'
            if action == 'prepare':
                config['asr_backend'] = self.window.fields['asr_backend'].currentData()
                config['nas_writeback'] = self.nas_auto.isChecked()
            if source:
                self.window.settings.setValue('video/source', source)
                self.window.settings.sync()
        except ValueError as error:
            self.report(str(error))
            return
        self.last_error = False
        self.cancel_pending = False
        process = QProcess(self)
        process.setProperty('nas_auto', action == 'prepare' and self.nas_auto.isChecked())
        self.process = process
        self.buffer = b''
        python = ROOT / '.venv/bin/python'
        process.setProgram(str(python) if python.is_file() else sys.executable)
        process.setArguments([str(ROOT / 'video_worker.py')])
        process.started.connect(lambda: process.write((json.dumps(config, ensure_ascii=False) + '\n').encode()))
        process.readyReadStandardOutput.connect(self.events)
        # Drain errors without echoing provider credentials or signed download URLs.
        process.readyReadStandardError.connect(lambda: process.readAllStandardError())
        process.finished.connect(lambda code, status: self.finished(process, code))
        process.errorOccurred.connect(lambda error: self.failed(process, error))
        for control in self.controls:
            control.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress.setRange(0, 0)
        self.progress.setFormat('%p%')
        self.report('正在处理…')
        process.start()

    def failed(self, process, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.report('无法启动视频工作进程：' + process.errorString())
            self.last_error = True
            self.finished(process, 1)

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
            if event.get('error'):
                self.last_error = True
                self.report(event['error'])
            elif event.get('message'):
                if event.get('transient'):
                    if not self.cancel_pending:
                        self.status.setText(event['message'])
                else:
                    self.report(event['message'])
            if 'progress' in event and not self.cancel_pending:
                self.progress.setFormat(event.get('progress_label', '%p%'))
                if event['progress'] is None:
                    self.progress.setRange(0, 0)
                else:
                    self.progress.setRange(0, 100)
                    self.progress.setValue(round(event['progress'] * 100))
            if 'inspection' in event:
                self.show_inspection(event['inspection'])
            if 'candidates' in event:
                self.candidates.clear()
                for item in event['candidates']:
                    self.candidates.addItem(item['title'], item)
                self.report(f'找到 {len(event["candidates"])} 个条目，请核对季集、语言和片源。')
            if 'files' in event:
                self.add_files(event['files'])
            if 'token' in event:
                self.token.setText(event['token'])
            if 'result' in event:
                self.show_result(event['result'])

    def show_inspection(self, info):
        self.inspected_source = info['source']
        self.info.setText(info['title'] + (f" · {info['duration']/60:.1f} 分钟" if info.get('duration') else ''))
        self.query.setText(info.get('query', ''))
        self.audio.clear()
        self.audio.addItem('自动选择英文音轨', None)
        for track in info.get('audio', []):
            self.audio.addItem(track['title'], track['index'])
        while self.subtitles.count() > len(SUBTITLE_MODES):
            self.subtitles.removeItem(len(SUBTITLE_MODES))
        for item in info.get('subtitles', []):
            data = {'subtitle_path': item['path']} if 'path' in item else {'subtitle_stream': item['stream']}
            self.subtitles.addItem(item['title'], data)
        self.report(f"检查完成：{len(info.get('subtitles', []))} 条可用文本字幕。选择匹配字幕后可直接加载；需要翻译或校准时再准备。")

    def show_result(self, result):
        self.result = result
        if result.get('title'):
            self.info.setText(result['title'])
        self.output.blockSignals(True)
        self.output.clear()
        title = '准备好的字幕'
        if result.get('loaded'):
            title = '已加载的原字幕'
        elif result.get('original_only'):
            title = '原字幕 · ' + result.get('origin_label', '未经处理')
        self.output.addItem(title, result['subtitle'])
        if result.get('bilingual'):
            self.output.setItemText(0, '仅中文字幕')
            self.output.addItem('中文 + 原文对照（英文为中英双语）', result['bilingual'])
            self.output.setCurrentIndex(1)
        if not result.get('loaded') and not result.get('original_only'):
            self.output.addItem('处理后的原文', result['timed_source'])
            self.output.addItem('原字幕 · ' + result.get('origin_label', '未经处理'), result['original'])
        self.output.blockSignals(False)
        self.play_button.setEnabled(True)
        self.export_button.setEnabled(True)
        self.nas_button.setEnabled(self.can_writeback_nas() and not self.process)
        self.reload_subtitle()

    def can_writeback_nas(self):
        return bool(self.result and self.result.get('bilingual') and
                    not self.result.get('loaded') and not self.result.get('original_only') and
                    not self.result.get('source', '').startswith(('https://', 'http://')) and
                    not self.nas_pending)

    def writeback_nas(self):
        if not self.can_writeback_nas() or self.process:
            return
        self.window.nas_page.accept_video_result(self.result, self.nas_context or self.nas_job_id)

    def nas_feedback(self, message, *, job_id=None, pending=False):
        self.nas_pending = pending
        if job_id is not None:
            self.nas_job_id = job_id
        self.nas_status.setText(message)
        self.nas_button.setEnabled(self.can_writeback_nas() and not self.process)

    def finished(self, process, code):
        if process is not self.process:
            return
        self.events()
        self.process = None
        self.cancel_button.setEnabled(False)
        self.progress.setRange(0, 100)
        self.progress.setFormat('%p%')
        if code or self.last_error or self.cancel_pending:
            self.progress.setValue(0)
        for control in self.controls:
            control.setEnabled(True)
        self.refresh_cached_videos()
        if code and self.cancel_pending and not self.last_error:
            self.report('已取消；已完成缓存保留。')
        elif code and not self.last_error:
            self.report(f'视频任务异常退出（{code}）；已完成缓存保留。')
        self.nas_button.setEnabled(self.can_writeback_nas())
        if (not code and not self.last_error and not self.cancel_pending and
                process.property('nas_auto') and self.nas_auto.isChecked()):
            if self.can_writeback_nas():
                self.writeback_nas()
            else:
                self.nas_status.setText('本次尚无可回传的完整中文与原文对照成品，请先准备对照字幕。')
        process.deleteLater()

    def cancel(self):
        if self.process and not self.cancel_pending:
            self.cancel_pending = True
            self.cancel_button.setEnabled(False)
            process = self.process
            stop_worker(process, lambda: self.process is process)
            self.report('正在取消并清理子进程…')

    def play(self):
        if not self.result:
            return
        try:
            self.player.play(self.players.currentData(), self.result['media'], self.output.currentData(),
                             audio_sink=self.window.fields['headphones'].currentData())
        except ValueError as error:
            self.report(str(error))

    def reload_subtitle(self):
        path = self.output.currentData()
        if (path and self.result and self.player.process and self.player.current_player == 'mpv'
                and self.player.current_media == self.result.get('media')
                and self.player.socket.state() == QLocalSocket.LocalSocketState.ConnectedState):
            self.player.send(['sub-add', path, 'select'])

    def export(self):
        source = self.output.currentData()
        if not source:
            return
        target = choose_file(self, '保存外挂 SRT（只读位置可选择其他目录）',
                    str(subtitle_save_path(self.result, source)), 'SRT (*.srt)', save=True)
        if target:
            try:
                if Path(target).resolve() != Path(source).resolve():
                    shutil.copyfile(source, target)
                self.report('字幕已保存：' + target)
            except OSError as error:
                self.report('无法保存，请再次点击保存并选择可写目录：' + str(error))

    def open_cache(self):
        path = ROOT / 'video-cache'
        path.mkdir(exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def clear_cache(self, kind, all_sources=False):
        if self.process:
            self.report('请等待视频任务完成或取消任务后再删除缓存。')
            return
        try:
            source = None if all_sources else self.source_value()
            title = {'subtitles': '字幕缓存（含翻译和识别进度）',
                     'video': '已下载的 YouTube 视频', 'audio': '识别音轨缓存'}[kind]
            scope = '全部视频' if all_sources else source
            answer = QMessageBox.question(self, '删除缓存',
                f'删除以下范围的{title}？\n{scope}\n\n删除后需要重新生成或下载。'
                '\n只清理应用缓存目录，目录外的原视频和字幕不受影响。\n当前播放器将关闭。',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.player.close()
            self.invalidate_result()
            try:
                count = delete_cache(ROOT / 'video-cache', kind, source)
            finally:
                # Remove stale imported/downloaded selections even after a partial deletion.
                for index in range(self.subtitles.count() - 1, len(SUBTITLE_MODES) - 1, -1):
                    path = (self.subtitles.itemData(index) or {}).get('subtitle_path')
                    if path and not Path(path).is_file():
                        self.subtitles.removeItem(index)
                if self.reference.text() and not Path(self.reference.text()).is_file():
                    self.reference.clear()
                self.inspected_source = None
                self.refresh_cached_videos()
            self.report(f'已删除{title}。' if count else f'没有可删除的{title}。')
        except (OSError, ValueError) as error:
            self.report('无法删除缓存：' + str(error))

    def shutdown(self):
        player_stopped = self.player.close()
        if self.process:
            self.cancel()
            return False
        return player_stopped
