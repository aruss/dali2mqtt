"""DALI bus configuration helpers (a small "cockpit" backend).

All operations share the running bridge's python-dali driver. The driver
serialises access through its ``transaction_lock`` (and repeats config commands
twice as required by DALI), so config actions and the normal bridge traffic can
safely interleave. Long, disruptive operations (commissioning) run through
``driver.run_sequence`` which holds the transaction lock for their whole
duration.
"""
import asyncio
import contextlib
import logging

import dali.address as address
import dali.gear.general as gear
from dali.command import YesNoResponse
from dali.exceptions import DALIError
from dali.sequences import Commissioning
from dali.sequences import sleep as seq_sleep

from .bus_monitor import get_monitor

try:
    import dali.gear.colour as _colour
except Exception:  # noqa: BLE001
    _colour = None
try:
    import dali.gear.emergency as _emergency
except Exception:  # noqa: BLE001
    _emergency = None

try:
    from dali.memory import diagnostics as _mem_diag
    from dali.memory import energy as _mem_energy
    from dali.memory import info as _mem_info
    from dali.memory import maintenance as _mem_maint
    from dali.memory import oem as _mem_oem
    from dali.memory.location import MemoryType as _MemoryType
    from dali.memory.location import MemoryValue as _MemoryValue

    _MEM_RW_TYPES = {
        _MemoryType.RAM_RW,
        _MemoryType.NVM_RW,
        _MemoryType.NVM_RW_L,
        _MemoryType.NVM_RW_P,
    }

    # Typed memory-bank groups (spec-accurate, from python-dali) — the data
    # behind a Cockpit-style typed "device page".
    _MEMORY_GROUPS = [
        ("Device info", _mem_info),
        ("OEM / luminaire", _mem_oem),
        ("Energy", _mem_energy),
        ("Diagnostics", _mem_diag),
        ("Maintenance", _mem_maint),
    ]
    _HAVE_MEMORY = True
except Exception:  # noqa: BLE001
    _MEMORY_GROUPS = []
    _MemoryValue = None
    _HAVE_MEMORY = False

try:
    import dali.device.general as device
    import dali.device.sequences as devseq
    from dali.device import pushbutton as pb
    from dali.device.general import EventScheme
    from dali.address import InstanceNumber

    _HAVE_DEVICE = True
except ImportError:  # python-dali < 0.11
    device = None
    devseq = None
    pb = None
    EventScheme = None
    InstanceNumber = None
    _HAVE_DEVICE = False

# Push-button instance event names (Part 301 Table 3), low bit first.
PB_EVENTS = [
    "button_released",
    "button_pressed",
    "short_press",
    "double_press",
    "long_press_start",
    "long_press_repeat",
    "long_press_stop",
    "button_stuck_free",
]

logger = logging.getLogger(__name__)

BROADCAST = address.Broadcast()


