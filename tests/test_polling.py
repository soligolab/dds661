"""Integrazione del poller contro il gateway emulato, senza broker MQTT."""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [ROOT, HERE]
import polling
from fake_gateway import METER_EXPECTED as EXPECTED, METER_UNIT as UNIT

CFG = {
    "serial": {"port": "/dev/ttyUSB0", "baudrate": 9600, "parity": "E", "timeout": 1.0},
    "tcp": {"host": "127.0.0.1", "port": 15021, "timeout": 2.0},
    "links": {
        "dr302":    {"protocol": "rtutcp", "host": "127.0.0.1", "port": 15020, "timeout": 2.0},
        "dr302_gw": {"protocol": "tcp",    "host": "127.0.0.1", "port": 15021, "timeout": 2.0},
    },
    "mqtt": {"base_topic": "dds661", "qos": 0, "retain": True},
    "polling": {"read_mode": "sequential", "per_measure_delay_ms": 0,
                "delay_ms_between_devices": 0, "debug_log": False},
    "devices": [
        {"id": UNIT, "type": "sdm230", "name": "Contatore DR302",      "link": "dr302"},
        {"id": UNIT, "type": "sdm230", "name": "Contatore DR302 GW",   "link": "dr302_gw"},
    ],
}

fails = []

def cmp(label, vals):
    bad = {k: (v, round(vals[k], 4)) for k, v in EXPECTED.items() if abs(vals[k] - v) > 1e-3}
    print(f"  {label:34s} {'OK' if not bad else 'DIVERGENTI ' + str(bad)}")
    if bad: fails.append(label)

print("1) _read_device_sequential per trasporto")
for name in ("dr302", "dr302_gw"):
    tc = polling.resolve_transport(CFG, {"id": UNIT, "link": name})
    vals = polling._read_device_sequential("sdm230", tc, UNIT, 0.0)
    cmp(f"sequential via {tc.describe()}", vals)

print("2) _read_device_bulk (usa il driver, non ADDR_MAP)")
for name in ("dr302", "dr302_gw"):
    tc = polling.resolve_transport(CFG, {"id": UNIT, "link": name})
    cmp(f"bulk via {tc.describe()}", polling._read_device_bulk("sdm230", tc, UNIT))

print("3) reconnect_each_read: true (riapre a ogni misura)")
tc = polling.resolve_transport(CFG, {"id": UNIT, "link": "dr302"})
cmp("sequential reconnect-each-read", polling._read_device_sequential(
    "sdm230", tc, UNIT, 0.0, reconnect_each_read=True))

print("4) _poll_once completo con client MQTT finto")
published = []
def _no_nan(token):
    # NaN e Infinity non fanno parte di JSON: json.loads di Python li accetta come
    # estensione, ma i consumatori veri (JSON.parse, jq) rifiutano il messaggio.
    # Qui devono far fallire la suite.
    raise ValueError(f"token non-JSON nel payload MQTT: {token}")

class FakeMqtt:
    def publish(self, topic, payload, qos=0, retain=False):
        published.append((topic, json.loads(payload, parse_constant=_no_nan)))
polling._poll_once(FakeMqtt(), CFG)
for topic, payload in published:
    cmp(f"publish {topic}", payload)
expected_topics = {"dds661/contatore-dr302/state", "dds661/contatore-dr302-gw/state"}
got = {t for t, _ in published}
print(f"  topic pubblicati: {sorted(got)}")
if got != expected_topics:
    fails.append(f"topic inattesi: {got ^ expected_topics}")

print("5) read_mode: bulk in _poll_once")
published.clear()
polling._poll_once(FakeMqtt(), {**CFG, "polling": {**CFG["polling"], "read_mode": "bulk"}})
print(f"  {len(published)} publish in modalita' bulk (prima era ignorata sui device TCP)")
for topic, payload in published:
    cmp(f"bulk publish {topic}", payload)

print("\n6) errori: device non raggiungibile -> NaN, il poller non muore")
published.clear()
bad_cfg = {**CFG, "links": {**CFG["links"], "morto": {"protocol": "rtutcp", "host": "127.0.0.1", "port": 15099, "timeout": 0.4}},
           "devices": [{"id": 99, "type": "sdm230", "name": "Assente", "link": "morto"}]}
polling._poll_once(FakeMqtt(), bad_cfg)
nan_payload = published[0][1] if published else {}
keys = [m.key for m in polling.DRIVERS['sdm230'].measures()]
# Una misura assente viaggia come 'null', non come NaN: il payload resta JSON valido
# e chi lo legge distingue "non misurato" da "misurato zero".
all_null = bool(keys) and all(k in nan_payload and nan_payload[k] is None for k in keys)
print(f"  publish emesso={bool(published)} tutte null={all_null}")
if not published or not all_null: fails.append("gestione device assente")

print("\nRISULTATO:", "tutti i controlli superati" if not fails else f"FALLITI: {fails}")
sys.exit(1 if fails else 0)
