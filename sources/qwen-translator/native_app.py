import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from PyQt6.QtCore import QLockFile, QProcess, QSettings, QStandardPaths, Qt, QTimer
from PyQt6.QtWidgets import QApplication, QMainWindow, QMessageBox, QTableWidgetItem
from PyQt6.QtGui import QKeySequence, QShortcut
from ui_tokens import LAYOUT

from desktop_ui import build_window, button, label, nav_icon, PAGE_TITLES
from caption_window import CaptionWindow
from ui_theme import THEMES, apply_theme


APP_DIR = Path(__file__).resolve().parent
MODES = [("麦克风试听 · 说中文听英文", "mic_test"), ("只听对方 · 英文转中文语音", "inbound"),
         ("只说中文 · 译音送入会议", "outbound"), ("双向同传 · 会议中英互译", "duplex"),
         ("实时字幕 · 仅看双语字幕（静音）", "subtitles")]


def preferred_device(kind, entries, result):
    values = {value for _, value in entries}
    if kind == "headphones":
        default_sink = (result.get("default_sink") or "").strip()
        if default_sink in values:
            return default_sink
        keywords = ("bluez", "bluetooth", "headphone", "headset", "earphone", "耳机", "usb")
    else:
        default_source = (result.get("default_source") or "").strip()
        unmuted = {item["name"] for item in result.get("sources", []) if not item.get("mute")}
        if default_source in values and (not unmuted or default_source in unmuted):
            return default_source
        keywords = ("bluez", "bluetooth", "headset", "mic", "麦克风", "耳麦", "usb")
        entries = [(title, value) for title, value in entries if not unmuted or value in unmuted] or entries
    for title, value in entries:
        text = f"{title} {value}".lower()
        if any(word in text for word in keywords):
            return value
    return entries[0][1] if entries else None


