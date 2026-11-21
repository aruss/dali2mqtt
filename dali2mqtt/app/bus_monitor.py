"""Bounded, process-local bus capture shared by all users of a driver.

Capture callbacks run on the driver's event-loop thread. They perform no I/O;
snapshots copy retained data before a caller serializes or otherwise uses it.
Raw adapter reports and library transaction summaries are separate records.
"""
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
import time
from uuid import uuid4

import dali.address as address
from dali.command import Command
from dali.device.general import _Event, AmbiguousInstanceType, UnknownDeviceCommand, UnknownEvent
from dali.frame import ForwardFrame
from dali.gear.general import EnableDeviceType, UnknownGearCommand


SCHEMA_VERSION = 1
CAPTURE_ID = str(uuid4())
MAX_RECORDS = 10_000

_MODES = {0x01: "initialization", 0x11: "observe", 0x12: "response"}
_TYPES = {
    0x71: "no frame", 0x72: "DALI8", 0x73: "DALI16",
    0x74: "eDALI25", 0x75: "DSI", 0x76: "DALI24",
    0x77: "adapter information", 0x78: "HELVAR17",
}
_STATUS = {
    1: "checksum", 2: "bus shorted", 3: "framing error", 4: "bus OK",
    5: "DSI mode", 6: "DALI mode",
}


