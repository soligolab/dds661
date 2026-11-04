#!/usr/bin/env python3
"""
tuya_bridge.py
--------------
Ponte Tuya Wi-Fi -> MQTT tutto in locale: nessuna chiamata al cloud a runtime.
Stesso broker e stesso blocco mqtt: di polling.py, base topic proprio (tuya).

I dispositivi e le loro local key stanno in tuya-devices.yaml, generato da
tuya_cloud.py e fuori dal repository. Ogni datapoint (dp) ha codice, tipo e
scala presi dal modello cloud: sul topic si pubblica {"temp_set": 21.0},
non {"16": 210}.

Topic (con base_topic: tuya):
  tuya/status                  online/offline del ponte (LWT, retained)
  tuya/<topic>/state           JSON con tutti i dp noti + online, ts (retained)
  tuya/<topic>/availability    online/offline del singolo dispositivo (retained)
  tuya/<topic>/set             JSON {"<code>": valore, ...} -> scrittura sul dispositivo
  tuya/<topic>/get             qualunque payload -> rilettura immediata

Config (blocco tuya: in config.yaml, tutto opzionale):
-----------------------------------------------------
tuya:
  devices_file: tuya-devices.yaml
  base_topic: tuya
  client_id: tuya-mqtt-bridge
  poll_s: 60             # rilettura completa; gli aggiornamenti spontanei arrivano subito
  heartbeat_s: 10        # tiene viva la connessione persistente
  retry_min_s: 10        # attesa dopo un errore, raddoppia fino a retry_max_s
  retry_max_s: 300
  min_publish_s: 1       # gli energy meter mandano raffiche di aggiornamenti parziali
  publish_raw: false     # i dp raw (mappe, programmi settimanali) sono blob base64
  deny_codes: [...]      # codici mai scrivibili da MQTT (default: reset, formattazioni, password)
  overrides:             # correzioni ai dp del modello cloud, per topic e codice;
    termostato-camera:   # stanno qui e non in tuya-devices.yaml, che si rigenera
      TempSet: {factor: 0.5}

Un dispositivo con ip: auto non riceve tentativi TCP finche' non si fa sentire
in broadcast: quelli spenti non costano nulla.
"""

from __future__ import annotations

import argparse
import json
import logging
import queue
import signal
import socket
import threading
import time
from typing import Any, Dict, Optional, Tuple

import tinytuya

from polling import _load_yaml, _mqtt_client, _mqtt_connect

log = logging.getLogger("tuya.bridge")

# Scrivibili per il modello Tuya, ma da MQTT non devono passare: un messaggio
# sbagliato cancella la configurazione Wi-Fi, i contatori o la scheda SD.
DEFAULT_DENY = ["factory_reset", "factor_reset", "wifi_reset", "data_reset",
                "clear_energy", "sd_format", "password", "use_password"]

# Porte dei broadcast di presenza: 6666 in chiaro (3.1), 6667 cifrato (3.3/3.4).
DISCOVERY_PORTS = (6666, 6667)

_stop_evt = threading.Event()

def _handle_sigterm(signum, frame):
    _stop_evt.set()

# ------------------------------- Conversioni --------------------------------

def _factor(dp: Dict[str, Any]) -> float:
    """Valore reale = grezzo * factor. Il default viene dalla scala del modello;
    'factor:' nel file dispositivi lo sovrascrive per i modelli che dichiarano
    una scala sbagliata (es. termostati a mezzi gradi con scale 0)."""
    if "factor" in dp:
        return float(dp["factor"])
    return 10.0 ** -int(dp.get("scale", 0))

def decode(dp: Dict[str, Any], raw: Any) -> Any:
    if dp.get("type") == "value" and isinstance(raw, (int, float)) and not isinstance(raw, bool):
        f = _factor(dp)
        return raw if f == 1 else round(raw * f, 6)
    return raw

