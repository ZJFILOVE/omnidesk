"""Argument-only player launching and asynchronous mpv JSON IPC."""
import json
from pathlib import Path
import shutil
import tempfile

from PyQt6.QtCore import QObject, QProcess, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtNetwork import QLocalSocket


def player_command(player, media, subtitle, socket=None, *, audio_sink=None):
    if player == 'mpv':
        # Device settings come from pactl, so use its sink name through mpv's
        # PulseAudio backend (also supported by PipeWire's PulseAudio server).
        # Autoselection can otherwise send video audio to a virtual meeting or
        # streaming sink even when the user selected physical headphones.
        audio = ['--audio-device=pulse/' + audio_sink] if audio_sink else []
        online = []
        if media.startswith(('https://', 'http://')):
            downloader = Path(__file__).resolve().parent / '.venv/bin/yt-dlp'
            online = ['--ytdl=yes', '--ytdl-raw-options=js-runtimes=node']
            if downloader.is_file():
                online += ['--script-opts-append=ytdl_hook-ytdl_path=' + str(downloader)]
        return ['--force-window=yes', '--load-scripts=no', '--sub-auto=no', *audio, *online,
                '--sub-font-size=28', '--sub-margin-x=12',
                '--input-ipc-server=' + socket, '--sub-file=' + subtitle, '--', media]
    if media.startswith(('https://', 'http://')):
        raise ValueError('YouTube 在线播放请选择 mpv，或先勾选下载视频再使用其他播放器。')
    if player == 'vlc':
        return ['--no-one-instance', '--sub-file=' + subtitle, '--', media]
    if player == 'smplayer':
        return ['-new-instance', '-sub', subtitle, media]
    raise ValueError('此播放器尚未实现自动传入字幕；请导出 SRT 后在播放器中加载。')


class VideoPlayer(QObject):
    status = pyqtSignal(str)
    position = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.process = None
        self.current_media = None
        self.current_player = None
        self.directory = None
        self._stopping = {}
        self.socket = QLocalSocket(self)
        self.socket.connected.connect(self.connected)
        self.socket.readyRead.connect(self.read)
        self.buffer = b''
        self.timer = QTimer(self)
        self.timer.setInterval(150)
        self.timer.timeout.connect(self.connect_socket)
        self.attempts = 0
        self.request_id = 100

    def play(self, player, media, subtitle, *, audio_sink=None):
        executable = shutil.which(player)
        if not executable:
            raise ValueError(f'未安装 {player}，请选择已安装的播放器；也可以导出 SRT 在其他播放器加载。')
        directory = tempfile.TemporaryDirectory(prefix='qwen-player-')
        socket_path = str(Path(directory.name) / 'ipc')
        try:
            args = player_command(player, media, subtitle, socket_path, audio_sink=audio_sink)
        except ValueError:
            directory.cleanup()
            raise
        self.close()
        self.directory = directory
        self.socket_path = socket_path
        process = QProcess(self)
        self.process = process
        self.current_media = media
        self.current_player = player
        process.setProgram(executable)
        process.setArguments(args)
        process.setStandardOutputFile('/dev/null')
        process.setStandardErrorFile('/dev/null')
        process.started.connect(self.started)
        process.finished.connect(self.finished)
        process.errorOccurred.connect(self.failed)
        self.buffer = b''
        process.start()

    @pyqtSlot()
    def started(self):
        process = self.sender()
        if process in self._stopping:
            # close() may be requested before QProcess has a child PID.
            self._stopping[process][1].start(1000)
            process.terminate()
        elif process is not self.process:
            return
        elif self.current_player == 'mpv':
            self.attempts = 0
            self.timer.start()
        else:
            self.status.emit('已启动播放器；播放控制请使用播放器窗口。')

    @pyqtSlot(int, QProcess.ExitStatus)
    def finished(self, code, status):
        process = self.sender()
        current = process is self.process
        self.release(process)
        if current:
            self.status.emit('播放器异常退出。' if status == QProcess.ExitStatus.CrashExit else '播放器已退出。')

    @pyqtSlot(QProcess.ProcessError)
    def failed(self, error):
        process = self.sender()
        # Stopping/replaced players must not access a deleted wrapper or update
        # the status and IPC connection belonging to the current player.
        if process is not self.process:
            if error == QProcess.ProcessError.FailedToStart:
                self.release(process)
            return
        message = ('播放器启动失败：' if error == QProcess.ProcessError.FailedToStart else '播放器错误：')
        message += process.errorString()
        if error == QProcess.ProcessError.FailedToStart:
            self.release(process)
        self.status.emit(message)

    def release(self, process):
        if process is self.process:
            directory, self.directory = self.directory, None
            self.process = None
            self.current_media = None
            self.current_player = None
            self.timer.stop()
            self.socket.abort()
        elif process in self._stopping:
            directory, timer = self._stopping.pop(process)
            timer.stop()
        else:
            return
        if directory:
            directory.cleanup()
        # Only finished/FailedToStart or an already stopped process gets here.
        process.deleteLater()

    def connect_socket(self):
        if self.process is None:
            self.timer.stop()
            return
        self.attempts += 1
        if self.attempts > 60:
            self.timer.stop()
            self.status.emit('播放器控制连接未建立，请使用播放器窗口。')
            return
        self.socket.abort()
        self.socket.connectToServer(self.socket_path)

    def connected(self):
        self.timer.stop()
        self.send(['observe_property', 1, 'time-pos'])
        self.status.emit('mpv 已连接，字幕随播放时间同步。')

    def send(self, command):
        if self.socket.state() == QLocalSocket.LocalSocketState.ConnectedState:
            self.request_id += 1
            self.socket.write((json.dumps({'command': command, 'request_id': self.request_id}) + '\n').encode())
        else:
            self.status.emit('播放控制仅在 mpv 连接后可用。')

    def read(self):
        self.buffer += bytes(self.socket.readAll())
        while b'\n' in self.buffer:
            line, self.buffer = self.buffer.split(b'\n', 1)
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('name') == 'time-pos' and isinstance(event.get('data'), (int, float)):
                self.position.emit(event['data'])
            if event.get('error') not in (None, 'success'):
                self.status.emit('播放器命令未完成：' + str(event['error']))

    def close(self):
        """Request asynchronous shutdown; return true once every player exits."""
        self.current_media = None
        self.current_player = None
        self.timer.stop()
        self.socket.abort()
        if self.process:
            process, self.process = self.process, None
            directory, self.directory = self.directory, None
            timer = QTimer(process)
            timer.setSingleShot(True)
            timer.timeout.connect(process.kill)
            self._stopping[process] = (directory, timer)
            if process.state() == QProcess.ProcessState.NotRunning:
                self.release(process)
            else:
                timer.start(1000)
                process.terminate()
        # A blocked child can outlive SIGKILL. Retain its QProcess and IPC
        # directory until finished instead of deleting a live Qt object.
        return not self._stopping
