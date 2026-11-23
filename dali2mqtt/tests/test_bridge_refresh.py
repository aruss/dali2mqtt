"""Delayed queries and slider bursts must not create a bus-read feedback loop."""
import asyncio
from unittest.mock import AsyncMock

import dali.address as address
import dali.gear.colour as colour
import dali.gear.general as gear
import pytest

from app import bridge


@pytest.mark.parametrize("command", [
    gear.QueryDeviceType(address.Short(0)),
    gear.QueryNextDeviceType(address.Short(0)),
    gear.QueryContentDTR0(address.Short(0)),
    colour.QueryColourStatus(address.Short(0)),
    colour.QueryColourTypeFeatures(address.Short(0)),
    colour.QueryColourValue(address.Short(0)),
    gear.QueryActualLevel(address.Broadcast()),
])
def test_delayed_query_after_guard_never_enqueues_refresh(monkeypatch, command):
    monkeypatch.setattr(bridge, "config_busy", asyncio.Event())
    data = {"all_lamps": {0: object()}, "queue": asyncio.Queue()}
    bridge.print_command_and_response(data, None, command, None, False)
    assert data["queue"].empty()


def test_changes_coalesce_per_lamp(monkeypatch):
    monkeypatch.setattr(bridge, "config_busy", asyncio.Event())
    monkeypatch.setattr(bridge, "publish_bus_event", lambda *args: None)
    data = {"all_lamps": {0: object(), 1: object()}, "queue": asyncio.Queue()}
    for _ in range(100):
        bridge.print_command_and_response(data, None, gear.DAPC(address.Short(0), 128), None, False)
        bridge.print_command_and_response(data, None, gear.Off(address.Broadcast()), None, False)
    assert data["queue"].qsize() == 2
    assert data["pending_lamp_updates"] == {0, 1}


async def test_change_during_read_schedules_one_followup(monkeypatch):
    monkeypatch.setattr(bridge, "config_busy", asyncio.Event())
    entered, finish = asyncio.Event(), asyncio.Event()
    async def read():
        entered.set()
        await finish.wait()
    lamp = type("Lamp", (), {"read_current_state": AsyncMock(side_effect=read)})()
    data = {"all_lamps": {0: lamp}, "queue": asyncio.Queue()}
    bridge.queue_lamp_update(data, 0)
    # Patch only the bridge's settling delay; retain real asyncio primitives.
    real_sleep = asyncio.sleep
    async def settle(delay):
        await real_sleep(0)
    class AsyncioProxy:
        sleep = staticmethod(settle)
        def __getattr__(self, name):
            return getattr(asyncio, name)
    monkeypatch.setattr(bridge, "asyncio", AsyncioProxy())
    task = asyncio.create_task(bridge.process_queue(None, data))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        for _ in range(100):
            bridge.queue_lamp_update(data, 0)
        assert data["queue"].qsize() == 1
        assert data["pending_lamp_updates"] == {0}
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
