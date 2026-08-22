#!/usr/bin/env python3
"""
polling.py
-----------
Generic Modbus poller for multiple meter types (DDS661, SDM230) defined in a YAML config.
Publishes to MQTT (Home Assistant discovery optional).

Config (example):
-----------------
serial:                    # default per i device su seriale locale (protocol: rtu)
  port: /dev/ttyCOM1
  baudrate: 9600
  parity: E
  stopbits: 1
  bytesize: 8
  timeout: 1.0

tcp:                       # default per i device Modbus TCP (protocol: tcp)
  host: 192.168.0.99
  port: 502
  timeout: 1.0

links:                     # trasporti con nome, referenziati da 'link:' nel device
  dr302:                   # USR-DR302 in modalita' trasparente (RTU dentro TCP)
    protocol: rtutcp
    host: 192.168.1.177
    port: 502
    timeout: 1.5

mqtt:
  host: 127.0.0.1
  port: 1883
  client_id: "meters-poller"
  base_topic: "energy"
  qos: 0
  retain: true
  tls:
    enabled: false

home_assistant:
  enabled: true
  discovery_prefix: homeassistant
  area: "Lab"

polling:
  read_mode: sequential      # or "bulk"
  per_measure_delay_ms: 50
  delay_ms_between_devices: 0
  period_s: 5
  debug_log: false
  reconnect_each_read: false # true = riapre la connessione a ogni misura (bus instabili)

devices:
  - id: 1
    type: dds661
    name: "Main DDS"         # nessun protocol/link -> rtu sul blocco serial: globale
  - id: 5
    type: sdm230
    name: "PV Import/Export"
  - id: 20
    type: sdm230
    name: "Behind gateway"
    link: dr302              # -> trasporto definito in links:
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
from dataclasses import asdict
from typing import Any, Dict, Optional, Tuple, List

import yaml
import paho.mqtt.client as mqtt

# ---- trasporto unificato (rtu / tcp / rtutcp) e driver
from transport import ModbusSession, TransportConfig, resolve_transport
from drivers import DRIVERS

log = logging.getLogger("meters.poller")

# Le grandezze non sono piu' un elenco fisso: ogni driver dichiara le proprie
# tramite measures(dev_cfg), e lettura e discovery HA seguono quelle.

def _driver(dev_type: str, tc: TransportConfig, unit_id: int, dev: Optional[Dict[str, Any]] = None):
    return DRIVERS[dev_type](tc, unit=unit_id, dev_cfg=dev)

def _device_uid(dev: Dict[str, Any], dev_type: str, unit_id: int) -> str:
    """Identita' del dispositivo per Home Assistant.

    Il default storico e' '{tipo}_{id}', ma con piu' gateway l'unit id non basta:
    due moduli identici in stanze diverse possono avere lo stesso indirizzo. La
    chiave 'uid:' permette di disambiguarli senza toccare le entita' esistenti.
    """
    return str(dev.get("uid") or f"{dev_type}_{unit_id}")

def _validate_devices(cfg: Dict[str, Any]) -> None:
    """Segnala tipi sconosciuti e uid duplicati all'avvio, invece di lasciare che
    le entita' HA si sovrascrivano in silenzio."""
    seen: Dict[str, str] = {}
    for d in (cfg.get("devices") or []):
        dev_type = str(d.get("type", "dds661")).lower()
        name = d.get("name") or f"{dev_type.upper()} {d.get('id')}"
        if dev_type not in DRIVERS:
            log.error("Tipo '%s' sconosciuto per il device '%s' (attesi: %s)",
                      dev_type, name, ", ".join(sorted(DRIVERS)))
            continue
        uid = _device_uid(d, dev_type, int(d.get("id", 0)))
        if uid in seen:
            log.error("uid duplicato '%s': '%s' e '%s' si sovrascriverebbero in Home "
                      "Assistant. Assegnare un 'uid:' esplicito a uno dei due.",
                      uid, seen[uid], name)
        else:
            seen[uid] = name

def _slugify_name(name: str) -> str:
    import re, unicodedata
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s

