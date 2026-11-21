# -*- coding: utf-8 -*-
"""
HomeKit -> Indigo mapping.

Pure Python (no `indigo` import) so it can be tested on its own. Works from the
raw accessory database aiohomekit returns (list_accessories_and_characteristics),
with every service and characteristic type already normalised to a full UUID.

Vocabulary
  plan   : one Indigo device to be, built from one primary HomeKit service plus
           any services folded into it (battery, accessory info, filter, TV speaker)
  chars  : the characteristic list stored in a device's props ("hkChars")
  values : the plugin's value cache, {(aid, iid): value}
"""

import colorsys
import math
import re

from aiohomekit.model.characteristics import CharacteristicsTypes as CT
from aiohomekit.model.services import ServicesTypes as ST


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def _uuid_table(cls):
    return {v.upper(): k for k, v in vars(cls).items() if isinstance(v, str) and v.count("-") == 4}


CHAR_NAMES = _uuid_table(CT)
SERVICE_NAMES = _uuid_table(ST)


def char_name(uuid):
    name = CHAR_NAMES.get(str(uuid).upper())
    if name:
        return name
    head = str(uuid).split("-")[0].lstrip("0") or "0"
    return f"C_{head.upper()}"


def service_name(uuid):
    name = SERVICE_NAMES.get(str(uuid).upper())
    if name:
        return name
    head = str(uuid).split("-")[0].lstrip("0") or "0"
    return f"S_{head.upper()}"


def camel(name):
    return "".join(p[:1].upper() + p[1:].lower() for p in re.split(r"[_\W]+", name) if p)


def title(name):
    words = [w for w in re.split(r"[_\W]+", name) if w]
    fixed = {"Pm25": "PM2.5", "Pm10": "PM10", "Voc": "VOC", "No2": "NO2", "So2": "SO2", "Lo": "Low", "Batt": "Battery"}
    return " ".join(fixed.get(w.capitalize(), w.capitalize()) for w in words)


def state_id(name, prefix=""):
    return "hk" + camel(prefix) + camel(name)


def _st(*names):
    return {getattr(ST, n).upper() for n in names if hasattr(ST, n)}


def _ct(name):
    return getattr(CT, name).upper()


# Services that never become devices and contribute nothing useful.
IGNORED_SERVICES = _st(
    "PROTOCOL_INFORMATION", "SERVICE_LABEL", "PAIRING", "CAMERA_RTP_STREAM_MANAGEMENT", "CAMERA_CONTROL",
    "CAMERA_RECORDING_MANAGEMENT", "CAMERA_OPERATING_MODE", "DATA_STREAM_TRANSPORT_MANAGEMENT",
    "TRANSFER_TRANSPORT_MANAGEMENT", "AUDIO_STREAM_MANAGEMENT", "SIRI", "SIRI_ENDPOINT", "ASSISTANT",
    "THREAD_TRANSPORT", "WI_FI_TRANSPORT", "TIME_INFORMATION", "TUNNEL", "BRIDGE_CONFIGURATION", "BRIDGING_STATE",
    "CLOUD_RELAY", "DIAGNOSTICS", "ASSET_UPDATE", "ACCESSORY_METRICS", "ACCESSORY_RUNTIME_INFORMATION",
    "POWER_MANAGEMENT", "TARGET_CONTROL_MANAGEMENT", "TARGET_CONTROL", "LOCK_MANAGEMENT", "ACCESS_CODE",
    "NFCACCESS", "ACCESS_CONTROL", "WI_FI_ROUTER", "WI_FI_SATELLITE",
)

INFO = ST.ACCESSORY_INFORMATION.upper()
BATTERY = ST.BATTERY_SERVICE.upper()
FILTER = ST.FILTER_MAINTENANCE.upper()
TELEVISION = ST.TELEVISION.upper()
SPEAKER = ST.SPEAKER.upper()
INPUT_SOURCE = ST.INPUT_SOURCE.upper()

# Folded into every other device on the same accessory instead of getting their own.
FOLDED_SERVICES = {INFO, BATTERY, FILTER}

INFO_CHARS = {_ct(n) for n in ("NAME", "MANUFACTURER", "MODEL", "SERIAL_NUMBER", "FIRMWARE_REVISION",
                               "HARDWARE_REVISION", "IDENTIFY")}

# Formats that cannot be shown as a state.
OPAQUE_FORMATS = {"tlv8", "data"}

# Characteristics a service shows only as its own label, not as a state.
NAME_CHARS = {_ct("NAME"), _ct("CONFIGURED_NAME")}


# ---------------------------------------------------------------------------
# Kinds and device types
# ---------------------------------------------------------------------------

SENSORS = {
    # service: (subkind, characteristic, value|bool, level characteristic)
    ST.TEMPERATURE_SENSOR: ("temperature", CT.TEMPERATURE_CURRENT, "value", None),
    ST.HUMIDITY_SENSOR: ("humidity", CT.RELATIVE_HUMIDITY_CURRENT, "value", None),
    ST.LIGHT_SENSOR: ("light", CT.LIGHT_LEVEL_CURRENT, "value", None),
    ST.AIR_QUALITY_SENSOR: ("airquality", CT.AIR_QUALITY, "value", None),
    ST.MOTION_SENSOR: ("motion", CT.MOTION_DETECTED, "bool", None),
    ST.OCCUPANCY_SENSOR: ("occupancy", CT.OCCUPANCY_DETECTED, "bool", None),
    ST.CONTACT_SENSOR: ("contact", CT.CONTACT_STATE, "bool", None),
    ST.LEAK_SENSOR: ("leak", CT.LEAK_DETECTED, "bool", None),
    ST.SMOKE_SENSOR: ("smoke", CT.SMOKE_DETECTED, "bool", None),
    ST.CARBON_MONOXIDE_SENSOR: ("co", CT.CARBON_MONOXIDE_DETECTED, "bool", CT.CARBON_MONOXIDE_LEVEL),
    ST.CARBON_DIOXIDE_SENSOR: ("co2", CT.CARBON_DIOXIDE_DETECTED, "bool", CT.CARBON_DIOXIDE_LEVEL),
}
SENSORS = {k.upper(): (a, b.upper(), c, d.upper() if d else None) for k, (a, b, c, d) in SENSORS.items()}

