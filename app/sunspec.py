from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

REG_BLOCK_START = 0x9C40
REG_COMMON_MODEL_ID = 0x9C42
REG_COMMON_MODEL_LENGTH = 0x9C43
REG_COMMON_DA = 0x9C84
REG_MODEL_ID = 0x9C85
REG_MODEL_LENGTH = 0x9C86
REG_MODEL_START = 0x9C87
REG_MODEL_END = 0x9CEF
REG_END_BLOCK_ID = 0x9CF0
REG_END_BLOCK_LENGTH = 0x9CF1

SUNSPEC_MODEL_AC_METER_INT_SF = 203
SUNSPEC_MODEL_LENGTH_INT_SF = 105

REG_A_SF = 0x9C8B
REG_V_SF = 0x9C94
REG_HZ_SF = 0x9C96
REG_W_SF = 0x9C9B
REG_VA_SF = 0x9CA0
REG_VAR_SF = 0x9CA5
REG_PF_SF = 0x9CAA
REG_TOTWH_SF = 0x9CBB
REG_TOTVAH_SF = 0x9CD0
REG_TOTVARH_SF = 0x9CEF

FIXED_A_SF = -2
FIXED_V_SF = -2
FIXED_HZ_SF = -2
FIXED_W_SF = 0
FIXED_VA_SF = 0
FIXED_VAR_SF = 0
FIXED_PF_SF = -3
FIXED_TOTWH_SF = 0
FIXED_TOTVAH_SF = 0
FIXED_TOTVARH_SF = 0


class ValueGroup(str, Enum):
    A = "A"
    V = "V"
    HZ = "HZ"
    W = "W"
    VA = "VA"
    VAR = "VAR"
    PF = "PF"
    WH = "WH"


class Encoding(str, Enum):
    S16 = "S16"
    ACC32 = "ACC32"


@dataclass(frozen=True)
class RegisterSpec:
    logical_key: str
    register: int
    group: ValueGroup
    encoding: Encoding


REGISTER_SPECS: tuple[RegisterSpec, ...] = (
    RegisterSpec("current_total", 0x9C87, ValueGroup.A, Encoding.S16),
    RegisterSpec("current_l1", 0x9C88, ValueGroup.A, Encoding.S16),
    RegisterSpec("current_l2", 0x9C89, ValueGroup.A, Encoding.S16),
    RegisterSpec("current_l3", 0x9C8A, ValueGroup.A, Encoding.S16),
    RegisterSpec("voltage_l1_n", 0x9C8D, ValueGroup.V, Encoding.S16),
    RegisterSpec("voltage_l2_n", 0x9C8E, ValueGroup.V, Encoding.S16),
    RegisterSpec("voltage_l3_n", 0x9C8F, ValueGroup.V, Encoding.S16),
    RegisterSpec("frequency", 0x9C95, ValueGroup.HZ, Encoding.S16),
    RegisterSpec("power_total", 0x9C97, ValueGroup.W, Encoding.S16),
    RegisterSpec("power_l1", 0x9C98, ValueGroup.W, Encoding.S16),
    RegisterSpec("power_l2", 0x9C99, ValueGroup.W, Encoding.S16),
    RegisterSpec("power_l3", 0x9C9A, ValueGroup.W, Encoding.S16),
    RegisterSpec("apparent_total", 0x9C9C, ValueGroup.VA, Encoding.S16),
    RegisterSpec("apparent_l1", 0x9C9D, ValueGroup.VA, Encoding.S16),
    RegisterSpec("apparent_l2", 0x9C9E, ValueGroup.VA, Encoding.S16),
    RegisterSpec("apparent_l3", 0x9C9F, ValueGroup.VA, Encoding.S16),
    RegisterSpec("reactive_total", 0x9CA1, ValueGroup.VAR, Encoding.S16),
    RegisterSpec("reactive_l1", 0x9CA2, ValueGroup.VAR, Encoding.S16),
    RegisterSpec("reactive_l2", 0x9CA3, ValueGroup.VAR, Encoding.S16),
    RegisterSpec("reactive_l3", 0x9CA4, ValueGroup.VAR, Encoding.S16),
    RegisterSpec("pf_total", 0x9CA6, ValueGroup.PF, Encoding.S16),
    RegisterSpec("pf_l1", 0x9CA7, ValueGroup.PF, Encoding.S16),
    RegisterSpec("pf_l2", 0x9CA8, ValueGroup.PF, Encoding.S16),
    RegisterSpec("pf_l3", 0x9CA9, ValueGroup.PF, Encoding.S16),
    RegisterSpec("energy_export_total", 0x9CAB, ValueGroup.WH, Encoding.ACC32),
    RegisterSpec("energy_import_total", 0x9CB3, ValueGroup.WH, Encoding.ACC32),
)

