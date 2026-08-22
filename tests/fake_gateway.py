"""Gateway emulato con i tre dispositivi reali, per la verifica senza hardware.

  porta 15020 -> framer RTU    (come USR-DR302 trasparente)
  porta 15021 -> framer SOCKET (come gateway con conversione Modbus TCP)

  unit 20 -> contatore SDM230        (float32 su input register)
  unit 10 -> lettore DS18B20 12 ch   (int16 x0.01 su input + maschera presenza)
  unit  5 -> modulo I/O MCM260       (16 bit in holding 1000)

I valori riproducono quelli letti sull'hardware, piu' i casi limite che il campo non
offre: una temperatura negativa e un canale con sonda staccata.
"""
import os, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymodbus.datastore import ModbusDeviceContext, ModbusSequentialDataBlock, ModbusServerContext
from pymodbus.framer import FramerType
from pymodbus.server import StartTcpServer

import sdm230, ds18b20, mcm260
from transport import float_to_registers, int16_to_register

# ----------------------------- SDM230 (unit 20) ------------------------------
METER_UNIT = 20
METER_EXPECTED = {
    "voltage": 231.4, "current": 3.25, "p_active": 742.0, "pf": 0.98,
    "freq": 50.02, "e_pos": 1234.5, "e_rev": 12.25, "e_total": 1246.75,
}
METER_PARAMS = {sdm230.REG_BAUD: 2.0, sdm230.REG_PARITY: 1.0, sdm230.REG_SLAVE: float(METER_UNIT)}

# --------------------------- DS18B20 12ch (unit 10) --------------------------
TEMP_UNIT = 10
TEMP_EXPECTED = {           # canale -> gradi, None = nessuna sonda
    1: 29.12, 2: 28.00, 3: -4.75, 4: None, 5: None, 6: None,
    7: None, 8: None, 9: None, 10: None, 11: None, 12: None,
}
TEMP_ADDRESS   = 10
TEMP_BAUD_CODE = 96          # x600 = 57600, come sul dispositivo reale
TEMP_CONFIG    = (TEMP_BAUD_CODE << 8) | TEMP_ADDRESS   # 0x600A

# ------------------------------ MCM260 (unit 5) ------------------------------
IO_UNIT = 5
IO_INPUTS_RAW = 0x0F1F       # bit 0-4 e 8-11 a 1 (contatti chiusi), come sul campo


def _meter_context():
    ir = [0] * 0x0200
    for m in sdm230.SDM230.measures():
        hi, lo = float_to_registers(METER_EXPECTED[m.key])
        ir[m.address], ir[m.address + 1] = hi, lo
    hr = [0] * 0x0100
    for addr, val in METER_PARAMS.items():
        hi, lo = float_to_registers(val)
        hr[addr], hr[addr + 1] = hi, lo
    return ir, hr


def _temp_context():
    ir = [0] * 0x0010
    mask = 0
    for ch, celsius in TEMP_EXPECTED.items():
        if celsius is None:
            mask |= 1 << (ch - 1)          # bit a 1 = canale assente
        else:
            ir[ds18b20.REG_TEMP_BASE + ch - 1] = int16_to_register(celsius, ds18b20.TEMP_SCALE)
    ir[ds18b20.REG_STATUS] = mask
    hr = [0] * 0x0010
    hr[ds18b20.REG_CONFIG] = TEMP_CONFIG
    return ir, hr


def _io_context():
    hr = [0] * (mcm260.REG_INPUTS + 8)
    hr[mcm260.REG_INPUTS] = IO_INPUTS_RAW
    return [0] * 0x0010, hr


def _context():
    # Il datastore di pymodbus risolve l'indirizzo N sull'indice N+1: si compensa con un
    # registro di padding, altrimenti ogni lettura esce sfasata di una word.
    devices = {}
    for unit, (ir, hr) in ((METER_UNIT, _meter_context()),
                           (TEMP_UNIT, _temp_context()),
                           (IO_UNIT, _io_context())):
        devices[unit] = ModbusDeviceContext(ir=ModbusSequentialDataBlock(0, [0] + ir),
                                            hr=ModbusSequentialDataBlock(0, [0] + hr))
    return ModbusServerContext(devices=devices, single=False)


def serve(port, framer):
    StartTcpServer(context=_context(), address=("127.0.0.1", port), framer=framer)


if __name__ == "__main__":
    for port, framer in ((15020, FramerType.RTU), (15021, FramerType.SOCKET)):
        threading.Thread(target=serve, args=(port, framer), daemon=True).start()
    time.sleep(1.0)
    print("ready", flush=True)
    while True:
        time.sleep(3600)
