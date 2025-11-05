# Generic Modbus Meters (DDS661 + SDM230)

Tooling per leggere/scrivere **DDS661** e **Eastron SDM230** via Modbus RTU con pubblicazione MQTT e discovery per Home Assistant.

## File
- `dds661.py` — driver DDS661
- `sdm230.py` — driver SDM230 (float32 su 2 registri, MSW first)
- `polling.py` — poller multi-dispositivo → MQTT/HA
- `meter.py` — CLI per singolo device (lettura/scrittura)
- `config.yaml` — configurazione

## Requisiti
```bash
pip install "pymodbus>=3,<4" pyserial paho-mqtt pyyaml
```

## Configurazione (adattata al tuo setup)
Vedi `config.yaml` incluso. Differenze rispetto al file storico:
- `polling.period_s` sostituisce `polling.interval_s` (qui impostato a **1.0** secondi).
- Aggiunto SDM230 con `id: 1` e `name: "Contatore F.M"`.
- Gli altri dispositivi (`10..13`) sono marcati `type: dds661`.

> L’abbinamento **ID ↔ tipo** strumento è ora nel blocco `devices:` del config. In futuro basta aggiungere nuovi oggetti con `type:` del driver.

## Esecuzione — Poller MQTT
One-shot:
```bash
python3 polling.py --config config.yaml --oneshot
```
Loop continuo:
```bash
python3 polling.py --config config.yaml
```

### Topic MQTT
Per ogni device:
```
<base_topic>/<slug_name_o_id>/state
```
Payload JSON con: `voltage`, `current`, `p_active`, `pf`, `freq`, `e_total`, `e_pos`, `e_rev`.

> `mqtt.topic_style` è informativo al momento; la pubblicazione standard usa sempre `/state`.

## Esecuzione — CLI singolo device
Con risoluzione del tipo dal config (via `id`):
```bash
python3 meter.py --config config.yaml --slave 1 read
```
Forza il driver senza config:
```bash
python3 meter.py --port /dev/ttyUSB0 --baudrate 9600 --parity E --slave 1 --type sdm230 read
```

Scrittura parametri (es. SDM230):
```bash
# baud come valore reale (1200/2400/4800/9600); parity è codice device:
# 0=N/1stop, 1=E/1stop, 2=O/1stop, 3=N/2stop
python3 meter.py --config config.yaml --slave 1 write --baud 9600 --parity-new 0
```

## Troubleshooting
- **NaN nelle misure** → controlla baud/parità/stop, terminazioni, ID corretto; le misure usano gli **input registers** (0x04).
- **MQTT** → verifica host/porta/credenziali/TLS; gestione compatibile Paho v1/v2.


## Modbus TCP (CNV520-21AD)
- Aggiunta opzione per interrogare i dispositivi via **TCP** oltre all'RTU.
- Nel `config.yaml` puoi definire un blocco globale:
  ```yaml
  tcp:
    host: 192.168.0.99
    port: 502
    timeout: 1.0
  ```
- Per ogni device in `devices:` puoi aggiungere `protocol: tcp` (default: `rtu`).

**meter.py** con TCP (solo lettura misure):
```bash
python3 meter.py --config config.yaml --slave 10 --protocol tcp read
# oppure
python3 meter.py --type dds661 --protocol tcp --host 192.168.0.99 --port-tcp 502 --slave 10 read
```
