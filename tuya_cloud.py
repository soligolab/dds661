#!/usr/bin/env python3
"""
tuya_cloud.py
-------------
Genera tuya-devices.yaml per tuya_bridge.py leggendo UNA VOLTA il cloud Tuya:
elenco dispositivi, local key e modello dei datapoint (codice, tipo, scala, unita').
A runtime il ponte non usa il cloud: serve solo a riscrivere il file quando si
aggiunge un dispositivo o se ne riaccoppia uno (riaccoppiare cambia la local key).

Solo libreria standard, cosi' gira anche dove stanno le credenziali senza venv.

Credenziali (file KEY=VALUE, progetto Tuya IoT Platform con IoT Core attivo):
  TUYA_ACCESS_ID=...
  TUYA_ACCESS_SECRET=...
  TUYA_ENDPOINT=https://openapi.tuyaeu.com

Uso:
  python3 tuya_cloud.py --credentials ~/.tuya-credentials --out tuya-devices.yaml \\
      --topic <device_id>=termostato-soggiorno

Il file prodotto contiene le local key: resta fuori dal repository (.gitignore)
e viene scritto con permessi 600.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
from typing import Any, Dict, List


class TuyaCloud:
    def __init__(self, access_id: str, secret: str, endpoint: str):
        self.aid, self.secret, self.ep = access_id, secret, endpoint.rstrip("/")
        self.token = ""
        tok = self.get("/v1.0/token?grant_type=1")
        self.token = tok["access_token"]
        self.uid = tok.get("uid")

    def get(self, path: str) -> Any:
        t = str(int(time.time() * 1000))
        sts = "\n".join(["GET", hashlib.sha256(b"").hexdigest(), "", path])
        sign = hmac.new(self.secret.encode(), (self.aid + self.token + t + sts).encode(),
                        hashlib.sha256).hexdigest().upper()
        h = {"client_id": self.aid, "sign": sign, "t": t, "sign_method": "HMAC-SHA256"}
        if self.token:
            h["access_token"] = self.token
        with urllib.request.urlopen(urllib.request.Request(self.ep + path, headers=h),
                                    timeout=15) as r:
            res = json.load(r)
        if not res.get("success"):
            # 28841002 = IoT Core scaduto: va rinnovato dal portale Tuya
            raise RuntimeError(f"{path}: {res.get('code')} {res.get('msg')}")
        return res["result"]

    def devices(self) -> List[Dict[str, Any]]:
        out, last = [], ""
        while True:
            # un parametro vuoto invalida la firma (1004 sign invalid)
            q = f"&last_row_key={last}" if last else ""
            r = self.get(f"/v1.0/iot-01/associated-users/devices?size=100{q}")
            out += r.get("devices", [])
            if not r.get("has_more"):
                return out
            last = r.get("last_row_key", "")

    def model(self, dev_id: str) -> List[Dict[str, Any]]:
        m = json.loads(self.get(f"/v2.0/cloud/thing/{dev_id}/model")["model"])
        return [p for s in m.get("services", []) for p in s.get("properties", [])]


def _slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "-", s.strip().lower())
    return re.sub(r"-{2,}", "-", s).strip("-")


def _dp_entry(p: Dict[str, Any]) -> Dict[str, Any]:
    ts = p.get("typeSpec") or {}
    e: Dict[str, Any] = {"code": p["code"], "type": ts.get("type", "raw"),
                         "mode": p.get("accessMode", "ro")}
    if e["type"] == "value":
        for k in ("scale", "min", "max", "step", "unit"):
            if ts.get(k) not in (None, ""):
                e[k] = ts[k]
    elif e["type"] == "enum":
        e["range"] = ts.get("range", [])
    return e


def _q(v: Any) -> str:
    # JSON e' YAML valido: stringhe con apici doppi e escape, liste in forma flow
    return json.dumps(v, ensure_ascii=False)


def _yaml(devs: List[Dict[str, Any]]) -> str:
    lines = [
        "# Generato da tuya_cloud.py il " + time.strftime("%Y-%m-%d %H:%M"),
        "# CONTIENE LE LOCAL KEY: non versionare, permessi 600.",
        "# ip/version: 'auto' = presi dai broadcast UDP del dispositivo.",
        "devices:",
    ]
    for d in devs:
        lines += [
            f"  - id: {_q(d['id'])}",
            f"    name: {_q(d['name'])}",
            f"    topic: {_q(d['topic'])}",
            f"    category: {_q(d['category'])}",
            f"    product: {_q(d['product'])}",
            f"    local_key: {_q(d['local_key'])}",
            "    ip: auto",
            "    version: auto",
            "    enabled: true",
            "    dps:",
        ]
        for dp, e in sorted(d["dps"].items()):
            lines.append(f"      {dp}: {{" + ", ".join(f"{k}: {_q(v)}" for k, v in e.items()) + "}")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Genera tuya-devices.yaml dal cloud Tuya.")
    ap.add_argument("--credentials", required=True, help="file KEY=VALUE con le credenziali")
    ap.add_argument("--out", required=True, help="file YAML da scrivere")
    ap.add_argument("--topic", action="append", default=[], metavar="ID=NOME",
                    help="nome del topic per un dispositivo (default: slug del nome)")
    args = ap.parse_args()

    with open(os.path.expanduser(args.credentials), encoding="utf-8") as f:
        cred = dict(l.strip().split("=", 1) for l in f if "=" in l and not l.startswith("#"))
    topics = dict(t.split("=", 1) for t in args.topic)

    cloud = TuyaCloud(cred["TUYA_ACCESS_ID"], cred["TUYA_ACCESS_SECRET"], cred["TUYA_ENDPOINT"])
    devs = []
    for d in sorted(cloud.devices(), key=lambda d: d["name"].strip().lower()):
        name = d["name"].strip()
        # i dispositivi virtuali del portale non esistono in rete
        if d["id"].startswith("vdevo"):
            print(f"salto {name}: dispositivo virtuale", file=sys.stderr)
            continue
        if not d.get("local_key"):
            print(f"salto {name}: nessuna local key", file=sys.stderr)
            continue
        dps = {int(p["abilityId"]): _dp_entry(p) for p in cloud.model(d["id"])}
        devs.append({"id": d["id"], "name": name, "topic": topics.get(d["id"], _slug(name)),
                     "category": d.get("category", ""),
                     "product": d.get("model") or d.get("product_name", ""),
                     "local_key": d["local_key"], "dps": dps})
        print(f"{name}: {len(dps)} datapoint, topic {devs[-1]['topic']}", file=sys.stderr)

    seen: Dict[str, str] = {}
    for d in devs:
        if d["topic"] in seen:
            sys.exit(f"topic duplicato '{d['topic']}': {seen[d['topic']]} e {d['name']}")
        seen[d["topic"]] = d["name"]

    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(_yaml(devs))
    os.chmod(args.out, 0o600)
    print(f"scritti {len(devs)} dispositivi in {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
