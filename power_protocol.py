from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


COMMAND_TERMINATOR = "\n"


@dataclass(frozen=True)
class PowerStatus:
    measured_voltage: Optional[float]
    measured_current: Optional[float]
    output_on: Optional[bool]


def build_set_voltage_command(voltage: float) -> str:
    return f"VOLT {voltage:.3f}"


def build_set_current_command(current: float) -> str:
    return f"CURR {current:.3f}"


def build_output_command(output_on: bool) -> str:
    return "OUTP ON" if output_on else "OUTP OFF"


def build_measure_voltage_command() -> str:
    return "MEAS:VOLT?"


def build_measure_current_command() -> str:
    return "MEAS:CURR?"


def build_query_output_command() -> str:
    return "OUTP?"


def encode_command(command: str) -> bytes:
    return (command + COMMAND_TERMINATOR).encode("ascii")


def parse_float_response(response: str, name: str) -> float:
    text = response.strip()
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"{name}返回值无法解析为数字：{response!r}") from exc


def parse_output_state(response: str) -> Optional[bool]:
    value = response.strip().upper()

    if value in {"1", "ON"}:
        return True

    if value in {"0", "OFF"}:
        return False

    return None