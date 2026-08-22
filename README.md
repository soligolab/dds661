# modbus-mqtt-bridge

Tooling per leggere dispositivi Modbus e pubblicarli su MQTT con discovery per Home Assistant:
contatori di energia **DDS661** e **Eastron SDM230**, lettore temperature **DS18B20-RS485** a 12
canali, modulo I/O **Pixsys MCM260**.
Tre trasporti: seriale RTU, Modbus TCP e RTU-over-TCP (gateway trasparenti tipo USR-DR302).

## File
- `transport.py` — trasporto Modbus (`rtu` / `tcp` / `rtutcp`), codec dei valori, descrittore `Measure`
- `drivers.py` — registro dei driver supportati
- `dds661.py` — driver DDS661
- `sdm230.py` — driver SDM230 (float32 su 2 registri, MSW first)
- `ds18b20.py` — driver lettore temperature DS18B20 a 12 canali (int16 ×0,01)
- `mcm260.py` — driver modulo I/O Pixsys MCM260 (ingressi digitali → binary sensor)
- `polling.py` — poller multi-dispositivo → MQTT/HA
- `meter.py` — CLI per singolo device (lettura/scrittura/`probe`)
- `config.example.yaml` — configurazione di esempio, da copiare in `config.yaml`
- `install.sh` — crea il virtualenv e installa le dipendenze
- `service/` — unit systemd per l'avvio automatico
- `tests/` — verifica offline su gateway emulato (vedi [Test](#test))

## Requisiti
```bash
pip install "pymodbus>=3,<4" pyserial paho-mqtt pyyaml
```

## Installazione e avvio del servizio (LXC con utente root)
Esempio per container Proxmox dove il repository è in `/root/dds661`.

1. **Clona il repository**
   ```bash
   cd /root
   git clone <URL_REPO> dds661
   cd dds661
   ```
2. **Crea il virtualenv e installa le dipendenze**
   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt
   ```
3. **Prepara la configurazione**
   ```bash
   cp config.example.yaml config.yaml
   ```
   `config.yaml` è ignorato da git: ogni installazione ha il suo, e i `git pull`
   successivi non entrano in conflitto con le personalizzazioni.
4. **Configura il servizio systemd**
   ```bash
   cp service/dds661-polling.service /etc/systemd/system/dds661-polling.service
   systemctl daemon-reload
   ```
5. **Abilita e avvia il servizio**
   ```bash
   systemctl enable --now dds661-polling.service
   ```
6. **Verifica lo stato**
   ```bash
   systemctl status dds661-polling.service
   ```

> L'unit conserva il nome storico `dds661-polling`: cambiarlo su un'installazione
> esistente vorrebbe dire disabilitare la vecchia unit prima di installare la nuova,
> e non vale il disturbo.

## Configurazione
Si parte da `config.example.yaml`, che documenta ogni chiave; `config.yaml` resta
locale all'installazione. Differenze rispetto al file storico:
- `polling.period_s` sostituisce `polling.interval_s` (qui impostato a **1.0** secondi).
- Aggiunto SDM230 con `id: 1` e `name: "Contatore F.M"`.
- Gli altri dispositivi (`10..13`) sono marcati `type: dds661`.

> L’abbinamento **ID ↔ tipo** strumento è nel blocco `devices:` del config.

Per aggiungere un **nuovo tipo** di dispositivo servono due cose: un modulo driver che dichiari le
proprie grandezze (`MEASURES`/`measures()`, più `MANUFACTURER` e `MODEL`) e una riga in `DRIVERS`
(`drivers.py`). Non ci sono tabelle di indirizzi né liste di sensori Home Assistant da tenere
allineate: poller e discovery leggono tutto da `measures()`.

### Identità dei dispositivi (`uid:`)

L'identificatore Home Assistant è `{tipo}_{id}`. Con più gateway l'unit id non basta — due moduli
identici in stanze diverse possono avere lo stesso indirizzo di fabbrica — quindi si può fissarlo
con `uid:` nel device. Gli `uid` duplicati vengono segnalati come errore all'avvio del poller.
Da CLI, quando lo stesso `id` compare su bus diversi, `meter.py` rifiuta `--slave` ambigui: usare
`--device "<nome>"`.

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

## Lettore temperature DS18B20-RS485 (12 canali)

Modulo RS485 per sonde DS18B20 venduto da TONIFISHI come "DS18B20-RS485".

> **La mappa è stata ricavata dal dispositivo, non da un datasheet.** Le fonti pubbliche
> ([Wiren Board](https://wiki.wirenboard.com/wiki/index.php?title=R4DCB08),
> [acpiccolo/R4DCB08](https://github.com/acpiccolo/R4DCB08-Temperature-Collector)) descrivono
> l'**R4DCB08**: 8 canali su holding `0x0000-0x000F`, int16 ×10, `0x8000` = sonda assente. Questo
> esemplare ha un firmware **diverso** e nessuna di quelle caratteristiche coincide.

| Registro | Indirizzo | Contenuto |
|---|---|---|
| Temperature CH1–12 | **input** `0x0000`–`0x000B` | int16 **×0,01 °C**, complemento a due |
| Maschera presenza | **input** `0x000C` | bit *n* a 1 → canale *n+1* **senza sonda** |
| Configurazione | **holding** `0x0000` | byte alto = baud/600, byte basso = indirizzo |
| Altri parametri | **holding** `0x0001`–`0x0008` | significato non identificato |

Riscontri che sostengono la lettura: `holding 0x0000` = `0x600A` su un modulo impostato a 57600
baud / indirizzo 10 (`0x0A` = 10, `0x60` = 96, 96 × 600 = 57600); `input 0x000C` = `0x0FFC` con due
sole sonde sui canali 1 e 2; scala 0,01 confermata dai valori letti e dalla trasformazione
`divide100` della precedente configurazione openHAB.

**Un canale senza sonda legge 0**, indistinguibile da 0,00 °C: il driver applica sempre la maschera
di presenza e pubblica `NaN`. Vale anche in modalità `sequential`, al costo di una lettura in più.

```yaml
- id: 10
  type: ds18b20
  name: "Temperature Casa"
  uid: temp_casa
  link: dr302
  read_mode: bulk           # 13 registri contigui in UNA transazione
  channels:                 # solo i canali elencati vengono pubblicati
    1: "Soggiorno"
    2: "Cucina"
```

Chiavi MQTT `t1`..`t12`; in HA `device_class: temperature`, `state_class: measurement`, unità °C.
La velocità si scrive con `meter.py ... write --baud`, che riscrive il registro di configurazione
preservando l'indirizzo — la mappa è dedotta, quindi il report invita a rileggere per conferma.

## Modulo I/O Pixsys MCM260-3AD

Sedici ingressi digitali in un solo registro, un contatto per bit; ogni bit diventa un
**binary_sensor**.

| Registro | Indirizzo | Contenuto |
|---|---|---|
| Ingressi digitali | **holding** `1000` | bit 0..15, bit 0 = ingresso 1 |

**Logica invertita** (`invert: true`, default): bit a 1 = contatto **chiuso** → binary sensor `off`.
È la convenzione dei contatti magnetici usati qui, e riproduce la trasformazione
`OpenCloseInversion` della configurazione precedente. Si disattiva con `invert: false`, globalmente
sul device o per singolo ingresso.

```yaml
- id: 5
  type: mcm260
  name: "MCM260 Soggiorno"
  uid: mcm260_soggiorno
  link: dr302
  read_mode: bulk
  inputs:                   # bit: {name, device_class}
    0: {name: "Finestra Cucina DX", device_class: window}
    2: {name: "Portoncino Disimpegno", device_class: door}
    4: {name: "Perimetrale Nord Est", device_class: occupancy}
```

Chiavi MQTT `di0`..`di15`, valori interi 0/1; la discovery emette `payload_on: "1"` /
`payload_off: "0"`. Senza `inputs:` vengono pubblicati tutti e 16 i bit con nomi generici, quindi
conviene dichiarare solo quelli cablati.

## Dispositivi senza datasheet: `probe`

Quando la mappa registri è incerta, `probe` legge gli intervalli tipici (`0x0000-0x001F` e
`0x00F0-0x00FF`) **sia come holding sia come input**, e affianca le interpretazioni possibili:

```bash
python3 meter.py --config config.yaml --device "Temperature Casa" probe
```
```
## FC03 holding 0x0000..0x001F  (10 registri non nulli su 32)
   0x0000  0x00D7  u16=215     i16=215      i16*0.1=21.5      f32=1.98364e-38
   0x0002  0x8000  u16=32768   i16=-32768   i16*0.1=-3276.8   <- 0x8000: sonda assente?
   0x00FE  0x000A  u16=10      i16=10       i16*0.1=1.0
   0x00FF  0x0006  u16=6       i16=6        i16*0.1=0.6
```

Si cerca la colonna che produce valori sensati: qui `i16*0.1` dà temperature plausibili, `0x00FE`
contiene l'indirizzo noto (10) e `0x00FF` il codice enum della velocità impostata. È sola lettura, e
se nessun function code risponde il problema è la linea (baud/parità del gateway, indirizzo,
cablaggio A/B), non la mappa.

## Test

Verifica senza hardware, contro un gateway Modbus emulato che riproduce i tre
dispositivi reali (SDM230, DS18B20 a 12 canali, MCM260) su due framer — RTU sulla
porta 15020, come un DR302 trasparente, e SOCKET sulla 15021, come un gateway che
converte in Modbus TCP:

```bash
bash tests/run_all.sh
```

Coprono i casi che il campo non offre: temperatura negativa, sonda staccata (che
deve dare `NaN`, non 0,00 °C), gateway che cade e torna, device muto.
`tests/test_devices.py` confronta inoltre chiavi e indirizzi dei contatori con la
`ADDR_MAP` di `48d0394`, l'ultimo commit prima del refactor: è il controllo che la
generalizzazione del vocabolario misure non abbia spostato nulla di ciò che era
già in produzione.

`tests/hardware/` è fuori dal runner: quelle misure vogliono il DR302 vero.

## Troubleshooting
- **NaN nelle misure** → controlla baud/parità/stop, terminazioni, ID corretto; le misure usano gli **input registers** (0x04).
- **MQTT** → verifica host/porta/credenziali/TLS; gestione compatibile Paho v1/v2.
- **`OSError: [Errno 101] Network is unreachable`** → la rete del container/host non ha route verso il broker.
  - Verifica gateway/interfaccia con `ip route` e connettività verso il broker (`ping <host>`, `nc -vz <host> 1883`).
  - In `mqtt` usa:
    - `connect_retries` (0 = retry infinito),
    - `connect_retry_delay_s` (ritardo tra tentativi),
    - `socket_timeout_s` (timeout socket).
  - Avvia con log dettagliato: `python3 polling.py --config config.yaml --log DEBUG`.


## Trasporti Modbus

Ogni dispositivo sceglie il proprio trasporto. Se non specifica nulla usa `rtu` sul blocco `serial:`
globale, quindi le configurazioni esistenti continuano a funzionare senza modifiche.

| `protocol` | Client | Framer | Quando usarlo |
|---|---|---|---|
| `rtu` | seriale | RTU | porta seriale locale (`/dev/tty*`) |
| `tcp` | TCP | SOCKET (MBAP) | Modbus TCP vero: CNV520-21AD, o gateway con conversione di protocollo attiva |
| `rtutcp` | TCP | RTU | **gateway trasparente**: trame RTU con CRC16 dentro il socket (USR-DR302 di default) |

### Blocco `links:` — trasporti con nome

Quando più contatori stanno sullo stesso bus o dietro lo stesso gateway, il trasporto si descrive
una volta sola e i device lo referenziano con `link:`:

```yaml
links:
  dr302:
    protocol: rtutcp
    host: 192.168.1.177
    port: 502
    timeout: 1.5

devices:
  - id: 20
    type: sdm230
    name: "Contatore Garage"
    link: dr302
  - id: 21
    type: dds661
    name: "Contatore Officina"
    link: dr302
    timeout: 2.0        # override puntuale
```

La risoluzione è a strati, l'ultimo vince: `serial:` globale → `tcp:` globale → voce di `links:` →
blocchi `serial:`/`tcp:` annidati nel device → chiavi piatte del device (`host`, `port`, `timeout`,
`baudrate`, …). Retrocompatibile con `protocol: tcp` + blocco `tcp:` per-device.

## USR-DR302 (Ethernet ↔ RS485)

Il DR302 può lavorare in due modi **mutuamente esclusivi**, e la scelta determina il `protocol`:

| Impostazione sul gateway | `protocol` da usare |
|---|---|
| `Modbus TCP` **disattivato** (trasparente, default) | `rtutcp` |
| `Modbus TCP` **attivato** (conversione TCP→RTU) | `tcp` |

### Configurazione lato gateway (web server, default `admin`/`admin`)

1. **`sernet1.shtml`** — *Work Mode* → **TCP Server**, *Local Port* → **502**.
   In `TCP Client` (default di fabbrica) il modulo **non** è in ascolto e la connessione alla 502
   viene rifiutata.
2. **`sernet1.shtml`** — baudrate / data bit / parità / stop bit **identici ai dispositivi** sul bus
   RS485. Il default di fabbrica è 115200 8N1; l'installazione attuale usa **57600 8N1** (linea del
   lettore temperature). I contatori SDM230/DDS661 lavorano a 8E1 e **non possono stare su questo
   bus**: restano sulla seriale locale, o vogliono un gateway proprio.
3. **`sernet2.shtml`** — checkbox **`Modbus TCP`**: lasciarla **spenta** per usare `rtutcp`,
   accenderla per usare `tcp`.
4. **`sernet2.shtml`** — checkbox **"Similar RFC2217"**: **spenta**. Fa interpretare sequenze del
   flusso TCP come comandi di riconfigurazione seriale, insidiosa con traffico Modbus binario.
5. Cablaggio RS485: rispettare la polarità A/B e terminare il bus.

Più dispositivi possono condividere lo stesso gateway se condividono la linea: qui il bus del
`.177` porta il lettore temperature (slave 10) e l'MCM260 (slave 5).

Passare da una modalità all'altra è solo un cambio di `link:` nel config, senza toccare codice.

### Diagnosi da CLI

```bash
# raggiungibilità: se la 502 dà "connection refused" il gateway non è in TCP Server
ping -c3 192.168.1.177
python3 -c "import socket; socket.create_connection(('192.168.1.177',502),3); print('socket ok')"

# quale delle due modalità è attiva: solo una delle due dà valori plausibili
python3 meter.py --config config.yaml --slave 20 --protocol rtutcp --host 192.168.1.177 read
python3 meter.py --config config.yaml --slave 20 --protocol tcp    --host 192.168.1.177 read

# via trasporto con nome
python3 meter.py --config config.yaml --slave 20 --link dr302 read
```

### Note su `rtutcp`

Un gateway trasparente non ha il transaction id dell'header MBAP: se un byte resta nel socket
(risposta tardiva, timeout) lo stream si desincronizza e **tutte** le letture successive falliscono.
Per questo `transport.ModbusSession` chiude e riapre il socket dopo un errore quando il protocollo è
`rtutcp`. Va inoltre tenuto `polling.per_measure_delay_ms` ≥ 80 (gap inter-frame RTU lato seriale) e
una sola connessione attiva verso il gateway per volta.

Se un bus si rivelasse instabile, `polling.reconnect_each_read: true` riapre la connessione a ogni
singola misura (comportamento storico del path seriale, più lento ma più tollerante).

### Timeout e `retries`: attenzione al blocco del loop

Il poller è sequenziale, quindi un dispositivo che non risponde rallenta **tutti** gli altri. Il costo
di una passata su un device muto è `8 misure × (1 + retries) tentativi × timeout`. Misurato verso un
DR302 senza contatore sul bus:

| `retries` | `timeout` | Passata |
|---|---|---|
| 3 (default pymodbus) | 1.0 s | **32.7 s** |
| 1 | 1.0 s | 16.7 s |
| 1 | 0.5 s | 8.7 s |
| 0 | 0.5 s | 4.7 s |

Con `polling.period_s: 1.0` il default di pymodbus paralizza il ciclo. Per questo i link del gateway
in `config.yaml` impostano `retries: 1`: una misura persa viene ripresa al ciclo successivo. Il campo
`retries:` è disponibile su qualunque trasporto (globale, `links:` o per-device).