def _topic_key(name: Optional[str], unit_id: int) -> str:
    slug = _slugify_name(name or "")
    return slug if slug else str(unit_id)

# ------------------------------- YAML ---------------------------------------

def _load_yaml(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

# ------------------------------- MQTT ---------------------------------------

def _mqtt_client(cfg: Dict[str, Any]) -> mqtt.Client:
    m = cfg.get("mqtt", {}) if isinstance(cfg, dict) else {}
    client_id = m.get("client_id", "meters-poller")
    base_topic = m.get("base_topic", "energy")
    qos = int(m.get("qos", 0))

    try:
        import paho.mqtt as paho_mod
        ver_str = getattr(paho_mod, "__version__", "2.0.0")
        major = int(str(ver_str).split(".")[0])
    except Exception:
        major = 2

    if major >= 2:
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv311,
            transport="tcp",
        )
        def on_connect(cli, userdata, flags, reason_code, properties=None):
            code = getattr(reason_code, "value", reason_code)
            if code == 0:
                log.info("Connected to MQTT broker")
                cli.publish(f"{base_topic}/status", payload="online", qos=qos, retain=True)
            else:
                log.error("MQTT connect failed rc=%s", reason_code)
        client.on_connect = on_connect
    else:
        client = mqtt.Client(client_id=client_id, clean_session=True)
        def on_connect(cli, userdata, flags, rc):
            if int(rc) == 0:
                log.info("Connected to MQTT broker")
                cli.publish(f"{base_topic}/status", payload="online", qos=qos, retain=True)
            else:
                log.error("MQTT connect failed rc=%s", rc)
        client.on_connect = on_connect

    client.will_set(f"{base_topic}/status", payload="offline", qos=qos, retain=True)

    if m.get("username"):
        client.username_pw_set(m["username"], m.get("password") or None)

    tls = m.get("tls", {})
    try:
        if isinstance(tls, bool):
            if tls:
                client.tls_set()
        elif tls and tls.get("enabled"):
            client.tls_set(
                ca_certs=tls.get("ca_certs") or None,
                certfile=tls.get("certfile") or None,
                keyfile=tls.get("keyfile") or None,
            )
    except Exception as e:
        log.error("TLS setup failed: %s", e)

    return client

def _mqtt_connect(client: mqtt.Client, cfg: Dict[str, Any]) -> None:
    m = cfg.get("mqtt", {}) if isinstance(cfg, dict) else {}
    host = m.get("host", "127.0.0.1")
    port = int(m.get("port", 1883))
    keepalive = int(m.get("keepalive", 60))
    client.connect(host, port, keepalive)
    client.loop_start()

# ------------------------------- Topic Helpers -------------------------------

def _device_topic(base_topic: str, dev_name: str, cfg: Dict[str, Any]) -> str:
    m = cfg.get("mqtt", {}) if isinstance(cfg, dict) else {}
    style = str(m.get("topic_style", "flat")).lower()
    slug = _slugify_name(dev_name)
    if style == "state":
        return f"{base_topic}/{slug}/state"
    elif style == "measurements":
        return f"{base_topic}/{slug}/measurements"
    else:
        return f"{base_topic}/{slug}"

# ------------------------------- HA Discovery -------------------------------

def _ha_device(unique: str, name: str, area: Optional[str], model: str, manufacturer: str) -> Dict[str, Any]:
    dev = {
        "identifiers": [unique],
        "manufacturer": manufacturer,
        "model": model,
        "name": name,
    }
    if area:
        dev["area"] = area
    return dev

