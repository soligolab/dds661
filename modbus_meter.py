#!/usr/bin/env python3
"""
modbus_meter.py — Lettura/scrittura parametri RS485 (float32) per contatore MODBUS-RTU.

- Bus: RS-485, 8E1 (default), CRC Modbus
- Word/byte order dei float: High word -> Low word (ABCD)

Parametri gestiti (Holding Registers 0x03/0x10):
  - 0x0000: Baud rate    (float32: 1200, 2400, 4800, 9600)
  - 0x0002: Parity       (float32: 0=Even, 1=Odd, 2=None)
  - 0x0008: Slave ID     (float32: 1..247)

Esempio d'uso (CLI):
  python modbus_meter.py --port /dev/ttyCOM1 --slave 1 read
  python modbus_meter.py --port /dev/ttyCOM1 --slave 1 write --baud 9600 --parity 0 --slave-new 2
"""

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from pymodbus.client import ModbusSerialClient
from pymodbus.constants import Endian
from pymodbus.payload import BinaryPayloadBuilder, BinaryPayloadDecoder
from pymodbus.exceptions import ModbusException

# ---- Mappa registri Holding (parametri) ----
REG_BAUD   = 0x0000  # float32 (2 regs)
REG_PARITY = 0x0002  # float32 (0=Even, 1=Odd, 2=None)
REG_SLAVE  = 0x0008  # float32 (1..247)

WORD_ORDER = Endian.BIG   # High word first (ABCD)
BYTE_ORDER = Endian.BIG   # Big-endian inside the word

@dataclass
class MeterConnection:
    port: str = "/dev/ttyCOM1"
    baudrate: int = 9600
    parity: str = "E"          # 'E' (Even), 'O', 'N'
    stopbits: int = 1
    bytesize: int = 8
    timeout: float = 1.0
    slave: int = 1             # unit id corrente del dispositivo

    def make_client(self) -> ModbusSerialClient:
        return ModbusSerialClient(
            method="rtu",
            port=self.port,
            baudrate=self.baudrate,
            parity=self.parity,
            stopbits=self.stopbits,
            bytesize=self.bytesize,
            timeout=self.timeout,
        )

# ---------------------- Utility Float32 <-> Registers ---------------------- #

def float_to_registers(value: float) -> Tuple[int, int]:
    """Encode a float32 into two 16-bit registers (HighWord, LowWord)."""
    b = BinaryPayloadBuilder(byteorder=BYTE_ORDER, wordorder=WORD_ORDER)
    b.add_32bit_float(value)
    regs = b.to_registers()
    return regs[0], regs[1]

def registers_to_float(regs: Tuple[int, int]) -> float:
    """Decode two 16-bit registers (HighWord, LowWord) into a float32."""
    dec = BinaryPayloadDecoder.fromRegisters(
        list(regs), byteorder=BYTE_ORDER, wordorder=WORD_ORDER
    )
    return dec.decode_32bit_float()

# ----------------------------- API di Lettura ------------------------------ #

def read_params(conn: MeterConnection) -> Dict[str, float]:
    """
    Legge i tre parametri di configurazione (holding).
    Ritorna un dict: {"baud": float, "parity": float, "slave": float}
    """
    client = conn.make_client()
    if not client.connect():
        raise RuntimeError("Impossibile aprire la porta seriale")

    try:
        out = {}
        for name, addr in (("baud", REG_BAUD), ("parity", REG_PARITY), ("slave", REG_SLAVE)):
            rr = client.read_holding_registers(address=addr, count=2, slave=conn.slave)
            if rr.isError():
                raise ModbusException(rr)
            value = registers_to_float((rr.registers[0], rr.registers[1]))
            out[name] = value
        return out
    finally:
        client.close()

# ---------------------------- API di Scrittura ----------------------------- #

