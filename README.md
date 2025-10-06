# DDS661 — Modbus RTU tool (Python)

A small Python utility to read **measurements** and read/write **configuration parameters** of a DDS661 energy meter over **MODBUS‑RTU** on **RS‑485**.

- Data encoding: **IEEE‑754 float32**, **two 16‑bit registers per value**.
- Word/byte order: **High word → Low word (ABCD)**, i.e. `Endian.BIG` for both byte and word order in `pymodbus` 3.x.
- Bus settings (device defaults): **9600 bps, 8E1, Modbus RTU**, slave ID 1..247.

> Library requirements: `pymodbus 3.x` and `pyserial`.

---

## Features

- Read **Input Registers** (0x04) for instant values (voltage, current, power, PF, frequency, energies).
- Read/Write **Holding Registers** (0x03/0x10) for device setup (baud, parity, slave ID).
- Encodes/decodes float32 with big‑endian word order (**ABCD**) matching the device protocol.
- Simple CLI and importable API (`read_params`, `write_params`, `read_measurements`).

---

## Register Map

> Each value is a **float32** → **2 Modbus registers** (4 bytes). The address listed is the **High Word** address.

### Measurements — *Input Registers* (Function **0x04**)

| Address (Hex) | Len (regs) | Type     | Description                       | Unit | Access |
|--------------:|:----------:|----------|-----------------------------------|:----:|:------:|
| 0x0000        | 2          | float32  | Voltage                           |  V   |   R    |
| 0x0008        | 2          | float32  | Current                           |  A   |   R    |
| 0x0012        | 2          | float32  | Active power                      |  kW  |   R    |
| 0x002A        | 2          | float32  | Power factor (cosφ)               |  —   |   R    |
| 0x0036        | 2          | float32  | Frequency                         |  Hz  |   R    |
| 0x0100        | 2          | float32  | Total active energy               | kWh  |   R    |
| 0x0102        | 2          | float32  | Positive active energy            | kWh  |   R    |
| 0x0103        | 2          | float32  | Reverse active energy             | kWh  |   R    |

### Parameters — *Holding Registers* (Function **0x03/0x10**)

| Address (Hex) | Len (regs) | Type     | Parameter             | Allowed values               | Unit | Access | Default |
|--------------:|:----------:|----------|-----------------------|------------------------------|:----:|:------:|:------:|
| 0x0000        | 2          | float32  | Baud rate             | 1200, 2400, 4800, **9600**   | bps  |  R/W   |  9600  |
| 0x0002        | 2          | float32  | Parity                | **0=Even**, 1=Odd, 2=None    |  —   |  R/W   |   0    |
| 0x0008        | 2          | float32  | Modbus slave address  | 1…247                        |  —   |  R/W   |   —    |

**Notes**

- Use **count=2** registers for each value.
- The device uses **big‑endian word order** (high word first). In `pymodbus`: `byteorder=Endian.BIG`, `wordorder=Endian.BIG`.
- After changing **baud** or **parity**, reconnect the master with the new line settings.
- After changing **slave ID**, use the new `--slave` address in subsequent commands.
- Some firmwares may return non‑meaningful values for `e_rev`; filter unrealistic numbers in your app if needed.

---

## Installation

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install "pymodbus>=3,<4" pyserial
```

Optionally with a `requirements.txt`:

```
pymodbus>=3,<4
pyserial
```

```bash
pip install -r requirements.txt
```

---

## Files

- `modbus_meter.py` — module/CLI to manage parameters and read measurements.

---

## Usage (CLI)

**Connection options** (`--port`, `--baudrate`, `--parity E|O|N`, `--slave`) configure the serial link.  
**Device parameters** are the *holding* values written as float32; for parity setting use `--parity-new 0|1|2` (not the serial `--parity`).

### Read parameters + measurements

```bash
python3 modbus_meter.py --port /dev/ttyUSB0 --baudrate 9600 --parity E --slave 11 read
```

**Real output example:**

```json
{
  "params": {
    "baud": 9600.0,
    "parity": 0.0,
    "slave": 11.0
  },
  "measurements": {
    "voltage": 238.60000610351562,
    "current": 0.032999999821186066,
    "p_active": 0.0,
    "pf": 1.0,
    "freq": 50.0,
    "e_total": 0.019999999552965164,
    "e_pos": 0.019999999552965164,
    "e_rev": -151732604633088.0
  }
}
```

### Change Modbus address (ID)

*Example: from ID 10 to 11*

```bash
# connect with the current ID
python3 modbus_meter.py --port /dev/ttyUSB0 --baudrate 9600 --parity E --slave 10 write --slave-new 11

# verify using the new ID
python3 modbus_meter.py --port /dev/ttyUSB0 --baudrate 9600 --parity E --slave 11 read
```

### Change device parity

`0=Even, 1=Odd, 2=None`

```bash
# set device parity to None (2.0)
python3 modbus_meter.py --port /dev/ttyUSB0 --baudrate 9600 --parity E --slave 11 write --parity-new 2

# reconnect with the new line parity
python3 modbus_meter.py --port /dev/ttyUSB0 --baudrate 9600 --parity N --slave 11 read
```

### Change device baud rate

```bash
python3 modbus_meter.py --port /dev/ttyUSB0 --baudrate 9600 --parity E --slave 11 write --baud 4800

# reconnect with the new baudrate
python3 modbus_meter.py --port /dev/ttyUSB0 --baudrate 4800 --parity E --slave 11 read
```

---

## Technical Notes

- Function codes: **0x04** (Input Registers), **0x03**/**0x10** (Holding Registers).
- CRC: Modbus CRC16, transmitted **LSB first** then MSB.
- In `pymodbus 3.x` the correct enum is `Endian.BIG` (uppercase).
- Internal write order: `slave` → `parity` → `baud` to minimise the chance of losing the device mid‑sequence.
- If your serial adapter is mapped differently, adjust `--port` (e.g., `/dev/ttyCOM1`, `/dev/ttyS0`).

---

## Publish to GitHub (repository **DDS661**)

### A) Prepare locally

```bash
mkdir DDS661
cd DDS661

# copy here:
#  - README.md (this file)
#  - modbus_meter.py
#  - requirements.txt (optional)

git init -b main
git add README.md modbus_meter.py requirements.txt
git commit -m "Initial commit: DDS661 Modbus RTU tool"
```

### B) Create the remote repository

**Using GitHub Web UI**  
1. Go to GitHub → **New repository** → Name: **DDS661** → Create.  
2. Copy the repo URL (SSH recommended).

**Using GitHub CLI (`gh`)**

```bash
gh repo create DDS661 --public --source=. --remote=origin --push
```

### C) Add remote & push (if created via Web UI)

```bash
# SSH (recommended if you have keys)
git remote add origin git@github.com:<your-user>/DDS661.git
git push -u origin main

# HTTPS alternative:
# git remote add origin https://github.com/<your-user>/DDS661.git
# git push -u origin main
```

From now on:

```bash
git add -A
git commit -m "Update"
git push
```

---

## License

MIT (or choose another license fitting your needs).