class BusMonitor:
    """A single 10,000-record budget for reports, transactions and diagnostics."""

    def __init__(self, driver, driver_name=None, raw_reports=False, busy=None):
        self.driver = driver
        self.busy = busy
        self.capture_id = CAPTURE_ID
        self._started = time.monotonic()
        self._records = deque(maxlen=MAX_RECORDS)
        self._seq = 0
        self._legacy_seq = 0
        self._device_type = 0
        self._repeat_context = None
        self._operation_seq = 0
        self.operation = None
        self._traffic_handle = None
        self._connection_handle = None
        self.counters = {"evicted": 0, "capture_errors": 0, "decode_errors": 0}
        name = driver_name or type(driver).__name__
        self.capabilities = {
            "driver": name,
            "raw_reports": raw_reports,
            "decoded_transactions": False,
            "connection_notifications": False,
            "external_masters": True if raw_reports else (False if name == "hasseb" else None),
            "notice": (
                "Captures reports delivered by Tridonic USB, not electrical waveforms. "
                "Host receipt times are not wire timestamps. Adapter origin hints cannot "
                "prove sender ownership: Tridonic firmware may misclassify observed "
                "frames matching the last transmitted frame. Gear destinations do not "
                "identify the sender."
                if raw_reports else
                "Decoded-only monitoring; individual adapter reports are unavailable. "
                + ("hasseb cannot observe frames sent by external masters."
                   if name == "hasseb" else "External-master visibility is driver dependent.")
            ),
        }
        self.connection = None
        self._register_callbacks()

    def _register_callbacks(self):
        traffic = getattr(self.driver, "bus_traffic", None)
        if traffic is not None:
            try:
                self._traffic_handle = traffic.register(self.on_traffic)
                self.capabilities["decoded_transactions"] = True
            except Exception:
                self.note_capture_error()
        connection = getattr(self.driver, "connection_status_callback", None)
        if connection is not None:
            try:
                self._connection_handle = connection.register(self.on_connection)
                self.capabilities["connection_notifications"] = True
            except Exception:
                self.note_capture_error()

    def note_capture_error(self):
        """Count failures without logging or recursively attempting more capture."""
        self.counters["capture_errors"] += 1

    def _record(self, classification):
        self._seq += 1
        return {
            "schema_version": SCHEMA_VERSION,
            "capture_id": self.capture_id,
            "seq": self._seq,
            "received_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "elapsed_seconds": time.monotonic() - self._started,
            "classification": classification,
            "raw_report_hex": None,
            "adapter": {
                "mode_code": None, "mode_name": None,
                "type_code": None, "type_name": None,
                "sequence": None, "interval": None, "frame_data_hex": None,
                "origin_hint": None,
            },
            "frame": None,
            "decoded": {
                "command_type": None, "command_text": None,
                "destination": None, "source": None,
                "instance_number": None, "instance_type": None,
                "device_group": None, "instance_group": None,
                "event_information": None,
                "device_type_context": None, "is_query": None,
                "destination_instance": None,
            },
            "reply": {"text": None, "raw": None, "state": None},
            "diagnostics": {
                "unknown": False, "ambiguous": False, "decode_error": None,
                "capture_error": None, "adapter_status_code": None,
                "adapter_status": None, "configuration_repeat_validation_failed": None,
                "configuration_busy": self.busy.is_set() if self.busy is not None else None,
                "capture_errors": self.counters["capture_errors"],
                "decode_errors": self.counters["decode_errors"],
                "comments": [],
            },
            "configuration": deepcopy(self.operation),
            "application": None,
            "legacy": None,
        }

    def _append(self, record):
        if len(self._records) == MAX_RECORDS:
            self.counters["evicted"] += 1
        self._records.append(record)

    def capture_report(self, data):
        """Retain exact bytes before parsing; malformed reports remain observable."""
        raw = bytes(data)
        record = self._record("adapter status")
        record["raw_report_hex"] = raw.hex().upper()
        self._append(record)
        try:
            self._parse_report(record, raw)
            if record["frame"] is None or record["frame"]["direction"] != "forward":
                self._repeat_context = None
            if record["frame"] is not None:
                if record["frame"]["direction"] == "forward":
                    self._decode_forward(record)
                else:
                    record["decoded"]["command_text"] = (
                        f"Backward response 0x{record['frame']['hex']} (unassociated)"
                    )
            elif record["adapter"]["mode_code"] == 0x01:
                record["decoded"]["command_text"] = "Adapter initialization report"
            elif record["adapter"]["mode_code"] in (0x11, 0x12) and record["adapter"]["type_code"] == 0x71:
                record["decoded"]["command_text"] = "No response (adapter reported no frame)"
            elif record["diagnostics"]["adapter_status"] is not None:
                record["decoded"]["command_text"] = "Adapter " + record["diagnostics"]["adapter_status"]
                if record["diagnostics"]["adapter_status_code"] == 3:
                    record["diagnostics"]["comments"].append("Framing-error notification has no valid DALI frame payload.")
            else:
                record["decoded"]["command_text"] = "Unknown or unsupported adapter report"
        except Exception as err:
            self._device_type = 0
            self._repeat_context = None
            self.counters["decode_errors"] += 1
            record["diagnostics"]["decode_error"] = str(err)
            record["diagnostics"]["decode_errors"] = self.counters["decode_errors"]
            record["diagnostics"]["comments"].append("Decode failed; raw report preserved.")

    def _decode_forward(self, record):
        frame = record["frame"]
        raw_frame = ForwardFrame(frame["bits"], frame["integer"])
        device_type = self._device_type
        repeat = self._repeat_context
        self._repeat_context = None
        matching_repeat = (
            repeat is not None and repeat[:2] == (frame["bits"], frame["integer"])
            and 0 <= record["elapsed_seconds"] - repeat[3] <= 0.2
        )
        if matching_repeat:
            device_type = repeat[2]
        # Match the pinned driver's observed EnableDeviceType context: it
        # applies to the next forward command, never to callback timing.
        self._device_type = 0
        record["decoded"]["device_type_context"] = device_type
        if len(raw_frame) == 24 and not raw_frame[16]:
            record["classification"] = "input event"
            self._event_identity(record, raw_frame)
        command = Command.from_frame(
            raw_frame, devicetype=device_type,
            dev_inst_map=getattr(self.driver, "dev_inst_map", None),
        )
        self._decode_command(record, command)
        if isinstance(command, EnableDeviceType):
            self._device_type = command.param
        elif getattr(command, "sendtwice", False) and not matching_repeat:
            # The library validates configuration repeats before decoding them.
            # Retain context for that one adjacent repeat, without merging rows
            # or linking any reply to either raw report. The timeout is host
            # elapsed time, not a measurement of DALI wire timing.
            self._repeat_context = (frame["bits"], frame["integer"], device_type, record["elapsed_seconds"])

    @staticmethod
    def _event_identity(record, frame):
        """Retain protocol-supplied event identity even if semantic decode fails."""
        decoded = record["decoded"]
        decoded["event_information"] = frame[9:0]
        if not frame[23]:
            decoded["source"] = {"namespace": "device", "kind": "short", "address": frame[22:17]}
            if frame[15]:
                decoded["instance_number"] = frame[14:10]
            else:
                decoded["instance_type"] = frame[14:10]
        elif not frame[22]:
            if frame[15]:
                decoded["instance_type"] = frame[21:17]
                decoded["instance_number"] = frame[14:10]
            else:
                decoded["device_group"] = frame[21:17]
                decoded["instance_type"] = frame[14:10]
                decoded["source"] = {"namespace": "device", "kind": "device_group", "address": frame[21:17]}
        elif not frame[15]:
            decoded["instance_group"] = frame[21:17]
            decoded["instance_type"] = frame[14:10]
            decoded["source"] = {"namespace": "device", "kind": "instance_group", "address": frame[21:17]}

    @staticmethod
    def _destination(dest):
        if isinstance(dest, address.GearAddress):
            namespace = "gear"
        elif isinstance(dest, address.DeviceAddress):
            namespace = "device"
        else:
            return None
        if isinstance(dest, (address.GearShort, address.DeviceShort)):
            kind, value = "short", dest.address
        elif isinstance(dest, (address.GearGroup, address.DeviceGroup)):
            kind, value = "group", dest.group
        elif isinstance(dest, (address.GearBroadcastUnaddressed, address.DeviceBroadcastUnaddressed)):
            kind, value = "broadcast_unaddressed", None
        else:
            kind, value = "broadcast", None
        return {"namespace": namespace, "kind": kind, "address": value}

    def _decode_command(self, record, command):
        decoded = record["decoded"]
        diagnostics = record["diagnostics"]
        decoded["command_type"] = type(command).__name__ if command is not None else None
        decoded["command_text"] = str(command) if command is not None else "Unknown forward frame"
        decoded["is_query"] = bool(getattr(command, "is_query", False))
        diagnostics["unknown"] = command is None or type(command) is Command or isinstance(
            command, (UnknownEvent, UnknownGearCommand, UnknownDeviceCommand)
        )
        diagnostics["ambiguous"] = isinstance(command, AmbiguousInstanceType)
        if isinstance(command, _Event):
            self._event_identity(record, command.frame)
            decoded["instance_type"] = command.instance_type
            if diagnostics["ambiguous"]:
                diagnostics["comments"].append("Instance type mapping unavailable; input event remains ambiguous.")
        else:
            destination = getattr(command, "destination", None)
            if destination is None and command is not None:
                destination = address.Address.from_frame(command.frame)
            decoded["destination"] = self._destination(destination)
            instance = getattr(command, "instance", None)
            if instance is not None:
                decoded["destination_instance"] = {
                    "kind": type(instance).__name__, "value": instance.value,
                }
        if diagnostics["unknown"]:
            diagnostics["comments"].append("Unknown or unsupported command/event; raw data preserved.")

    @staticmethod
    def _parse_report(record, raw):
        adapter = record["adapter"]
        diagnostics = record["diagnostics"]
        # Initialization reports use a different payload layout. Preserve those
        # bytes without interpreting firmware/serial data as a DALI frame.
        if raw:
            adapter["mode_code"] = raw[0]
            adapter["mode_name"] = _MODES.get(raw[0], "unknown")
        if len(raw) != 64:
            raise ValueError(f"Malformed USB report: expected 64 bytes, received {len(raw)}")
        if raw[0] == 0x01:
            return
        adapter.update({
            "type_code": raw[1], "type_name": _TYPES.get(raw[1], "unknown"),
            "frame_data_hex": raw[2:6].hex().upper(),
            "interval": int.from_bytes(raw[6:8], "big"), "sequence": raw[8],
        })
        if raw[0] not in (0x11, 0x12):
            diagnostics["unknown"] = True
            return
        adapter["origin_hint"] = (
            "adapter-reported observed traffic" if raw[0] == 0x11 else
            "adapter-reported local transmission/response; firmware may misclassify external traffic"
        )
        if raw[1] == 0x77:
            diagnostics["adapter_status_code"] = raw[5]
            diagnostics["adapter_status"] = _STATUS.get(raw[5], "unknown status")
            diagnostics["unknown"] = raw[5] not in _STATUS
        elif raw[1] in (0x72, 0x73, 0x76):
            bits = {0x72: 8, 0x73: 16, 0x76: 24}[raw[1]]
            value = int.from_bytes(raw[2:6], "big")
            if value >= 1 << bits:
                raise ValueError("Frame payload exceeds reported DALI width")
            direction = "backward" if bits == 8 else "forward"
            record["classification"] = f"{direction} frame"
            record["frame"] = {
                "direction": direction, "bits": bits,
                "hex": f"{value:0{bits // 4}X}", "integer": value,
                "binary": f"{value:0{bits}b}",
            }
        elif raw[1] != 0x71:
            # Unsupported transport types stay adapter reports. Their four-byte
            # payload is preserved above without claiming valid DALI bytes.
            diagnostics["unknown"] = True

    def on_traffic(self, dev, command, response, config_command_error):
        """Preserve the legacy callback contract as a distinct transaction."""
        try:
            record = self._record("transaction")
            self._append(record)
            self._decode_reply(record, command, response)
            self._legacy_seq += 1
            dest = getattr(command, "destination", None)
            legacy = {
                "seq": self._legacy_seq, "t": time.strftime("%H:%M:%S"),
                "cmd": str(command) if command is not None else "",
                "type": type(command).__name__ if command is not None else "",
                "address": dest.address if isinstance(dest, address.Short) else None,
                "resp": record["reply"]["text"] if response is not None else "",
                "err": bool(config_command_error),
            }
            record["legacy"] = legacy
            self._decode_command(record, command)
            record["diagnostics"]["configuration_repeat_validation_failed"] = bool(config_command_error)
            if config_command_error:
                record["diagnostics"]["comments"].append("configuration repeat validation failed")
        except Exception as err:
            self.note_capture_error()
            if "record" in locals():
                record["diagnostics"]["decode_error"] = str(err)
                record["diagnostics"]["capture_errors"] = self.counters["capture_errors"]

    def _decode_reply(self, record, command, response):
        raw_reply = getattr(response, "raw_value", None)
        if raw_reply is not None:
            error = bool(getattr(raw_reply, "error", False))
            value = raw_reply.as_integer
            record["reply"]["raw"] = {
                "bits": None if error else 8,
                "hex": None if error else f"{value:02X}",
                "integer": None if error else value,
                "binary": None if error else f"{value:08b}",
                "error": error, "library_value": value if error else None,
            }
            record["reply"]["state"] = "framing error" if error else "received"
            if error:
                record["diagnostics"]["comments"].append(
                    "Library error response value is not a valid captured backward frame."
                )
        else:
            record["reply"]["state"] = "no response" if getattr(command, "is_query", False) else "not expected"
        if response is not None:
            try:
                record["reply"]["text"] = str(response)
            except Exception as err:
                record["reply"]["text"] = f"Response decode error: {type(err).__name__}: {err}"
                self.counters["decode_errors"] += 1
                record["diagnostics"]["decode_error"] = record["reply"]["text"]
                record["diagnostics"]["decode_errors"] = self.counters["decode_errors"]

    def start_operation(self, name, **parameters):
        """Mark the guarded application's operation, without claiming bus origin."""
        self._operation_seq += 1
        self.operation = {"id": self._operation_seq, "name": name, "parameters": parameters}
        self._operation_record("start")

    def finish_operation(self, error=None):
        self._operation_record("failure" if error is not None else "completion", error)
        self.operation = None

    def _operation_record(self, phase, error=None):
        try:
            record = self._record("application diagnostic")
            record["application"] = {
                "event": "configuration operation", "name": self.operation["name"],
                "phase": phase, "error": error,
            }
            record["diagnostics"]["comments"].append(
                "Configuration context describes application activity, not sender ownership."
            )
            self._append(record)
        except Exception:
            self.note_capture_error()

    def on_connection(self, dev, status):
        try:
            self.connection = status
            self._device_type = 0
            self._repeat_context = None
            record = self._record("application diagnostic")
            record["application"] = {"event": "connection", "status": status}
            self._append(record)
        except Exception:
            self.note_capture_error()

    def legacy_events(self, since=0):
        events = [r["legacy"] for r in self._records if r["legacy"] is not None]
        return deepcopy([e for e in events[-400:] if e["seq"] > since])

    def snapshot(self):
        """Return an independent, consistent view of retained capture and metadata."""
        return {
            "schema_version": SCHEMA_VERSION, "capture_id": self.capture_id,
            "oldest": self._records[0]["seq"] if self._records else None,
            "newest": self._records[-1]["seq"] if self._records else None,
            "capabilities": deepcopy(self.capabilities),
            "counters": dict(self.counters), "connection": self.connection,
            "records": deepcopy(list(self._records)),
        }

    def records_since(self, since=0, limit=500, capture_id=None):
        """Read a bounded cursor page, reporting restart and eviction gaps."""
        if type(since) is not int or since < 0:
            raise ValueError("since must be a nonnegative integer")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("limit must be an integer from 1 to 500")
        reset = capture_id is not None and capture_id != self.capture_id
        if reset:
            since = 0
        oldest = self._records[0]["seq"] if self._records else None
        newest = self._records[-1]["seq"] if self._records else None
        selected = []
        for record in self._records:
            if record["seq"] > since:
                selected.append(record)
                if len(selected) == limit:
                    break
        next_cursor = selected[-1]["seq"] if selected else since
        return {
            "schema_version": SCHEMA_VERSION, "capture_id": self.capture_id,
            "records": deepcopy(selected), "next": next_cursor,
            "oldest": oldest, "newest": newest,
            "has_more": newest is not None and next_cursor < newest,
            "capture_reset": reset,
            "history_gap": oldest is not None and since < oldest - 1,
            "capabilities": deepcopy(self.capabilities),
            "counters": dict(self.counters), "connection": self.connection,
        }


def get_monitor(driver, *, driver_name=None, raw_reports=False, busy=None):
    """Attach capture once, before connection; reconnects retain identity/history."""
    monitor = getattr(driver, "_bus_monitor", None)
    if monitor is None:
        monitor = BusMonitor(driver, driver_name=driver_name, raw_reports=raw_reports, busy=busy)
        driver._bus_monitor = monitor
    elif busy is not None:
        monitor.busy = busy
    return monitor
