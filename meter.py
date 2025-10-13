#!/usr/bin/env python3
"""
meter.py
---------
Generic CLI for Modbus meters (DDS661, SDM230). Uses YAML config to resolve device type by ID.

Examples:
  python3 meter.py --config config.yaml --slave 5 read
  python3 meter.py --config config.yaml --slave 5 write --baud 9600 --parity-new 0 --slave-new 5

CLI precedence:
- --type can explicitly select a driver (dds661/sdm230)
- Else, we try to match --slave in config.devices[].id and read 'type' from there
- Else, fallback to 'dds661' for backward compatibility
"""
from __future__ import annotations

import argparse, json, sys
from typing import Dict, Any, Optional

try:
    import yaml
    _HAS_YAML = True
except Exception:
    _HAS_YAML = False

from dds661 import DDS661, LinkConfig
from sdm230 import SDM230

def _load_config(path: str) -> dict:
    if not _HAS_YAML:
        raise SystemExit("ERROR: PyYAML not installed. Install with: pip install pyyaml")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def _link_from_cfg_and_args(args, cfg: dict | None) -> LinkConfig:
    kw = {}
    if cfg:
        s = (cfg.get("serial") or {}) if isinstance(cfg, dict) else {}
        if "port" in s: kw["port"] = s["port"]
        if "baudrate" in s: kw["baudrate"] = int(s["baudrate"])
        if "parity" in s: kw["parity"] = str(s["parity"]).upper()
        if "stopbits" in s: kw["stopbits"] = int(s["stopbits"])
        if "bytesize" in s: kw["bytesize"] = int(s["bytesize"])
        if "timeout" in s: kw["timeout"] = float(s["timeout"])
    if args.port: kw["port"] = args.port
    if args.baudrate: kw["baudrate"] = args.baudrate
    if args.parity: kw["parity"] = args.parity
    if args.stopbits: kw["stopbits"] = args.stopbits
    if args.bytesize: kw["bytesize"] = args.bytesize
    if args.timeout is not None: kw["timeout"] = args.timeout
    return LinkConfig(**kw)

def _resolve_type(args, cfg: dict | None) -> str:
    if args.type:
        return args.type.lower()
    if cfg and args.slave is not None:
        for d in (cfg.get("devices") or []):
            try:
                if int(d.get("id")) == int(args.slave):
                    t = str(d.get("type", "dds661")).lower()
                    return t
            except Exception:
                continue
    return "dds661"

def main():
    ap = argparse.ArgumentParser(description="Generic Modbus RTU CLI (DDS661, SDM230).")
    ap.add_argument("--config", help="Optional YAML config with 'serial:' defaults and 'devices:' list.", default=None)

    # Serial link options
    ap.add_argument("--port", default=None)
    ap.add_argument("--baudrate", type=int, default=None)
    ap.add_argument("--parity", choices=["E", "O", "N"], default=None)
    ap.add_argument("--stopbits", type=int, default=1)
    ap.add_argument("--bytesize", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=1.0)

    ap.add_argument("--slave", type=int, default=1, help="Current Modbus unit ID")
    ap.add_argument("--type", choices=["dds661", "sdm230"], help="Override meter type (default is read from config)")

    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("read", help="Read params (holding) and main measurements (input).")

    w = sub.add_parser("write", help="Write params (only changed).")
    w.add_argument("--baud", type=float, help="New baud rate (e.g., 9600). For SDM230 will be mapped to enum.")
    w.add_argument("--parity-new", type=float, help="New parity code (device-specific).")
    w.add_argument("--slave-new", type=float, help="New Modbus unit id 1..247")

    args = ap.parse_args()

    cfg = _load_config(args.config) if args.config else None
    link = _link_from_cfg_and_args(args, cfg)
    dev_type = _resolve_type(args, cfg)

    if dev_type == "sdm230":
        dev = SDM230(link, unit=args.slave)
        manufacturer = "Eastron"
    else:
        dev = DDS661(link, unit=args.slave)
        manufacturer = "DDS"

    if args.cmd == "read":
        params = dev.read_params()
        meas = dev.read_measurements()
        out = {
            "device": {"type": dev_type, "manufacturer": manufacturer, "unit": args.slave},
            "params": getattr(params, "__dict__", dict(params)) if hasattr(params, "__dict__") else params,
            "measurements": getattr(meas, "__dict__", dict(meas)) if hasattr(meas, "__dict__") else meas,
        }
        print(json.dumps(out, indent=2))
        sys.exit(0)

    if args.cmd == "write":
        rep = dev.write_params(baud=args.baud, parity=args.parity_new, slave=args.slave_new)
        note = []
        if args.slave_new is not None:
            note.append("If SLAVE changed, re-run with --slave <new>")
        if args.baud is not None or args.parity_new is not None:
            note.append("If PARITY/BAUD changed, reconnect with new serial settings")
        print(json.dumps({"report": rep, "note": note}, indent=2))

if __name__ == "__main__":
    main()
