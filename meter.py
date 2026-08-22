#!/usr/bin/env python3
"""
meter.py
---------
CLI generica per dispositivi Modbus (contatori DDS661/SDM230, lettore temperature
DS18B20-RS485, modulo I/O MCM260) su seriale, Modbus TCP o RTU-over-TCP. Tipo e
trasporto sono risolti dal YAML di config, con override da CLI.

Esempi:
  # device gia' descritto nel config
  python3 meter.py --config config.yaml --device "Contatore Cucina" read
  python3 meter.py --config config.yaml --slave 5 read

  # dietro un USR-DR302 in modalita' trasparente
  python3 meter.py --config config.yaml --slave 20 --protocol rtutcp --host 192.168.1.177 read

  # dispositivo senza datasheet: dump dei registri con le interpretazioni possibili
  python3 meter.py --config config.yaml --device "Temperature Casa" probe

  python3 meter.py --config config.yaml --slave 5 write --baud 9600 --slave-new 5

Selezione del dispositivo:
- --device <nome> lo sceglie per nome: e' la forma non ambigua, da preferire
- altrimenti --slave cerca in config.devices[].id, ristretto da --type/--link se dati
- --type forza il driver, --protocol/--link/--host/--port-tcp forzano il trasporto
"""
from __future__ import annotations

import argparse, json, sys
from typing import Any, Dict, List, Optional

try:
    import yaml
    _HAS_YAML = True
except Exception:
    _HAS_YAML = False

from transport import ModbusSession, TransportConfig, registers_to_float, resolve_transport
from drivers import DRIVERS


def _load_config(path: str) -> dict:
    if not _HAS_YAML:
        raise SystemExit("ERROR: PyYAML not installed. Install with: pip install pyyaml")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ----------------------------- Selezione device ------------------------------

def _dev_entry(args, cfg: dict | None) -> Dict[str, Any]:
    """Voce di config.devices[] scelta da --device oppure da --slave (+ --type/--link).

    Con piu' gateway lo stesso unit id puo' comparire su bus diversi: in quel caso
    scegliere il primo che capita significa parlare al dispositivo sbagliato, quindi
    l'ambiguita' va segnalata invece di essere risolta a caso.
    """
    devices = ((cfg or {}).get("devices") or [])

    if args.device:
        named = [dict(d) for d in devices if str(d.get("name", "")) == args.device]
        if not named:
            names = ", ".join(repr(str(d.get("name"))) for d in devices) or "(nessuno)"
            raise SystemExit(f"ERROR: nessun device di nome {args.device!r}. Disponibili: {names}")
        if len(named) > 1:
            raise SystemExit(f"ERROR: piu' device si chiamano {args.device!r}: rinominarli")
        return named[0]

    matches: List[Dict[str, Any]] = []
    for d in devices:
        try:
            if int(d.get("id")) != int(args.slave):
                continue
        except Exception:
            continue
        if args.type and str(d.get("type", "dds661")).lower() != args.type.lower():
            continue
        if args.link and str(d.get("link", "")) != args.link:
            continue
        matches.append(dict(d))

    if len(matches) > 1:
        detail = "; ".join(
            f"{d.get('name')!r} (type={d.get('type')}, link={d.get('link', 'seriale')})"
            for d in matches)
        raise SystemExit(
            f"ERROR: l'unit id {args.slave} corrisponde a piu' device: {detail}.\n"
            f"       Disambiguare con --device <nome>, oppure con --type/--link.")
    return matches[0] if matches else {}


def _resolve_type(args, dev: Dict[str, Any]) -> str:
    dev_type = (args.type or str(dev.get("type", "dds661"))).lower()
    if dev_type not in DRIVERS:
        raise SystemExit(f"ERROR: tipo '{dev_type}' non supportato (attesi: {', '.join(sorted(DRIVERS))})")
    return dev_type


