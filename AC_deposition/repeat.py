from PySide6.QtCore import QObject, QThread, Signal, QTimer
import time

class RelayCycleWorker(QObject):
    finished = Signal()
    
    def __init__(self, relay_control_worker, repeat_count):
        super().__init__()
        self.relay_control = relay_control_worker
        self.repeat_count = repeat_count
        self.running = True
    
    def run(self):
        """Execute the relay control cycle."""
        for _ in range(self.repeat_count):
            if not self.running:
                break
            
            print("Turning all relays ON")
            self.relay_control.turn_on.emit()
            time.sleep(10)  # Wait 10 seconds
            
            if not self.running:
                break
            
            print("Turning all relays OFF")
            self.relay_control.turn_off.emit()
            time.sleep(10)  # Wait 10 seconds
        
        self.finished.emit()
    
    def stop(self):
        """Stop the relay control cycle."""
        self.running = False