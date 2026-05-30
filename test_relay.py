from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Optional
from pathlib import Path
from datetime import datetime

import serial
from serial.tools import list_ports
from PySide6.QtCore import QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSpinBox,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from relay_protocol import (
    ProtocolError,
    RelayStatus,
    build_read_status_frame,
    build_write_single_frame,
    format_hex,
    parse_status_reply,
    parse_write_single_reply,
)

POLL_INTERVAL_MS = 200
SERIAL_TIMEOUT_SECONDS = 0.5
WRITE_CONFIRM_ATTEMPTS = 3

@dataclass(frozen=True)
class Command:
    kind: str
    relay: Optional[int] = None
    state: Optional[bool] = None

class SerialThread(QThread):
    connected = Signal(str)
    disconnected = Signal()
    error = Signal(str)
    log = Signal(str)
    relay_command_confirmed = Signal(int, bool)
    status_changed = Signal(object)

    def __init__(self, port_name: str, baudrate: int, address: int) -> None:
        super().__init__()
        self._commands: Queue[Command] = Queue()
        self._running = True
        self._port_name = port_name
        self._baudrate = baudrate
        self._address = address
        self._serial: Optional[serial.Serial] = None

    def run(self) -> None:
        try:
            self._serial = serial.Serial(
                port=self._port_name,
                baudrate=self._baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=SERIAL_TIMEOUT_SECONDS,
                write_timeout=SERIAL_TIMEOUT_SECONDS,
            )
        except serial.SerialException as exc:
            self._running = False
            self.error.emit(f"打开串口失败：{exc}")
            self.disconnected.emit()
            return

        self.connected.emit(f"{self._port_name} @ {self._baudrate}")
        self._loop()

    def stop(self) -> None:
        self._running = False
        self._commands.put(Command("stop"))

    def request_status(self) -> None:
        self._commands.put(Command("read"))

    def set_relay(self, relay: int, state: bool) -> None:
        self._commands.put(Command("write", relay=relay, state=state))

    def _loop(self) -> None:
        while self._running:
            try:
                command = self._commands.get(timeout=0.05)
            except Empty:
                continue

            if command.kind == "stop":
                break
            if command.kind == "read":
                self._read_status()
            elif command.kind == "write" and command.relay is not None:
                self._write_relay(command.relay, bool(command.state))

        self._close_serial()
        self.disconnected.emit()

    def _close_serial(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            except serial.SerialException as exc:
                self.error.emit(f"关闭串口失败：{exc}")
            finally:
                self._serial = None

    def _write_and_read(self, request: bytes, response_size: int) -> bytes:
        if self._serial is None or not self._serial.is_open:
            raise serial.SerialException("串口未打开")

        self._serial.reset_input_buffer()
        self._serial.write(request)
        self._serial.flush()
        self.log.emit(f"TX  {format_hex(request)}")

        response = self._serial.read(response_size)
        if response:
            self.log.emit(f"RX  {format_hex(response)}")
        if len(response) != response_size:
            raise TimeoutError(f"响应超时：期望 {response_size} 字节，收到 {len(response)} 字节")
        return response

    def _read_status(self, emit_errors: bool = True) -> Optional[RelayStatus]:
        try:
            request = build_read_status_frame(self._address)
            response = self._write_and_read(request, 15)
            status = parse_status_reply(response, self._address)
        except (ProtocolError, TimeoutError, serial.SerialException) as exc:
            if emit_errors:
                self.error.emit(f"读取状态失败：{exc}")
            return None

        self.status_changed.emit(status)
        return status

    def _write_relay(self, relay: int, state: bool) -> None:
        action = "打开" if state else "关闭"
        last_error = ""
        for attempt in range(1, WRITE_CONFIRM_ATTEMPTS + 1):
            try:
                request = build_write_single_frame(self._address, relay, state)
                response = self._write_and_read(request, 10)
                parse_write_single_reply(response, self._address, relay, state)
                self.relay_command_confirmed.emit(relay, state)
                self._read_status(emit_errors=False)
                return
            except (ProtocolError, TimeoutError, serial.SerialException, ValueError) as exc:
                last_error = str(exc)

            if attempt < WRITE_CONFIRM_ATTEMPTS:
                self.log.emit(
                    f"INFO 第 {relay} 路{action}未确认，正在重试 "
                    f"({attempt + 1}/{WRITE_CONFIRM_ATTEMPTS})"
                )

        self.error.emit(
            f"控制第 {relay} 路失败：已重试 {WRITE_CONFIRM_ATTEMPTS} 次，"
            f"未收到正确应答指令。{last_error}"
        )

class RelayIndicator(QLabel):
    def __init__(self, relay_name: str) -> None:
        super().__init__("未知")
        self._relay_name = relay_name
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumHeight(36)
        self.setAutoFillBackground(True)
        self.set_status(None)

    def set_status(self, is_on: Optional[bool]) -> None:
        if is_on is None:
            text = f"{self._relay_name}：未知"
            color = QColor("#9ca3af")
        elif is_on:
            text = f"{self._relay_name}：已打开"
            color = QColor("#16a34a")
        else:
            text = f"{self._relay_name}：已关闭"
            color = QColor("#6b7280")

        palette = self.palette()
        palette.setColor(QPalette.Window, color)
        palette.setColor(QPalette.WindowText, QColor("white"))
        self.setPalette(palette)
        self.setText(text)

class MainWindow(QMainWindow):
    MAX_LOG_MESSAGES = 20  # 最多显示20条消息
    
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("2 路串口继电器控制器")
        self.resize(780, 560)

        self._serial_thread: Optional[SerialThread] = None
        self._connected = False
        
        # 初始化日志相关
        self._log_messages: list[str] = []  # 存储日志消息
        self._log_file_path = self._init_log_file()  # 初始化日志文件路径

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self.request_status)

        # 自动循环相关
        self._cycle_timer = QTimer(self)
        self._cycle_timer.setSingleShot(True)
        self._cycle_timer.timeout.connect(self._on_cycle_timeout)
        self._auto_cycle_enabled = False
        self._cycle_state = "wait_open"
        # 倒计时显示用定时器（每秒更新一次）
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._on_countdown_tick)
        self._countdown_remaining = 0

        self._build_ui()
        self.refresh_ports()
        self._set_connected(False)

    def _build_ui(self) -> None:
        root = QWidget(self)
        layout = QVBoxLayout(root)

        connection_group = QGroupBox("串口连接")
        connection_layout = QGridLayout(connection_group)

        self.port_combo = QComboBox()
        self.refresh_button = QPushButton("刷新串口")
        self.refresh_button.clicked.connect(self.refresh_ports)

        self.baud_combo = QComboBox()
        self.baud_combo.addItems(["9600", "115200", "19200", "38400", "57600"])
        self.baud_combo.setCurrentText("9600")

        self.address_spin = QSpinBox()
        self.address_spin.setRange(0, 255)
        self.address_spin.setValue(1)

        self.connect_button = QPushButton("打开串口")
        self.connect_button.clicked.connect(self.toggle_connection)

        self.auto_poll_check = QCheckBox("自动轮询")
        self.auto_poll_check.setChecked(True)
        self.auto_poll_check.toggled.connect(self._sync_poll_timer)

        self.manual_refresh_button = QPushButton("手动刷新")
        self.manual_refresh_button.clicked.connect(self.request_status)

        connection_layout.addWidget(QLabel("串口"), 0, 0)
        connection_layout.addWidget(self.port_combo, 0, 1)
        connection_layout.addWidget(self.refresh_button, 0, 2)
        connection_layout.addWidget(QLabel("波特率"), 1, 0)
        connection_layout.addWidget(self.baud_combo, 1, 1)
        connection_layout.addWidget(QLabel("地址"), 1, 2)
        connection_layout.addWidget(self.address_spin, 1, 3)
        connection_layout.addWidget(self.connect_button, 0, 3)
        connection_layout.addWidget(self.auto_poll_check, 2, 0)
        connection_layout.addWidget(self.manual_refresh_button, 2, 1)

        relay_group = QGroupBox("继电器")
        relay_layout = QGridLayout(relay_group)

        self.relay1_indicator = RelayIndicator("第 1 路")
        self.relay2_indicator = RelayIndicator("第 2 路")
        self.relay_buttons: list[QPushButton] = []

        relay_layout.addWidget(self.relay1_indicator, 0, 0, 1, 2)
        relay_layout.addWidget(self._relay_button("第 1 路打开", 1, True), 1, 0)
        relay_layout.addWidget(self._relay_button("第 1 路关闭", 1, False), 1, 1)
        relay_layout.addWidget(self.relay2_indicator, 0, 2, 1, 2)
        relay_layout.addWidget(self._relay_button("第 2 路打开", 2, True), 1, 2)
        relay_layout.addWidget(self._relay_button("第 2 路关闭", 2, False), 1, 3)

        # 自动循环控制界面（针对第1路）
        cycle_group = QGroupBox("第 1 路自动循环")
        cycle_layout = QGridLayout(cycle_group)

        self.t1_spin = QSpinBox()
        self.t1_spin.setRange(1, 24 * 3600)
        self.t1_spin.setValue(10)
        self.t1_spin.setSuffix(" s")

        self.t2_spin = QSpinBox()
        self.t2_spin.setRange(1, 24 * 3600)
        self.t2_spin.setValue(2)
        self.t2_spin.setSuffix(" s")

        self.cycle_button = QPushButton("开始自动循环")
        self.cycle_button.setCheckable(True)
        self.cycle_button.clicked.connect(lambda checked: self._toggle_auto_cycle(checked))

        cycle_layout.addWidget(QLabel("间隔 T1 (打开前等待):"), 0, 0)
        cycle_layout.addWidget(self.t1_spin, 0, 1)
        cycle_layout.addWidget(QLabel("开 -> 关 间隔 T2:"), 1, 0)
        cycle_layout.addWidget(self.t2_spin, 1, 1)
        cycle_layout.addWidget(self.cycle_button, 0, 2, 2, 1)

        layout.addWidget(cycle_group)

        log_group = QGroupBox("通信日志")
        log_layout = QVBoxLayout(log_group)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.clear_log_button = QPushButton("清空日志")
        self.clear_log_button.clicked.connect(self._clear_log)
        log_layout.addWidget(self.log_view)
        log_layout.addWidget(self.clear_log_button, alignment=Qt.AlignRight)

        layout.addWidget(connection_group)
        layout.addWidget(relay_group)
        layout.addWidget(log_group, stretch=1)

        self.setCentralWidget(root)
        self.setStatusBar(QStatusBar(self))
        self.statusBar().showMessage("未连接")

    def _relay_button(self, text: str, relay: int, state: bool) -> QPushButton:
        button = QPushButton(text)
        button.clicked.connect(lambda _checked=False, r=relay, s=state: self.set_relay(r, s))
        self.relay_buttons.append(button)
        return button

    @Slot()
    def request_status(self) -> None:
        if self._serial_thread is not None:
            self._serial_thread.request_status()

    def _init_log_file(self) -> Path:
        """初始化日志文件路径，创建当前文件夹下的日志目录"""
        log_dir = Path(__file__).resolve().parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        
        # 使用当前日期作为日志文件名
        log_filename = f"relay_log_{datetime.now().strftime('%Y-%m-%d')}.txt"
        log_file = log_dir / log_filename
        return log_file
    
    def _get_rotated_log_file(self) -> Path:
        """返回当前应写入的日志文件路径；若超过 10MB 则生成下一个序号文件。"""
        log_dir = Path(__file__).resolve().parent / "logs"
        datepart = datetime.now().strftime('%Y-%m-%d')
        base_name = f"relay_log_{datepart}"
        log_dir.mkdir(parents=True, exist_ok=True)

        candidates = sorted(log_dir.glob(f"{base_name}*.txt"))
        if not candidates:
            return log_dir / f"{base_name}.txt"

        last = candidates[-1]
        try:
            if last.stat().st_size < 10 * 1024 * 1024:
                return last
        except OSError:
            return log_dir / f"{base_name}.txt"

        # 若已超出大小，生成下一个序号文件名
        stem = last.stem
        if stem == base_name:
            idx = 1
        else:
            parts = stem.rsplit('_', 1)
            if len(parts) == 2 and parts[0] == base_name and parts[1].isdigit():
                idx = int(parts[1]) + 1
            else:
                idx = 1
        return log_dir / f"{base_name}_{idx}.txt"

    def _save_log_to_file(self, message: str) -> None:
        """将日志消息保存到本地文件，超过 10MB 时轮转到新文件"""
        try:
            # 根据当前日志文件大小或已有轮转文件确定要写入的文件
            self._log_file_path = self._get_rotated_log_file()
            with open(self._log_file_path, 'a', encoding='utf-8') as f:
                f.write(message + '\n')
        except Exception as e:
            print(f"保存日志文件失败: {e}")
    
    def _refresh_log_display(self) -> None:
        """刷新日志显示，只显示最近20条消息"""
        self.log_view.clear()
        # 只显示最后20条消息
        for msg in self._log_messages[-self.MAX_LOG_MESSAGES:]:
            self.log_view.appendPlainText(msg)
    
    @Slot()
    def _clear_log(self) -> None:
        """清空日志内存和显示"""
        self._log_messages.clear()
        self.log_view.clear()

    @Slot(int, bool)
    def set_relay(self, relay: int, state: bool) -> None:
        if self._serial_thread is not None:
            self._serial_thread.set_relay(relay, state)

    @Slot()
    def refresh_ports(self) -> None:
        current = self.port_combo.currentData() or self.port_combo.currentText()
        self.port_combo.clear()

        ports = list(list_ports.comports())
        for port in ports:
            label = f"{port.device}  {port.description}"
            self.port_combo.addItem(label, port.device)

        if current:
            index = self.port_combo.findData(current)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)

        if not ports:
            self.port_combo.addItem("未发现串口", "")

    @Slot()
    def toggle_connection(self) -> None:
        if self._connected:
            self._disconnect_serial()
        else:
            self._connect_serial()

    def _connect_serial(self) -> None:
        port_name = self.port_combo.currentData()
        if not port_name:
            QMessageBox.warning(self, "无法打开串口", "请先选择可用串口。")
            return

        self._serial_thread = SerialThread(
            port_name,
            int(self.baud_combo.currentText()),
            self.address_spin.value(),
        )
        self._serial_thread.connected.connect(self._on_connected)
        self._serial_thread.disconnected.connect(self._on_disconnected)
        self._serial_thread.error.connect(self._on_error)
        self._serial_thread.log.connect(self._append_log)
        self._serial_thread.relay_command_confirmed.connect(self._on_relay_command_confirmed)
        self._serial_thread.status_changed.connect(self._on_status_changed)
        self._serial_thread.start()
        self.connect_button.setEnabled(False)
        self.statusBar().showMessage("正在打开串口...")

    def _disconnect_serial(self) -> None:
        self._poll_timer.stop()
        if self._serial_thread is not None:
            self._serial_thread.stop()
        self.statusBar().showMessage("正在关闭串口...")

    @Slot(str)
    def _on_connected(self, message: str) -> None:
        self._set_connected(True)
        self._append_log(f"INFO 已连接 {message}")
        self.statusBar().showMessage(f"已连接 {message}")
        self._sync_poll_timer()
        self.request_status()

    @Slot()
    def _on_disconnected(self) -> None:
        # 断开连接时停止自动循环（如在运行中）
        try:
            self._stop_auto_cycle()
        except Exception:
            pass

        self._poll_timer.stop()
        self._set_connected(False)
        self._append_log("INFO 已断开")
        self.statusBar().showMessage("未连接")

        if self._serial_thread is not None:
            self._serial_thread.wait(1000)
            self._serial_thread = None

    @Slot(str)
    def _on_error(self, message: str) -> None:
        self._append_log(f"ERR  {message}")
        self.statusBar().showMessage(message)
        if "打开串口失败" in message:
            self._set_connected(False)

    @Slot(int, bool)
    def _on_relay_command_confirmed(self, relay: int, state: bool) -> None:
        action = "打开" if state else "关闭"
        message = f"第 {relay} 路{action}成功，应答已确认"
        self._append_log(f"OK   {message}")
        self.statusBar().showMessage(message)

    @Slot(object)
    def _on_status_changed(self, status: RelayStatus) -> None:
        self.relay1_indicator.set_status(status.relay1)
        self.relay2_indicator.set_status(status.relay2)
        self.statusBar().showMessage(
            f"状态已更新：第 1 路 {'开' if status.relay1 else '关'}，"
            f"第 2 路 {'开' if status.relay2 else '关'}"
        )

    @Slot(str)
    def _append_log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        formatted_message = f"[{timestamp}] {message}"
        
        # 添加到日志消息列表
        self._log_messages.append(formatted_message)
        
        # 刷新日志显示（只显示最近20条）
        self._refresh_log_display()
        
        # 保存到本地文件
        self._save_log_to_file(formatted_message)

    def _on_countdown_tick(self) -> None:
        """每秒更新倒计时并在按钮上显示剩余秒数"""
        if not self._auto_cycle_enabled:
            self._countdown_timer.stop()
            return

        if self._countdown_remaining > 0:
            self._countdown_remaining -= 1
            try:
                self.cycle_button.setText(f"停止自动循环 ({self._countdown_remaining}s)")
            except Exception:
                pass
        else:
            # 当倒计时到 0 时，下一次动作由 _cycle_timer 触发；停止单独的倒计时直到重新设置
            self._countdown_timer.stop()

    def _toggle_auto_cycle(self, enable: bool) -> None:
        if enable:
            self._start_auto_cycle()
        else:
            self._stop_auto_cycle()

    def _start_auto_cycle(self) -> None:
        if self._auto_cycle_enabled:
            return
        # 仅在已连接时允许启动
        if not self._connected:
            QMessageBox.warning(self, "未连接", "请先打开串口再启动自动循环。")
            # 确保按钮状态一致
            self.cycle_button.setChecked(False)
            return

        self._auto_cycle_enabled = True
        self._cycle_state = "wait_open"
        self._append_log(f"INFO 自动循环启动：T1={self.t1_spin.value()}s, T2={self.t2_spin.value()}s")

        # 禁用其他继电器按钮以避免冲突
        for btn in self.relay_buttons:
            btn.setEnabled(False)

        # 启动倒计时与单次定时器
        self._countdown_remaining = int(self.t1_spin.value())
        self.cycle_button.setText(f"停止自动循环 ({self._countdown_remaining}s)")
        self.cycle_button.setChecked(True)
        self._countdown_timer.start()
        self._cycle_timer.start(int(self._countdown_remaining * 1000))

    def _stop_auto_cycle(self) -> None:
        if not self._auto_cycle_enabled:
            return
        self._auto_cycle_enabled = False
        self._cycle_timer.stop()
        self._countdown_timer.stop()
        self._countdown_remaining = 0
        self._append_log("INFO 自动循环已停止")
        self.cycle_button.setText("开始自动循环")
        self.cycle_button.setChecked(False)

        # 恢复继电器按钮可用性
        for btn in self.relay_buttons:
            btn.setEnabled(True)

    def _on_cycle_timeout(self) -> None:
        if not self._auto_cycle_enabled:
            return

        if self._cycle_state == "wait_open":
            # 打开第1路
            self._append_log("AUTO 打开第 1 路")
            self.set_relay(1, True)
            self._cycle_state = "wait_close"
            # 设置下一次等待为 T2
            self._countdown_remaining = int(self.t2_spin.value())
            self._countdown_timer.start()
            self._cycle_timer.start(self._countdown_remaining * 1000)
            self.cycle_button.setText(f"停止自动循环 ({self._countdown_remaining}s)")
        else:
            # 关闭第1路
            self._append_log("AUTO 关闭第 1 路")
            self.set_relay(1, False)
            self._cycle_state = "wait_open"
            # 设置下一次等待为 T1
            self._countdown_remaining = int(self.t1_spin.value())
            self._countdown_timer.start()
            self._cycle_timer.start(self._countdown_remaining * 1000)
            self.cycle_button.setText(f"停止自动循环 ({self._countdown_remaining}s)")

    def _set_connected(self, connected: bool) -> None:
        self._connected = connected
        self.connect_button.setEnabled(True)
        self.connect_button.setText("关闭串口" if connected else "打开串口")
        self.refresh_button.setEnabled(not connected)
        self.port_combo.setEnabled(not connected)
        self.baud_combo.setEnabled(not connected)
        self.address_spin.setEnabled(not connected)
        self.manual_refresh_button.setEnabled(connected)
        self.auto_poll_check.setEnabled(connected)
        for button in self.relay_buttons:
            button.setEnabled(connected)

        # 仅在串口连接时允许使用自动循环按钮
        try:
            self.cycle_button.setEnabled(connected)
        except Exception:
            pass

        if not connected:
            self.relay1_indicator.set_status(None)
            self.relay2_indicator.set_status(None)

    @Slot()
    def _sync_poll_timer(self) -> None:
        if self._connected and self.auto_poll_check.isChecked():
            self._poll_timer.start()
        else:
            self._poll_timer.stop()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._connected:
            self._disconnect_serial()
        if self._serial_thread is not None:
            self._serial_thread.stop()
            self._serial_thread.wait(1200)
        event.accept()

def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()

if __name__ == "__main__":
    raise SystemExit(main())