def _resolve_transport(args, cfg: dict | None, dev: Dict[str, Any]) -> TransportConfig:
    """Trasporto dal config, con gli override della CLI applicati come chiavi del device."""
    dev = dict(dev)
    if args.link:      dev["link"] = args.link
    if args.protocol:  dev["protocol"] = args.protocol
    if args.host:      dev["host"] = args.host
    if args.port_tcp:  dev["tcp_port"] = args.port_tcp
    if args.port:      dev["serial_port"] = args.port
    if args.baudrate:  dev["baudrate"] = args.baudrate
    if args.parity:    dev["parity"] = args.parity
    if args.stopbits is not None: dev["stopbits"] = args.stopbits
    if args.bytesize is not None: dev["bytesize"] = args.bytesize
    if args.timeout is not None:  dev["timeout"] = args.timeout
    try:
        return resolve_transport(cfg or {}, dev)
    except ValueError as e:
        raise SystemExit(f"ERROR: {e}")


# ---------------------------------- Probe ------------------------------------

# Intervalli dove i moduli Modbus economici tengono dati e configurazione.
PROBE_RANGES = ((0x0000, 32), (0x00F0, 16))


def _read_range(ses, base: int, count: int, unit: int, input_registers: bool):
    """Legge un intervallo, degradando a registro singolo se il blocco viene rifiutato.

    Un modulo economico espone spesso pochi registri sparsi: un blocco che sborda oltre
    l'ultimo esistente si becca 'illegal data address' e non dice nulla su cosa ci sia
    dentro. Leggendo uno per uno si scopre esattamente quali indirizzi rispondono, che
    e' il punto del probe.
    """
    try:
        regs = ses.read_block(base, count, unit, input_registers=input_registers)
        return {base + i: r for i, r in enumerate(regs)}, "lettura a blocco", None
    except Exception as block_err:
        pass

    values, refused, silent = {}, 0, 0
    for addr in range(base, base + count):
        try:
            values[addr] = ses.read_block(addr, 1, unit, input_registers=input_registers)[0]
        except Exception as e:
            if "in errore" in str(e):
                refused += 1        # il device ha risposto: indirizzo inesistente
            else:
                silent += 1         # nessuna risposta: linea o indirizzo sbagliato
    how = (f"blocco rifiutato -> letti uno per uno: {len(values)} esistono, "
           f"{refused} inesistenti, {silent} senza risposta")
    return values, how, (block_err if not values and not refused else None)


def _interpretations(values: Dict[int, int], addr: int) -> str:
    """Le letture plausibili dello stesso registro, affiancate."""
    raw = values[addr] & 0xFFFF
    signed = raw - 0x10000 if raw >= 0x8000 else raw
    out = [f"u16={raw:<6d}", f"i16={signed:<7d}", f"i16*0.1={signed / 10:<8.1f}"]
    nxt = values.get(addr + 1)
    if nxt is not None:
        try:
            out.append(f"f32={registers_to_float((raw, nxt)):.6g}")
        except Exception:
            pass
    if raw == 0x8000:
        out.append("<- 0x8000: sonda assente?")
    return "  ".join(out)


def cmd_probe(tc: TransportConfig, unit: int) -> None:
    """Dump dei registri con le interpretazioni possibili.

    Serve quando manca il datasheet: si guarda quale colonna produce valori sensati
    (temperature plausibili, l'unit id noto, un codice baud) e da li' si ricostruisce
    la mappa. Sola lettura.
    """
    print(f"# probe unit={unit} via {tc.describe()}")
    answered = False
    with ModbusSession(tc, label=f"probe unit={unit}") as ses:
        for input_registers in (False, True):
            fc = "FC04 input" if input_registers else "FC03 holding"
            for base, count in PROBE_RANGES:
                rng = f"0x{base:04X}..0x{base + count - 1:04X}"
                values, how, dead = _read_range(ses, base, count, unit, input_registers)
                if dead is not None:
                    print(f"\n## {fc} {rng}: nessuna risposta ({dead})")
                    continue
                answered = True
                shown = [a for a in sorted(values) if values[a] != 0]
                print(f"\n## {fc} {rng}  [{how}]"
                      f"  -  {len(shown)} registri non nulli su {len(values)} leggibili")
                for a in shown:
                    print(f"   0x{a:04X}  0x{values[a]:04X}  {_interpretations(values, a)}")
                if values and not shown:
                    print("   tutti zero")
    if not answered:
        print("\nNessun function code ha risposto: il problema e' la linea "
              "(baud/parita' del gateway, indirizzo, cablaggio A/B), non la mappa registri.")


