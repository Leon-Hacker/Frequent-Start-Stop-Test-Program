import serial
import time
from PySide6.QtCore import QObject, QThread, Signal, QTimer, QMutex, QMutexLocker
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QPushButton, QLabel
from repeat import RelayCycleWorker

WRITE_CONFIRM_ATTEMPTS = 3


class RelayControl:
    def __init__(self, serial_port, address=0x01):
        self.serial_port = serial_port
        self.address = address
        self.ser = serial.Serial(port=serial_port, baudrate=9600, bytesize=8, parity='N', stopbits=1, timeout=1)
    
    def calculate_checksum(self, data):
        checksum = sum(data[:12]) & 0xFF  # Sum first 12 bytes and take the lower 8 bits
        return checksum
    
    def create_command(self, cmd, data_bytes):
        command = [0x48, 0x3A, self.address, cmd] + data_bytes
        checksum = self.calculate_checksum(command)
        command.append(checksum)
        command += [0x45, 0x44]  # Trailer as per the protocol
        return bytes(command)

    def create_single_relay_command(self, relay_number, turn_on):
        if relay_number not in (1, 2):
            raise ValueError("Relay number must be 1 or 2.")
        return bytes([
            0x48,
            0x3A,
            self.address,
            0x70,
            relay_number,
            0x01 if turn_on else 0x00,
            0x00,
            0x00,
            0x45,
            0x44,
        ])

    def validate_single_relay_response(self, response, relay_number, turn_on):
        expected_state = 0x01 if turn_on else 0x00
        expected = bytes([
            0x48,
            0x3A,
            self.address,
            0x71,
            relay_number,
            expected_state,
            0x00,
            0x00,
            0x45,
            0x44,
        ])
        return response == expected
    
    def send_command(self, command, response_size=15):
        try:
            self.ser.reset_input_buffer()
            self.ser.write(command)
            self.ser.flush()
            response = self.ser.read(response_size)
            return response if len(response) == response_size else None
        except Exception as e:
            print(f"Error sending command: {e}")
            return None

    def set_single_relay(self, relay_number, turn_on):
        command = self.create_single_relay_command(relay_number, turn_on)
        for attempt in range(1, WRITE_CONFIRM_ATTEMPTS + 1):
            response = self.send_command(command, response_size=10)
            if response and self.validate_single_relay_response(response, relay_number, turn_on):
                return True
            print(
                f"Relay {relay_number} {'ON' if turn_on else 'OFF'} was not confirmed; "
                f"retry {attempt}/{WRITE_CONFIRM_ATTEMPTS}"
            )
            time.sleep(0.1)
        return False
        
    def read_relay_status(self):
        cmd = 0x53
        data_bytes = [0x00] * 8
        command = self.create_command(cmd, data_bytes)
        self.ser.reset_input_buffer()
        self.ser.write(command)
        response = self.ser.read(15)
        if len(response) < 15:
            return None
        valid_channels = [0, 2, 4, 6]
        channel_states = [response[4 + (i // 2)] & (0x01 if i % 2 == 0 else 0x10) >> (4 * (i % 2)) for i in valid_channels]
        return channel_states
    
    def turn_all_on(self):
        return all(self.set_single_relay(relay_number, True) for relay_number in (1, 2))
    
    def turn_all_off(self):
        return all(self.set_single_relay(relay_number, False) for relay_number in (1, 2))
    
    def close(self):
        self.ser.close()

class RelayControlWorker(QObject):
    relay_state_updated = Signal(list)
    turn_on = Signal()
    turn_off = Signal()
    
    def __init__(self, relay_control):
        super().__init__()
        self.relay_control = relay_control
        self.running = True
        self.mutex = QMutex()
        self.poll_timer = None
        self.turn_on.connect(self.turn_all_on)
        self.turn_off.connect(self.turn_all_off)
    
    def start_monitoring(self):
        self.poll_timer = QTimer()
        self.poll_timer.setInterval(950)
        self.poll_timer.timeout.connect(self.monitor_relay_state)
        self.poll_timer.start()
    
    def monitor_relay_state(self):
        if not self.running:
            self.poll_timer.stop()
            return
        QThread.msleep(50)
        with QMutexLocker(self.mutex):
            states = self.relay_control.read_relay_status()
            if states:
                self.relay_state_updated.emit(states)
    
    def turn_all_on(self):
        with QMutexLocker(self.mutex):  # Ensure safe access to the critical section
            try:
                response = self.relay_control.turn_all_on()
            except Exception as e:
                print(f"Error controlling relay: {e}")

    def turn_all_off(self):
        with QMutexLocker(self.mutex):  # Ensure safe access to the critical section
            try:
                response = self.relay_control.turn_all_off()
            except Exception as e:
                print(f"Error controlling relay: {e}")

    def stop(self):
        self.running = False
        self.poll_timer.stop()

class RelayControlGUI(QWidget):
    def __init__(self, serial_port, repeat_count=2000):
        super().__init__()
        self.relay_control = RelayControl(serial_port)
        self.worker = RelayControlWorker(self.relay_control)
        
        self.thread = QThread()
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.start_monitoring)
        self.thread.start()
        
        self.worker.relay_state_updated.connect(self.update_status)
        
        self.layout = QVBoxLayout()
        self.status_labels = [QLabel(f"Relay {i+1}: OFF") for i in range(4)]
        for label in self.status_labels:
            self.layout.addWidget(label)
        
        self.btn_on = QPushButton("Turn All ON")
        self.btn_on.clicked.connect(self.turn_all_on)
        self.layout.addWidget(self.btn_on)
        
        self.btn_off = QPushButton("Turn All OFF")
        self.btn_off.clicked.connect(self.turn_all_off)
        self.layout.addWidget(self.btn_off)
        
        self.btn_cycle = QPushButton("Start Relay Cycle")
        self.btn_cycle.clicked.connect(self.start_relay_cycle)
        self.layout.addWidget(self.btn_cycle)
        
        self.setLayout(self.layout)
        self.setWindowTitle("Relay Control GUI")
        self.resize(300, 200)
        
        # RelayCycleWorker Setup
        self.cycle_worker = RelayCycleWorker(self.worker, repeat_count)
        self.cycle_thread = QThread()
        self.cycle_worker.moveToThread(self.cycle_thread)
        self.cycle_thread.started.connect(self.cycle_worker.run)
        self.cycle_worker.finished.connect(self.cycle_thread.quit)
        
    def update_status(self, states):
        for i, state in enumerate(states):
            self.status_labels[i].setText(f"Relay {i+1}: {'ON' if state else 'OFF'}")
    
    def turn_all_on(self):
        self.relay_control.turn_all_on()
    
    def turn_all_off(self):
        self.relay_control.turn_all_off()
    
    def start_relay_cycle(self):
        if not self.cycle_thread.isRunning():
            self.cycle_thread.start()
    
    def closeEvent(self, event):
        self.worker.stop()
        self.thread.quit()
        self.thread.wait()
        self.cycle_worker.stop()
        self.cycle_thread.quit()
        self.cycle_thread.wait()
        self.relay_control.close()
        event.accept()

if __name__ == "__main__":
    app = QApplication([])
    gui = RelayControlGUI("/dev/tty.usbserial-D30JKHXN", repeat_count=4000)
    gui.show()
    app.exec()
