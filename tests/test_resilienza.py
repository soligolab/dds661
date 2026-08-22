"""La sessione rtutcp deve riprendersi da una caduta del gateway senza essere ricreata."""
import logging, os, subprocess, sys, tempfile, time
logging.basicConfig(level=logging.CRITICAL)
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [ROOT, HERE]
from transport import TransportConfig, ModbusSession
from fake_gateway import METER_UNIT as UNIT, METER_EXPECTED as EXPECTED
import sdm230

LOGDIR = tempfile.mkdtemp(prefix="mmb-resilienza-")

def pids_on_ports():
    out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True).stdout
    return sorted({l.split("pid=")[1].split(",")[0] for l in out.splitlines()
                   if "127.0.0.1:15020" in l or "127.0.0.1:15021" in l})

def start():
    subprocess.Popen(["setsid", sys.executable, os.path.join(HERE, "fake_gateway.py")],
                     stdout=open(os.path.join(LOGDIR, "fake.log"), "w"), stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, cwd=HERE)
    for _ in range(40):
        time.sleep(0.25)
        if pids_on_ports(): return True
    return False

tc  = TransportConfig(protocol="rtutcp", host="127.0.0.1", tcp_port=15020, timeout=0.6)
ses = ModbusSession(tc, label="test resilienza")
ok = lambda v: abs(v - EXPECTED["voltage"]) < 1e-3

v1 = ses.read_float32(sdm230.IN_VOLTAGE, UNIT)
print(f"1) gateway attivo          voltage={v1:.2f}  {'OK' if ok(v1) else 'FALLITO'}")

for pid in pids_on_ports():
    subprocess.run(["kill", pid])
time.sleep(1.5)
v2 = ses.read_float32(sdm230.IN_VOLTAGE, UNIT)
print(f"2) gateway spento          voltage={v2}      {'OK (NaN, nessuna eccezione)' if str(v2)=='nan' else 'FALLITO'}")

print("3) riavvio del gateway...", "ok" if start() else "NON RIPARTITO")
time.sleep(0.5)
v3 = ses.read_float32(sdm230.IN_VOLTAGE, UNIT)
print(f"4) gateway ripristinato    voltage={v3:.2f}  {'OK (ripresa senza ricreare la sessione)' if ok(v3) else 'FALLITO'}")
ses.close()

good = ok(v1) and str(v2) == "nan" and ok(v3)
print("\nRISULTATO:", "la sessione si riprende da sola" if good else "FALLITO")
sys.exit(0 if good else 1)
