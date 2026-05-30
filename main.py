from __future__ import annotations

import sys
import time
import csv
from dataclasses import dataclass
from queue import Empty, Queue
from typing import Optional
from pathlib import Path
from datetime import datetime
from collections import deque

import serial
from serial.tools import list_ports
from PySide6.QtCore import QPointF, QRectF, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette, QPen
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
    QSpinBox,
    QStatusBar,
    QTabWidget,
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

POLL_INTERVAL_MS = 200
SERIAL_TIMEOUT_SECONDS = 0.5
WRITE_CONFIRM_ATTEMPTS = 3
POWER_POLL_INTERVAL_MS = 1000
POWER_SERIAL_TIMEOUT_SECONDS = 0.8
QUERY_INTERVAL_SECONDS = 0.05
DEFAULT_BAUDRATE = 19200


@dataclass(frozen=True)
class Command:
    kind: str
    relay: Optional[int] = None
    state: Optional[bool] = None


@dataclass(frozen=True)
class PowerCommand:
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
        self._commands: Queue[PowerCommand] = Queue()
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
                timeout=POWER_SERIAL_TIMEOUT_SECONDS,
                write_timeout=POWER_SERIAL_TIMEOUT_SECONDS,
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
        self._commands.put(PowerCommand("stop"))

    def request_status(self) -> None:
        if self._commands.empty():
            self._commands.put(PowerCommand("read_status"))

    def set_voltage(self, voltage: float) -> None:
        self._commands.put(PowerCommand("set_voltage", voltage=voltage))

    def set_current(self, current: float) -> None:
        self._commands.put(PowerCommand("set_current", current=current))

    def set_output(self, output_on: bool) -> None:
        self._commands.put(PowerCommand("set_output", output_on=output_on))

    def set_all_and_start(self, voltage: float, current: float) -> None:
        self._commands.put(
            PowerCommand(
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


# 电压趋势显示控件
class VoltageTrendWidget(QWidget):
    def __init__(self, title: str, color: QColor) -> None:
        super().__init__()
        self._title = title
        self._color = color
        self._points: deque[tuple[float, float]] = deque()
        self._window_seconds = 300.0
        self.setMinimumHeight(220)

    def add_reading(self, timestamp: datetime, voltage: float) -> None:
        ts = timestamp.timestamp()
        self._points.append((ts, voltage))
        self._trim(ts)
        self.update()

    def clear(self) -> None:
        self._points.clear()
        self.update()

    def _trim(self, now_ts: float) -> None:
        cutoff = now_ts - self._window_seconds
        while self._points and self._points[0][0] < cutoff:
            self._points.popleft()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = self.rect()
        painter.fillRect(rect, QColor("#f7f8fa"))

        margin_left = 62
        margin_right = 20
        margin_top = 34
        margin_bottom = 42
        plot = QRectF(
            margin_left,
            margin_top,
            max(1, rect.width() - margin_left - margin_right),
            max(1, rect.height() - margin_top - margin_bottom),
        )

        painter.setPen(QPen(QColor("#d1d5db"), 1))
        painter.drawRect(plot)

        painter.setPen(QColor("#111827"))
        painter.drawText(12, 22, self._title)

        if not self._points:
            painter.setPen(QColor("#6b7280"))
            painter.drawText(plot, Qt.AlignCenter, "暂无电压数据")
            return

        now_ts = self._points[-1][0]
        start_ts = now_ts - self._window_seconds

        values = [value for _, value in self._points]
        min_value = min(values)
        max_value = max(values)
        if min_value == max_value:
            padding = max(0.1, abs(max_value) * 0.05)
        else:
            padding = (max_value - min_value) * 0.08
        min_value -= padding
        max_value += padding
        value_span = max_value - min_value

        grid_pen = QPen(QColor("#e5e7eb"), 1)
        text_color = QColor("#4b5563")

        for i in range(6):
            y = plot.top() + plot.height() * i / 5
            value = max_value - value_span * i / 5
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(text_color)
            painter.drawText(6, int(y + 4), f"{value:.2f}")

        for i in range(6):
            x = plot.left() + plot.width() * i / 5
            seconds_ago = int(self._window_seconds * (5 - i) / 5)
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            painter.setPen(text_color)
            label = "现在" if seconds_ago == 0 else f"-{seconds_ago}s"
            painter.drawText(int(x - 18), rect.height() - 16, label)

        path = QPainterPath()
        has_point = False
        last_point: Optional[QPointF] = None
        last_value: Optional[float] = None

        for ts, value in self._points:
            if ts < start_ts:
                continue
            x_ratio = (ts - start_ts) / self._window_seconds
            y_ratio = (value - min_value) / value_span
            point = QPointF(
                plot.left() + x_ratio * plot.width(),
                plot.bottom() - y_ratio * plot.height(),
            )
            if not has_point:
                path.moveTo(point)
                has_point = True
            else:
                path.lineTo(point)
            last_point = point
            last_value = value

        if has_point:
            painter.setPen(QPen(self._color, 2.2))
            painter.drawPath(path)

        if last_point is not None and last_value is not None:
            painter.setPen(QPen(self._color, 2))
            painter.setBrush(self._color)
            painter.drawEllipse(last_point, 4, 4)
            painter.setPen(QColor("#111827"))
            painter.drawText(
                int(plot.right() - 120),
                int(plot.top() - 10),
                f"最新：{last_value:.3f} V",
            )


class MainWindow(QMainWindow):
    MAX_LOG_MESSAGES = 60

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("继电器和电源控制器")
        self.resize(1000, 800)

        self._serial_thread: Optional[SerialThread] = None
        self._power_serial_thread: Optional[PowerSerialThread] = None
        self._connected = False
        self._power_connected = False

        self._log_messages: list[str] = []
        self._log_file_path = self._init_log_file()

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self.request_status)

        self._power_poll_timer = QTimer(self)
        self._power_poll_timer.setInterval(POWER_POLL_INTERVAL_MS)
        self._power_poll_timer.timeout.connect(self.request_power_status)

        # 继电器自动循环相关
        self._cycle_timer = QTimer(self)
        self._cycle_timer.setInterval(200)
        self._cycle_timer.timeout.connect(self._on_cycle_timeout)
        self._auto_cycle_enabled = False
        self._cycle_state = "wait_open"
        self._cycle_next_timestamp: Optional[float] = None
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._on_countdown_tick)
        self._countdown_remaining = 0

        # 定时通电相关
        self._timed_output_timer = QTimer(self)
        self._timed_output_timer.setInterval(1000)
        self._timed_output_timer.timeout.connect(self._on_timed_output_tick)
        self._timed_output_remaining = 0
        self._timed_output_end_timestamp: Optional[float] = None
        self._timed_output_csv_path: Optional[Path] = None
        self._timed_output_writer: Optional[csv.writer] = None
        self._timed_output_file: Optional[object] = None
        self._timed_output_active = False


        self._build_ui()
        self.refresh_ports()
        self._set_connected(False)
        self._set_power_connected(False)

    def _build_ui(self) -> None:
        root = QWidget(self)
        layout = QVBoxLayout(root)

        # 创建标签页
        tabs = QTabWidget()

        # 继电器标签页
        relay_tab = QWidget()
        relay_layout = QVBoxLayout(relay_tab)

        relay_connection_group = QGroupBox("继电器 - 串口连接")
        relay_connection_layout = QGridLayout(relay_connection_group)

        self.relay_port_combo = QComboBox()
        self.relay_refresh_button = QPushButton("刷新串口")
        self.relay_refresh_button.clicked.connect(self.refresh_ports)

        self.relay_baud_combo = QComboBox()
        self.relay_baud_combo.addItems(["9600", "115200", "19200", "38400", "57600"])
        self.relay_baud_combo.setCurrentText("9600")

        self.relay_address_spin = QSpinBox()
        self.relay_address_spin.setRange(0, 255)
        self.relay_address_spin.setValue(1)

        self.relay_connect_button = QPushButton("打开继电器串口")
        self.relay_connect_button.clicked.connect(self.toggle_relay_connection)

        self.relay_auto_poll_check = QCheckBox("自动轮询")
        self.relay_auto_poll_check.setChecked(True)
        self.relay_auto_poll_check.toggled.connect(self._sync_relay_poll_timer)

        self.relay_manual_refresh_button = QPushButton("手动读取")
        self.relay_manual_refresh_button.clicked.connect(self.request_status)

        relay_connection_layout.addWidget(QLabel("串口"), 0, 0)
        relay_connection_layout.addWidget(self.relay_port_combo, 0, 1)
        relay_connection_layout.addWidget(self.relay_refresh_button, 0, 2)
        relay_connection_layout.addWidget(self.relay_connect_button, 0, 3)

        relay_connection_layout.addWidget(QLabel("波特率"), 1, 0)
        relay_connection_layout.addWidget(self.relay_baud_combo, 1, 1)
        relay_connection_layout.addWidget(QLabel("地址"), 1, 2)
        relay_connection_layout.addWidget(self.relay_address_spin, 1, 3)

        relay_connection_layout.addWidget(self.relay_auto_poll_check, 2, 0)
        relay_connection_layout.addWidget(self.relay_manual_refresh_button, 2, 1)

        relay_control_group = QGroupBox("继电器控制")
        relay_control_layout = QGridLayout(relay_control_group)

        self.relay1_indicator = RelayIndicator("第 1 路")
        self.relay2_indicator = RelayIndicator("第 2 路")
        self.relay_buttons: list[QPushButton] = []

        relay_control_layout.addWidget(self.relay1_indicator, 0, 0, 1, 2)
        relay_control_layout.addWidget(self._relay_button("第 1 路打开", 1, True), 1, 0)
        relay_control_layout.addWidget(self._relay_button("第 1 路关闭", 1, False), 1, 1)
        relay_control_layout.addWidget(self.relay2_indicator, 0, 2, 1, 2)
        relay_control_layout.addWidget(self._relay_button("第 2 路打开", 2, True), 1, 2)
        relay_control_layout.addWidget(self._relay_button("第 2 路关闭", 2, False), 1, 3)

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

        relay_layout.addWidget(relay_connection_group)
        relay_layout.addWidget(relay_control_group)
        relay_layout.addWidget(cycle_group)
        relay_layout.addStretch()

        # 电源标签页
        power_tab = QWidget()
        power_layout = QVBoxLayout(power_tab)

        power_connection_group = QGroupBox("电源 - 串口连接")
        power_connection_layout = QGridLayout(power_connection_group)

        self.power_port_combo = QComboBox()
        self.power_refresh_button = QPushButton("刷新串口")
        self.power_refresh_button.clicked.connect(self.refresh_ports)

        self.power_baud_combo = QComboBox()
        self.power_baud_combo.addItems(["19200", "9600", "115200", "38400", "57600"])
        self.power_baud_combo.setCurrentText("19200")

        self.power_connect_button = QPushButton("打开电源串口")
        self.power_connect_button.clicked.connect(self.toggle_power_connection)

        self.power_auto_poll_check = QCheckBox("实时监控")
        self.power_auto_poll_check.setChecked(True)
        self.power_auto_poll_check.toggled.connect(self._sync_power_poll_timer)

        self.power_manual_refresh_button = QPushButton("手动读取")
        self.power_manual_refresh_button.clicked.connect(self.request_power_status)

        power_connection_layout.addWidget(QLabel("串口"), 0, 0)
        power_connection_layout.addWidget(self.power_port_combo, 0, 1)
        power_connection_layout.addWidget(self.power_refresh_button, 0, 2)
        power_connection_layout.addWidget(self.power_connect_button, 0, 3)

        power_connection_layout.addWidget(QLabel("波特率"), 1, 0)
        power_connection_layout.addWidget(self.power_baud_combo, 1, 1)
        power_connection_layout.addWidget(self.power_auto_poll_check, 1, 2)
        power_connection_layout.addWidget(self.power_manual_refresh_button, 1, 3)

        power_setting_group = QGroupBox("电源设置")
        power_setting_layout = QGridLayout(power_setting_group)

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
        self.output_on_button.clicked.connect(lambda: self.set_power_output(True))

        self.output_off_button = QPushButton("停止输出")
        self.output_off_button.clicked.connect(lambda: self.set_power_output(False))

        power_setting_layout.addWidget(QLabel("设定电压"), 0, 0)
        power_setting_layout.addWidget(self.voltage_spin, 0, 1)
        power_setting_layout.addWidget(self.set_voltage_button, 0, 2)

        power_setting_layout.addWidget(QLabel("设定电流"), 1, 0)
        power_setting_layout.addWidget(self.current_spin, 1, 1)
        power_setting_layout.addWidget(self.set_current_button, 1, 2)

        power_setting_layout.addWidget(self.apply_and_start_button, 0, 3, 2, 1)
        power_setting_layout.addWidget(self.output_on_button, 2, 1)
        power_setting_layout.addWidget(self.output_off_button, 2, 2)

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

        self.start_timed_and_cycle_button = QPushButton("定时通电并开始自动循环")
        self.start_timed_and_cycle_button.clicked.connect(self.start_timed_output_and_cycle)

        self.timed_status_label = QLabel("剩余时间：-- s")
        self.timed_status_label.setAlignment(Qt.AlignCenter)
        self.timed_status_label.setMinimumHeight(30)

        timed_layout.addWidget(QLabel("通电时长"), 0, 0)
        timed_layout.addWidget(self.duration_spin, 0, 1)
        timed_layout.addWidget(self.start_timed_button, 0, 2)
        timed_layout.addWidget(self.start_timed_and_cycle_button, 1, 0, 1, 3)
        timed_layout.addWidget(self.timed_status_label, 2, 0, 1, 3)

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

        voltage_trend_group = QGroupBox("最近五分钟电压曲线")
        voltage_trend_layout = QVBoxLayout(voltage_trend_group)
        self.voltage_trend = VoltageTrendWidget("实测电压 - 最近 5 分钟", QColor("#1473e6"))
        voltage_trend_layout.addWidget(self.voltage_trend)

        power_layout.addWidget(power_connection_group)
        power_layout.addWidget(power_setting_group)
        power_layout.addWidget(timed_group)
        power_layout.addWidget(monitor_group)
        power_layout.addWidget(voltage_trend_group)
        power_layout.addStretch()

        tabs.addTab(relay_tab, "继电器控制")
        tabs.addTab(power_tab, "电源控制")

        layout.addWidget(tabs)

        # 日志组
        log_group = QGroupBox("通信日志")
        log_layout = QVBoxLayout(log_group)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.clear_log_button = QPushButton("清空日志")
        self.clear_log_button.clicked.connect(self._clear_log)
        log_layout.addWidget(self.log_view, stretch=1)
        log_layout.addWidget(self.clear_log_button, alignment=Qt.AlignRight)

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

    @Slot()
    def request_power_status(self) -> None:
        if self._power_serial_thread is not None:
            self._power_serial_thread.request_status()

    @Slot()
    def set_voltage(self) -> None:
        if self._power_serial_thread is not None:
            self._power_serial_thread.set_voltage(float(self.voltage_spin.value()))

    @Slot()
    def set_current(self) -> None:
        if self._power_serial_thread is not None:
            self._power_serial_thread.set_current(float(self.current_spin.value()))

    @Slot()
    def apply_and_start(self) -> None:
        if self._power_serial_thread is not None:
            self._power_serial_thread.set_all_and_start(
                voltage=float(self.voltage_spin.value()),
                current=float(self.current_spin.value()),
            )

    def set_power_output(self, output_on: bool) -> None:
        if self._power_serial_thread is not None:
            self._power_serial_thread.set_output(output_on)

    def _init_log_file(self) -> Path:
        log_dir = Path(__file__).resolve().parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_filename = f"integrated_log_{datetime.now().strftime('%Y-%m-%d')}.txt"
        return log_dir / log_filename

    def _get_rotated_log_file(self) -> Path:
        log_dir = Path(__file__).resolve().parent / "logs"
        datepart = datetime.now().strftime('%Y-%m-%d')
        base_name = f"integrated_log_{datepart}"
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
            parts = stem.rsplit('_', 1)
            if len(parts) == 2 and parts[0] == base_name and parts[1].isdigit():
                idx = int(parts[1]) + 1
            else:
                idx = 1
        return log_dir / f"{base_name}_{idx}.txt"

    def _save_log_to_file(self, message: str) -> None:
        try:
            self._log_file_path = self._get_rotated_log_file()
            with open(self._log_file_path, 'a', encoding='utf-8') as f:
                f.write(message + '\n')
        except Exception as e:
            print(f"保存日志文件失败: {e}")

    def _refresh_log_display(self) -> None:
        self.log_view.clear()
        for msg in self._log_messages[-self.MAX_LOG_MESSAGES:]:
            self.log_view.appendPlainText(msg)

    @Slot()
    def _clear_log(self) -> None:
        self._log_messages.clear()
        self.log_view.clear()

    @Slot(int, bool)
    def set_relay(self, relay: int, state: bool) -> None:
        if self._serial_thread is not None:
            self._serial_thread.set_relay(relay, state)

    @Slot()
    def refresh_ports(self) -> None:
        for combo in [self.relay_port_combo, self.power_port_combo]:
            current = combo.currentData() or combo.currentText()
            combo.clear()

            ports = list(list_ports.comports())
            for port in ports:
                label = f"{port.device}  {port.description}"
                combo.addItem(label, port.device)

            if current:
                index = combo.findData(current)
                if index >= 0:
                    combo.setCurrentIndex(index)

            if not ports:
                combo.addItem("未发现串口", "")

    @Slot()
    def toggle_relay_connection(self) -> None:
        if self._connected:
            self._disconnect_relay()
        else:
            self._connect_relay()

    @Slot()
    def toggle_power_connection(self) -> None:
        if self._power_connected:
            self._disconnect_power()
        else:
            self._connect_power()

    def _connect_relay(self) -> None:
        port_name = self.relay_port_combo.currentData()
        if not port_name:
            QMessageBox.warning(self, "无法打开串口", "请先选择可用串口。")
            return

        self._serial_thread = SerialThread(
            port_name,
            int(self.relay_baud_combo.currentText()),
            self.relay_address_spin.value(),
        )
        self._serial_thread.connected.connect(self._on_relay_connected)
        self._serial_thread.disconnected.connect(self._on_relay_disconnected)
        self._serial_thread.error.connect(self._on_relay_error)
        self._serial_thread.log.connect(self._append_log)
        self._serial_thread.relay_command_confirmed.connect(self._on_relay_command_confirmed)
        self._serial_thread.status_changed.connect(self._on_relay_status_changed)
        self._serial_thread.start()
        self.relay_connect_button.setEnabled(False)
        self.statusBar().showMessage("正在打开继电器串口...")

    def _connect_power(self) -> None:
        port_name = self.power_port_combo.currentData()
        if not port_name:
            QMessageBox.warning(self, "无法打开串口", "请先选择可用串口。")
            return

        self._power_serial_thread = PowerSerialThread(
            port_name=port_name,
            baudrate=int(self.power_baud_combo.currentText()),
        )
        self._power_serial_thread.connected.connect(self._on_power_connected)
        self._power_serial_thread.disconnected.connect(self._on_power_disconnected)
        self._power_serial_thread.error.connect(self._on_power_error)
        self._power_serial_thread.log.connect(self._append_log)
        self._power_serial_thread.status_changed.connect(self._on_power_status_changed)
        self._power_serial_thread.voltage_set_confirmed.connect(self._on_voltage_set_confirmed)
        self._power_serial_thread.current_set_confirmed.connect(self._on_current_set_confirmed)
        self._power_serial_thread.output_confirmed.connect(self._on_power_output_confirmed)

        self._power_serial_thread.start()
        self.power_connect_button.setEnabled(False)
        self.statusBar().showMessage("正在打开电源串口...")

    def _disconnect_relay(self) -> None:
        self._poll_timer.stop()
        if self._serial_thread is not None:
            self._serial_thread.stop()
        self.statusBar().showMessage("正在关闭继电器串口...")

    def _disconnect_power(self) -> None:
        self._power_poll_timer.stop()
        self._stop_timed_output()
        if self._power_serial_thread is not None:
            self._power_serial_thread.stop()
        self.statusBar().showMessage("正在关闭电源串口...")

    @Slot(str)
    def _on_relay_connected(self, message: str) -> None:
        self._set_connected(True)
        self._append_log(f"INFO [继电器] 已连接 {message}")
        self.statusBar().showMessage(f"[继电器] 已连接 {message}")
        self._sync_relay_poll_timer()
        self.request_status()

    @Slot(str)
    def _on_power_connected(self, message: str) -> None:
        self._set_power_connected(True)
        self._append_log(f"INFO [电源] 已连接 {message}")
        self.statusBar().showMessage(f"[电源] 已连接 {message}")
        self._sync_power_poll_timer()
        self.request_power_status()

    @Slot()
    def _on_relay_disconnected(self) -> None:
        try:
            self._stop_auto_cycle()
        except Exception:
            pass
        self._poll_timer.stop()
        self._set_connected(False)
        self._append_log("INFO [继电器] 已断开")
        self.statusBar().showMessage("[继电器] 未连接")
        if self._serial_thread is not None:
            self._serial_thread.wait(1000)
            self._serial_thread = None

    @Slot()
    def _on_power_disconnected(self) -> None:
        self._power_poll_timer.stop()
        self._stop_timed_output()
        self._set_power_connected(False)
        self._append_log("INFO [电源] 已断开")
        self.statusBar().showMessage("[电源] 未连接")
        if self._power_serial_thread is not None:
            self._power_serial_thread.wait(1000)
            self._power_serial_thread = None

    @Slot(str)
    def _on_relay_error(self, message: str) -> None:
        self._append_log(f"ERR  [继电器] {message}")
        self.statusBar().showMessage(message)
        if "打开串口失败" in message:
            self._set_connected(False)

    @Slot(str)
    def _on_power_error(self, message: str) -> None:
        self._append_log(f"ERR  [电源] {message}")
        self.statusBar().showMessage(message)
        if "打开串口失败" in message:
            self._set_power_connected(False)

    @Slot(int, bool)
    def _on_relay_command_confirmed(self, relay: int, state: bool) -> None:
        action = "打开" if state else "关闭"
        message = f"第 {relay} 路{action}成功，应答已确认"
        self._append_log(f"OK   {message}")
        self.statusBar().showMessage(message)

    @Slot(object)
    def _on_relay_status_changed(self, status: RelayStatus) -> None:
        self.relay1_indicator.set_status(status.relay1)
        self.relay2_indicator.set_status(status.relay2)
        self.statusBar().showMessage(
            f"[继电器] 第 1 路 {'开' if status.relay1 else '关'}，"
            f"第 2 路 {'开' if status.relay2 else '关'}"
        )

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
    def _on_power_output_confirmed(self, output_on: bool) -> None:
        action = "开启" if output_on else "停止"
        message = f"输出{action}命令已发送"
        self._append_log(f"OK   {message}")
        self.statusBar().showMessage(message)

    @Slot(object)
    def _on_power_status_changed(self, status: PowerStatus) -> None:
        if status.measured_voltage is None:
            self.measured_voltage_label.setText("-- V")
        else:
            self.measured_voltage_label.setText(f"{status.measured_voltage:.3f} V")
            self.voltage_trend.add_reading(datetime.now(), status.measured_voltage)

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
            f"[电源] U={self.measured_voltage_label.text()}，"
            f"I={self.measured_current_label.text()}，输出={state_text}"
        )

    @Slot(str)
    def _append_log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        formatted_message = f"[{timestamp}] {message}"
        self._log_messages.append(formatted_message)
        self._refresh_log_display()
        self._save_log_to_file(formatted_message)

    def _on_countdown_tick(self) -> None:
        if not self._auto_cycle_enabled:
            self._countdown_timer.stop()
            return

        if self._cycle_next_timestamp is None:
            self._countdown_timer.stop()
            return

        remaining_seconds = self._cycle_next_timestamp - time.monotonic()
        self._countdown_remaining = max(0, int(round(remaining_seconds)))
        try:
            self.cycle_button.setText(f"停止自动循环 ({self._countdown_remaining}s)")
        except Exception:
            pass

    def _toggle_auto_cycle(self, enable: bool) -> None:
        if enable:
            self._start_auto_cycle()
        else:
            self._stop_auto_cycle()

    def _start_auto_cycle(self) -> None:
        if self._auto_cycle_enabled:
            return
        if not self._connected:
            QMessageBox.warning(self, "未连接", "请先打开继电器串口再启动自动循环。")
            self.cycle_button.setChecked(False)
            return

        self._auto_cycle_enabled = True
        self._cycle_state = "wait_open"
        self._cycle_next_timestamp = time.monotonic() + float(self.t1_spin.value())
        self._append_log(f"INFO 继电器自动循环启动：T1={self.t1_spin.value()}s, T2={self.t2_spin.value()}s")

        for btn in self.relay_buttons:
            btn.setEnabled(False)

        self._countdown_remaining = max(0, int(round(self._cycle_next_timestamp - time.monotonic())))
        self.cycle_button.setText(f"停止自动循环 ({self._countdown_remaining}s)")
        self.cycle_button.setChecked(True)
        self._countdown_timer.start()
        self._cycle_timer.start()

    def _stop_auto_cycle(self) -> None:
        if not self._auto_cycle_enabled:
            return
        self._auto_cycle_enabled = False
        self._cycle_next_timestamp = None
        self._cycle_timer.stop()
        self._countdown_timer.stop()
        self._countdown_remaining = 0
        self._append_log("INFO 继电器自动循环已停止")
        self.cycle_button.setText("开始自动循环")
        self.cycle_button.setChecked(False)

        for btn in self.relay_buttons:
            btn.setEnabled(True)

    def _on_cycle_timeout(self) -> None:
        if not self._auto_cycle_enabled:
            return

        if self._cycle_next_timestamp is None:
            self._stop_auto_cycle()
            return

        now = time.monotonic()
        if now < self._cycle_next_timestamp:
            return

        scheduled_timestamp = self._cycle_next_timestamp

        if self._cycle_state == "wait_open":
            self._append_log("AUTO 打开第 1 路")
            self.set_relay(1, True)
            self._cycle_state = "wait_close"
            self._cycle_next_timestamp = scheduled_timestamp + float(self.t2_spin.value())
        else:
            self._append_log("AUTO 关闭第 1 路")
            self.set_relay(1, False)
            self._cycle_state = "wait_open"
            self._cycle_next_timestamp = scheduled_timestamp + float(self.t1_spin.value())

        self._countdown_remaining = max(0, int(round(self._cycle_next_timestamp - time.monotonic())))
        self.cycle_button.setText(f"停止自动循环 ({self._countdown_remaining}s)")
        if not self._countdown_timer.isActive():
            self._countdown_timer.start()

    def _toggle_timed_output(self, checked: bool) -> None:
        if checked:
            self._start_timed_output()
        else:
            self._stop_timed_output()

    def _start_timed_output(self) -> None:
        if not self._power_connected or self._power_serial_thread is None:
            QMessageBox.warning(self, "未连接", "请先打开电源串口再启动定时通电。")
            self.start_timed_button.setChecked(False)
            return

        if self._timed_output_active:
            return

        duration_seconds = float(self.duration_spin.value())
        self._timed_output_active = True
        self._timed_output_end_timestamp = time.monotonic() + duration_seconds
        self._timed_output_remaining = max(0, int(round(duration_seconds)))
        self._open_timed_output_csv()
        self._append_log(
            f"INFO 定时通电启动：{duration_seconds:.1f} 秒，电压 {self.voltage_spin.value():.3f} V，电流 {self.current_spin.value():.3f} A"
        )

        self._power_serial_thread.set_all_and_start(
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
        self._timed_output_end_timestamp = None
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

        if self._power_serial_thread is not None:
            self._power_serial_thread.set_output(False)

    def _on_timed_output_tick(self) -> None:
        if not self._timed_output_active:
            return

        if self._timed_output_end_timestamp is None:
            self._stop_timed_output()
            return

        remaining_seconds = self._timed_output_end_timestamp - time.monotonic()
        self._timed_output_remaining = max(0, int(round(remaining_seconds)))

        if remaining_seconds <= 0:
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
            self._timed_output_end_timestamp = None
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
        self.relay_connect_button.setEnabled(True)
        self.relay_connect_button.setText("关闭继电器串口" if connected else "打开继电器串口")
        self.relay_refresh_button.setEnabled(not connected)
        self.relay_port_combo.setEnabled(not connected)
        self.relay_baud_combo.setEnabled(not connected)
        self.relay_address_spin.setEnabled(not connected)
        self.relay_manual_refresh_button.setEnabled(connected)
        self.relay_auto_poll_check.setEnabled(connected)
        for button in self.relay_buttons:
            button.setEnabled(connected)

        try:
            self.cycle_button.setEnabled(connected)
        except Exception:
            pass

        if not connected:
            self.relay1_indicator.set_status(None)
            self.relay2_indicator.set_status(None)

        self._update_timed_and_cycle_button()

    def _set_power_connected(self, connected: bool) -> None:
        self._power_connected = connected
        self.power_connect_button.setEnabled(True)
        self.power_connect_button.setText("关闭电源串口" if connected else "打开电源串口")
        self.power_refresh_button.setEnabled(not connected)
        self.power_port_combo.setEnabled(not connected)
        self.power_baud_combo.setEnabled(not connected)
        self.power_manual_refresh_button.setEnabled(connected)
        self.power_auto_poll_check.setEnabled(connected)

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
            self.start_timed_and_cycle_button,
        ):
            widget.setEnabled(connected)

        if not connected:
            self.measured_voltage_label.setText("-- V")
            self.measured_current_label.setText("-- A")
            self.output_state_label.setText("未知")
            self.output_indicator.set_status(None)
            if hasattr(self, "voltage_trend"):
                self.voltage_trend.clear()

        self._update_timed_and_cycle_button()

    @Slot()
    def _sync_relay_poll_timer(self) -> None:
        if self._connected and self.relay_auto_poll_check.isChecked():
            self._poll_timer.start()
        else:
            self._poll_timer.stop()

    @Slot()
    def _sync_power_poll_timer(self) -> None:
        if self._power_connected and self.power_auto_poll_check.isChecked():
            self._power_poll_timer.start()
        else:
            self._power_poll_timer.stop()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._connected:
            self._disconnect_relay()
        if self._power_connected:
            self._disconnect_power()

        if self._serial_thread is not None:
            self._serial_thread.stop()
            self._serial_thread.wait(1200)

        if self._power_serial_thread is not None:
            self._power_serial_thread.stop()
            self._power_serial_thread.wait(1200)

        event.accept()

    def _update_timed_and_cycle_button(self) -> None:
        if hasattr(self, 'start_timed_and_cycle_button'):
            self.start_timed_and_cycle_button.setEnabled(self._connected and self._power_connected)

    @Slot()
    def start_timed_output_and_cycle(self) -> None:
        if not self._power_connected or self._power_serial_thread is None:
            QMessageBox.warning(self, "未连接", "请先打开电源串口再启动定时通电与自动循环。")
            return

        if not self._connected or self._serial_thread is None:
            QMessageBox.warning(self, "未连接", "请先打开继电器串口再启动定时通电与自动循环。")
            return

        if self._timed_output_active or self._auto_cycle_enabled:
            QMessageBox.warning(self, "已运行", "定时通电或自动循环已经处于运行状态。请先停止后再启动。")
            return

        self._start_timed_output()
        if not self._timed_output_active:
            return

        self._start_auto_cycle()


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