def _ha_publish_discovery(client: mqtt.Client, cfg: Dict[str, Any]) -> None:
    ha = (cfg.get("home_assistant") or {}) if isinstance(cfg, dict) else {}
    if not ha or not ha.get("enabled"):
        return

    m = cfg.get("mqtt", {}) if isinstance(cfg, dict) else {}
    base_topic = m.get("base_topic", "energy")
    qos = int(m.get("qos", 0))
    retain = bool(m.get("retain", True))
    dprefix = ha.get("discovery_prefix", "homeassistant")
    area = ha.get("area")

    for d in cfg.get("devices", []):
        unit_id = int(d["id"])
        dev_type = str(d.get("type", "dds661")).lower()
        if dev_type not in DRIVERS:
            continue
        cls = DRIVERS[dev_type]
        name = d.get("name") or f"{dev_type.upper()} {unit_id}"
        unique_base = _device_uid(d, dev_type, unit_id)
        device = _ha_device(unique_base, name, area, cls.MODEL, cls.MANUFACTURER)

        state_topic = f"{base_topic}/{_topic_key(name, unit_id)}/state"
        availability = {"topic": f"{base_topic}/status"}

        for m in cls.measures(d):
            comp = m.component
            unique_id = f"{unique_base}_{m.key}"
            obj_id = f"{unique_id}"
            binary = comp == "binary_sensor"
            cfg_payload = {
                "name": f"{name} {m.label}",
                "uniq_id": unique_id,
                "stat_t": state_topic,
                "avty": [availability],
                # I binary_sensor pubblicano 0/1 interi e si confrontano con pl_on/pl_off;
                # i sensori numerici passano da '| float'.
                "val_tpl": (f"{{{{ value_json.{m.key} }}}}" if binary
                            else f"{{{{ value_json.{m.key} | float }}}}"),
                "dev": device,
            }
            if binary:
                cfg_payload["pl_on"] = "1"
                cfg_payload["pl_off"] = "0"
            if m.unit:
                cfg_payload["unit_of_meas"] = m.unit
            if m.device_class:
                cfg_payload["dev_cla"] = m.device_class
            if m.state_class:
                cfg_payload["stat_cla"] = m.state_class

            topic = f"{dprefix}/{comp}/{obj_id}/config"
            client.publish(topic, json.dumps(cfg_payload, ensure_ascii=False), qos=qos, retain=retain)

# ------------------------------- Reading ------------------------------------
# Il trasporto (rtu / tcp / rtutcp) e' risolto per dispositivo da
# transport.resolve_transport(): vedi il docstring del modulo per i blocchi di config.

