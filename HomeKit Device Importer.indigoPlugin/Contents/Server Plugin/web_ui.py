# -*- coding: utf-8 -*-
"""
Pairing page, served by the Indigo Web Server.

  GET  /message/<pluginId>/ui/            -> the page (web_ui.html)
  GET  /message/<pluginId>/api/?op=...    -> JSON reads
  POST /message/<pluginId>/api/?op=...    -> JSON writes (JSON request body)

Indigo authenticates every request before it reaches the plugin, so the page is
only reachable by someone already logged in to this Indigo server. Pairing keys
never leave the plugin: nothing below returns pairingData.
"""

import json
import os
import threading

import indigo  # noqa

PAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_ui.html")


def _reply(status, content, content_type="application/json"):
    reply = indigo.Dict()
    reply["status"] = status
    reply["headers"] = indigo.Dict({"Content-Type": f"{content_type}; charset=utf-8", "Cache-Control": "no-store"})
    reply["content"] = content
    return reply


def _json(status, payload):
    return _reply(status, json.dumps(payload, default=str))


def _request_args(props):
    """Merge query args, form params and a JSON body, whichever IWS supplied."""
    args = {}
    query = props.get("url_query_args") or {}
    try:
        args.update({k: v for k, v in dict(query).items()})
    except (TypeError, ValueError):
        pass
    value = props.get("body_params")
    if value:
        try:
            args.update(dict(value))
        except (TypeError, ValueError):
            pass
    for key in ("request_body", "body"):
        raw = props.get(key)
        if not raw:
            continue
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        if isinstance(raw, str):
            try:
                body = json.loads(raw)
            except ValueError:
                continue
        else:
            try:
                body = dict(raw)
            except (TypeError, ValueError):
                continue
        if isinstance(body, dict):
            args.update(body)
        break
    return args


def serve_page(plugin, action):
    try:
        with open(PAGE_PATH, encoding="utf-8") as fh:
            return _reply(200, fh.read(), "text/html")
    except OSError as err:
        return _reply(500, f"Cannot load page: {err}", "text/plain")


def serve_api(plugin, action):
    props = action.props
    method = str(props.get("incoming_request_method", "GET")).upper()
    args = _request_args(props)
    op = str(args.get("op", ""))
    handler = ROUTES.get((method, op))
    if handler is None:
        plugin.logger.debug(f"Pairing page: unknown {method} {op!r}")
        return _json(404, {"error": f"unknown request {method} {op}"})
    try:
        return _json(200, handler(plugin, args))
    except UserError as err:
        return _json(400, {"error": str(err)})
    except Exception as err:
        name = type(err).__name__
        if name == "EngineError":
            return _json(400, {"error": str(err)})
        plugin.logger.exception(f"Pairing page {op} failed")
        return _json(500, {"error": f"{name}: {err}"})


class UserError(Exception):
    pass


def _on_worker(plugin, fn, timeout=15):
    """Run fn on the plugin's worker thread (where all device changes happen) and wait for it."""
    box = {}
    done = threading.Event()

    def run():
        try:
            box["result"] = fn()
        except Exception as err:
            box["error"] = err
        finally:
            done.set()
    plugin.events.put(("call", None, run))
    if not done.wait(timeout):
        raise UserError("Indigo is busy; try again in a moment.")
    if "error" in box:
        raise box["error"]
    return box.get("result")


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _job_public(job):
    out = {k: v for k, v in job.items() if k != "result"}
    result = job.get("result")
    if isinstance(result, dict):
        out["result"] = {k: v for k, v in result.items() if k != "pairingData"}
    return out


def _device_summary(dev):
    for attr in ("displayStateValUi",):
        val = getattr(dev, attr, None)
        if val not in (None, ""):
            return str(val)
    key = getattr(dev, "displayStateId", "") or ""
    if key:
        ui = dev.states.get(f"{key}.ui")
        if ui not in (None, ""):
            return str(ui)
        val = dev.states.get(key)
        if val is not None:
            return str(val)
    return ""


