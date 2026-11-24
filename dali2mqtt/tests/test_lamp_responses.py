"""Required lamp replies must never corrupt retained state or abort healthy lamps."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import dali.address as address
import dali.gear.general as gear
from dali.frame import BackwardFrame, BackwardFrameError
import pytest

from app import bridge
from app.config import Config
from app.devicesnamesconfig import DevicesNamesConfig
from app.lamp import Lamp, LampReadError
from conftest import FakeDriver, RawResp, cname, dest_addr, numeric, yes, no


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(Config, "_instance", None)
    Config().setup({})
    monkeypatch.setattr(DevicesNamesConfig, "get_friendly_name", lambda self, name: name)
    monkeypatch.setattr(DevicesNamesConfig, "is_devices_file_empty", lambda self: False)
    monkeypatch.setattr(bridge, "config_busy", asyncio.Event())
    mqtt = SimpleNamespace(published=[])
    mqtt.publish = lambda *args, **kw: mqtt.published.append((args, kw))
    return mqtt


def response(cmd):
    name = cname(cmd)
    if name == "QueryControlGearPresent":
        return yes() if dest_addr(cmd) in (0, 1, 2) else no()
    if name.startswith("QueryGroups"):
        return cmd.response(BackwardFrame(1 if name == "QueryGroupsZeroToSeven" else 0))
    if name == "QuerySceneLevel":
        return cmd.response(BackwardFrame(255))
    if name in ("QueryPhysicalMinimum", "QueryMinLevel"):
        return numeric(1)
    if name == "QueryActualLevel":
        return cmd.response(BackwardFrame(128))
    return None


def make_lamp(mqtt):
    return Lamp(FakeDriver(response), mqtt, address.Short(0), busy=bridge.config_busy)


INVALID = [
    (None, "missing"),
    (gear.QueryActualLevel.response(None), "missing"),
    (gear.QueryActualLevel.response(BackwardFrameError(0)), "framing_error"),
    (gear.QueryActualLevel.response(BackwardFrameError(255)), "framing_error"),
    (gear.QueryActualLevel.response(BackwardFrame(255)), "mask"),
    *[(RawResp(v, raw_int=1), "invalid_value") for v in (True, False, "oops", 1.5, -1, 256)],
]


@pytest.mark.parametrize("reply,reason", INVALID)
@pytest.mark.parametrize("operation", ["read_current_state", "pollLevel", "_getLevelDALI"])
async def test_invalid_actual_preserves_state(setup, reply, reason, operation):
    lamp = make_lamp(setup)
    await lamp.read_current_state()
    original = lamp.__dict__.copy()
    setup.published.clear()
    lamp.driver._responder = lambda cmd: reply if cname(cmd) == "QueryActualLevel" else response(cmd)
    with pytest.raises(LampReadError) as caught:
        await getattr(lamp, operation)()
    assert caught.value.reason == reason
    assert caught.value.query == "QueryActualLevel"
    assert lamp.__dict__ == original
    assert not setup.published
    lamp.driver._responder = response
    await getattr(lamp, operation)()
    assert lamp.level > 0


@pytest.mark.parametrize("query,value", [
    ("QueryPhysicalMinimum", 0), ("QueryMinLevel", 255),
    ("QuerySceneLevel", True), ("QuerySceneLevel", -1),
])
async def test_invalid_prerequisite_is_atomic(setup, query, value):
    lamp = make_lamp(setup)
    await lamp.read_current_state()
    fields = lamp.__dict__.copy()
    setup.published.clear()
    lamp.driver._responder = lambda cmd: RawResp(value, raw_int=1) if cname(cmd) == query else response(cmd)
    with pytest.raises(LampReadError):
        await lamp.read_current_state()
    assert fields == lamp.__dict__
    assert setup.published == []


@pytest.mark.parametrize("actual,minimum,expected", [(0, 1, 0), (1, 1, 1), (254, 1, 255), (254, 254, 255), (0, 254, 0)])
async def test_normalization(setup, actual, minimum, expected):
    lamp = make_lamp(setup)
    lamp.driver._responder = lambda cmd: numeric(actual) if cname(cmd) == "QueryActualLevel" else numeric(minimum) if cname(cmd) in ("QueryMinLevel", "QueryPhysicalMinimum") else response(cmd)
    await lamp.read_current_state()
    assert lamp.level == expected
    assert lamp.scenes == ["MASK"] * 16


async def test_partial_discovery_and_repeated_group_rebuild(setup):
    driver = FakeDriver(response)
    data = {"driver": driver, "all_lamps": {}, "all_groups": {}}
    original_response = response
    driver._responder = lambda cmd: None if dest_addr(cmd) == 0 and cname(cmd) == "QueryActualLevel" else original_response(cmd)
    summary = await bridge.initialize_lamps(data, setup)
    assert summary["partial"] and summary["refreshed"] == [1, 2]
    assert 0 not in data["all_lamps"] and not data["all_groups"]
    assert {f["kind"] for f in summary["failed"]} == {"lamp", "group"}
    driver._responder = response
    assert not (await bridge.initialize_lamps(data, setup))["partial"]
    lamps = data["all_lamps"].copy()
    old_group = data["all_groups"][0]
    driver._responder = lambda cmd: None if dest_addr(cmd) == 0 and cname(cmd) == "QueryActualLevel" else response(cmd)
    old_level = lamps[0].level
    await bridge.initialize_lamps(data, setup)
    assert data["all_groups"][0] is old_group
    assert lamps[0].level == old_level
    assert all(data["all_lamps"][a] is lamp for a, lamp in lamps.items())
    driver._responder = response
    for _ in range(2):
        await bridge.initialize_lamps(data, setup)
        assert all(lamp.groups == [data["all_groups"][0]] for lamp in lamps.values())
        assert set(data["all_groups"][0].lamps) == set(lamps.values())
    assert not driver._config_lock.locked() and not bridge.config_busy.is_set()


async def test_all_failed_discovery(setup):
    driver = FakeDriver(lambda cmd: None if cname(cmd) == "QueryActualLevel" else response(cmd))
    data = {"driver": driver, "all_lamps": {}, "all_groups": {}}
    result = await bridge.initialize_lamps(data, setup)
    assert result["ok"] and result["partial"] and result["refreshed"] == []
    assert data["all_lamps"] == {}


@pytest.mark.parametrize("error", [RuntimeError("transport"), asyncio.CancelledError()])
async def test_failure_releases_guard(setup, error):
    driver = FakeDriver(response)
    async def fail(cmd):
        raise error
    driver.send = fail
    with pytest.raises(type(error)):
        await bridge.initialize_lamps({"driver": driver, "all_lamps": {}, "all_groups": {}}, setup)
    assert not driver._config_lock.locked() and not bridge.config_busy.is_set()


async def test_poll_continues_only_on_invalid_reply(setup):
    bad = SimpleNamespace(pollLevel=AsyncMock(side_effect=LampReadError(0, "QueryActualLevel", "missing")))
    good = SimpleNamespace(pollLevel=AsyncMock())
    data = {"all_lamps": {0: bad, 1: good}, "all_groups": {}}
    await bridge.on_message_poll_lamps_cmd(None, data, None)
    good.pollLevel.assert_awaited_once()
    bad.pollLevel.side_effect = RuntimeError("transport")
    with pytest.raises(RuntimeError):
        await bridge.on_message_poll_lamps_cmd(None, data, None)

async def test_initialization_waits_for_configuration_guard(setup):
    driver = FakeDriver(response)
    data = {"driver": driver, "all_lamps": {}, "all_groups": {}}
    from app.dali_config import DaliConfig
    cfg = DaliConfig(driver, busy=bridge.config_busy)
    async with cfg._guard("other operation"):
        task = asyncio.create_task(bridge.initialize_lamps(data, setup))
        await asyncio.sleep(0)
        assert driver.sent == []
    await asyncio.wait_for(task, 1)
    assert not bridge.config_busy.is_set() and not cfg._lock.locked()


async def test_cancel_required_read_releases_guard(setup):
    driver = FakeDriver(response)
    entered = asyncio.Event()
    original = driver.send
    async def send(cmd):
        if cname(cmd) == "QueryActualLevel":
            entered.set()
            await asyncio.Event().wait()
        return await original(cmd)
    driver.send = send
    task = asyncio.create_task(bridge.initialize_lamps(
        {"driver": driver, "all_lamps": {}, "all_groups": {}}, setup))
    await asyncio.wait_for(entered.wait(), 1)
    assert bridge.config_busy.is_set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not bridge.config_busy.is_set() and not driver._config_lock.locked()


async def test_invalid_membership_preserves_group(setup):
    driver = FakeDriver(response)
    data = {"driver": driver, "all_lamps": {}, "all_groups": {}}
    await bridge.initialize_lamps(data, setup)
    group = data["all_groups"][0]
    driver._responder = lambda cmd: None if cname(cmd) == "QueryGroupsZeroToSeven" and dest_addr(cmd) == 0 else response(cmd)
    result = await bridge.initialize_lamps(data, setup)
    assert result["partial"]
    assert data["all_groups"][0] is group
    assert {"kind": "group", "address": 0, "query": "QueryGroupsZeroToSeven", "reason": "missing"} in result["failed"]


@pytest.mark.parametrize("fatal", [False, True])
async def test_queued_invalid_read_continues_and_transport_failure_propagates(setup, monkeypatch, fatal):
    real_sleep = asyncio.sleep
    class AsyncioProxy:
        @staticmethod
        async def sleep(delay):
            await real_sleep(0)
        def __getattr__(self, name):
            return getattr(asyncio, name)
    monkeypatch.setattr(bridge, "asyncio", AsyncioProxy())
    entered = asyncio.Event()
    good = SimpleNamespace(read_current_state=AsyncMock(side_effect=lambda: entered.set()))
    bad = make_lamp(setup)
    await bad.read_current_state()
    fields = bad.__dict__.copy()
    setup.published.clear()
    if fatal:
        bad.driver.send = AsyncMock(side_effect=RuntimeError("transport"))
    else:
        bad.driver._responder = lambda cmd: None if cname(cmd) == "QueryActualLevel" else response(cmd)
    data = {"all_lamps": {0: bad, 1: good}, "queue": asyncio.Queue()}
    bridge.queue_lamp_update(data, 0)
    bridge.queue_lamp_update(data, 1)
    task = asyncio.create_task(bridge.process_queue(None, data))
    if fatal:
        with pytest.raises(RuntimeError):
            await task
        good.read_current_state.assert_not_awaited()
    else:
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert fields == bad.__dict__ and not setup.published
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


def test_pinned_dali_response_reproduces_original_type_error():
    reply = gear.QueryActualLevel.response(BackwardFrameError(255))
    assert reply.value == "(framing error)"
    with pytest.raises(TypeError):
        max(reply.value, 1)


@pytest.mark.parametrize("query", ["QuerySceneLevel", "QueryMinLevel", "QueryPhysicalMinimum"])
@pytest.mark.parametrize("reply,reason", INVALID[:4])
async def test_missing_and_framing_prerequisites_preserve_fields(setup, query, reply, reason):
    lamp = make_lamp(setup)
    await lamp.read_current_state()
    fields = lamp.__dict__.copy()
    setup.published.clear()
    lamp.driver._responder = lambda cmd: reply if cname(cmd) == query else response(cmd)
    with pytest.raises(LampReadError) as caught:
        await lamp.read_current_state()
    assert caught.value.reason == reason
    assert fields == lamp.__dict__ and not setup.published
