# ds18b20.py
# Driver per il lettore Modbus RS485 a 12 canali per sonde DS18B20
# (venduto da TONIFISHI come "DS18B20-RS485").
#
# MAPPA RICAVATA DAL DISPOSITIVO REALE, non da un datasheet: le fonti pubbliche
# descrivono il modulo R4DCB08 (8 canali, holding 0x0000-0x000F, int16 x10,
# 0x8000 = sonda assente), che ha un firmware DIVERSO da questo. Qui:
#
#   INPUT (FC04)
#     0x0000-0x000B   temperature CH1..CH12   int16 x0.01 gradi C
#     0x000C          maschera presenza: bit n a 1 => canale n+1 ASSENTE
#   HOLDING (FC03)
#     0x0000          configurazione: byte alto = baud/600, byte basso = indirizzo
#     0x0001-0x0008   altri parametri, significato non identificato
#
# Verifiche che sostengono questa lettura:
# - holding 0x0000 = 0x600A su un modulo noto essere a 57600 baud, indirizzo 10:
#   0x0A = 10 e 0x60 = 96, con 96 x 600 = 57600.
# - input 0x000C = 0x0FFC con due sole sonde collegate sui canali 1 e 2:
#   i due bit bassi a zero, i dieci alti a uno.
# - scala 0.01 confermata dai valori letti (29,12 e 28,00 gradi) e dalla
#   trasformazione divide100 usata nella precedente configurazione openHAB.
#
# Un canale senza sonda legge 0, che sarebbe indistinguibile da 0,00 gradi:
# per questo la maschera di presenza NON e' opzionale e i canali assenti
# diventano NaN.

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from transport import Measure, ModbusSession, TransportConfig, coerce, decode, make_client

# ------------------------------ Register map ---------------------------------
REG_TEMP_BASE = 0x0000    # input, 12 registri consecutivi
REG_STATUS    = 0x000C    # input, maschera di presenza
REG_CONFIG    = 0x0000    # holding, (baud/600) << 8 | indirizzo
CONFIG_REGS   = 9         # holding 0x0000-0x0008: tutto cio' che esiste

CHANNELS   = 12
TEMP_SCALE = 0.01
BAUD_UNIT  = 600          # il byte alto di REG_CONFIG e' baud/600

BLOCK_LEN = CHANNELS + 1  # temperature + maschera, in una sola transazione


@dataclass
class Params:
    address: Optional[int] = None      # byte basso di holding 0x0000
    baud: Optional[int] = None         # byte alto x 600
    baud_code: Optional[int] = None    # byte alto grezzo
    present: List[int] = field(default_factory=list)   # canali con sonda collegata
    raw_config: List[int] = field(default_factory=list)  # holding 0x0000-0x0008, non decodificati


