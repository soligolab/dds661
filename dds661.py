
# dds661.py
# High-level library for DDS661 Modbus RTU meter (pymodbus-version agnostic).
#
# - Uses struct for float32 <-> 2x16bit registers (ABCD: high word then low word).
# - Compatible with pymodbus variants that use either method="rtu" or framer=ModbusRtuFramer.
# - Automatically handles slave/unit kwarg differences in read/write calls.

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Dict, Tuple, Any, Callable
import struct

from pymodbus.client import ModbusSerialClient
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
    b = struct.pack('>f', float(value))  # big-endian float32: b0 b1 b2 b3
    hi = (b[0] << 8) | b[1]
    lo = (b[2] << 8) | b[3]
    return hi, lo

def _registers_to_float(regs: Tuple[int, int]) -> float:
    hi, lo = int(regs[0]) & 0xFFFF, int(regs[1]) & 0xFFFF
    b = bytes([(hi >> 8) & 0xFF, hi & 0xFF, (lo >> 8) & 0xFF, lo & 0xFF])
    return struct.unpack('>f', b)[0]

# ---------------------- pymodbus compat (slave/unit) -----------------------

def _call_with_unit(func: Callable[..., Any], *, address: int, count: int, unit_id: int):
    try:
        return func(address=address, count=count, slave=unit_id)
    except TypeError:
        return func(address=address, count=count, unit=unit_id)

def _write_with_unit(func: Callable[..., Any], *, address: int, values: list[int], unit_id: int):
    try:
        return func(address=address, values=values, slave=unit_id)
    except TypeError:
        return func(address=address, values=values, unit=unit_id)

# --------------------------------- Client ----------------------------------

class DDS661:
    def __init__(self, link: LinkConfig, unit: int = 1):
        self.link = link
        self.unit = int(unit)

    def _make_client(self) -> ModbusSerialClient:
        kwargs = dict(
            port=self.link.port,
            baudrate=self.link.baudrate,
            parity=self.link.parity,
            stopbits=self.link.stopbits,
            bytesize=self.link.bytesize,
            timeout=self.link.timeout,
        )
        # Prefer new-style framer if available
        RTUFramer = None
        try:
            from pymodbus.framer.rtu_framer import ModbusRtuFramer as RTUFramer  # newer
        except Exception:
            try:
                from pymodbus.framer.rtu import ModbusRtuFramer as RTUFramer      # older 3.x
            except Exception:
                RTUFramer = None
        # Try modern constructor
        if RTUFramer is not None:
            try:
                return ModbusSerialClient(framer=RTUFramer, **kwargs)
            except TypeError:
                pass
        # Fallback to legacy method="rtu"
        try:
            return ModbusSerialClient(method="rtu", **kwargs)
        except TypeError:
            # Last resort: no framer/method argument accepted
            return ModbusSerialClient(**kwargs)

    # ---- params ----
    def read_params(self) -> Params:
        cli = self._make_client()
        if not cli.connect():
            raise RuntimeError("Impossibile aprire la porta seriale")
        try:
            def _r(addr: int) -> float:
                rr = _call_with_unit(cli.read_holding_registers, address=addr, count=2, unit_id=self.unit)
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
                rq = _write_with_unit(cli.write_registers, address=addr, values=[hi, lo], unit_id=self.unit)
                if rq.isError():
                    report[name] = f"ERROR: {rq}"
                else:
                    report[name] = f"written ({desired})"
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
                rr = _call_with_unit(cli.read_input_registers, address=addr, count=2, unit_id=self.unit)
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