SENSOR_LABELS = {
    "temperature": "Temperature", "humidity": "Humidity", "light": "Light Level", "airquality": "Air Quality",
    "motion": "Motion", "occupancy": "Occupancy", "contact": "Contact", "leak": "Leak", "smoke": "Smoke",
    "co": "Carbon Monoxide", "co2": "Carbon Dioxide",
}
SENSOR_SUBTYPES = {
    "temperature": "Temperature", "humidity": "Humidity", "light": "Illuminance", "motion": "Motion",
    "occupancy": "Presence", "contact": "DoorWindow", "leak": "Water", "smoke": "Smoke",
    "co": "CO", "co2": "CO2", "airquality": "AirQuality",
}
BOOL_TEXT = {
    "motion": ("motion", "clear"), "occupancy": ("occupied", "clear"), "contact": ("open", "closed"),
    "leak": ("leak", "dry"), "smoke": ("smoke", "clear"), "co": ("detected", "normal"),
    "co2": ("high", "normal"),
}
AIR_QUALITY_TEXT = {0: "unknown", 1: "excellent", 2: "good", 3: "fair", 4: "inferior", 5: "poor"}

KIND_BY_SERVICE = {
    ST.LIGHTBULB: "light", ST.SWITCH: "onoff", ST.OUTLET: "onoff", ST.LOCK_MECHANISM: "lock",
    ST.GARAGE_DOOR_OPENER: "garage", ST.WINDOW_COVERING: "covering", ST.WINDOW: "covering", ST.DOOR: "covering",
    ST.FAN: "fan", ST.FAN_V2: "fan", ST.AIR_PURIFIER: "fan", ST.THERMOSTAT: "thermostat",
    ST.HEATER_COOLER: "heatercooler", ST.HUMIDIFIER_DEHUMIDIFIER: "humidifier", ST.VALVE: "valve",
    ST.FAUCET: "valve", ST.IRRIGATION_SYSTEM: "valve", ST.SECURITY_SYSTEM: "security",
    ST.TELEVISION: "television", ST.STATELESS_PROGRAMMABLE_SWITCH: "button", ST.DOORBELL: "button",
}
KIND_BY_SERVICE = {k.upper(): v for k, v in KIND_BY_SERVICE.items()}

DEVICE_TYPES = {
    "light": "hkLight", "onoff": "hkOnOff", "lock": "hkLock", "garage": "hkGarageDoor", "covering": "hkCovering",
    "fan": "hkFan", "thermostat": "hkThermostat", "heatercooler": "hkThermostat", "humidifier": "hkHumidifier",
    "valve": "hkValve", "security": "hkSecurity", "television": "hkTelevision", "button": "hkButton",
    "sensor": "hkSensor", "generic": "hkGeneric",
}

KIND_LABELS = {
    "light": "Light", "onoff": "Switch", "lock": "Lock", "garage": "Garage Door", "covering": "Covering",
    "fan": "Fan", "thermostat": "Thermostat", "heatercooler": "Heater/Cooler", "humidifier": "Humidifier",
    "valve": "Valve", "security": "Security System", "television": "Television", "button": "Button",
    "generic": "Accessory",
}

RELAY_SUBTYPES = {ST.OUTLET.upper(): "Outlet", ST.SWITCH.upper(): "Switch", ST.LIGHTBULB.upper(): "Switch"}

ENERGY_WATT = {_ct(n) for n in ("VENDOR_EVE_ENERGY_WATT", "VENDOR_CONNECTSENSE_ENERGY_WATT",
                                "VENDOR_KOOGEEK_REALTIME_ENERGY", "VENDOR_KOOGEEK_REALTIME_ENERGY_2") if hasattr(CT, n)}
ENERGY_KWH = {_ct(n) for n in ("VENDOR_EVE_ENERGY_KW_HOUR", "VENDOR_CONNECTSENSE_ENERGY_KW_HOUR") if hasattr(CT, n)}

TEMPERATURE_CHARS = {_ct(n) for n in ("TEMPERATURE_CURRENT", "TEMPERATURE_TARGET", "TEMPERATURE_HEATING_THRESHOLD",
                                      "TEMPERATURE_COOLING_THRESHOLD")}