REGISTER_BY_KEY = {spec.logical_key: spec for spec in REGISTER_SPECS}


def value_group_scale_factor(group: ValueGroup) -> int:
    return {
        ValueGroup.A: FIXED_A_SF,
        ValueGroup.V: FIXED_V_SF,
        ValueGroup.HZ: FIXED_HZ_SF,
        ValueGroup.W: FIXED_W_SF,
        ValueGroup.VA: FIXED_VA_SF,
        ValueGroup.VAR: FIXED_VAR_SF,
        ValueGroup.PF: FIXED_PF_SF,
        ValueGroup.WH: FIXED_TOTWH_SF,
    }[group]


def preferred_decimals_for_group(group: ValueGroup) -> int:
    if group in {ValueGroup.W, ValueGroup.VA, ValueGroup.VAR}:
        return 2
    return 0


def choose_group_scale_factor(group: ValueGroup, values: Iterable[float | None]) -> int:
    preferred_decimals = preferred_decimals_for_group(group)
    if preferred_decimals <= 0:
        return value_group_scale_factor(group)

    actual = [float(v) for v in values if v is not None]
    if not actual:
        return -preferred_decimals

    for sf in range(-preferred_decimals, 4):
        if all(-32768 <= round(v * (10 ** (-sf))) <= 32767 for v in actual):
            return sf
    return 3


def clamp_s16(value: float) -> int:
    rounded = int(round(value))
    if rounded < -32768:
        return -32768
    if rounded > 32767:
        return 32767
    return rounded


def encode_s16(raw_value: float, sf: int) -> int:
    scaled = raw_value * (10 ** (-sf))
    return clamp_s16(scaled) & 0xFFFF


def encode_acc32(raw_value: float, sf: int) -> tuple[int, int]:
    scaled = raw_value * (10 ** (-sf))
    if scaled < 0:
        scaled = 0
    if scaled > 0xFFFFFFFF:
        scaled = 0xFFFFFFFF
    encoded = int(round(scaled))
    return (encoded >> 16) & 0xFFFF, encoded & 0xFFFF


def pack_ascii_to_registers(text: str, byte_len: int) -> list[int]:
    encoded = text.encode("ascii", errors="ignore")[:byte_len]
    encoded = encoded.ljust(byte_len, b"\x00")
    regs: list[int] = []
    for idx in range(0, len(encoded), 2):
        hi = encoded[idx]
        lo = encoded[idx + 1] if idx + 1 < len(encoded) else 0
        regs.append((hi << 8) | lo)
    return regs


def all_scale_factor_registers() -> Iterable[tuple[int, int]]:
    return (
        (REG_A_SF, FIXED_A_SF & 0xFFFF),
        (REG_V_SF, FIXED_V_SF & 0xFFFF),
        (REG_HZ_SF, FIXED_HZ_SF & 0xFFFF),
        (REG_W_SF, FIXED_W_SF & 0xFFFF),
        (REG_VA_SF, FIXED_VA_SF & 0xFFFF),
        (REG_VAR_SF, FIXED_VAR_SF & 0xFFFF),
        (REG_PF_SF, FIXED_PF_SF & 0xFFFF),
        (REG_TOTWH_SF, FIXED_TOTWH_SF & 0xFFFF),
        (REG_TOTVAH_SF, FIXED_TOTVAH_SF & 0xFFFF),
        (REG_TOTVARH_SF, FIXED_TOTVARH_SF & 0xFFFF),
    )
