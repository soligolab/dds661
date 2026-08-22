# mcm260.py
# Driver per il modulo I/O Pixsys MCM260 su Modbus RTU (esemplare in campo: MCM260-3AD).
# Verificato sul dispositivo reale.
#
# Gli ingressi digitali stanno tutti in un registro holding: 16 bit, un contatto per
# bit. Ogni bit diventa un binary_sensor in Home Assistant.
#
#   HOLDING (FC03)
#     1000    ingressi digitali, bit 0..15 (bit 0 = ingresso 1)
#
# Logica invertita: sui contatti magnetici usati qui il bit a 1 significa contatto
# CHIUSO (finestra chiusa -> binary_sensor "off"). E' il default del driver e si
# cambia con 'invert: false', globalmente o per singolo ingresso.
#
# Mappa verificata sul dispositivo: holding 1000 = 0x0F1F con tutti i contatti
# mappati chiusi. Corrisponde alla configurazione openHAB/Home Assistant preesistente
# (readStart="1000.N", readValueType="bit", OpenCloseInversion).

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from transport import Measure, ModbusSession, TransportConfig, coerce, decode, make_client

REG_INPUTS = 1000    # holding
INPUT_BITS = 16


@dataclass
class Params:
    inputs_raw: Optional[int] = None        # holding 1000, valore grezzo
    inputs_bits: str = ""                   # stessa cosa in binario, per il debug
    raw_config: List[int] = field(default_factory=list)   # holding 0x0000-0x0003, non decodificati


class MCM260:
    MANUFACTURER = "Pixsys"
    MODEL = "MCM260-3AD"

    @classmethod
    def input_specs(cls, dev_cfg: Optional[Dict[str, Any]] = None) -> Dict[int, Dict[str, Any]]:
        """Ingressi da pubblicare, letti da 'inputs:' nel config.

        Ogni voce e' {bit: nome} oppure {bit: {name, device_class, invert}}.
        Senza indicazioni si pubblicano tutti e 16 i bit con nomi generici: sono
        parecchie entita', quindi conviene dichiarare solo quelle cablate.
        """
        raw = (dev_cfg or {}).get("inputs")
        default_invert = bool((dev_cfg or {}).get("invert", True))
        if not raw:
            raw = {b: None for b in range(INPUT_BITS)}

        out: Dict[int, Dict[str, Any]] = {}
        for bit, spec in raw.items():
            b = int(bit)
            if not 0 <= b < INPUT_BITS:
                raise ValueError(f"bit {b} fuori range 0..{INPUT_BITS - 1}")
            if isinstance(spec, dict):
                out[b] = {"name": str(spec.get("name") or f"Input {b}"),
                          "device_class": spec.get("device_class"),
                          "invert": bool(spec.get("invert", default_invert))}
            else:
                out[b] = {"name": str(spec) if spec else f"Input {b}",
                          "device_class": None, "invert": default_invert}
        return dict(sorted(out.items()))

    @classmethod
    def measures(cls, dev_cfg: Optional[Dict[str, Any]] = None) -> tuple:
        return tuple(
            Measure(key=f"di{bit}", address=REG_INPUTS, label=spec["name"],
                    device_class=spec["device_class"], codec="bit", bit=bit,
                    invert=spec["invert"], component="binary_sensor",
                    input_registers=False)
            for bit, spec in cls.input_specs(dev_cfg).items()
        )

    def __init__(self, transport: Any, unit: int = 1, dev_cfg: Optional[Dict[str, Any]] = None):
        self.transport = coerce(transport)
        self.unit = int(unit)
        self.dev_cfg = dev_cfg or {}

    @property
    def link(self) -> TransportConfig:
        return self.transport

    def _make_client(self):
        return make_client(self.transport)

    def _session(self) -> ModbusSession:
        return ModbusSession(self.transport, label=f"MCM260 unit={self.unit}")

    # ---- misure ----

    def read_values(self, dev_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Tutti gli ingressi con UNA lettura: stanno nello stesso registro."""
        measures = self.measures(dev_cfg if dev_cfg is not None else self.dev_cfg)
        with self._session() as ses:
            regs = ses.read_block(REG_INPUTS, 1, self.unit, input_registers=False)
        return {m.key: decode(m, regs) for m in measures}

    def read_measurements(self) -> Dict[str, Any]:
        return self.read_values()

    # ---- parametri ----

    def read_params(self) -> Params:
        p = Params()
        with self._session() as ses:
            try:
                p.inputs_raw = ses.read_block(REG_INPUTS, 1, self.unit, input_registers=False)[0]
                p.inputs_bits = format(p.inputs_raw, "016b")
            except Exception:
                pass
            try:
                p.raw_config = [int(r) for r in ses.read_block(0, 4, self.unit, input_registers=False)]
            except Exception:
                pass
        return p

    def write_params(self, baud=None, parity=None, slave=None) -> Dict[str, str]:
        return {"config": "non supportato: la mappa di configurazione dell'MCM260 non e' "
                          "stata verificata su questo dispositivo, si configura dal suo software"}