def op_status(plugin, args):
    import service_map as sm

    engine = plugin.engine
    discovered = []
    if engine is not None:
        try:
            discovered = engine.call(engine.discoveries, timeout=5)
            plugin.discovered = discovered
        except Exception:
            discovered = plugin.discovered
    with plugin.store_lock:
        store = {k: dict(v) for k, v in plugin.store.items()}

    devices_by_key = {}
    pairing_devices = {}
    for dev in indigo.devices.iter("self"):
        p = dev.pluginProps
        if p.get("hkKey"):
            devices_by_key[p.get("hkKey")] = dev
        elif p.get("hkPairing"):
            pairing_devices[p.get("hkPairing")] = dev

    pairings = []
    for alias, record in sorted(store.items(), key=lambda kv: str(kv[1].get("name", "")).lower()):
        info = plugin.infos.get(alias, {})
        accessories = plugin.accessories.get(alias) or record.get("accessories") or []
        services = []
        for plan in sm.plan_accessories(alias, accessories):
            dev = devices_by_key.get(plan["key"])
            services.append({
                "key": plan["key"], "name": plan["name"], "aid": plan["aid"],
                "kind": sm.SENSOR_LABELS.get(plan.get("sensor"), "") or sm.KIND_LABELS.get(plan["kind"], "Accessory"),
                "deviceId": dev.id if dev else None, "deviceName": dev.name if dev else "",
                "summary": _device_summary(dev) if dev else "", "error": (dev.errorState or "") if dev else "",
            })
        online = plugin.online.get(alias)
        pdev = pairing_devices.get(alias)
        pairings.append({
            "alias": alias, "id": alias.upper(), "name": record.get("customName") or record.get("name") or alias.upper(),
            "model": record.get("model", ""), "category": record.get("categoryName", ""),
            "transport": info.get("transport") or record.get("transport", ""),
            "address": info.get("address", ""), "online": online, "reason": plugin.reasons.get(alias, ""),
            "pairedAt": record.get("pairedAt", ""), "accessoryCount": len(accessories),
            "pairingDevice": {"id": pdev.id, "name": pdev.name} if pdev else None, "services": services,
        })

    for d in discovered:
        d["indigo"] = d.get("id") in store

    return {
        "engine": {"running": engine is not None and engine.running(), "error": plugin.engine_error},
        "settings": {"thread": bool(plugin.pluginPrefs.get("enableThread", True)),
                     "bluetooth": bool(plugin.pluginPrefs.get("enableBluetooth", False))},
        "discovered": sorted(discovered, key=lambda d: (d.get("name") or "").lower()),
        "pairings": pairings,
        "jobs": [_job_public(j) for j in plugin.jobs.values()],
    }


def op_job(plugin, args):
    job = plugin.jobs.get(str(args.get("id", "")))
    if job is None:
        raise UserError("That job has finished or doesn't exist.")
    return _job_public(job)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def op_pair(plugin, args):
    device_id = str(args.get("id", "")).strip()
    code = str(args.get("code", "")).strip()
    if not device_id:
        raise UserError("Pick an accessory to pair.")
    from hap_engine import normalise_code
    normalise_code(code)  # raises with a readable message
    job = plugin.pair_accessory(device_id, code, str(args.get("name", "")))
    return _job_public(job)


def op_unpair(plugin, args):
    alias = str(args.get("alias", "")).strip()
    force = str(args.get("force", "")).lower() in ("1", "true", "yes", "on")
    delete = str(args.get("deleteDevices", "")).lower() in ("1", "true", "yes", "on")
    return _job_public(plugin.unpair_accessory(alias, force=force, delete_devices=delete))


def op_identify(plugin, args):
    if args.get("alias"):
        alias = str(args["alias"])
        aid = int(args.get("aid", 1) or 1)
        if plugin.engine is None:
            raise UserError("The HomeKit controller isn't running.")
        return _job_public(plugin.start_job("identify", plugin.engine.identify, alias, aid))
    device_id = str(args.get("id", "")).strip()
    if not device_id:
        raise UserError("Pick an accessory to identify.")
    return _job_public(plugin.identify_unpaired(device_id))


def op_add_device(plugin, args):
    alias = str(args.get("alias", ""))
    key = str(args.get("key", ""))
    dev_id = _on_worker(plugin, lambda: plugin.add_service_device(alias, key))
    return {"deviceId": dev_id}


def op_add_all(plugin, args):
    import service_map as sm
    alias = str(args.get("alias", ""))

    def run():
        keys = {p["key"] for p in sm.plan_accessories(alias, plugin.accessories.get(alias) or [])}
        return plugin._sync_devices(alias, force_keys=keys)
    created = _on_worker(plugin, run, timeout=60)
    return {"created": len(created or [])}


def op_resync(plugin, args):
    alias = str(args.get("alias", ""))
    if plugin.engine is None:
        raise UserError("The HomeKit controller isn't running.")
    plugin.engine.call(plugin.engine.resync, alias, timeout=5)
    return {"ok": True}


def op_rename(plugin, args):
    alias = str(args.get("alias", ""))
    name = str(args.get("name", "")).strip()
    with plugin.store_lock:
        if alias not in plugin.store:
            raise UserError("That accessory isn't paired with Indigo.")
        if name:
            plugin.store[alias]["customName"] = name
        else:
            plugin.store[alias].pop("customName", None)
    plugin._save_store()
    return {"ok": True}


ROUTES = {
    ("GET", "status"): op_status,
    ("GET", "job"): op_job,
    ("POST", "pair"): op_pair,
    ("POST", "unpair"): op_unpair,
    ("POST", "identify"): op_identify,
    ("POST", "addDevice"): op_add_device,
    ("POST", "addAll"): op_add_all,
    ("POST", "resync"): op_resync,
    ("POST", "rename"): op_rename,
}
