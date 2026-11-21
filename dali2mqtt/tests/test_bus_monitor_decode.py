"""Semantic capture decodes and application operation diagnostics."""
import asyncio

import dali.address as address
from dali.command import Command, NumericResponse
from dali.device import general as device
from dali.device.helpers import DeviceInstanceTypeMapper
from dali.frame import BackwardFrame, BackwardFrameError
from dali.gear import colour, general as gear
import pytest

from app.bus_monitor import get_monitor
from app.dali_config import DaliConfig
from app.monitored_tridonic import MonitoredTridonic
from test_bus_monitor import report


def capture_command(monitor, command):
    frame = command.frame
    monitor.capture_report(report(rtype=0x73 if len(frame) == 16 else 0x76, value=frame.as_integer))
    return monitor.records_since(since=monitor._seq - 1)["records"][0]


def test_dt8_context_applies_to_next_observed_forward_only():
    monitor = MonitoredTridonic("unused")._bus_monitor
    capture_command(monitor, gear.EnableDeviceType(8))
    dt8 = capture_command(monitor, colour.QueryColourValue(address.GearShort(0)))
    assert dt8["decoded"]["command_type"] == "QueryColourValue"
    assert dt8["decoded"]["device_type_context"] == 8
    assert dt8["decoded"]["is_query"] is True
    next_frame = capture_command(monitor, gear.Off(address.GearShort(0)))
    assert next_frame["decoded"]["device_type_context"] == 0
    capture_command(monitor, gear.EnableDeviceType(8))
    monitor.on_connection(None, "disconnected")
    assert capture_command(monitor, gear.Off(address.GearShort(0)))["decoded"]["device_type_context"] == 0


def test_dt8_configuration_repeat_keeps_context_once_and_preserves_rows():
    monitor = MonitoredTridonic("unused")._bus_monitor
    capture_command(monitor, gear.EnableDeviceType(8))
    command = colour.StoreGearFeaturesStatus(address.GearShort(0))
    first = capture_command(monitor, command)
    second = capture_command(monitor, command)
    third = capture_command(monitor, command)
    assert first["decoded"]["command_type"] == second["decoded"]["command_type"] == "StoreGearFeaturesStatus"
    assert first["decoded"]["device_type_context"] == second["decoded"]["device_type_context"] == 8
    assert first["raw_report_hex"] == second["raw_report_hex"]
    assert first["seq"] + 1 == second["seq"]
    assert third["decoded"]["device_type_context"] == 0
    assert third["diagnostics"]["unknown"] is True


@pytest.mark.parametrize("interruption", ["forward", "backward", "status", "timeout", "reconnect"])
def test_dt8_repeat_context_does_not_survive_unrelated_traffic_or_timeout(interruption):
    monitor = MonitoredTridonic("unused")._bus_monitor
    capture_command(monitor, gear.EnableDeviceType(8))
    command = colour.StoreGearFeaturesStatus(address.GearShort(0))
    capture_command(monitor, command)
    if interruption == "forward":
        capture_command(monitor, gear.Off(address.Short(0)))
    elif interruption == "backward":
        monitor.capture_report(report(rtype=0x72, value=0))
    elif interruption == "status":
        monitor.capture_report(report(rtype=0x77, value=4))
    elif interruption == "timeout":
        bits, value, device_type, elapsed = monitor._repeat_context
        monitor._repeat_context = (bits, value, device_type, elapsed - 1)
    else:
        monitor.on_connection(None, "disconnected")
    assert capture_command(monitor, command)["decoded"]["device_type_context"] == 0


@pytest.mark.parametrize("bits,value,expected,namespace", [
    (16, 0x01F0, "UnknownGearCommand", "gear"),
    (24, 0x01FEFF, "UnknownDeviceCommand", "device"),
])
def test_unknown_library_command_classes_are_flagged_and_keep_destination(bits, value, expected, namespace):
    monitor = MonitoredTridonic("unused")._bus_monitor
    monitor.capture_report(report(rtype=0x73 if bits == 16 else 0x76, value=value))
    record = monitor.snapshot()["records"][0]
    assert record["diagnostics"]["unknown"] is True
    assert record["decoded"]["command_type"] == expected
    assert record["decoded"]["destination"] == {"namespace": namespace, "kind": "short", "address": 0}


