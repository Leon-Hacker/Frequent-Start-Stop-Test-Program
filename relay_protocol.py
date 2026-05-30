from __future__ import annotations

from dataclasses import dataclass


FRAME_HEAD = bytes((0x48, 0x3A))
FRAME_TAIL = bytes((0x45, 0x44))
READ_STATUS_COMMAND = 0x53
READ_STATUS_REPLY = 0x54
WRITE_SINGLE_COMMAND = 0x70
WRITE_SINGLE_REPLY = 0x71


class ProtocolError(ValueError):
    """Raised when a relay protocol frame is malformed or unexpected."""


@dataclass(frozen=True)
class RelayStatus:
    relay1: bool
    relay2: bool


def _validate_address(address: int) -> int:
    if not 0 <= address <= 0xFF:
        raise ValueError("Device address must be between 0 and 255.")
    return address


def _checksum15(frame_first_12_bytes: bytes) -> int:
    if len(frame_first_12_bytes) != 12:
        raise ValueError("15-byte frame checksum uses exactly the first 12 bytes.")
    return sum(frame_first_12_bytes) & 0xFF


def format_hex(data: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in data)


def build_read_status_frame(address: int) -> bytes:
    address = _validate_address(address)
    frame = bytearray(15)
    frame[0:2] = FRAME_HEAD
    frame[2] = address
    frame[3] = READ_STATUS_COMMAND
    frame[12] = _checksum15(bytes(frame[:12]))
    frame[13:15] = FRAME_TAIL
    return bytes(frame)


def build_write_single_frame(
    address: int,
    relay_number: int,
    turn_on: bool,
    delay_high: int = 0,
    delay_low: int = 0,
) -> bytes:
    address = _validate_address(address)
    if relay_number not in (1, 2):
        raise ValueError("Relay number must be 1 or 2.")
    if not 0 <= delay_high <= 0xFF or not 0 <= delay_low <= 0xFF:
        raise ValueError("Delay bytes must be between 0 and 255.")

    frame = bytearray(10)
    frame[0:2] = FRAME_HEAD
    frame[2] = address
    frame[3] = WRITE_SINGLE_COMMAND
    frame[4] = relay_number
    frame[5] = 0x01 if turn_on else 0x00
    frame[6] = delay_high
    frame[7] = delay_low
    frame[8:10] = FRAME_TAIL
    return bytes(frame)


def parse_write_single_reply(
    frame: bytes,
    expected_address: int,
    expected_relay_number: int,
    expected_state: bool,
) -> None:
    expected_address = _validate_address(expected_address)
    if expected_relay_number not in (1, 2):
        raise ValueError("Relay number must be 1 or 2.")
    if len(frame) != 10:
        raise ProtocolError(f"Expected a 10-byte write reply, got {len(frame)} bytes.")
    if frame[0:2] != FRAME_HEAD:
        raise ProtocolError("Invalid frame header.")
    if frame[8:10] != FRAME_TAIL:
        raise ProtocolError("Invalid frame tail.")
    if frame[2] != expected_address:
        raise ProtocolError(
            f"Reply address {frame[2]} does not match expected address {expected_address}."
        )
    if frame[3] != WRITE_SINGLE_REPLY:
        raise ProtocolError(
            f"Unexpected write reply command 0x{frame[3]:02X}; "
            f"expected 0x{WRITE_SINGLE_REPLY:02X}."
        )
    if frame[4] != expected_relay_number:
        raise ProtocolError(
            f"Reply relay {frame[4]} does not match expected relay {expected_relay_number}."
        )
    expected_state_value = 0x01 if expected_state else 0x00
    if frame[5] != expected_state_value:
        raise ProtocolError(
            f"Reply state 0x{frame[5]:02X} does not match expected state 0x{expected_state_value:02X}."
        )
    if frame[6] != 0x00 or frame[7] != 0x00:
        raise ProtocolError(
            f"Unexpected write reply delay bytes: 0x{frame[6]:02X} 0x{frame[7]:02X}."
        )


def parse_status_reply(frame: bytes, expected_address: int) -> RelayStatus:
    expected_address = _validate_address(expected_address)
    if len(frame) != 15:
        raise ProtocolError(f"Expected a 15-byte status reply, got {len(frame)} bytes.")
    if frame[0:2] != FRAME_HEAD:
        raise ProtocolError("Invalid frame header.")
    if frame[13:15] != FRAME_TAIL:
        raise ProtocolError("Invalid frame tail.")
    if frame[2] != expected_address:
        raise ProtocolError(
            f"Reply address {frame[2]} does not match expected address {expected_address}."
        )
    if frame[3] != READ_STATUS_REPLY:
        raise ProtocolError(f"Unexpected reply command 0x{frame[3]:02X}.")
    expected_checksum = _checksum15(frame[:12])
    if frame[12] != expected_checksum:
        raise ProtocolError(
            f"Checksum mismatch: got 0x{frame[12]:02X}, expected 0x{expected_checksum:02X}."
        )
    if frame[4] not in (0, 1) or frame[5] not in (0, 1):
        raise ProtocolError(
            f"Unknown relay state values: relay1=0x{frame[4]:02X}, relay2=0x{frame[5]:02X}."
        )
    return RelayStatus(relay1=bool(frame[4]), relay2=bool(frame[5]))


def make_status_reply_for_test(address: int, relay1: bool, relay2: bool) -> bytes:
    address = _validate_address(address)
    frame = bytearray(15)
    frame[0:2] = FRAME_HEAD
    frame[2] = address
    frame[3] = READ_STATUS_REPLY
    frame[4] = 0x01 if relay1 else 0x00
    frame[5] = 0x01 if relay2 else 0x00
    frame[12] = _checksum15(bytes(frame[:12]))
    frame[13:15] = FRAME_TAIL
    return bytes(frame)