LOCK_TEXT = {0: "unlocked", 1: "locked", 2: "jammed", 3: "unknown"}
DOOR_TEXT = {0: "open", 1: "closed", 2: "opening", 3: "closing", 4: "stopped"}
POSITION_TEXT = {0: "closing", 1: "opening", 2: "stopped"}
SECURITY_TEXT = {0: "home", 1: "away", 2: "night", 3: "disarmed", 4: "triggered"}
SECURITY_TARGETS = {"home": 0, "away": 1, "night": 2, "disarmed": 3}
HVAC_TEXT = {0: "idle", 1: "heating", 2: "cooling"}
HEATER_COOLER_TEXT = {0: "inactive", 1: "idle", 2: "heating", 3: "cooling"}
HUMIDIFIER_TEXT = {0: "inactive", 1: "idle", 2: "humidifying", 3: "dehumidifying"}
HUMIDIFIER_MODES = {"auto": 0, "humidify": 1, "dehumidify": 2}
PURIFIER_TEXT = {0: "inactive", 1: "idle", 2: "purifying"}
FAN_STATE_TEXT = {0: "inactive", 1: "idle", 2: "blowing"}
VALVE_TYPES = {0: "generic", 1: "irrigation", 2: "shower", 3: "tap"}
PRESS_TEXT = {0: "single", 1: "double", 2: "long"}
REMOTE_KEYS = {
    "rewind": 0, "fastForward": 1, "nextTrack": 2, "previousTrack": 3, "arrowUp": 4, "arrowDown": 5,
    "arrowLeft": 6, "arrowRight": 7, "select": 8, "back": 9, "exit": 10, "playPause": 11, "information": 15,
}

# Indigo hvacOperationMode values (kHvacMode): Off 0, Heat 1, Cool 2, HeatCool 3
HVAC_FROM_HK = {0: 0, 1: 1, 2: 2, 3: 3}
HK_FROM_HVAC = {0: 0, 1: 1, 2: 2, 3: 3, 4: 1, 5: 2, 6: 3}


# ---------------------------------------------------------------------------
# Raw accessory database helpers
# ---------------------------------------------------------------------------

def _value_of(service, char_type):
    for c in service.get("characteristics", []):
        if str(c.get("type", "")).upper() == char_type:
            return c.get("value")
    return None


def _readable(c):
    perms = c.get("perms") or []
    return ("pr" in perms or "ev" in perms) and c.get("format") not in OPAQUE_FORMATS


def _usable(service):
    for c in service.get("characteristics", []):
        if str(c.get("type", "")).upper() in NAME_CHARS:
            continue
        if c.get("format") in OPAQUE_FORMATS:
            continue
        perms = c.get("perms") or []
        if {"pr", "pw", "ev"} & set(perms):
            return True
    return False


def _char_entry(aid, service, c, source):
    entry = {
        "a": aid, "i": c.get("iid"), "t": str(c.get("type", "")).upper(), "n": char_name(c.get("type", "")),
        "f": c.get("format"), "p": list(c.get("perms") or []), "s": source,
    }
    for src, dst in (("unit", "u"), ("minValue", "min"), ("maxValue", "max"), ("minStep", "step"),
                     ("valid-values", "vv"), ("description", "d")):
        if c.get(src) is not None:
            entry[dst] = c.get(src)
    return entry


def accessory_name(acc):
    for s in acc.get("services", []):
        if str(s.get("type", "")).upper() == INFO:
            name = _value_of(s, _ct("NAME"))
            if name:
                return str(name).strip()
    return f"Accessory {acc.get('aid')}"


def accessory_info(acc):
    out = {}
    for s in acc.get("services", []):
        if str(s.get("type", "")).upper() == INFO:
            for key, ct in (("name", "NAME"), ("manufacturer", "MANUFACTURER"), ("model", "MODEL"),
                            ("serial", "SERIAL_NUMBER"), ("firmware", "FIRMWARE_REVISION")):
                v = _value_of(s, _ct(ct))
                if v not in (None, ""):
                    out[key] = str(v).strip()
    return out


def kind_for(service):
    stype = str(service.get("type", "")).upper()
    if stype in SENSORS:
        return "sensor"
    kind = KIND_BY_SERVICE.get(stype)
    if kind == "light":
        has_brightness = any(str(c.get("type", "")).upper() == _ct("BRIGHTNESS") for c in service.get("characteristics", []))
        return "light" if has_brightness else "onoff"
    return kind or "generic"


def service_key(alias, aid, sid):
    return f"{alias}:{aid}:{sid}"


def plan_accessories(alias, accessories):
    """Return one plan per Indigo device the accessory database calls for."""
    plans = []
    for acc in accessories:
        plans.extend(plan_accessory(alias, acc))
    return plans


def plan_accessory(alias, acc):
    aid = acc.get("aid")
    services = acc.get("services", [])
    acc_name = accessory_name(acc)
    info = accessory_info(acc)
    has_tv = any(str(s.get("type", "")).upper() == TELEVISION for s in services)

    shared = []  # (service, source) folded into every device on this accessory
    tv_extra = []
    primaries = []
    for s in services:
        stype = str(s.get("type", "")).upper()
        if stype in IGNORED_SERVICES:
            continue
        if stype in FOLDED_SERVICES:
            shared.append((s, {INFO: "info", BATTERY: "battery", FILTER: "filter"}[stype]))
            continue
        if has_tv and stype in (SPEAKER, INPUT_SOURCE):
            if stype == SPEAKER:
                tv_extra.append((s, "speaker"))
            continue
        if not _usable(s):
            continue
        primaries.append(s)

    multiple = len(primaries) > 1
    plans = []
    used_labels = set()
    for s in primaries:
        stype = str(s.get("type", "")).upper()
        kind = kind_for(s)
        chars = [_char_entry(aid, s, c, "p") for c in s.get("characteristics", [])
                 if c.get("format") not in OPAQUE_FORMATS or str(c.get("type", "")).upper() == _ct("IDENTIFY")]
        extra = list(shared) + (tv_extra if kind == "television" else [])
        for fs, source in extra:
            for c in fs.get("characteristics", []):
                ctype = str(c.get("type", "")).upper()
                if c.get("format") in OPAQUE_FORMATS:
                    continue
                if source == "info" and ctype not in INFO_CHARS:
                    continue
                if source != "info" and ctype in NAME_CHARS:
                    continue
                chars.append(_char_entry(aid, fs, c, source))

        label = _label(s, kind, acc_name, multiple)
        base, n = label, 2
        while label in used_labels:
            label, n = f"{base} {n}", n + 1
        used_labels.add(label)

        plan = {
            "key": service_key(alias, aid, s.get("iid")), "aid": aid, "sid": s.get("iid"), "kind": kind,
            "deviceType": DEVICE_TYPES[kind], "service": service_name(stype), "serviceType": stype,
            "name": label, "accessoryName": acc_name, "info": info, "chars": chars, "inputs": [],
        }
        if stype in SENSORS:
            plan["sensor"] = SENSORS[stype][0]
        if kind == "television":
            plan["inputs"] = _tv_inputs(s, services)
        plan["props"] = device_props(plan)
        plans.append(plan)
    return plans


