"""Replay tests for the pinned python-dali 0.11 capture integration."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import dali.address as address
import dali.gear.general as gear
from dali.driver.hid import tridonic
import pytest

from app import bridge
from app.bus_monitor import CAPTURE_ID, MAX_RECORDS, get_monitor
from app.dali_config import DaliConfig
from app.monitored_tridonic import MonitoredTridonic


def report(mode=0x11, rtype=0x73, value=0x0100, interval=0x1234, seq=7):
    return tridonic._resptmpl.pack(mode, rtype, value.to_bytes(4, "big"), interval, seq)


@pytest.mark.parametrize("raw", [
    report(), report(), report(rtype=0x72, value=0),
    report(mode=0x12), report(mode=1), report(rtype=0x77, value=3),
    report(rtype=0x71), report(rtype=0xFE), report(mode=0xFF),
    b"", b"\x11\x72", bytes(65),
])
def test_report_replay_delivers_identical_input_once(monkeypatch, raw):
    driver = MonitoredTridonic("unused")
    calls = []

    def original(self, data):
        assert self is driver
        assert data is raw
        # Capture must precede the library's processing.
        assert self._bus_monitor.snapshot()["records"][0]["raw_report_hex"] == raw.hex().upper()
        calls.append(data)

    monkeypatch.setattr(tridonic, "_handle_read", original)
    driver._handle_read(raw)
    assert calls == [raw]
    assert len(driver._bus_monitor.snapshot()["records"]) == 1


def test_mutable_input_is_copied_before_library_processing(monkeypatch):
    driver = MonitoredTridonic("unused")
    raw = bytearray(report())
    expected = bytes(raw).hex().upper()

    def original(self, data):
        assert data is raw
        data[:] = bytes(64)

    monkeypatch.setattr(tridonic, "_handle_read", original)
    driver._handle_read(raw)
    assert driver._bus_monitor.snapshot()["records"][0]["raw_report_hex"] == expected


def test_parser_failure_retains_raw_and_calls_original_once(monkeypatch):
    driver = MonitoredTridonic("unused")
    monitor = driver._bus_monitor
    raw = report()
    calls = []

    def fail(*args):
        raise RuntimeError("injected parser failure")

    monkeypatch.setattr(monitor, "_parse_report", fail)
    monkeypatch.setattr(tridonic, "_handle_read", lambda self, data: calls.append(data))
    driver._handle_read(raw)
    snap = monitor.snapshot()
    assert calls == [raw]
    assert snap["records"][0]["raw_report_hex"] == raw.hex().upper()
    assert snap["records"][0]["diagnostics"]["decode_error"] == "injected parser failure"
    assert snap["counters"]["decode_errors"] == 1
    assert snap["counters"]["capture_errors"] == 0


def test_capture_failure_does_not_block_original(monkeypatch):
    driver = MonitoredTridonic("unused")
    raw = report()
    calls = []

    def fail(*args):
        raise RuntimeError("injected capture failure")

    monkeypatch.setattr(driver._bus_monitor, "capture_report", fail)
    monkeypatch.setattr(tridonic, "_handle_read", lambda self, data: calls.append(data))
    driver._handle_read(raw)
    assert calls == [raw]
    assert driver._bus_monitor.snapshot()["counters"]["capture_errors"] == 1


def test_original_failure_propagates_without_retry(monkeypatch):
    driver = MonitoredTridonic("unused")
    calls = []

    def fail(self, data):
        calls.append(data)
        raise RuntimeError("original driver failure")

    monkeypatch.setattr(tridonic, "_handle_read", fail)
    raw = report()
    with pytest.raises(RuntimeError, match="original driver failure"):
        driver._handle_read(raw)
    assert calls == [raw]
    assert driver._bus_monitor.snapshot()["counters"]["capture_errors"] == 0


def test_adapter_fields_zero_reply_and_status_are_honest():
    monitor = MonitoredTridonic("unused")._bus_monitor
    for raw in [report(), report(rtype=0x72, value=0), report(rtype=0x71),
                report(rtype=0x77, value=3), report(rtype=0x74),
                report(rtype=0xFE), report(mode=0xFF), report(mode=1), b"\x11"]:
        monitor.capture_report(raw)
    records = monitor.snapshot()["records"]
    forward, backward, silence, error, unsupported, unknown, mode, init, malformed = records
    assert forward["adapter"]["interval"] == 0x1234
    assert forward["adapter"]["sequence"] == 7
    assert forward["adapter"]["mode_code"] == 0x11
    assert forward["adapter"]["type_code"] == 0x73
    assert forward["frame"] == {
        "direction": "forward", "bits": 16, "integer": 0x0100,
        "hex": "0100", "binary": "0000000100000000",
    }
    assert backward["classification"] == "backward frame"
    assert backward["frame"]["integer"] == 0
    assert backward["frame"]["hex"] == "00"
    assert all(r["frame"] is None for r in records[2:])
    assert silence["adapter"]["type_name"] == "no frame"
    assert error["diagnostics"]["adapter_status"] == "framing error"
    assert unsupported["adapter"]["frame_data_hex"] == "00000100"
    assert unsupported["diagnostics"]["unknown"]
    assert unknown["diagnostics"]["unknown"]
    assert mode["diagnostics"]["unknown"]
    assert init["adapter"]["type_code"] is None
    assert init["adapter"]["interval"] is None
    assert malformed["diagnostics"]["decode_error"]
    assert datetime.fromisoformat(forward["received_utc"]).utcoffset().total_seconds() == 0
    assert "." in forward["received_utc"]
    assert 0 <= forward["elapsed_seconds"] <= records[-1]["elapsed_seconds"]


def test_out_of_width_payload_does_not_invent_frame():
    monitor = MonitoredTridonic("unused")._bus_monitor
    monitor.capture_report(report(rtype=0x72, value=0x100))
    record = monitor.snapshot()["records"][0]
    assert record["frame"] is None
    assert record["diagnostics"]["decode_error"]


async def test_shared_capture_registers_once_and_survives_busy_reconnect(driver, busy):
    # Add the same callback-registration interface the real HID driver uses.
    callbacks = []
    driver.connection_status_callback = SimpleNamespace(register=lambda cb: callbacks.append(cb))
    first, second = DaliConfig(driver, busy), DaliConfig(driver, busy)
    monitor = first.monitor
    assert second.monitor is monitor
    assert len(driver.bus_traffic._cbs) == 1
    assert len(callbacks) == 1
    assert monitor.legacy_events() == []
    capture_id = monitor.capture_id
    async with first._guard():
        monitor.capture_report(report())
        driver.bus_traffic._cbs[0](driver, gear.Off(address.Short(0)), None, False)
    callbacks[0](driver, "disconnected")
    callbacks[0](driver, "connected")
    assert get_monitor(driver).capture_id == capture_id == CAPTURE_ID
    records = monitor.snapshot()["records"]
    assert len(records) == 6
    assert records[0]["diagnostics"]["configuration_busy"] is True
    assert records[1]["diagnostics"]["configuration_busy"] is True
    assert records[-1]["diagnostics"]["configuration_busy"] is False
    assert first.monitor_events() == second.monitor_events()
    assert first.recent_switch_addresses() == [0]
    assert monitor.snapshot()["connection"] == "connected"


def test_one_total_budget_burst_eviction_and_cursors(driver):
    monitor = get_monitor(driver)
    monitor.on_traffic(driver, gear.Off(address.Short(0)), None, False)
    # Mix record classes in one ring: an older transaction must be evicted too.
    raw = report()
    for _ in range(MAX_RECORDS + 1):
        monitor.capture_report(raw)
    snap = monitor.snapshot()
    assert len(snap["records"]) == MAX_RECORDS
    assert snap["oldest"] == 3
    assert snap["newest"] == MAX_RECORDS + 2
    assert snap["counters"]["evicted"] == 2
    assert monitor.legacy_events() == []
    assert [r["seq"] for r in snap["records"]] == list(range(3, MAX_RECORDS + 3))
    page = monitor.records_since(limit=2)
    assert page["history_gap"] and page["has_more"]
    assert [r["seq"] for r in page["records"]] == [3, 4]
    assert page["next"] == 4
    assert not monitor.records_since(since=4)["history_gap"]
    assert monitor.records_since(capture_id="previous-process")["capture_reset"]


def test_snapshots_and_pages_are_independent(driver):
    monitor = get_monitor(driver)
    monitor.capture_report(report())
    snap = monitor.snapshot()
    snap["records"][0]["adapter"]["sequence"] = -1
    snap["counters"]["evicted"] = -1
    page = monitor.records_since()
    assert page["records"][0]["adapter"]["sequence"] == 7
    assert page["counters"]["evicted"] == 0
    page["records"].clear()
    assert len(monitor.records_since()["records"]) == 1


def test_legacy_newest_400_and_sequence_remain_compatible(driver):
    monitor = get_monitor(driver)
    for _ in range(401):
        monitor.on_traffic(driver, gear.Off(address.Short(0)), None, False)
    events = monitor.legacy_events()
    assert len(events) == 400
    assert events[0]["seq"] == 2
    assert events[-1]["seq"] == 401
    assert set(events[0]) == {"seq", "t", "cmd", "type", "address", "resp", "err"}


def test_callback_failure_is_counted_without_escape(driver):
    monitor = get_monitor(driver)

    class BrokenCommand:
        def __str__(self):
            raise ValueError("cannot decode")

    monitor.on_traffic(driver, BrokenCommand(), None, False)
    assert monitor.snapshot()["counters"]["capture_errors"] == 1


def test_capabilities_are_explicit_for_other_drivers():
    hasseb = bridge.build_driver({"dali_driver": "hasseb", "dali_device": "unused"}, None)
    caps = hasseb._bus_monitor.capabilities
    assert caps["raw_reports"] is False
    assert caps["decoded_transactions"] is True
    assert caps["external_masters"] is False
    assert "cannot observe" in caps["notice"]
    object_with_attributes = SimpleNamespace()
    no_callbacks = get_monitor(object_with_attributes)
    assert object_with_attributes._bus_monitor is no_callbacks
    assert no_callbacks.capabilities["decoded_transactions"] is False
    assert no_callbacks.capabilities["connection_notifications"] is False
    assert no_callbacks.capabilities["external_masters"] is None


async def test_start_listening_installs_capture_before_connect(driver):
    driver.firmware_version, driver.serial = "test", "test"

    def connect():
        assert len(driver.bus_traffic._cbs) == 2
        monitor = driver._bus_monitor
        driver.bus_traffic._cbs[0](driver, gear.Off(address.Short(0)), None, False)
        assert monitor.legacy_events()[0]["address"] == 0

    driver.connect = connect
    await bridge.start_listening_on_dali({"driver": driver})
    assert driver._bus_monitor.busy is bridge.config_busy


async def test_real_callback_delivery_and_reconnect_preserve_history():
    driver = MonitoredTridonic("unused")
    monitor = driver._bus_monitor
    driver.bus_traffic._invoke(gear.Off(address.Short(0)), None, False)
    driver.connection_status_callback._invoke("disconnected")
    driver.connection_status_callback._invoke("connected")
    await asyncio.sleep(0)
    assert [r["classification"] for r in monitor.snapshot()["records"]] == [
        "transaction", "application diagnostic", "application diagnostic",
    ]
    assert len(driver.bus_traffic._callbacks) == 1
    assert len(driver.connection_status_callback._callbacks) == 1


def test_real_handler_preserves_bus_queue_and_outstanding_response_delivery():
    driver = MonitoredTridonic("unused")
    event = asyncio.Event()
    messages = []
    driver._outstanding[7] = (event, messages)
    repeated = report()
    backward_zero = report(mode=0x12, rtype=0x72, value=0, seq=7)
    unsolicited = report(mode=0x12, rtype=0x72, value=0xFF, seq=8)
    inputs = [repeated, repeated, backward_zero, unsolicited]
    for raw in inputs:
        driver._handle_read(raw)

    # The real pinned handler receives every report, including repeats and
    # unsolicited replies, and still routes only matching sequence responses.
    assert driver._bus_watch_data == inputs
    assert all(actual is raw for actual, raw in zip(driver._bus_watch_data, inputs))
    assert driver._bus_watch_data_available.is_set()
    assert event.is_set()
    assert messages == [backward_zero]
    assert messages[0] is backward_zero
    records = driver._bus_monitor.snapshot()["records"]
    assert len(records) == 4
    assert records[0]["raw_report_hex"] == records[1]["raw_report_hex"]
    assert records[2]["frame"]["integer"] == 0
    assert records[3]["frame"]["integer"] == 0xFF


def test_real_handler_malformed_input_error_preserves_capture():
    driver = MonitoredTridonic("unused")
    with pytest.raises(IndexError):
        driver._handle_read(b"")
    records = driver._bus_monitor.snapshot()["records"]
    assert len(records) == 1
    assert records[0]["raw_report_hex"] == ""
    assert records[0]["diagnostics"]["decode_error"]
    assert driver._bus_watch_data == []
