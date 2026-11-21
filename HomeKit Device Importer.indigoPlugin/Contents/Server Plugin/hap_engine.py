# -*- coding: utf-8 -*-
"""
HomeKit controller engine.

Runs aiohomekit on its own asyncio loop in a background thread. Indigo's threads
talk to it only through submit()/call(), which hand coroutines to the loop with
run_coroutine_threadsafe, so nothing ever blocks the loop.

Everything the engine learns goes out through `emit(event)`, a thread-safe
callable the plugin supplies (it puts events on a queue that the plugin drains in
runConcurrentThread):

  ("structure", alias, accessories)          accessory database (re)read
  ("values", alias, {(aid, iid): v}, source) source: snapshot | event | poll
  ("availability", alias, online, reason)    only on change
  ("info", alias, info)                       address/transport/name details
"""

import asyncio
import contextlib
import re
import threading

OPAQUE_FORMATS = {"tlv8", "data"}
INFO_SERVICE = "0000003E-0000-1000-8000-0026BB765291"
PROTOCOL_SERVICE = "000000A2-0000-1000-8000-0026BB765291"
IDENTIFY_CHAR = "00000014-0000-1000-8000-0026BB765291"
STATIC_SERVICES = {INFO_SERVICE, PROTOCOL_SERVICE}

HAP_STATUS = {
    -70401: "the accessory refused the request (insufficient privileges)",
    -70402: "the accessory could not reach the device it controls",
    -70403: "the accessory is busy",
    -70404: "that characteristic is read-only",
    -70405: "that characteristic is write-only",
    -70406: "the accessory does not support notifications for that characteristic",
    -70407: "the accessory is out of resources",
    -70408: "the accessory timed out",
    -70409: "the accessory does not recognise that characteristic",
    -70410: "the accessory rejected the value",
    -70411: "the accessory requires authorisation for that",
}


class EngineError(Exception):
    pass


def normalise_code(code):
    digits = re.sub(r"\D", "", str(code or ""))
    if len(digits) != 8:
        raise EngineError("A HomeKit setup code has 8 digits, like 123-45-678.")
    return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"


def friendly_error(err):
    """Turn aiohomekit exceptions into something worth showing in the Event Log or on the page."""
    name = type(err).__name__
    text = str(err) or name
    table = {
        "AuthenticationError": "The accessory rejected the setup code. Check the code and try again.",
        "MalformedPinError": "A HomeKit setup code has 8 digits, like 123-45-678.",
        "UnavailableError": "The accessory is already paired with another controller (probably Apple Home). "
                            "Remove it from Apple Home or reset it, then try again.",
        "AlreadyPairedError": "The accessory is already paired with another controller (probably Apple Home). "
                              "Remove it from Apple Home or reset it, then try again.",
        "MaxPeersError": "The accessory has no room for another controller. Reset it, then pair again.",
        "MaxTriesError": "The accessory has locked pairing after too many wrong codes. Power-cycle it, then try again.",
        "BusyError": "The accessory is busy pairing with something else. Wait a minute and try again.",
        "BackoffError": "The accessory asked us to wait before trying again. Wait a minute and retry.",
        "AccessoryNotFoundError": "The accessory isn't advertising on the network right now. "
                                  "Check it's powered on and on the same network, then rescan.",
        "TransportNotSupportedError": "This accessory uses a connection type that is turned off in the plugin "
                                      "settings (Bluetooth or Thread).",
        "TimeoutError": "The accessory didn't answer in time.",
        "AccessoryDisconnectedError": "The connection to the accessory dropped.",
        "ConnectionError": "Couldn't connect to the accessory.",
    }
    if name in table:
        return table[name]
    if isinstance(err, asyncio.TimeoutError):
        return "The accessory didn't answer in time."
    return text


def transport_label(obj):
    cls = type(obj).__name__.lower()
    if "coap" in cls:
        return "Thread"
    if "ble" in cls:
        return "Bluetooth"
    return "Wi-Fi/Ethernet"


