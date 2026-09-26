"""MouseLink desktop: one owner of the serial port and input capture."""
import argparse
import ctypes
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets as W
from serial.tools.list_ports import comports

from bridge import BridgeConfig, KVMController, SerialBridge
from absolute_diagnostic import POINTS
from app_version import APP_VERSION
from app_settings import bridge_mode, migrate_settings
from flash_dialog import FlashDialog

MODES = {
    "free": ("自由切换", "推荐", "从 iPad 最左边缘继续向左推动，即可返回电脑。"),
    "locked": ("锁定在 iPad", "专注操作", "碰到边缘仍留在 iPad；按 Ctrl + 左 Alt 返回电脑。"),
}
POINT_LABELS = ["屏幕中心", "左侧约 10%", "最左边缘", "右侧约 90%", "上方约 10%", "下方约 90%", "屏幕中心"]


def app_data():
    root = (Path(os.environ["MOUSELINK_DATA_DIR"]) if os.environ.get("MOUSELINK_DATA_DIR")
            else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "MouseLink")
    root.mkdir(parents=True, exist_ok=True)
    return root


def read_json(path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def make_icon():
    pix = QtGui.QPixmap(96, 96)
    pix.fill(QtCore.Qt.GlobalColor.transparent)
    p = QtGui.QPainter(pix)
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    p.setPen(QtCore.Qt.PenStyle.NoPen)
    p.setBrush(QtGui.QColor("#2563eb"))
    p.drawRoundedRect(3, 3, 90, 90, 24, 24)
    p.setPen(QtGui.QPen(QtGui.QColor("white"), 5, QtCore.Qt.PenStyle.SolidLine,
                       QtCore.Qt.PenCapStyle.RoundCap, QtCore.Qt.PenJoinStyle.RoundJoin))
    p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(21, 24, 23, 37, 4, 4)
    p.drawRoundedRect(56, 34, 18, 36, 4, 4)
    p.drawLine(36, 76, 61, 76)
    p.drawLine(55, 70, 61, 76)
    p.end()
    return QtGui.QIcon(pix)


class Worker(QtCore.QThread):
    calibration_done = QtCore.Signal(str, bool, str)
    start_handled = QtCore.Signal()

    def __init__(self, native_edges=True, trial_seconds=0):
        super().__init__()
        self.native_edges = native_edges
        self.trial_seconds = trial_seconds
        self.commands = queue.Queue()
        self.closing = False
        self.controller = None
        self.monitor = None
        self.running_bridge = False
        self.busy = False
        self.error = ""
        self.port = ""
        self.device_serial = ""
        self.phase = ""
        self.stop_requested = False
        self.shutdown = threading.Event()

    def request_shutdown(self):
        self.closing = True
        self.shutdown.set()
        self.stop_bridge()
        while True:
            try:
                self.commands.get_nowait()
            except queue.Empty:
                break

    def stop_bridge(self):
        self.stop_requested = True
        controller = self.controller
        if controller is not None:
            controller.stop()

    def snapshot(self):
        controller = self.controller
        bridge = controller._bridge if controller else self.monitor
        return {
            "usb": bool(bridge and bridge.is_connected),
            "ble": bool(bridge and bridge.ble_connected),
            "ready": bool(bridge and bridge.absolute_ready),
            "remote": bool(controller and controller._is_active),
        }

    def run(self):
        while not self.closing:
            try:
                self._idle()
            except Exception as exc:
                self.error = str(exc)
                self.stop_bridge()
                self.shutdown.wait(.3)
            finally:
                if self.monitor:
                    self.monitor.disconnect()
                self.monitor = None
        self.stop_bridge()

    def _idle(self):
        ports = [p for p in comports() if p.vid == 0x303A and p.pid == 0x1001]
        if len(ports) != 1:
            self.port = ""
            self.device_serial = ""
            self.error = "检测到多个开发板，请仅连接本次使用的一个。" if ports else ""
            self.shutdown.wait(.4)
            return
        self.port = ports[0].device
        self.device_serial = ports[0].serial_number or ""
        config = BridgeConfig(port=self.port, reconnect_max_attempts=1)
        self.monitor = SerialBridge(config)
        if not self.monitor.connect():
            self.error = "设备正被其他程序使用，请先关闭旧版键鼠桥。"
            self.shutdown.wait(.4)
            return
        self.error = ""
        while not self.closing and self.monitor.is_connected:
            try:
                action, value = self.commands.get(timeout=.15)
            except queue.Empty:
                continue
            self.error = ""
            if action == "start":
                if self.closing or self.stop_requested:
                    self.error = "已取消开启。"
                    self.start_handled.emit()
                    continue
                if not self.monitor.absolute_ready:
                    self.error = "请先连接 iPad，并确认使用新版定位固件。"
                    self.start_handled.emit()
                    continue
                self.monitor.disconnect()
                self.monitor = None
                mode, speed = value
                self.controller = KVMController(BridgeConfig(
                    port=self.port, mode=mode, sensitivity=speed,
                    absolute_enabled=True, native_edges_enabled=self.native_edges,
                    reconnect_max_attempts=1))
                if self.closing or self.stop_requested:
                    self.controller.stop()
                    self.controller = None
                    return
                self.running_bridge = True
                self.start_handled.emit()
                trial_timer = None
                if self.trial_seconds:
                    trial_timer = threading.Timer(self.trial_seconds, self.stop_bridge)
                    trial_timer.daemon = True
                    trial_timer.start()
                try:
                    self.controller.run()
                finally:
                    if trial_timer:
                        trial_timer.cancel()
                    self.controller.stop()
                    self.controller = None
                    self.running_bridge = False
                return
            if action == "calibrate":
                if self.closing:
                    return
                self.busy = True
                success, detail = False, ""
                try:
                    if not self.monitor.absolute_ready:
                        raise RuntimeError("iPad 定位通道尚未连接。")
                    for i, (_, x, y) in enumerate(POINTS):
                        if self.closing:
                            raise RuntimeError("检查已取消。")
                        self.phase = f"{i + 1} / 7 · {POINT_LABELS[i]}"
                        if not self.monitor.send_absolute_report(0, x, y, 0, i + 1):
                            raise RuntimeError("设备连接已断开。")
                        deadline = time.monotonic() + 2.5
                        while time.monotonic() < deadline and not self.closing:
                            if not self.monitor.absolute_ready:
                                raise RuntimeError("iPad 蓝牙连接已断开。")
                            self.shutdown.wait(.05)
                    success = not self.closing
                except Exception as exc:
                    detail = str(exc)
                finally:
                    self.monitor.send_mouse_report(0, 0, 0, 0)
                    self.busy = False
                    self.phase = ""
                    self.calibration_done.emit(value, success, detail)


class ModeCard(W.QFrame):
    def __init__(self, key, group):
        super().__init__()
        self.key = key
        self.setObjectName("modeCard")
        title, badge, description = MODES[key]
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        self.radio = W.QRadioButton(title)
        group.addButton(self.radio)
        layout.addWidget(self.radio)
        tag = W.QLabel(badge)
        tag.setObjectName("tag")
        layout.addWidget(tag)
        text = W.QLabel(description)
        text.setWordWrap(True)
        text.setObjectName("muted")
        layout.addWidget(text)
        layout.addStretch()
        self.hotkey_toggle = None
        if key == "free":
            row = W.QHBoxLayout()
            row.addStretch()
            self.hotkey_toggle = W.QPushButton("快捷键返回  ·  关闭")
            self.hotkey_toggle.setObjectName("hotkeyToggle")
            self.hotkey_toggle.setCheckable(True)
            self.hotkey_toggle.setAccessibleName("快捷键返回")
            self.hotkey_toggle.setToolTip("开启后，也可按 Ctrl + 左 Alt 返回电脑。")
            self.hotkey_toggle.toggled.connect(lambda checked: self.hotkey_toggle.setText(
                "快捷键返回  ·  开启" if checked else "快捷键返回  ·  关闭"))
            row.addWidget(self.hotkey_toggle)
            layout.addLayout(row)
        self.setMinimumHeight(175)
        self.radio.toggled.connect(self.update_style)

    def update_style(self, checked):
        self.setProperty("selected", checked)
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event):
        if self.isEnabled():
            self.radio.setChecked(True)
        super().mousePressEvent(event)


