# transport.py
# Trasporto Modbus unificato: unico punto di costruzione dei client pymodbus.
#
# Protocolli supportati:
#   rtu     -> ModbusSerialClient, framer RTU        (seriale locale)
#   tcp     -> ModbusTcpClient,    framer SOCKET     (Modbus TCP vero, header MBAP:
#                                                     CNV520-21AD, USR-DR302 con
#                                                     "Modbus TCP to Modbus RTU" attivo)
#   rtutcp  -> ModbusTcpClient,    framer RTU        (trame RTU con CRC16 dentro il socket:
#                                                     USR-DR302 in modalita' trasparente)
#
# Il modulo incapsula anche le differenze di API fra le versioni di pymodbus:
# il kwarg dell'unit id e' passato da 'unit' (2.x) a 'slave' (3.0-3.8) a 'device_id' (3.9+).

from __future__ import annotations

import inspect
import logging
import struct
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Optional, Tuple

from pymodbus.client import ModbusSerialClient, ModbusTcpClient

try:
    from pymodbus.framer import FramerType  # pymodbus >= 3.7
except Exception:  # pragma: no cover - solo su pymodbus vecchi
    FramerType = None  # type: ignore

log = logging.getLogger("meters.transport")

PROTOCOLS = ("rtu", "tcp", "rtutcp")
TCP_PROTOCOLS = ("tcp", "rtutcp")

# --------------------------- Float <-> Registri -----------------------------
# float32 IEEE754 su 2 registri, big-endian, MSW per prima (ABCD).

def float_to_registers(value: float) -> Tuple[int, int]:
    b = struct.pack('>f', float(value))
    return (b[0] << 8) | b[1], (b[2] << 8) | b[3]

def registers_to_float(regs: Tuple[int, int]) -> float:
    hi, lo = int(regs[0]) & 0xFFFF, int(regs[1]) & 0xFFFF
    b = bytes([(hi >> 8) & 0xFF, hi & 0xFF, (lo >> 8) & 0xFF, lo & 0xFF])
    return struct.unpack('>f', b)[0]

# --------------------------- Int16 <-> Registri -----------------------------
# Intero con segno su 1 registro, complemento a due, con fattore di scala:
# usato dai sensori di temperatura (0,1 gradi per unita').

def int16_to_register(value: float, scale: float = 1.0) -> int:
    raw = int(round(float(value) / (scale or 1.0)))
    if not -32768 <= raw <= 32767:
        raise ValueError(f"valore {value} fuori dal range int16 con scala {scale}")
    return raw & 0xFFFF

def register_to_int16(reg: int, scale: float = 1.0, nan_raw: Optional[int] = None) -> float:
    raw = int(reg) & 0xFFFF
    if nan_raw is not None and raw == (int(nan_raw) & 0xFFFF):
        return float("nan")      # sentinella "sensore assente/guasto"
    if raw >= 0x8000:
        raw -= 0x10000           # complemento a due
    return raw * (scale if scale else 1.0)

# ---------------------------- Descrittore misura -----------------------------

@dataclass(frozen=True)
class Measure:
    """Una grandezza leggibile da un dispositivo: dove sta, come si decodifica,
    come si presenta in Home Assistant.

    I driver espongono le proprie Measure: il poller e la discovery HA le seguono,
    invece di conoscere in anticipo un elenco fisso di grandezze.
    """
    key: str                              # chiave nel payload MQTT (es. "voltage", "t1")
    address: int
    label: str = ""                       # etichetta leggibile (es. "Voltage", "Mandata")
    unit: str = ""
    device_class: Optional[str] = None    # device_class Home Assistant
    state_class: Optional[str] = None     # state_class Home Assistant
    codec: str = "float32"                # "float32" (2 reg) | "int16" (1 reg) | "bit" (1 reg)
    scale: float = 1.0                    # valore = grezzo * scale
    input_registers: bool = True          # False -> holding (FC03)
    nan_raw: Optional[int] = None         # grezzo che significa "nessun valore" (es. 0x8000)
    # --- solo per codec "bit": un registro che raccoglie piu' ingressi digitali ---
    bit: Optional[int] = None             # indice del bit, 0 = meno significativo
    invert: bool = False                  # True: 1 sul filo significa "inattivo"
    component: str = "sensor"             # componente Home Assistant: sensor | binary_sensor

    @property
    def count(self) -> int:
        return 2 if self.codec == "float32" else 1