def _label(service, kind, acc_name, multiple):
    stype = str(service.get("type", "")).upper()
    own = _value_of(service, _ct("CONFIGURED_NAME")) or _value_of(service, _ct("NAME"))
    own = str(own).strip() if own else ""
    if kind == "button":
        idx = _value_of(service, _ct("SERVICE_LABEL_INDEX"))
        if stype == ST.DOORBELL.upper():
            return acc_name if not multiple else f"{acc_name} Doorbell"
        if idx is not None and multiple:
            return f"{acc_name} Button {idx}"
    if not multiple:
        return acc_name
    if own and own != acc_name:
        return own if own.startswith(acc_name) else f"{acc_name} {own}"
    if stype in SENSORS:
        suffix = SENSOR_LABELS[SENSORS[stype][0]]
    elif stype == ST.OUTLET.upper():
        suffix = "Outlet"
    else:
        suffix = KIND_LABELS.get(kind, title(service_name(stype)))
    if suffix.lower() in acc_name.lower():
        return acc_name
    return f"{acc_name} {suffix}"


def _tv_inputs(tv_service, services):
    linked = set(tv_service.get("linked") or [])
    inputs = []
    for s in services:
        if str(s.get("type", "")).upper() != INPUT_SOURCE:
            continue
        if linked and s.get("iid") not in linked:
            continue
        ident = _value_of(s, _ct("IDENTIFIER"))
        name = _value_of(s, _ct("CONFIGURED_NAME")) or _value_of(s, _ct("NAME")) or f"Input {ident}"
        hidden = _value_of(s, _ct("CURRENT_VISIBILITY_STATE"))
        if ident is not None:
            inputs.append({"id": ident, "name": str(name), "hidden": bool(hidden)})
    return inputs


def device_props(plan):
    """Indigo props that describe the device's capabilities (SupportsColor and so on)."""
    idx = CharIndex(plan["chars"])
    kind = plan["kind"]
    props = {"SupportsStatusRequest": True, "SupportsBatteryLevel": idx.has("BATTERY_LEVEL")}
    sub = None
    if kind == "light":
        rgb = idx.has("HUE") and idx.has("SATURATION")
        ct = idx.has("COLOR_TEMPERATURE")
        props.update({"SupportsColor": rgb or ct, "SupportsRGB": rgb, "SupportsWhite": ct,
                      "SupportsWhiteTemperature": ct, "SupportsTwoWhiteLevels": False})
        if ct:
            e = idx.get("COLOR_TEMPERATURE")
            props["WhiteTemperatureMin"] = int(round(1e6 / float(e.get("max") or 500)))
            props["WhiteTemperatureMax"] = int(round(1e6 / float(e.get("min") or 140)))
        sub = "Bulb"
    elif kind == "onoff":
        sub = RELAY_SUBTYPES.get(plan["serviceType"], "Switch")
    elif kind == "lock":
        sub = "Lock"
    elif kind == "covering":
        sub = "Blind"
    elif kind == "sensor":
        mode = SENSORS[plan["serviceType"]][2]
        level = SENSORS[plan["serviceType"]][3]
        has_level = bool(level and idx.has_type(level))
        props["SupportsOnState"] = mode == "bool"
        props["SupportsSensorValue"] = mode == "value" or has_level
        props["AllowOnStateChange"] = False
        props["AllowSensorValueChange"] = False
        sub = SENSOR_SUBTYPES.get(plan.get("sensor"))
    elif kind in ("thermostat", "heatercooler"):
        humid = idx.has("RELATIVE_HUMIDITY_CURRENT")
        if kind == "thermostat":
            heat = idx.has("TEMPERATURE_TARGET") or idx.has("TEMPERATURE_HEATING_THRESHOLD")
            cool = idx.has("TEMPERATURE_TARGET") or idx.has("TEMPERATURE_COOLING_THRESHOLD")
        else:
            heat = idx.has("TEMPERATURE_HEATING_THRESHOLD")
            cool = idx.has("TEMPERATURE_COOLING_THRESHOLD")
        props.update({"NumTemperatureInputs": 1 if idx.has("TEMPERATURE_CURRENT") else 0,
                      "NumHumidityInputs": 1 if humid else 0, "SupportsHeatSetpoint": heat,
                      "SupportsCoolSetpoint": cool, "SupportsHvacOperationMode": True,
                      "SupportsHvacFanMode": False, "ShowCoolHeatEquipmentStateUI": True})
    if kind in ("onoff", "valve", "humidifier", "television"):
        watt = any(e["t"] in ENERGY_WATT for e in plan["chars"])
        kwh = any(e["t"] in ENERGY_KWH for e in plan["chars"])
        props["SupportsEnergyMeter"] = kwh
        props["SupportsEnergyMeterCurPower"] = watt
    if sub:
        props["hkSubType"] = sub
    return props