def describe(desc, paired=None, transport=""):
    if desc is None:
        return {}
    category = getattr(desc, "category", None)
    out = {
        "id": str(getattr(desc, "id", "")).lower(),
        "name": getattr(desc, "name", "") or "",
        "model": getattr(desc, "model", "") or "",
        "category": int(category) if category is not None else 0,
        "categoryName": category.name.replace("_", " ").title() if category is not None else "",
        "configNum": getattr(desc, "config_num", None),
        "address": getattr(desc, "address", "") or "",
        "port": getattr(desc, "port", None),
        "transport": transport,
    }
    if paired is not None:
        out["paired"] = bool(paired)
    return out


class Session:
    """One paired accessory (or bridge): keeps it connected, subscribed and polled."""

    def __init__(self, engine, alias, pairing):
        self.engine = engine
        self.alias = alias
        self.pairing = pairing
        self.need_structure = True
        self.online = None
        self.reason = ""
        self.wake = None
        self.task = None
        self.unsubs = []
        self.poll_list = []
        self.heartbeat = []
        self.readable = []
        self.accessories = []
        self.last_ok = None

    @property
    def transport(self):
        return transport_label(self.pairing)

    def start(self):
        self.wake = asyncio.Event()
        self.unsubs = [self.pairing.dispatcher_connect(self._on_event),
                       self.pairing.dispatcher_connect_config_changed(self._on_config)]
        self.task = asyncio.create_task(self._run(), name=f"homekit-{self.alias}")

    async def stop(self, close=True):
        for unsub in self.unsubs:
            with contextlib.suppress(Exception):
                unsub()
        self.unsubs = []
        if self.task:
            self.task.cancel()
            with contextlib.suppress(BaseException):
                await self.task
            self.task = None
        if close:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.pairing.close(), 5)

    def poke(self, structure=False):
        if structure:
            self.need_structure = True
        if self.wake:
            self.wake.set()

    def interval(self):
        base = max(10, int(self.engine.poll_seconds))
        try:
            own = int(self.pairing.poll_interval.total_seconds())
        except Exception:
            own = base
        return base if self.transport == "Wi-Fi/Ethernet" else max(base, own)

    async def _sleep(self, seconds):
        self.wake.clear()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.wake.wait(), seconds)

    async def _run(self):
        backoff = 5
        while True:
            try:
                if self.need_structure:
                    accessories = await asyncio.wait_for(self.pairing.list_accessories_and_characteristics(), 45)
                    self._apply_structure(accessories)
                    self.need_structure = False
                    events = self._event_list()
                    if events:
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(self.pairing.subscribe(events), 30)
                    self._set_online(True)
                    backoff = 5
                await self._sleep(self.interval())
                if self.need_structure:
                    continue
                targets = self.poll_list or self.heartbeat
                if targets:
                    result = await asyncio.wait_for(self.pairing.get_characteristics(targets), 30)
                    changes = {k: v["value"] for k, v in result.items() if isinstance(v, dict) and "value" in v}
                    failed = [v for v in result.values() if isinstance(v, dict) and v.get("status", 0) not in (0, None)
                              and "value" not in v]
                    if failed and len(failed) == len(targets):
                        raise EngineError(HAP_STATUS.get(failed[0].get("status"), f"status {failed[0].get('status')}"))
                    if changes and self.poll_list:
                        self.engine.emit(("values", self.alias, changes, "poll"))
                self._set_online(True)
                backoff = 5
            except asyncio.CancelledError:
                raise
            except Exception as err:  # connection problems: mark offline, back off, re-read everything
                self._set_online(False, friendly_error(err))
                self.engine.log_debug(f"{self.alias}: {type(err).__name__}: {err}")
                self.need_structure = True
                await self._sleep(backoff)
                backoff = min(backoff * 2, 300)

    def _apply_structure(self, accessories):
        self.accessories = accessories
        values, polls, readable, heartbeat = {}, [], [], []
        for acc in accessories:
            aid = acc.get("aid")
            for svc in acc.get("services", []):
                stype = str(svc.get("type", "")).upper()
                for c in svc.get("characteristics", []):
                    if c.get("format") in OPAQUE_FORMATS:
                        continue
                    perms = set(c.get("perms") or [])
                    key = (aid, c.get("iid"))
                    if "pr" in perms:
                        readable.append(key)
                        if not heartbeat:
                            heartbeat = [key]
                        if c.get("value") is not None:
                            values[key] = c.get("value")
                        if "ev" not in perms and stype not in STATIC_SERVICES:
                            polls.append(key)
        self.poll_list, self.readable, self.heartbeat = polls, readable, heartbeat
        self.engine.emit(("structure", self.alias, accessories))
        self.engine.emit(("info", self.alias, self.info()))
        self.engine.emit(("values", self.alias, values, "snapshot"))

    def _event_list(self):
        out = []
        for acc in self.accessories:
            for svc in acc.get("services", []):
                for c in svc.get("characteristics", []):
                    if "ev" in (c.get("perms") or []) and c.get("format") not in OPAQUE_FORMATS:
                        out.append((acc.get("aid"), c.get("iid")))
        return out

    def _on_event(self, data):
        changes = {}
        for key, val in (data or {}).items():
            if isinstance(val, dict) and "value" in val:
                changes[key] = val["value"]
        if changes:
            self.engine.emit(("values", self.alias, changes, "event"))

    def _on_config(self, _config_num):
        # aiohomekit has already re-read the database; read ours again so new services appear
        self.poke(structure=True)

    def _set_online(self, online, reason=""):
        if online:
            import time
            self.last_ok = time.time()
        if online != self.online:
            self.online = online
            self.reason = reason
            self.engine.emit(("availability", self.alias, online, reason))
            if online:
                self.engine.emit(("info", self.alias, self.info()))

    def info(self):
        out = describe(getattr(self.pairing, "description", None), True, self.transport)
        data = getattr(self.pairing, "pairing_data", {}) or {}
        if not out.get("address"):
            out["address"] = data.get("AccessoryIP") or data.get("AccessoryAddress") or ""
            out["port"] = data.get("AccessoryPort")
        out["transport"] = self.transport
        out["online"] = bool(self.online)
        out["reason"] = self.reason
        return out


