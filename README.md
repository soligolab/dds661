# DDS661 — Modbus RTU Tools
Utilities and a small library to read **measurements** and read/write **parameters** of **DDS661** energy meters over **Modbus‑RTU** (RS‑485).

Tools included:
- `modbus_meter.py` — one‑shot CLI and importable module to **read** measurements and **read/write** parameters.
- `dds661_polling.py` — daemon‑style poller that periodically reads one or more meters and **publishes to MQTT** (optional **Home Assistant discovery**).

> Data encoding on the device is **IEEE‑754 float32**, using **2 registers per value** with **big‑endian word + big‑endian byte** order (ABCD).

---

## Features
- Read **Input Registers** (0x04): Voltage, Current, Active Power, Power Factor, Frequency, Energies (total/import/export).
- Read/Write **Holding Registers** (0x03/0x10): baud rate, parity, slave address.
- CLI + Python API; YAML configuration supported.
- MQTT publishing with **name‑based topics** and configurable layout.
- Robust sequential read mode with per‑measurement and inter‑device delays for noisy/slow buses.
- Optional Home Assistant Discovery (sensors auto‑created).

---

## Installation
```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install "pymodbus>=3,<4" pyserial paho-mqtt pyyaml
```

Optional `requirements.txt`:
```
pymodbus>=3,<4
pyserial
paho-mqtt
pyyaml
```

---

## Configuration (YAML)
Create `config.yaml` and adjust to your hardware. Example:

```yaml
serial:
  port: /dev/ttyCOM1
  baudrate: 9600
  parity: E           # E, O, N (must match the device)
  stopbits: 1
  bytesize: 8
  timeout: 1.0

mqtt:
  host: 192.168.1.6
  port: 1883
  client_id: dds661-bridge
  username: mqttuser
  password: mqttpass
  keepalive: 30
  base_topic: dds661
  topic_style: flat    # flat | state | measurements
  qos: 0
  retain: true
  tls: false           # true | false | or dict: {enabled: true, ca_certs: ..., certfile: ..., keyfile: ...}

home_assistant:
  enabled: true
  discovery_prefix: homeassistant
  # area: "Quadro elettrico"   # optional

polling:
  interval_s: 1.0
  read_mode: sequential        # sequential | bulk
  per_measure_delay_ms: 80     # delay between individual register reads
  delay_ms_between_devices: 80 # delay between devices on the bus
  debug_log: true              # print per-device JSON measurements

devices:
  - id: 10
    name: "Contatore Cucina"
  - id: 11
    name: "Contatore PompaCalore"
  - id: 12
    name: "Contatore Giardino"
  - id: 13
    name: "Contatore Rack"
```

**Notes**
- `mqtt.topic_style` controls the topic layout:
  - `flat` → `<base>/<slug>`
  - `state` → `<base>/<slug>/state`
  - `measurements` → `<base>/<slug>/measurements`
- A device name like `"Contatore Cucina"` is slugified to `contatore-cucina`.
- Availability/LWT is always `<base>/status` (`online`/`offline`).

---

## modbus_meter.py — one‑shot CLI

### Read measurements (and parameters) using config
```bash
python3 modbus_meter.py --config config.yaml --slave 11 read
```

### Read using explicit serial options
```bash
python3 modbus_meter.py --port /dev/ttyCOM1 --baudrate 9600 --parity E --slave 11 read
```

### Change Modbus slave ID
```bash
# connect with current ID 10
python3 modbus_meter.py --config config.yaml --slave 10 write --slave-new 11
# verify
python3 modbus_meter.py --config config.yaml --slave 11 read
```

### Change device parity (0=Even, 1=Odd, 2=None)
```bash
python3 modbus_meter.py --config config.yaml --slave 11 write --parity-new 2
# then reconnect with --parity N
python3 modbus_meter.py --port /dev/ttyCOM1 --baudrate 9600 --parity N --slave 11 read
```

### Example output
```json
{
  "params": { "baud": 9600.0, "parity": 0.0, "slave": 11.0 },
  "measurements": {
    "voltage": 238.6, "current": 0.033, "p_active": 0.0, "pf": 1.0, "freq": 50.0,
    "e_total": 0.02, "e_pos": 0.02, "e_rev": -151732604633088.0
  }
}
```

