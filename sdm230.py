# sdm230.py
# High-level library for Eastron SDM230-Modbus single-phase meter.
# Inspired by dds661.py interface for symmetry.
#
# Notes:
# - SDM230 uses IEEE754 float32 values across 2 registers (big-endian, MSW first).
# - Input registers (0x) for measurements via function 0x04.
# - Holding registers (4x) for params via function 0x03/0x10.
# - Baud register stores an enumerated code; we expose it as the ACTUAL baud rate.
# - Trasporto (RTU seriale, Modbus TCP, RTU-over-TCP) delegato a transport.py.
#
# Mapping references: "SDM230-Modbus Protocol V1.2" (addresses are the start address hex).

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, Optional

from transport import (
    LinkConfig,
    Measure,
    ModbusSession,
    TransportConfig,
    coerce,
    float_to_registers,
    make_client,
    registers_to_float,
)

# ----------------------- Input Register Map (addresses) ----------------------
# Each address is the start of a float32 value: 2x16-bit registers (MSW first).
IN_VOLTAGE = 0x0000  # L-N Volts
IN_CURRENT = 0x0006  # Amps
IN_P_ACT   = 0x000C  # Watts (active power)
IN_PF      = 0x001E  # Power Factor
IN_FREQ    = 0x0046  # Hz

IN_E_POS   = 0x0048  # Import active energy (kWh)
IN_E_REV   = 0x004A  # Export active energy (kWh)
IN_E_TOT   = 0x0156  # Total active energy (kWh)

# ----------------------- Holding (4x) Register Map ---------------------------
REG_PARITY = 0x0012  # float-coded parity/stopbits mode (see mapping below)
REG_SLAVE  = 0x0014  # float-coded Modbus node (1..247)
REG_BAUD   = 0x001C  # float-coded baud rate (enum)

# ---------------------------- Dataclasses ------------------------------------

@dataclass
class Params:
    baud: float    # actual numeric baud (e.g., 9600.0)
    parity: float  # code as in device (0,1,2,3) — see PARITY_CODES
    slave: float   # 1..247

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

# ---------------------------- Mappings ---------------------------------------

# Baud code <-> actual baud
_BAUD_FROM_CODE = {
    0.0: 2400.0,
    1.0: 4800.0,
    2.0: 9600.0,
    5.0: 1200.0,
}
_BAUD_TO_CODE = {v: k for k, v in _BAUD_FROM_CODE.items()}

# Parity codes for SDM230 (float-coded in register):
# 0 = 1 stop, NO parity
# 1 = 1 stop, EVEN parity
# 2 = 1 stop, ODD parity
# 3 = 2 stop, NO parity
PARITY_CODES = {
    0.0: "N,1 stop",
    1.0: "E,1 stop",
    2.0: "O,1 stop",
    3.0: "N,2 stop",
}

# --------------------------- Client Helper -----------------------------------

class SDM230:
    MANUFACTURER = "Eastron"
    MODEL = "SDM230"

    MEASURES = (
        Measure("voltage",  IN_VOLTAGE, "Voltage",       "V",   "voltage"),
        Measure("current",  IN_CURRENT, "Current",       "A",   "current"),
        Measure("p_active", IN_P_ACT,   "Active Power",  "W",   "power"),
        Measure("pf",       IN_PF,      "Power Factor"),
        Measure("freq",     IN_FREQ,    "Frequency",     "Hz",  "frequency"),
        Measure("e_total",  IN_E_TOT,   "Energy Total",  "kWh", "energy"),
        Measure("e_pos",    IN_E_POS,   "Energy Import", "kWh", "energy"),
        Measure("e_rev",    IN_E_REV,   "Energy Export", "kWh", "energy"),
    )

    @classmethod
    def measures(cls, dev_cfg: Optional[Dict[str, Any]] = None) -> tuple:
        return cls.MEASURES

    def __init__(self, transport: Any, unit: int = 1, dev_cfg: Optional[Dict[str, Any]] = None):
        # Accetta un TransportConfig, un LinkConfig (=> RTU) o un dict.
        self.transport = coerce(transport)
        self.unit = int(unit)
        self.dev_cfg = dev_cfg or {}

    @property
    def link(self) -> TransportConfig:
        """Compatibilita': il vecchio attributo .link esponeva i parametri di trasporto."""
        return self.transport

    def _make_client(self):
        return make_client(self.transport)

    def _session(self) -> ModbusSession:
        return ModbusSession(self.transport, label=f"SDM230 unit={self.unit}")

    # ------------------------- Params ----------------------------------------

    def read_params(self) -> Params:
        with self._session() as ses:
            def _r(addr: int) -> float:
                rr = ses.read_registers(addr, 2, self.unit, input_registers=False)
                return registers_to_float((rr.registers[0], rr.registers[1]))
            baud_code = _r(REG_BAUD)
            baud = _BAUD_FROM_CODE.get(float(baud_code), baud_code)  # fall back to code if unknown
            parity = _r(REG_PARITY)
            slave = _r(REG_SLAVE)
            return Params(baud=float(baud), parity=float(parity), slave=float(slave))

    def write_params(self, baud: Optional[float] = None,
                     parity: Optional[float] = None,
                     slave: Optional[float] = None) -> Dict[str, str]:
        cur = self.read_params()
        # Map baud (actual) to device code if user passes a standard speed
        def _desired(name: str, val: Optional[float]) -> Optional[float]:
            if val is None:
                return None
            if name == "baud":
                # If exact mapping known, write code; else write raw (device will likely reject non-enum)
                code = _BAUD_TO_CODE.get(float(val))
                return float(code) if code is not None else float(val)
            return float(val)

        plan = [("slave", REG_SLAVE, _desired("slave", slave)),
                ("parity", REG_PARITY, _desired("parity", parity)),
                ("baud", REG_BAUD, _desired("baud", baud))]

        report: Dict[str, str] = {}
        with self._session() as ses:
            for name, addr, desired in plan:
                if desired is None:
                    report[name] = "skipped (None)"
                    continue
                cur_val = getattr(cur, name)
                if abs(cur_val - float(desired)) < 1e-6:
                    report[name] = f"unchanged ({desired})"
                    continue
                hi, lo = float_to_registers(float(desired))
                rq = ses.write_registers(addr, [hi, lo], self.unit)
                if rq.isError():
                    report[name] = f"ERROR: {rq}"
                else:
                    report[name] = f"written ({desired})"
                    if name == "slave":
                        self.unit = int(desired)
        return report

    # ------------------------ Measurements -----------------------------------

    def read_values(self, dev_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
        """Legge tutte le grandezze dichiarate, in un'unica sessione."""
        with self._session() as ses:
            return {m.key: ses.read_measure(m, self.unit) for m in self.measures(dev_cfg)}

    def read_measurements(self) -> Measurements:
        return Measurements(**self.read_values())