@pytest.mark.parametrize("command,namespace,kind,value", [
    (gear.Off(address.GearShort(0)), "gear", "short", 0),
    (gear.Off(address.GearGroup(0)), "gear", "group", 0),
    (gear.Off(address.GearBroadcast()), "gear", "broadcast", None),
    (gear.Off(address.GearBroadcastUnaddressed()), "gear", "broadcast_unaddressed", None),
    (device.QueryDeviceStatus(address.DeviceShort(0)), "device", "short", 0),
    (device.QueryDeviceStatus(address.DeviceGroup(0)), "device", "group", 0),
    (device.QueryDeviceStatus(address.DeviceBroadcast()), "device", "broadcast", None),
])
def test_destinations_preserve_namespaces_and_zero(command, namespace, kind, value):
    monitor = MonitoredTridonic("unused")._bus_monitor
    record = capture_command(monitor, command)
    assert record["decoded"]["destination"] == {"namespace": namespace, "kind": kind, "address": value}
    assert record["decoded"]["source"] is None


@pytest.mark.parametrize("instance_type,info,expected,ambiguous,unknown", [
    (1, 0, "ButtonReleased", False, False),
    (1, 1, "ButtonPressed", False, False),
    (0, 6, "UnknownEvent", False, True),
    (None, 5, "AmbiguousInstanceType", True, False),
    (1, 1023, "UnknownEvent", False, True),
])
def test_event_mapper_known_unknown_and_ambiguous_keep_raw_info(instance_type, info, expected, ambiguous, unknown):
    mapper = DeviceInstanceTypeMapper()
    if instance_type is not None:
        mapper.add_type(short_address=0, instance_number=0, instance_type=instance_type)
    driver = MonitoredTridonic("unused", dev_inst_map=mapper)
    monitor = driver._bus_monitor
    raw = 0x8000 | info
    monitor.capture_report(report(rtype=0x76, value=raw))
    record = monitor.snapshot()["records"][0]
    assert record["classification"] == "input event"
    assert record["decoded"]["command_type"] == expected
    assert record["decoded"]["source"] == {"namespace": "device", "kind": "short", "address": 0}
    assert record["decoded"]["destination"] is None
    assert record["decoded"]["instance_number"] == 0
    assert record["decoded"]["instance_type"] == instance_type
    assert record["decoded"]["event_information"] == info
    assert record["diagnostics"]["ambiguous"] is ambiguous
    assert record["diagnostics"]["unknown"] is unknown
    # In-place changes to M02's shared mapper must be visible on the next report.
    mapper.add_type(short_address=0, instance_number=0, instance_type=1)
    monitor.capture_report(report(rtype=0x76, value=0x8000))
    assert monitor.records_since(since=1)["records"][0]["decoded"]["command_type"] == "ButtonReleased"


@pytest.mark.parametrize("raw,group,kind", [
    (0x800000 | (1 << 10), "device_group", "device_group"),
    (0xC00000 | (1 << 10), "instance_group", "instance_group"),
])
def test_group_event_zero_identity(raw, group, kind):
    monitor = MonitoredTridonic("unused")._bus_monitor
    monitor.capture_report(report(rtype=0x76, value=raw))
    record = monitor.snapshot()["records"][0]
    assert record["decoded"][group] == 0
    assert record["decoded"]["source"] == {"namespace": "device", "kind": kind, "address": 0}
    assert record["decoded"]["event_information"] == 0
    assert record["decoded"]["instance_type"] == 1


def test_device_command_instance_zero_is_destination():
    monitor = MonitoredTridonic("unused")._bus_monitor
    cmd = device.QueryInstanceType(address.DeviceShort(0), address.InstanceNumber(0))
    record = capture_command(monitor, cmd)
    assert record["decoded"]["destination_instance"] == {"kind": "InstanceNumber", "value": 0}
    assert record["decoded"]["source"] is None


