from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Optional
import csv

import serial
from serial.tools import list_ports
from PySide6.QtCore import QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from power_protocol import (
    PowerStatus,
    build_measure_current_command,
    build_measure_voltage_command,
    build_output_command,
    build_query_output_command,
    build_set_current_command,
    build_set_voltage_command,
    encode_command,
    parse_float_response,
    parse_output_state,
)


POLL_INTERVAL_MS = 1000
SERIAL_TIMEOUT_SECONDS = 0.8
DEFAULT_BAUDRATE = 19200
QUERY_INTERVAL_SECONDS = 0.05


@dataclass(frozen=True)
class Command:
    kind: str
    voltage: Optional[float] = None
    current: Optional[float] = None
    output_on: Optional[bool] = None


class PowerSerialThread(QThread):
    connected = Signal(str)
    disconnected = Signal()
    error = Signal(str)
    log = Signal(str)
    status_changed = Signal(object)

    voltage_set_confirmed = Signal(float)
    current_set_confirmed = Signal(float)
    output_confirmed = Signal(bool)

    def __init__(self, port_name: str, baudrate: int) -> None:
        super().__init__()
        self._commands: Queue[Command] = Queue()
        self._running = True
        self._port_name = port_name
        self._baudrate = baudrate
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
        # 避免实时监控定时器不断堆积 read_status 命令。
        # 如果队列里已经有待执行命令，优先让已有命令先执行，
        # 这样“开启输出/停止输出”等手动控制不会被大量读取状态命令堵住。
        if self._commands.empty():
            self._commands.put(Command("read_status"))

    def set_voltage(self, voltage: float) -> None:
        self._commands.put(Command("set_voltage", voltage=voltage))

    def set_current(self, current: float) -> None:
        self._commands.put(Command("set_current", current=current))

    def set_output(self, output_on: bool) -> None:
        self._commands.put(Command("set_output", output_on=output_on))

    def set_all_and_start(self, voltage: float, current: float) -> None:
        self._commands.put(
            Command(
                "set_all_and_start",
                voltage=voltage,
                current=current,
                output_on=True,
            )
        )

    def _loop(self) -> None:
        while self._running:
            try:
                command = self._commands.get(timeout=0.05)
            except Empty:
                continue

            if command.kind == "stop":
                break

            if command.kind == "read_status":
                self._read_status()

            elif command.kind == "set_voltage" and command.voltage is not None:
                self._set_voltage(command.voltage)

            elif command.kind == "set_current" and command.current is not None:
                self._set_current(command.current)

            elif command.kind == "set_output" and command.output_on is not None:
                self._set_output(command.output_on)

            elif (
                command.kind == "set_all_and_start"
                and command.voltage is not None
                and command.current is not None
            ):
                self._set_all_and_start(command.voltage, command.current)

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

    def _write_command(self, command: str) -> None:
        if self._serial is None or not self._serial.is_open:
            raise serial.SerialException("串口未打开")

        self._serial.reset_input_buffer()
        self._serial.write(encode_command(command))
        self._serial.flush()
        self.log.emit(f"TX  {command}")

    def _query_command(self, command: str) -> str:
        if self._serial is None or not self._serial.is_open:
            raise serial.SerialException("串口未打开")

        self._serial.reset_input_buffer()
        self._serial.write(encode_command(command))
        self._serial.flush()
        self.log.emit(f"TX  {command}")

        response = self._serial.readline().decode("ascii", errors="replace").strip()

        if not response:
            raise TimeoutError("响应超时：未收到返回值")

        self.log.emit(f"RX  {response}")
        return response

    def _read_status(self, emit_errors: bool = True) -> Optional[PowerStatus]:
        try:
            current_response = self._query_command(build_measure_current_command())
            voltage_response = self._query_command(build_measure_voltage_command())
            output_response = self._query_command(build_query_output_command())

            measured_current = parse_float_response(current_response, "电流")
            measured_voltage = parse_float_response(voltage_response, "电压")
            output_on = parse_output_state(output_response)

        except (TimeoutError, serial.SerialException, ValueError, UnicodeError) as exc:
            if emit_errors:
                self.error.emit(f"读取电源状态失败：{exc}")
            return None

        status = PowerStatus(
            measured_voltage=measured_voltage,
            measured_current=measured_current,
            output_on=output_on,
        )

        self.status_changed.emit(status)
        return status

    def _set_voltage(self, voltage: float) -> None:
        try:
            self._write_command(build_set_voltage_command(voltage))
            self.voltage_set_confirmed.emit(voltage)
            self._read_status(emit_errors=False)
        except (TimeoutError, serial.SerialException, ValueError, UnicodeError) as exc:
            self.error.emit(f"设置电压失败：{exc}")

    def _set_current(self, current: float) -> None:
        try:
            self._write_command(build_set_current_command(current))
            self.current_set_confirmed.emit(current)
            self._read_status(emit_errors=False)
        except (TimeoutError, serial.SerialException, ValueError, UnicodeError) as exc:
            self.error.emit(f"设置电流失败：{exc}")

    def _set_output(self, output_on: bool) -> None:
        try:
            self._write_command(build_output_command(output_on))
            time.sleep(QUERY_INTERVAL_SECONDS)
            self.output_confirmed.emit(output_on)
            self._read_status(emit_errors=False)
        except (TimeoutError, serial.SerialException, ValueError, UnicodeError) as exc:
            self.error.emit(f"设置输出状态失败：{exc}")

    def _set_all_and_start(self, voltage: float, current: float) -> None:
        try:
            self._write_command(build_set_voltage_command(voltage))
            time.sleep(QUERY_INTERVAL_SECONDS)
            self.voltage_set_confirmed.emit(voltage)

            self._write_command(build_set_current_command(current))
            time.sleep(QUERY_INTERVAL_SECONDS)
            self.current_set_confirmed.emit(current)

            self._write_command(build_output_command(True))
            time.sleep(QUERY_INTERVAL_SECONDS)
            self.output_confirmed.emit(True)

            self._read_status(emit_errors=False)

        except (TimeoutError, serial.SerialException, ValueError, UnicodeError) as exc:
            self.error.emit(f"设置并开启电源失败：{exc}")


