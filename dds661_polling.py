#!/usr/bin/env python3
"""
dds661_polling.py
------------------
Periodic poller/daemon for DDS661 meters defined in a YAML config, publishing
measurements to an MQTT broker (with optional Home Assistant discovery).

Now supports two read modes:
  - bulk        : single session per device using the high-level dds661.read_measurements()
  - sequential  : one Modbus transaction per measurement, with an optional delay between reads

Enable verbose per-device logging with polling.debug_log: true
Control per-measure delay with polling.per_measure_delay_ms (default 50 ms).

Requirements:
  - pyyaml
  - paho-mqtt (v2 recommended; v1 supported)
  - pymodbus >= 3,<4
  - pyserial
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
from typing import Any, Dict, Optional, Tuple, List

import yaml

# Local high-level lib (provided alongside this script)
from dds661 import (
    DDS661,
    LinkConfig,
    IN_VOLTAGE, IN_CURRENT, IN_P_ACT, IN_PF, IN_FREQ, IN_E_TOT, IN_E_POS, IN_E_REV,
    _call_with_unit, _registers_to_float,
)

import paho.mqtt.client as mqtt


log = logging.getLogger("dds661.poller")

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
    # Prefer a safe slug derived from name; fallback to unit id if empty
    slug = _slugify_name(name or "")
    return slug if slug else str(unit_id)



# ------------------------------- YAML ---------------------------------------

def _load_yaml(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ------------------------------- MQTT ---------------------------------------

def _mqtt_client(cfg: Dict[str, Any]) -> mqtt.Client:
    """Create a Paho client compatible with v1 and v2 APIs, no deprecation/Destructor warnings."""
    m = cfg.get("mqtt", {}) if isinstance(cfg, dict) else {}
    client_id = m.get("client_id", "dds661-poller")
    base_topic = m.get("base_topic", "dds661")
    qos = int(m.get("qos", 0))

    # Detect paho major version
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
        # v2 expects ReasonCodes in callbacks
        def on_connect(cli, userdata, flags, reason_code, properties=None):
            code = getattr(reason_code, "value", reason_code)
            ok = (code == 0)
            if ok:
                log.info("Connected to MQTT broker")
                cli.publish(f"{base_topic}/status", payload="online", qos=qos, retain=True)
            else:
                log.error("MQTT connect failed rc=%s", reason_code)

        client.on_connect = on_connect

    else:
        # v1 API
        client = mqtt.Client(client_id=client_id, clean_session=True)

        def on_connect(cli, userdata, flags, rc):
            ok = (int(rc) == 0)
            if ok:
                log.info("Connected to MQTT broker")
                cli.publish(f"{base_topic}/status", payload="online", qos=qos, retain=True)
            else:
                log.error("MQTT connect failed rc=%s", rc)

        client.on_connect = on_connect

    # LWT
    client.will_set(f"{base_topic}/status", payload="offline", qos=qos, retain=True)

    # Auth
    if m.get("username"):
        client.username_pw_set(m["username"], m.get("password") or None)

    # TLS (accept both bool and dict)
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

    # loop in a separate thread
    client.connect(host, port, keepalive)
    client.loop_start()



# ------------------------------- Topic Helpers -------------------------------

def _slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s or "device"

def _device_topic(base_topic: str, dev_name: str, cfg: Dict[str, Any]) -> str:
    """
    Build device topic using its name instead of ID.
    Styles (mqtt.topic_style):
      - "flat" (default):         <base>/<slug>
      - "state":                  <base>/<slug>/state
      - "measurements":           <base>/<slug>/measurements
    """
    m = cfg.get("mqtt", {}) if isinstance(cfg, dict) else {}
    style = str(m.get("topic_style", "flat")).lower()
    slug = _slugify(dev_name)
    if style == "state":
        return f"{base_topic}/{slug}/state"
    elif style == "measurements":
        return f"{base_topic}/{slug}/measurements"
    else:
        return f"{base_topic}/{slug}"
# ------------------------------- HA Discovery -------------------------------

def _ha_device(base_id: str, name: str, area: Optional[str]) -> Dict[str, Any]:
    dev = {
        "identifiers": [f"dds661_{base_id}"],
        "manufacturer": "DDS",
        "model": "DDS661",
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
    base_topic = m.get("base_topic", "dds661")
    qos = int(m.get("qos", 0))
    retain = bool(m.get("retain", True))
    dprefix = ha.get("discovery_prefix", "homeassistant")
    area = ha.get("area")

    # Sensors to expose
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
        name = d.get("name") or f"DDS661 {unit_id}"
        unique_base = f"dds661_{unit_id}"
        device = _ha_device(str(unit_id), name, area)

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
    # parity expected by our lib is 'E'/'O'/'N'
    parity = str(s.get("parity", "E")).upper()[0]
    return LinkConfig(
        port=s.get("port", "/dev/ttyCOM1"),
        baudrate=int(s.get("baudrate", 9600)),
        parity=parity,
        stopbits=int(s.get("stopbits", 1)),
        bytesize=int(s.get("bytesize", 8)),
        timeout=float(s.get("timeout", 1.0)),
    )


# ------------------------------- Reading ------------------------------------

_FIELDS: List[Tuple[str, int]] = [
    ("voltage", IN_VOLTAGE),
    ("current", IN_CURRENT),
    ("p_active", IN_P_ACT),
    ("pf", IN_PF),
    ("freq", IN_FREQ),
    ("e_total", IN_E_TOT),
    ("e_pos", IN_E_POS),
    ("e_rev", IN_E_REV),
]

def _read_device_bulk(link: LinkConfig, unit_id: int) -> Dict[str, float]:
    """Single session read using the high-level library."""
    dev = DDS661(link, unit=unit_id)
    meas = dev.read_measurements()
    return {
        "voltage": meas.voltage,
        "current": meas.current,
        "p_active": meas.p_active,
        "pf": meas.pf,
        "freq": meas.freq,
        "e_total": meas.e_total,
        "e_pos": meas.e_pos,
        "e_rev": meas.e_rev,
    }

def _read_device_sequential(link: LinkConfig, unit_id: int, per_measure_delay: float, step_log: bool=False) -> Dict[str, float]:
    """
    One Modbus transaction per measurement. Opens a client per read to maximize isolation.
    Many DDS661 variants require a short pause between reads; per_measure_delay helps stability.
    """
    out: Dict[str, float] = {}
    for name, addr in _FIELDS:
        # Make an isolated client for this single read
        dev = DDS661(link, unit=unit_id)
        cli = dev._make_client()  # intentionally use the same transport config as the lib
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
            log.error("Unit %s read '%s' failed: %s", unit_id, name, e)
            val = float("nan")
        finally:
            try:
                cli.close()
            except Exception:
                pass

        out[name] = val

        if step_log:
            log.info("read-step device=%s %s=%s", unit_id, name, val)

        # Inter-read pause
        if per_measure_delay > 0:
            time.sleep(per_measure_delay)
    return out


# ------------------------------- Polling ------------------------------------

_stop_evt = threading.Event()

def _handle_sigterm(signum, frame):
    _stop_evt.set()


def _poll_once(client: mqtt.Client, cfg: Dict[str, Any], link: LinkConfig) -> None:
    m = cfg.get("mqtt", {}) if isinstance(cfg, dict) else {}
    base_topic = m.get("base_topic", "dds661")
    qos = int(m.get("qos", 0))
    retain = bool(m.get("retain", True))

    p = cfg.get("polling", {}) if isinstance(cfg, dict) else {}
    mode = str(p.get("read_mode", "sequential")).lower()  # default to sequential for robustness
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
            name = d.get("name") or f"meter_{unit_id}"

            if mode == "bulk":
                vals = _read_device_bulk(link, unit_id)
            else:
                vals = _read_device_sequential(link, unit_id, per_measure_delay, step_log=debug_log)

            payload = {
                "id": unit_id,
                "name": name,
                **vals,
            }

            # Verbose per-device JSON log exactly as requested
            if debug_log:
                dbg = {"measurements": {"deviceid": unit_id, **vals}}
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
    _ha_publish_discovery(client, cfg)

    m = cfg.get("mqtt", {}) if isinstance(cfg, dict) else {}
    base_topic = m.get("base_topic", "dds661")

    p = cfg.get("polling", {}) if isinstance(cfg, dict) else {}
    interval = float(p.get("interval_s", 1.0))

    log.info("Starting polling loop, interval=%.1fs, base_topic=%s", interval, base_topic)

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    if oneshot:
        _poll_once(client, cfg, link)
        return

    while not _stop_evt.is_set():
        start = time.monotonic()
        _poll_once(client, cfg, link)

        # Simple pacing
        elapsed = time.monotonic() - start
        to_sleep = max(0.0, interval - elapsed)
        if _stop_evt.wait(to_sleep):
            break

    # publish offline and stop loop
    qos = int(m.get("qos", 0))
    client.publish(f"{base_topic}/status", payload="offline", qos=qos, retain=True)
    client.loop_stop()
    client.disconnect()


# --------------------------------- CLI --------------------------------------

def main():
    ap = argparse.ArgumentParser(description="DDS661 Modbus RTU poller → MQTT")
    ap.add_argument("--config", required=True, help="Path to YAML config")
    ap.add_argument("--oneshot", action="store_true", help="Run a single iteration and exit")
    ap.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    ap.add_argument("--debug-read", action="store_true", help="Force debug_log for this run")

    args = ap.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = _load_yaml(args.config)
    # Alias: support both "homeassistant" and "home_assistant"
    if isinstance(cfg, dict) and "homeassistant" in cfg and "home_assistant" not in cfg:
        cfg["home_assistant"] = cfg["homeassistant"]
    if args.debug_read:
        cfg.setdefault("polling", {})["debug_log"] = True

    run_poll(cfg, oneshot=args.oneshot)


if __name__ == "__main__":
    main()