# ---------------------------------------------------------------------------
# Characteristic lookup and value coercion
# ---------------------------------------------------------------------------

class CharIndex:
    """Find characteristics in a device's char list, preferring its own (primary) service."""

    def __init__(self, chars):
        self.chars = chars or []
        self.by_type = {}
        for e in self.chars:
            cur = self.by_type.get(e["t"])
            if cur is None or (cur.get("s") != "p" and e.get("s") == "p"):
                self.by_type[e["t"]] = e

    def get(self, name):
        return self.by_type.get(_ct(name)) if hasattr(CT, name) else None

    def get_type(self, uuid):
        return self.by_type.get(str(uuid).upper())

    def has(self, name):
        return self.get(name) is not None

    def has_type(self, uuid):
        return str(uuid).upper() in self.by_type

    def val(self, values, name, default=None):
        e = self.get(name)
        if e is None:
            return default
        v = values.get((e["a"], e["i"]))
        return default if v is None else v

    def val_type(self, values, uuid, default=None):
        e = self.get_type(uuid)
        if e is None:
            return default
        v = values.get((e["a"], e["i"]))
        return default if v is None else v

    def write(self, name, value):
        """(aid, iid, coerced value) for a writable characteristic, or None."""
        e = self.get(name)
        if e is None or "pw" not in e.get("p", []):
            return None
        return (e["a"], e["i"], coerce(e, value))

    def writable(self):
        return [e for e in self.chars if "pw" in e.get("p", []) and e.get("f") not in OPAQUE_FORMATS
                and e["t"] != _ct("IDENTIFY")]


def coerce(entry, value):
    """Convert a value to the characteristic's format, clamped to its range and step."""
    fmt = entry.get("f")
    if fmt == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "on", "yes")
        return bool(value)
    if fmt == "string":
        return str(value)
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{title(entry['n'])} needs a number, not {value!r}")
    lo, hi, step = entry.get("min"), entry.get("max"), entry.get("step")
    if lo is not None:
        num = max(num, float(lo))
    if hi is not None:
        num = min(num, float(hi))
    if step:
        base = float(lo or 0)
        num = base + round((num - base) / float(step)) * float(step)
    if fmt in ("float",):
        return round(num, 4)
    num = int(round(num))
    vv = entry.get("vv")
    if vv and num not in vv:
        raise ValueError(f"{title(entry['n'])} accepts {vv}, not {num}")
    return num


def parse_value(entry, text):
    """Parse a value typed by the user (Set Characteristic action) for this characteristic."""
    if entry.get("f") == "bool":
        t = str(text).strip().lower()
        if t not in ("1", "0", "true", "false", "on", "off", "yes", "no"):
            raise ValueError(f"{title(entry['n'])} needs true or false, not {text!r}")
    return coerce(entry, text)


# ---------------------------------------------------------------------------
# Dynamic (per-characteristic) states
# ---------------------------------------------------------------------------

def dynamic_states(chars):
    """[(state id, kind, label, char entry)] for every readable characteristic."""
    out, seen = [], set()
    for e in chars:
        if e.get("f") in OPAQUE_FORMATS or not ({"pr", "ev"} & set(e.get("p", []))):
            continue
        if e["t"] in NAME_CHARS and e.get("s") != "info":
            continue
        sid = state_id(e["n"])
        if sid in seen:
            sid = state_id(e["n"], e.get("s", ""))
            if sid in seen:
                continue
        seen.add(sid)
        fmt = e.get("f")
        if fmt == "bool":
            kind = "bool"
        elif fmt == "string":
            kind = "string"
        elif fmt == "float":
            kind = "number"
        else:
            kind = "integer"
        label = title(e["n"]) if e.get("s") in ("p", "info") else f"{title(e['s'])} {title(e['n'])}"
        out.append((sid, kind, label, e))
    return out


def dynamic_state_values(chars, values):
    out = []
    for sid, kind, _label, e in dynamic_states(chars):
        v = values.get((e["a"], e["i"]))
        if v is None:
            continue
        try:
            if kind == "bool":
                v = bool(v)
            elif kind == "integer":
                v = int(v)
            elif kind == "number":
                v = round(float(v), 2)
            else:
                v = str(v)
        except (TypeError, ValueError):
            continue
        out.append({"key": sid, "value": v})
    return out


# ---------------------------------------------------------------------------
# State computation
# ---------------------------------------------------------------------------

def to_unit(celsius, unit):
    if celsius is None:
        return None
    c = float(celsius)
    return round(c * 9 / 5 + 32, 1) if unit == "F" else round(c, 1)


def from_unit(value, unit):
    v = float(value)
    return (v - 32) * 5 / 9 if unit == "F" else v


def _temp_ui(v, unit):
    return f"{v:.1f} °{unit}"


