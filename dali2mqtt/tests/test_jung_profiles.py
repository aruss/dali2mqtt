"""Jung profile discovery, physical frame routing and ingress assignment."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

from dali.command import Command
from dali.device.helpers import DeviceInstanceTypeMapper
from dali.frame import ForwardFrame
from dali.frame import BackwardFrame

from app import bridge
from app.jung import ABSOLUTE, ACTIONS, GESTURES, JungProfiles
from conftest import FakeDriver, dest_addr
from test_input_devices import DeviceBus
from test_webserver import _client


class Broker:
    def __init__(self):
        self.retained = {}
        self.messages = []
        self.publications = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.publications.append((topic, payload, retain))
        if retain:
            if payload:
                self.retained[topic] = json.loads(payload)
            else:
                self.retained.pop(topic, None)
        else:
            self.messages.append((topic, payload))


async def test_scan_absence_preserves_profile_discovery_and_event_topic(tmp_path):
    profiles = JungProfiles(tmp_path, "home/bus", "ha")
    broker = Broker()
    profiles.refresh(broker)
    driver = FakeDriver()
    client = await _client(driver, profiles=profiles)
    try:
        response = await client.post("/api/device/0/profile", json={"profile": ABSOLUTE})
        assert response.status == 200
        saved = profiles.path.read_bytes()
        discovery = broker.retained.copy()
        broker.publications.clear()
        for present in (True, False, True):
            driver._responder = lambda cmd: cmd.response(BackwardFrame(0)) if present and dest_addr(cmd) == 0 else None
            response = await client.get("/api/scan_devices")
            assert response.status == 200
            devices = (await response.json())["devices"]
            assert [d["address"] for d in devices] == ([0] if present else [])
            if present:
                assert devices[0]["profile"] == ABSOLUTE
            assert profiles.path.read_bytes() == saved
            assert broker.retained == discovery
            assert not broker.publications
        profiles.handle_event({"source_address": 0, "instance_number": 0,
                               "instance_type": 1, "event_info": 1})
        assert broker.messages == [("home/bus/control_device_0/action", '{"event_type": "press"}')]
    finally:
        await client.close()


async def test_profile_assignment_without_bus_writes(tmp_path):
    profiles = JungProfiles(tmp_path, "home/bus", "ha")
    broker = Broker()
    profiles.refresh(broker)
    driver = FakeDriver(DeviceBus())
    client = await _client(driver, profiles=profiles)
    try:
        for addr, body, status in [(64, {"profile": GESTURES}, 400), (1, {}, 400),
                                  (1, {"profile": "other"}, 400), (1, {"profile": GESTURES}, 200)]:
            response = await client.post(f"/api/device/{addr}/profile", json=body)
            assert response.status == status
        assert not driver.sent
        assert len(broker.retained) == 11
        assert all(c["device"]["name"] == "Jung 1745 DSTE D1" for c in broker.retained.values())
    finally:
        await client.close()


def test_routing_and_restart(tmp_path, monkeypatch):
    profiles = JungProfiles(tmp_path, "home/bus", "ha")
    broker = Broker()
    profiles.refresh(broker)
    profiles.assign(1, ABSOLUTE)
    profiles.assign(2, GESTURES)
    assert len(broker.retained) == 23
    assert len({c["device"]["identifiers"][0] for c in broker.retained.values()}) == 2
    assert not broker.messages
    monkeypatch.setitem(bridge._runtime, "client", broker)
    monkeypatch.setitem(bridge._runtime, "profiles", profiles)
    monkeypatch.setattr(bridge, "Config", lambda: {"mqtt_base_topic": "home/bus"})
    data = {"queue": asyncio.Queue(), "all_lamps": {1: None}}
    def send(addr, instance, info):
        mapper = DeviceInstanceTypeMapper()
        mapper.add_type(short_address=addr, instance_number=instance,
                        instance_type={0: 1, 2: 0, 3: 2}[instance])
        raw = (addr << 17) | 0x8000 | (instance << 10) | info
        command = Command.from_frame(ForwardFrame(24, raw), dev_inst_map=mapper)
        broker.messages.clear()
        bridge.print_command_and_response(data, None, command, None, False)
        generic = broker.messages.pop(0)
        assert generic[0] == "home/bus/bus_event"
        assert json.loads(generic[1])["raw_frame"] == raw
        assert data["queue"].empty()
        return broker.messages

    for addr in (1, 2):
        for (instance, code), action in ACTIONS.items():
            [(topic, payload)] = send(addr, instance, code)
            assert topic == f"home/bus/control_device_{addr}/action"
            assert json.loads(payload) == {"event_type": action}
    assert not send(0, 0, 1)
    assert not send(1, 2, 7)
    assert not send(2, 3, 512)
    for value in (0, 512, 1023):
        assert send(1, 3, value) == [("home/bus/control_device_1/rotary_input", str(value))]
    send(2, 0, 1)
    broker.messages.clear()
    discovery = broker.retained.copy()
    restored = JungProfiles(tmp_path, "home/bus", "ha")
    restored.refresh(broker)
    restored.refresh(broker)
    assert broker.retained == discovery
    assert not broker.messages
    restored.assign(1, GESTURES)
    assert not any("/sensor/" in key for key in broker.retained)
    # Remove while disconnected; a new process clears the previous discovery.
    JungProfiles(tmp_path, "home/bus", "ha").assign(2, None)
    JungProfiles(tmp_path, "home/bus", "ha").refresh(broker)
    assert len(broker.retained) == 11
    assert all(c["device"]["name"].endswith("D1") for c in broker.retained.values())


async def test_reconnect_registers_bus_listener_once(monkeypatch):
    driver = FakeDriver(DeviceBus())
    listen = AsyncMock()
    monkeypatch.setattr(bridge, "Config", lambda: {"mqtt_base_topic": "test", "ha_discovery_prefix": "ha"})
    monkeypatch.setattr(bridge, "start_listening_on_dali", listen)
    monkeypatch.setattr(bridge, "initialize_lamps", AsyncMock())
    monkeypatch.setattr(bridge, "register_bridge", AsyncMock())
    monkeypatch.setitem(bridge._runtime, "client", None)
    monkeypatch.setitem(bridge._runtime, "data", None)
    profiles = MagicMock()
    monkeypatch.setitem(bridge._runtime, "profiles", profiles)
    data = {"driver": driver}
    client = MagicMock()
    await bridge.on_connect(client, data, None, None)
    worker = data["queue_worker"]
    try:
        await bridge.on_connect(client, data, None, None)
        listen.assert_awaited_once()
        assert data["queue_worker"] is worker
        await bridge.reinit_discovery()
        await bridge.on_message_ha_online(client, data, SimpleNamespace(payload=b"online"))
        assert profiles.refresh.call_count == 4
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
