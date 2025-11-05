#!/usr/bin/env python3
"""
polling.py
-----------
Generic Modbus poller for multiple meter types (DDS661, SDM230) defined in a YAML config.
Publishes to MQTT (Home Assistant discovery optional).

Config (example):
-----------------
serial:
  port: /dev/ttyCOM1
  baudrate: 9600
  parity: E
  stopbits: 1
  bytesize: 8
  timeout: 1.0

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

devices:
  - id: 1
    type: dds661
    name: "Main DDS"
  - id: 5
    type: sdm230
    name: "PV Import/Export"
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

# TCP client import (pymodbus 3.x then 2.x fallback)
try:
    from pymodbus.client import ModbusTcpClient
except Exception:
    try:
        from pymodbus.client.sync import ModbusTcpClient  # type: ignore
    except Exception:
        ModbusTcpClient = None  # type: ignore


# ---- import driver libs and helpers
from dds661 import (
    DDS661, LinkConfig,
    _call_with_unit, _registers_to_float,
    IN_VOLTAGE as D_VOLT, IN_CURRENT as D_CURR, IN_P_ACT as D_PACT,
    IN_PF as D_PF, IN_FREQ as D_FREQ, IN_E_TOT as D_ETOT, IN_E_POS as D_EPOS, IN_E_REV as D_EREV,
)
from sdm230 import (
    SDM230,
    IN_VOLTAGE as S_VOLT, IN_CURRENT as S_CURR, IN_P_ACT as S_PACT,
    IN_PF as S_PF, IN_FREQ as S_FREQ, IN_E_TOT as S_ETOT, IN_E_POS as S_EPOS, IN_E_REV as S_EREV,
)

log = logging.getLogger("meters.poller")

MEAS_KEYS = ("voltage", "current", "p_active", "pf", "freq", "e_total", "e_pos", "e_rev")

# For sequential reads we need address maps per driver
ADDR_MAP = {
    "dds661": {
        "voltage": D_VOLT, "current": D_CURR, "p_active": D_PACT, "pf": D_PF, "freq": D_FREQ,
        "e_total": D_ETOT, "e_pos": D_EPOS, "e_rev": D_EREV,
    },
    "sdm230": {
        "voltage": S_VOLT, "current": S_CURR, "p_active": S_PACT, "pf": S_PF, "freq": S_FREQ,
        "e_total": S_ETOT, "e_pos": S_EPOS, "e_rev": S_EREV,
    },
}

DRIVERS = {
    "dds661": DDS661,
    "sdm230": SDM230,
}

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

    sensors = [
        ("voltage", "Voltage", "V", "voltage"),
        ("current", "Current", "A", "current"),
        ("p_active", "Active Power", "W", "power"),
        ("pf", "Power Factor", "", None),
        ("freq", "Frequency", "Hz", "frequency"),
        ("e_total", "Energy Total", "kWh", "energy"),
        ("e_pos", "Energy Import", "kWh", "energy"),
        ("e_rev", "Energy Export", "kWh", "energy"),
    ]

    for d in cfg.get("devices", []):
        unit_id = int(d["id"])
        dev_type = str(d.get("type", "dds661")).lower()
        name = d.get("name") or f"{dev_type.upper()} {unit_id}"
        unique_base = f"{dev_type}_{unit_id}"
        model = dev_type.upper()
        manufacturer = "DDS" if dev_type == "dds661" else "Eastron"
        device = _ha_device(unique_base, name, area, model, manufacturer)

        state_topic = f"{base_topic}/{_topic_key(name, unit_id)}/state"
        availability = {"topic": f"{base_topic}/status"}

        for key, label, unit, dev_class in sensors:
            comp = "sensor"
            unique_id = f"{unique_base}_{key}"
            obj_id = f"{unique_id}"
            cfg_payload = {
                "name": f"{name} {label}",
                "uniq_id": unique_id,
                "stat_t": state_topic,
                "avty": [availability],
                "val_tpl": f"{{{{ value_json.{key} | float }}}}",
                "dev": device,
            }
            if unit:
                cfg_payload["unit_of_meas"] = unit
            if dev_class:
                cfg_payload["dev_cla"] = dev_class

            topic = f"{dprefix}/{comp}/{obj_id}/config"
            client.publish(topic, json.dumps(cfg_payload, ensure_ascii=False), qos=qos, retain=retain)

# ------------------------------- Serial Link --------------------------------

def _make_link(cfg: Dict[str, Any]) -> LinkConfig:
    s = cfg.get("serial", {}) if isinstance(cfg, dict) else {}
    parity = str(s.get("parity", "E")).upper()[0]
    return LinkConfig(
        port=s.get("port", "/dev/ttyCOM1"),
        baudrate=int(s.get("baudrate", 9600)),
        parity=parity,
        stopbits=int(s.get("stopbits", 1)),
        bytesize=int(s.get("bytesize", 8)),
        timeout=float(s.get("timeout", 1.0)),
    )

# ------------------------------- TCP config -----------------------------------
def _tcp_merge(cfg: Dict[str, Any], dev: Dict[str, Any]) -> Dict[str, Any]:
    g = (cfg.get("tcp") or {}) if isinstance(cfg, dict) else {}
    d = (dev.get("tcp") or {}) if isinstance(dev, dict) else {}
    out = dict(g)
    out.update(d)
    # defaults
    out.setdefault("host", "192.168.0.99")
    out.setdefault("port", 502)
    out.setdefault("timeout", 1.0)
    return out

# ------------------------------- Reading ------------------------------------

def _read_device_bulk(dev_type: str, link: LinkConfig, unit_id: int) -> Dict[str, float]:
    cls = DRIVERS[dev_type]
    dev = cls(link, unit=unit_id)
    meas = dev.read_measurements()
    dct = asdict(meas)
    return {k: float(dct.get(k, float("nan"))) for k in MEAS_KEYS}

def _read_device_sequential(dev_type: str, link: LinkConfig, unit_id: int, per_measure_delay: float, step_log: bool=False, protocol: str='rtu', tcp: Dict[str, Any]|None=None) -> Dict[str, float]:
    out: Dict[str, float] = {}
    cls = DRIVERS[dev_type]
    addrs = ADDR_MAP[dev_type]

    # If TCP, reuse a single TCP client for the whole device pass (minimal change)
    if str(protocol).lower() == "tcp":
        if ModbusTcpClient is None:
            raise RuntimeError("pymodbus ModbusTcpClient not available")
        t = tcp or {}
        cli = ModbusTcpClient(host=t.get("host","192.168.0.99"), port=int(t.get("port",502)), timeout=float(t.get("timeout",1.0)))
        try:
            if not cli.connect():
                raise RuntimeError("tcp connect failed")
            for name in MEAS_KEYS:
                addr = addrs[name]
                val = float("nan")
                try:
                    rr = _call_with_unit(cli.read_input_registers, address=addr, count=2, unit_id=unit_id)
                    if hasattr(rr, "isError") and rr.isError():
                        val = float("nan")
                    else:
                        regs = (rr.registers[0], rr.registers[1])
                        val = _registers_to_float(regs)
                except Exception as e:
                    log.error("Unit %s (%s/TCP) read '%s' failed: %s", unit_id, dev_type, name, e)
                    val = float("nan")
                out[name] = val
                if step_log:
                    log.info("read-step device=%s type=%s %s=%s", unit_id, dev_type, name, val)
                if per_measure_delay > 0:
                    time.sleep(per_measure_delay)
        finally:
            try:
                cli.close()
            except Exception:
                pass
        return out

    # RTU path (unchanged semantics): fresh client per measurement
    dummy = cls(link, unit=unit_id)  # just to reuse the client's transport config
    for name in MEAS_KEYS:
        addr = addrs[name]
        cli = dummy._make_client()  # type: ignore[attr-defined]
        val = float("nan")
        try:
            if not cli.connect():
                raise RuntimeError("serial open failed")
            rr = _call_with_unit(cli.read_input_registers, address=addr, count=2, unit_id=unit_id)
            if hasattr(rr, "isError") and rr.isError():
                val = float("nan")
            else:
                regs = (rr.registers[0], rr.registers[1])
                val = _registers_to_float(regs)
        except Exception as e:
            log.error("Unit %s (%s) read '%s' failed: %s", unit_id, dev_type, name, e)
            val = float("nan")
        finally:
            try:
                cli.close()
            except Exception:
                pass
        out[name] = val
        if step_log:
            log.info("read-step device=%s type=%s %s=%s", unit_id, dev_type, name, val)
        if per_measure_delay > 0:
            time.sleep(per_measure_delay)
    return out

# ------------------------------- Polling ------------------------------------

_stop_evt = threading.Event()

def _handle_sigterm(signum, frame):
    _stop_evt.set()

def _poll_once(client: mqtt.Client, cfg: Dict[str, Any], link: LinkConfig) -> None:
    m = cfg.get("mqtt", {}) if isinstance(cfg, dict) else {}
    base_topic = m.get("base_topic", "energy")
    qos = int(m.get("qos", 0))
    retain = bool(m.get("retain", True))

    p = cfg.get("polling", {}) if isinstance(cfg, dict) else {}
    mode = str(p.get("read_mode", "sequential")).lower()
    per_measure_delay_ms = int(p.get("per_measure_delay_ms", 50))
    per_measure_delay = max(0.0, per_measure_delay_ms / 1000.0)
    debug_log = bool(p.get("debug_log", False))
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
            protocol = str(d.get("protocol","rtu")).lower()
            if dev_type not in DRIVERS:
                log.error("Unsupported device type '%s' for id=%s", dev_type, unit_id)
                continue
            name = d.get("name") or f"{dev_type.upper()} {unit_id}"

            # If TCP is selected for this device, force the sequential/TCP path (minimal change)
            if protocol == "tcp":
                tcp = _tcp_merge(cfg, d)
                vals = _read_device_sequential(dev_type, link, unit_id, per_measure_delay, step_log=debug_log, protocol="tcp", tcp=tcp)
            else:
                if mode == "bulk":
                    vals = _read_device_bulk(dev_type, link, unit_id)
                else:
                    vals = _read_device_sequential(dev_type, link, unit_id, per_measure_delay, step_log=debug_log)

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
    link = _make_link(cfg)
    client = _mqtt_client(cfg)
    _mqtt_connect(client, cfg)

    # HA discovery
    _ha_publish_discovery(client, cfg)

    period_s = float((cfg.get("polling") or {}).get("period_s", 5))
    if oneshot:
        _poll_once(client, cfg, link)
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
        _poll_once(client, cfg, link)
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
