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

print("\nRISULTATO:", "tutti i controlli superati" if not fails else f"FALLITI: {fails}")
sys.exit(1 if fails else 0)