def write_params(conn: MeterConnection,
                 current: Dict[str, float],
                 newvals: Dict[str, Optional[float]]) -> Dict[str, str]:
    """
    Scrive selettivamente i parametri che differiscono da 'current'.

    Args:
      conn: MeterConnection (stato di collegamento attuale)
      current: valori letti (chiavi: "baud", "parity", "slave")
      newvals: nuovi valori desiderati; usare None per lasciare invariato.

    Returns:
      report con esito per ciascun parametro (str).
    Note importanti:
      - La scrittura di 'slave', 'baud' o 'parity' può rendere necessario
        riconnettere il master con i nuovi parametri *prima* di poter proseguire.
      - Per sicurezza eseguiamo le scritture nell'ordine: slave -> parity -> baud.
    """
    plan = [("slave", REG_SLAVE), ("parity", REG_PARITY), ("baud", REG_BAUD)]
    client = conn.make_client()
    if not client.connect():
        raise RuntimeError("Impossibile aprire la porta seriale")

    report: Dict[str, str] = {}
    try:
        for name, addr in plan:
            desired = newvals.get(name, None)
            if desired is None:
                report[name] = "skipped (None)"
                continue
            if name in current and abs(current[name] - desired) < 1e-6:
                report[name] = f"unchanged ({desired})"
                continue

            hi, lo = float_to_registers(float(desired))
            rq = client.write_registers(address=addr, values=[hi, lo], slave=conn.slave)
            if rq.isError():
                report[name] = f"ERROR: {rq}"
                # Se fallisce una, continuiamo comunque a tentare le altre
            else:
                report[name] = f"written ({desired})"
                # Aggiorna stato locale perché step successivi possono dipendere
                current[name] = float(desired)

                # Se abbiamo cambiato lo slave, aggiorniamo anche conn.slave
                if name == "slave":
                    conn.slave = int(desired)

        return report
    finally:
        client.close()

# ----------------------------- Letture misure ------------------------------ #
# Opzionale: utility per leggere alcune misure dagli Input Registers 0x04.
INPUT_MAP = {
    "voltage":   0x0000,
    "current":   0x0008,
    "p_active":  0x0012,
    "pf":        0x002A,
    "freq":      0x0036,
    "e_total":   0x0100,
    "e_pos":     0x0102,
    "e_rev":     0x0103,
}

def read_measurements(conn: MeterConnection) -> Dict[str, float]:
    client = conn.make_client()
    if not client.connect():
        raise RuntimeError("Impossibile aprire la porta seriale")

    try:
        out = {}
        for name, addr in INPUT_MAP.items():
            rr = client.read_input_registers(address=addr, count=2, slave=conn.slave)
            if rr.isError():
                out[name] = float("nan")
            else:
                out[name] = registers_to_float((rr.registers[0], rr.registers[1]))
        return out
    finally:
        client.close()

# ------------------------------- CLI semplice ------------------------------ #

if __name__ == "__main__":
    import argparse, sys, json

    ap = argparse.ArgumentParser(description="Tool Modbus per contatore RS485 (float32).")
    ap.add_argument("--port", default="/dev/ttyCOM1")
    ap.add_argument("--baudrate", type=int, default=9600)
    ap.add_argument("--parity", choices=["E", "O", "N"], default="E")
    ap.add_argument("--stopbits", type=int, default=1)
    ap.add_argument("--slave", type=int, default=1)

    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("read", help="Legge i parametri (holding) e le principali misure (input).")

    w = sub.add_parser("write", help="Scrive parametri (solo quelli diversi).")
    w.add_argument("--baud", type=float, help="Nuovo baud rate (float), es. 9600")
    w.add_argument("--parity-new", type=float, help="Nuova parità: 0=Even, 1=Odd, 2=None")
    w.add_argument("--slave-new", type=float, help="Nuovo indirizzo Modbus 1..247")

    args = ap.parse_args()

    conn = MeterConnection(
        port=args.port,
        baudrate=args.baudrate,
        parity=args.parity,
        stopbits=args.stopbits,
        slave=args.slave,
    )

    if args.cmd == "read":
        params = read_params(conn)
        meas = read_measurements(conn)
        print(json.dumps({"params": params, "measurements": meas}, indent=2))
        sys.exit(0)

    if args.cmd == "write":
        # 1) Leggo lo stato corrente
        cur = read_params(conn)
        # 2) Preparo nuovi valori (None = non toccare)
        newvals = {
            "baud": args.baud,
            "parity": args.parity_new,
            "slave": args.slave_new
        }

        rep = write_params(conn, cur, newvals)
        print(json.dumps({"current": cur, "requested": newvals, "report": rep}, indent=2))
        print("\nATTENZIONE:")
        print("- Se hai cambiato SLAVE, usa --slave <nuovo> nelle chiamate successive.")
        print("- Se hai cambiato PARITY o BAUD, riconnetti con i nuovi parametri di linea.")