def compute_states(kind, plan_props, chars, values, opts):
    """Indigo state updates (list of dicts) for a device from the value cache."""
    idx = CharIndex(chars)
    unit = opts.get("tempUnit", "C")
    st = []

    def add(key, value, ui=None, dp=None):
        if value is None:
            return
        d = {"key": key, "value": value}
        if ui is not None:
            d["uiValue"] = ui
        if dp is not None:
            d["decimalPlaces"] = dp
        st.append(d)

    def active():
        if idx.has("ACTIVE"):
            return int(idx.val(values, "ACTIVE", 0) or 0) == 1
        return bool(idx.val(values, "ON", False))

    if kind == "light":
        on = bool(idx.val(values, "ON", False))
        bri = float(idx.val(values, "BRIGHTNESS", 100) or 0)
        level = max(1, int(round(bri))) if on else 0  # on at 0% still shows as on in Indigo
        add("brightnessLevel", level)
        if plan_props.get("SupportsRGB"):
            h = float(idx.val(values, "HUE", 0) or 0)
            s = float(idx.val(values, "SATURATION", 0) or 0)
            r, g, b = colorsys.hsv_to_rgb(h / 360.0, s / 100.0, level / 100.0)
            add("redLevel", int(round(r * 100)))
            add("greenLevel", int(round(g * 100)))
            add("blueLevel", int(round(b * 100)))
        if plan_props.get("SupportsWhiteTemperature"):
            mired = idx.val(values, "COLOR_TEMPERATURE")
            if mired:
                add("whiteTemperature", int(round(1e6 / float(mired))))
            sat = float(idx.val(values, "SATURATION", 0) or 0)
            add("whiteLevel", level if (not plan_props.get("SupportsRGB") or sat < 1) else 0)

    elif kind == "onoff":
        add("onOffState", active())

    elif kind == "lock":
        cur = idx.val(values, "LOCK_MECHANISM_CURRENT_STATE")
        tgt = idx.val(values, "LOCK_MECHANISM_TARGET_STATE")
        locked = (cur == 1) if cur is not None else (tgt == 1)
        add("onOffState", locked, "locked" if locked else "unlocked")
        if cur is not None:
            add("lockStatus", LOCK_TEXT.get(int(cur), str(cur)))
        if tgt is not None:
            add("lockTarget", "locked" if int(tgt) == 1 else "unlocked")

    elif kind == "garage":
        cur = idx.val(values, "DOOR_STATE_CURRENT")
        tgt = idx.val(values, "DOOR_STATE_TARGET")
        if cur is not None:
            is_open = int(cur) != 1
            add("onOffState", is_open, DOOR_TEXT.get(int(cur), str(cur)))
            add("doorStatus", DOOR_TEXT.get(int(cur), str(cur)))
        if tgt is not None:
            add("doorTarget", "open" if int(tgt) == 0 else "closed")
        if idx.has("OBSTRUCTION_DETECTED"):
            add("obstructed", bool(idx.val(values, "OBSTRUCTION_DETECTED", False)))

    elif kind == "covering":
        pos = idx.val(values, "POSITION_CURRENT")
        if pos is not None:
            add("brightnessLevel", int(round(float(pos))))
        tgt = idx.val(values, "POSITION_TARGET")
        if tgt is not None:
            add("targetPosition", int(round(float(tgt))))
        ps = idx.val(values, "POSITION_STATE")
        if ps is not None:
            add("movement", POSITION_TEXT.get(int(ps), str(ps)))

    elif kind == "fan":
        on = active()
        speed_e = idx.get("ROTATION_SPEED")
        if speed_e is not None:
            raw = float(idx.val(values, "ROTATION_SPEED", 0) or 0)
            top = float(speed_e.get("max") or 100) or 100
            pct = int(round(raw / top * 100))
        else:
            pct = 100
        level = max(pct, 1) if on else 0
        add("speedLevel", level)
        add("speedIndex", 0 if level == 0 else min(3, max(1, math.ceil(level / 100 * 3))))
        if idx.has("ROTATION_DIRECTION"):
            add("direction", "counterclockwise" if int(idx.val(values, "ROTATION_DIRECTION", 0) or 0) else "clockwise")
        if idx.has("SWING_MODE"):
            add("swing", bool(idx.val(values, "SWING_MODE", 0)))
        if idx.has("AIR_PURIFIER_STATE_CURRENT"):
            add("fanStatus", PURIFIER_TEXT.get(int(idx.val(values, "AIR_PURIFIER_STATE_CURRENT", 0) or 0), "unknown"))
        elif idx.has("FAN_STATE_CURRENT"):
            add("fanStatus", FAN_STATE_TEXT.get(int(idx.val(values, "FAN_STATE_CURRENT", 0) or 0), "unknown"))
        tgt_mode = idx.val(values, "AIR_PURIFIER_STATE_TARGET")
        if tgt_mode is None:
            tgt_mode = idx.val(values, "FAN_STATE_TARGET")
        if tgt_mode is not None:
            add("fanMode", "auto" if int(tgt_mode) == 1 else "manual")

    elif kind in ("thermostat", "heatercooler"):
        temp = to_unit(idx.val(values, "TEMPERATURE_CURRENT"), unit)
        if temp is not None:
            add("temperatureInput1", temp, _temp_ui(temp, unit), 1)
        hum = idx.val(values, "RELATIVE_HUMIDITY_CURRENT")
        if hum is not None:
            add("humidityInput1", round(float(hum), 1), f"{float(hum):.0f}%", 0)
        target = idx.val(values, "TEMPERATURE_TARGET")
        heat_t = idx.val(values, "TEMPERATURE_HEATING_THRESHOLD")
        cool_t = idx.val(values, "TEMPERATURE_COOLING_THRESHOLD")
        if kind == "thermostat":
            mode = int(idx.val(values, "HEATING_COOLING_TARGET", 0) or 0)
            cur = int(idx.val(values, "HEATING_COOLING_CURRENT", 0) or 0)
            hvac = HVAC_FROM_HK.get(mode, 0)
            heater, cooler = cur == 1, cur == 2
            status = HVAC_TEXT.get(cur, "idle")
            if mode == 1:
                heat, cool = target, cool_t if cool_t is not None else target
            elif mode == 2:
                heat, cool = heat_t if heat_t is not None else target, target
            else:
                heat = heat_t if heat_t is not None else target
                cool = cool_t if cool_t is not None else target
        else:
            on = active()
            tgt = int(idx.val(values, "TARGET_HEATER_COOLER_STATE", 0) or 0)
            cur = int(idx.val(values, "CURRENT_HEATER_COOLER_STATE", 0) or 0)
            hvac = {0: 3, 1: 1, 2: 2}.get(tgt, 3) if on else 0
            heater, cooler = cur == 2, cur == 3
            status = HEATER_COOLER_TEXT.get(cur, "inactive") if on else "off"
            heat, cool = heat_t, cool_t
        add("hvacOperationMode", hvac)
        add("hvacHeaterIsOn", heater)
        add("hvacCoolerIsOn", cooler)
        add("hvacStatus", status)
        if heat is not None:
            v = to_unit(heat, unit)
            add("setpointHeat", v, _temp_ui(v, unit), 1)
        if cool is not None:
            v = to_unit(cool, unit)
            add("setpointCool", v, _temp_ui(v, unit), 1)

    elif kind == "humidifier":
        add("onOffState", active())
        hum = idx.val(values, "RELATIVE_HUMIDITY_CURRENT")
        if hum is not None:
            add("humidity", round(float(hum), 1), f"{float(hum):.0f}%")
        cur = idx.val(values, "CURRENT_HUMIDIFIER_DEHUMIDIFIER_STATE")
        if cur is not None:
            add("humidifierStatus", HUMIDIFIER_TEXT.get(int(cur), str(cur)))
        tgt = idx.val(values, "TARGET_HUMIDIFIER_DEHUMIDIFIER_STATE")
        if tgt is not None:
            add("humidifierMode", {0: "auto", 1: "humidify", 2: "dehumidify"}.get(int(tgt), str(tgt)))

    elif kind == "valve":
        add("onOffState", active())
        if idx.has("IN_USE"):
            add("inUse", int(idx.val(values, "IN_USE", 0) or 0) == 1)
        if idx.has("REMAINING_DURATION"):
            add("remainingSeconds", int(idx.val(values, "REMAINING_DURATION", 0) or 0))
        if idx.has("VALVE_TYPE"):
            add("valveType", VALVE_TYPES.get(int(idx.val(values, "VALVE_TYPE", 0) or 0), "generic"))

    elif kind == "security":
        cur = idx.val(values, "SECURITY_SYSTEM_STATE_CURRENT")
        tgt = idx.val(values, "SECURITY_SYSTEM_STATE_TARGET")
        if cur is not None:
            add("securityState", SECURITY_TEXT.get(int(cur), str(cur)))
            add("alarmTriggered", int(cur) == 4)
        if tgt is not None:
            add("securityTarget", SECURITY_TEXT.get(int(tgt), str(tgt)))

    elif kind == "television":
        add("onOffState", active())
        ident = idx.val(values, "ACTIVE_IDENTIFIER")
        if ident is not None:
            name = next((i["name"] for i in opts.get("inputs", []) if i["id"] == ident), f"Input {ident}")
            add("activeInput", name)

    elif kind == "sensor":
        stype = opts.get("serviceType")
        sub, ctype, mode, level_t = SENSORS[stype]
        raw = idx.val_type(values, ctype)
        if mode == "bool":
            if raw is not None:
                on = bool(int(raw)) if not isinstance(raw, bool) else raw
                yes, no = BOOL_TEXT.get(sub, ("on", "off"))
                add("onOffState", on, yes if on else no)
            if level_t is not None:
                lvl = idx.val_type(values, level_t)
                if lvl is not None:
                    add("sensorValue", round(float(lvl), 1), f"{float(lvl):.0f} ppm", 0)
        elif raw is not None:
            if sub == "temperature":
                v = to_unit(raw, unit)
                add("sensorValue", v, _temp_ui(v, unit), 1)
            elif sub == "humidity":
                add("sensorValue", round(float(raw), 1), f"{float(raw):.0f}%", 0)
            elif sub == "light":
                add("sensorValue", round(float(raw), 1), f"{float(raw):.0f} lux", 0)
            elif sub == "airquality":
                add("sensorValue", int(raw), AIR_QUALITY_TEXT.get(int(raw), str(raw)), 0)

    elif kind == "generic":
        first = next((e for e in chars if e.get("s") == "p" and e["t"] not in NAME_CHARS and _readable_entry(e)), None)
        if first is not None:
            v = values.get((first["a"], first["i"]))
            if v is not None:
                add("status", f"{title(first['n'])}: {v}")

    # Shared extras
    if idx.has("BATTERY_LEVEL"):
        b = idx.val(values, "BATTERY_LEVEL")
        if b is not None:
            add("batteryLevel", int(b))
    watt = next((e for e in chars if e["t"] in ENERGY_WATT), None)
    if watt is not None and values.get((watt["a"], watt["i"])) is not None:
        w = round(float(values[(watt["a"], watt["i"])]), 1)
        add("curEnergyLevel", w, f"{w} W")
    kwh = next((e for e in chars if e["t"] in ENERGY_KWH), None)
    if kwh is not None and values.get((kwh["a"], kwh["i"])) is not None:
        k = round(float(values[(kwh["a"], kwh["i"])]), 3)
        add("accumEnergyTotal", k, f"{k} kWh")
    return st