def _read_device_bulk(dev_type: str, tc: TransportConfig, unit_id: int,
                      dev: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    """Delega al driver, che puo' leggere a blocco i registri contigui."""
    return _driver(dev_type, tc, unit_id, dev).read_values(dev)

def _read_device_sequential(dev_type: str, tc: TransportConfig, unit_id: int,
                            per_measure_delay: float, dev: Optional[Dict[str, Any]] = None,
                            step_log: bool = False,
                            reconnect_each_read: bool = False) -> Dict[str, float]:
    """Legge le grandezze una per una riusando una sola connessione per passata.

    Vale per tutti i trasporti: su rtutcp la ModbusSession riapre il socket dopo un
    errore, perche' un gateway trasparente non ha transaction id e uno stream
    desincronizzato farebbe fallire tutte le letture successive.
    """
    out: Dict[str, Any] = {}
    where = tc.describe()
    drv = _driver(dev_type, tc, unit_id, dev)

    with ModbusSession(tc, reconnect_each_read=reconnect_each_read,
                       label=f"{dev_type} unit={unit_id} via {where}") as ses:
        for m in drv.measures(dev):
            val = float("nan")
            try:
                val = ses.read_measure(m, unit_id)
            except Exception as e:
                log.error("Unit %s (%s via %s) read '%s' failed: %s",
                          unit_id, dev_type, where, m.key, e)
            out[m.key] = val
            if step_log:
                log.info("read-step device=%s type=%s %s=%s", unit_id, dev_type, m.key, val)
            if per_measure_delay > 0:
                time.sleep(per_measure_delay)

        # Alcuni valori non si giudicano dal singolo registro: il lettore DS18B20
        # distingue "0,00 gradi" da "sonda assente" solo con la maschera di presenza.
        if hasattr(drv, "postprocess"):
            out = drv.postprocess(out, ses, unit_id)
    return out

# ------------------------------- Polling ------------------------------------

_stop_evt = threading.Event()

def _handle_sigterm(signum, frame):
    _stop_evt.set()

def _poll_once(client: mqtt.Client, cfg: Dict[str, Any]) -> None:
    m = cfg.get("mqtt", {}) if isinstance(cfg, dict) else {}
    base_topic = m.get("base_topic", "energy")
    qos = int(m.get("qos", 0))
    retain = bool(m.get("retain", True))

    p = cfg.get("polling", {}) if isinstance(cfg, dict) else {}
    default_mode = str(p.get("read_mode", "sequential")).lower()
    per_measure_delay_ms = int(p.get("per_measure_delay_ms", 50))
    per_measure_delay = max(0.0, per_measure_delay_ms / 1000.0)
    debug_log = bool(p.get("debug_log", False))
    reconnect_each_read = bool(p.get("reconnect_each_read", False))
    delay_between_devices_ms = int(p.get("delay_ms_between_devices", 0))
    delay_between_devices_s = max(0.0, delay_between_devices_ms / 1000.0)

    devices = cfg.get("devices", [])
    if not devices:
        log.warning("No devices configured; nothing to poll.")
        return

    for d in devices:
        try:
            unit_id = int(d["id"])
            dev_type = str(d.get("type", "dds661")).lower()
            if dev_type not in DRIVERS:
                log.error("Unsupported device type '%s' for id=%s", dev_type, unit_id)
                continue
            name = d.get("name") or f"{dev_type.upper()} {unit_id}"
            tc = resolve_transport(cfg, d)
            # 'read_mode' per-device: un lettore con registri contigui vuole 'bulk'
            # (una transazione) mentre i contatori restano 'sequential', nello stesso config.
            mode = str(d.get("read_mode", default_mode)).lower()
            if debug_log:
                log.info("polling device=%s type=%s transport=%s mode=%s",
                         unit_id, dev_type, tc.describe(), mode)

            if mode == "bulk":
                vals = _read_device_bulk(dev_type, tc, unit_id, d)
            else:
                vals = _read_device_sequential(dev_type, tc, unit_id, per_measure_delay, d,
                                               step_log=debug_log,
                                               reconnect_each_read=reconnect_each_read)

            payload = {
                "id": unit_id,
                "type": dev_type,
                "name": name,
                **vals,
            }

            if debug_log:
                dbg = {"measurements": {"deviceid": unit_id, "type": dev_type, **vals}}
                log.info(json.dumps(dbg, ensure_ascii=False, indent=2))

            topic = f"{base_topic}/{_topic_key(name, unit_id)}/state"
            client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=qos, retain=retain)
        except Exception as e:
            log.error("Read/publish failed for unit %s: %s", d.get("id"), e)
        finally:
            if delay_between_devices_s > 0:
                time.sleep(delay_between_devices_s)

def run_poll(cfg: Dict[str, Any], oneshot: bool = False) -> None:
    _validate_devices(cfg)
    client = _mqtt_client(cfg)
    _mqtt_connect(client, cfg)

    # HA discovery
    _ha_publish_discovery(client, cfg)

    period_s = float((cfg.get("polling") or {}).get("period_s", 5))
    if oneshot:
        _poll_once(client, cfg)
        client.loop_stop()
        client.disconnect()
        return

    # Handle signals
    try:
        signal.signal(signal.SIGINT, _handle_sigterm)
        signal.signal(signal.SIGTERM, _handle_sigterm)
    except Exception:
        pass

    log.info("Starting polling loop; period_s=%.3f", period_s)
    while not _stop_evt.is_set():
        start = time.time()
        _poll_once(client, cfg)
        elapsed = time.time() - start
        delay = max(0.0, period_s - elapsed)
        _stop_evt.wait(delay)

    client.loop_stop()
    client.disconnect()

def main():
    ap = argparse.ArgumentParser(description="Generic Modbus meters poller (DDS661, SDM230).")
    ap.add_argument("--config", required=True, help="YAML config file")
    ap.add_argument("--oneshot", action="store_true", help="Read/publish once and exit")
    ap.add_argument("--log", default="INFO", help="Logging level (DEBUG, INFO, WARNING, ERROR)")
    args = ap.parse_args()

    logging.basicConfig(level=getattr(logging, args.log.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = _load_yaml(args.config)
    run_poll(cfg, oneshot=args.oneshot)

if __name__ == "__main__":
    main()
