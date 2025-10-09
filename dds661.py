
# dds661.py
# High-level library for DDS661 Modbus RTU meter.
#
# - Encodes/decodes IEEE-754 float32 across two 16-bit Modbus registers
# - Word/byte order: High word -> Low word (ABCD) i.e. Endian.BIG for both
#
# Public API:
#   - LinkConfig: serial link configuration (dataclass)
#   - Params: device parameters (dataclass)
#   - Measurements: device measurements (dataclass)
#   - DDS661: class with high-level methods:
#       * read_params() -> Params
#       * write_params(baud=None, parity=None, slave=None) -> dict
#       * read_measurements() -> Measurements
#
# Notes:
#   - Device parity values (holding register 0x0002): 0=Even, 1=Odd, 2=None
#   - Pymodbus >= 3.x is required

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Dict, Tuple

from pymodbus.client import ModbusSerialClient
from pymodbus.constants import Endian
from pymodbus.payload import BinaryPayloadBuilder, BinaryPayloadDecoder
from pymodbus.exceptions import ModbusException

# ---------------- Register map (addresses are the High Word) ---------------
REG_BAUD   = 0x0000  # float32 (2 regs)
REG_PARITY = 0x0002  # float32 (0=Even, 1=Odd, 2=None)
REG_SLAVE  = 0x0008  # float32 (1..247)

IN_VOLTAGE = 0x0000  # float32
IN_CURRENT = 0x0008  # float32
IN_P_ACT   = 0x0012  # float32
IN_PF      = 0x002A  # float32
IN_FREQ    = 0x0036  # float32
IN_E_TOT   = 0x0100  # float32
IN_E_POS   = 0x0102  # float32
IN_E_REV   = 0x0103  # float32

WORD_ORDER = Endian.BIG   # High word first (ABCD)
BYTE_ORDER = Endian.BIG   # Big-endian inside each word

# ------------------------------- Dataclasses -------------------------------

@dataclass
class LinkConfig:
    port: str = "/dev/ttyCOM1"
    baudrate: int = 9600
    parity: str = "E"      # 'E' (Even), 'O', 'N'
    stopbits: int = 1
    bytesize: int = 8
    timeout: float = 1.0

@dataclass
class Params:
    baud: float
    parity: float
    slave: float

@dataclass
class Measurements:
    voltage: float
    current: float
    p_active: float
    pf: float
    freq: float
    e_total: float
    e_pos: float
    e_rev: float

# -------------------------- Float <-> Registers ----------------------------

def _float_to_registers(value: float) -> Tuple[int, int]:
    b = BinaryPayloadBuilder(byteorder=BYTE_ORDER, wordorder=WORD_ORDER)
    b.add_32bit_float(float(value))
    r = b.to_registers()
    return r[0], r[1]

def _registers_to_float(regs: Tuple[int, int]) -> float:
    dec = BinaryPayloadDecoder.fromRegisters(list(regs), byteorder=BYTE_ORDER, wordorder=WORD_ORDER)
    return dec.decode_32bit_float()

# --------------------------------- Client ----------------------------------

class DDS661:
    """
    High-level client for the DDS661 meter.
    Opens and closes the serial client on each public call to keep usage simple.
    """
    def __init__(self, link: LinkConfig, unit: int = 1):
        self.link = link
        self.unit = int(unit)

    # ---- internals ----
    def _make_client(self) -> ModbusSerialClient:
        return ModbusSerialClient(
            method="rtu",
            port=self.link.port,
            baudrate=self.link.baudrate,
            parity=self.link.parity,
            stopbits=self.link.stopbits,
            bytesize=self.link.bytesize,
            timeout=self.link.timeout,
        )

    # ---- params ----
    def read_params(self) -> Params:
        cli = self._make_client()
        if not cli.connect():
            raise RuntimeError("Impossibile aprire la porta seriale")
        try:
            def _r(addr: int) -> float:
                rr = cli.read_holding_registers(address=addr, count=2, slave=self.unit)
                if rr.isError():
                    raise ModbusException(rr)
                return _registers_to_float((rr.registers[0], rr.registers[1]))
            return Params(
                baud=_r(REG_BAUD),
                parity=_r(REG_PARITY),
                slave=_r(REG_SLAVE),
            )
        finally:
            cli.close()

    def write_params(self, baud: Optional[float] = None,
                     parity: Optional[float] = None,
                     slave: Optional[float] = None) -> Dict[str, str]:
        """
        Selectively write parameters that are not None and differ from current.
        Write order: slave -> parity -> baud
        """
        cur = self.read_params()
        plan = [("slave", REG_SLAVE, slave), ("parity", REG_PARITY, parity), ("baud", REG_BAUD, baud)]

        cli = self._make_client()
        if not cli.connect():
            raise RuntimeError("Impossibile aprire la porta seriale")

        report: Dict[str, str] = {}
        try:
            for name, addr, desired in plan:
                if desired is None:
                    report[name] = "skipped (None)"
                    continue
                cur_val = getattr(cur, name)
                if abs(cur_val - float(desired)) < 1e-6:
                    report[name] = f"unchanged ({desired})"
                    continue
                hi, lo = _float_to_registers(float(desired))
                rq = cli.write_registers(address=addr, values=[hi, lo], slave=self.unit)
                if rq.isError():
                    report[name] = f"ERROR: {rq}"
                else:
                    report[name] = f"written ({desired})"
                    # update local state if we changed the unit id
                    if name == "slave":
                        self.unit = int(desired)
            return report
        finally:
            cli.close()

    # ---- measurements ----
    def read_measurements(self) -> Measurements:
        cli = self._make_client()
        if not cli.connect():
            raise RuntimeError("Impossibile aprire la porta seriale")
        try:
            def _rin(addr: int) -> float:
                rr = cli.read_input_registers(address=addr, count=2, slave=self.unit)
                if rr.isError():
                    return float("nan")
                return _registers_to_float((rr.registers[0], rr.registers[1]))
            return Measurements(
                voltage=_rin(IN_VOLTAGE),
                current=_rin(IN_CURRENT),
                p_active=_rin(IN_P_ACT),
                pf=_rin(IN_PF),
                freq=_rin(IN_FREQ),
                e_total=_rin(IN_E_TOT),
                e_pos=_rin(IN_E_POS),
                e_rev=_rin(IN_E_REV),
            )
        finally:
            cli.close()
