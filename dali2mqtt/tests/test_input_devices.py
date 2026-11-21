"""Real python-dali frames and addressed configuration/readback contracts."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dali.command import Command
from dali.device import general as device
from dali.device.helpers import DeviceInstanceTypeMapper
from dali.frame import BackwardFrame, BackwardFrameError, ForwardFrame

from app import bridge
from app.dali_config import DaliConfig
from conftest import FakeDriver, cname, dest_addr, no, numeric, yes


@pytest.mark.parametrize("addr,instance,itype,info,expected", [
    (0, 0, 1, 0, "ButtonReleased"),
    (0, 0, 1, 1, "ButtonPressed"),
    (12, 0, 1, 2, "ShortPress"),
    (13, 2, 0, 6, "UnknownEvent"),
    (14, 2, None, 5, "AmbiguousInstanceType"),
    (15, 0, 1, 1023, "UnknownEvent"),
])
def test_real_frame_input_event(monkeypatch, addr, instance, itype, info, expected):
    mapper = DeviceInstanceTypeMapper()
    if itype is not None:
        mapper.add_type(short_address=addr, instance_number=instance, instance_type=itype)
    raw = (addr << 17) | 0x8000 | (instance << 10) | info
    cmd = Command.from_frame(ForwardFrame(24, raw), dev_inst_map=mapper)
    assert cname(cmd) == expected
    published = []
    monkeypatch.setitem(bridge._runtime, "client", type("Client", (), {
        "publish": lambda self, topic, payload: published.append((topic, json.loads(payload)))})())
    monkeypatch.setattr(bridge, "Config", lambda: {"mqtt_base_topic": "test/bus"})
    data = {"all_lamps": {addr: None}, "queue": asyncio.Queue(), "mirror": object()}
    bridge.print_command_and_response(data, None, cmd, None, False)
    assert data["queue"].empty()
    topic, event = published.pop()
    assert topic == "test/bus/bus_event"
    assert event["event"] is True
    assert event["source_address"] == addr
    assert event["instance_number"] == instance
    assert event["instance_type"] == itype
    assert event["raw_frame"] == raw
    assert event["event_info"] == info
    assert event["address"] is None and event["destination_kind"] is None
    assert event["destination"] == ""
    assert event["device_group"] is None and event["instance_group"] is None


@pytest.mark.parametrize("source,raw", [("device_group", 0x800000), ("instance_group", 0xC00000)])
def test_group_event_identity(monkeypatch, source, raw):
    raw |= (3 << 17) | (1 << 10) | 2
    cmd = Command.from_frame(ForwardFrame(24, raw))
    with patch.object(bridge, "Config", return_value={"mqtt_base_topic": "test"}):
        client = type("Client", (), {"publish": lambda self, t, p: setattr(self, "event", json.loads(p))})()
        monkeypatch.setitem(bridge._runtime, "client", client)
        bridge.publish_bus_event(cmd, None)
    assert client.event[source] == 3
    assert client.event["source_address"] is None
    assert client.event["instance_number"] is None


def test_input_event_respects_error_and_busy_guards(monkeypatch):
    cmd = Command.from_frame(ForwardFrame(24, 0x8001))
    with patch.object(bridge, "publish_bus_event") as publish:
        bridge.print_command_and_response({}, None, cmd, None, True)
        bridge.config_busy.set()
        try:
            bridge.print_command_and_response({}, None, cmd, None, False)
        finally:
            bridge.config_busy.clear()
        publish.assert_not_called()


class DeviceBus:
    """Stateful replies using actual library response classes, including NO."""
    def __init__(self):
        self.addresses = {0, 7}
        self.types = [1, 0, 0, 2]
        self.enabled = [True, False, True, False]
        self.schemes = [2, 0, 2, 1]
        self.controller = True
        self.dtr = 0
        self.ignore_writes = False

    def __call__(self, cmd):
        name, addr = cname(cmd), dest_addr(cmd)
        if name == "DTR0":
            self.dtr = cmd.param
            return
        if addr not in self.addresses:
            return cmd.response(None) if cmd.response else None
        inst = getattr(cmd, "instance", None)
        index = inst.value if inst is not None else None
        if name == "QueryDeviceStatus":
            return device.QueryDeviceStatusResponse(BackwardFrame(8 if self.controller else 0))
        if name == "QueryDeviceCapabilities":
            return device.QueryDeviceCapabilitiesResponse(BackwardFrame(3))
        if name == "QueryApplicationControlEnabled":
            return yes() if self.controller else no()
        if name == "QueryNumberOfInstances":
            return numeric(len(self.types))
        if name == "QueryOperatingMode":
            return numeric(0)
        if name == "QueryInstanceType":
            return numeric(self.types[index])
        if name == "QueryInstanceEnabled":
            return yes() if self.enabled[index] else no()
        if name == "QueryEventScheme":
            return device.QueryEventSchemeResponse(BackwardFrame(self.schemes[index]))
        if not self.ignore_writes:
            if name in ("EnableApplicationController", "DisableApplicationController"):
                self.controller = name == "EnableApplicationController"
            if name in ("EnableInstance", "DisableInstance"):
                self.enabled[index] = name == "EnableInstance"
            if name == "SetEventScheme":
                self.schemes[index] = self.dtr
            if name == "SetShortAddress":
                self.addresses.remove(addr)
                self.addresses.add(self.dtr)


async def test_four_instances_and_configuration_readback(busy):
    bus = DeviceBus()
    driver = FakeDriver(bus)
    cfg = DaliConfig(driver, busy)
    info = await cfg.read_device(0)
    assert info["application_controller"] == {"present": True, "always_active": False, "enabled": True}
    assert [i["type"] for i in info["instances"]] == [1, 0, 0, 2]
    assert [i["enabled"] for i in info["instances"]] == [True, False, True, False]
    assert [i["scheme"] for i in info["instances"]] == [2, 0, 2, 1]
    assert (await cfg.set_pushbutton_scheme(0, 2, 0))["scheme"] == 0
    assert (await cfg.set_instance_enabled(0, 2, False))["enabled"] is False
    assert (await cfg.set_application_controller(0, False))["application_controller"]["enabled"] is False
    assert (await cfg.save_device(0))["persistence_verified"] is False
    writes = [c for c in driver.sent if not cname(c).startswith("Query") and cname(c) != "DTR0"]
    assert [cname(c) for c in writes] == ["SetEventScheme", "DisableInstance", "DisableApplicationController", "SavePersistentVariables"]
    assert all(dest_addr(c) == 0 for c in writes)
    assert writes[0].instance.value == 2 and writes[1].instance.value == 2
    assert writes[-1].sendtwice
    assert not busy.is_set()


@pytest.mark.parametrize("method,args", [
    ("set_instance_enabled", (-1, 0, True)),
    ("set_instance_enabled", (64, 0, True)),
    ("set_instance_enabled", (0, 0, 1)),
    ("set_instance_enabled", (0, 0, "false")),
    ("set_instance_enabled", (0, 32, True)),
    ("set_instance_enabled", (0, 4, True)),
    ("set_application_controller", (0, None)),
    ("set_pushbutton_scheme", (0, 0, 5)),
    ("set_pushbutton_scheme", (0, 0, True)),
    ("set_pushbutton_scheme", (0, 0, "2")),
    ("set_pushbutton_scheme", (0, 4, 2)),
])
async def test_invalid_configuration_never_writes(method, args):
    driver = FakeDriver(DeviceBus())
    with pytest.raises(ValueError):
        await getattr(DaliConfig(driver), method)(*args)
    assert all(cname(c).startswith("Query") for c in driver.sent)


@pytest.mark.parametrize("method,args", [
    ("set_instance_enabled", (0, 0, False)),
    ("set_application_controller", (0, False)),
    ("set_pushbutton_scheme", (0, 0, 0)),
])
async def test_mismatch_clears_guard(method, args, busy):
    bus = DeviceBus()
    bus.ignore_writes = True
    cfg = DaliConfig(FakeDriver(bus), busy)
    with pytest.raises(RuntimeError, match="mismatch"):
        await getattr(cfg, method)(*args)
    assert not busy.is_set()


async def test_disconnected_is_not_disabled(busy):
    bus = DeviceBus()
    def responder(cmd):
        if cname(cmd) == "DisableInstance":
            bus.addresses.clear()
        return bus(cmd)
    cfg = DaliConfig(FakeDriver(responder), busy)
    with pytest.raises(RuntimeError, match="did not respond"):
        await cfg.set_instance_enabled(0, 0, False)
    assert not busy.is_set()


async def test_missing_device_and_driver_failure(busy):
    driver = FakeDriver(DeviceBus())
    cfg = DaliConfig(driver, busy)
    with pytest.raises(RuntimeError):
        await cfg.save_device(3)
    assert all(cname(c).startswith("Query") for c in driver.sent)
    def fail(cmd):
        raise OSError("USB lost")
    driver._responder = fail
    with pytest.raises(OSError):
        await cfg.save_device(0)
    assert not busy.is_set()


@pytest.mark.parametrize("caps,enabled", [(0, True), (5, False), (None, False)])
async def test_controller_capability_rejects_unsupported_writes(caps, enabled):
    bus = DeviceBus()
    def responder(cmd):
        if cname(cmd) == "QueryDeviceCapabilities":
            return device.QueryDeviceCapabilitiesResponse(BackwardFrame(caps) if caps is not None else None)
        return bus(cmd)
    driver = FakeDriver(responder)
    with pytest.raises(ValueError):
        await DaliConfig(driver).set_application_controller(0, enabled)
    assert all(cname(c).startswith("Query") for c in driver.sent)


async def test_mapper_queries_missing_types_leave_raw_event_support():
    bus = DeviceBus()
    def responder(cmd):
        if cname(cmd) == "QueryInstanceType":
            return numeric(1) if cmd.instance.value == 0 else numeric(255)
        return bus(cmd)
    driver = FakeDriver(responder)
    driver.dev_inst_map = DeviceInstanceTypeMapper()
    await DaliConfig(driver).refresh_instance_map()
    cmd = Command.from_frame(ForwardFrame(24, 0x8806), dev_inst_map=driver.dev_inst_map)
    assert isinstance(cmd, device.AmbiguousInstanceType)
    assert cmd.frame.as_integer & 0x3FF == 6


async def test_mapper_rebuild_startup_address_change_and_stale_removal():
    bus = DeviceBus()
    driver = FakeDriver(bus)
    driver.dev_inst_map = DeviceInstanceTypeMapper({(63, 31): 1})
    mapper = driver.dev_inst_map
    cfg = DaliConfig(driver)
    await cfg.refresh_instance_map()
    assert driver.dev_inst_map is mapper
    assert mapper.mapping == {(addr, i): t for addr in (0, 7) for i, t in enumerate(bus.types)}
    assert all(cname(c).startswith("Query") for c in driver.sent)
    await cfg.change_device_address(7, 8)
    assert (7, 0) not in mapper.mapping and mapper.mapping[(8, 0)] == 1
    bus.addresses.clear()
    await cfg.refresh_instance_map()
    assert mapper.mapping == {}


def test_tridonic_receives_shared_mapper():
    from app.monitored_tridonic import MonitoredTridonic

    driver = bridge.build_driver({"dali_driver": "tridonic", "dali_device": "test"}, None)
    assert isinstance(driver, MonitoredTridonic)
    assert isinstance(driver.dev_inst_map, DeviceInstanceTypeMapper)
    assert driver._bus_monitor.capabilities["raw_reports"] is True


async def test_startup_populates_map_before_lamps(monkeypatch):
    driver = FakeDriver(DeviceBus())
    driver.dev_inst_map = DeviceInstanceTypeMapper()
    data = {"driver": driver}
    client = MagicMock()
    monkeypatch.setitem(bridge._runtime, "data", None)
    monkeypatch.setitem(bridge._runtime, "client", None)
    monkeypatch.setattr(bridge, "Config", lambda: {
        "mqtt_base_topic": "test", "ha_discovery_prefix": "homeassistant"})
    monkeypatch.setattr(bridge, "start_listening_on_dali", AsyncMock())
    async def initialize(data, client):
        assert driver.dev_inst_map.mapping[(0, 0)] == 1
        assert bridge._runtime["client"] is client
    monkeypatch.setattr(bridge, "initialize_lamps", initialize)
    monkeypatch.setattr(bridge, "register_bridge", AsyncMock())
    monkeypatch.setattr(bridge, "process_queue", AsyncMock())
    await bridge.on_connect(client, data, None, None)


async def test_shared_guard_serializes_config_instances(driver, busy):
    first, second = DaliConfig(driver, busy), DaliConfig(driver, busy)
    entered = asyncio.Event()
    async def wait_for_guard():
        async with second._guard():
            entered.set()
    async with first._guard():
        task = asyncio.create_task(wait_for_guard())
        await asyncio.sleep(0)
        assert busy.is_set() and not entered.is_set()
    await task
    assert entered.is_set() and not busy.is_set()


async def test_address_change_refuses_framing_error_at_new_address():
    bus = DeviceBus()
    def responder(cmd):
        if cname(cmd) == "QueryDeviceStatus" and dest_addr(cmd) == 8:
            return device.QueryDeviceStatusResponse(BackwardFrameError(0))
        return bus(cmd)
    driver = FakeDriver(responder)
    with pytest.raises(ValueError, match="occupied"):
        await DaliConfig(driver).change_device_address(7, 8)
    assert all(cname(c).startswith("Query") for c in driver.sent)
