"""Costo di una passata su un device che non risponde (nessun contatore sul bus)."""
import logging, os, sys, time
logging.basicConfig(level=logging.CRITICAL)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import polling
from transport import resolve_transport

CFG = {"links": {"dr302": {"protocol": "rtutcp", "host": "192.168.1.177", "port": 502}}}

for retries, timeout in ((3, 1.0), (1, 1.0), (1, 0.5), (0, 0.5)):
    tc = resolve_transport(CFG, {"id": 20, "link": "dr302", "timeout": timeout, "retries": retries})
    t0 = time.time()
    vals = polling._read_device_sequential("sdm230", tc, 20, 0.08)
    dt = time.time() - t0
    nan = sum(1 for v in vals.values() if str(v) == "nan")
    print(f"  retries={retries} timeout={timeout}s -> passata {dt:5.1f}s  ({nan}/8 NaN)")
print("\n  8 misure x (1 + retries) tentativi x timeout, + per_measure_delay_ms")
