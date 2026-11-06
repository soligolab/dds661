"""Ponte Tuya senza dispositivi ne' broker: conversioni, override e filtri sui comandi."""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [ROOT, HERE]
import tuya_bridge as tb

fails = []

def check(label, ok):
    print(f"  {label:58s} {'OK' if ok else 'FALLITO'}")
    if not ok: fails.append(label)

TEMP = {"code": "temp_set", "type": "value", "mode": "rw", "scale": 1, "min": 5, "max": 350}
HALF = {"code": "TempSet", "type": "value", "mode": "rw", "scale": 0, "min": 10, "max": 70, "factor": 0.5}

print("1) decode/encode")
check("scale 1: 210 -> 21.0", tb.decode(TEMP, 210) == 21.0)
check("scale 1: 21.5 -> 215", tb.encode(TEMP, 21.5) == 215)
check("factor 0.5: 44 -> 22.0", tb.decode(HALF, 44) == 22.0)
check("factor 0.5: 22 -> 44", tb.encode(HALF, 22) == 44)
check("bool: 'ON' -> True", tb.encode({"type": "bool"}, "ON") is True)
check("enum valido passa come stringa", tb.encode({"type": "enum", "range": ["0", "1"]}, 1) == "1")
for label, dp, v in (("fuori range rifiutato", TEMP, 40), ("bool non booleano rifiutato", {"type": "bool"}, 3),
                     ("numero come stringa rifiutato", TEMP, "21"), ("enum fuori lista rifiutato", {"type": "enum", "range": ["a"]}, "b")):
    try:
        tb.encode(dp, v); check(label, False)
    except ValueError:
        check(label, True)

print("2) worker: override, filtri sui comandi, stato")
class FakeBridge:
    base, deny, publish_raw, overrides = "tuya", set(tb.DEFAULT_DENY), False, \
        {"termostato": {"TempSet": {"factor": 0.5}}}
    published = []
    def publish(self, topic, payload): self.published.append((topic, payload))

dev = {"id": "x", "name": "T", "topic": "termostato", "dps": {
    2: {"code": "TempSet", "type": "value", "mode": "rw", "scale": 0, "min": 10, "max": 70},
    3: {"code": "TempCurrent", "type": "value", "mode": "ro", "scale": 0},
    39: {"code": "factory_reset", "type": "bool", "mode": "rw"},
    101: {"code": "program", "type": "raw", "mode": "rw"}}}
w = tb.TuyaWorker(dev, FakeBridge())
w.command(b'{"TempSet": 21.5}')
check("override applicato al comando (21.5 -> 43)", w.cmds.get_nowait() == {"2": 43})
for label, payload in (("sola lettura bloccata", b'{"TempCurrent": 20}'),
                       ("deny_codes blocca factory_reset", b'{"factory_reset": true}'),
                       ("codice sconosciuto scartato", b'{"boh": 1}'),
                       ("JSON non valido scartato", b'ON'),
                       ("un codice cattivo scarta tutto il comando", b'{"TempSet": 21, "factory_reset": true}')):
    w.command(payload)
    check(label, w.cmds.empty())
w._merge({"dps": {"2": 44, "3": 41, "101": "AAEC", "200": 7}})
check("stato decodificato con override", w.state.get("TempSet") == 22.0 and w.state.get("TempCurrent") == 41)
check("dp raw non pubblicato, dp ignoto come dp_<n>", "program" not in w.state and w.state.get("dp_200") == 7)
w.online = True
w._publish_state()
t, p = FakeBridge.published[-1]
check("topic e payload JSON valido", t == "tuya/termostato/state" and json.loads(p)["online"] is True)
w.command(b"21.5", code="TempSet")
check("set/<codice>: valore semplice con override", w.cmds.get_nowait() == {"2": 43})
w.command(b"true", code="factory_reset")
check("set/<codice>: deny_codes vale anche qui", w.cmds.empty())