---

## dds661_polling.py — MQTT poller

Continuously polls all `devices` in the YAML and publishes a JSON payload per device.

### Run
```bash
# normal daemon-style
python3 dds661_polling.py --config config.yaml

# one cycle + debug JSON logs
python3 dds661_polling.py --config config.yaml --oneshot --debug-read --log-level INFO
```

### Topics
Given `base_topic: dds661` and `name: "Contatore Cucina"`:
- With `topic_style: flat` → `dds661/contatore-cucina`
- With `topic_style: state` → `dds661/contatore-cucina/state`
- With `topic_style: measurements` → `dds661/contatore-cucina/measurements`
- Availability (LWT): `dds661/status`

### Payload (single topic per device)
```json
{
  "id": 10,
  "name": "Contatore Cucina",
  "voltage": 239.0,
  "current": 0.03,
  "p_active": 0.0,
  "pf": 1.0,
  "freq": 50.0,
  "e_total": 0.019,
  "e_pos": 0.0199,
  "e_rev": -15173
}
```

### Reliability knobs
- `polling.read_mode: sequential` — reads one register group at a time.
- `polling.per_measure_delay_ms` — pause between measurement reads (50–100 ms typical).
- `polling.delay_ms_between_devices` — pause between devices (80–150 ms typical).
- `polling.debug_log: true` — prints a per‑device JSON block and step‑by‑step values.

### Home Assistant discovery
If `home_assistant.enabled: true`, the poller publishes discovery configs under
`<discovery_prefix>/sensor/.../config`, with `state_topic` set to the name‑based topic and `availability` pointing to `<base>/status`.
Sensors exposed: `voltage`, `current`, `p_active`, `pf`, `freq`, `e_total`, `e_pos`, `e_rev`.

---

## Register Map (reference)

> Each value is a **float32** → **2 Modbus registers** (4 bytes). Address below is **High Word**.

### Measurements — Input Registers (0x04)
| Address (Hex) | Len | Type    | Description                 | Unit |
|--------------:|:---:|---------|-----------------------------|:----:|
| 0x0000        |  2  | float32 | Voltage                     |  V   |
| 0x0008        |  2  | float32 | Current                     |  A   |
| 0x0012        |  2  | float32 | Active power                |  kW  |
| 0x002A        |  2  | float32 | Power factor (cosφ)         |  —   |
| 0x0036        |  2  | float32 | Frequency                   |  Hz  |
| 0x0100        |  2  | float32 | Total active energy         | kWh  |
| 0x0102        |  2  | float32 | Positive active energy      | kWh  |
| 0x0103        |  2  | float32 | Reverse active energy       | kWh  |

### Parameters — Holding Registers (0x03/0x10)
| Address (Hex) | Len | Type    | Parameter            | Allowed values            |
|--------------:|:---:|---------|----------------------|---------------------------|
| 0x0000        |  2  | float32 | Baud rate            | 1200, 2400, 4800, **9600** |
| 0x0002        |  2  | float32 | Parity               | **0=Even**, 1=Odd, 2=None |
| 0x0008        |  2  | float32 | Modbus slave address | 1…247                     |

**Implementation details**
- Always request `count=2` registers per float.
- Word/byte order: **big‑endian/high‑word‑first** (ABCD). In `pymodbus`: `byteorder=Endian.BIG`, `wordorder=Endian.BIG`.

---

## Troubleshooting
- `NaN` values on some reads → use `read_mode: sequential` and tune `per_measure_delay_ms` and `delay_ms_between_devices`.
- No response → verify wiring, slave ID, and serial line (`baudrate`, `parity`). Try:  
  `python3 modbus_meter.py --port /dev/ttyCOM1 --baudrate 9600 --parity E --slave <id> read`
- Permission denied on `/dev/tty*` → add user to `dialout` (or run as root for a quick test).
- MQTT not connecting → check `host`, `port`, credentials, and `tls` settings. If `tls: true`, server must accept TLS on that port.

---

## License
MIT (or adapt to your project policy).