def test_semantic_decode_failure_keeps_frame_and_event_identity(monkeypatch):
    monitor = MonitoredTridonic("unused")._bus_monitor

    def fail(*args, **kwargs):
        raise RuntimeError("semantic decode failed")

    monkeypatch.setattr(Command, "from_frame", fail)
    monitor.capture_report(report(rtype=0x76, value=0x8000))
    record = monitor.snapshot()["records"][0]
    assert record["raw_report_hex"]
    assert record["frame"]["integer"] == 0x8000
    assert record["decoded"]["source"]["address"] == 0
    assert record["decoded"]["instance_number"] == 0
    assert record["decoded"]["event_information"] == 0
    assert record["diagnostics"]["decode_error"] == "semantic decode failed"
    assert monitor.counters["decode_errors"] == 1


@pytest.mark.parametrize("reply,state,integer,hex_value", [
    (NumericResponse(BackwardFrame(0)), "received", 0, "00"),
    (NumericResponse(None), "no response", None, None),
    (NumericResponse(BackwardFrameError(255)), "framing error", None, None),
])
def test_transactions_keep_reply_bytes_and_do_not_fabricate_error_frame(driver, reply, state, integer, hex_value):
    monitor = get_monitor(driver)
    monitor.on_traffic(driver, gear.QueryActualLevel(address.Short(0)), reply, True)
    record = monitor.snapshot()["records"][0]
    assert record["classification"] == "transaction"
    assert record["raw_report_hex"] is None
    assert record["reply"]["text"] == str(reply)
    assert record["reply"]["state"] == state
    if state == "no response":
        assert record["reply"]["raw"] is None
    else:
        assert record["reply"]["raw"]["integer"] == integer
        assert record["reply"]["raw"]["hex"] == hex_value
    if state == "framing error":
        assert record["reply"]["raw"]["error"] is True
        assert record["reply"]["raw"]["library_value"] == 255
    assert "configuration repeat validation failed" in record["diagnostics"]["comments"]
    assert record["legacy"]["err"] is True


def test_response_text_failure_preserves_available_raw_reply(driver):
    class BrokenResponse(NumericResponse):
        def __str__(self):
            raise ValueError("unsupported response value")

    monitor = get_monitor(driver)
    monitor.on_traffic(driver, gear.QueryActualLevel(address.Short(0)), BrokenResponse(BackwardFrame(0)), False)
    record = monitor.snapshot()["records"][0]
    assert record["reply"]["raw"]["integer"] == 0
    assert record["reply"]["state"] == "received"
    assert "Response decode error" in record["reply"]["text"]
    assert record["diagnostics"]["decode_error"]
    assert monitor.counters["decode_errors"] == 1


async def test_named_config_operation_context_and_failure(driver, busy):
    cfg = DaliConfig(driver, busy)

    def respond(command):
        cfg.monitor.capture_report(report())
        raise OSError("USB disconnected")

    driver._responder = respond
    with pytest.raises(OSError, match="USB disconnected"):
        await cfg.set_level(0, 100)
    records = cfg.monitor.snapshot()["records"]
    assert records[0]["application"]["phase"] == "start"
    assert records[-1]["application"] == {
        "event": "configuration operation", "name": "set_level", "phase": "failure", "error": "OSError: USB disconnected",
    }
    assert records[1]["configuration"]["name"] == "set_level"
    assert records[1]["diagnostics"]["configuration_busy"] is True
    assert cfg.monitor.operation is None and not busy.is_set()


async def test_named_config_completion_and_cancelled_guard(driver, busy):
    cfg = DaliConfig(driver, busy)
    await cfg.set_level(0, 0)
    assert cfg.monitor.snapshot()["records"][-1]["application"]["phase"] == "completion"
    with pytest.raises(asyncio.CancelledError):
        async with cfg._guard("cancelled_test"):
            raise asyncio.CancelledError()
    assert cfg.monitor.snapshot()["records"][-1]["application"]["phase"] == "failure"
    assert cfg.monitor.operation is None and not busy.is_set()


async def test_gear_verification_failure_remains_return_value_and_is_diagnosed(driver):
    cfg = DaliConfig(driver)
    assert await cfg.change_address(0, 1) == {"ok": True, "verified": False}
    assert cfg.monitor.snapshot()["records"][-1]["application"]["phase"] == "failure"