print("3) discovery Home Assistant")
import tuya_ha
def configs(dev, dps):
    return {t: json.loads(p) for t, p in tuya_ha.Discovery(dev, dps, "tuya", "homeassistant", tb.DEFAULT_DENY).configs()}
wk = configs({"id": "x", "name": "T", "topic": "termostato", "category": "wk"},
             {**w.dps, "1": {"code": "Power", "type": "bool", "mode": "rw"}})
clim = wk.get("homeassistant/climate/tuya_x_climate/config", {})
check("termostato -> climate con setpoint su set/<codice>", clim.get("temperature_command_topic") == "tuya/termostato/set/TempSet")
check("climate: limiti con override (5..35 gradi)", clim.get("min_temp") == 5 and clim.get("max_temp") == 35)
check("climate: accensione e temperatura non duplicate",
      not any("_TempSet/" in t or "_TempCurrent/" in t for t in wk))
check("factory_reset e raw senza entita'", not any("factory_reset" in t or "program" in t for t in wk))
SD = {"2": {"code": "switch_go", "type": "bool", "mode": "rw"},
      "5": {"code": "status", "type": "enum", "mode": "ro", "range": ["charging", "smart_clean"]},
      "6": {"code": "residual_electricity", "type": "value", "mode": "ro", "scale": 0, "unit": "%"},
      "10": {"code": "reset_filter", "type": "bool", "mode": "rw"},
      "14": {"code": "suction", "type": "enum", "mode": "rw", "range": ["gentle", "strong"]}}
sd = configs({"id": "v", "name": "V", "topic": "aspira", "category": "sd"}, SD)
vac = sd.get("homeassistant/vacuum/tuya_v_vacuum/config", {})
check("aspirapolvere -> vacuum con velocita' su set/suction", vac.get("set_fan_speed_topic") == "tuya/aspira/set/suction")
check("reset_* -> button", "homeassistant/button/tuya_v_reset_filter/config" in sd)
check("batteria attiva, nome italiano", sd["homeassistant/sensor/tuya_v_residual_electricity/config"].get("name") == "Batteria"
      and "en" not in sd["homeassistant/sensor/tuya_v_residual_electricity/config"])
check("stato vacuum: charging -> docked", tuya_ha.vacuum_state({"status": "charging", "suction": "strong"}) == {"state": "docked", "fan_speed": "strong"})
CZ = {"17": {"code": "add_ele", "type": "value", "mode": "rw", "scale": 2, "unit": "kwh"},
      "104": {"code": "ovp", "type": "value", "mode": "rw", "scale": 1, "min": 1, "max": 3200, "unit": "V"}}
cz = configs({"id": "e", "name": "E", "topic": "meter", "category": "cz"}, CZ)
check("contatore kWh scrivibile resta sensor", "homeassistant/sensor/tuya_e_add_ele/config" in cz)
ovp = cz.get("homeassistant/number/tuya_e_ovp/config", {})
check("impostazione -> number spento, categoria config", ovp.get("en") is False and ovp.get("ent_cat") == "config" and ovp.get("max") == 320)

print("4) smistamento dei topic")
class Rec:
    def __init__(self): self.calls = []
    def command(self, payload, code=None): self.calls.append(("set", payload, code))
    def refresh(self): self.calls.append(("get",))
br = tb.Bridge.__new__(tb.Bridge)
br.base, br.workers = "tuya", {"t": Rec()}
class M:
    def __init__(self, topic, payload=b""): self.topic, self.payload = topic, payload
for topic in ("tuya/t/set", "tuya/t/set/mode", "tuya/t/get", "tuya/t/state", "tuya/x/set", "tuya/t/set/a/b"):
    br._on_message(None, None, M(topic, b"1"))
check("set, set/<codice> e get instradati, il resto ignorato",
      br.workers["t"].calls == [("set", b"1", None), ("set", b"1", "mode"), ("get",)])

print("\nRISULTATO:", "tutti i controlli superati" if not fails else f"FALLITI: {fails}")
sys.exit(1 if fails else 0)