def decode(m: Measure, regs):
    """Decodifica i registri grezzi secondo il codec della misura.

    Ritorna float per le grandezze analogiche, int 0/1 per i bit (che in JSON devono
    restare interi: Home Assistant confronta lo stato con payload_on/payload_off).
    """
    regs = list(regs or [])
    if len(regs) < m.count:
        return float("nan")
    if m.codec == "float32":
        val = registers_to_float((regs[0], regs[1]))
        return val * m.scale if m.scale != 1.0 else val
    if m.codec == "int16":
        return register_to_int16(regs[0], m.scale, m.nan_raw)
    if m.codec == "bit":
        if m.bit is None:
            raise ValueError(f"la misura '{m.key}' usa il codec 'bit' senza indicare quale bit")
        val = (int(regs[0]) >> int(m.bit)) & 1
        return val ^ 1 if m.invert else val
    raise ValueError(f"codec '{m.codec}' non supportato")

# ------------------------------ Config --------------------------------------

@dataclass
class LinkConfig:
    """Parametri della sola linea seriale (compatibilita' storica)."""
    port: str = "/dev/ttyCOM1"
    baudrate: int = 9600
    parity: str = "E"      # 'E' (Even), 'O', 'N'
    stopbits: int = 1
    bytesize: int = 8
    timeout: float = 1.0

@dataclass
class TransportConfig:
    """Descrive come raggiungere un dispositivo Modbus, su qualunque trasporto."""
    protocol: str = "rtu"
    # seriale locale
    port: str = "/dev/ttyCOM1"
    baudrate: int = 9600
    parity: str = "E"
    stopbits: int = 1
    bytesize: int = 8
    # TCP / gateway
    host: str = "192.168.0.99"
    tcp_port: int = 502
    # comune
    timeout: float = 1.0
    retries: int = 3

    @property
    def is_tcp(self) -> bool:
        return self.protocol in TCP_PROTOCOLS

    def key(self) -> tuple:
        """Identita' del bus: due config con la stessa key parlano sullo stesso mezzo."""
        if self.is_tcp:
            return (self.protocol, self.host, self.tcp_port)
        return (self.protocol, self.port, self.baudrate, self.parity, self.stopbits, self.bytesize)

    def describe(self) -> str:
        if self.is_tcp:
            return f"{self.protocol} {self.host}:{self.tcp_port}"
        return f"{self.protocol} {self.port}@{self.baudrate}{self.parity}"

    def as_dict(self) -> Dict[str, Any]:
        """Sottoinsieme rilevante per i log/JSON diagnostici."""
        if self.is_tcp:
            return {"protocol": self.protocol, "host": self.host,
                    "port": self.tcp_port, "timeout": self.timeout}
        return {"protocol": self.protocol, "port": self.port, "baudrate": self.baudrate,
                "parity": self.parity, "stopbits": self.stopbits,
                "bytesize": self.bytesize, "timeout": self.timeout}

def from_link(link: LinkConfig, protocol: str = "rtu") -> TransportConfig:
    return TransportConfig(
        protocol=protocol,
        port=link.port,
        baudrate=int(link.baudrate),
        parity=str(link.parity).upper()[0],
        stopbits=int(link.stopbits),
        bytesize=int(link.bytesize),
        timeout=float(link.timeout),
    )

def coerce(transport: Any, protocol: str = "rtu") -> TransportConfig:
    """Accetta un TransportConfig, un LinkConfig o un dict e restituisce un TransportConfig."""
    if isinstance(transport, TransportConfig):
        return transport
    if isinstance(transport, LinkConfig):
        return from_link(transport, protocol)
    if isinstance(transport, dict):
        return _apply(TransportConfig(protocol=protocol), transport)
    raise TypeError(f"transport non supportato: {type(transport).__name__}")