# ----------------------------------- Main ------------------------------------

def main():
    ap = argparse.ArgumentParser(description="CLI Modbus generica (DDS661, SDM230, DS18B20-RS485, MCM260).")
    ap.add_argument("--config", help="YAML con i default 'serial:'/'tcp:'/'links:' e la lista 'devices:'.", default=None)

    # Linea seriale (override del blocco serial: del config)
    ap.add_argument("--port", default=None, help="Device seriale, es. /dev/ttyUSB0")
    ap.add_argument("--baudrate", type=int, default=None)
    ap.add_argument("--parity", choices=["E", "O", "N"], default=None)
    ap.add_argument("--stopbits", type=int, default=None)
    ap.add_argument("--bytesize", type=int, default=None)
    ap.add_argument("--timeout", type=float, default=None)

    ap.add_argument("--device", default=None, help="Nome del device nel config (selezione non ambigua)")
    ap.add_argument("--slave", type=int, default=1, help="Unit ID Modbus")
    ap.add_argument("--type", choices=sorted(DRIVERS), help="Forza il tipo di dispositivo (default: dal config)")

    # Trasporto
    ap.add_argument("--protocol", choices=["rtu", "tcp", "rtutcp"], default=None,
                    help="rtu (seriale), tcp (Modbus TCP/MBAP), rtutcp (RTU dentro TCP: gateway trasparente)")
    ap.add_argument("--link", default=None, help="Nome di un trasporto definito nel blocco links: del config")
    ap.add_argument("--host", default=None, help="Host TCP (override di config)")
    ap.add_argument("--port-tcp", type=int, default=None, help="Porta TCP (override di config; default 502)")

    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("read", help="Legge parametri e misure.")
    sub.add_parser("probe", help="Dump dei registri con le interpretazioni possibili (sola lettura).")

    w = sub.add_parser("write", help="Scrive i parametri (solo quelli cambiati).")
    w.add_argument("--baud", type=float, help="Nuovo baud rate (es. 9600). Mappato sul codice enum del device.")
    w.add_argument("--parity-new", type=float, help="Nuovo codice parita' (dipende dal dispositivo).")
    w.add_argument("--slave-new", type=float, help="Nuovo unit id Modbus 1..247")

    args = ap.parse_args()

    cfg = _load_config(args.config) if args.config else None
    dev_entry = _dev_entry(args, cfg)
    if args.device:
        args.slave = int(dev_entry.get("id", args.slave))
    dev_type = _resolve_type(args, dev_entry)
    tc = _resolve_transport(args, cfg, dev_entry)

    if args.cmd == "probe":
        cmd_probe(tc, args.slave)
        return

    cls = DRIVERS[dev_type]
    dev = cls(tc, unit=args.slave, dev_cfg=dev_entry)
    ident = {"type": dev_type, "model": cls.MODEL, "manufacturer": cls.MANUFACTURER,
             "unit": args.slave, "name": dev_entry.get("name"), "transport": tc.as_dict()}

    if args.cmd == "read":
        try:
            params = dev.read_params()
        except Exception as e:
            # Le misure restano leggibili anche se i parametri no (holding negati dal
            # gateway, mappa diversa dal previsto): meglio riportare l'errore che abortire.
            params = {"error": str(e)}
        values = dev.read_values(dev_entry)
        labels = {m.key: m.label for m in cls.measures(dev_entry)}
        print(json.dumps({
            "device": ident,
            "params": getattr(params, "__dict__", params),
            "measurements": values,
            "labels": labels,
        }, indent=2, ensure_ascii=False))
        return

    if args.cmd == "write":
        out: Dict[str, Any] = {"device": ident}
        out["report"] = dev.write_params(baud=args.baud, parity=args.parity_new,
                                         slave=args.slave_new)
        note = []
        if args.slave_new is not None:
            note.append("Se lo SLAVE e' cambiato, rilanciare con --slave <nuovo>")
        if args.baud is not None or args.parity_new is not None:
            note.append("Se BAUD/PARITY sono cambiati, allineare la linea (gateway o porta seriale)")
        out["note"] = note
        print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