class DS18B20RS485:
    MANUFACTURER = "TONIFISHI"
    MODEL = "DS18B20-RS485"

    @classmethod
    def channel_labels(cls, dev_cfg: Optional[Dict[str, Any]] = None) -> Dict[int, str]:
        """Canali da pubblicare, con la loro etichetta.

        'channels:' accetta una mappa {numero: nome} o una lista di numeri; senza
        indicazioni si pubblicano tutti e 12 i canali.
        """
        raw = (dev_cfg or {}).get("channels")
        if not raw:
            return {n: f"Temperature {n}" for n in range(1, CHANNELS + 1)}
        items = raw.items() if isinstance(raw, dict) else ((n, None) for n in raw)
        out: Dict[int, str] = {}
        for num, label in items:
            n = int(num)
            if not 1 <= n <= CHANNELS:
                raise ValueError(f"canale {n} fuori range: il modulo ne ha {CHANNELS}")
            out[n] = str(label) if label else f"Temperature {n}"
        return dict(sorted(out.items()))

    @classmethod
    def measures(cls, dev_cfg: Optional[Dict[str, Any]] = None) -> tuple:
        return tuple(
            Measure(key=f"t{n}", address=REG_TEMP_BASE + (n - 1), label=label,
                    unit="°C", device_class="temperature", state_class="measurement",
                    codec="int16", scale=TEMP_SCALE, input_registers=True)
            for n, label in cls.channel_labels(dev_cfg).items()
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
        return ModbusSession(self.transport, label=f"DS18B20-RS485 unit={self.unit}")

    # ---- presenza sonde ----

    @staticmethod
    def _absent(mask: int) -> set:
        """Canali senza sonda, dal registro di stato (bit a 1 = assente)."""
        return {n for n in range(1, CHANNELS + 1) if (int(mask) >> (n - 1)) & 1}

    def postprocess(self, values: Dict[str, Any], ses: ModbusSession, unit_id: int) -> Dict[str, Any]:
        """Marca NaN i canali senza sonda.

        Necessario perche' un canale scollegato legge 0: senza la maschera
        pubblicheremmo uno 0,00 gradi indistinguibile da una misura vera.
        """
        try:
            mask = ses.read_block(REG_STATUS, 1, unit_id, input_registers=True)[0]
        except Exception:
            return values
        absent = self._absent(mask)
        return {k: (float("nan") if k.startswith("t") and k[1:].isdigit()
                    and int(k[1:]) in absent else v)
                for k, v in values.items()}

    # ---- misure ----

    def read_values(self, dev_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
        """Temperature e maschera di presenza in UNA transazione (13 registri contigui)."""
        measures = self.measures(dev_cfg if dev_cfg is not None else self.dev_cfg)
        with self._session() as ses:
            try:
                regs = ses.read_block(REG_TEMP_BASE, BLOCK_LEN, self.unit, input_registers=True)
            except Exception:
                vals = {m.key: ses.read_measure(m, self.unit) for m in measures}
                return self.postprocess(vals, ses, self.unit)
        absent = self._absent(regs[REG_STATUS - REG_TEMP_BASE])
        return {m.key: (float("nan") if int(m.key[1:]) in absent
                        else decode(m, [regs[m.address - REG_TEMP_BASE]]))
                for m in measures}

    def read_measurements(self) -> Dict[str, float]:
        return self.read_values()

    # ---- parametri ----

    def read_params(self) -> Params:
        p = Params()
        with self._session() as ses:
            try:
                cfg = ses.read_block(REG_CONFIG, CONFIG_REGS, self.unit, input_registers=False)
                p.raw_config = [int(r) for r in cfg]
                p.address = cfg[0] & 0xFF
                p.baud_code = (cfg[0] >> 8) & 0xFF
                p.baud = p.baud_code * BAUD_UNIT
            except Exception:
                pass
            try:
                mask = ses.read_block(REG_STATUS, 1, self.unit, input_registers=True)[0]
                absent = self._absent(mask)
                p.present = [n for n in range(1, CHANNELS + 1) if n not in absent]
            except Exception:
                pass
        return p

    def write_params(self, baud: Optional[float] = None,
                     parity: Optional[float] = None,
                     slave: Optional[float] = None) -> Dict[str, str]:
        """Indirizzo e velocita' stanno nello STESSO registro: va riscritto intero,
        preservando il byte che non si sta cambiando."""
        report: Dict[str, str] = {}
        if parity is not None:
            report["parity"] = "non supportato da questo dispositivo (linea fissa N,8,1)"
        if baud is None and slave is None:
            report["config"] = "skipped (None)"
            return report

        cur = self.read_params()
        if cur.address is None:
            report["config"] = "ERROR: registro di configurazione non leggibile, scrittura annullata"
            return report

        code, addr = cur.baud_code, cur.address
        if baud is not None:
            want = int(baud)
            if want % BAUD_UNIT or not 1 <= want // BAUD_UNIT <= 0xFF:
                report["baud"] = (f"ERROR: {want} non rappresentabile: il codice e' baud/{BAUD_UNIT} "
                                  f"e deve stare in un byte (1200..153000, multipli di {BAUD_UNIT})")
                return report
            code = want // BAUD_UNIT
        if slave is not None:
            new = int(slave)
            if not 1 <= new <= 247:
                report["slave"] = f"ERROR: indirizzo {new} fuori range 1..247"
                return report
            addr = new

        with self._session() as ses:
            rq = ses.write_registers(REG_CONFIG, [(code << 8) | addr], self.unit)
        if rq.isError():
            report["config"] = f"ERROR: {rq}"
            return report
        if baud is not None:
            report["baud"] = f"written ({code * BAUD_UNIT} baud, codice {code})"
        if slave is not None:
            report["slave"] = f"written ({addr})"
            self.unit = addr
        report["note"] = ("mappa del registro di configurazione dedotta dal dispositivo, "
                          "non da datasheet: verificare rileggendo i parametri")
        return report
