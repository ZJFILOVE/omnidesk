"""Native NAS workspace. QProcess handles network, keychain and filesystem work."""
import json
from pathlib import Path, PurePosixPath
import sys

from PyQt6.QtCore import QProcess, Qt, QTimer
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QHeaderView, QLineEdit,
                             QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from desktop_ui import advanced_section, button, card, field, label, row, scroll_page
from file_dialogs import choose_file
from nas_queue import LABELS
from ui_controls import WheelSafeComboBox, WheelSafeSpinBox
from worker_shutdown import stop_worker

ROOT = Path(__file__).resolve().parent


class NasPage(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.process = None
        self.helpers = set()
        self.buffer = b''
        self.pending = []
        self.entries = []
        self.rows = []
        self.last_config = None
        self.stopping = False
        self.closing = False
        self.paused = False
        self.proofs = {}
        self._last_suspend = None
        try:
            saved = json.loads(window.settings.value('nas/config', '{}'))
        except (ValueError, TypeError):
            saved = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll, layout = scroll_page()
        outer.addWidget(scroll)
        layout.addWidget(label('NAS 自动字幕', 'title'))
        layout.addWidget(label('自动扫描家庭影视库中的电影与剧集，自动匹配或生成中英双语字幕并存回影片目录。', 'muted', True))

        summary_card, summary_col = card('同步状态与快捷控制', '开启后将在后台定期检查新入库的影片；已有影片可在下方清单中勾选后手动处理。')
        self.enabled = QCheckBox('启用后台同步（仅在本应用运行期间）')
        self.enabled.setChecked(bool(saved.get('enabled', False)))
        summary_col.addWidget(self.enabled)
        self.auto = QCheckBox('新入库或画质升级的影片自动制作字幕')
        self.auto.setChecked(saved.get('auto', False))
        summary_col.addWidget(self.auto)
        self.status = label('未启用后台同步。', 'secondary', True)
        summary_col.addWidget(self.status)
        self.pause_button = button('暂停整个队列', self.pause_all)
        summary_col.addLayout(row(button('保存并立即同步', self.apply, 'primary'), self.pause_button, None))
        layout.addWidget(summary_card)

        queue_card, queue_col = card('影视库清单与字幕进度')
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(['影片 / 季集', '字幕来源', '任务状态', '阶段', '进度', '错误 / 说明', '服务路径'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setDefaultSectionSize(130)
        self.table.setColumnWidth(0, 220)
        self.table.setColumnWidth(5, 320)
        self.table.setColumnWidth(6, 400)
        self.table.setMinimumHeight(280)
        queue_col.addWidget(self.table)
        queue_col.addLayout(row(button('处理所选影片', self.enqueue, 'primary'),
                                button('继续 / 重试', lambda: self.selected_command('retry')),
                                button('暂停所选', lambda: self.selected_command('pause')),
                                button('取消所选', lambda: self.selected_command('cancel')),
                                button('在视频字幕页打开', self.open_video), None))
        queue_col.addWidget(label('提示：“字幕已生成”表示已在电脑准备好；“已写回 NAS”表示已存回影视库目录。', 'muted', True))
        self.choices = WheelSafeComboBox()
        field(queue_col, '待确认的候选字幕（当自动匹配存在多个版本时选择）', self.choices)
        queue_col.addLayout(row(button('采用所选字幕', self.choose_candidate),
                                button('手动指定字幕文件…', self.choose_subtitle),
                                button('改用本地语音识别生成', lambda: self.selected_command('fallback')), None))
        self.table.itemSelectionChanged.connect(self.populate_choices)
        layout.addWidget(queue_card)

        adv_widget, adv_layout = advanced_section(
            '高级设置 · 影视库连接、目录映射与字幕规则',
            '配置 Radarr / Sonarr 服务地址、本机挂载文件夹与字幕搜索策略',
        )
        self.connections = {}
        for kind, name, port, folder in (('radarr', 'Radarr · 电影', 7878, 'movies'),
                                          ('sonarr', 'Sonarr · 美剧', 8989, 'tv')):
            old = next((s for s in saved.get('services', []) if s['kind'] == kind), {})
            mapping = next((m for m in saved.get('mappings', []) if m['kind'] == kind), {})
            self.proofs[kind] = mapping
            box, col = card(name)
            use = QCheckBox('同步此服务')
            use.setChecked(old.get('enabled', True))
            col.addWidget(use)
            url = QLineEdit(old.get('url', f'http://192.168.1.100:{port}'))
            key = QLineEdit()
            key.setEchoMode(QLineEdit.EchoMode.Password)
            key.setPlaceholderText('留空使用已保存的系统密钥库凭据')
            field(col, '服务地址', url)
            field(col, 'API Key', key)
            status = label('尚未测试连接', 'muted', True)
            col.addLayout(row(button('保存 Key / 测试连接', lambda checked=False, k=kind: self.test_service(k)),
                              button('忘记 Key', lambda checked=False, k=kind: self.forget_key(k), 'quiet'), None))
            col.addWidget(status)
            remote = QLineEdit(mapping.get('remote', '/var/apps/transmission/shares/transmission/media/' + folder))
            local = QLineEdit(mapping.get('local', ''))
            local.setPlaceholderText('填写已实际挂载的媒体目录；不假定 /mnt/feiniu 下的位置')
            mounted = QCheckBox('要求此目录位于已挂载的 NAS 文件系统')
            mounted.setChecked(mapping.get('require_mount', True))
            field(col, 'API 返回路径的远端前缀', remote)
            field(col, '对应本机目录', local)
            col.addWidget(mounted)
            mapping_status = label(mapping.get('message', '先确认目录可达，再测试读写；不会修改视频。'), 'muted', True)
            col.addWidget(button('测试映射读取 / 临时文件 / 重命名', lambda checked=False, k=kind: self.test_mapping(k)))
            col.addWidget(mapping_status)
            self.connections[kind] = dict(use=use, url=url, key=key, status=status,
                                           remote=remote, local=local, mounted=mounted, mapping_status=mapping_status)
            adv_layout.addWidget(box)

        box, col = card('字幕下载与处理策略', '首次成功连接只建立影片清单，不会自动处理旧库存。')
        self.interval = WheelSafeSpinBox()
        self.interval.setRange(30, 86400)
        self.interval.setSuffix(' 秒')
        self.interval.setValue(saved.get('interval', 300))
        field(col, '后台检查间隔', self.interval)
        options = saved.get('processing', {})
        self.provider = WheelSafeComboBox()
        self.provider.setSizeAdjustPolicy(WheelSafeComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.provider.setMinimumContentsLength(20)
        for title, value in (('先搜索 SubHD，版本不明时人工选择', 'subhd'),
                             ('先搜索射手网 ASSRT（复用视频页已保存的 Token）', 'assrt'),
                             ('不搜索网站，直接使用内嵌字幕或本地语音识别', 'none')):
            self.provider.addItem(title, value)
        self.provider.setCurrentIndex(max(0, self.provider.findData(options.get('search_provider', 'subhd'))))
        field(col, '字幕来源优先级', self.provider)
        self.sync_mode = WheelSafeComboBox()
        for title, value in (('保留匹配字幕时间轴', 'none'), ('自动对齐（英文内嵌参考 / 音轨）', 'auto'), ('分段对齐', 'split')):
            self.sync_mode.addItem(title, value)
        self.sync_mode.setCurrentIndex(max(0, self.sync_mode.findData(options.get('sync_mode', 'none'))))
        field(col, '时间轴处理', self.sync_mode)
        self.zh = QCheckBox('中文字幕 · 视频名.zh.srt')
        self.bilingual = QCheckBox('最终中英对照 · 视频名.bilingual.srt（必选）')
        self.zh.setChecked('zh' in options.get('outputs', ['zh', 'bilingual']))
        self.bilingual.setChecked(True)
        self.bilingual.setEnabled(False)
        col.addWidget(self.zh)
        col.addWidget(self.bilingual)
        col.addWidget(label('中文在上，英文在下。仅有中文时继续补英文原文；无法可靠配对就等待人工补充，不以纯中文宣告完成。', 'muted', True))
        self.update_managed = QCheckBox('允许更新本软件管理且未被外部修改的旧输出')
        self.update_managed.setChecked(options.get('update_managed', False))
        col.addWidget(self.update_managed)
        col.addWidget(label('翻译服务与模型沿用「偏好设置」中的当前选项。队列每次按顺序处理一部影片。', 'muted', True))
        adv_layout.addWidget(box)
        layout.addWidget(adv_widget)
        layout.addStretch()
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        self.enabled.toggled.connect(self.apply)

    def service(self, kind):
        c = self.connections[kind]
        return {'kind': kind, 'url': c['url'].text().strip(), 'enabled': c['use'].isChecked()}

    def mapping(self, kind):
        c = self.connections[kind]
        return {**self.proofs.get(kind, {}), 'kind': kind, 'remote': c['remote'].text().strip(),
                'local': c['local'].text().strip(), 'require_mount': c['mounted'].isChecked()}

    def configuration(self):
        translation = self.window.translation_config()
        key = translation.pop('text_api_key', '')
        from nas_queue import processing_config
        processing = processing_config({**translation,
            'asr_backend': self.window.fields['asr_backend'].currentData(),
            'outputs': [s for s, control in (('zh', self.zh), ('bilingual', self.bilingual)) if control.isChecked()],
            'search_provider': self.provider.currentData(), 'sync_mode': self.sync_mode.currentData(),
            'update_managed': self.update_managed.isChecked()})
        config = {'enabled': self.enabled.isChecked(), 'auto': self.auto.isChecked(),
                  'interval': self.interval.value(), 'services': [self.service(k) for k in self.connections],
                  'mappings': [self.mapping(k) for k in self.connections], 'processing': processing}
        from nas_api import service_id
        for service in config['services']:
            service_id(service)
        return config, key

    def activate(self):
        self.timer.start()
        if self.enabled.isChecked():
            self.apply()

    def apply(self, checked=False):
        try:
            config, key = self.configuration()
        except ValueError as error:
            self.status.setText(str(error))
            return
        self.window.settings.setValue('nas/config', json.dumps(config, ensure_ascii=False))
        self.window.settings.sync()
        if config['enabled'] and not self.closing:
            if self.process is None:
                self.start({**config, 'text_api_key': key})
            else:
                self.send({'command': 'configure', 'config': {**config, 'text_api_key': key}})
                self.send({'command': 'sync'})
        elif self.process:
            self.stop()

    def start(self, config):
        self.stopping = False
        self.last_config = config
        process = QProcess(self)
        self.process = process
        self.buffer = b''
        self.pending = []
        python = ROOT / '.venv/bin/python'
        process.setProgram(str(python) if python.is_file() else sys.executable)
        process.setArguments([str(ROOT / 'nas_worker.py')])
        def started():
            process.write((json.dumps(config, ensure_ascii=False) + '\n').encode())
            process.setProperty('nas_ready', True)
            for command in self.pending:
                process.write((json.dumps(command, ensure_ascii=False) + '\n').encode())
            self.pending.clear()
        process.started.connect(started)
        process.readyReadStandardOutput.connect(self.events)
        process.readyReadStandardError.connect(lambda: process.readAllStandardError())
        process.finished.connect(lambda *_: self.finished(process))
        process.errorOccurred.connect(lambda error: self.finished(process) if error == QProcess.ProcessError.FailedToStart else None)
        self.status.setText('正在恢复队列并补同步…')
        process.start()

    def finished(self, process):
        if self.process is process:
            self.events()
            self.process = None
            self.last_config = None
            self.status.setText('后台已停止；队列和本机生成结果已保存。' if self.stopping else '后台进程退出；检查设置后重新启用。')
            process.deleteLater()

    def send(self, value):
        if self.process and not self.stopping:
            if self.process.property('nas_ready'):
                self.process.write((json.dumps(value, ensure_ascii=False) + '\n').encode())
            else:
                self.pending.append(value)

    def tick(self):
        if self.closing or not self.process or self.stopping:
            return
        try:
            config, key = self.configuration()
        except ValueError:
            return
        current = {**config, 'text_api_key': key}
        if current != self.last_config:
            self.last_config = current
            self.send({'command': 'configure', 'config': current})
        suspended = bool(self.window.video_page.process or self.window.session)
        if suspended != self._last_suspend:
            self._last_suspend = suspended
            self.send({'command': 'suspend', 'value': suspended})

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
            if 'snapshot' in event:
                self.entries = event['snapshot']
                self.paused = event.get('paused', False)
                self.pause_button.setText('继续整个队列' if self.paused else '暂停整个队列')
                self.status.setText('；'.join(event.get('services', {}).values()) or '队列已恢复，正在同步服务…')
                self.render()
                page = self.window.video_page
                if page.nas_job_id:
                    for media in self.entries:
                        for job in media['jobs']:
                            if job['id'] == page.nas_job_id:
                                page.nas_feedback('NAS 回传：' + LABELS.get(job['state'], job['state']) +
                                                  (' · ' + job['error'] if job.get('error') else ''))
            elif 'progress' in event:
                value = event['progress']
                for i, (_, job) in enumerate(self.rows):
                    if job and job['id'] == value['id']:
                        if 'stage' in value:
                            self.table.item(i, 3).setText(value['stage'])
                        if value.get('progress') is not None:
                            self.table.item(i, 4).setText(f"{value['progress']:.0%}")
                        if 'origin' in value:
                            self.table.item(i, 1).setText(value['origin'])
                self.status.setText(value.get('message', ''))
            elif event.get('error'):
                self.status.setText(event['error'])

    def render(self):
        selected = {(m['id'], j['id'] if j else '') for m, j in self.selected()}
        self.rows = [(media, job) for media in self.entries for job in (media['jobs'] or [None])]
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.rows))
        for i, (media, job) in enumerate(self.rows):
            data = media['data']
            state = job['state'] if job else 'listed' if media['present'] else 'missing'
            cells = [data['title'] + ' ' + ' / '.join(data.get('episodes', [])),
                     job.get('origin', '') if job else '', LABELS.get(state, state),
                     job.get('stage', '') if job else '',
                     f"{job['progress']:.0%}" if job and job.get('progress') is not None else '',
                     job.get('error', '') if job else '', data['path']]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                item.setToolTip(str(text))
                self.table.setItem(i, col, item)
                if (media['id'], job['id'] if job else '') in selected:
                    item.setSelected(True)
        self.table.blockSignals(False)
        self.populate_choices()

    def selected(self):
        return [self.rows[i.row()] for i in self.table.selectionModel().selectedRows() if i.row() < len(self.rows)]

    def enqueue(self):
        self.apply()
        self.send({'command': 'enqueue', 'ids': list(dict.fromkeys(m['id'] for m, _ in self.selected()))})

    def selected_command(self, name):
        self.send({'command': name, 'ids': [j['id'] for _, j in self.selected() if j],
                   'update_managed': self.update_managed.isChecked()})

    def pause_all(self):
        self.send({'command': 'pause_all', 'value': not self.paused})

    def populate_choices(self):
        self.choices.clear()
        selected = self.selected()
        if len(selected) != 1 or not selected[0][1]:
            return
        choices = (selected[0][1].get('override') or {}).get('choices', {})
        for value in choices.get('candidates', []):
            self.choices.addItem('网站条目 · ' + value['title'], {'candidate': value})
        for value in choices.get('files', []):
            self.choices.addItem('下载文件 · ' + value['title'], {'subtitle_path': value['path']})

    def choose_candidate(self):
        if self.choices.currentData():
            self.send({'command': 'choose', 'ids': [j['id'] for _, j in self.selected() if j],
                       'override': self.choices.currentData()})

    def choose_subtitle(self):
        if len(self.selected()) != 1 or not self.selected()[0][1]:
            return
        path = choose_file(self, '选择已核对匹配版本的文本字幕', str(Path.home()), '字幕 (*.srt *.ass *.ssa *.vtt)')
        if path:
            self.send({'command': 'choose', 'ids': [self.selected()[0][1]['id']], 'override': {'subtitle_path': path}})

    def open_video(self):
        selected = self.selected()
        if len(selected) != 1:
            self.status.setText('请先选择一个影片 / 任务。')
            return
        media, job = selected[0]
        result = job.get('result') if job else None
        path = result.get('source') if result else None
        if not path:
            kind = media['service'].split(':', 1)[0]
            mapping = self.mapping(kind)
            remote = PurePosixPath(media['data']['path'])
            prefix = PurePosixPath(mapping['remote'])
            if mapping.get('local') and remote.is_relative_to(prefix):
                path = str(Path(mapping['local']) / str(remote.relative_to(prefix)))
        if not path:
            self.status.setText('先填写此影片的本机路径映射。')
            return
        page = self.window.video_page
        if page.process:
            self.status.setText('视频页当前有运行中的任务，请先结束后打开。')
            return
        page.source.setText(path)
        page.nas_context = job['id'] if job else None
        page.nas_auto.setChecked(True)
        from subtitle_sources import search_name
        page.query.setText(search_name(Path(media['data']['path']).name))
        if job:
            choices = (job.get('override') or {}).get('choices', {})
            page.candidates.clear()
            for candidate in choices.get('candidates', []):
                page.candidates.addItem(candidate['title'], candidate)
            if choices.get('files'):
                page.add_files(choices['files'])
        if result:
            page.show_result(result)
        self.window.navigate(4)

    def accept_video_result(self, prepared, job_id=None):
        page = self.window.video_page
        if page.nas_pending:
            return
        try:
            config, _ = self.configuration()
        except ValueError as error:
            page.nas_feedback(str(error))
            return
        page.nas_job_id = None
        page.nas_feedback('正在核对视频版本并接回 NAS 任务…', pending=True)
        def done(response):
            # A helper may finish after the user has opened another video.
            if page.result is not prepared:
                page.nas_pending = False
                page.nas_button.setEnabled(page.can_writeback_nas() and not page.process)
                return
            if 'nas_error' in response:
                page.nas_feedback('回传未完成：' + response['nas_error']['message'])
                return
            result = response['nas_result']
            message = result['message']
            if not self.enabled.isChecked():
                message += ' 请在 NAS 自动字幕页启用后台同步后继续。'
            elif self.paused:
                message += ' 请在 NAS 自动字幕页继续整个队列。'
            page.nas_feedback(message, job_id=result['job_id'])
        self.helper({'action': 'nas_adopt', 'database': str(ROOT / 'nas-cache' / 'queue.sqlite3'),
                     'prepared': prepared, 'mappings': config['mappings'],
                     'processing': config['processing'], 'job_id': job_id}, done)

    def helper(self, config, callback):
        process = QProcess(self)
        self.helpers.add(process)
        python = ROOT / '.venv/bin/python'
        process.setProgram(str(python) if python.is_file() else sys.executable)
        process.setArguments([str(ROOT / 'video_worker.py')])
        data = bytearray()
        process.started.connect(lambda: process.write((json.dumps(config, ensure_ascii=False) + '\n').encode()))
        process.readyReadStandardOutput.connect(lambda: data.extend(bytes(process.readAllStandardOutput())))
        process.readyReadStandardError.connect(lambda: process.readAllStandardError())
        def done(*_):
            if process not in self.helpers:
                return
            self.helpers.remove(process)
            data.extend(bytes(process.readAllStandardOutput()))
            response = {'nas_error': {'message': '工作进程无法完成测试。'}}
            for line in bytes(data).splitlines():
                try:
                    value = json.loads(line)
                    if 'nas_result' in value or 'nas_error' in value:
                        response = value
                except ValueError:
                    pass
            callback(response)
            process.deleteLater()
        process.finished.connect(done)
        process.errorOccurred.connect(lambda e: done() if e == QProcess.ProcessError.FailedToStart else None)
        # Bound a stalled mount/keychain test without blocking Qt's event loop.
        QTimer.singleShot(60000, lambda: stop_worker(process, lambda: process in self.helpers)
                          if process in self.helpers else None)
        process.start()

    def test_service(self, kind):
        controls = self.connections[kind]
        controls['status'].setText('正在测试连接…')
        def done(response):
            if 'nas_result' in response:
                controls['key'].clear()
            controls['status'].setText(response.get('nas_result', response.get('nas_error', {})).get('message', ''))
        self.helper({'action': 'nas_test', 'service': self.service(kind), 'key': controls['key'].text().strip()}, done)

    def forget_key(self, kind):
        controls = self.connections[kind]
        controls['key'].clear()
        self.helper({'action': 'nas_key', 'operation': 'clear', 'service': self.service(kind)},
                    lambda response: controls['status'].setText(response.get('nas_result', response.get('nas_error', {})).get('message', '')))

    def test_mapping(self, kind):
        controls = self.connections[kind]
        mapping = self.mapping(kind)
        controls['mapping_status'].setText('正在后台测试映射目录…')
        def done(response):
            if 'nas_result' in response:
                result = response['nas_result']
                if self.mapping(kind)['remote'] == mapping['remote'] and self.mapping(kind)['local'] == mapping['local']:
                    self.proofs[kind] = result
                    self.apply()
                controls['mapping_status'].setText(result['message'])
            else:
                controls['mapping_status'].setText(response['nas_error']['message'])
        self.helper({'action': 'nas_probe', 'mapping': mapping}, done)

    def stop(self):
        if self.process and not self.stopping:
            self.stopping = True
            process = self.process
            stop_worker(process, lambda: self.process is process)

    def shutdown(self):
        self.closing = True
        self.timer.stop()
        self.stop()
        for process in list(self.helpers):
            if not process.property('nas_stopping'):
                process.setProperty('nas_stopping', True)
                stop_worker(process, lambda p=process: p in self.helpers)
        return self.process is None and not self.helpers
