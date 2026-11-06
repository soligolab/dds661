"""
tuya_ha.py
----------
Discovery Home Assistant per tuya_bridge.py: dai dp di un dispositivo (codice,
tipo, scala, modo) alle config MQTT. Nessun I/O: restituisce (topic, payload)
e il ponte li pubblica retained a ogni connessione al broker.

- termostati (categoria wk): un'entita' climate su accensione/setpoint/temperatura;
- aspirapolvere (categoria sd): un'entita' vacuum, con uno stato derivato su
  <topic>/vacuum perche' HA lo vuole come {"state": ...} e non accetta template;
- tutti gli altri dp: sensor, binary_sensor, switch, number, select o button.
  Quelli in NAMES hanno un nome italiano e sono attivi; gli altri (impostazioni,
  contatori interni) nascono disattivati: si accendono dalla UI se servono.

I dp raw e i deny_codes non diventano entita'. unique_id = tuya_<id>_<codice>:
stabile anche se cambia il topic.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

# codice -> (nome, extra della config). Le unita' le corregge UNITS.
NAMES: Dict[str, Tuple[str, Dict[str, Any]]] = {
    # prese ed energy meter
    "switch_1": ("Presa", {}),
    "switch_on": ("Presa", {}),
    "cur_power": ("Potenza", {"dev_cla": "power"}),
    "cur_voltage": ("Tensione", {"dev_cla": "voltage"}),
    "cur_current": ("Corrente", {"dev_cla": "current"}),
    "cur_frequency": ("Frequenza", {"dev_cla": "frequency"}),
    "power_factor": ("Fattore di potenza", {"dev_cla": "power_factor", "unit_of_meas": None}),
    "ele": ("Energia totale", {"dev_cla": "energy", "stat_cla": "total_increasing"}),
    # termostati (accensione, setpoint e temperatura stanno nell'entita' climate)
    "child_lock": ("Blocco bambini", {}),
    "ChildLock": ("Blocco bambini", {}),
    "temp_correction": ("Correzione temperatura", {"dev_cla": "temperature", "unit_of_meas": "°C"}),
    "ECO": ("Eco", {}),
    "floorTemp": ("Temperatura pavimento", {"dev_cla": "temperature"}),
    # aspirapolvere
    "residual_electricity": ("Batteria", {"dev_cla": "battery"}),
    "edge_brush": ("Durata spazzola laterale", {}),
    "roll_brush": ("Durata spazzola rotante", {}),
    "filter": ("Durata filtro", {}),
    "clean_area": ("Area di pulizia", {}),
    "clean_time": ("Tempo di pulizia", {"dev_cla": "duration"}),
    "reset_edge_brush": ("Ripristina spazzola laterale", {}),
    "reset_roll_brush": ("Ripristina spazzola a rullo", {}),
    "reset_filter": ("Ripristina filtro", {}),
    "fault": ("Guasto", {"ent_cat": "diagnostic"}),
}
# il modello cloud e' scritto a mano dai produttori: stessa cosa, mille grafie
UNITS = {"℃": "°C", "秒": "s", "S": "s", "度": "kWh", "kwh": "kWh", "kw.h": "kWh",
         "kW·h": "kWh", "㎡": "m²", "PF": None, "day": "d"}
UNIT_CLASS = {"W": "power", "V": "voltage", "A": "current", "mA": "current",
              "Hz": "frequency", "°C": "temperature", "kWh": "energy"}

# accensione, setpoint, temperatura e stato: i nomi cambiano da un termostato all'altro
CLIMATE_CODES = {"power": ("switch", "Power"), "set": ("temp_set", "TempSet"),
                 "current": ("temp_current", "TempCurrent"), "action": ("work_state",)}
# dp gia' coperti dall'entita' vacuum, o comandi di guida manuale che non servono
VACUUM_CODES = {"switch_go", "status", "suction", "seek", "direction_control"}
VACUUM_STATE = {"standby": "idle", "sleep": "idle", "paused": "paused",
                "goto_charge": "returning", "charging": "docked", "charge_done": "docked"}


def _slug(s: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", s.lower())).strip("_")


def _get(code: str) -> str:
    # template che non produce nulla se il dp manca (dispositivo mai letto):
    # HA ignora il payload vuoto invece di loggare un errore
    return "{% if value_json." + code + " is defined %}{{ value_json." + code + " }}{% endif %}"


def _factor(dp: Dict[str, Any]) -> float:
    # stessa regola di tuya_bridge._factor; copiata per non importare il ponte da qui
    return float(dp["factor"]) if "factor" in dp else 10.0 ** -int(dp.get("scale", 0))


def _num(dp: Dict[str, Any], key: str) -> Optional[float]:
    v = dp.get(key)
    return None if v is None else round(v * _factor(dp), 6)


def _pick(codes: Iterable[str], dps: Dict[str, Dict[str, Any]]) -> Optional[str]:
    have = {dp["code"] for dp in dps.values()}
    return next((c for c in codes if c in have), None)


def vacuum_state(state: Dict[str, Any]) -> Dict[str, Any]:
    st = state.get("status")
    out = {"state": "error" if state.get("fault") else VACUUM_STATE.get(st, "cleaning" if st else "idle")}
    if "suction" in state:
        out["fan_speed"] = state["suction"]
    return out


class Discovery:
    def __init__(self, dev: Dict[str, Any], dps: Dict[str, Dict[str, Any]], base: str,
                 prefix: str, deny: Iterable[str]):
        self.dev, self.dps, self.prefix, self.deny = dev, dps, prefix, set(deny)
        self.t = f"{base}/{dev['topic']}"
        self.obj = _slug(dev["topic"])
        self.uid = f"tuya_{dev['id']}"
        self.common = {
            "avty": [{"topic": f"{base}/status"}, {"topic": f"{self.t}/availability"}],
            "avty_mode": "all",
            "dev": {"identifiers": [self.uid], "manufacturer": "Tuya",
                    "model": dev.get("product") or dev.get("category") or "",
                    "name": dev.get("name") or dev["topic"]},
        }

    def _cfg(self, comp: str, key: str, payload: Dict[str, Any]) -> Tuple[str, str]:
        p = {**payload, "uniq_id": f"{self.uid}_{key}",
             "default_entity_id": f"{comp}.{self.obj}" + ("" if key == comp else f"_{_slug(key)}"),
             **self.common}
        return f"{self.prefix}/{comp}/{self.uid}_{key}/config", json.dumps(p, ensure_ascii=False)

    def climate(self) -> Tuple[List[Tuple[str, str]], set]:
        c = {k: _pick(v, self.dps) for k, v in CLIMATE_CODES.items()}
        if not (c["power"] and c["set"]):
            return [], set()
        dp_set = next(dp for dp in self.dps.values() if dp["code"] == c["set"])
        f = _factor(dp_set)
        p: Dict[str, Any] = {
            "name": None, "temperature_unit": "C",
            "modes": ["off", "heat"],
            "mode_state_topic": f"{self.t}/state",
            "mode_state_template": "{% if value_json." + c["power"] + " is defined %}"
                                   "{{ 'heat' if value_json." + c["power"] + " else 'off' }}{% endif %}",
            "mode_command_topic": f"{self.t}/set",
            "mode_command_template": '{"' + c["power"] + '": {{ "true" if value == "heat" else "false" }}}',
            "temperature_state_topic": f"{self.t}/state",
            "temperature_state_template": _get(c["set"]),
            "temperature_command_topic": f"{self.t}/set/{c['set']}",
            "temp_step": 0.5 if f < 1 else 1, "precision": 0.1 if f < 0.5 else (0.5 if f < 1 else 1.0),
        }
        for k in ("min", "max"):
            if dp_set.get(k) is not None:
                p[f"{k}_temp"] = _num(dp_set, k)
        used = {c["power"], c["set"]}
        if c["current"]:
            p.update(current_temperature_topic=f"{self.t}/state",
                     current_temperature_template=_get(c["current"]))
            used.add(c["current"])
        if c["action"]:
            p.update(action_topic=f"{self.t}/state", action_template=(
                "{% set v = value_json %}{% if v." + c["power"] + " is defined %}"
                "{% if not v." + c["power"] + " %}off{% elif v." + c["action"] + " == 'heat' %}heating"
                "{% elif v." + c["action"] + " == 'cool' %}cooling{% else %}idle{% endif %}{% endif %}"))
            used.add(c["action"])
        # preset solo se la modalita' ha nomi veri: "0"/"1" non dicono niente
        mode = next((dp for dp in self.dps.values() if dp["code"] in ("mode", "Mode")), None)
        if mode and mode.get("type") == "enum" and not any(str(r).isdigit() for r in mode.get("range", [])):
            p.update(preset_mode_state_topic=f"{self.t}/state",
                     preset_mode_value_template=_get(mode["code"]),
                     preset_mode_command_topic=f"{self.t}/set/{mode['code']}",
                     preset_modes=list(mode["range"]))
            used.add(mode["code"])
        return [self._cfg("climate", "climate", p)], used

    def vacuum(self) -> Tuple[List[Tuple[str, str]], set]:
        codes = {dp["code"] for dp in self.dps.values()}
        if "switch_go" not in codes:
            return [], set()
        feats = ["start", "pause", "stop", "return_home"]
        p: Dict[str, Any] = {
            "name": None, "state_topic": f"{self.t}/vacuum", "command_topic": f"{self.t}/set",
            "payload_start": '{"switch_go": true}', "payload_stop": '{"switch_go": false}',
            "payload_pause": '{"switch_go": false}', "payload_return_to_base": '{"mode": "chargego"}',
        }
        if "seek" in codes:
            feats.append("locate")
            p["payload_locate"] = '{"seek": true}'
        if "mode" in codes:
            feats.append("clean_spot")
            p["payload_clean_spot"] = '{"mode": "spiral"}'
        suction = next((dp for dp in self.dps.values() if dp["code"] == "suction"), None)
        if suction:
            feats.append("fan_speed")
            p.update(fan_speed_list=list(suction.get("range", [])),
                     set_fan_speed_topic=f"{self.t}/set/suction")
        p["supported_features"] = feats
        return [self._cfg("vacuum", "vacuum", p)], VACUUM_CODES & codes

    def entity(self, dp: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        code, t, rw = dp["code"], dp.get("type"), "w" in dp.get("mode", "ro")
        if t == "raw" or code in self.deny:
            return None
        name, extra = NAMES.get(code, (code.replace("_", " ").capitalize(), {}))
        p: Dict[str, Any] = {"name": name, "stat_t": f"{self.t}/state", "val_tpl": _get(code)}
        cmd = f"{self.t}/set/{code}"
        if t == "bool":
            if rw and code.startswith("reset_"):
                comp, p = "button", {"name": name, "cmd_t": cmd, "pl_prs": "true"}
            elif rw:
                comp = "switch"
                p.update(cmd_t=cmd, pl_on="true", pl_off="false", stat_on="True", stat_off="False")
            else:
                comp = "binary_sensor"
                p.update(pl_on="True", pl_off="False")
        elif t == "enum":
            if rw:
                comp = "select"
                p.update(cmd_t=cmd, ops=[str(r) for r in dp.get("range", [])])
            else:
                comp = "sensor"
                p.update(dev_cla="enum", ops=[str(r) for r in dp.get("range", [])])
        elif t == "value":
            unit = UNITS.get(dp.get("unit"), dp.get("unit")) if dp.get("unit") else None
            if unit:
                p["unit_of_meas"] = unit
            if unit in UNIT_CLASS:
                p["dev_cla"] = UNIT_CLASS[unit]
                p["stat_cla"] = "total_increasing" if unit == "kWh" else "measurement"
            # un contatore di energia resta un sensore anche se il modello lo dice scrivibile
            if rw and p.get("dev_cla") != "energy" and extra.get("dev_cla") != "energy":
                comp = "number"
                p.pop("stat_cla", None)
                p.update(cmd_t=cmd, mode="box", min=_num(dp, "min"), max=_num(dp, "max"),
                         step=_num(dp, "step") or 1)
            else:
                comp = "sensor"
        else:  # string, bitmap
            comp = "sensor"
        p.update(extra)
        if p.get("unit_of_meas", "") is None:
            p.pop("unit_of_meas")
        if code not in NAMES:
            # impostazioni e contatori interni: ci sono, ma spenti finche' non servono
            p["en"] = False
            p["ent_cat"] = "config" if rw else "diagnostic"
        return self._cfg(comp, code, p)

    def configs(self) -> List[Tuple[str, str]]:
        cat = self.dev.get("category")
        out, used = self.climate() if cat == "wk" else self.vacuum() if cat == "sd" else ([], set())
        for dp in self.dps.values():
            if dp["code"] not in used:
                c = self.entity(dp)
                if c:
                    out.append(c)
        return out