def _readable_entry(e):
    return e.get("f") not in OPAQUE_FORMATS and bool({"pr", "ev"} & set(e.get("p", [])))


# ---------------------------------------------------------------------------
# Writes (Indigo actions -> characteristic writes)
# ---------------------------------------------------------------------------

def _w(*items):
    return [i for i in items if i is not None]


def writes_on_off(kind, chars, on, values=None):
    idx = CharIndex(chars)
    if kind in ("light", "onoff"):
        if idx.has("ON"):
            out = _w(idx.write("ON", on))
            if on and kind == "light" and values is not None and not idx.val(values, "BRIGHTNESS", 100):
                out += _w(idx.write("BRIGHTNESS", 100))  # turning on at 0% would look like nothing happened
            return out
        return _w(idx.write("ACTIVE", 1 if on else 0))
    if kind == "lock":
        return _w(idx.write("LOCK_MECHANISM_TARGET_STATE", 1 if on else 0))
    if kind == "garage":
        return _w(idx.write("DOOR_STATE_TARGET", 0 if on else 1))
    if kind == "covering":
        return _w(idx.write("POSITION_TARGET", 100 if on else 0))
    if kind == "fan":
        if idx.has("ACTIVE"):
            return _w(idx.write("ACTIVE", 1 if on else 0))
        return _w(idx.write("ON", on))
    if kind in ("humidifier", "valve", "television", "heatercooler"):
        return _w(idx.write("ACTIVE", 1 if on else 0))
    if kind == "thermostat":
        return _w(idx.write("HEATING_COOLING_TARGET", 3 if on else 0))
    return []