def encode(dp: Dict[str, Any], value: Any) -> Any:
    """Valore da MQTT -> valore grezzo per il dispositivo. Solleva ValueError se
    tipo o range non tornano: meglio un rifiuto nel log che un comando storto."""
    t = dp.get("type")
    if t == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.upper() in ("ON", "OFF", "TRUE", "FALSE"):
            return value.upper() in ("ON", "TRUE")
        raise ValueError(f"atteso booleano, ricevuto {value!r}")
    if t == "value":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"atteso numero, ricevuto {value!r}")
        raw = int(round(value / _factor(dp)))
        lo, hi = dp.get("min"), dp.get("max")
        if (lo is not None and raw < lo) or (hi is not None and raw > hi):
            raise ValueError(f"{value} fuori range ({decode(dp, lo)}..{decode(dp, hi)})")
        return raw
    if t == "enum":
        if str(value) not in [str(r) for r in dp.get("range", [])]:
            raise ValueError(f"{value!r} non in {dp.get('range')}")
        return str(value)
    return value

# ------------------------------- Discovery ----------------------------------

class Discovery(threading.Thread):
    """Ascolta i broadcast UDP e tiene la tabella id -> (ip, versione).
    Un solo ascoltatore per tutti: tinytuya.find_device per dispositivo
    aprirebbe le stesse porte da piu' thread."""

    def __init__(self):
        super().__init__(name="tuya-discovery", daemon=True)
        self._lock = threading.Lock()
        self._seen: Dict[str, Tuple[str, str, float]] = {}

    def lookup(self, dev_id: str) -> Optional[Tuple[str, str]]:
        with self._lock:
            v = self._seen.get(dev_id)
        return (v[0], v[1]) if v else None

    def run(self):
        import select
        socks = []
        for port in DISCOVERY_PORTS:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("", port))
                socks.append(s)
            except OSError as e:
                log.error("Discovery: porta UDP %d non disponibile: %s", port, e)
        while not _stop_evt.is_set() and socks:
            ready, _, _ = select.select(socks, [], [], 1.0)
            for s in ready:
                data, addr = s.recvfrom(4096)
                try:
                    msg = json.loads(tinytuya.decrypt_udp(data))
                except Exception:
                    continue
                dev_id = msg.get("gwId") or msg.get("devId")
                if not dev_id:
                    continue
                ip, ver = msg.get("ip") or addr[0], str(msg.get("version", "3.3"))
                with self._lock:
                    prev = self._seen.get(dev_id)
                    self._seen[dev_id] = (ip, ver, time.time())
                if not prev or prev[:2] != (ip, ver):
                    log.info("Discovery: %s su %s (protocollo %s)", dev_id, ip, ver)

# ------------------------------- Dispositivo --------------------------------