class StatusIndicator(QLabel):
    def __init__(self, title: str) -> None:
        super().__init__()
        self._title = title
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumHeight(42)
        self.setAutoFillBackground(True)
        self.set_status(None)

    def set_status(self, output_on: Optional[bool]) -> None:
        if output_on is None:
            text = f"{self._title}：未知"
            color = QColor("#9ca3af")
        elif output_on:
            text = f"{self._title}：运行中"
            color = QColor("#16a34a")
        else:
            text = f"{self._title}：已停止"
            color = QColor("#6b7280")

        palette = self.palette()
        palette.setColor(QPalette.Window, color)
        palette.setColor(QPalette.WindowText, QColor("white"))
        self.setPalette(palette)
        self.setText(text)


class MainWindow(QMainWindow):
    MAX_LOG_MESSAGES = 60

    def __init__(self) -> None:
        super().__init__()

        self.setWindowTitle("串口电源控制器")
        self.resize(840, 620)

        self._serial_thread: Optional[PowerSerialThread] = None
        self._connected = False
        self._log_messages: list[str] = []
        self._log_file_path = self._init_log_file()

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self.request_status)

        self._timed_output_timer = QTimer(self)
        self._timed_output_timer.setInterval(1000)
        self._timed_output_timer.timeout.connect(self._on_timed_output_tick)
        self._timed_output_remaining = 0
        self._timed_output_csv_path: Optional[Path] = None
        self._timed_output_writer: Optional[csv.writer] = None
        self._timed_output_file: Optional[object] = None
        self._timed_output_active = False

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
        self.baud_combo.addItems(["19200", "9600", "115200", "38400", "57600"])
        self.baud_combo.setCurrentText(str(DEFAULT_BAUDRATE))

        self.connect_button = QPushButton("打开串口")
        self.connect_button.clicked.connect(self.toggle_connection)

        self.auto_poll_check = QCheckBox("实时监控")
        self.auto_poll_check.setChecked(True)
        self.auto_poll_check.toggled.connect(self._sync_poll_timer)

        self.manual_refresh_button = QPushButton("手动读取")
        self.manual_refresh_button.clicked.connect(self.request_status)

        connection_layout.addWidget(QLabel("串口"), 0, 0)
        connection_layout.addWidget(self.port_combo, 0, 1)
        connection_layout.addWidget(self.refresh_button, 0, 2)
        connection_layout.addWidget(self.connect_button, 0, 3)

        connection_layout.addWidget(QLabel("波特率"), 1, 0)
        connection_layout.addWidget(self.baud_combo, 1, 1)
        connection_layout.addWidget(self.auto_poll_check, 1, 2)
        connection_layout.addWidget(self.manual_refresh_button, 1, 3)

        setting_group = QGroupBox("电源设置")
        setting_layout = QGridLayout(setting_group)

        self.voltage_spin = QDoubleSpinBox()
        self.voltage_spin.setRange(0.0, 1000.0)
        self.voltage_spin.setDecimals(3)
        self.voltage_spin.setSingleStep(0.1)
        self.voltage_spin.setSuffix(" V")

        self.current_spin = QDoubleSpinBox()
        self.current_spin.setRange(0.0, 1000.0)
        self.current_spin.setDecimals(3)
        self.current_spin.setSingleStep(0.1)
        self.current_spin.setSuffix(" A")

        self.set_voltage_button = QPushButton("设置电压")
        self.set_voltage_button.clicked.connect(self.set_voltage)

        self.set_current_button = QPushButton("设置电流")
        self.set_current_button.clicked.connect(self.set_current)

        self.apply_and_start_button = QPushButton("设置并开启")
        self.apply_and_start_button.clicked.connect(self.apply_and_start)

        self.output_on_button = QPushButton("开启输出")
        self.output_on_button.clicked.connect(lambda: self.set_output(True))

        self.output_off_button = QPushButton("停止输出")
        self.output_off_button.clicked.connect(lambda: self.set_output(False))

        setting_layout.addWidget(QLabel("设定电压"), 0, 0)
        setting_layout.addWidget(self.voltage_spin, 0, 1)
        setting_layout.addWidget(self.set_voltage_button, 0, 2)

        setting_layout.addWidget(QLabel("设定电流"), 1, 0)
        setting_layout.addWidget(self.current_spin, 1, 1)
        setting_layout.addWidget(self.set_current_button, 1, 2)

        setting_layout.addWidget(self.apply_and_start_button, 0, 3, 2, 1)
        setting_layout.addWidget(self.output_on_button, 2, 1)
        setting_layout.addWidget(self.output_off_button, 2, 2)

        timed_group = QGroupBox("定时通电")
        timed_layout = QGridLayout(timed_group)

        self.duration_spin = QDoubleSpinBox()
        self.duration_spin.setRange(0.1, 86400.0)
        self.duration_spin.setDecimals(1)
        self.duration_spin.setSingleStep(0.5)
        self.duration_spin.setValue(10.0)
        self.duration_spin.setSuffix(" s")

        self.start_timed_button = QPushButton("开始定时通电")
        self.start_timed_button.setCheckable(True)
        self.start_timed_button.clicked.connect(self._toggle_timed_output)

        self.timed_status_label = QLabel("剩余时间：-- s")
        self.timed_status_label.setAlignment(Qt.AlignCenter)
        self.timed_status_label.setMinimumHeight(30)

        timed_layout.addWidget(QLabel("通电时长"), 0, 0)
        timed_layout.addWidget(self.duration_spin, 0, 1)
        timed_layout.addWidget(self.start_timed_button, 0, 2)
        timed_layout.addWidget(self.timed_status_label, 1, 0, 1, 3)

        layout.addWidget(timed_group)

        monitor_group = QGroupBox("实时监控")
        monitor_layout = QGridLayout(monitor_group)

        self.output_indicator = StatusIndicator("电源输出")

        self.measured_voltage_label = QLabel("-- V")
        self.measured_current_label = QLabel("-- A")
        self.output_state_label = QLabel("未知")

        for label in (
            self.measured_voltage_label,
            self.measured_current_label,
            self.output_state_label,
        ):
            label.setAlignment(Qt.AlignCenter)
            label.setMinimumHeight(36)
            label.setStyleSheet("font-size: 18px; font-weight: 600;")

        monitor_layout.addWidget(self.output_indicator, 0, 0, 1, 4)

        monitor_layout.addWidget(QLabel("实测电压"), 1, 0)
        monitor_layout.addWidget(self.measured_voltage_label, 1, 1)

        monitor_layout.addWidget(QLabel("实测电流"), 1, 2)
        monitor_layout.addWidget(self.measured_current_label, 1, 3)

        monitor_layout.addWidget(QLabel("输出状态"), 2, 0)
        monitor_layout.addWidget(self.output_state_label, 2, 1)

        log_group = QGroupBox("通信日志")
        log_layout = QVBoxLayout(log_group)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)

        self.clear_log_button = QPushButton("清空显示日志")
        self.clear_log_button.clicked.connect(self._clear_log)

        log_layout.addWidget(self.log_view)
        log_layout.addWidget(self.clear_log_button, alignment=Qt.AlignRight)

        layout.addWidget(connection_group)
        layout.addWidget(setting_group)
        layout.addWidget(monitor_group)
        layout.addWidget(log_group, stretch=1)

        self.setCentralWidget(root)
        self.setStatusBar(QStatusBar(self))
        self.statusBar().showMessage("未连接")

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

        self._serial_thread = PowerSerialThread(
            port_name=port_name,
            baudrate=int(self.baud_combo.currentText()),
        )

        self._serial_thread.connected.connect(self._on_connected)
        self._serial_thread.disconnected.connect(self._on_disconnected)
        self._serial_thread.error.connect(self._on_error)
        self._serial_thread.log.connect(self._append_log)
        self._serial_thread.status_changed.connect(self._on_status_changed)
        self._serial_thread.voltage_set_confirmed.connect(self._on_voltage_set_confirmed)
        self._serial_thread.current_set_confirmed.connect(self._on_current_set_confirmed)
        self._serial_thread.output_confirmed.connect(self._on_output_confirmed)

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
        self._poll_timer.stop()
        self._stop_timed_output()
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

    @Slot()
    def request_status(self) -> None:
        if self._serial_thread is not None:
            self._serial_thread.request_status()

    @Slot()
    def set_voltage(self) -> None:
        if self._serial_thread is not None:
            self._serial_thread.set_voltage(float(self.voltage_spin.value()))

    @Slot()
    def set_current(self) -> None:
        if self._serial_thread is not None:
            self._serial_thread.set_current(float(self.current_spin.value()))

    @Slot()
    def apply_and_start(self) -> None:
        if self._serial_thread is not None:
            self._serial_thread.set_all_and_start(
                voltage=float(self.voltage_spin.value()),
                current=float(self.current_spin.value()),
            )

    def set_output(self, output_on: bool) -> None:
        if self._serial_thread is not None:
            self._serial_thread.set_output(output_on)

    @Slot(float)
    def _on_voltage_set_confirmed(self, voltage: float) -> None:
        message = f"电压设定命令已发送：{voltage:.3f} V"
        self._append_log(f"OK   {message}")
        self.statusBar().showMessage(message)

    @Slot(float)
    def _on_current_set_confirmed(self, current: float) -> None:
        message = f"电流设定命令已发送：{current:.3f} A"
        self._append_log(f"OK   {message}")
        self.statusBar().showMessage(message)

    @Slot(bool)
    def _on_output_confirmed(self, output_on: bool) -> None:
        action = "开启" if output_on else "停止"
        message = f"输出{action}命令已发送"
        self._append_log(f"OK   {message}")
        self.statusBar().showMessage(message)

    @Slot(object)
    def _on_status_changed(self, status: PowerStatus) -> None:
        if status.measured_voltage is None:
            self.measured_voltage_label.setText("-- V")
        else:
            self.measured_voltage_label.setText(f"{status.measured_voltage:.3f} V")

        if status.measured_current is None:
            self.measured_current_label.setText("-- A")
        else:
            self.measured_current_label.setText(f"{status.measured_current:.3f} A")

        self.output_indicator.set_status(status.output_on)

        if self._timed_output_active:
            self._record_timed_output_row(status)

        if status.output_on is True:
            state_text = "开启"
        elif status.output_on is False:
            state_text = "关闭"
        else:
            state_text = "未知"

        self.output_state_label.setText(state_text)

        self.statusBar().showMessage(
            f"状态已更新：U={self.measured_voltage_label.text()}，"
            f"I={self.measured_current_label.text()}，输出={state_text}"
        )

    def _init_log_file(self) -> Path:
        log_dir = Path(__file__).resolve().parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        log_filename = f"power_log_{datetime.now().strftime('%Y-%m-%d')}.txt"
        return log_dir / log_filename

    def _get_rotated_log_file(self) -> Path:
        log_dir = Path(__file__).resolve().parent / "logs"
        datepart = datetime.now().strftime("%Y-%m-%d")
        base_name = f"power_log_{datepart}"

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

        stem = last.stem

        if stem == base_name:
            idx = 1
        else:
            parts = stem.rsplit("_", 1)
            if len(parts) == 2 and parts[0] == base_name and parts[1].isdigit():
                idx = int(parts[1]) + 1
            else:
                idx = 1

        return log_dir / f"{base_name}_{idx}.txt"

    def _save_log_to_file(self, message: str) -> None:
        try:
            self._log_file_path = self._get_rotated_log_file()

            with open(self._log_file_path, "a", encoding="utf-8") as file:
                file.write(message + "\n")

        except Exception as exc:
            print(f"保存日志文件失败: {exc}")

    @Slot(str)
    def _append_log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        formatted_message = f"[{timestamp}] {message}"

        self._log_messages.append(formatted_message)
        self._refresh_log_display()
        self._save_log_to_file(formatted_message)

    def _refresh_log_display(self) -> None:
        self.log_view.clear()

        for message in self._log_messages[-self.MAX_LOG_MESSAGES:]:
            self.log_view.appendPlainText(message)

    @Slot()
    def _clear_log(self) -> None:
        self._log_messages.clear()
        self.log_view.clear()

    def _toggle_timed_output(self, checked: bool) -> None:
        if checked:
            self._start_timed_output()
        else:
            self._stop_timed_output()

    def _start_timed_output(self) -> None:
        if not self._connected or self._serial_thread is None:
            QMessageBox.warning(self, "未连接", "请先打开串口再启动定时通电。")
            self.start_timed_button.setChecked(False)
            return

        if self._timed_output_active:
            return

        self._timed_output_active = True
        self._timed_output_remaining = int(self.duration_spin.value())
        self._open_timed_output_csv()
        self._append_log(
            f"INFO 定时通电启动：{self._timed_output_remaining} 秒，电压 {self.voltage_spin.value():.3f} V，电流 {self.current_spin.value():.3f} A"
        )

        # 先设定电压、电流，并开启输出
        self._serial_thread.set_all_and_start(
            voltage=float(self.voltage_spin.value()),
            current=float(self.current_spin.value()),
        )

        self.start_timed_button.setText(f"停止定时通电 ({self._timed_output_remaining}s)")
        self.timed_status_label.setText(f"剩余时间：{self._timed_output_remaining} s")
        self._timed_output_timer.start()

    def _stop_timed_output(self) -> None:
        if not self._timed_output_active:
            return

        self._timed_output_active = False
        self._timed_output_timer.stop()
        self.start_timed_button.setChecked(False)
        self.start_timed_button.setText("开始定时通电")
        self.timed_status_label.setText("剩余时间：-- s")
        self._append_log("INFO 定时通电停止")

        if self._timed_output_file is not None:
            try:
                self._timed_output_file.close()
            except Exception:
                pass
            self._timed_output_file = None
            self._timed_output_writer = None
            self._timed_output_csv_path = None

        # 关闭输出
        if self._serial_thread is not None:
            self._serial_thread.set_output(False)

    def _on_timed_output_tick(self) -> None:
        if not self._timed_output_active:
            return

        self._timed_output_remaining -= 1
        if self._timed_output_remaining <= 0:
            self.timed_status_label.setText("剩余时间：0 s")
            self._stop_timed_output()
            return

        self.timed_status_label.setText(f"剩余时间：{self._timed_output_remaining} s")
        self.start_timed_button.setText(f"停止定时通电 ({self._timed_output_remaining}s)")

    def _open_timed_output_csv(self) -> None:
        log_dir = Path(__file__).resolve().parent
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._timed_output_csv_path = log_dir / f"power_output_{timestamp}.csv"
        try:
            self._timed_output_file = open(self._timed_output_csv_path, "w", encoding="utf-8", newline="")
            self._timed_output_writer = csv.writer(self._timed_output_file)
            self._timed_output_writer.writerow(["timestamp", "voltage_V", "current_A"])
        except Exception as exc:
            self._append_log(f"ERR  无法创建 CSV 文件：{exc}")
            self._timed_output_active = False
            self.start_timed_button.setChecked(False)
            self.start_timed_button.setText("开始定时通电")
            self.timed_status_label.setText("剩余时间：-- s")

    def _record_timed_output_row(self, status: PowerStatus) -> None:
        if self._timed_output_active and self._timed_output_writer is not None:
            try:
                self._timed_output_writer.writerow(
                    [
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-5],
                        status.measured_voltage if status.measured_voltage is not None else "",
                        status.measured_current if status.measured_current is not None else "",
                    ]
                )
                if self._timed_output_file is not None:
                    self._timed_output_file.flush()
            except Exception as exc:
                self._append_log(f"ERR  CSV 写入失败：{exc}")

    def _set_connected(self, connected: bool) -> None:
        self._connected = connected

        self.connect_button.setEnabled(True)
        self.connect_button.setText("关闭串口" if connected else "打开串口")

        self.refresh_button.setEnabled(not connected)
        self.port_combo.setEnabled(not connected)
        self.baud_combo.setEnabled(not connected)

        self.manual_refresh_button.setEnabled(connected)
        self.auto_poll_check.setEnabled(connected)

        for widget in (
            self.voltage_spin,
            self.current_spin,
            self.set_voltage_button,
            self.set_current_button,
            self.apply_and_start_button,
            self.output_on_button,
            self.output_off_button,
            self.duration_spin,
            self.start_timed_button,
        ):
            widget.setEnabled(connected)

        if not connected:
            self.measured_voltage_label.setText("-- V")
            self.measured_current_label.setText("-- A")
            self.output_state_label.setText("未知")
            self.output_indicator.set_status(None)

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