# --------------------------- Risoluzione config ------------------------------

_SERIAL_KEYS = ("baudrate", "parity", "stopbits", "bytesize")

def _apply(tc: TransportConfig, block: Optional[Dict[str, Any]]) -> TransportConfig:
    """Applica un blocco di config su un TransportConfig, restituendone uno nuovo.

    La chiave 'port' e' ambigua (device seriale vs porta TCP): viene interpretata in base
    al protocollo risultante. 'serial_port' e 'tcp_port' sono sempre espliciti.
    """
    if not isinstance(block, dict) or not block:
        return tc

    upd: Dict[str, Any] = {}

    proto = block.get("protocol")
    if proto:
        proto = str(proto).lower()
        if proto not in PROTOCOLS:
            raise ValueError(f"protocollo '{proto}' non supportato (attesi: {', '.join(PROTOCOLS)})")
        upd["protocol"] = proto
    protocol = upd.get("protocol", tc.protocol)

    for key in _SERIAL_KEYS:
        if key in block:
            upd[key] = str(block[key]).upper()[0] if key == "parity" else int(block[key])
    if "serial_port" in block:
        upd["port"] = str(block["serial_port"])
    if "host" in block:
        upd["host"] = str(block["host"])
    if "tcp_port" in block:
        upd["tcp_port"] = int(block["tcp_port"])
    if "port" in block:
        if protocol in TCP_PROTOCOLS:
            upd["tcp_port"] = int(block["port"])
        else:
            upd["port"] = str(block["port"])
    if "timeout" in block:
        upd["timeout"] = float(block["timeout"])
    if "retries" in block:
        upd["retries"] = int(block["retries"])

    return replace(tc, **upd)