def writes_brightness(kind, chars, level):
    idx = CharIndex(chars)
    level = max(0, min(100, int(round(float(level)))))
    if kind == "light":
        if level == 0:
            return _w(idx.write("ON", False))
        return _w(idx.write("BRIGHTNESS", level), idx.write("ON", True))
    if kind == "covering":
        return _w(idx.write("POSITION_TARGET", level))
    return []


def writes_color(chars, rgb=None, white_temp=None, white_level=None):
    """rgb: (r, g, b) 0-100. white_temp: Kelvin. white_level: 0-100."""
    idx = CharIndex(chars)
    out = []
    if rgb is not None and idx.has("HUE"):
        r, g, b = (max(0.0, min(100.0, float(x))) / 100.0 for x in rgb)
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        out += _w(idx.write("HUE", h * 360), idx.write("SATURATION", s * 100))
        if v > 0:
            out += _w(idx.write("BRIGHTNESS", v * 100), idx.write("ON", True))
        else:
            out += _w(idx.write("ON", False))
    if white_temp and idx.has("COLOR_TEMPERATURE"):
        out += _w(idx.write("COLOR_TEMPERATURE", 1e6 / float(white_temp)))
        if rgb is None and idx.has("SATURATION") and idx.has("HUE"):
            pass  # HomeKit bulbs switch to white mode when colour temperature is written
    if white_level is not None and rgb is None:
        level = max(0, min(100, float(white_level)))
        if level > 0:
            out += _w(idx.write("BRIGHTNESS", level), idx.write("ON", True))
        else:
            out += _w(idx.write("ON", False))
    return out


def writes_speed(chars, level):
    idx = CharIndex(chars)
    level = max(0, min(100, int(round(float(level)))))
    e = idx.get("ROTATION_SPEED")
    on = level > 0
    out = []
    if e is not None and on:
        top = float(e.get("max") or 100)
        out += _w(idx.write("ROTATION_SPEED", level / 100.0 * top))
    if idx.has("ACTIVE"):
        out += _w(idx.write("ACTIVE", 1 if on else 0))
    elif idx.has("ON"):
        out += _w(idx.write("ON", on))
    return out


def writes_thermostat_mode(kind, chars, hvac_mode):
    idx = CharIndex(chars)
    hk = HK_FROM_HVAC.get(int(hvac_mode), 0)
    if kind == "thermostat":
        e = idx.get("HEATING_COOLING_TARGET")
        if e is not None and e.get("vv") and hk not in e["vv"]:
            raise ValueError(f"this thermostat does not support that mode (supports {e['vv']})")
        return _w(idx.write("HEATING_COOLING_TARGET", hk))
    if hk == 0:
        return _w(idx.write("ACTIVE", 0))
    return _w(idx.write("ACTIVE", 1), idx.write("TARGET_HEATER_COOLER_STATE", {3: 0, 1: 1, 2: 2}[hk]))


def writes_setpoint(kind, chars, values, which, value, unit):
    """which: 'heat' or 'cool'. value in the user's unit."""
    idx = CharIndex(chars)
    c = from_unit(value, unit)
    if kind == "heatercooler":
        name = "TEMPERATURE_HEATING_THRESHOLD" if which == "heat" else "TEMPERATURE_COOLING_THRESHOLD"
        return _w(idx.write(name, c))
    mode = int(idx.val(values, "HEATING_COOLING_TARGET", 0) or 0)
    threshold = "TEMPERATURE_HEATING_THRESHOLD" if which == "heat" else "TEMPERATURE_COOLING_THRESHOLD"
    if mode == 3 and idx.has(threshold):
        return _w(idx.write(threshold, c))
    if (which == "heat" and mode in (0, 1, 3)) or (which == "cool" and mode in (0, 2, 3)) or not idx.has(threshold):
        return _w(idx.write("TEMPERATURE_TARGET", c))
    return _w(idx.write(threshold, c))