class Translator(QMainWindow):
    def __init__(self, settings=None, initialize=True):
        super().__init__()
        self.setWindowTitle("同声 · 实时翻译与字幕助手")
        self.resize(1380, 900)
        self.setMinimumSize(680, 660)
        self.sync_waybar = settings is None
        self.settings = settings if settings is not None else QSettings("QwenLiveTranslate", "Desktop")
        self.jobs = set()
        self.session = None
        self.session_buffer = b""
        self.closing = False
        self.device_loading = False
        self.devices_ready = False
        self.asr_ready = False
        self.text_request = 0
        self.text_test_loading = False
        self.codex_models_loading = False
        self.antigravity_models_loading = False
        self.codex_model_catalog = {}
        self.caption_sequences = {}
        self.session_error = False
        self.stop_requested = False
        self.session_record = None
        self.session_started = None
        self.recovery_target = 3
        self.key_request = 0
        self.overlay = CaptionWindow(self.settings)
        self.theme = self.settings.value("theme", "light")
        build_window(self, MODES)
        from music_companion_ui import install_music_companion
        self.music_companion = install_music_companion(self.music_page)
        self.transcript_split.splitterMoved.connect(self.save_reading_layout)
        self.shortcuts = []
        for index in range(len(PAGE_TITLES)):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{index + 1}"), self)
            shortcut.activated.connect(lambda i=index: self.navigate(i))
            self.shortcuts.append(shortcut)
        caption_shortcut = QShortcut(QKeySequence("F9"), self)
        caption_shortcut.activated.connect(self.toggle_captions)
        self.shortcuts.append(caption_shortcut)
        for index, item in enumerate(self.nav_buttons):
            item.setToolTip(f"{PAGE_TITLES[index]} · Ctrl+{index + 1}")
        self.caption_button.setToolTip("显示或隐藏悬浮字幕 · F9")
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.update_duration)
        self.set_theme(self.theme, persist=False)
        self.render_history()
        self.route()
        QTimer.singleShot(0, self.restore_reading_layout)
        if initialize:
            self.nas_page.activate()
            self.music_page.activate()
            self.refresh()
            self.load_key()
            self.refresh_codex_models()
            self.refresh_antigravity_models()
            if self.text_fields["text_provider"].currentData() == "api" and self.text_fields["text_endpoint"].text().strip():
                self.text_key_action("lookup")

    def restore(self, combo, name):
        index = combo.findData(self.settings.value(name))
        if index >= 0:
            combo.setCurrentIndex(index)

    def navigate(self, index):
        self.pages.setCurrentIndex(index)
        self.nav_buttons[index].setChecked(True)
        self.breadcrumb.setText("同声  ·  " + PAGE_TITLES[index])
        self.update_navigation_colors()
        if index == 1:
            self.render_history()

    def set_theme(self, name, persist=True):
        self.theme = name if name in THEMES else "light"
        self.colors = apply_theme(QApplication.instance(), self.theme)
        self.theme_combo.blockSignals(True)
        self.theme_combo.setCurrentIndex(self.theme_combo.findData(self.theme))
        self.theme_combo.blockSignals(False)
        self.update_navigation_colors()
        for panel in self.transcripts.values():
            panel.colors = self.colors
            panel.render()
        self.adapt_layout()
        if self.sync_waybar:
            from waybar_theme import sync_theme
            try:
                appearance = getattr(self.music_page, 'caption_appearance', None)
                if appearance is not None:
                    from caption_style import resolve_appearance
                    options = resolve_appearance(appearance.options)
                else:
                    options = None
                sync_theme(self.theme, appearance=options)
                QProcess.startDetached('pkill', ['-SIGUSR2', '-x', 'waybar'])
            except (OSError, ValueError):
                pass
        if persist:
            self.settings.setValue("theme", self.theme)
            self.settings.sync()

    def update_navigation_colors(self):
        if not hasattr(self, "colors"):
            return
        for index, item in enumerate(self.nav_buttons):
            item.setIcon(nav_icon(index, self.colors["accent" if item.isChecked() else "secondary"]))

    def toggle_theme(self):
        self.set_theme("dark" if self.theme == "light" else "light")

    def translation_config(self):
        from PyQt6.QtWidgets import QComboBox
        result = {name: control.currentData() if isinstance(control, QComboBox) else control.text().strip()
                  for name, control in self.text_fields.items()}
        result.update(text_api_key=self.text_key.text().strip(), vad_threshold=self.vad_threshold.value(),
                      subtitle_language=self.fields["subtitle_language"].currentData())
        return result

    def update_text_provider(self):
        provider = self.text_fields["text_provider"].currentData()
        self.codex_options.setVisible(provider == "codex")
        self.antigravity_options.setVisible(provider == "antigravity")
        self.api_options.setVisible(provider == "api")
        self.text_test_button.setEnabled(provider != "none" and self.session is None and not self.text_test_loading)
        self.codex_refresh_button.setEnabled(self.session is None and not self.codex_models_loading)
        self.antigravity_refresh_button.setEnabled(self.session is None and not self.antigravity_models_loading)
        self.route()

    def refresh_antigravity_models(self):
        if self.antigravity_models_loading or self.session:
            return
        self.antigravity_models_loading = True
        self.antigravity_refresh_button.setEnabled(False)
        self.antigravity_refresh_button.setText("正在读取…")
        self.antigravity_model_status.setText("正在读取本机 Antigravity 模型列表…")
        self.job({"action": "antigravity_models"}, self.populate_antigravity_models)

    def populate_antigravity_models(self, result):
        models = result["models"]
        if not models:
            raise ValueError("Antigravity 未返回可选模型，请检查登录后刷新。")
        combo = self.text_fields["antigravity_model"]
        selected = combo.currentData()
        combo.blockSignals(True)
        try:
            combo.clear()
            for item in models:
                combo.addItem(f'{item["displayName"]} ({item["model"]})', item["model"])
            missing = bool(selected and combo.findData(selected) < 0)
            if missing:
                combo.addItem(selected + " · 当前列表未提供", selected)
            if not selected:
                selected = next((item["model"] for item in models
                                 if "flash" in item["model"] and item["model"].endswith("-low")), models[0]["model"])
            combo.setCurrentIndex(max(0, combo.findData(selected)))
        finally:
            combo.blockSignals(False)
        self.settings.setValue("antigravity_model", combo.currentData())
        self.settings.sync()
        self.antigravity_model_status.setText(f"已读取 {len(models)} 个模型。" +
            (" 上次选择不在当前列表中，建议重新选择。" if missing else ""))

    def save_codex_model(self):
        model = self.text_fields["codex_model"].currentData()
        if model:
            self.settings.setValue("codex_model", model)
            self.settings.sync()
        self.update_codex_efforts()

    def save_codex_effort(self):
        model = self.text_fields["codex_model"].currentData()
        effort = self.text_fields["codex_reasoning_effort"].currentData()
        if model and effort:
            self.settings.setValue("codex_efforts/" + model, effort)
            self.settings.setValue("codex_reasoning_effort", effort)
            self.settings.sync()

    def update_codex_efforts(self):
        model = self.text_fields["codex_model"].currentData()
        metadata = self.codex_model_catalog.get(model, {})
        options = metadata.get("supportedReasoningEfforts", [])
        combo = self.text_fields["codex_reasoning_effort"]
        saved = self.settings.value("codex_efforts/" + model, "low")
        titles = {"none": "不思考", "minimal": "极少", "low": "低 · 实时字幕建议",
                  "medium": "中", "high": "高", "xhigh": "很高", "max": "最高", "ultra": "超高"}
        combo.blockSignals(True)
        try:
            combo.clear()
            default = metadata.get("defaultReasoningEffort")
            combo.addItem("模型默认" + (f" ({default})" if default else ""), "default")
            for option in options:
                effort = option["reasoningEffort"]
                combo.addItem(effort + " · " + titles.get(effort, effort), effort)
                combo.setItemData(combo.count() - 1, option.get("description", ""), Qt.ItemDataRole.ToolTipRole)
            # Before discovery, do not guess the model's supported levels.
            combo.setCurrentIndex(max(0, combo.findData(saved)))
        finally:
            combo.blockSignals(False)

    def refresh_codex_models(self):
        if self.codex_models_loading or self.session:
            return
        self.codex_models_loading = True
        self.codex_refresh_button.setEnabled(False)
        self.codex_refresh_button.setText("正在读取…")
        self.codex_model_status.setText("正在读取本机 Codex 模型列表…")
        self.job({"action": "codex_models"}, self.populate_codex_models)

    def populate_codex_models(self, result):
        combo = self.text_fields["codex_model"]
        selected = combo.currentData()
        models = result["models"]
        if not models:
            raise ValueError("Codex 未返回可选模型，请检查登录状态后刷新。")
        self.codex_model_catalog = {item["model"]: item for item in models}
        combo.blockSignals(True)
        try:
            combo.clear()
            for item in models:
                model = item["model"]
                title = item["displayName"]
                combo.addItem(title if title == model else f"{title} ({model})", model)
                combo.setItemData(combo.count() - 1, item.get("description", ""), Qt.ItemDataRole.ToolTipRole)
            missing = bool(selected and combo.findData(selected) < 0)
            if missing:
                combo.addItem(selected + " · 当前列表未提供", selected)
            combo.setCurrentIndex(max(0, combo.findData(selected)))
        finally:
            combo.blockSignals(False)
        self.save_codex_model()
        self.codex_model_status.setText(
            f"已读取 {len(models)} 个模型，点击菜单选择。" +
            (" 上次选择不在当前列表中，建议重新选择。" if missing else ""))

    def text_endpoint_changed(self):
        self.text_request += 1
        self.text_key.clear()
        self.text_status.setText("地址已更改。请读取该地址保存的 Key，或重新填写。")

    def text_key_action(self, action):
        endpoint = self.text_fields["text_endpoint"].text().strip()
        if not endpoint:
            self.text_status.setText("请先填写文本 API 地址。")
            return
        self.text_request += 1
        request = self.text_request
        value = self.text_key.text().strip()
        if action == "clear":
            self.text_key.clear()
        def done(result):
            if request != self.text_request or endpoint != self.text_fields["text_endpoint"].text().strip():
                return
            if action == "lookup" and not self.text_key.isModified():
                self.text_key.setText(result["key"])
            self.text_status.setText({"lookup": "已读取保存的 Key" if result["key"] else "该地址尚未保存 Key，本地免鉴权服务可留空。",
                                      "store": "文本 API Key 已保存到系统密钥库", "clear": "已忘记该地址的 Key"}[action])
        self.job({"action": "text_" + action, "text_endpoint": endpoint, "key": value}, done)

    def validate_text_config(self):
        config = self.translation_config()
        if config["text_provider"] == "api":
            from text_translation import api_url
            api_url(config["text_endpoint"], config["api_format"])
            if not config["text_model"]:
                raise ValueError("请填写文本翻译模型名称。")
        elif config["text_provider"] == "codex" and not config["codex_model"]:
            raise ValueError("请从菜单选择 Codex 模型；列表为空时点击刷新模型列表。")
        elif config["text_provider"] == "antigravity" and not config["antigravity_model"]:
            raise ValueError("请刷新模型列表并选择 Antigravity 模型。")
        return config

    def test_translation(self):
        if self.text_test_loading or self.session:
            return
        try:
            config = self.validate_text_config()
        except ValueError as error:
            self.text_status.setText(str(error))
            return
        self.persist()
        self.text_test_loading = True
        self.text_test_button.setEnabled(False)
        self.text_test_button.setText("正在测试…")
        self.text_status.setText("正在发送固定测试句，请稍候…")
        self.job({**config, "action": "test_translation"},
                 lambda result: self.text_status.setText("连接成功：" + result["text"] + "\n" + self.usage_text(result)))

    @staticmethod
    def usage_text(result):
        if result.get("cache_hit"):
            return "本次复用相同字幕译文 · 未请求模型"
        usage = result.get("usage") or {}
        incoming = usage.get("input_tokens", usage.get("prompt_tokens"))
        outgoing = usage.get("output_tokens", usage.get("completion_tokens"))
        if incoming is None or outgoing is None:
            return "服务未返回 token 用量"
        cached = usage.get("cached_input_tokens", (usage.get("input_tokens_details") or
                           usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
        reasoning = usage.get("reasoning_output_tokens", (usage.get("output_tokens_details") or
                              usage.get("completion_tokens_details") or {}).get("reasoning_tokens"))
        return (f"本次 token：输入 {incoming}（缓存命中 {cached}），输出 {outgoing}" +
                (f"（含思考 {reasoning}）" if reasoning is not None else ""))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "nav_buttons"):
            self.adapt_layout()

    def adapt_layout(self):
        compact = self.width() < 1180
        narrow = self.width() < 900
        self.sidebar.setFixedWidth(LAYOUT['railWidth'] if compact else LAYOUT['sidebarWidth'])
        for widget in (self.brand_text, self.nav_caption, self.media_nav_caption, self.sidebar_note):
            widget.setVisible(not compact)
        for index, b in enumerate(self.nav_buttons):
            b.setText("" if compact else PAGE_TITLES[index])
        theme_text = "切换深色外观" if self.theme == "light" else "切换浅色外观"
        self.theme_button.setText(("深" if self.theme == "light" else "浅") if compact else theme_text)
        self.theme_button.setToolTip(theme_text)
        self.theme_button.setAccessibleName(theme_text)
        self.live_layout.setContentsMargins(18 if narrow else 28, 20, 18 if narrow else 28, 18)
        orientation = Qt.Orientation.Vertical if narrow else Qt.Orientation.Horizontal
        if self.transcript_split.orientation() != orientation:
            self.transcript_split.setOrientation(orientation)
            self.restore_reading_layout()
        self.metrics.itemAt(2).widget().setVisible(not narrow)

    def reading_layout_key(self):
        return 'layout/reading-' + ('horizontal' if self.transcript_split.orientation() == Qt.Orientation.Horizontal else 'vertical')

    def save_reading_layout(self, *_):
        sizes = self.transcript_split.sizes()
        if len(sizes) == 2 and all(sizes):
            self.settings.setValue(self.reading_layout_key(), sizes)

    def restore_reading_layout(self):
        sizes = self.settings.value(self.reading_layout_key(), [400, 400])
        try:
            sizes = [int(value) for value in sizes]
            if len(sizes) != 2 or not all(value > 0 for value in sizes):
                raise ValueError('Invalid reading layout')
        except (ValueError, TypeError):
            sizes = [400, 400]
        self.transcript_split.setSizes(sizes)

    def dismiss_onboarding(self):
        self.onboarding.hide()
        self.settings.setValue("onboarded", True)

    def show_onboarding(self):
        self.onboarding.show()
        self.navigate(0)

    def prepare_test(self):
        if self.session:
            return
        self.fields["mode"].setCurrentIndex(self.fields["mode"].findData("mic_test"))
        self.navigate(0)
        self.status.setText("已准备好试听：点击「开始翻译」后说一句中文，就能在耳机里听到英文译音。")
        self.update_onboarding()

    def update_onboarding(self):
        connected = bool(self.key.text().strip())
        device = bool(self.fields["headphones"].currentData() and self.fields["microphone"].currentData())
        self.setup_buttons[0].setText("1  密钥已就绪" if connected else "1  配置翻译服务")
        self.setup_buttons[1].setText("2  设备已就绪" if device else "2  选择耳机与麦克风")
        self.setup_buttons[2].setEnabled(connected and device and self.session is None)

    def set_badge(self, text, state="idle"):
        self.badge.setText(text)
        self.badge.setProperty("state", state)
        self.badge.style().unpolish(self.badge)
        self.badge.style().polish(self.badge)

    def redact(self, value):
        text = str(value)
        if self.key.text().strip():
            text = text.replace(self.key.text().strip(), "[已隐藏凭据]")
        if self.text_key.text().strip():
            text = text.replace(self.text_key.text().strip(), "[已隐藏凭据]")
        return re.sub(r"\bsk-[A-Za-z0-9_-]+", "[已隐藏凭据]", text)[:700]

    def show_error(self, error, affects_session=False):
        detail = self.redact(error)
        lower = detail.lower()
        self.recovery_target = 3
        if any(term in lower for term in ("api key", "api_key", "401", "403", "unauthorized", "authentication", "invalid_api_key")):
            title = "API 连接未通过验证"
            hint = "翻译未能继续。请核对服务区域、API Key 与模型权限，然后重新开始。"
            recovery = "检查 API 设置"
        elif any(term in lower for term in ("device", "microphone", "capture", "playback", "音频", "设备", "静音", "connection refused")):
            title = "音频设备暂不可用"
            hint = "无法采集或播放译音。请确认设备已连接、麦克风已取消静音，再刷新设备。"
            recovery = "检查设备"
            self.recovery_target = 2
        elif any(term in lower for term in ("network", "connect", "timeout", "closed", "dns", "websocket", "网络")):
            title = "翻译连接已中断"
            hint = "本次翻译无法继续。请检查网络，连接恢复后点击开始翻译重试。"
            recovery = "返回实时翻译"
            self.recovery_target = 0
        else:
            title = "暂时无法完成操作"
            hint = "请检查连接与设备设置后重试。"
            recovery = "检查设置"
        self.error_title.setText(title)
        self.error_detail.setText(hint + ("\n" + detail if detail else ""))
        self.recovery_button.setText(recovery)
        self.error_card.show()
        self.status.setText(title + " · " + hint)
        self.set_badge("需要处理", "error")
        if self.session and affects_session:
            self.session_error = True

    def recover(self):
        self.navigate(self.recovery_target)
        if self.recovery_target == 2 and not self.session:
            self.refresh()

    def job(self, config, callback):
        process = QProcess(self)
        self.jobs.add(process)
        process.setProgram(str(APP_DIR / ".venv/bin/python"))
        process.setArguments([str(APP_DIR / "native_worker.py")])
        process.started.connect(lambda: (process.write((json.dumps(config) + "\n").encode()), process.closeWriteChannel()))
        action = config["action"]

        def cleanup():
            self.jobs.discard(process)
            if action == "antigravity_models":
                self.antigravity_models_loading = False
                self.antigravity_refresh_button.setText("刷新模型列表")
                self.antigravity_refresh_button.setEnabled(self.session is None)
            if action == "codex_models":
                self.codex_models_loading = False
                self.codex_refresh_button.setText("刷新模型列表")
                self.codex_refresh_button.setEnabled(self.session is None)
            if action == "devices":
                self.device_loading = False
                self.refresh_button.setEnabled(self.session is None)
                self.refresh_button.setText("刷新设备")
            if action == "test_translation":
                self.text_test_loading = False
                self.text_test_button.setText("测试翻译连接")
                self.update_text_provider()
            process.deleteLater()

        def finished():
            raw = bytes(process.readAllStandardOutput())
            try:
                result = json.loads(raw)
                if "error" in result:
                    raise RuntimeError(result["error"])
                callback(result)
            except Exception as error:
                message = str(error) if raw else "后台任务失败，请检查运行环境。"
                if action == "antigravity_models":
                    self.antigravity_model_status.setText(self.redact(message) + " 已保留原选择，可点击刷新重试。")
                    return
                if action == "codex_models":
                    self.codex_model_status.setText(self.redact(message) + " 已保留原选择，可点击刷新重试。")
                    return
                if action.startswith("text_") or action == "test_translation":
                    self.text_status.setText(self.redact(message))
                    return
                if action in {"lookup", "store", "clear"}:
                    self.connection_status.setText(self.redact(message))
                    if self.session:
                        self.status.setText("密钥库操作失败；当前翻译继续。请稍后在偏好设置中检查。")
                        return
                elif action == "devices":
                    self.device_status.setText(self.redact(message))
                self.show_error(message)
            finally:
                cleanup()

        def failed(error):
            if error == QProcess.ProcessError.FailedToStart:
                if action == "antigravity_models":
                    self.antigravity_model_status.setText("无法读取模型列表，请检查运行环境后刷新。")
                elif action == "codex_models":
                    self.codex_model_status.setText("无法读取模型列表，请检查运行环境后刷新。")
                elif action.startswith("text_") or action == "test_translation":
                    self.text_status.setText("无法启动文本翻译任务：" + process.errorString())
                else:
                    self.show_error("无法启动后台任务：" + process.errorString())
                cleanup()

        process.finished.connect(finished)
        process.errorOccurred.connect(failed)
        process.start()

    def load_key(self):
        region = self.region.currentData()
        self.key_request += 1
        request = self.key_request
        self.key.clear()
        self.connection_status.setText("正在读取系统密钥库…")

        def loaded(result):
            if request == self.key_request and self.region.currentData() == region and not self.key.isModified():
                self.key.setText(result["key"])
                message = "已从系统密钥库读取 Key" if result["key"] else "首次使用：请输入并保存 API Key"
                self.connection_status.setText(message)
                if not self.session and self.fields["mode"].currentData() != "subtitles":
                    self.status.setText(message)
                self.update_onboarding()
                if result["key"] and not self.settings.contains("onboarded"):
                    self.onboarding.hide()

        self.job({"action": "lookup", "region": region}, loaded)

    def save_key(self, checked=False):
        if not self.key.text().strip():
            self.connection_status.setText("请输入 API Key 后再保存。")
            return
        region = self.region.currentData()

        def saved(result):
            if self.region.currentData() == region:
                self.connection_status.setText("API Key 已保存到系统密钥库")
            self.update_onboarding()

        self.job({"action": "store", "region": region, "key": self.key.text().strip()}, saved)

    def forget_key(self):
        self.key_request += 1
        self.key.clear()
        self.remember.setChecked(False)
        self.job({"action": "clear", "region": self.region.currentData()},
                 lambda result: self.connection_status.setText("已删除当前区域保存的 Key"))
        self.update_onboarding()

    def refresh(self):
        if self.device_loading or self.session:
            return
        self.device_loading = True
        self.devices_ready = False
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText("正在刷新…")
        self.device_status.setText("正在读取设备并准备虚拟音频…")
        self.start_button.setEnabled(False)

        def loaded(result):
            options = {
                "headphones": [(item["description"], item["name"]) for item in result["sinks"] if item["name"] not in {"meeting-remote", "meeting-translator"}],
                "microphone": [(item["description"] + ("（静音）" if item["mute"] else ""), item["name"]) for item in result["sources"] if not item["name"].endswith(".monitor")],
                "subtitle_source": [(("系统声音 · " if item["name"].endswith(".monitor") else "麦克风 · ") + item["description"], item["name"])
                                    for item in result["sources"] if item["name"] != "meeting-translator.monitor"],
            }
            voices = [(voice, voice) for voice in result["voices"]] + [("跟随说话人（每次会话一次）", "clone_once"), ("跟随说话人（逐段更新）", "clone_always")]
            options.update(inbound_voice=voices, outbound_voice=voices)
            for name, entries in options.items():
                combo = self.fields[name]
                previous = combo.currentData() or self.settings.value(name)
                combo.blockSignals(True)
                combo.clear()
                for title, value in entries:
                    combo.addItem(title, value)
                if not previous:
                    if name == "subtitle_source":
                        previous = result.get("default_sink", "") + ".monitor"
                        if not any(value == previous for _, value in entries) and entries:
                            previous = entries[0][1]
                    else:
                        previous = "Tina" if "voice" in name else preferred_device(name, entries, result)
                index = combo.findData(previous)
                if index >= 0:
                    combo.setCurrentIndex(index)
                elif previous and "voice" not in name:
                    combo.insertItem(0, "已断开 · 请重新选择设备", None)
                    combo.setCurrentIndex(0)
                combo.blockSignals(False)
            self.devices_ready = True
            self.asr_ready = result.get("asr_ready", False)
            self.asr_status.setText("本地语音识别组件已就绪" if self.asr_ready else "尚未安装本地语音识别组件：运行 scripts/setup_local_asr.py 后刷新设备。")
            self.device_status.setText("已自动匹配可用耳机与麦克风。如果更换了设备，点击「刷新设备」即可。")
            self.route()
            if not self.session and self.error_card.isHidden():
                self.set_badge("准备就绪")
            self.update_onboarding()

        self.job({"action": "devices"}, loaded)

    def route(self):
        from subtitle_settings import subtitle_profile
        mode = self.fields["mode"].currentData()
        subtitles = mode == "subtitles"
        pacing = self.fields["subtitle_profile"].currentData()
        self.subtitle_profile_hint.setText(subtitle_profile({'subtitle_profile': pacing})['hint'])
        self.overlay.responsive = subtitles and pacing == 'fast'
        self.pipeline_status.setVisible(subtitles)
        self.subtitle_options.setVisible(subtitles)
        if subtitles:
            self.onboarding.hide()
        headphones = self.fields["headphones"].currentText() or "请选择输出设备"
        microphone = self.fields["microphone"].currentText() or "请选择麦克风"
        routes = {
            "inbound": "听到外语  →  播放中文语音  →  " + headphones,
            "outbound": "你说中文  →  生成英文语音  →  送入会议麦克风",
            "mic_test": "你说中文  →  播放英文语音  →  " + headphones,
            "duplex": "对方外语 → 耳机听中文  /  你说中文 → 会议听英文",
            "subtitles": "本地语音识别 → 实时双语字幕（静音，不播语音）",
        }
        hints = {
            "inbound": "将会议软件或浏览器的声音输出设为 meeting-remote，即可在耳机中收听中文翻译。",
            "outbound": "将会议软件的麦克风设为 Monitor of meeting-translator；此模式不向耳机播放译音。",
            "mic_test": "对着「" + microphone + "」说一句中文，即可在耳机里听到翻译后的英文。",
            "duplex": "会议扬声器选 meeting-remote，会议麦克风选 Monitor of meeting-translator，即可双向交流。",
            "subtitles": "适合看外语视频、直播或开会时静默看字幕，电脑原本的声音照常播放。",
        }
        self.route_label.setText(routes.get(mode, "请选择翻译模式"))
        self.route_hint.setText(hints.get(mode, ""))
        self.fields["headphones"].setToolTip(headphones)
        self.fields["microphone"].setToolTip(microphone)
        self.metric_labels[0].setText(headphones if mode != "outbound" else "会议虚拟麦克风")
        self.metric_labels[0].setToolTip(headphones if mode != "outbound" else "meeting-translator")
        valid = (mode == "outbound" or bool(self.fields["headphones"].currentData())) and (mode == "inbound" or bool(self.fields["microphone"].currentData()))
        self.metric_titles[0].setText("收音来源" if subtitles else "播放设备")
        self.metric_titles[2].setText("出字速度" if subtitles else "响应速度")
        self.token_usage.setVisible(subtitles and self.text_fields["text_provider"].currentData() != "none")
        if not self.session:
            self.metric_labels[2].setText("等待识别" if subtitles else "暂未测量")
        if subtitles:
            source = self.fields["subtitle_source"].currentText()
            if not self.session and self.devices_ready and self.error_card.isHidden():
                self.status.setText("实时字幕已就绪 · 点击「开始翻译」即可看字幕，无需填写语音密钥" if self.asr_ready else "请先安装本地语音识别组件，再刷新设备。")
            self.metric_labels[0].setText(source)
            self.metric_labels[0].setToolTip(source)
            self.fields["subtitle_source"].setToolTip(source)
            valid = bool(self.fields["subtitle_source"].currentData()) and self.asr_ready
            provider = self.text_fields["text_provider"].currentData()
            model_hint = {"codex": "识别出的文字由本机已登录的 Codex 翻译。", "antigravity": "识别出的文字由本机已登录的 Antigravity 翻译。", "api": "识别出的文字由自定义在线服务翻译。", "none": "仅显示原文字幕，不调用翻译服务。"}[provider]
            self.privacy_note.setText("声音仅在电脑本地识别为文字。" + model_hint)
            if provider == "none":
                self.metric_titles[2].setText("本地识别耗时")
                self.route_label.setText("本地识别 → 原文字幕（不播放译音）")
        else:
            self.privacy_note.setText("开始后将通过百炼实时语音服务翻译；停止或关闭软件即可结束。")
        self.start_button.setEnabled(self.devices_ready and valid and self.session is None)
        if hasattr(self, "setup_buttons"):
            self.update_onboarding()

    def persist(self):
        self.settings.setValue("region", self.region.currentData())
        self.settings.setValue("remember", self.remember.isChecked())
        self.settings.setValue("theme", self.theme)
        self.settings.setValue("vad_threshold", self.vad_threshold.value())
        for name, value in self.translation_config().items():
            if name != "text_api_key":
                self.settings.setValue(name, value)
        for name, control in self.fields.items():
            if control.currentData() is not None:
                self.settings.setValue(name, control.currentData())
        self.settings.sync()

    def start(self):
        if self.session is not None:
            return
        subtitles = self.fields["mode"].currentData() == "subtitles"
        if not subtitles and not self.key.text().strip():
            self.show_error("请输入 API Key")
            self.navigate(3)
            self.connection_status.setText("请填写当前区域的 API Key 后开始翻译。")
            self.key.setFocus()
            return
        if not self.devices_ready:
            self.show_error("音频设备未准备好，请刷新设备。")
            return
        text_config = {}
        if subtitles:
            if not self.asr_ready or not self.fields["subtitle_source"].currentData():
                self.show_error("本地 ASR 或字幕音频来源未准备好，请检查设备页面。")
                return
            try:
                text_config = self.validate_text_config()
            except ValueError as error:
                self.text_status.setText(str(error))
                self.navigate(3)
                return
        self.persist()
        if not subtitles and self.remember.isChecked():
            self.save_key()
        config = {name: control.currentData() for name, control in self.fields.items()}
        config.update(action="translate")
        if subtitles:
            config.update(text_config)
        else:
            config.update(api_key=self.key.text().strip(), region=self.region.currentData())
        process = QProcess(self)
        self.session = process
        self.session_buffer = b""
        self.session_error = False
        self.stop_requested = False
        self.caption_sequences.clear()
        self.token_usage.clear()
        self.pipeline_status.clear()
        self.session_started = time.monotonic()
        self.session_record = {"started": datetime.now().astimezone().isoformat(), "mode": config["mode"],
                               "device": self.fields["headphones"].currentText() if config["mode"] != "outbound" else "meeting-translator"}
        if subtitles:
            self.session_record["device"] = self.fields["subtitle_source"].currentText()
        self.error_card.hide()
        self.onboarding.hide()
        for panel in self.transcripts.values():
            panel.clear()
        self.overlay.clear()
        process.setProgram(str(APP_DIR / ".venv/bin/python"))
        process.setArguments([str(APP_DIR / "native_worker.py")])
        process.started.connect(lambda: process.write((json.dumps(config) + "\n").encode()))
        process.readyReadStandardOutput.connect(self.events)
        process.finished.connect(self.stopped)

        def failed(error):
            if self.stop_requested and error == QProcess.ProcessError.Crashed:
                return
            self.show_error("翻译进程错误：" + process.errorString(), affects_session=True)
            if error == QProcess.ProcessError.FailedToStart:
                self.stopped(1)

        process.errorOccurred.connect(failed)
        for control in self.config_widgets:
            control.setEnabled(False)
        for control in self.setup_buttons:
            control.setEnabled(False)
        self.start_button.hide()
        self.stop_button.show()
        self.stop_button.setEnabled(True)
        self.stop_button.setText("结束翻译")
        self.status.setText("正在启动本地 FunASR 字幕…" if subtitles else "正在连接百炼…")
        self.set_badge("正在连接")
        self.navigate(0)
        self.timer.start()
        self.update_duration()
        process.start()

    def events(self):
        if not self.session:
            return
        self.session_buffer += bytes(self.session.readAllStandardOutput())
        while b"\n" in self.session_buffer:
            line, self.session_buffer = self.session_buffer.split(b"\n", 1)
            try:
                event = json.loads(line)
            except ValueError:
                continue
            self.handle_event(event)

    def handle_event(self, event):
        if event.get("error"):
            self.show_error(event["error"], affects_session=True)
            self.stop()
            return
        direction = event.get("direction")
        if direction not in self.transcripts:
            return
        if event.get("warning"):
            self.status.setText(self.redact(event["warning"]))
            self.set_badge("原文继续 · 需留意")
            return
        if event.get("status") and not self.stop_requested and not self.session_error:
            translating = event["status"] == "translating"
            self.status.setText("正在翻译 · 原文与译文实时更新" if translating else "正在监听 · 等待说话")
            self.set_badge("正在翻译" if translating else "正在监听", "active")
        now = datetime.now().strftime("%H:%M:%S")
        seq = event.get("segment_id")
        subtitles = self.fields["mode"].currentData() == "subtitles"
        if subtitles and (event.get("source_final") or event.get("translation_final")):
            timings = [("segment_seconds", "音频片段"), ("asr_queue_seconds", "识别排队"),
                       ("asr_seconds", "ASR"), ("translation_queue_seconds", "翻译排队"),
                       ("translation_seconds", "翻译")]
            self.pipeline_status.setText(" · ".join(f"{title} {event[key]} s" for key, title in timings if key in event))
        if subtitles and event.get("source_final"):
            self.caption_sequences[direction] = seq
            if self.text_fields["text_provider"].currentData() == "none":
                self.overlay.original(direction, event["source_final"])
            else:
                self.overlay.pending(direction, event["source_final"], seq)
            self.metric_labels[2].setText(str(event.get("asr_seconds", "—")) + " s · 识别")
        if not subtitles and event.get("source_final"):
            self.overlay.pending(direction, event["source_final"])
        for key, prefix in (("source_final", "原文"), ("translation_final", "译文")):
            if event.get(key):
                self.transcripts[direction].append_entry(prefix, event[key], now, seq)
        if event.get("translation_final"):
            if subtitles:
                self.token_usage.setText(self.usage_text(event))
            self.overlay.caption(direction, event["translation_final"], seq if subtitles else None)
            if event.get("latency_seconds") is not None:
                self.metric_labels[2].setText(str(event["latency_seconds"]) + " s")
            if self.fields["mode"].currentData() == "mic_test":
                self.settings.setValue("onboarded", True)

    def stop(self):
        if self.session and not self.stop_requested:
            self.stop_requested = True
            from worker_shutdown import stop_worker
            process = self.session
            stop_worker(process, lambda: self.session is process)
            self.stop_button.setEnabled(False)
            self.stop_button.setText("正在结束…")
            if not self.session_error:
                self.status.setText("正在结束翻译与音频连接…")
                self.set_badge("正在结束")

    def stopped(self, exit_code=0, exit_status=None):
        process = self.session
        if process is None:
            return
        self.events()
        if not self.stop_requested and not self.session_error:
            self.show_error("翻译进程意外退出，请检查网络和设备后重试。", affects_session=True)
        self.session = None
        self.timer.stop()
        self.update_duration()
        self.save_session_record()
        process.deleteLater()
        for control in self.config_widgets:
            control.setEnabled(True)
        for control in self.setup_buttons:
            control.setEnabled(True)
        self.start_button.show()
        self.stop_button.hide()
        self.route()
        self.update_text_provider()
        if not self.session_error:
            self.status.setText("本次会话已结束 · 字幕保留在窗口中，未写入磁盘")
            self.set_badge("已结束")
        if self.closing:
            self.close()

    def update_duration(self):
        seconds = int(time.monotonic() - self.session_started) if self.session_started else 0
        self.metric_labels[1].setText(self.duration_text(seconds))

    @staticmethod
    def duration_text(seconds):
        hours, remaining = divmod(max(0, int(seconds)), 3600)
        minutes, seconds = divmod(remaining, 60)
        return f"{hours:02}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"

    def history_records(self):
        try:
            records = json.loads(self.settings.value("history", "[]"))
            return [record for record in records if isinstance(record, dict) and "started" in record] if isinstance(records, list) else []
        except (ValueError, TypeError):
            return []

    def save_session_record(self):
        if not self.session_record:
            return
        record = dict(self.session_record)
        record.update(duration=int(time.monotonic() - self.session_started), status="中断" if self.session_error else "完成")
        self.settings.setValue("history", json.dumps(([record] + self.history_records())[:100], ensure_ascii=False))
        self.settings.sync()
        self.session_record = None
        self.render_history()

    def render_history(self):
        search = self.history_search.text().strip().casefold()
        days = self.history_filter.currentData() or 0
        earliest = datetime.now().astimezone() - timedelta(days=days)
        rows = []
        names = {value: title for title, value in MODES}
        for record in self.history_records():
            try:
                start = datetime.fromisoformat(record["started"]).astimezone()
                if days and start < earliest:
                    continue
                values = [start.strftime("%Y-%m-%d  %H:%M"), names.get(record.get("mode"), "翻译"),
                          self.duration_text(record.get("duration", 0)), record.get("device", "—"), record.get("status", "中断")]
                if search and search not in " ".join(values).casefold():
                    continue
                rows.append(values)
            except (ValueError, TypeError):
                continue
        self.history_table.setRowCount(len(rows))
        for r, values in enumerate(rows):
            self.history_table.setRowHeight(r, 58)
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.history_table.setItem(r, c, item)
        self.history_table.setVisible(bool(rows))
        self.history_empty.setVisible(not rows)
        self.history_empty.setText("没有符合条件的会话记录" if search or days else "还没有会话记录\n结束一次翻译后，时间、时长和设备会显示在这里。")

    def toggle_captions(self):
        self.overlay.setVisible(not self.overlay.isVisible())
        if self.overlay.isVisible():
            self.overlay.raise_()

    def closeEvent(self, event):
        self.persist()
        if not self.music_page.shutdown():
            self.closing = True
            QTimer.singleShot(200, self.close)
            event.ignore()
            return
        if not self.pdf_page.shutdown():
            self.closing = True
            QTimer.singleShot(200, self.close)
            event.ignore()
            return
        if not self.nas_page.shutdown():
            self.closing = True
            QTimer.singleShot(200, self.close)
            event.ignore()
            return
        if not self.web_page.shutdown():
            self.closing = True
            QTimer.singleShot(200, self.close)
            event.ignore()
            return
        if not self.video_page.shutdown():
            self.closing = True
            QTimer.singleShot(200, self.close)
            event.ignore()
            return
        if self.session:
            self.closing = True
            self.stop()
            event.ignore()
            return
        if self.jobs:
            self.closing = True
            QTimer.singleShot(200, self.close)
            event.ignore()
            return
        self.overlay.close()
        event.accept()


def main():
    app = QApplication(sys.argv)
    lock = QLockFile(str(Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.RuntimeLocation)) / "qwen-live-translate.lock"))
    if not lock.tryLock(0):
        QMessageBox.information(None, "Qwen 双向同传", "应用已经运行，请切换到现有窗口。")
        return 0
    app.setDesktopFileName("qwen-live-translate")
    app.setStyle("Fusion")
    window = Translator()
    if "--web" in sys.argv:
        window.navigate(5)
    if "--nas" in sys.argv:
        window.navigate(6)
    if "--video" in sys.argv:
        index = sys.argv.index("--video")
        if len(sys.argv) > index + 1:
            window.video_page.source.setText(sys.argv[index + 1])
        window.navigate(4)
    if "--subtitles" in sys.argv:
        window.fields["mode"].setCurrentIndex(window.fields["mode"].findData("subtitles"))
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
