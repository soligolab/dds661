"""Verifica offline dei driver DS18B20-RS485 e MCM260 sull'emulatore."""
import logging, math, os, re, subprocess, sys
logging.basicConfig(level=logging.CRITICAL)
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [ROOT, HERE]

import transport, polling
from transport import TransportConfig, decode, Measure
from drivers import DRIVERS
from ds18b20 import DS18B20RS485
from mcm260 import MCM260
from fake_gateway import (TEMP_UNIT, TEMP_EXPECTED, TEMP_ADDRESS, TEMP_BAUD_CODE,
                          IO_UNIT, IO_INPUTS_RAW)

fails = []
def check(label, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'}  {label}{('  ' + detail) if detail and not ok else ''}")
    if not ok: fails.append(label)

def tc_for(proto, port):
    return TransportConfig(protocol=proto, host="127.0.0.1", tcp_port=port, timeout=2.0)

print("1) Non-regressione del vocabolario dei contatori (vs ADDR_MAP di git HEAD)")
# 48d0394 e' l'ultimo commit prima del refactor: e' li' che vive la ADDR_MAP storica
# con cui vanno confrontate le measures() dei contatori.
BASELINE = "48d0394"
old = subprocess.run(["git", "show", f"{BASELINE}:polling.py"], cwd=ROOT,
                     capture_output=True, text=True).stdout
import dds661, sdm230
env = {}
for mod, pref in ((dds661, "D_"), (sdm230, "S_")):
    for in_name, alias in re.findall(r"IN_(\w+) as (" + pref + r"\w+)", old):
        env[alias] = getattr(mod, "IN_" + in_name)
exec(old[old.index("ADDR_MAP = {"):old.index("DRIVERS = {")], env)
for dev_type, expected in env["ADDR_MAP"].items():
    got = {m.key: m.address for m in DRIVERS[dev_type].measures()}
    check(f"{dev_type}: chiavi e indirizzi invariati", got == expected, "" if got == expected else str(got))

print("\n2) Codec")
t = DS18B20RS485.measures()[0]
for raw, want in ((0x0B60, 29.12), (0x0AF0, 28.0), (0xFE25, -4.75), (0xFED5, -2.99), (0x0000, 0.0)):
    check(f"int16 x0.01: 0x{raw:04X} -> {want}", abs(decode(t, [raw]) - want) < 1e-9, f"letto {decode(t,[raw])}")
b = Measure("x", 1000, codec="bit", bit=0, invert=True)
check("bit invertito: 1 -> 0 (contatto chiuso)", decode(b, [0x0001]) == 0)
check("bit invertito: 0 -> 1 (contatto aperto)", decode(b, [0x0000]) == 1)
check("bit resta int (serve a pl_on/pl_off)", isinstance(decode(b, [0]), int))

print("\n3) DS18B20-RS485 emulato")
CH = {n: f"Sonda {n}" for n in range(1, 13)}
DEV_T = {"id": TEMP_UNIT, "type": "ds18b20", "channels": CH}
for proto, port in (("rtutcp", 15020), ("tcp", 15021)):
    vals = DS18B20RS485(tc_for(proto, port), unit=TEMP_UNIT, dev_cfg=DEV_T).read_values(DEV_T)
    bad = [(ch, w, vals[f"t{ch}"]) for ch, w in TEMP_EXPECTED.items()
           if not (math.isnan(vals[f"t{ch}"]) if w is None else abs(vals[f"t{ch}"] - w) < 1e-9)]
    check(f"{proto}: 12 canali (negativa e sonde assenti comprese)", not bad, str(bad))

check("canale senza sonda -> NaN, non 0,00 gradi",
      math.isnan(DS18B20RS485(tc_for("rtutcp", 15020), unit=TEMP_UNIT,
                              dev_cfg=DEV_T).read_values(DEV_T)["t4"]))

p = DS18B20RS485(tc_for("rtutcp", 15020), unit=TEMP_UNIT, dev_cfg=DEV_T).read_params()
check("indirizzo dal registro di configurazione", p.address == TEMP_ADDRESS, str(p.address))
check("baud decodificato (codice x600)", p.baud == TEMP_BAUD_CODE * 600 == 57600, str(p.baud))
check("elenco sonde presenti", p.present == [1, 2, 3], str(p.present))

print("\n4) MCM260 emulato")
INP = {b: {"name": f"Ingresso {b}", "device_class": "window"} for b in (0, 1, 4, 8, 11, 5)}
DEV_IO = {"id": IO_UNIT, "type": "mcm260", "inputs": INP}
for proto, port in (("rtutcp", 15020), ("tcp", 15021)):
    vals = MCM260(tc_for(proto, port), unit=IO_UNIT, dev_cfg=DEV_IO).read_values(DEV_IO)
    want = {f"di{b}": (0 if (IO_INPUTS_RAW >> b) & 1 else 1) for b in INP}
    check(f"{proto}: bit estratti con logica invertita", vals == want, f"{vals} != {want}")
check("bit non cablato (5) risulta aperto",
      MCM260(tc_for("rtutcp", 15020), unit=IO_UNIT, dev_cfg=DEV_IO).read_values(DEV_IO)["di5"] == 1)
check("invert: false ribalta la logica",
      MCM260(tc_for("rtutcp", 15020), unit=IO_UNIT,
             dev_cfg={**DEV_IO, "invert": False}).read_values({**DEV_IO, "invert": False})["di0"] == 1)

print("\n5) Transazioni: un solo round trip per dispositivo")
calls = []
orig = transport.ModbusSession.read_registers
def counting(self, address, count, unit_id, **kw):
    calls.append((address, count)); return orig(self, address, count, unit_id, **kw)
transport.ModbusSession.read_registers = counting
try:
    for label, dev_type, unit, dev, n_seq in (("ds18b20", "ds18b20", TEMP_UNIT, DEV_T, 13),
                                              ("mcm260", "mcm260", IO_UNIT, DEV_IO, len(INP))):
        tc = tc_for("rtutcp", 15020)
        calls.clear(); polling._read_device_bulk(dev_type, tc, unit, dev); n_bulk = len(calls)
        calls.clear(); polling._read_device_sequential(dev_type, tc, unit, 0.0, dev); n_s = len(calls)
        check(f"{label}: bulk = 1 transazione", n_bulk == 1, f"{n_bulk}")
        check(f"{label}: sequenziale = {n_seq}", n_s == n_seq, f"{n_s}")
finally:
    transport.ModbusSession.read_registers = orig

print("\n6) Il percorso sequenziale applica comunque la maschera sonde")
seq = polling._read_device_sequential("ds18b20", tc_for("rtutcp", 15020), TEMP_UNIT, 0.0, DEV_T)
check("sequenziale: canale assente -> NaN", math.isnan(seq["t4"]), str(seq["t4"]))
check("sequenziale == bulk", all(
    (math.isnan(seq[k]) and math.isnan(v)) or seq[k] == v
    for k, v in polling._read_device_bulk("ds18b20", tc_for("rtutcp", 15020), TEMP_UNIT, DEV_T).items()))

print("\nRISULTATO:", "tutti i controlli superati" if not fails else f"FALLITI: {fails}")
sys.exit(1 if fails else 0)
