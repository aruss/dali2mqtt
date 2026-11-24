"""Control-device scan validation with real python-dali response wrappers."""
import pytest

from dali.exceptions import CommunicationError
from dali.frame import BackwardFrame, BackwardFrameError

from app.dali_config import DaliConfig
from conftest import RawResp, cname, dest_addr
from test_webserver import _client


def reply(cmd, frame):
    return cmd.response(frame)


@pytest.mark.parametrize("frame", [BackwardFrame(0), BackwardFrame(32), None,
                                  BackwardFrameError(0), BackwardFrameError(255),
                                  "literal_none", "undecodable"])
async def test_status_validation(driver, caplog, frame):
    def respond(cmd):
        if cname(cmd) == "QueryDeviceStatus":
            if frame == "literal_none":
                return None
            if frame == "undecodable":
                return RawResp(value="invalid", raw_int=32)
            return reply(cmd, frame)
        return reply(cmd, BackwardFrame(2))
    driver._responder = respond
    result = await DaliConfig(driver).scan_devices([0])
    valid = isinstance(frame, BackwardFrame) and not frame.error
    if valid:
        assert result == [{"address": 0, "status": frame.as_integer,
                           "instances": 2, "operating_mode": 2}]
        assert len(driver.sent) == 3
    else:
        assert result == []
        assert driver.sent_types() == ["QueryDeviceStatus"]
    invalid = frame is not None and frame != "literal_none" and not valid
    assert ("D0 QueryDeviceStatus" in caplog.text) == invalid


@pytest.mark.parametrize("query,field", [("QueryNumberOfInstances", "instances"),
                                        ("QueryOperatingMode", "operating_mode")])
@pytest.mark.parametrize("frame", [None, BackwardFrameError(0), BackwardFrameError(255), "undecodable"])
async def test_metadata_validation(driver, caplog, query, field, frame):
    def respond(cmd):
        if cname(cmd) == query and frame == "undecodable":
            return RawResp(value="invalid", raw_int=32)
        return reply(cmd, frame if cname(cmd) == query else BackwardFrame(0))
    driver._responder = respond
    result = await DaliConfig(driver).scan_devices([0])
    assert result[0][field] is None
    assert result[0]["status"] == 0
    assert len(driver.sent) == 3
    assert (f"D0 {query}" in caplog.text) == (frame is not None)


async def test_mixed_and_repeated_scans(driver):
    present = True
    def respond(cmd):
        if cname(cmd) == "QueryDeviceStatus":
            frame = (BackwardFrame(0) if present else None) if dest_addr(cmd) == 0 else BackwardFrameError(255)
        else:
            frame = BackwardFrame(2)
        return reply(cmd, frame)
    driver._responder = respond
    cfg = DaliConfig(driver)
    for present, expected in [(True, [0]), (False, []), (True, [0])]:
        assert [d["address"] for d in await cfg.scan_devices([0, 1])] == expected


@pytest.mark.parametrize("query", ["QueryDeviceStatus", "QueryNumberOfInstances", "QueryOperatingMode"])
async def test_transport_failure_aborts_and_releases_guard(driver, busy, query):
    def respond(cmd):
        assert busy.is_set()
        if dest_addr(cmd) == 1 and cname(cmd) == query:
            raise CommunicationError("adapter unavailable")
        return reply(cmd, BackwardFrame(0))
    driver._responder = respond
    cfg = DaliConfig(driver, busy)
    with pytest.raises(CommunicationError):
        await cfg.scan_devices([0, 1, 2])
    assert not busy.is_set()
    assert not cfg._lock.locked()
    assert not any(dest_addr(cmd) == 2 for cmd in driver.sent)
    failure = cfg.monitor.snapshot()["records"][-1]["application"]
    assert failure["name"] == "scan_devices"
    assert failure["phase"] == "failure"
    assert "CommunicationError" in failure["error"]
    driver._responder = lambda cmd: None
    assert await cfg.scan_devices([0]) == []


async def test_rejected_reply_remains_in_monitor_capture(driver):
    cfg = DaliConfig(driver)
    def respond(cmd):
        response = reply(cmd, BackwardFrameError(255))
        # Replay the traffic callback the real driver emits before returning.
        cfg.monitor.on_traffic(driver, cmd, response, False)
        return response
    driver._responder = respond
    assert await cfg.scan_devices([0]) == []
    records = cfg.monitor.snapshot()["records"]
    transaction = records[1]
    assert transaction["reply"]["state"] == "framing error"
    assert transaction["reply"]["raw"]["library_value"] == 255
    assert records[-1]["application"]["phase"] == "completion"


async def test_http_empty_and_transport_failure(driver):
    client = await _client(driver)
    try:
        response = await client.get("/api/scan_devices")
        assert response.status == 200
        assert await response.json() == {"supported": True, "devices": []}
        def fail(cmd):
            raise CommunicationError("adapter unavailable")
        driver._responder = fail
        response = await client.get("/api/scan_devices")
        assert response.status == 500
        assert await response.json() == {"error": "adapter unavailable"}
    finally:
        await client.close()
