"""Exercise the pinned USB send loop with silent and responding adapters."""
import asyncio
import os

import dali.address as address
import dali.gear.general as gear
from dali.driver.hid import tridonic
from dali.exceptions import CommunicationError
import pytest

from app.monitored_tridonic import MonitoredTridonic


@pytest.fixture
def usb_driver(monkeypatch):
    driver = MonitoredTridonic("unused")
    driver.response_timeout = 0.02
    driver.connected.set()
    driver._f = 123
    monkeypatch.setattr(os, "write", lambda fd, data: len(data))
    driver.reconnects = []
    def disconnect(reconnect=False):
        driver.reconnects.append(reconnect)
        driver._shutdown_device()
        driver.connected.clear()
    monkeypatch.setattr(driver, "disconnect", disconnect)
    return driver


async def reply(driver, command):
    while not driver._outstanding:
        await asyncio.sleep(0)
    seq = next(iter(driver._outstanding))
    driver._handle_read(tridonic._resptmpl.pack(0x12, 0x73, command.frame.pack_len(4), 0, seq))
    driver._handle_read(tridonic._resptmpl.pack(0x12, 0x72, (100).to_bytes(4, "big"), 0, seq))


async def test_silent_adapter_releases_bus_clears_pending_and_can_recover(usb_driver):
    command = gear.QueryActualLevel(address.Short(0))
    with pytest.raises(CommunicationError, match="timed out"):
        await asyncio.wait_for(usb_driver.send(command), 1)
    assert usb_driver.reconnects == [True]
    assert not usb_driver.transaction_lock.locked()
    assert usb_driver._outstanding == {}
    assert usb_driver._command_semaphore._value == 2
    # Simulate the connection returning and deliver real adapter report bytes.
    usb_driver.connected.set()
    response_task = asyncio.create_task(reply(usb_driver, command))
    result = await usb_driver.send(command)
    await response_task
    assert result.value == 100
    assert usb_driver._outstanding == {}


async def test_cancelled_send_cleans_up_outstanding_sequences(usb_driver):
    command = gear.QueryActualLevel(address.Short(0))
    task = asyncio.create_task(usb_driver.send(command))
    while not usb_driver._outstanding:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert usb_driver.reconnects == [True]
    assert usb_driver._outstanding == {}
    assert not usb_driver.transaction_lock.locked()
    assert usb_driver._command_semaphore._value == 2


async def test_success_keeps_connection_and_report_capture(usb_driver):
    command = gear.QueryActualLevel(address.Short(0))
    response_task = asyncio.create_task(reply(usb_driver, command))
    result = await usb_driver.send(command)
    await response_task
    assert result.value == 100
    assert usb_driver.reconnects == []
    assert usb_driver.connected.is_set()
    assert len(usb_driver._bus_monitor.snapshot()["records"]) >= 2