def _val(resp):
    """Extract an integer value from a query response, or None."""
    try:
        v = resp.value
    except Exception:
        return None
    if v is None:
        return None
    # Some responses expose the raw frame; prefer its integer form.
    if hasattr(v, "as_integer"):
        try:
            return int(v.as_integer)
        except Exception:
            return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class DaliConfig:
    def __init__(self, driver, busy=None):
        self.driver = driver
        # Share the guard across startup discovery and the configuration UI.
        if not hasattr(driver, "_config_lock"):
            driver._config_lock = asyncio.Lock()
        self._lock = driver._config_lock
        # Optional asyncio.Event the bridge watches to pause its bus traffic
        # while a config operation is running (prevents a re-read storm).
        self._busy = busy
        self.monitor = get_monitor(driver, busy=busy)

    async def _ready(self):
        await self.driver.connected.wait()

    # ------------------------------------------------------------- bus monitor
    def monitor_events(self, since=0):
        """Return legacy decoded events from the shared continuous capture."""
        return self.monitor.legacy_events(since)

    def recent_switch_addresses(self, limit=5):
        """Most-recent short addresses seen sending switch commands (for 'learn')."""
        seen = []
        for e in reversed(self.monitor.legacy_events()):
            if e.get("type") in ("RecallMaxLevel", "Off") and e.get("address") is not None:
                a = e["address"]
                if a not in seen:
                    seen.append(a)
            if len(seen) >= limit:
                break
        return seen

    @contextlib.asynccontextmanager
    async def _guard(self, operation="configuration operation", **parameters):
        """Own the bus for one operation: pause the bridge, serialise access."""
        async with self._lock:
            if self._busy is not None:
                self._busy.set()
            self.monitor.start_operation(operation, **parameters)
            outcome = {}
            try:
                yield outcome
            except BaseException as err:
                self.monitor.finish_operation(f"{type(err).__name__}: {err}")
                raise
            else:
                self.monitor.finish_operation(outcome.get("error"))
            finally:
                if self._busy is not None:
                    self._busy.clear()

    # ------------------------------------------------------------------ scan
    async def scan_gear(self, addresses=range(64)):
        """Scan control gear and return a list of dicts describing each one."""
        await self._ready()
        result = []
        async with self._guard("scan_gear"):
            for a in addresses:
                short = address.Short(a)
                try:
                    present = await self.driver.send(gear.QueryControlGearPresent(short))
                except DALIError:
                    continue
                if not (isinstance(present, YesNoResponse) and present.value):
                    continue
                info = {"address": a}
                info["level"] = _val(await self._q(gear.QueryActualLevel(short)))
                info["min_level"] = _val(await self._q(gear.QueryMinLevel(short)))
                info["max_level"] = _val(await self._q(gear.QueryMaxLevel(short)))
                info["physical_min"] = _val(
                    await self._q(gear.QueryPhysicalMinimum(short))
                )
                info["power_on_level"] = _val(
                    await self._q(gear.QueryPowerOnLevel(short))
                )
                info["system_failure_level"] = _val(
                    await self._q(gear.QuerySystemFailureLevel(short))
                )
                info["device_type"] = _val(await self._q(gear.QueryDeviceType(short)))
                info["groups"] = await self._read_groups(short)
                result.append(info)
        logger.info("Config scan found %d control gear", len(result))
        return result

    def memory_supported(self):
        return _HAVE_MEMORY

    @staticmethod
    def _mem_value_classes(module):
        out = []
        for name, obj in vars(module).items():
            if (
                isinstance(obj, type)
                and issubclass(obj, _MemoryValue)
                and getattr(obj, "locations", None)
                and obj.__module__ == module.__name__
                and not name.endswith("_legacy")
            ):
                out.append((name, obj))
        return out

    async def read_typed_memory(self, addr):
        """Read all standard typed memory-bank values (spec-accurate).

        Produces a Cockpit-style typed device page: grouped, decoded fields
        (device info, OEM/luminaire, energy, diagnostics, maintenance). Each
        bank is read once and decoded locally.
        """
        if not _HAVE_MEMORY:
            raise RuntimeError("python-dali memory module not available")
        await self._ready()
        short = address.Short(addr)
        groups = []
        async with self._guard("read_typed_memory"):
            for group_name, module in _MEMORY_GROUPS:
                # bank -> [(name, cls)]
                banks = {}
                for name, cls in self._mem_value_classes(module):
                    banks.setdefault(cls.bank.address, []).append((name, cls))
                fields = []
                for bank in sorted(banks):
                    row = await self._read_whole_bank(short, bank)
                    if not row:
                        continue
                    for name, cls in banks[bank]:
                        try:
                            val = cls.from_list(row)
                        except Exception:  # noqa: BLE001 - not implemented on device
                            continue
                        fields.append(
                            {
                                "name": name,
                                "bank": bank,
                                "value": self._mem_display(val),
                                "writeable": all(
                                    loc.type_ in _MEM_RW_TYPES for loc in cls.locations
                                ),
                            }
                        )
                if fields:
                    groups.append({"group": group_name, "fields": fields})
        return {"address": addr, "groups": groups}

    async def _read_whole_bank(self, short, bank):
        """Read locations 0..last of a memory bank; returns a list indexed by
        address, or None if the bank is not implemented."""

        def _seq():
            yield gear.DTR1(int(bank))
            yield gear.DTR0(0)
            r = yield gear.ReadMemoryLocation(short)
            last = self._resp_byte(r)
            if last is None:
                return None
            row = [last]
            for _ in range(int(last)):
                r = yield gear.ReadMemoryLocation(short)
                row.append(self._resp_byte(r))
            return row

        try:
            return await self.driver.run_sequence(_seq())
        except DALIError:
            return None

    @staticmethod
    def _mem_display(val):
        if isinstance(val, (bytes, bytearray)):
            return val.hex()
        try:
            import enum

            if isinstance(val, enum.Enum):
                return val.name
        except Exception:  # noqa: BLE001
            pass
        return str(val)

    # ----------------------------------------------------- DALI command console
    _CONSOLE_COMMANDS = {
        "off": ("Off", None),
        "recall_max": ("RecallMaxLevel", None),
        "recall_min": ("RecallMinLevel", None),
        "up": ("Up", None),
        "down": ("Down", None),
        "step_up": ("StepUp", None),
        "step_down": ("StepDown", None),
        "on_and_step_up": ("OnAndStepUp", None),
        "goto_last_active": ("GoToLastActiveLevel", None),
        "dapc": ("DAPC", "level"),          # arg 0-254
        "goto_scene": ("GoToScene", "scene"),  # arg 0-15
    }

    def _target(self, kind, addr):
        if kind == "broadcast":
            return address.Broadcast()
        if kind == "group":
            return address.Group(int(addr))
        return address.Short(int(addr))

    async def send_command(self, kind, addr, command, arg=None):
        """Send one curated DALI command to a short/group/broadcast target."""
        if command not in self._CONSOLE_COMMANDS:
            raise ValueError(f"unknown command {command}")
        await self._ready()
        cls_name, arg_kind = self._CONSOLE_COMMANDS[command]
        dest = self._target(kind, addr)
        cls = getattr(gear, cls_name)
        async with self._guard("send_command"):
            if command == "dapc":
                cmd = gear.DAPC(dest, int(arg))
            elif command == "goto_scene":
                cmd = gear.GoToScene(dest, int(arg))
            else:
                cmd = cls(dest)
            resp = await self.driver.send(cmd)
        return {"ok": True, "command": str(cmd), "response": str(resp) if resp is not None else None}

    # ----------------------------------------------------- colour control (DT8)
    @staticmethod
    def _colour_byte(response):
        raw = getattr(response, "raw_value", None)
        if raw is None or getattr(raw, "error", False):
            raise ValueError("Missing or invalid DT8 response")
        return raw.as_integer

    def _colour_value(self, short, selector, word=False):
        """Run inside a driver sequence so DTR selection/readback stays atomic."""
        yield gear.DTR0(int(selector))
        high = self._colour_byte((yield _colour.QueryColourValue(short)))
        if word:
            low = self._colour_byte((yield gear.QueryContentDTR0(short)))
            value = (high << 8) | low
            if value in (0, 65535):
                raise ValueError("Unavailable DT8 temperature")
            return value
        if high == 255:
            raise ValueError("Unavailable DT8 channel level")
        return high

    async def colour_capabilities(self, addr):
        """Discover DT8 features, including gear supporting multiple types."""
        if _colour is None:
            return {}
        await self._ready()
        short = address.Short(addr)

        def sequence():
            device_type = self._colour_byte((yield gear.QueryDeviceType(short)))
            types = [device_type]
            if device_type == 255:
                types = []
                for _ in range(64):
                    value = self._colour_byte((yield gear.QueryNextDeviceType(short)))
                    if value == 254:
                        break
                    if value in types:
                        raise ValueError("Repeated device type")
                    types.append(value)
                else:
                    raise ValueError("Unterminated device type list")
            if 8 not in types:
                return {}
            features = self._colour_byte((yield _colour.QueryColourTypeFeatures(short)))
            caps = {"rgb": ((features >> 5) & 7) >= 3}
            if features & 2:
                try:
                    coolest = yield from self._colour_value(short, 128, word=True)
                    warmest = yield from self._colour_value(short, 130, word=True)
                    if coolest > warmest:
                        raise ValueError("Reversed temperature limits")
                    caps.update(color_temp=True, min_mireds=coolest, max_mireds=warmest)
                except ValueError as err:
                    logger.warning("DT8 A%s temperature limits unavailable: %s", addr, err)
            return caps

        async with self._guard("colour_capabilities", address=addr):
            return await self.driver.run_sequence(sequence())

    async def colour_state(self, addr, capabilities):
        await self._ready()
        short = address.Short(addr)

        def sequence():
            yield gear.QueryActualLevel(short)  # Refresh the gear's reported colour.
            status = self._colour_byte((yield _colour.QueryColourStatus(short)))
            if status & 32 and capabilities.get("color_temp"):
                value = yield from self._colour_value(short, 2, word=True)
                return {"color_mode": "color_temp", "color_temp": value}
            if status & 128 and capabilities.get("rgb"):
                channels = []
                for selector in (9, 10, 11):
                    channels.append((yield from self._colour_value(short, selector)))
                return {"color_mode": "rgb", "rgb": [round(v * 255 / 254) for v in channels]}
            return {}

        async with self._guard("colour_state", address=addr):
            return await self.driver.run_sequence(sequence())

    async def read_colour(self, addr):
        if _colour is None:
            raise RuntimeError("colour module unavailable")
        await self._ready()
        short = address.Short(addr)
        async with self._guard("read_colour"):
            status = await self._q(_colour.QueryColourStatus(short))
            feat = await self._q(_colour.QueryColourTypeFeatures(short))
        return {
            "address": addr,
            "colour_status": self._mem_display(status.value) if status and getattr(status, "raw_value", None) is not None else None,
            "colour_features": _val(feat),
        }

    async def set_colour_temp(self, addr, mireds):
        """Set tunable-white colour temperature (mireds) on a DT8 gear."""
        if _colour is None:
            raise RuntimeError("colour module unavailable")
        await self._ready()
        short = address.Short(addr)
        mireds = int(mireds)

        def _seq():
            yield gear.DTR0(mireds & 0xFF)
            yield gear.DTR1((mireds >> 8) & 0xFF)
            yield _colour.SetTemporaryColourTemperature(short)
            yield _colour.Activate(short)

        async with self._guard("set_colour_temp"):
            await self.driver.run_sequence(_seq())
        return {"ok": True, "mireds": mireds}

    async def set_colour_rgb(self, addr, r, g, b):
        """Set RGB dim levels (0-254 each) on a DT8 RGBWAF gear."""
        if _colour is None:
            raise RuntimeError("colour module unavailable")
        await self._ready()
        short = address.Short(addr)

        def _seq():
            yield gear.DTR0(int(r) & 0xFF)
            yield gear.DTR1(int(g) & 0xFF)
            yield gear.DTR2(int(b) & 0xFF)
            yield _colour.SetTemporaryRGBDimLevel(short)
            yield _colour.Activate(short)

        async with self._guard("set_colour_rgb"):
            await self.driver.run_sequence(_seq())
        return {"ok": True, "rgb": [int(r), int(g), int(b)]}

    # ------------------------------------------------- emergency lighting (DT1)
    async def read_emergency(self, addr):
        if _emergency is None:
            raise RuntimeError("emergency module unavailable")
        await self._ready()
        short = address.Short(addr)
        async with self._guard("read_emergency"):
            out = {"address": addr}
            status = await self._q(_emergency.QueryEmergencyStatus(short))
            out["status"] = self._mem_display(status.value) if status and getattr(status, "raw_value", None) is not None else None
            mode = await self._q(_emergency.QueryEmergencyMode(short))
            out["mode"] = self._mem_display(mode.value) if mode and getattr(mode, "raw_value", None) is not None else None
            out["battery_charge"] = _val(await self._q(_emergency.QueryBatteryCharge(short)))
            out["rated_duration_min"] = self._maybe_mul(_val(await self._q(_emergency.QueryRatedDuration(short))), 2)
            out["duration_test_result_min"] = self._maybe_mul(_val(await self._q(_emergency.QueryDurationTestResult(short))), 2)
            out["lamp_emergency_time_h"] = _val(await self._q(_emergency.QueryLampEmergencyTime(short)))
        return out

    @staticmethod
    def _maybe_mul(v, factor):
        return v * factor if isinstance(v, int) else v

    async def emergency_test(self, addr, kind):
        if _emergency is None:
            raise RuntimeError("emergency module unavailable")
        await self._ready()
        short = address.Short(addr)
        cmd_map = {
            "function": _emergency.StartFunctionTest,
            "duration": _emergency.StartDurationTest,
            "stop": _emergency.StopTest,
        }
        if kind not in cmd_map:
            raise ValueError(f"unknown test {kind}")
        async with self._guard("emergency_test"):
            await self.driver.send(cmd_map[kind](short))
        return {"ok": True, "test": kind}

    # -------------------------------------------------- writable typed memory
    def _find_typed(self, name):
        for _grp, module in _MEMORY_GROUPS:
            for n, cls in self._mem_value_classes(module):
                if n == name:
                    return cls
        return None

    async def write_typed(self, addr, name, value):
        """Write a writable typed memory value (e.g. an OEM/luminaire field)."""
        if not _HAVE_MEMORY:
            raise RuntimeError("memory module unavailable")
        cls = self._find_typed(name)
        if cls is None:
            raise ValueError(f"unknown field {name}")
        if not all(loc.type_ in _MEM_RW_TYPES for loc in cls.locations):
            raise ValueError(f"{name} is read-only")
        await self._ready()
        short = address.Short(addr)
        # Coerce the value: try int, else pass through as string.
        try:
            coerced = int(value)
        except (TypeError, ValueError):
            coerced = value
        async with self._guard("write_typed"):
            await self.driver.run_sequence(cls.write(short, coerced))
        return {"ok": True, "field": name, "value": coerced}

    async def scan_lunatone(self, bank=3, count=32, addresses=range(64)):
        """Read a Lunatone config memory bank from every control gear.

        Lunatone DALI-1 input devices (switches) occupy a short address and
        store their button configuration (destination address, command, button
        function) in a proprietary memory bank (typically bank 3). This dumps
        that bank for each present gear so switches can be identified and their
        destination mapping inspected.
        """
        await self._ready()
        result = []
        async with self._guard("scan_lunatone"):
            for a in addresses:
                short = address.Short(a)
                present = await self._q(gear.QueryControlGearPresent(short))
                if not (isinstance(present, YesNoResponse) and present.value):
                    continue
                gtin = self._resp_byte(
                    await self._q(gear.QueryDeviceType(short))
                )

                def _seq(sh=short):
                    out = []
                    yield gear.DTR1(int(bank))
                    yield gear.DTR0(0)
                    for _ in range(int(count)):
                        r = yield gear.ReadMemoryLocation(sh)
                        out.append(self._resp_byte(r))
                    return out

                try:
                    data = await self.driver.run_sequence(_seq())
                except DALIError:
                    data = None
                result.append(
                    {"address": a, "bank": int(bank), "device_type": gtin, "data": data}
                )
        logger.info("Lunatone scan read bank %s from %d gear", bank, len(result))
        return result

    async def _q(self, command):
        try:
            return await self.driver.send(command)
        except DALIError as err:
            logger.debug("Query %s failed: %s", command, err)
            return None

    async def _read_groups(self, short):
        groups = []
        low = _val(await self._q(gear.QueryGroupsZeroToSeven(short))) or 0
        high = _val(await self._q(gear.QueryGroupsEightToFifteen(short))) or 0
        for i in range(8):
            if low & (1 << i):
                groups.append(i)
            if high & (1 << i):
                groups.append(i + 8)
        return groups

    # ----------------------------------------------------------- gear actions
    async def identify(self, addr, count=3, speed=0.5):
        """Blink a control gear so it can be located physically."""
        await self._ready()
        short = address.Short(addr)
        async with self._guard("identify"):
            original = _val(await self._q(gear.QueryActualLevel(short)))
            for _ in range(count):
                await self.driver.send(gear.RecallMaxLevel(short))
                await asyncio.sleep(speed)
                await self.driver.send(gear.RecallMinLevel(short))
                await asyncio.sleep(speed)
            # restore
            await self.driver.send(gear.DAPC(short, original or 0))
        return {"ok": True}

    async def set_level(self, addr, level):
        await self._ready()
        async with self._guard("set_level"):
            await self.driver.send(gear.DAPC(address.Short(addr), int(level)))
        return {"ok": True}

    async def _set_via_dtr(self, addr, value, command_cls):
        await self._ready()
        short = address.Short(addr)
        async with self._guard("set_via_dtr", command=command_cls.__name__, address=addr):
            await self.driver.send(gear.DTR0(int(value)))
            await self.driver.send(command_cls(short))
        return {"ok": True}

    async def set_min_level(self, addr, value):
        return await self._set_via_dtr(addr, value, gear.SetMinLevel)

    async def set_max_level(self, addr, value):
        return await self._set_via_dtr(addr, value, gear.SetMaxLevel)

    async def set_power_on_level(self, addr, value):
        return await self._set_via_dtr(addr, value, gear.SetPowerOnLevel)

    async def set_system_failure_level(self, addr, value):
        return await self._set_via_dtr(addr, value, gear.SetSystemFailureLevel)

    async def set_fade_time(self, addr, value):
        return await self._set_via_dtr(addr, value, gear.SetFadeTime)

    async def set_fade_rate(self, addr, value):
        return await self._set_via_dtr(addr, value, gear.SetFadeRate)

    async def add_to_group(self, addr, group):
        await self._ready()
        async with self._guard("add_to_group"):
            await self.driver.send(gear.AddToGroup(address.Short(addr), int(group)))
        return {"ok": True}

    async def remove_from_group(self, addr, group):
        await self._ready()
        async with self._guard("remove_from_group"):
            await self.driver.send(
                gear.RemoveFromGroup(address.Short(addr), int(group))
            )
        return {"ok": True}

    async def get_scenes(self, addr):
        await self._ready()
        short = address.Short(addr)
        scenes = {}
        async with self._guard("get_scenes"):
            for s in range(16):
                v = _val(await self._q(gear.QuerySceneLevel(short, s)))
                if v is not None and str(v).upper() != "MASK":
                    scenes[s] = v
        return scenes

    async def set_scene(self, addr, scene, level):
        await self._ready()
        short = address.Short(addr)
        async with self._guard("set_scene"):
            await self.driver.send(gear.DTR0(int(level)))
            await self.driver.send(gear.SetScene(short, int(scene)))
        return {"ok": True}

    async def clear_scene(self, addr, scene):
        await self._ready()
        async with self._guard("clear_scene"):
            await self.driver.send(
                gear.RemoveFromScene(address.Short(addr), int(scene))
            )
        return {"ok": True}

    async def change_address(self, old, new):
        """Change the short address of an existing control gear."""
        await self._ready()
        if not (0 <= int(new) <= 63):
            raise ValueError("new address out of range")
        async with self._guard("change_address", old=old, new=new) as outcome:
            # DTR0 = (new << 1) | 1, then SET SHORT ADDRESS (config, sent twice)
            await self.driver.send(gear.DTR0((int(new) << 1) | 1))
            await self.driver.send(gear.SetShortAddress(address.Short(int(old))))
            # Verify the gear now answers at the new short address.
            present = await self._q(
                gear.QueryControlGearPresent(address.Short(int(new)))
            )
            verified = bool(isinstance(present, YesNoResponse) and present.value)
            if not verified:
                outcome["error"] = "New gear address verification failed"
        return {"ok": True, "verified": verified}

    async def commission(self, readdress=False, progress_cb=None):
        """Assign short addresses to control gear (DALI commissioning).

        readdress=False : only address currently-unaddressed gear (safe).
        readdress=True  : clear and re-assign ALL short addresses (disruptive).
        """
        await self._ready()

        def _prog(p):
            logger.info("Commissioning: %s", getattr(p, "message", p))
            if progress_cb:
                progress_cb(p)

        async with self._guard("commission"):
            await self.driver.run_sequence(
                Commissioning(readdress=readdress), progress=_prog
            )
        logger.info("Commissioning finished (readdress=%s)", readdress)
        return {"ok": True}

    # ----------------------------------------------- control devices (DALI-2)
    # Input devices such as push-button panels and sensors (e.g. Lunatone DALI
    # Switch). Requires python-dali >= 0.11.
    def devices_supported(self):
        return _HAVE_DEVICE

    async def scan_devices(self, addresses=range(64)):
        """Scan DALI-2 control devices (buttons/sensors)."""
        if not _HAVE_DEVICE:
            return []
        await self._ready()
        result = []
        async with self._guard("scan_devices"):
            async def query_value(command, addr):
                # Send outside decoding: transport failures must fail the scan.
                response = await self.driver.send(command)
                raw = getattr(response, "raw_value", None)
                if raw is None:
                    return None
                value = None if raw.error else _val(response)
                if value is None or not 0 <= value <= 255:
                    logger.warning("Rejected invalid control-device reply D%d %s",
                                   addr, type(command).__name__)
                    return None
                return value

            for a in addresses:
                short = address.DeviceShort(a)
                status = await query_value(device.QueryDeviceStatus(short), a)
                if status is None:
                    continue
                info = {"address": a}
                info["status"] = status
                info["instances"] = await query_value(
                    device.QueryNumberOfInstances(short), a
                )
                info["operating_mode"] = await query_value(
                    device.QueryOperatingMode(short), a
                )
                result.append(info)
        logger.info("Config scan found %d control devices", len(result))
        return result

    async def identify_device(self, addr):
        if not _HAVE_DEVICE:
            raise RuntimeError("control device support requires python-dali 0.11")
        await self._ready()
        async with self._guard("identify_device"):
            await self.driver.send(device.IdentifyDevice(address.DeviceShort(addr)))
        return {"ok": True}

    async def read_device(self, addr):
        """Read controller capabilities/state and generic instance settings."""
        if not _HAVE_DEVICE:
            raise RuntimeError("control device support requires python-dali 0.11")
        await self._ready()
        short = self._device_short(addr)
        async with self._guard("read_device"):
            await self._require_device(short)
            n = await self._instance_count(short)
            op = _val(await self._q(device.QueryOperatingMode(short)))
            caps = _val(await self._q(device.QueryDeviceCapabilities(short)))
            controller = await self._controller_state(short, caps)
            instances = []
            for i in range(n):
                inst = {"instance": i}
                inst["type"] = _val(
                    await self._q(device.QueryInstanceType(short, InstanceNumber(i)))
                    if hasattr(device, "QueryInstanceType")
                    else None
                )
                inst["enabled"] = await self._enabled_state(short, InstanceNumber(i))
                inst["scheme"] = _val(await self._q(
                    device.QueryEventScheme(short, InstanceNumber(i))))
                instances.append(inst)
        return {"address": addr, "operating_mode": op, "instances": instances,
                "application_controller": controller}

    @staticmethod
    def _device_short(addr):
        if type(addr) is not int or not 0 <= addr <= 63:
            raise ValueError("device address must be an integer from 0 to 63")
        return address.DeviceShort(addr)

    async def _require_device(self, short):
        if _val(await self._q(device.QueryDeviceStatus(short))) is None:
            raise RuntimeError("device did not respond to status query")

    async def _instance_count(self, short):
        count = _val(await self._q(device.QueryNumberOfInstances(short)))
        if count is None or not 0 <= count <= 32:
            raise RuntimeError("device instance count unavailable or invalid")
        return count

    async def _require_instance(self, short, instance):
        await self._require_device(short)
        if instance >= await self._instance_count(short):
            raise ValueError("instance does not exist on this device")
        return InstanceNumber(instance)

    @staticmethod
    def _instance_number(instance):
        if type(instance) is not int or not 0 <= instance <= 31:
            raise ValueError("instance must be an integer from 0 to 31")
        return instance

    @staticmethod
    def _boolean(enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")

    async def _enabled_state(self, short, inst):
        result = _val(await self._q(device.QueryInstanceEnabled(short, inst)))
        # NO is silence on DALI. Check device presence after the query so a
        # disconnected device is never reported as a disabled instance.
        await self._require_device(short)
        if result not in (0, 1):
            raise RuntimeError("instance enabled state unavailable")
        return bool(result)

    async def _controller_state(self, short, caps):
        if caps is None:
            return {"present": None, "always_active": None, "enabled": None}
        present, always = bool(caps & 1), bool(caps & 4)
        enabled = False
        if present:
            value = _val(await self._q(device.QueryApplicationControlEnabled(short)))
            await self._require_device(short)
            enabled = bool(value) if value in (0, 1) else None
        return {"present": present, "always_active": always, "enabled": enabled}

    async def set_application_controller(self, addr, enabled):
        short = self._device_short(addr)
        self._boolean(enabled)
        await self._ready()
        async with self._guard("set_application_controller"):
            await self._require_device(short)
            caps = _val(await self._q(device.QueryDeviceCapabilities(short)))
            if caps is None or not caps & 1:
                raise ValueError("device has no confirmed application controller")
            if not enabled and caps & 4:
                raise ValueError("application controller is always active")
            cmd = device.EnableApplicationController if enabled else device.DisableApplicationController
            await self.driver.send(cmd(short))
            state = await self._controller_state(short, caps)
            if state["enabled"] is not enabled:
                raise RuntimeError("application controller readback mismatch")
        return {"ok": True, "application_controller": state}

    async def set_instance_enabled(self, addr, instance, enabled):
        short = self._device_short(addr)
        self._instance_number(instance)
        self._boolean(enabled)
        await self._ready()
        async with self._guard("set_instance_enabled"):
            inst = await self._require_instance(short, instance)
            cmd = device.EnableInstance if enabled else device.DisableInstance
            await self.driver.send(cmd(short, inst))
            actual = await self._enabled_state(short, inst)
            if actual is not enabled:
                raise RuntimeError("instance enabled readback mismatch")
        return {"ok": True, "enabled": actual}

    async def save_device(self, addr):
        short = self._device_short(addr)
        await self._ready()
        async with self._guard("save_device"):
            await self._require_device(short)
            await self.driver.send(device.SavePersistentVariables(short))
            await self._require_device(short)
        return {"ok": True, "persistence_verified": False}

    async def refresh_instance_map(self):
        """Rebuild this bus's shared decoder map using addressed queries only."""
        mapper = getattr(self.driver, "dev_inst_map", None)
        if mapper is None:
            return
        await self._ready()
        async with self._guard("refresh_instance_map"):
            mapper.clear()
            for addr in range(64):
                short = address.DeviceShort(addr)
                if _val(await self._q(device.QueryDeviceStatus(short))) is None:
                    continue
                count = _val(await self._q(device.QueryNumberOfInstances(short)))
                if count is None or not 0 <= count <= 32:
                    continue
                for i in range(count):
                    itype = _val(await self._q(device.QueryInstanceType(short, InstanceNumber(i))))
                    if itype is not None and 0 <= itype <= 31:
                        mapper.add_type(short_address=addr, instance_number=i, instance_type=itype)

    async def change_device_address(self, old, new):
        """Change the short address of a control device."""
        if not _HAVE_DEVICE:
            raise RuntimeError("control device support requires python-dali 0.11")
        await self._ready()
        short = self._device_short(old)
        new_short = self._device_short(new)
        try:
            async with self._guard("change_device_address"):
                await self._require_device(short)
                if old == new:
                    return {"ok": True}
                status = await self._q(device.QueryDeviceStatus(new_short))
                if getattr(status, "raw_value", None) is not None:
                    raise ValueError("new device address already occupied")
                def seq():
                    yield device.DTR0(new)
                    yield device.SetShortAddress(short)
                await self.driver.run_sequence(seq())
                await self._require_device(new_short)
        finally:
            await self.refresh_instance_map()
        return {"ok": True}

    async def commission_devices(self, readdress=False, dry_run=False,
                                 progress_cb=None):
        """Commission DALI-2 control devices (Part 103), like DALI Cockpit.

        Lunatone push-button / input modules are *control devices* (they show
        up as ``eA0`` .. ``eA63``).  Until they have a short device address they
        do not answer ``QueryDeviceStatus`` and cannot be read or configured --
        this is why a plain scan finds nothing.  Cockpit assigns those addresses
        with the randomise/compare/withdraw process (the same one used for
        control gear, but on the 24-bit device layer); afterwards each switch is
        addressable and its configuration can be read.

        readdress=False : only address devices that currently have none (safe;
                          existing device addresses are preserved).
        readdress=True  : clear and re-assign ALL device short addresses.
        dry_run=True    : run the search but never program an address -- use it
                          to *count* how many uncommissioned devices are on the
                          bus without changing anything.

        Returns {"found": n, "programmed": [addr, ...], "dry_run": bool}.
        """
        if not _HAVE_DEVICE:
            raise RuntimeError("control device support requires python-dali 0.11")
        await self._ready()

        found = {"n": 0}
        programmed = []
        failures = []

        def _prog(msg, **kw):
            logger.info("Device commissioning: %s", msg)
            if progress_cb:
                progress_cb({"message": msg, **kw})

        def _find_next(low, high):
            """Binary-search the random-address space for the next device."""
            yield device.SearchAddrH((high >> 16) & 0xFF)
            yield device.SearchAddrM((high >> 8) & 0xFF)
            yield device.SearchAddrL(high & 0xFF)
            r = yield device.Compare()
            present = bool(r) and r.value is True
            if low == high:
                if present:
                    # A framing error on Compare means two devices share this
                    # exact random address -> a clash we must re-randomise past.
                    raw = getattr(r, "raw_value", None)
                    return "clash" if getattr(raw, "error", False) else low
                return None
            if present:
                mid = (low + high) // 2
                res = yield from _find_next(low, mid)
                if res is not None:
                    return res
                return (yield from _find_next(mid + 1, high))
            return None

        def _seq():
            # Which short addresses are free to hand out?
            available = []
            for a in range(64):
                status = yield device.QueryDeviceStatus(address.DeviceShort(a))
                if readdress or getattr(status, "raw_value", None) is None:
                    available.append(a)
            yield device.Terminate()
            # 0x00 = all control devices react; 0x7F = only those without a
            # short address (Part 103 INITIALISE argument).
            yield device.Initialise(0x00 if readdress else 0x7F)
            if readdress and not dry_run:
                yield device.DTR0(0xFF)
                yield device.SetShortAddress(address.DeviceBroadcast())

            finished = False
            while not finished:
                yield device.Randomise()
                yield seq_sleep(0.1)
                low, high = 0, 0xFFFFFF
                while low is not None:
                    low = yield from _find_next(low, high)
                    if low == "clash":
                        _prog("Two devices picked the same address; retrying")
                        break
                    if low is None:
                        finished = True
                        break
                    found["n"] += 1
                    _prog(f"Device found at random address {low:#08x}")
                    if dry_run:
                        yield device.Withdraw()
                        continue
                    if available:
                        new = available.pop(0)
                        yield device.ProgramShortAddress(new)
                        v = yield device.VerifyShortAddress(new)
                        if v is not None and v.value is True:
                            programmed.append(new)
                            _prog(f"Programmed device short address {new}")
                        else:
                            _prog(f"Failed to verify device address {new}")
                            failures.append(f"Failed to verify device address {new}")
                        yield device.Withdraw()
                    else:
                        _prog("No free device addresses left")
                        failures.append("No free device addresses left")
                        finished = True
                        break
            yield device.Terminate()

        try:
            async with self._guard("commission_devices", readdress=readdress, dry_run=dry_run) as outcome:
                await self.driver.run_sequence(_seq())
                if failures:
                    outcome["error"] = "; ".join(failures)
        finally:
            await self.refresh_instance_map()
        logger.info(
            "Device commissioning done: found=%d programmed=%s dry_run=%s",
            found["n"], programmed, dry_run,
        )
        return {"found": found["n"], "programmed": programmed, "dry_run": dry_run}

    # ------------------------------------------------------- memory bank tool
    def _mem_addr_mod(self, addr, is_device):
        if is_device:
            if not _HAVE_DEVICE:
                raise RuntimeError("control device support requires python-dali 0.11")
            return address.DeviceShort(addr), device
        return address.Short(addr), gear

    @staticmethod
    def _resp_byte(r):
        if r is None:
            return None
        raw = getattr(r, "raw_value", None)
        if raw is None:
            return None
        try:
            return int(raw.as_integer)
        except Exception:  # noqa: BLE001
            return None

    async def read_memory(self, addr, bank, start=0, count=16, is_device=False):
        """Read `count` bytes of a memory bank from a control gear or device."""
        await self._ready()
        short, mod = self._mem_addr_mod(addr, is_device)
        count = max(1, min(int(count), 255))

        def seq():
            out = []
            yield mod.DTR1(int(bank))
            yield mod.DTR0(int(start))
            for _ in range(count):
                r = yield mod.ReadMemoryLocation(short)
                out.append(self._resp_byte(r))
            return out

        async with self._guard("read_memory"):
            data = await self.driver.run_sequence(seq())
        return {"address": addr, "bank": int(bank), "start": int(start), "data": data}

    async def write_memory(self, addr, bank, offset, value, is_device=False, unlock=True):
        """Write a single byte to a memory bank location (targeted, with unlock).

        Uses the addressed ENABLE WRITE MEMORY command so only this device
        accepts the following (broadcast) WRITE MEMORY LOCATION.
        """
        await self._ready()
        short, mod = self._mem_addr_mod(addr, is_device)
        value = int(value) & 0xFF
        offset = int(offset) & 0xFF

        def seq():
            yield mod.DTR1(int(bank))
            yield mod.EnableWriteMemory(short)
            if unlock:
                yield mod.DTR0(2)
                yield mod.WriteMemoryLocationNoReply(0x55)
            yield mod.DTR0(offset)
            r = yield mod.WriteMemoryLocation(value)
            written = self._resp_byte(r)
            if unlock:
                yield mod.DTR0(2)
                yield mod.WriteMemoryLocationNoReply(0xFF)
            return written

        async with self._guard("write_memory"):
            written = await self.driver.run_sequence(seq())
        logger.info(
            "Memory write %s bank %s offset %s = %s (readback %s)",
            addr, bank, offset, value, written,
        )
        return {"ok": True, "written": written, "expected": value}

    # -------------------------------------- DALI-2 push-button instance config
    # Part 301. Timers are raw bytes: short/double/repeat in 20 ms steps,
    # stuck in 1 s steps. Event filter selects which events the instance emits.
    _PB_TIMER_SET = None  # set lazily to avoid touching pb when unavailable
    _PB_TIMER_QUERY = None

    def _pb_maps(self):
        return (
            {
                "short": pb.SetShortTimer,
                "double": pb.SetDoubleTimer,
                "repeat": pb.SetRepeatTimer,
                "stuck": pb.SetStuckTimer,
            },
            {
                "short": pb.QueryShortTimer,
                "double": pb.QueryDoubleTimer,
                "repeat": pb.QueryRepeatTimer,
                "stuck": pb.QueryStuckTimer,
            },
        )

    async def read_pushbutton(self, addr, instance):
        """Read a push-button instance: type, timers, and event filter."""
        if not _HAVE_DEVICE:
            raise RuntimeError("control device support requires python-dali 0.11")
        await self._ready()
        short = address.DeviceShort(addr)
        inst = InstanceNumber(int(instance))
        _, qtimers = self._pb_maps()
        async with self._guard("read_pushbutton"):
            itype = _val(await self._q(device.QueryInstanceType(short, inst)))
            timers = {}
            for name, cmd in qtimers.items():
                timers[name] = _val(await self._q(cmd(short, inst)))
            try:
                flt = await self.driver.run_sequence(
                    devseq.QueryEventFilters(device=short, instance=inst, filter_type=pb)
                )
                mask = int(flt) if flt is not None else None
            except Exception as err:  # noqa: BLE001
                logger.debug("QueryEventFilters failed: %s", err)
                mask = None
        events = None
        if mask is not None:
            events = [PB_EVENTS[i] for i in range(8) if mask & (1 << i)]
        return {
            "address": addr,
            "instance": int(instance),
            "instance_type": itype,
            "timers": timers,
            "event_filter_mask": mask,
            "events": events,
        }

    async def set_pushbutton_timer(self, addr, instance, which, value):
        if not _HAVE_DEVICE:
            raise RuntimeError("control device support requires python-dali 0.11")
        await self._ready()
        setters, _ = self._pb_maps()
        if which not in setters:
            raise ValueError(f"unknown timer {which}")
        short = address.DeviceShort(addr)
        inst = InstanceNumber(int(instance))
        async with self._guard("set_pushbutton_timer"):
            await self.driver.send(device.DTR0(int(value) & 0xFF))
            await self.driver.send(setters[which](short, inst))
        return {"ok": True}

    async def set_pushbutton_events(self, addr, instance, mask):
        """Set the event filter mask (which events this button emits)."""
        if not _HAVE_DEVICE:
            raise RuntimeError("control device support requires python-dali 0.11")
        await self._ready()
        short = address.DeviceShort(addr)
        inst = InstanceNumber(int(instance))
        filt = pb.InstanceEventFilter(int(mask) & 0xFF)
        async with self._guard("set_pushbutton_events"):
            result = await self.driver.run_sequence(
                devseq.SetEventFilters(device=short, instance=inst, filter_value=filt)
            )
        return {"ok": True, "result": int(result) if result is not None else None}

    async def set_instance_scheme(self, addr, instance, scheme):
        """Set the event addressing scheme for an instance."""
        if not _HAVE_DEVICE:
            raise RuntimeError("control device support requires python-dali 0.11")
        short = self._device_short(addr)
        self._instance_number(instance)
        if type(scheme) is not int:
            raise ValueError("scheme must be an integer")
        scheme = EventScheme(scheme)
        await self._ready()
        async with self._guard("set_instance_scheme"):
            inst = await self._require_instance(short, instance)
            result = await self.driver.run_sequence(
                devseq.SetEventSchemes(
                    device=short, instance=inst, scheme=scheme
                )
            )
            actual = _val(result)
            await self._require_device(short)
            if actual != scheme:
                raise RuntimeError("event scheme readback mismatch")
        return {"ok": True, "result": actual, "scheme": actual}

    async def set_pushbutton_scheme(self, addr, instance, scheme):
        """Compatibility entry point; event schemes apply to every instance type."""
        return await self.set_instance_scheme(addr, instance, scheme)
