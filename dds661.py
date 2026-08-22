
# dds661.py
# High-level library for DDS661 Modbus meter (RTU seriale, Modbus TCP, RTU-over-TCP).
#
# - Valori float32 su 2 registri (ABCD: high word poi low word).
# - Il trasporto e la compatibilita' fra versioni di pymodbus sono in transport.py:
#   qui si descrive solo la mappa registri e la semantica del dispositivo.

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Dict, Any, Callable

from transport import (
    LinkConfig,
    Measure,
    ModbusSession,
    TransportConfig,
    call_read,
    call_write,
    coerce,
    float_to_registers,
    make_client,
    registers_to_float,
)

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
# LinkConfig e' definita in transport.py e ri-esportata qui per compatibilita'.

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
# Alias storici verso transport.py (usati da sdm230.py e dalle app).

_float_to_registers = float_to_registers
_registers_to_float = registers_to_float

def _call_with_unit(func: Callable[..., Any], *, address: int, count: int, unit_id: int):
    return call_read(func, address=address, count=count, unit_id=unit_id)

def _write_with_unit(func: Callable[..., Any], *, address: int, values: list[int], unit_id: int):
    return call_write(func, address=address, values=values, unit_id=unit_id)

# --------------------------------- Client ----------------------------------

class DDS661:
    MANUFACTURER = "DDS"
    MODEL = "DDS661"

    # Grandezze esposte: il poller e la discovery HA leggono queste, non tabelle globali.
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
        """Grandezze per questo dispositivo. Statiche qui; i driver multicanale
        (vedi ds18b20.py, mcm260.py) le derivano dalla config del device."""
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
        return ModbusSession(self.transport, label=f"DDS661 unit={self.unit}")

    # ---- params ----
    def read_params(self) -> Params:
        with self._session() as ses:
            def _r(addr: int) -> float:
                rr = ses.read_registers(addr, 2, self.unit, input_registers=False)
                return registers_to_float((rr.registers[0], rr.registers[1]))
            return Params(
                baud=_r(REG_BAUD),
                parity=_r(REG_PARITY),
                slave=_r(REG_SLAVE),
            )

    def write_params(self, baud: Optional[float] = None,
                     parity: Optional[float] = None,
                     slave: Optional[float] = None) -> Dict[str, str]:
        cur = self.read_params()
        plan = [("slave", REG_SLAVE, slave), ("parity", REG_PARITY, parity), ("baud", REG_BAUD, baud)]

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

    # ---- measurements ----
    def read_values(self, dev_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
        """Legge tutte le grandezze dichiarate, in un'unica sessione."""
        with self._session() as ses:
            return {m.key: ses.read_measure(m, self.unit) for m in self.measures(dev_cfg)}

    def read_measurements(self) -> Measurements:
        return Measurements(**self.read_values())
