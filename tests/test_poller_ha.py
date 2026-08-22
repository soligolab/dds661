"""Verifica 4b-6: transazioni, poller, discovery HA, selezione device in meter.py."""
import json, logging, math, re, subprocess, sys, tempfile, os
logging.basicConfig(level=logging.CRITICAL)
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [ROOT, HERE]

import transport, polling
from fake_gateway import TEMP_UNIT, METER_UNIT, METER_EXPECTED, TEMP_EXPECTED, IO_UNIT, IO_INPUTS_RAW

fails = []
def check(label, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'}  {label}{('  ' + detail) if detail else ''}")
    if not ok: fails.append(label)

CFG = {
    "links": {
        "dr302":      {"protocol": "rtutcp", "host": "127.0.0.1", "port": 15020, "timeout": 2.0},
        "dr302_temp": {"protocol": "rtutcp", "host": "127.0.0.1", "port": 15020, "timeout": 2.0},
    },
    "mqtt": {"base_topic": "dds661", "qos": 0, "retain": True},
    "home_assistant": {"enabled": True, "discovery_prefix": "homeassistant"},
    "polling": {"read_mode": "sequential", "per_measure_delay_ms": 0, "delay_ms_between_devices": 0},
    "devices": [
        {"id": METER_UNIT, "type": "sdm230", "name": "Contatore F.M", "link": "dr302"},
        {"id": TEMP_UNIT, "type": "ds18b20", "name": "Temperature Soggiorno",
         "uid": "temp_soggiorno", "link": "dr302_temp", "read_mode": "bulk",
         "channels": {1: "Mandata", 2: "Ritorno", 3: "Boiler"}},
        {"id": IO_UNIT, "type": "mcm260", "name": "IO Soggiorno", "uid": "io_soggiorno",
         "link": "dr302_temp", "read_mode": "bulk",
         "inputs": {0: {"name": "Finestra", "device_class": "window"},
                    5: {"name": "Non cablato"}}},
    ],
}

print("4b) La lettura bulk deve costare UNA transazione, non otto")
calls = []
orig = transport.ModbusSession.read_registers
def counting(self, address, count, unit_id, **kw):
    calls.append((address, count)); return orig(self, address, count, unit_id, **kw)
transport.ModbusSession.read_registers = counting
try:
    tc = polling.resolve_transport(CFG, CFG["devices"][1])
    calls.clear(); polling._read_device_bulk("ds18b20", tc, TEMP_UNIT, CFG["devices"][1])
    n_bulk = len(calls)
    calls.clear(); polling._read_device_sequential("ds18b20", tc, TEMP_UNIT, 0.0, CFG["devices"][1])
    n_seq = len(calls)
finally:
    transport.ModbusSession.read_registers = orig
check("bulk = 1 transazione", n_bulk == 1, f"{n_bulk}")
check("sequenziale = 3 canali + 1 maschera di presenza", n_seq == 4, f"{n_seq}")

print("\n5) _poll_once: payload")
published = []
class FakeMqtt:
    def publish(self, topic, payload, qos=0, retain=False):
        published.append((topic, json.loads(payload)))
polling._poll_once(FakeMqtt(), CFG)
by_topic = dict(published)

temp = by_topic.get("dds661/temperature-soggiorno/state", {})
check("solo i canali dichiarati nel payload",
      sorted(k for k in temp if re.fullmatch(r"t\d+", k)) == ["t1", "t2", "t3"], str(sorted(temp)))
check("valori temperatura corretti",
      temp.get("t1") == TEMP_EXPECTED[1] and temp.get("t2") == TEMP_EXPECTED[2]
      and temp.get("t3") == TEMP_EXPECTED[3], str(temp))

meter = by_topic.get("dds661/contatore-f-m/state", {})
check("contatore: 8 grandezze invariate",
      sorted(k for k in meter if k not in ("id", "type", "name")) ==
      sorted(METER_EXPECTED), str(sorted(meter)))
check("contatore: valori corretti",
      all(abs(meter[k] - v) < 1e-3 for k, v in METER_EXPECTED.items()))

print("\n5b) Discovery HA")
published.clear()
polling._ha_publish_discovery(FakeMqtt(), CFG)
disc = {t: p for t, p in published}

# I contatori devono restare identici alla vecchia lista cablata in polling.py.
OLD_SENSORS = [("voltage","Voltage","V","voltage"), ("current","Current","A","current"),
               ("p_active","Active Power","W","power"), ("pf","Power Factor","",None),
               ("freq","Frequency","Hz","frequency"), ("e_total","Energy Total","kWh","energy"),
               ("e_pos","Energy Import","kWh","energy"), ("e_rev","Energy Export","kWh","energy")]
bad = []
for key, label, unit, dcla in OLD_SENSORS:
    t = f"homeassistant/sensor/sdm230_{METER_UNIT}_{key}/config"
    p = disc.get(t)
    if p is None: bad.append(f"{key}: topic mancante"); continue
    want = {"name": f"Contatore F.M {label}", "uniq_id": f"sdm230_{METER_UNIT}_{key}",
            "stat_t": "dds661/contatore-f-m/state",
            "val_tpl": "{{ value_json.%s | float }}" % key}
    for k, v in want.items():
        if p.get(k) != v: bad.append(f"{key}.{k}: {p.get(k)!r} != {v!r}")
    if unit and p.get("unit_of_meas") != unit: bad.append(f"{key}.unit")
    if dcla and p.get("dev_cla") != dcla: bad.append(f"{key}.dev_cla")
    if not dcla and "dev_cla" in p: bad.append(f"{key}: dev_cla di troppo")
    if "stat_cla" in p: bad.append(f"{key}: stat_cla aggiunto ai contatori (era assente)")
check("contatori: discovery identica alla lista cablata precedente", not bad, str(bad[:3]))

tt = disc.get("homeassistant/sensor/temp_soggiorno_t1/config")
check("temperature: uniq_id usa 'uid'", tt is not None, str([k for k in disc if "t1" in k]))
if tt:
    check("temperature: nome canale nell'etichetta", tt["name"] == "Temperature Soggiorno Mandata", tt["name"])
    check("temperature: device_class/unit/state_class",
          tt.get("dev_cla") == "temperature" and tt.get("unit_of_meas") == "°C"
          and tt.get("stat_cla") == "measurement", json.dumps(tt, ensure_ascii=False)[:120])
check("temperature: solo 3 sensori (canali dichiarati)",
      sum(1 for k in disc if k.startswith("homeassistant/sensor/temp_soggiorno_")) == 3)

bs = disc.get("homeassistant/binary_sensor/io_soggiorno_di0/config")
check("MCM260: pubblicato come binary_sensor", bs is not None,
      str([k for k in disc if "io_soggiorno" in k]))
if bs:
    check("MCM260: pl_on/pl_off e template senza '| float'",
          bs.get("pl_on") == "1" and bs.get("pl_off") == "0"
          and bs["val_tpl"] == "{{ value_json.di0 }}", json.dumps(bs)[:120])
    check("MCM260: device_class e nome", bs.get("dev_cla") == "window"
          and bs["name"] == "IO Soggiorno Finestra", bs["name"])
io_payload = by_topic.get("dds661/io-soggiorno/state", {})
check("MCM260: bit interi 0/1 nel payload",
      io_payload.get("di0") == 0 and io_payload.get("di5") == 1
      and isinstance(io_payload.get("di0"), int), str(io_payload))

print("\n5c) uid duplicati segnalati")
import io as _io
logbuf = _io.StringIO(); h = logging.StreamHandler(logbuf)
polling.log.addHandler(h); polling.log.setLevel(logging.ERROR)
dup = {**CFG, "devices": [dict(CFG["devices"][1]), {**CFG["devices"][1], "name": "Altro"}]}
polling._validate_devices(dup)
polling._validate_devices({**CFG, "devices": [{"id": 1, "type": "inesistente", "name": "X"}]})
polling.log.removeHandler(h)
out = logbuf.getvalue()
check("uid duplicato -> errore in log", "uid duplicato" in out, out.strip()[:80])
check("tipo sconosciuto -> errore in log", "sconosciuto" in out)

print("\n6) meter.py: selezione del device con id ambiguo")
cfg_file = os.path.join(tempfile.mkdtemp(prefix="mmb-test-"), "cfg_ambiguo.yaml")
import yaml
with open(cfg_file, "w") as f:
    yaml.safe_dump({**CFG, "devices": [
        {"id": TEMP_UNIT, "type": "dds661", "name": "Contatore Cucina", "link": "dr302"},
        CFG["devices"][1],
    ]}, f, allow_unicode=True)

def run_meter(*a):
    return subprocess.run([sys.executable, "meter.py", "--config", cfg_file, *a],
                          cwd=ROOT, capture_output=True, text=True)

r = run_meter("--slave", str(TEMP_UNIT), "read")
check("--slave ambiguo -> errore esplicito",
      r.returncode != 0 and "piu' device" in r.stderr, r.stderr.strip()[:90])
r = run_meter("--device", "Temperature Soggiorno", "read")
ok = r.returncode == 0 and json.loads(r.stdout)["device"]["type"] == "ds18b20"
check("--device sceglie il modulo giusto", ok, r.stderr[:90])
r = run_meter("--slave", str(TEMP_UNIT), "--type", "ds18b20", "read")
check("--slave + --type disambigua", r.returncode == 0 and json.loads(r.stdout)["device"]["type"] == "ds18b20")

print("\nRISULTATO:", "tutti i controlli superati" if not fails else f"FALLITI: {fails}")
sys.exit(1 if fails else 0)