class HapEngine:
    def __init__(self, logger, emit, interfaces=None, enable_ble=False, enable_thread=True, poll_seconds=60):
        self.logger = logger
        self.emit = emit
        self.interfaces = [i.strip() for i in (interfaces or []) if i and i.strip()]
        self.enable_ble = enable_ble
        self.enable_thread = enable_thread
        self.poll_seconds = poll_seconds
        self.debug = False
        self.loop = None
        self.thread = None
        self.controller = None
        self.azc = None
        self.browser = None
        self.sessions = {}
        self._ready = threading.Event()
        self._start_error = None

    # ----- lifecycle ---------------------------------------------------------

    def log_debug(self, msg):
        if self.debug:
            self.logger.debug(msg)

    def start(self, timeout=30):
        self.thread = threading.Thread(target=self._thread_main, name="HomeKitEngine", daemon=True)
        self.thread.start()
        if not self._ready.wait(timeout):
            raise EngineError("HomeKit engine did not start in time")
        if self._start_error:
            raise EngineError(f"HomeKit engine failed to start: {self._start_error}")

    def stop(self, timeout=15):
        if not self.loop or not self.loop.is_running():
            return
        fut = asyncio.run_coroutine_threadsafe(self._teardown(), self.loop)
        with contextlib.suppress(Exception):
            fut.result(timeout)
        self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread:
            self.thread.join(timeout=5)

    def running(self):
        return bool(self.loop and self.loop.is_running() and self.controller)

    def _thread_main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.set_exception_handler(self._loop_exception)
        try:
            self.loop.run_until_complete(self._startup())
        except Exception as err:
            self._start_error = err
            self._ready.set()
            return
        self._ready.set()
        try:
            self.loop.run_forever()
        finally:
            with contextlib.suppress(Exception):
                self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()

    def _loop_exception(self, loop, context):
        msg = context.get("message", "")
        exc = context.get("exception")
        self.log_debug(f"Async loop: {msg} {exc!r}" if exc else f"Async loop: {msg}")

    async def _startup(self):
        from zeroconf import InterfaceChoice, IPVersion
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf
        import aiohomekit.controller.controller as controller_module
        from aiohomekit import Controller
        from aiohomekit.zeroconf import ZeroconfServiceListener

        if not self.enable_ble:
            controller_module.BLE_TRANSPORT_SUPPORTED = False
        if not self.enable_thread:
            controller_module.COAP_TRANSPORT_SUPPORTED = False

        if self.interfaces:
            ip_version = IPVersion.All if any(":" in i for i in self.interfaces) else IPVersion.V4Only
            self.azc = AsyncZeroconf(interfaces=self.interfaces, ip_version=ip_version)
        else:
            ip_version = IPVersion.All if self.enable_thread else IPVersion.V4Only
            self.azc = AsyncZeroconf(interfaces=InterfaceChoice.All, ip_version=ip_version)
        types = ["_hap._tcp.local."] + (["_hap._udp.local."] if self.enable_thread else [])
        self.browser = AsyncServiceBrowser(self.azc.zeroconf, types, listener=ZeroconfServiceListener())
        await asyncio.sleep(0.2)  # the browser registers itself on the next loop pass; aiohomekit looks for it
        self.controller = Controller(async_zeroconf_instance=self.azc)
        await self.controller.async_start()

    async def _teardown(self):
        for session in list(self.sessions.values()):
            with contextlib.suppress(Exception):
                await session.stop()
        self.sessions.clear()
        if self.controller:
            with contextlib.suppress(Exception):
                await self.controller.async_stop()
        if self.browser:
            with contextlib.suppress(Exception):
                await self.browser.async_cancel()
        if self.azc:
            with contextlib.suppress(Exception):
                await self.azc.async_close()

    # ----- calling in from Indigo threads ------------------------------------

    def submit(self, fn, *args):
        """Schedule coroutine function fn(*args) on the loop; returns a concurrent.futures.Future."""
        if not self.running():
            raise EngineError("The HomeKit engine isn't running.")
        return asyncio.run_coroutine_threadsafe(fn(*args), self.loop)

    def call(self, fn, *args, timeout=30):
        return self.submit(fn, *args).result(timeout)

    # ----- coroutines (run on the loop) --------------------------------------

    async def discoveries(self):
        out = []
        async for d in self.controller.async_discover():
            out.append(describe(d.description, d.paired, transport_label(d)))
        return out

    async def pair(self, device_id, code):
        pin = normalise_code(code)
        device_id = str(device_id).strip().lower()
        if device_id in self.sessions:
            raise EngineError("Indigo is already paired with that accessory.")
        discovery = await self.controller.async_find(device_id, timeout=15)
        if discovery.paired:
            raise EngineError("The accessory is already paired with another controller (probably Apple Home). "
                              "Remove it from Apple Home or reset it, then try again.")
        alias = discovery.description.id.lower()
        info = describe(discovery.description, False, transport_label(discovery))
        finish = await asyncio.wait_for(discovery.async_start_pairing(alias), 30)
        pairing = await asyncio.wait_for(finish(pin), 60)
        data = dict(pairing.pairing_data)
        self._start_session(alias, pairing)
        return {"alias": alias, "pairingData": data, "description": info}

    async def load(self, alias, pairing_data):
        if alias in self.sessions:
            return True
        pairing = self.controller.load_pairing(alias, dict(pairing_data))
        if pairing is None:
            raise EngineError("This pairing uses a connection type that is turned off in the plugin settings.")
        self._start_session(alias, pairing)
        return True

    def _start_session(self, alias, pairing):
        session = Session(self, alias, pairing)
        self.sessions[alias] = session
        session.start()

    async def unpair(self, alias, force=False):
        session = self.sessions.get(alias)
        pairing = session.pairing if session else self.controller.aliases.get(alias)
        if session:
            await session.stop(close=False)
        error = None
        if pairing is not None:
            try:
                await asyncio.wait_for(pairing.remove_pairing(pairing.pairing_data["iOSPairingId"]), 20)
            except Exception as err:
                error = err
            if error is not None and not force:
                if session:
                    session.start()
                raise EngineError(f"Couldn't remove the pairing from the accessory: {friendly_error(error)}")
            self._forget(alias, pairing)
        self.sessions.pop(alias, None)
        return {"accessoryReset": error is None, "error": friendly_error(error) if error else ""}

    def _forget(self, alias, pairing):
        ids = {str(pairing.id), str(pairing.id).lower()}
        holders = [self.controller] + list(getattr(self.controller, "transports", {}).values())
        for holder in holders:
            getattr(holder, "aliases", {}).pop(alias, None)
            for pid in ids:
                getattr(holder, "pairings", {}).pop(pid, None)
        task = asyncio.ensure_future(self._shutdown_quietly(pairing))
        task.add_done_callback(lambda _t: None)

    @staticmethod
    async def _shutdown_quietly(pairing):
        with contextlib.suppress(Exception):
            if hasattr(pairing, "shutdown"):
                await asyncio.wait_for(pairing.shutdown(), 5)
            else:
                await asyncio.wait_for(pairing.close(), 5)

    def _session(self, alias):
        session = self.sessions.get(alias)
        if session is None:
            raise EngineError("That accessory isn't paired with Indigo.")
        return session

    async def put(self, alias, writes):
        session = self._session(alias)
        try:
            result = await asyncio.wait_for(session.pairing.put_characteristics(writes), 20)
        except (AttributeError, KeyError):
            session.poke(structure=True)
            raise EngineError("The accessory no longer has that characteristic. Its device list is being re-read.")
        problems = {k: v for k, v in (result or {}).items() if isinstance(v, dict) and v.get("status", 0) not in (0, None)}
        if problems:
            status = next(iter(problems.values())).get("status")
            raise EngineError(HAP_STATUS.get(status, f"the accessory returned status {status}"))
        return True

    async def refresh(self, alias):
        session = self._session(alias)
        if not session.readable:
            session.poke(structure=True)
            return {}
        result = await asyncio.wait_for(session.pairing.get_characteristics(session.readable), 30)
        changes = {k: v["value"] for k, v in result.items() if isinstance(v, dict) and "value" in v}
        self.emit(("values", alias, changes, "poll"))
        return changes

    async def resync(self, alias):
        self._session(alias).poke(structure=True)
        return True

    async def reconnect_all(self):
        for session in self.sessions.values():
            session.poke(structure=True)
        return len(self.sessions)

    async def identify(self, alias, aid):
        session = self._session(alias)
        for acc in session.accessories:
            if acc.get("aid") != aid:
                continue
            for svc in acc.get("services", []):
                for c in svc.get("characteristics", []):
                    if str(c.get("type", "")).upper() == IDENTIFY_CHAR:
                        await self.put(alias, [(aid, c.get("iid"), True)])
                        return True
        raise EngineError("That accessory doesn't support Identify.")

    async def identify_unpaired(self, device_id):
        discovery = await self.controller.async_find(str(device_id).lower(), timeout=10)
        await asyncio.wait_for(discovery.async_identify(), 15)
        return True

    async def session_info(self):
        return {alias: s.info() for alias, s in self.sessions.items()}
