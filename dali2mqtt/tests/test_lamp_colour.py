"""DT8 discovery and commands, exercised without a physical DALI interface."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import dali.address as address
import pytest

from app import bridge
import app.lamp as lamp_module
from app.config import Config
from app.dali_config import DaliConfig
from app.devicesnamesconfig import DevicesNamesConfig
from app.lamp import Lamp
from conftest import cname, dtr_value, numeric


class ColourGear:
    def __init__(self, features=98, device_type=8, status=32):
        self.features = features
        self.device_type = device_type
        self.types = iter([1, 8, 254])
        self.status = status
        self.selector = 0
        self.values = {128: 153, 130: 500, 2: 300, 9: 254, 10: 127, 11: 0}

    def __call__(self, cmd):
        name = cname(cmd)
        if name == "DTR0":
            self.selector = dtr_value(cmd)
        if name == "QueryDeviceType":
            return numeric(self.device_type)
        if name == "QueryNextDeviceType":
            return numeric(next(self.types))
        if name == "QueryColourTypeFeatures":
            return numeric(self.features)
        if name == "QueryColourStatus":
            return numeric(self.status)
        if name == "QueryColourValue":
            value = self.values[self.selector]
            return numeric(value >> 8 if self.selector in (2, 128, 130) else value)
        if name == "QueryContentDTR0":
            return numeric(self.values[self.selector] & 255)
        if name == "QuerySceneLevel":
            return numeric(255)
        if name in ("QueryPhysicalMinimum", "QueryMinLevel"):
            return numeric(1)
        if name == "QueryActualLevel":
            return numeric(128)


@pytest.fixture
def lamp(driver, monkeypatch):
    monkeypatch.setattr(Config, "_instance", None)
    Config().setup({})
    monkeypatch.setattr(DevicesNamesConfig, "get_friendly_name", lambda self, name: name)
    client = SimpleNamespace(published=[])
    client.publish = lambda topic, payload, retain=False: client.published.append((topic, payload, retain))
    driver._responder = ColourGear()
    return Lamp(driver, client, address.Short(0), busy=asyncio.Event())


@pytest.mark.parametrize("device_type,features,expected", [
    (0, 98, {}), (254, 98, {}), (8, 2, {"rgb": False, "color_temp": True, "min_mireds": 153, "max_mireds": 500}),
    (8, 96, {"rgb": True}), (8, 64, {"rgb": False}),
    (255, 98, {"rgb": True, "color_temp": True, "min_mireds": 153, "max_mireds": 500}),
])
async def test_capabilities(driver, device_type, features, expected):
    driver._responder = ColourGear(features, device_type)
    assert await DaliConfig(driver).colour_capabilities(0) == expected


async def test_invalid_limits_keep_rgb(driver):
    gear = ColourGear()
    gear.values[128] = 65535
    driver._responder = gear
    assert await DaliConfig(driver).colour_capabilities(0) == {"rgb": True}


@pytest.mark.parametrize("status,expected", [
    (32, {"color_mode": "color_temp", "color_temp": 300}),
    (128, {"color_mode": "rgb", "rgb": [255, 128, 0]}), (16, {}),
])
async def test_read_actual_state(driver, status, expected):
    driver._responder = ColourGear(status=status)
    cfg = DaliConfig(driver)
    assert await cfg.colour_state(0, {"rgb": True, "color_temp": True}) == expected
    assert driver.sent_types()[0] == "QueryActualLevel"


async def test_discovery_and_retained_state(lamp):
    await lamp.read_current_state()
    topic, payload, retain = next(p for p in lamp.mqtt.published if "/light/" in p[0])
    discovery = json.loads(payload)
    assert retain
    assert discovery["unique_id"] == "dali2mqtt_lamp_0"
    assert discovery["brightness_command_topic"] == "dali2mqtt/lamp_0/brightness/set"
    assert discovery["color_temp_command_topic"] == "dali2mqtt/lamp_0/color_temp/set"
    assert discovery["rgb_command_topic"] == "dali2mqtt/lamp_0/rgb/set"
    assert discovery["min_mireds"] == 153
    assert ("dali2mqtt/lamp_0/color_temp/status", 300, True) in lamp.mqtt.published
    assert ("dali2mqtt/lamp_0/color_mode/status", "color_temp", True) in lamp.mqtt.published
    lamp.mqtt.published.clear()
    await lamp.read_current_state()
    assert next(json.loads(p[1])["unique_id"] for p in lamp.mqtt.published if "/light/" in p[0]) == discovery["unique_id"]


async def test_failed_detection_preserves_brightness(lamp):
    responder = lamp.driver._responder
    lamp.driver._responder = lambda cmd: None if cname(cmd) == "QueryDeviceType" else responder(cmd)
    await lamp.read_current_state()
    discovery = next(json.loads(p[1]) for p in lamp.mqtt.published if "/light/" in p[0])
    assert "brightness_command_topic" in discovery
    assert "rgb_command_topic" not in discovery
    assert "color_temp_command_topic" not in discovery


@pytest.mark.parametrize("payload,expected", [(b"0", 0), (b"128", 128), (b"255", 255)])
async def test_existing_brightness_command(lamp, payload, expected):
    await lamp.read_current_state()
    await bridge.on_message_brightness_cmd(None, {"all_lamps": {0: lamp}}, SimpleNamespace(
        topic="dali2mqtt/lamp_0/brightness/set", payload=payload))
    assert lamp.level == expected
    assert ("dali2mqtt/lamp_0/brightness/status", expected, True) in lamp.mqtt.published


async def test_rgb_conversion_and_readback(lamp):
    lamp.colour_capabilities = {"rgb": True}
    lamp.driver._responder.status = 128
    await lamp.setColour("rgb", "255,128,0")
    dtrs = [(cname(c), dtr_value(c)) for c in lamp.driver.sent[:3]]
    assert dtrs == [("DTR0", 254), ("DTR1", 127), ("DTR2", 0)]
    assert ("dali2mqtt/lamp_0/rgb/status", "255,128,0", True) in lamp.mqtt.published


@pytest.mark.parametrize("mode,payload", [
    ("rgb", "256,0,0"), ("rgb", "-1,0,0"), ("rgb", "1,2"),
    ("rgb", "a,0,0"), ("color_temp", "152"), ("color_temp", "501"),
    ("color_temp", "300.5"), ("color_temp", "-1"),
])
async def test_invalid_commands_never_touch_bus(lamp, mode, payload):
    lamp.colour_capabilities = {"rgb": True, "color_temp": True, "min_mireds": 153, "max_mireds": 500}
    with pytest.raises(ValueError):
        await lamp.setColour(mode, payload)
    assert not lamp.driver.sent


async def test_unsupported_command(lamp):
    with pytest.raises(ValueError):
        await lamp.setColour("rgb", "255,0,0")
    assert not lamp.driver.sent


async def test_failed_read_publishes_nothing(lamp):
    lamp.colour_capabilities = {"rgb": True}
    lamp.driver._responder = lambda cmd: None
    await lamp.pollColour()
    assert lamp.mqtt.published == []


async def test_callback_handles_failure_and_nested_base(lamp):
    lamp.colour_capabilities = {"rgb": True}
    lamp.colour_config.set_colour_rgb = AsyncMock(side_effect=RuntimeError("disconnected"))
    await bridge.on_message_colour_cmd(None, {"all_lamps": {0: lamp}}, SimpleNamespace(
        topic="house/dali2mqtt/lamp_0/rgb/set", payload=b"255,0,0"))
    lamp.colour_config.set_colour_rgb.assert_awaited_once_with(0, 254, 0, 0)
    assert not lamp.mqtt.published


async def test_shared_guard_serializes_configuration(lamp):
    cfg = DaliConfig(lamp.driver, busy=lamp.colour_config._busy)
    async with cfg._guard("test"):
        pending = asyncio.create_task(lamp.colour_config.colour_capabilities(0))
        await asyncio.sleep(0)
        assert not lamp.driver.sent
    await pending
    assert not lamp.colour_config._busy.is_set()


async def test_physical_minimum_is_on(lamp):
    responder = lamp.driver._responder
    lamp.driver._responder = lambda cmd: numeric(1) if cname(cmd) == "QueryActualLevel" else responder(cmd)
    await lamp.read_current_state()
    assert lamp.level == 1
    assert ("dali2mqtt/lamp_0/status", b"ON", True) in lamp.mqtt.published


async def test_brightness_failure_can_be_retried(lamp):
    await lamp.read_current_state()
    old = lamp.level
    lamp.mqtt.published.clear()
    original = lamp._sendLevelDALI
    lamp._sendLevelDALI = AsyncMock(side_effect=RuntimeError("disconnected"))
    with pytest.raises(RuntimeError):
        await lamp.setLevel(12)
    assert lamp.level == old
    assert lamp.mqtt.published == []
    lamp._sendLevelDALI = original
    await lamp.setLevel(12)
    assert lamp.level == 12


@pytest.mark.parametrize("value", [153, 500])
async def test_temperature_bounds_send_word(lamp, value):
    lamp.colour_capabilities = {"color_temp": True, "min_mireds": 153, "max_mireds": 500}
    await lamp.setColour("color_temp", str(value))
    assert [(cname(c), dtr_value(c)) for c in lamp.driver.sent[:2]] == [
        ("DTR0", value & 255), ("DTR1", value >> 8)]


async def test_poll_refreshes_external_colour_change(lamp):
    await lamp.read_current_state()
    lamp.driver._responder.status = 128
    lamp.driver._responder.values[9] = 0
    lamp.driver._responder.values[11] = 254
    lamp.mqtt.published.clear()
    await lamp.pollLevel()
    assert ("dali2mqtt/lamp_0/rgb/status", "0,128,255", True) in lamp.mqtt.published
    assert ("dali2mqtt/lamp_0/color_mode/status", "rgb", True) in lamp.mqtt.published


@pytest.mark.parametrize("features,temp,rgb", [(0, False, False), (2, True, False), (96, False, True)])
async def test_discovery_only_advertises_supported_controls(lamp, features, temp, rgb):
    lamp.driver._responder.features = features
    await lamp.read_current_state()
    config = next(json.loads(p[1]) for p in lamp.mqtt.published if "/light/" in p[0])
    assert ("color_temp_command_topic" in config) == temp
    assert ("rgb_command_topic" in config) == rgb
    assert "brightness_command_topic" in config


async def test_home_assistant_birth_reinitializes(monkeypatch):
    initialize = AsyncMock()
    monkeypatch.setattr(bridge, "initialize_lamps", initialize)
    data, client = {}, object()
    await bridge.on_message_ha_online(client, data, SimpleNamespace(payload=b"online"))
    initialize.assert_awaited_once_with(data, client)


async def test_dali_failure_releases_guard(lamp):
    def fail(cmd):
        raise RuntimeError("device disconnected")
    lamp.driver._responder = fail
    with pytest.raises(RuntimeError):
        await lamp.colour_config.colour_capabilities(0)
    assert not lamp.colour_config._busy.is_set()
    assert not lamp.colour_config._lock.locked()


async def test_stalled_detection_does_not_block_brightness(lamp, monkeypatch):
    monkeypatch.setattr(lamp_module, "COLOUR_OPERATION_TIMEOUT", 0.01)
    async def stalled_sequence(sequence):
        try:
            await asyncio.Event().wait()
        finally:
            sequence.close()
    lamp.driver.run_sequence = stalled_sequence
    await asyncio.wait_for(lamp.read_current_state(), 1)
    assert ("dali2mqtt/lamp_0/status", b"ON", True) in lamp.mqtt.published
    assert not lamp.colour_config._busy.is_set()
    assert not lamp.colour_config._lock.locked()


async def test_stalled_state_read_keeps_brightness_and_releases_bus(lamp, monkeypatch):
    await lamp.read_current_state()
    lamp.mqtt.published.clear()
    monkeypatch.setattr(lamp_module, "COLOUR_OPERATION_TIMEOUT", 0.01)
    async def stalled_sequence(sequence):
        try:
            await asyncio.Event().wait()
        finally:
            sequence.close()
    lamp.driver.run_sequence = stalled_sequence
    await asyncio.wait_for(lamp.pollLevel(), 1)
    assert lamp.mqtt.published == []
    assert not lamp.colour_config._busy.is_set()
    assert not lamp.colour_config._lock.locked()


async def test_single_level_controller_reports_full_brightness(lamp):
    responder = lamp.driver._responder
    lamp.driver._responder = lambda cmd: numeric(254) if cname(cmd) in (
        "QueryActualLevel", "QueryPhysicalMinimum", "QueryMinLevel") else responder(cmd)
    await lamp.read_current_state()
    assert lamp.level == 255