class TuyaWorker(threading.Thread):
    """Un thread per dispositivo: connessione persistente, aggiornamenti spinti dal
    dispositivo, heartbeat e rilettura periodica. I comandi MQTT arrivano in coda
    e li esegue questo thread: tinytuya.Device non e' thread-safe."""

    def __init__(self, dev: Dict[str, Any], bridge: "Bridge"):
        super().__init__(name=f"tuya-{dev['topic']}", daemon=True)
        self.dev, self.bridge = dev, bridge
        self.dps: Dict[str, Dict[str, Any]] = {str(k): dict(v) for k, v in (dev.get("dps") or {}).items()}
        for dp in self.dps.values():
            dp.update((bridge.overrides.get(dev["topic"]) or {}).get(dp["code"]) or {})
        self.by_code = {v["code"]: k for k, v in self.dps.items()}
        self.cmds: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        self.state: Dict[str, Any] = {}
        self.online: Optional[bool] = None
        self.dirty = False
        self.last_pub = 0.0
        self.topic = f"{bridge.base}/{dev['topic']}"

    # --- MQTT -> coda
    def command(self, payload: bytes) -> None:
        try:
            req = json.loads(payload)
            if not isinstance(req, dict) or not req:
                raise ValueError("serve un oggetto JSON non vuoto")
        except Exception as e:
            log.warning("%s: comando scartato (%s): %r", self.dev["topic"], e, payload[:200])
            return
        out: Dict[str, Any] = {}
        for code, value in req.items():
            dp_id = self.by_code.get(code) or (code[3:] if code.startswith("dp_") else None)
            dp = self.dps.get(dp_id or "")
            if not dp:
                log.warning("%s: codice sconosciuto '%s'", self.dev["topic"], code)
                return
            if "w" not in dp.get("mode", "ro"):
                log.warning("%s: '%s' e' in sola lettura", self.dev["topic"], code)
                return
            if dp["code"] in self.bridge.deny:
                log.warning("%s: '%s' bloccato da deny_codes", self.dev["topic"], code)
                return
            try:
                out[dp_id] = encode(dp, value)
            except ValueError as e:
                log.warning("%s: '%s' rifiutato: %s", self.dev["topic"], code, e)
                return
        self.cmds.put(out)

    def refresh(self) -> None:
        self.cmds.put(None)  # None = rileggi lo stato

    # --- stato -> MQTT
    def _set_online(self, online: bool) -> None:
        if online != self.online:
            self.online = online
            log.info("%s: %s", self.dev["topic"], "online" if online else "offline")
            self.bridge.publish(f"{self.topic}/availability", "online" if online else "offline")
            self._publish_state()

    def _merge(self, data: Any) -> bool:
        if not isinstance(data, dict) or "dps" not in data:
            return False
        for k, raw in data["dps"].items():
            dp = self.dps.get(str(k))
            if dp is None:
                self.state[f"dp_{k}"] = raw
            elif dp.get("type") == "raw" and not self.bridge.publish_raw:
                continue
            else:
                self.state[dp["code"]] = decode(dp, raw)
        return True

    def _flush(self, force: bool = False) -> None:
        if self.dirty and (force or time.time() - self.last_pub >= self.bridge.min_publish_s):
            self._publish_state()

    def _publish_state(self) -> None:
        self.dirty, self.last_pub = False, time.time()
        payload = {"id": self.dev["id"], "name": self.dev.get("name"),
                   "category": self.dev.get("category"), "online": bool(self.online),
                   "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **self.state}
        self.bridge.publish(f"{self.topic}/state", json.dumps(payload, ensure_ascii=False))

    # --- ciclo
    def _address(self) -> Optional[Tuple[str, float]]:
        ip, ver = str(self.dev.get("ip", "auto")), str(self.dev.get("version", "auto"))
        found = self.bridge.discovery.lookup(self.dev["id"])
        if ip == "auto":
            if not found:
                return None
            ip = found[0]
        if ver == "auto":
            ver = found[1] if found else "3.3"
        return ip, float(ver)

    @staticmethod
    def _error(res: Any) -> Optional[str]:
        if isinstance(res, dict) and ("Error" in res or "Err" in res):
            return f"{res.get('Err')} {res.get('Error')}"
        return None

    def _session(self, ip: str, ver: float) -> None:
        d = tinytuya.Device(self.dev["id"], ip, self.dev["local_key"], version=ver,
                            persist=True, connection_timeout=5, connection_retry_limit=1)
        d.set_socketTimeout(1)
        try:
            st = d.status()
            if self._error(st):
                raise ConnectionError(self._error(st))
            self._merge(st)
            self._set_online(True)  # pubblica anche lo stato
            last_poll = last_hb = time.time()
            while not _stop_evt.is_set():
                try:
                    cmd = self.cmds.get_nowait()
                except queue.Empty:
                    cmd = False
                if cmd is None:
                    last_poll = 0.0
                elif cmd:
                    res = d.set_multiple_values(cmd, nowait=False)
                    if self._error(res):
                        log.warning("%s: scrittura %s fallita: %s", self.dev["topic"], cmd, self._error(res))
                    else:
                        self.dirty |= self._merge(res)
                    log.info("%s: scritto %s", self.dev["topic"], cmd)

                msg = d.receive()
                err = self._error(msg)
                # 902 = timeout di ricezione: nessun aggiornamento spontaneo, normale
                if err and not err.startswith("902"):
                    raise ConnectionError(err)
                if not err:
                    self.dirty |= self._merge(msg)

                now = time.time()
                if now - last_poll >= self.bridge.poll_s:
                    st = d.status()
                    if self._error(st):
                        raise ConnectionError(self._error(st))
                    self.dirty |= self._merge(st)
                    last_poll = last_hb = now
                elif now - last_hb >= self.bridge.heartbeat_s:
                    d.heartbeat(nowait=True)
                    last_hb = now
                self._flush()
        finally:
            d.close()

    def run(self):
        delay = self.bridge.retry_min
        while not _stop_evt.is_set():
            addr = self._address()
            if not addr:
                self._set_online(False)
                _stop_evt.wait(5)
                continue
            try:
                self._session(*addr)
                delay = self.bridge.retry_min
            except Exception as e:
                log.warning("%s (%s): %s; nuovo tentativo tra %ds",
                            self.dev["topic"], addr[0], e, delay)
                self._set_online(False)
                _stop_evt.wait(delay)
                delay = min(delay * 2, self.bridge.retry_max)

# ------------------------------- Ponte --------------------------------------

class Bridge:
    def __init__(self, cfg: Dict[str, Any], devices_file: Optional[str] = None):
        t = cfg.get("tuya") or {}
        self.base = str(t.get("base_topic", "tuya"))
        self.poll_s = float(t.get("poll_s", 60))
        self.heartbeat_s = float(t.get("heartbeat_s", 10))
        self.retry_min = float(t.get("retry_min_s", 10))
        self.retry_max = float(t.get("retry_max_s", 300))
        self.publish_raw = bool(t.get("publish_raw", False))
        self.min_publish_s = float(t.get("min_publish_s", 1))
        self.overrides: Dict[str, Dict[str, Dict[str, Any]]] = t.get("overrides") or {}
        self.deny = set(t.get("deny_codes", DEFAULT_DENY))

        m = dict(cfg.get("mqtt") or {})
        self.qos = int(m.get("qos", 0))
        # stesso broker del poller Modbus, ma client e LWT propri
        m["client_id"] = str(t.get("client_id", "tuya-mqtt-bridge"))
        m["base_topic"] = self.base
        self.mqtt_cfg = {"mqtt": m}

        devs = _load_yaml(devices_file or t.get("devices_file", "tuya-devices.yaml"))
        self.workers = {d["topic"]: TuyaWorker(d, self)
                        for d in devs.get("devices") or [] if d.get("enabled", True)}
        self.discovery = Discovery()
        self.client = None

    def publish(self, topic: str, payload: str) -> None:
        if self.client is not None:
            self.client.publish(topic, payload, qos=self.qos, retain=True)

    def _on_message(self, cli, userdata, msg) -> None:
        parts = msg.topic.split("/")
        w = self.workers.get(parts[-2]) if len(parts) >= 3 else None
        if w is None:
            return
        if parts[-1] == "set":
            w.command(msg.payload)
        elif parts[-1] == "get":
            w.refresh()

    def run(self) -> None:
        self.client = _mqtt_client(self.mqtt_cfg)
        base_on_connect = self.client.on_connect

        def on_connect(cli, *args, **kwargs):
            base_on_connect(cli, *args, **kwargs)
            # a ogni (ri)connessione: le sottoscrizioni non sopravvivono a clean_session
            cli.subscribe([(f"{self.base}/+/set", self.qos), (f"{self.base}/+/get", self.qos)])
        self.client.on_connect = on_connect
        self.client.on_message = self._on_message
        _mqtt_connect(self.client, self.mqtt_cfg)

        try:
            signal.signal(signal.SIGINT, _handle_sigterm)
            signal.signal(signal.SIGTERM, _handle_sigterm)
        except Exception:
            pass

        self.discovery.start()
        for w in self.workers.values():
            w.start()
        log.info("Ponte Tuya avviato: %d dispositivi, base topic '%s'", len(self.workers), self.base)
        _stop_evt.wait()

        for w in self.workers.values():
            w.join(timeout=3)
            self.publish(f"{w.topic}/availability", "offline")
        self.publish(f"{self.base}/status", "offline")
        self.client.loop_stop()
        self.client.disconnect()

def main():
    ap = argparse.ArgumentParser(description="Ponte Tuya Wi-Fi locale -> MQTT.")
    ap.add_argument("--config", required=True, help="config.yaml (blocchi mqtt: e tuya:)")
    ap.add_argument("--devices", help="file dispositivi (default: tuya.devices_file)")
    ap.add_argument("--log", default="INFO", help="Logging level (DEBUG, INFO, WARNING, ERROR)")
    args = ap.parse_args()

    logging.basicConfig(level=getattr(logging, args.log.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    Bridge(_load_yaml(args.config), args.devices).run()

if __name__ == "__main__":
    main()