class Window(W.QMainWindow):
    def __init__(self, preview=False, native_edges=True, trial_seconds=0):
        super().__init__()
        self.preview = preview
        self.native_edges = native_edges
        self.trial_seconds = trial_seconds
        candidate = native_edges and trial_seconds > 0
        self.worker = None if preview else Worker(native_edges, trial_seconds)
        self.closing = False
        self.allow_close = False
        self.flash_open_pending = False
        self.flash_dialog = None
        self.save_error = ""
        self.start_pending = False
        self.config_path = app_data() / "settings.json"
        self.settings = migrate_settings({} if preview else read_json(self.config_path, {}))
        self.seed = read_json(Path(__file__).with_name("device-calibration.json"), {})
        self.setWindowTitle("MouseLink · 原生边缘候选版" if candidate else "MouseLink · 键鼠桥")
        self.setWindowIcon(make_icon())
        self.resize(1000, 800)
        self.setMinimumSize(820, 520)
        scroll = W.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(W.QFrame.Shape.NoFrame)
        content = W.QWidget()
        root = W.QVBoxLayout(content)
        root.setContentsMargins(32, 26, 32, 26)
        root.setSpacing(22)
        header = W.QHBoxLayout()
        logo = W.QLabel()
        logo.setPixmap(make_icon().pixmap(42, 42))
        header.addWidget(logo)
        title = W.QLabel("MouseLink")
        title.setObjectName("brand")
        header.addWidget(title)
        header.addStretch()
        version = W.QLabel("原生边缘 · 候选版" if candidate else f"键鼠桥  /  {APP_VERSION}")
        version.setObjectName("muted")
        header.addWidget(version)
        root.addLayout(header)
        if candidate:
            candidate_hint = W.QLabel(
                f"边缘手势待实机验收 · 每次开启最多 {trial_seconds:g} 秒，之后自动停止；可再次开启。")
            candidate_hint.setObjectName("muted")
            candidate_hint.setWordWrap(True)
            root.addWidget(candidate_hint)

        hero = W.QFrame()
        hero.setObjectName("hero")
        hero_layout = W.QVBoxLayout(hero)
        hero_layout.setContentsMargins(26, 24, 26, 24)
        route = W.QLabel("WINDOWS     ⇄     iPAD")
        route.setObjectName("route")
        hero_layout.addWidget(route)
        self.headline = W.QLabel("一套键鼠，自由往返。")
        self.headline.setObjectName("headline")
        hero_layout.addWidget(self.headline)
        self.subtitle = W.QLabel("从电脑右边缘进入 iPad，让操作自然延续。")
        self.subtitle.setWordWrap(True)
        hero_layout.addWidget(self.subtitle)
        root.addWidget(hero)

        section = W.QHBoxLayout()
        label = W.QLabel("选择切换方式")
        label.setObjectName("section")
        section.addWidget(label)
        section.addStretch()
        self.selection_hint = W.QLabel("停止后可更换模式")
        self.selection_hint.setObjectName("muted")
        section.addWidget(self.selection_hint)
        root.addLayout(section)
        cards = W.QHBoxLayout()
        cards.setSpacing(12)
        self.group = W.QButtonGroup(self)
        self.cards = {}
        for key in MODES:
            card = ModeCard(key, self.group)
            cards.addWidget(card, 1)
            self.cards[key] = card
        self.cards[self.settings["mode"]].radio.setChecked(True)
        self.hotkey_toggle = self.cards["free"].hotkey_toggle
        self.hotkey_toggle.setChecked(self.settings["hotkey_return_enabled"])
        root.addLayout(cards)

        controls = W.QFrame()
        controls.setObjectName("panel")
        grid = W.QGridLayout(controls)
        grid.setContentsMargins(22, 20, 22, 20)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(15)
        self.connection = W.QLabel("正在检测设备…")
        self.connection.setObjectName("section")
        grid.addWidget(self.connection, 0, 0, 1, 2)
        self.status_text = W.QLabel("通过 USB 连接开发板，再连接 iPad 蓝牙。")
        self.status_text.setWordWrap(True)
        self.status_text.setObjectName("muted")
        grid.addWidget(self.status_text, 1, 0, 1, 2)
        self.start_button = W.QPushButton("开启键鼠桥")
        self.start_button.setObjectName("primary")
        self.start_button.setMinimumHeight(48)
        self.start_button.setMinimumWidth(160)
        self.start_button.clicked.connect(self.toggle_bridge)
        grid.addWidget(self.start_button, 0, 2, 2, 1)
        speed_label = W.QLabel("移动速度")
        grid.addWidget(speed_label, 2, 0)
        self.speed = W.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.speed.setRange(25, 150)
        self.speed.setValue(int(self.settings.get("speed", 50)))
        grid.addWidget(self.speed, 2, 1)
        self.speed_value = W.QLabel()
        self.speed.valueChanged.connect(lambda v: self.speed_value.setText(f"{v / 50:.2g} 倍"))
        self.speed_value.setText(f"{self.speed.value() / 50:.2g} 倍")
        grid.addWidget(self.speed_value, 2, 2)
        grid.setColumnStretch(1, 1)
        root.addWidget(controls)

        footer = W.QHBoxLayout()
        instructions = W.QLabel("Ctrl + 左 Alt：按下组合键后全部松开；Scroll Lock：紧急返回。")
        instructions.setObjectName("muted")
        instructions.setWordWrap(True)
        footer.addWidget(instructions, 1)
        self.check_button = W.QPushButton("检查光标位置")
        self.check_button.clicked.connect(self.choose_calibration)
        footer.addWidget(self.check_button)
        root.addLayout(footer)
        maintenance = W.QHBoxLayout()
        maintenance_note = W.QLabel("新开发板可直接在软件内刷入固件。")
        maintenance_note.setObjectName("muted")
        maintenance_note.setWordWrap(True)
        maintenance.addWidget(maintenance_note, 1)
        self.flash_button = W.QPushButton("一键刷机")
        self.flash_button.clicked.connect(self.open_flash)
        maintenance.addWidget(self.flash_button)
        root.addLayout(maintenance)
        self.notice = W.QLabel("")
        self.notice.setObjectName("notice")
        self.notice.setWordWrap(True)
        root.addWidget(self.notice)
        root.addStretch()
        scroll.setWidget(content)
        self.setCentralWidget(scroll)
        self.setStyleSheet(STYLE)

        self.tray = W.QSystemTrayIcon(make_icon(), self)
        self.tray.setToolTip("MouseLink · 键鼠桥")
        menu = W.QMenu()
        menu.addAction("打开 MouseLink", self.show_normal)
        menu.addAction("停止键鼠桥", lambda: self.worker.stop_bridge() if self.worker else None)
        menu.addAction("退出", self.close)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.show_normal() if reason == W.QSystemTrayIcon.ActivationReason.DoubleClick else None)
        if not preview:
            self.tray.show()
            self.worker.calibration_done.connect(self.finish_calibration)
            self.worker.finished.connect(self.worker_finished)
            self.worker.start_handled.connect(self.clear_start_pending)
            self.worker.start()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(150)
        self.refresh()

    def show_normal(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def selected_mode(self):
        return next(k for k, card in self.cards.items() if card.radio.isChecked())

    def effective_mode(self):
        return bridge_mode(self.selected_mode(), self.hotkey_toggle.isChecked())

    def clear_start_pending(self):
        self.start_pending = False

    def calibrated(self):
        if self.preview:
            return True
        serial = self.worker.device_serial
        profile = self.settings.get("calibration", {})
        if profile.get("device_serial") != serial:
            profile = self.seed if self.seed.get("device_serial") == serial else {}
        return bool(serial and profile.get("landscape") and profile.get("portrait"))

    def save(self):
        if self.preview:
            return
        self.settings.update(mode=self.selected_mode(), speed=self.speed.value(),
                             hotkey_return_enabled=self.hotkey_toggle.isChecked())
        try:
            temp = self.config_path.with_suffix(".tmp")
            temp.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.config_path)
            self.save_error = ""
        except OSError as exc:
            self.save_error = "设置暂时无法保存，本次仍可正常使用。"
            print(f"[SETTINGS] {exc}")

    def toggle_bridge(self):
        if self.preview or self.closing or self.flash_open_pending or self.flash_dialog:
            return
        if self.worker.running_bridge or self.start_pending:
            self.worker.stop_bridge()
            return
        self.save()
        self.start_pending = True
        self.worker.stop_requested = False
        self.worker.commands.put(("start", (self.effective_mode(), self.speed.value() / 100)))
        self.refresh()

    def choose_calibration(self):
        box = W.QMessageBox(self)
        box.setWindowTitle("检查光标位置")
        box.setText("保持 iPad 解锁，横屏与竖屏各检查一次。")
        box.setInformativeText("请先把 iPad 转到要检查的方向并解锁。光标将经过七个位置，全程不会点击。")
        landscape = box.addButton("检查横屏", W.QMessageBox.ButtonRole.ActionRole)
        portrait = box.addButton("检查竖屏", W.QMessageBox.ButtonRole.ActionRole)
        box.addButton("取消", W.QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if self.worker and box.clickedButton() in (landscape, portrait):
            orientation = "landscape" if box.clickedButton() == landscape else "portrait"
            self.worker.busy = True
            self.worker.commands.put(("calibrate", orientation))
            self.refresh()

    def finish_calibration(self, orientation, success, detail):
        if self.closing:
            if self.worker:
                self.worker.stop_bridge()
            return
        if not success:
            W.QMessageBox.warning(self, "检查未完成", detail or "连接已中断，请重新检查。")
            return
        answer = W.QMessageBox.question(self, "确认光标位置",
            "七个位置是否全部正确，第三步真正贴住最左边缘？\n\n中心 → 左侧 → 最左边缘 → 右侧 → 上方 → 下方 → 中心",
            W.QMessageBox.StandardButton.Yes | W.QMessageBox.StandardButton.No)
        profile = self.settings.get("calibration", {}).copy()
        if profile.get("device_serial") != self.worker.device_serial:
            profile = {"device_serial": self.worker.device_serial}
        profile[orientation] = answer == W.QMessageBox.StandardButton.Yes
        self.settings["calibration"] = profile
        self.save()

    def refresh(self):
        if self.closing:
            self.finish_shutdown()
            return
        if self.flash_open_pending or self.flash_dialog:
            return
        state = {"usb": True, "ble": True, "ready": True, "remote": False} if self.preview else self.worker.snapshot()
        running = False if self.preview else self.worker.running_bridge
        busy = False if self.preview else self.worker.busy
        error = "" if self.preview else self.worker.error
        if running or error:
            self.start_pending = False
        if state["ready"]:
            self.connection.setText("●  开发板与 iPad 已连接")
        elif state["ble"]:
            self.connection.setText("●  已连接 · 定位通道待就绪")
        elif state["usb"]:
            self.connection.setText("●  开发板已连接 · 等待 iPad")
        else:
            self.connection.setText("○  等待连接开发板")
        if busy:
            self.headline.setText("正在检查光标位置")
            self.subtitle.setText(self.worker.phase or "请观察 iPad 屏幕…")
        elif running:
            self.headline.setText("正在控制 iPad" if state["remote"] else "已开启，随时出发。")
            self.subtitle.setText(MODES[self.selected_mode()][2] if state["remote"] else "把鼠标推到电脑最右边缘，稍停即可进入 iPad。")
        else:
            self.headline.setText("一套键鼠，自由往返。")
            self.subtitle.setText("从电脑右边缘进入 iPad，让操作自然延续。")
        self.start_button.setText("停止键鼠桥" if running else "正在开启…" if self.start_pending else "开启键鼠桥")
        self.start_button.setEnabled(not busy and not self.start_pending and (running or (state["ready"] and self.calibrated())))
        self.check_button.setEnabled(state["ready"] and not running and not busy and not self.start_pending)
        self.flash_button.setEnabled(not busy and not self.start_pending)
        for card in self.cards.values():
            card.setEnabled(not running and not busy and not self.start_pending)
        self.speed.setEnabled(not running and not busy and not self.start_pending)
        self.hotkey_toggle.setEnabled(self.selected_mode() == "free" and not running and not busy and not self.start_pending)
        if error:
            detail = error
        elif not state["usb"]:
            detail = "请用支持数据传输的 USB 线连接开发板。"
        elif not state["ble"]:
            detail = "请在 iPad 蓝牙设置中连接 MouseLink-iPad。"
        elif not state["ready"]:
            detail = "需要新版定位固件。刚升级后，可忽略旧设备并重新配对。"
        elif not self.calibrated():
            detail = "首次使用请完成横屏、竖屏的光标位置检查。"
        else:
            detail = "横竖屏定位已确认 · 切换方向后无需更换模式。"
        self.status_text.setText(detail)
        self.notice.setText(self.save_error or ("锁定模式：边缘不返回；Ctrl + 左 Alt 返回电脑。" if self.selected_mode() == "locked"
            else "自由返回：先到达 iPad 最左边缘，再继续向左推动；按住鼠标拖动时留在 iPad。"))

    def open_flash(self):
        if self.flash_dialog or self.flash_open_pending or self.closing:
            return
        self.save()
        self.flash_open_pending = True
        self.centralWidget().setEnabled(False)
        self.start_pending = False
        self.start_button.setEnabled(False)
        self.flash_button.setEnabled(False)
        self.check_button.setEnabled(False)
        self.status_text.setText("正在停止桥接并释放设备…")
        if self.worker and self.worker.isRunning():
            self.worker.request_shutdown()
        else:
            self.worker_finished()

    def worker_finished(self):
        if self.closing:
            self.finish_shutdown()
        elif self.flash_open_pending:
            self.flash_open_pending = False
            self.flash_dialog = FlashDialog(app_data(), self)
            self.flash_dialog.finished.connect(self.flash_closed)
            self.flash_dialog.show()

    def flash_closed(self):
        dialog = self.flash_dialog
        self.flash_dialog = None
        if dialog:
            dialog.deleteLater()
        if self.closing:
            self.finish_shutdown()
        elif not self.preview:
            self.centralWidget().setEnabled(True)
            self.worker.deleteLater()
            self.worker = Worker(self.native_edges, self.trial_seconds)
            self.worker.calibration_done.connect(self.finish_calibration)
            self.worker.finished.connect(self.worker_finished)
            self.worker.start_handled.connect(self.clear_start_pending)
            self.worker.start()
            self.refresh()
        else:
            self.centralWidget().setEnabled(True)

    def finish_shutdown(self):
        if not self.closing or self.allow_close:
            return
        if self.worker and self.worker.isRunning():
            return
        if self.flash_dialog:
            if self.flash_dialog.is_busy():
                return
            self.flash_dialog.close()
            return
        self.allow_close = True
        self.timer.stop()
        self.tray.hide()
        self.close()

    def closeEvent(self, event):
        if self.preview or self.allow_close:
            event.accept()
            return
        event.ignore()
        if self.closing:
            return
        self.closing = True
        self.flash_open_pending = False
        self.start_pending = False
        self.save()
        if self.worker:
            self.worker.request_shutdown()
        self.start_button.setEnabled(False)
        self.start_button.setText("正在停止…")
        if self.flash_dialog:
            self.flash_dialog.close()
        self.finish_shutdown()


STYLE = """
QMainWindow, QScrollArea, QWidget { background: #f5f7fb; color: #18243a; font-family: 'Microsoft YaHei UI'; font-size: 13px; }
QLabel { background: transparent; }
QLabel#brand { font-size: 25px; font-weight: 700; }
QLabel#muted { color: #66758b; font-size: 12px; }
QFrame#hero { background: #172a48; border-radius: 20px; }
QFrame#hero QLabel { color: #cfddf1; }
QFrame#hero QLabel#headline { font-size: 28px; font-weight: 700; color: white; padding: 9px 0; }
QFrame#hero QLabel#route { color: #91b7fc; font-size: 11px; font-weight: 600; }
QLabel#section { font-size: 15px; font-weight: 600; }
QFrame#modeCard { background: white; border: 1px solid #dce3ef; border-radius: 14px; }
QFrame#modeCard[selected=true] { background: #eff5ff; border: 2px solid #3875e8; }
QFrame#modeCard QLabel, QFrame#modeCard QRadioButton { background: transparent; border: none; }
QRadioButton { font-size: 14px; font-weight: 600; spacing: 8px; }
QRadioButton::indicator { width: 15px; height: 15px; border: 1px solid #a5b3c8; border-radius: 8px; background: white; }
QRadioButton::indicator:checked { background: #3274ed; border: 3px solid #c4d8fc; }
QLabel#tag { color: #3466b9; font-size: 11px; padding: 4px 0; }
QFrame#panel { background: white; border: 1px solid #e1e7f0; border-radius: 16px; }
QFrame#panel QLabel, QFrame#panel QSlider { background: transparent; }
QPushButton { background: white; border: 1px solid #d4ddeb; border-radius: 9px; padding: 10px 16px; color: #28446d; }
QPushButton:hover { background: #eaf1ff; border-color: #86a9eb; }
QPushButton#primary { background: #2869e8; color: white; border: none; font-weight: 600; }
QPushButton#primary:hover { background: #1656cd; }
QPushButton#hotkeyToggle { padding: 7px 12px; font-size: 12px; border-radius: 13px; }
QPushButton#hotkeyToggle:checked { background: #2869e8; border-color: #2869e8; color: white; }
QComboBox { background: white; border: 1px solid #d4ddeb; padding: 10px; border-radius: 8px; }
QProgressBar { border: none; background: #e0e8f5; border-radius: 5px; min-height: 15px; text-align: center; }
QProgressBar::chunk { background: #3778ec; border-radius: 5px; }
QPlainTextEdit { background: white; border: 1px solid #dce3ef; border-radius: 6px; font-size: 11px; }
QPushButton:disabled, QPushButton#primary:disabled { background: #e6ebf3; color: #8492a8; border: none; }
QSlider::groove:horizontal { height: 5px; background: #dce5f3; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #3778ec; border-radius: 2px; }
QSlider::handle:horizontal { background: #2869e8; border: 3px solid white; width: 15px; margin: -7px 0; border-radius: 10px; }
QLabel#notice { color: #718097; font-size: 11px; }
QScrollBar:vertical { background: #f5f7fb; width: 8px; }
QScrollBar::handle:vertical { background: #ccd6e5; border-radius: 4px; min-height: 25px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", type=Path)
    parser.add_argument("--width", type=int, default=1000)
    parser.add_argument("--height", type=int, default=800)
    parser.add_argument("--native-edges", action="store_true",
                        help="Run the isolated, time-limited native-edge candidate")
    parser.add_argument("--trial-seconds", type=float, default=180,
                        help="Candidate capture limit per start, 15..600 seconds")
    parser.add_argument("--data-dir", type=Path, help="Separate candidate settings and logs")
    args = parser.parse_args()
    if not 15 <= args.trial_seconds <= 600:
        parser.error("--trial-seconds must be between 15 and 600")
    if args.data_dir:
        os.environ["MOUSELINK_DATA_DIR"] = str(args.data_dir.resolve())
    elif args.native_edges:
        original_data = app_data()
        candidate_data = original_data / "NativeEdgesCandidate"
        candidate_data.mkdir(parents=True, exist_ok=True)
        candidate_settings = candidate_data / "settings.json"
        if not args.preview and not candidate_settings.exists():
            candidate_settings.write_text(json.dumps(read_json(original_data / "settings.json", {}),
                                                       ensure_ascii=False, indent=2), encoding="utf-8")
        os.environ["MOUSELINK_DATA_DIR"] = str(candidate_data)
    app = W.QApplication(sys.argv[:1])
    app.setFont(QtGui.QFont("Microsoft YaHei UI", 10))
    app.setApplicationName("MouseLink")
    app.setOrganizationName("MouseLink")
    app.setApplicationVersion(APP_VERSION)
    lock = None
    mutex = None
    kernel = None
    if not args.preview:
        lock = QtCore.QLockFile(str(app_data() / "instance.lock"))
        if not lock.tryLock(0):
            W.QMessageBox.information(None, "MouseLink", "MouseLink 已在运行，请从任务栏或通知区域打开。")
            return 0
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        mutex = kernel.CreateMutexW(None, False, "Local\\MouseLink.KVM.Desktop")
        log_path = app_data() / "bridge.log"
        if log_path.exists() and log_path.stat().st_size > 2_000_000:
            log_path.replace(log_path.with_name("bridge.previous.log"))
        sys.stdout = open(log_path, "a", encoding="utf-8", buffering=1)
        sys.stderr = sys.stdout
    window = Window(preview=bool(args.preview), native_edges=True,
                    trial_seconds=args.trial_seconds if args.native_edges else 0)
    window.resize(args.width, args.height)
    if not args.preview:
        available = app.primaryScreen().availableGeometry()
        window.resize(min(args.width, available.width()), min(args.height, available.height()))
    window.show()
    if args.preview:
        def capture():
            args.preview.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(args.preview))
            app.quit()
        QtCore.QTimer.singleShot(350, capture)
    result = app.exec()
    if lock:
        lock.unlock()
        if mutex:
            kernel.CloseHandle(mutex)
        sys.stdout.close()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
