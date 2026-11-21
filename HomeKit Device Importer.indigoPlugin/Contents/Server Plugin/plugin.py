#! /usr/bin/env python
# -*- coding: utf-8 -*-
"""
HomeKit Device Importer for Indigo.

Pairs Indigo directly with HomeKit accessories (HAP over Wi-Fi/Ethernet, Thread,
and optionally Bluetooth) and turns each of their services into an Indigo device.
Pairing is done from a web page served by the Indigo Web Server:

    Plugins > HomeKit Device Importer > Open Pairing Page...

Author: Durosity
"""

import datetime
import json
import logging
import os
import queue
import subprocess
import threading
import time
import uuid

import indigo  # noqa

try:
    import service_map as sm
    from hap_engine import EngineError, HapEngine, friendly_error
    IMPORT_ERROR = None
except Exception as _err:  # dependencies still installing, or broken
    IMPORT_ERROR = _err

import web_ui

PAIRING_TYPE = "hkPairing"
LOGGED_STATES = ("onOffState", "brightnessLevel", "speedLevel", "hvacOperationMode", "securityState", "activeInput",
                 "doorStatus", "lockStatus")
SUBTYPE_CANDIDATES = {
    "Bulb": ["Bulb"], "Outlet": ["Outlet", "Plug"], "Switch": ["Switch"], "Lock": ["Lock"], "Blind": ["Blind", "Blinds"],
    "Temperature": ["Temperature"], "Humidity": ["Humidity"], "Illuminance": ["Illuminance", "Light"],
    "Motion": ["Motion"], "Presence": ["Presence", "Occupancy"], "DoorWindow": ["DoorWindow", "Door"],
    "Water": ["Water", "Leak", "Moisture"], "Smoke": ["Smoke"], "CO": ["CO", "CarbonMonoxide"],
    "CO2": ["CO2", "CarbonDioxide"], "AirQuality": ["AirQuality"],
}
MODE_NAMES = {0: "off", 1: "heat", 2: "cool", 3: "auto"}


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Plugin(indigo.PluginBase):

    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        super().__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.debug = bool(pluginPrefs.get("debugLogging", False))
        self.engine = None
        self.events = queue.Queue()
        self.store = {}
        self.store_lock = threading.RLock()
        self.values = {}            # alias -> {(aid, iid): value}
        self.accessories = {}       # alias -> raw accessory database
        self.online = {}            # alias -> bool
        self.reasons = {}           # alias -> last offline reason
        self.infos = {}             # alias -> engine info dict
        self.discovered = []        # last discovery list, for the page
        self.dev_index = {}         # (alias, aid, iid) -> {device id}
        self.dev_by_key = {}        # service key -> device id
        self.dev_meta = {}          # device id -> (alias, kind, chars, props)
        self.pairing_devs = {}      # alias -> pairing device id
        self.triggers = {}
        self.jobs = {}
        self.engine_error = ""

    # ======================================================================
    # Lifecycle
    # ======================================================================

    def startup(self):
        self.logger.debug("startup")
        if IMPORT_ERROR is not None:
            self.engine_error = f"Missing dependencies ({IMPORT_ERROR}). If Indigo is still installing them, " \
                                f"restart the plugin in a minute."
            self.logger.error(self.engine_error)
            return
        self._route_library_logging()
        self._load_store()
        self._start_engine()
        self.logger.info(f"Pairing page: {self.page_url()}")

    def shutdown(self):
        self.logger.debug("shutdown")
        if self.engine:
            self.engine.stop()
            self.engine = None
        self._save_store()

    def _route_library_logging(self):
        # The library's own output goes to the plugin log file only, and only with debug logging on:
        # the plugin already reports connection problems once, in plain words, in the Event Log.
        handler = getattr(self, "plugin_file_handler", None)
        for name in ("aiohomekit", "zeroconf"):
            lib = logging.getLogger(name)
            lib.propagate = False
            lib.setLevel(logging.DEBUG if self.debug else logging.CRITICAL)
            if handler is not None and handler not in lib.handlers:
                lib.addHandler(handler)

    def _engine_settings(self):
        prefs = self.pluginPrefs
        interfaces = [i.strip() for i in str(prefs.get("interfaces", "")).split(",") if i.strip()]
        return {
            "interfaces": interfaces,
            "enable_ble": bool(prefs.get("enableBluetooth", False)),
            "enable_thread": bool(prefs.get("enableThread", True)),
            "poll_seconds": self._int_pref("pollInterval", 60, 10),
        }

    def _int_pref(self, key, default, minimum):
        try:
            return max(minimum, int(self.pluginPrefs.get(key, default)))
        except (TypeError, ValueError):
            return default

    def _start_engine(self):
        settings = self._engine_settings()
        self.engine = HapEngine(self.logger, self.events.put, **settings)
        self.engine.debug = self.debug
        try:
            self.engine.start()
        except Exception as err:
            self.engine_error = str(err)
            self.logger.error(f"Couldn't start the HomeKit controller: {err}")
            self.engine = None
            return
        self.engine_error = ""
        self._engine_settings_used = settings
        extras = [n for n, on in (("Thread", settings["enable_thread"]), ("Bluetooth", settings["enable_ble"])) if on]
        self.logger.info(f"HomeKit controller started (Wi-Fi/Ethernet{''.join(', ' + e for e in extras)})")
        with self.store_lock:
            aliases = list(self.store.items())
        for alias, record in aliases:
            self._load_pairing(alias, record)

    def _load_pairing(self, alias, record):
        future = self.engine.submit(self.engine.load, alias, record["pairingData"])

        def done(fut, alias=alias, name=record.get("name", alias)):
            err = fut.exception()
            if err:
                self.logger.error(f'Couldn\'t load the pairing for "{name}": {friendly_error(err)}')
            else:
                self.logger.debug(f'Loaded pairing for "{name}"')
        future.add_done_callback(done)

    def _restart_engine(self):
        if self.engine:
            self.engine.stop()
            self.engine = None
        self.online.clear()
        self._start_engine()

    def runConcurrentThread(self):
        try:
            while True:
                handled = 0
                while handled < 500:
                    try:
                        event = self.events.get(timeout=0.25 if handled == 0 else 0)
                    except queue.Empty:
                        break
                    handled += 1
                    try:
                        self._handle_event(event)
                    except Exception:
                        self.logger.exception(f"Error handling {event[0]} event")
                self.sleep(0.01)
        except self.StopThread:
            pass

    # ======================================================================
    # Preferences
    # ======================================================================

    def validatePrefsConfigUi(self, valuesDict):
        errors = indigo.Dict()
        try:
            if int(valuesDict.get("pollInterval", 60)) < 10:
                raise ValueError
        except (TypeError, ValueError):
            errors["pollInterval"] = "Use a whole number of seconds, 10 or more."
        url = str(valuesDict.get("iwsUrl", "")).strip()
        if url and not url.startswith(("http://", "https://")):
            errors["iwsUrl"] = "Start the address with http:// or https://"
        if errors:
            return False, valuesDict, errors
        return True, valuesDict

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        if userCancelled:
            return
        self.debug = bool(valuesDict.get("debugLogging", False))
        self._route_library_logging()
        if IMPORT_ERROR is not None:
            return
        if self.engine:
            self.engine.debug = self.debug
            self.engine.poll_seconds = self._int_pref("pollInterval", 60, 10)
        settings = self._engine_settings()
        used = getattr(self, "_engine_settings_used", None)
        if used and {k: v for k, v in settings.items() if k != "poll_seconds"} != \
                {k: v for k, v in used.items() if k != "poll_seconds"}:
            self.logger.info("Connection settings changed, restarting the HomeKit controller")
            self._restart_engine()
        for dev_id in list(self.dev_meta):
            self._refresh_device(dev_id)

    # ======================================================================
    # Pairing store (pairing keys live in a private file, not in the prefs XML)
    # ======================================================================

    def _store_path(self):
        folder = os.path.join(indigo.server.getInstallFolderPath(), "Preferences", "Plugins", self.pluginId)
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, "pairings.json")

    def _load_store(self):
        path = self._store_path()
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as err:
            self.logger.error(f"Couldn't read {path}: {err}")
            data = {}
        with self.store_lock:
            self.store = data if isinstance(data, dict) else {}
            for alias, record in self.store.items():
                self.accessories[alias] = record.get("accessories", [])
        self.logger.debug(f"Loaded {len(self.store)} pairings")

    def _save_store(self):
        try:
            path = self._store_path()
        except Exception as err:
            self.logger.error(f"Can't save pairings: {err}")
            return
        tmp = path + ".tmp"
        with self.store_lock:
            text = json.dumps(self.store, indent=1, default=str)
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError as err:
            self.logger.error(f"Couldn't save pairings to {path}: {err}")

    # ======================================================================
    # Engine events (all handled on the runConcurrentThread thread)
    # ======================================================================

    def _handle_event(self, event):
        kind, alias = event[0], event[1]
        if kind == "structure":
            self._on_structure(alias, event[2])
        elif kind == "values":
            self._on_values(alias, event[2], event[3])
        elif kind == "availability":
            self._on_availability(alias, event[2], event[3])
        elif kind == "info":
            self.infos[alias] = event[2]
            self._update_pairing_device(alias)
        elif kind == "paired":
            self._on_paired(event[2])
        elif kind == "unpaired":
            self._on_unpaired(alias, event[2], event[3])
        elif kind == "call":
            event[2]()

    def _on_structure(self, alias, accessories):
        self.accessories[alias] = accessories
        with self.store_lock:
            record = self.store.get(alias)
            if record is None:
                return  # pairing still being saved; _on_paired will sync
            if record.get("accessories") != accessories:
                record["accessories"] = accessories
                if accessories:
                    record["name"] = record.get("customName") or sm.accessory_name(accessories[0])
                self._save_store()
        self._sync_devices(alias)

    def _on_values(self, alias, changes, source):
        cache = self.values.setdefault(alias, {})
        cache.update(changes)
        affected = set()
        for key in changes:
            affected |= self.dev_index.get((alias, key[0], key[1]), set())
        for dev_id in affected:
            self._refresh_device(dev_id, log=source != "snapshot")
            if source == "event":
                self._check_button(dev_id, changes)

    def _on_availability(self, alias, online, reason):
        was = self.online.get(alias)
        self.online[alias] = online
        self.reasons[alias] = reason
        name = self._pairing_name(alias)
        if online and was is False:
            self.logger.info(f'"{name}" is back online')
        elif not online and was is not False:
            self.logger.warning(f'"{name}" is offline: {reason}')
        self._update_pairing_device(alias)
        for dev_id, meta in list(self.dev_meta.items()):
            if meta[0] != alias:
                continue
            dev = self._get_dev(dev_id)
            if dev is None:
                continue
            if online:
                self._refresh_device(dev_id, log=False, clear_error=True)
            else:
                self._set_error(dev, "offline")
        self._fire_connection_triggers(alias, online)

    # ======================================================================
    # Device creation and sync
    # ======================================================================

    def _sync_devices(self, alias, force_keys=None):
        """Create devices for new services and refresh props of existing ones."""
        accessories = self.accessories.get(alias) or []
        if not accessories:
            return []
        with self.store_lock:
            record = self.store.get(alias)
            if record is None:
                return []
            known = set(record.get("known", []))
        auto = bool(self.pluginPrefs.get("autoCreateDevices", True))
        created = []

        if f"{alias}:pairing" not in known and alias not in self.pairing_devs:
            self._create_pairing_device(alias)
            known.add(f"{alias}:pairing")

        for plan in sm.plan_accessories(alias, accessories):
            key = plan["key"]
            dev_id = self.dev_by_key.get(key) or self._find_device_by_key(key)
            if dev_id:
                self._update_device_props(dev_id, plan)
            elif (auto and key not in known) or (force_keys and key in force_keys):
                dev = self._create_device(alias, plan)
                if dev is not None:
                    created.append(dev.id)
            known.add(key)

        with self.store_lock:
            if alias in self.store and set(self.store[alias].get("known", [])) != known:
                self.store[alias]["known"] = sorted(known)
                self._save_store()
        return created

    def _find_device_by_key(self, key):
        for dev in indigo.devices.iter("self"):
            if dev.pluginProps.get("hkKey") == key:
                return dev.id
        return None

    def _folder_id(self, alias):
        if not self.pluginPrefs.get("createFolders", True):
            return 0
        name = self._pairing_name(alias)
        try:
            if name in indigo.devices.folders:
                return indigo.devices.folders[name].id
            return indigo.devices.folder.create(name).id
        except Exception as err:
            self.logger.debug(f"Folder {name}: {err}")
            return 0

    def _unique_name(self, wanted):
        name, n = wanted, 2
        while name in indigo.devices:
            name, n = f"{wanted} ({n})", n + 1
        return name

    def _create_pairing_device(self, alias):
        with self.store_lock:
            record = dict(self.store.get(alias, {}))
        name = self._unique_name(self._pairing_name(alias))
        props = {"hkPairing": alias, "address": alias.upper()}
        try:
            dev = indigo.device.create(protocol=indigo.kProtocol.Plugin, address=alias.upper(), name=name,
                                       description=record.get("model", ""), deviceTypeId=PAIRING_TYPE,
                                       props=props, folder=self._folder_id(alias))
            self.logger.info(f'Created "{dev.name}" for the HomeKit pairing')
            return dev
        except Exception as err:
            self.logger.error(f'Couldn\'t create a device for "{name}": {err}')
            return None

    def _device_props(self, alias, plan):
        props = dict(plan["props"])
        props.update({
            "hkPairing": alias, "hkKey": plan["key"], "hkAid": plan["aid"], "hkSid": plan["sid"],
            "hkKind": plan["kind"], "hkService": plan["service"], "hkServiceType": plan["serviceType"],
            "hkChars": json.dumps(plan["chars"], separators=(",", ":")),
            "hkInputs": json.dumps(plan["inputs"], separators=(",", ":")),
            "hkSensor": plan.get("sensor", ""), "address": f"{alias.upper()} · {plan['aid']}.{plan['sid']}",
        })
        return props

    def _create_device(self, alias, plan):
        name = self._unique_name(plan["name"])
        info = plan.get("info", {})
        description = " ".join(x for x in (info.get("manufacturer", ""), info.get("model", "")) if x)
        props = self._device_props(alias, plan)
        try:
            dev = indigo.device.create(protocol=indigo.kProtocol.Plugin, address=props["address"], name=name,
                                       description=description, deviceTypeId=plan["deviceType"], props=props,
                                       folder=self._folder_id(alias))
        except Exception as err:
            self.logger.error(f'Couldn\'t create "{name}": {err}')
            return None
        self.logger.info(f'Created "{dev.name}" ({sm.KIND_LABELS.get(plan["kind"], "sensor")})')
        return dev

    def _update_device_props(self, dev_id, plan):
        dev = self._get_dev(dev_id)
        if dev is None:
            return
        props = dev.pluginProps
        alias = props.get("hkPairing", "")
        new = self._device_props(alias, plan)
        changed = [k for k, v in new.items() if k != "address" and props.get(k) != v]
        if not changed:
            return
        merged = dict(props)
        merged.update(new)
        merged.pop("hkSubTypeApplied", None) if "hkSubType" in changed else None
        dev.replacePluginPropsOnServer(merged)
        dev = self._get_dev(dev_id)
        if dev is not None:
            dev.stateListOrDisplayStateIdChanged()
            self._index_device(dev)
            self._refresh_device(dev_id, log=False)
        self.logger.debug(f'Updated "{dev.name if dev else dev_id}" ({", ".join(changed)})')

    def add_service_device(self, alias, key):
        """Create the Indigo device for one service (from the page)."""
        created = self._sync_devices(alias, force_keys={key})
        if not created:
            raise EngineError("That service already has a device, or no longer exists.")
        return created[0]

    # ======================================================================
    # Device lifecycle
    # ======================================================================

    def deviceStartComm(self, dev):
        props = dev.pluginProps
        if dev.deviceTypeId == PAIRING_TYPE:
            alias = props.get("hkPairing", "")
            self.pairing_devs[alias] = dev.id
            self._update_pairing_device(alias)
            return
        if not props.get("hkKey"):
            self.logger.warning(f'"{dev.name}" wasn\'t created from a HomeKit pairing. Add HomeKit devices from '
                                f'Plugins > HomeKit Device Importer > Open Pairing Page.')
            return
        dev.stateListOrDisplayStateIdChanged()
        self._apply_subtype(dev)
        dev = self._get_dev(dev.id) or dev
        self._index_device(dev)
        alias = props.get("hkPairing")
        if self.online.get(alias) is False:
            self._set_error(dev, "offline")
        else:
            self._refresh_device(dev.id, log=False)

    def deviceStopComm(self, dev):
        self._unindex_device(dev.id)
        for alias, dev_id in list(self.pairing_devs.items()):
            if dev_id == dev.id:
                del self.pairing_devs[alias]

    def _index_device(self, dev):
        self._unindex_device(dev.id)
        props = dev.pluginProps
        alias = props.get("hkPairing", "")
        try:
            chars = json.loads(props.get("hkChars", "[]"))
        except ValueError:
            chars = []
        self.dev_meta[dev.id] = (alias, props.get("hkKind", "generic"), chars, dict(props))
        self.dev_by_key[props.get("hkKey")] = dev.id
        for e in chars:
            self.dev_index.setdefault((alias, e["a"], e["i"]), set()).add(dev.id)

    def _unindex_device(self, dev_id):
        meta = self.dev_meta.pop(dev_id, None)
        if meta is None:
            return
        alias, _kind, chars, props = meta
        if self.dev_by_key.get(props.get("hkKey")) == dev_id:
            del self.dev_by_key[props.get("hkKey")]
        for e in chars:
            ids = self.dev_index.get((alias, e["a"], e["i"]))
            if ids:
                ids.discard(dev_id)

    def _apply_subtype(self, dev):
        props = dev.pluginProps
        wanted = props.get("hkSubType")
        if not wanted or props.get("hkSubTypeApplied") == wanted:
            return
        family = {"relay": "kRelayDeviceSubType", "dimmer": "kDimmerDeviceSubType",
                  "sensor": "kSensorDeviceSubType"}.get(self._base_type(dev))
        enum = getattr(indigo, family, None) if family else None
        value = None
        for candidate in SUBTYPE_CANDIDATES.get(wanted, [wanted]):
            value = getattr(enum, candidate, None) if enum is not None else None
            if value is not None:
                break
        if value is not None:
            try:
                dev.subType = value
                dev.replaceOnServer()
            except Exception as err:
                self.logger.debug(f'"{dev.name}": couldn\'t set subtype {wanted}: {err}')
        new_props = self._get_dev(dev.id).pluginProps
        new_props["hkSubTypeApplied"] = wanted
        self._get_dev(dev.id).replacePluginPropsOnServer(new_props)

    @staticmethod
    def _base_type(dev):
        name = type(dev).__name__.lower()
        for t in ("dimmer", "relay", "sensor", "speedcontrol", "thermostat"):
            if t in name:
                return t
        return "custom"

    def getDeviceStateList(self, dev):
        state_list = indigo.PluginBase.getDeviceStateList(self, dev)
        if dev.deviceTypeId == PAIRING_TYPE or IMPORT_ERROR is not None:
            return state_list
        try:
            chars = json.loads(dev.pluginProps.get("hkChars", "[]"))
        except ValueError:
            return state_list
        existing = {s["Key"] for s in state_list}
        makers = {"bool": self.getDeviceStateDictForBoolTrueFalseType, "string": self.getDeviceStateDictForStringType,
                  "number": self.getDeviceStateDictForNumberType, "integer": self.getDeviceStateDictForNumberType}
        for sid, kind, label, _e in sm.dynamic_states(chars):
            if sid not in existing:
                state_list.append(makers[kind](sid, label, label))
                existing.add(sid)
        return state_list

    def validateDeviceConfigUi(self, valuesDict, typeId, devId):
        if typeId != PAIRING_TYPE and not valuesDict.get("hkKey"):
            errors = indigo.Dict()
            errors["showAlertText"] = ("HomeKit devices are added from the pairing page: "
                                       "Plugins > HomeKit Device Importer > Open Pairing Page.")
            return False, valuesDict, errors
        return True, valuesDict

    # ======================================================================
    # State updates
    # ======================================================================

    def _get_dev(self, dev_id):
        try:
            return indigo.devices[dev_id]
        except (KeyError, ValueError):
            return None

    def _set_error(self, dev, text):
        try:
            dev.setErrorStateOnServer(text)
        except Exception as err:
            self.logger.debug(f"setErrorStateOnServer: {err}")

    def _opts(self, meta):
        alias, kind, chars, props = meta
        try:
            inputs = json.loads(props.get("hkInputs", "[]"))
        except ValueError:
            inputs = []
        return {"tempUnit": self.pluginPrefs.get("tempUnit", "C"), "serviceType": props.get("hkServiceType"),
                "inputs": inputs}

    def _refresh_device(self, dev_id, log=False, clear_error=False):
        meta = self.dev_meta.get(dev_id)
        dev = self._get_dev(dev_id)
        if meta is None or dev is None:
            return
        alias, kind, chars, props = meta
        values = self.values.get(alias, {})
        if not values:
            return
        updates = sm.compute_states(kind, props, chars, values, self._opts(meta)) + \
            sm.dynamic_state_values(chars, values)
        changed = []
        for u in updates:
            if u["key"] not in dev.states:
                continue
            if dev.states[u["key"]] != u["value"] or ("uiValue" in u and dev.states.get(u["key"] + ".ui") not in
                                                       (None, u["uiValue"])):
                changed.append(u)
        if clear_error and dev.errorState:
            self._set_error(dev, None)
        if not changed:
            return
        try:
            dev.updateStatesOnServer(changed)
        except Exception as err:
            self.logger.debug(f'"{dev.name}": state update failed: {err}')
            return
        if log and self.pluginPrefs.get("logChanges", True):
            for u in changed:
                if u["key"] in LOGGED_STATES or (u["key"] == "sensorValue" and self.pluginPrefs.get("logSensors", False)):
                    self.logger.info(f'received "{dev.name}" {self._describe_change(dev, u)}')
                    break
        self._update_state_image(dev_id)

    @staticmethod
    def _describe_change(dev, u):
        key, val = u["key"], u["value"]
        ui = u.get("uiValue")
        if key == "onOffState":
            return ui or ("on" if val else "off")
        if key == "brightnessLevel":
            return f"brightness {val}" if val else "off"
        if key == "speedLevel":
            return f"speed {val}" if val else "off"
        if key == "hvacOperationMode":
            return f"mode {MODE_NAMES.get(val, val)}"
        return ui or str(val)

    def _update_state_image(self, dev_id):
        meta = self.dev_meta.get(dev_id)
        dev = self._get_dev(dev_id)
        if meta is None or dev is None:
            return
        kind = meta[1]
        sel = indigo.kStateImageSel
        image = None
        try:
            if kind == "security":
                image = sel.SensorTripped if dev.states.get("alarmTriggered") else (
                    sel.SensorOff if dev.states.get("securityState") == "disarmed" else sel.SensorOn)
            elif kind == "button":
                image = sel.SensorOn
            elif kind == "generic":
                image = sel.SensorOn
            if image is not None:
                dev.updateStateImageOnServer(image)
        except Exception:
            pass

    def _check_button(self, dev_id, changes):
        meta = self.dev_meta.get(dev_id)
        if meta is None or meta[1] != "button":
            return
        alias, _kind, chars, _props = meta
        idx = sm.CharIndex(chars)
        e = idx.get("INPUT_EVENT")
        if e is None or (e["a"], e["i"]) not in changes:
            return
        try:
            press = sm.PRESS_TEXT.get(int(changes[(e["a"], e["i"])]), "single")
        except (TypeError, ValueError):
            return
        dev = self._get_dev(dev_id)
        if dev is None:
            return
        count = int(dev.states.get("pressCount", 0) or 0) + 1
        dev.updateStatesOnServer([
            {"key": "lastPress", "value": press}, {"key": "lastPressTime", "value": _now()},
            {"key": "pressCount", "value": count},
        ])
        if self.pluginPrefs.get("logChanges", True):
            self.logger.info(f'received "{dev.name}" {press} press')
        for trigger in list(self.triggers.values()):
            if trigger.pluginTypeId != "buttonPressed":
                continue
            p = trigger.pluginProps
            if str(p.get("deviceId", "")) != str(dev_id):
                continue
            if p.get("pressType", "any") in ("any", press):
                indigo.trigger.execute(trigger)

    # ----- pairing device ----------------------------------------------------

    def _pairing_name(self, alias):
        with self.store_lock:
            record = self.store.get(alias, {})
        return record.get("customName") or record.get("name") or alias.upper()

    def _update_pairing_device(self, alias):
        dev_id = self.pairing_devs.get(alias)
        dev = self._get_dev(dev_id) if dev_id else None
        if dev is None:
            return
        info = self.infos.get(alias, {})
        with self.store_lock:
            record = dict(self.store.get(alias, {}))
        online = self.online.get(alias)
        status = "online" if online else ("connecting" if online is None else "offline")
        accessories = self.accessories.get(alias) or []
        acc_info = sm.accessory_info(accessories[0]) if accessories else {}
        states = [
            {"key": "status", "value": status},
            {"key": "reason", "value": "" if online else self.reasons.get(alias, "")},
            {"key": "deviceId", "value": alias.upper()},
            {"key": "transport", "value": info.get("transport", record.get("transport", ""))},
            {"key": "address", "value": f"{info.get('address', '')}:{info.get('port')}" if info.get("port") else
             info.get("address", "")},
            {"key": "category", "value": record.get("categoryName", "")},
            {"key": "manufacturer", "value": acc_info.get("manufacturer", "")},
            {"key": "model", "value": acc_info.get("model", record.get("model", ""))},
            {"key": "firmware", "value": acc_info.get("firmware", "")},
            {"key": "accessoryCount", "value": len(accessories)},
        ]
        if online:
            states.append({"key": "lastSeen", "value": _now()})
        changed = [s for s in states if s["key"] in dev.states and dev.states[s["key"]] != s["value"]]
        if changed:
            dev.updateStatesOnServer(changed)
        try:
            dev.updateStateImageOnServer(indigo.kStateImageSel.SensorOn if online else indigo.kStateImageSel.SensorOff)
        except Exception:
            pass
        if online is False:
            self._set_error(dev, "offline")
        elif online and dev.errorState:
            self._set_error(dev, None)

    # ======================================================================
    # Sending commands
    # ======================================================================

    def _meta_for(self, dev):
        meta = self.dev_meta.get(dev.id)
        if meta is None:
            self._index_device(dev)
            meta = self.dev_meta.get(dev.id)
        return meta

    def _send(self, dev, writes, what):
        meta = self._meta_for(dev)
        if not writes:
            self.logger.error(f'"{dev.name}" doesn\'t support {what}')
            return False
        if self.engine is None:
            self.logger.error(f'send "{dev.name}" {what} failed: the HomeKit controller isn\'t running')
            return False
        alias = meta[0]
        if self.online.get(alias) is False:
            self.logger.error(f'send "{dev.name}" {what} failed: "{self._pairing_name(alias)}" is offline')
            return False
        if self.pluginPrefs.get("logChanges", True):
            self.logger.info(f'sent "{dev.name}" {what}')
        self.logger.debug(f'"{dev.name}" writes {writes}')
        future = self.engine.submit(self.engine.put, alias, writes)
        name = dev.name

        def done(fut):
            err = fut.exception()
            if err:
                self.logger.error(f'send "{name}" {what} failed: {friendly_error(err)}')
        future.add_done_callback(done)
        return True

    def _refresh_request(self, dev):
        meta = self._meta_for(dev)
        alias = meta[0] if meta else dev.pluginProps.get("hkPairing")
        if not alias or self.engine is None:
            return
        self.logger.info(f'sent "{dev.name}" status request')
        self.engine.submit(self.engine.refresh, alias).add_done_callback(
            lambda fut: fut.exception() and self.logger.error(
                f'status request for "{dev.name}" failed: {friendly_error(fut.exception())}'))

    # ----- native device actions ---------------------------------------------

    def actionControlDevice(self, action, dev):
        meta = self._meta_for(dev)
        if meta is None:
            return
        alias, kind, chars, _props = meta
        values = self.values.get(alias, {})
        da = indigo.kDeviceAction
        a = action.deviceAction
        try:
            if a == da.TurnOn:
                self._send(dev, sm.writes_on_off(kind, chars, True, values), self._on_word(kind, True))
            elif a == da.TurnOff:
                self._send(dev, sm.writes_on_off(kind, chars, False, values), self._on_word(kind, False))
            elif a == da.Toggle:
                on = not bool(dev.onState)
                self._send(dev, sm.writes_on_off(kind, chars, on, values), self._on_word(kind, on))
            elif a == da.SetBrightness:
                level = int(action.actionValue)
                self._send(dev, sm.writes_brightness(kind, chars, level), self._level_word(kind, level))
            elif a == da.BrightenBy:
                level = min(100, int(dev.brightness) + int(action.actionValue))
                self._send(dev, sm.writes_brightness(kind, chars, level), self._level_word(kind, level))
            elif a == da.DimBy:
                level = max(0, int(dev.brightness) - int(action.actionValue))
                self._send(dev, sm.writes_brightness(kind, chars, level), self._level_word(kind, level))
            elif a == da.SetColorLevels:
                self._set_color(dev, chars, action.actionValue)
            elif a == getattr(da, "RequestStatus", object()):
                self._refresh_request(dev)
            else:
                self.logger.error(f'"{dev.name}" doesn\'t support that action')
        except ValueError as err:
            self.logger.error(f'"{dev.name}": {err}')

    @staticmethod
    def _on_word(kind, on):
        return {"lock": ("lock", "unlock"), "garage": ("open", "close"), "covering": ("open", "close")}.get(
            kind, ("on", "off"))[0 if on else 1]

    @staticmethod
    def _level_word(kind, level):
        return f"position to {level}%" if kind == "covering" else f"brightness to {level}"

    def _set_color(self, dev, chars, value):
        value = dict(value or {})
        rgb = None
        if any(k in value for k in ("redLevel", "greenLevel", "blueLevel")):
            rgb = (float(value.get("redLevel", dev.states.get("redLevel", 0))),
                   float(value.get("greenLevel", dev.states.get("greenLevel", 0))),
                   float(value.get("blueLevel", dev.states.get("blueLevel", 0))))
        white_temp = value.get("whiteTemperature")
        white_level = value.get("whiteLevel")
        writes = sm.writes_color(chars, rgb=rgb, white_temp=white_temp, white_level=white_level)
        bits = []
        if rgb:
            bits.append("RGB " + ", ".join(str(int(round(x))) for x in rgb))
        if white_temp:
            bits.append(f"{int(white_temp)}K")
        if white_level is not None and not rgb:
            bits.append(f"white {int(float(white_level))}")
        self._send(dev, writes, "colour to " + " ".join(bits) if bits else "colour")

    def actionControlSpeedControl(self, action, dev):
        meta = self._meta_for(dev)
        if meta is None:
            return
        _alias, kind, chars, _props = meta
        sa = indigo.kSpeedControlAction
        a = action.speedControlAction
        steps = {0: 0, 1: 33, 2: 66, 3: 100}
        current = int(dev.states.get("speedLevel", 0) or 0)
        if a == sa.TurnOn:
            self._send(dev, sm.writes_on_off(kind, chars, True), "on")
        elif a == sa.TurnOff:
            self._send(dev, sm.writes_on_off(kind, chars, False), "off")
        elif a == sa.Toggle:
            on = current == 0
            self._send(dev, sm.writes_on_off(kind, chars, on), "on" if on else "off")
        elif a == sa.SetSpeedLevel:
            level = int(action.actionValue)
            self._send(dev, sm.writes_speed(chars, level), f"speed to {level}")
        elif a == sa.SetSpeedIndex:
            level = steps.get(int(action.actionValue), 100)
            self._send(dev, sm.writes_speed(chars, level), f"speed to {level}")
        elif a in (sa.IncreaseSpeedIndex, sa.DecreaseSpeedIndex):
            index = int(dev.states.get("speedIndex", 0) or 0)
            index = min(3, index + 1) if a == sa.IncreaseSpeedIndex else max(0, index - 1)
            level = steps[index]
            self._send(dev, sm.writes_speed(chars, level), f"speed to {level}")
        elif a == getattr(sa, "RequestStatus", object()):
            self._refresh_request(dev)

    def actionControlThermostat(self, action, dev):
        meta = self._meta_for(dev)
        if meta is None:
            return
        alias, kind, chars, _props = meta
        ta = indigo.kThermostatAction
        a = action.thermostatAction
        unit = self.pluginPrefs.get("tempUnit", "C")
        values = self.values.get(alias, {})
        try:
            if a == ta.SetHvacMode:
                mode = int(action.actionMode)
                self._send(dev, sm.writes_thermostat_mode(kind, chars, mode), f"mode to {MODE_NAMES.get(mode, mode)}")
            elif a in (ta.SetHeatSetpoint, ta.IncreaseHeatSetpoint, ta.DecreaseHeatSetpoint):
                cur = float(dev.heatSetpoint)
                new = float(action.actionValue) if a == ta.SetHeatSetpoint else (
                    cur + float(action.actionValue) if a == ta.IncreaseHeatSetpoint else cur - float(action.actionValue))
                self._send(dev, sm.writes_setpoint(kind, chars, values, "heat", new, unit),
                           f"heat setpoint to {new:.1f}°{unit}")
            elif a in (ta.SetCoolSetpoint, ta.IncreaseCoolSetpoint, ta.DecreaseCoolSetpoint):
                cur = float(dev.coolSetpoint)
                new = float(action.actionValue) if a == ta.SetCoolSetpoint else (
                    cur + float(action.actionValue) if a == ta.IncreaseCoolSetpoint else cur - float(action.actionValue))
                self._send(dev, sm.writes_setpoint(kind, chars, values, "cool", new, unit),
                           f"cool setpoint to {new:.1f}°{unit}")
            elif a == ta.SetFanMode:
                self.logger.error(f'"{dev.name}" doesn\'t support fan modes; use Set HomeKit Characteristic instead')
            else:
                self._refresh_request(dev)
        except ValueError as err:
            self.logger.error(f'"{dev.name}": {err}')

    def actionControlSensor(self, action, dev):
        if action.sensorAction == getattr(indigo.kSensorAction, "RequestStatus", object()):
            self._refresh_request(dev)
        else:
            self.logger.error(f'"{dev.name}" is a HomeKit sensor and can\'t be turned on or off')

    def actionControlUniversal(self, action, dev):
        ua = indigo.kUniversalAction
        if action.deviceAction == ua.RequestStatus:
            self._refresh_request(dev)
        elif action.deviceAction == getattr(ua, "Beep", object()):
            self._identify_device(dev)
        elif action.deviceAction in (getattr(ua, "EnergyUpdate", object()),):
            self._refresh_request(dev)
        elif action.deviceAction == getattr(ua, "EnergyReset", object()):
            self.logger.error(f'"{dev.name}": HomeKit accessories can\'t reset their energy total from Indigo')

    # ----- plugin actions (Actions.xml) --------------------------------------

    def _char_write(self, dev, name, value, what):
        meta = self._meta_for(dev)
        if meta is None:
            return
        idx = sm.CharIndex(meta[2])
        try:
            w = idx.write(name, value)
        except ValueError as err:
            self.logger.error(f'"{dev.name}": {err}')
            return
        if w is None:
            self.logger.error(f'"{dev.name}" doesn\'t support {what}')
            return
        self._send(dev, [w], what)

    def actionSetCharacteristic(self, action, dev):
        meta = self._meta_for(dev)
        if meta is None:
            return
        target = action.props.get("characteristic", "")
        entry = next((e for e in meta[2] if f"{e['a']}.{e['i']}" == target), None)
        if entry is None:
            self.logger.error(f'"{dev.name}": pick a characteristic in the action settings')
            return
        raw = self.substitute(str(action.props.get("value", "")))
        try:
            value = sm.parse_value(entry, raw)
        except ValueError as err:
            self.logger.error(f'"{dev.name}": {err}')
            return
        self._send(dev, [(entry["a"], entry["i"], value)], f"{sm.title(entry['n'])} to {value}")

    def actionIdentify(self, action, dev):
        self._identify_device(dev)

    def _identify_device(self, dev):
        props = dev.pluginProps
        alias = props.get("hkPairing")
        aid = int(props.get("hkAid", 1) or 1)
        if not alias or self.engine is None:
            return
        self.logger.info(f'sent "{dev.name}" identify')
        self.engine.submit(self.engine.identify, alias, aid).add_done_callback(
            lambda fut: fut.exception() and self.logger.error(
                f'identify "{dev.name}" failed: {friendly_error(fut.exception())}'))

    def actionSecurityMode(self, action, dev):
        mode = action.props.get("mode", "disarmed")
        self._char_write(dev, "SECURITY_SYSTEM_STATE_TARGET", sm.SECURITY_TARGETS.get(mode, 3), f"arm mode {mode}")

    def actionFanDirection(self, action, dev):
        want = action.props.get("direction", "toggle")
        if want == "toggle":
            want = "clockwise" if dev.states.get("direction") == "counterclockwise" else "counterclockwise"
        self._char_write(dev, "ROTATION_DIRECTION", 1 if want == "counterclockwise" else 0, f"direction {want}")

    def actionFanSwing(self, action, dev):
        want = action.props.get("swing", "toggle")
        on = (not dev.states.get("swing", False)) if want == "toggle" else want == "on"
        self._char_write(dev, "SWING_MODE", 1 if on else 0, f"swing {'on' if on else 'off'}")

    def actionFanMode(self, action, dev):
        mode = action.props.get("mode", "auto")
        meta = self._meta_for(dev)
        name = "AIR_PURIFIER_STATE_TARGET" if sm.CharIndex(meta[2]).has("AIR_PURIFIER_STATE_TARGET") else "FAN_STATE_TARGET"
        self._char_write(dev, name, 1 if mode == "auto" else 0, f"mode {mode}")

    def actionCoverTilt(self, action, dev):
        meta = self._meta_for(dev)
        try:
            angle = int(float(self.substitute(str(action.props.get("angle", "0")))))
        except ValueError:
            self.logger.error(f'"{dev.name}": the tilt angle must be a number from -90 to 90')
            return
        idx = sm.CharIndex(meta[2])
        name = next((n for n in ("HORIZONTAL_TILT_TARGET", "VERTICAL_TILT_TARGET", "TILT_TARGET") if idx.has(n)), None)
        if name is None:
            self.logger.error(f'"{dev.name}" doesn\'t support tilt')
            return
        self._char_write(dev, name, angle, f"tilt to {angle}°")

    def actionCoverStop(self, action, dev):
        self._char_write(dev, "POSITION_HOLD", True, "stop")

    def actionTvInput(self, action, dev):
        try:
            ident = int(action.props.get("input", ""))
        except ValueError:
            self.logger.error(f'"{dev.name}": pick an input in the action settings')
            return
        self._char_write(dev, "ACTIVE_IDENTIFIER", ident, "input change")

    def actionTvKey(self, action, dev):
        key = action.props.get("key", "select")
        self._char_write(dev, "REMOTE_KEY", sm.REMOTE_KEYS.get(key, 8), f"remote key {key}")

    def actionTvVolume(self, action, dev):
        what = action.props.get("volume", "up")
        if what in ("up", "down"):
            self._char_write(dev, "VOLUME_SELECTOR", 0 if what == "up" else 1, f"volume {what}")
        else:
            self._char_write(dev, "MUTE", what == "mute", what)

    def actionHumidifierMode(self, action, dev):
        mode = action.props.get("mode", "auto")
        self._char_write(dev, "TARGET_HUMIDIFIER_DEHUMIDIFIER_STATE", sm.HUMIDIFIER_MODES.get(mode, 0), f"mode {mode}")

    def actionHumidityTarget(self, action, dev):
        which = action.props.get("which", "dehumidify")
        try:
            level = float(self.substitute(str(action.props.get("level", "50"))))
        except ValueError:
            self.logger.error(f'"{dev.name}": the humidity target must be a number')
            return
        name = "RELATIVE_HUMIDITY_DEHUMIDIFIER_THRESHOLD" if which == "dehumidify" else \
            "RELATIVE_HUMIDITY_HUMIDIFIER_THRESHOLD"
        self._char_write(dev, name, level, f"{which} target to {level:.0f}%")

    def actionValveDuration(self, action, dev):
        try:
            seconds = int(float(self.substitute(str(action.props.get("seconds", "300")))))
        except ValueError:
            self.logger.error(f'"{dev.name}": the duration must be a number of seconds')
            return
        self._char_write(dev, "SET_DURATION", seconds, f"duration to {seconds}s")

    def actionRefresh(self, action, dev):
        self._refresh_request(dev)

    # ----- dynamic lists for action dialogs ---------------------------------

    def charListGenerator(self, filter="", valuesDict=None, typeId="", targetId=0):
        dev = self._get_dev(int(targetId)) if targetId else None
        if dev is None:
            return [("", "Save the action with a device selected first")]
        try:
            chars = json.loads(dev.pluginProps.get("hkChars", "[]"))
        except ValueError:
            return []
        out = []
        for e in sm.CharIndex(chars).writable():
            detail = e.get("f", "")
            if e.get("vv"):
                detail += f" {e['vv']}"
            elif e.get("min") is not None and e.get("max") is not None:
                detail += f" {e['min']}–{e['max']}"
            source = "" if e.get("s") == "p" else f"{sm.title(e['s'])}: "
            out.append((f"{e['a']}.{e['i']}", f"{source}{sm.title(e['n'])} ({detail})"))
        return out or [("", "This device has no writable characteristics")]

    def tvInputGenerator(self, filter="", valuesDict=None, typeId="", targetId=0):
        dev = self._get_dev(int(targetId)) if targetId else None
        if dev is None:
            return [("", "Save the action with a device selected first")]
        try:
            inputs = json.loads(dev.pluginProps.get("hkInputs", "[]"))
        except ValueError:
            inputs = []
        return [(str(i["id"]), i["name"]) for i in inputs] or [("", "This TV reported no inputs")]

    # ======================================================================
    # Triggers
    # ======================================================================

    def triggerStartProcessing(self, trigger):
        self.triggers[trigger.id] = trigger

    def triggerStopProcessing(self, trigger):
        self.triggers.pop(trigger.id, None)

    def _fire_connection_triggers(self, alias, online):
        dev_id = self.pairing_devs.get(alias)
        for trigger in list(self.triggers.values()):
            if trigger.pluginTypeId != "connectionChanged":
                continue
            p = trigger.pluginProps
            if p.get("deviceId") not in ("", None) and str(p.get("deviceId")) != str(dev_id):
                continue
            if p.get("change", "offline") in ("any", "online" if online else "offline"):
                indigo.trigger.execute(trigger)

    # ======================================================================
    # Menu items
    # ======================================================================

    def page_url(self):
        base = str(self.pluginPrefs.get("iwsUrl", "http://localhost:8176") or "http://localhost:8176").rstrip("/")
        return f"{base}/message/{self.pluginId}/ui/"

    def menuOpenPage(self, *args, **kwargs):
        url = self.page_url()
        self.logger.info(f"Pairing page: {url}")
        try:
            subprocess.Popen(["/usr/bin/open", url])
        except OSError as err:
            self.logger.error(f"Couldn't open a browser: {err}")

    def menuListDiscovered(self, *args, **kwargs):
        if self.engine is None:
            self.logger.error("The HomeKit controller isn't running")
            return
        try:
            found = self.engine.call(self.engine.discoveries, timeout=10)
        except Exception as err:
            self.logger.error(f"Discovery failed: {friendly_error(err)}")
            return
        if not found:
            self.logger.info("No HomeKit accessories are advertising on the network")
            return
        self.logger.info(f"{len(found)} HomeKit accessories on the network:")
        for d in sorted(found, key=lambda x: x["name"].lower()):
            if d["id"] in self.store:
                status = "paired with Indigo"
            else:
                status = "paired elsewhere" if d["paired"] else "ready to pair"
            self.logger.info(f"  {d['name']} ({d['categoryName'] or 'accessory'}, {d['transport']}): {status}")

    def menuRefreshAll(self, *args, **kwargs):
        if self.engine is None:
            return
        for alias in list(self.store):
            self.engine.submit(self.engine.refresh, alias)
        self.logger.info("Refreshing every HomeKit device")

    def menuReconnectAll(self, *args, **kwargs):
        if self.engine is None:
            self.logger.info("Starting the HomeKit controller")
            self._start_engine()
            return
        self.engine.submit(self.engine.reconnect_all)
        self.logger.info("Reconnecting to every HomeKit accessory")

    # ======================================================================
    # Pairing jobs (started from the page; run on the engine loop)
    # ======================================================================

    def start_job(self, kind, coro_fn, *args, on_done=None):
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "kind": kind, "state": "running", "error": "", "result": None, "started": time.time()}
        self.jobs[job_id] = job
        for old in [k for k, j in self.jobs.items() if time.time() - j["started"] > 3600]:
            self.jobs.pop(old, None)
        future = self.engine.submit(coro_fn, *args)

        def done(fut):
            err = fut.exception()
            if err:
                job["state"], job["error"] = "failed", friendly_error(err)
            else:
                job["result"] = fut.result()
                job["state"] = "done"
                if on_done:
                    self.events.put(("call", None, lambda: on_done(job["result"])))
        future.add_done_callback(done)
        return job

    def pair_accessory(self, device_id, code, name=""):
        if self.engine is None:
            raise EngineError(self.engine_error or "The HomeKit controller isn't running.")
        job = self.start_job("pair", self.engine.pair, device_id, code,
                             on_done=lambda result: self.events.put(("paired", result["alias"], result)))
        job["name"] = name
        return job

    def _on_paired(self, result):
        alias = result["alias"]
        desc = result.get("description", {})
        accessories = self.accessories.get(alias) or []
        with self.store_lock:
            self.store[alias] = {
                "pairingData": result["pairingData"], "name": sm.accessory_name(accessories[0]) if accessories else
                (desc.get("name") or alias.upper()),
                "model": desc.get("model", ""), "category": desc.get("category", 0),
                "categoryName": desc.get("categoryName", ""), "transport": desc.get("transport", ""),
                "pairedAt": _now(), "known": [], "accessories": accessories,
            }
            self._save_store()
        self.logger.info(f'Paired with "{self._pairing_name(alias)}"')
        if accessories:
            self._sync_devices(alias)
        if self.online.get(alias):
            self._update_pairing_device(alias)

    def unpair_accessory(self, alias, force=False, delete_devices=False):
        if alias not in self.store:
            raise EngineError("That accessory isn't paired with Indigo.")
        if self.engine is None:
            raise EngineError(self.engine_error or "The HomeKit controller isn't running.")
        return self.start_job("unpair", self.engine.unpair, alias, bool(force),
                              on_done=lambda result: self.events.put(("unpaired", alias, result, delete_devices)))

    def _on_unpaired(self, alias, result, delete_devices):
        name = self._pairing_name(alias)
        with self.store_lock:
            self.store.pop(alias, None)
            self._save_store()
        self.values.pop(alias, None)
        self.accessories.pop(alias, None)
        self.online.pop(alias, None)
        if result.get("accessoryReset"):
            self.logger.info(f'Unpaired "{name}". It can now be paired with Apple Home or Indigo again.')
        else:
            self.logger.warning(f'Removed "{name}" from Indigo, but the accessory couldn\'t be told '
                                f'({result.get("error")}). Reset it before pairing it again.')
        for dev in list(indigo.devices.iter("self")):
            if dev.pluginProps.get("hkPairing") != alias:
                continue
            if delete_devices:
                self.logger.info(f'Deleting "{dev.name}"')
                indigo.device.delete(dev)
            else:
                self._set_error(dev, "unpaired")

    def identify_unpaired(self, device_id):
        if self.engine is None:
            raise EngineError(self.engine_error or "The HomeKit controller isn't running.")
        return self.start_job("identify", self.engine.identify_unpaired, device_id)

    # ======================================================================
    # Web page (Indigo Web Server)
    # ======================================================================

    def web_page(self, action, dev=None, callerWaitingForResult=None):
        return web_ui.serve_page(self, action)

    def web_api(self, action, dev=None, callerWaitingForResult=None):
        return web_ui.serve_api(self, action)
