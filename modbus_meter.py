
# modbus_meter.py
# CLI wrapper that uses the high-level library dds661.py

from __future__ import annotations
import argparse, json, sys

try:
    import yaml  # optional, only needed if --config is used
    _HAS_YAML = True
except Exception:
    _HAS_YAML = False

from dds661 import DDS661, LinkConfig

def _load_config(path: str) -> dict:
    if not _HAS_YAML:
        raise SystemExit("ERROR: PyYAML not installed. Install with: pip install pyyaml")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def _link_from_args(args, cfg: dict | None) -> LinkConfig:
    # If --config provided, use those defaults and let CLI override them
    kw = {}
    if cfg:
        s = (cfg.get("serial") or {}) if isinstance(cfg, dict) else {}
        if "port" in s: kw["port"] = s["port"]
        if "baudrate" in s: kw["baudrate"] = int(s["baudrate"])
        if "parity" in s: kw["parity"] = str(s["parity"]).upper()
        if "stopbits" in s: kw["stopbits"] = int(s["stopbits"])
        if "bytesize" in s: kw["bytesize"] = int(s["bytesize"])
        if "timeout" in s: kw["timeout"] = float(s["timeout"])
    # CLI overrides
    if args.port: kw["port"] = args.port
    if args.baudrate: kw["baudrate"] = args.baudrate
    if args.parity: kw["parity"] = args.parity
    if args.stopbits: kw["stopbits"] = args.stopbits
    if args.bytesize: kw["bytesize"] = args.bytesize
    if args.timeout is not None: kw["timeout"] = args.timeout
    return LinkConfig(**kw)

def main():
    ap = argparse.ArgumentParser(description="DDS661 Modbus RTU CLI (float32, ABCD order).")
    ap.add_argument("--config", help="Optional YAML config with 'serial:' defaults.", default=None)

    # Serial link options
    ap.add_argument("--port", default=None)
    ap.add_argument("--baudrate", type=int, default=None)
    ap.add_argument("--parity", choices=["E", "O", "N"], default=None)
    ap.add_argument("--stopbits", type=int, default=1)
    ap.add_argument("--bytesize", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=1.0)

    # Unit id
    ap.add_argument("--slave", type=int, default=1, help="Current Modbus unit ID")

    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("read", help="Read params (holding) and main measurements (input).")

    w = sub.add_parser("write", help="Write params (only changed).")
    w.add_argument("--baud", type=float, help="New baud rate (float), e.g., 9600")
    w.add_argument("--parity-new", type=float, help="New device parity: 0=Even, 1=Odd, 2=None")
    w.add_argument("--slave-new", type=float, help="New Modbus unit id 1..247")

    args = ap.parse_args()

    cfg = _load_config(args.config) if args.config else None
    link = _link_from_args(args, cfg)
    dev = DDS661(link, unit=args.slave)

    if args.cmd == "read":
        params = dev.read_params()
        meas = dev.read_measurements()
        print(json.dumps({"params": params.__dict__, "measurements": meas.__dict__}, indent=2))
        sys.exit(0)

    if args.cmd == "write":
        rep = dev.write_params(baud=args.baud, parity=args.parity_new, slave=args.slave_new)
        print(json.dumps({"report": rep, "note": [
            "If SLAVE changed, re-run with --slave <new>",
            "If PARITY/BAUD changed, reconnect with new serial settings"
        ]}, indent=2))

if __name__ == "__main__":
    main()