def resolve_transport(cfg: Optional[Dict[str, Any]], dev: Optional[Dict[str, Any]] = None) -> TransportConfig:
    """Risolve il trasporto di un dispositivo a strati (l'ultimo vince):

      1. default
      2. blocco globale 'serial:'          (parametri di linea)
      3. blocco globale 'tcp:'             (host/porta di default)
      4. voce di 'links:' indicata da 'link:' nel device
      5. blocchi 'serial:'/'tcp:' annidati nel device
      6. chiavi piatte nel device (host, port, timeout, baudrate, ...)
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    dev = dev if isinstance(dev, dict) else {}

    link_name = dev.get("link")
    link_block: Dict[str, Any] = {}
    if link_name:
        links = cfg.get("links") or {}
        if not isinstance(links, dict) or link_name not in links:
            raise ValueError(f"link '{link_name}' non trovato nel blocco 'links:' del config")
        link_block = dict(links.get(link_name) or {})

    # Il protocollo va deciso prima di applicare i blocchi, perche' disambigua 'port'.
    protocol = str(dev.get("protocol") or link_block.get("protocol") or "rtu").lower()
    if protocol not in PROTOCOLS:
        raise ValueError(f"protocollo '{protocol}' non supportato (attesi: {', '.join(PROTOCOLS)})")

    tc = TransportConfig(protocol=protocol)
    tc = _apply(tc, _rename_port(cfg.get("serial"), "serial_port"))
    tc = _apply(tc, _rename_port(cfg.get("tcp"), "tcp_port"))
    tc = _apply(tc, link_block)
    tc = _apply(tc, _rename_port(dev.get("serial"), "serial_port"))
    tc = _apply(tc, _rename_port(dev.get("tcp"), "tcp_port"))
    tc = _apply(tc, {k: v for k, v in dev.items() if k in _DEV_FLAT_KEYS})
    return replace(tc, protocol=protocol)

def _rename_port(block: Any, target: str) -> Dict[str, Any]:
    """Nei blocchi 'serial:'/'tcp:' il senso di 'port' e' dato dal blocco, non dal protocollo:
    dentro 'serial:' e' il device (/dev/tty...), dentro 'tcp:' e' la porta TCP."""
    if not isinstance(block, dict):
        return {}
    out = dict(block)
    if "port" in out and target not in out:
        out[target] = out.pop("port")
    return out

_DEV_FLAT_KEYS = ("host", "port", "tcp_port", "serial_port", "timeout", "retries") + _SERIAL_KEYS

# ------------------------------- Client -------------------------------------

def _framer_for(protocol: str):
    if FramerType is None:
        if protocol == "rtutcp":
            raise RuntimeError(
                "pymodbus troppo vecchio: 'rtutcp' richiede pymodbus.framer.FramerType "
                "(pymodbus >= 3.7). Aggiornare pymodbus oppure abilitare la conversione "
                "'Modbus TCP to Modbus RTU' sul gateway e usare protocol: tcp."
            )
        return None
    return FramerType.RTU if protocol in ("rtu", "rtutcp") else FramerType.SOCKET

def make_client(tc: TransportConfig):
    """Costruisce il client pymodbus adatto al trasporto. Non apre la connessione."""
    if tc.protocol not in PROTOCOLS:
        raise ValueError(f"protocollo '{tc.protocol}' non supportato")
    framer = _framer_for(tc.protocol)

    if tc.is_tcp:
        kwargs: Dict[str, Any] = dict(port=int(tc.tcp_port), timeout=float(tc.timeout))
        if framer is not None:
            kwargs["framer"] = framer
        return _construct(ModbusTcpClient, tc.host, kwargs, tc.retries)

    kwargs = dict(
        baudrate=int(tc.baudrate),
        parity=str(tc.parity).upper()[0],
        stopbits=int(tc.stopbits),
        bytesize=int(tc.bytesize),
        timeout=float(tc.timeout),
    )
    if framer is not None:
        kwargs["framer"] = framer
    return _construct(ModbusSerialClient, tc.port, kwargs, tc.retries)

def _construct(cls, positional: Any, kwargs: Dict[str, Any], retries: int):
    """'retries' non esiste su tutte le versioni: si tenta con, poi senza."""
    try:
        return cls(positional, retries=int(retries), **kwargs)
    except TypeError:
        return cls(positional, **kwargs)

# ------------------- Compat kwarg unit id (unit/slave/device_id) -------------

_UNIT_KWARG_CACHE: Dict[Any, str] = {}
_UNIT_KWARG_CANDIDATES = ("device_id", "slave", "unit")

def _unit_kwarg(func: Callable[..., Any]) -> str:
    """Nome del kwarg dell'unit id per questa versione di pymodbus.

    Ispezione della firma invece di una catena try/except TypeError: deterministico e
    senza chiamate sprecate sul bus.
    """
    cache_key = getattr(func, "__func__", func)
    cached = _UNIT_KWARG_CACHE.get(cache_key)
    if cached:
        return cached
    name = "device_id"
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover
        params = {}
    for candidate in _UNIT_KWARG_CANDIDATES:
        if candidate in params:
            name = candidate
            break
    _UNIT_KWARG_CACHE[cache_key] = name
    return name

def call_read(func: Callable[..., Any], *, address: int, count: int, unit_id: int):
    return func(address=address, count=count, **{_unit_kwarg(func): unit_id})

def call_write(func: Callable[..., Any], *, address: int, values: list, unit_id: int):
    return func(address=address, values=values, **{_unit_kwarg(func): unit_id})

def is_error(rr: Any) -> bool:
    return rr is None or (hasattr(rr, "isError") and rr.isError())

# ------------------------------ Sessione ------------------------------------

class ModbusSession:
    """Un client Modbus per una passata di letture, con riapertura su errore.

    Motivo: un gateway trasparente (rtutcp) non ha transaction id MBAP. Se un byte resta
    nel socket - risposta tardiva, timeout - lo stream si desincronizza e *tutte* le
    letture successive falliscono. Dopo un errore la connessione viene quindi marcata
    sporca e riaperta alla lettura seguente.
    """

    def __init__(self, tc: TransportConfig, reconnect_each_read: bool = False, label: str = ""):
        self.tc = tc
        self.label = label or tc.describe()
        # Su rtutcp la riapertura dopo errore non e' opzionale.
        self.reconnect_each_read = bool(reconnect_each_read)
        self._resync_on_error = self.reconnect_each_read or tc.protocol == "rtutcp"
        self._cli = None

    # ---- ciclo di vita ----

    def __enter__(self) -> "ModbusSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def client(self):
        """Client connesso, aprendolo se serve. Solleva se la connessione non si apre."""
        if self._cli is None:
            cli = make_client(self.tc)
            if not cli.connect():
                _safe_close(cli)
                raise ConnectionError(f"connessione non aperta ({self.tc.describe()})")
            self._cli = cli
        return self._cli

    def close(self) -> None:
        if self._cli is not None:
            _safe_close(self._cli)
            self._cli = None

    def _drop(self) -> None:
        """Chiude il client: la prossima lettura ne apre uno pulito."""
        self.close()

    # ---- letture/scritture ----

    def read_registers(self, address: int, count: int, unit_id: int, *, input_registers: bool = True):
        """Legge registri; solleva su errore Modbus o di trasporto."""
        cli = self.client()
        func = cli.read_input_registers if input_registers else cli.read_holding_registers
        try:
            rr = call_read(func, address=address, count=count, unit_id=unit_id)
        except Exception:
            self._drop()
            raise
        if is_error(rr):
            if self._resync_on_error:
                self._drop()
            raise IOError(f"risposta Modbus in errore @0x{address:04X}: {rr}")
        if self.reconnect_each_read:
            self._drop()
        return rr

    def read_block(self, address: int, count: int, unit_id: int, *, input_registers: bool = True) -> list:
        """Legge 'count' registri contigui in una sola transazione.

        Per i dispositivi con registri adiacenti (es. le 12 temperature del lettore DS18B20)
        e' una richiesta sola invece di una per grandezza: su un gateway di rete la
        differenza fra un round trip e otto e' sostanziale.
        """
        rr = self.read_registers(address, count, unit_id, input_registers=input_registers)
        regs = list(getattr(rr, "registers", None) or [])
        if len(regs) < count:
            raise IOError(f"attesi {count} registri @0x{address:04X}, ricevuti {len(regs)}")
        return regs

    def read_measure(self, m: Measure, unit_id: int) -> float:
        """Legge una singola grandezza. Ritorna nan su errore, senza sollevare."""
        try:
            rr = self.read_registers(m.address, m.count, unit_id, input_registers=m.input_registers)
        except Exception as e:
            log.debug("lettura '%s' @0x%04X unit=%s (%s) fallita: %s",
                      m.key, m.address, unit_id, self.label, e)
            return float("nan")
        return decode(m, getattr(rr, "registers", None))

    def read_float32(self, address: int, unit_id: int, *, input_registers: bool = True) -> float:
        """Legge un float32 su 2 registri. Ritorna nan su errore, senza sollevare."""
        try:
            rr = self.read_registers(address, 2, unit_id, input_registers=input_registers)
        except Exception as e:
            log.debug("read_float32 @0x%04X unit=%s (%s) fallita: %s", address, unit_id, self.label, e)
            return float("nan")
        regs = getattr(rr, "registers", None) or []
        if len(regs) < 2:
            return float("nan")
        return registers_to_float((regs[0], regs[1]))

    def write_registers(self, address: int, values: list, unit_id: int):
        cli = self.client()
        try:
            rq = call_write(cli.write_registers, address=address, values=values, unit_id=unit_id)
        except Exception:
            self._drop()
            raise
        if is_error(rq) and self._resync_on_error:
            self._drop()
        return rq

def _safe_close(cli: Any) -> None:
    try:
        cli.close()
    except Exception:
        pass
