"""Capture API pagination, cursor validation and snapshot download contracts."""
import asyncio
from contextlib import asynccontextmanager
import json
from uuid import uuid4

from aiohttp.test_utils import TestClient, TestServer
import dali.address as address
import dali.gear.general as gear
import pytest

from app.bus_monitor import MAX_RECORDS
from app.webserver import DaliWebServer
from conftest import FakeDriver


@asynccontextmanager
async def client_for(driver):
    server = DaliWebServer(driver, {})
    client = TestClient(TestServer(server._app()))
    await client.start_server()
    try:
        yield client, server.cfg.monitor
    finally:
        await client.close()


def traffic(monitor, driver, count=1):
    for _ in range(count):
        monitor.on_traffic(driver, gear.Off(address.Short(0)), None, False)


async def test_records_empty_and_default_page_limit():
    driver = FakeDriver()
    async with client_for(driver) as (client, monitor):
        response = await client.get("/api/monitor/records")
        assert response.status == 200
        empty = await response.json()
        assert empty["records"] == []
        assert empty["next"] == 0
        assert empty["oldest"] is None and empty["newest"] is None
        assert not empty["has_more"] and not empty["history_gap"]
        assert not empty["capture_reset"]
        assert empty["capabilities"]["raw_reports"] is False
        traffic(monitor, driver, 501)
        response = await client.get("/api/monitor/records")
        page = await response.json()
        assert len(page["records"]) == 500
        assert page["next"] == 500 and page["has_more"]
        assert page["oldest"] == 1 and page["newest"] == 501
        response = await client.get("/api/monitor/records", params={
            "since": page["next"], "capture_id": page["capture_id"], "limit": 1,
        })
        last = await response.json()
        assert [record["seq"] for record in last["records"]] == [501]
        assert not last["has_more"]
        response = await client.get("/api/monitor/records?since=501")
        assert (await response.json())["records"] == []


@pytest.mark.parametrize("query", [
    "since=-1", "since=1.5", "since=", "since=%2B1", "since=%201",
    "since=bad", "since=0&since=1", "limit=0", "limit=501", "limit=-1",
    "limit=1.5", "limit=", "limit=1&limit=2", "capture_id=",
    "capture_id=bad", f"capture_id={uuid4()}&capture_id={uuid4()}",
])
async def test_records_reject_invalid_cursors_and_limits(query):
    async with client_for(FakeDriver()) as (client, _):
        response = await client.get("/api/monitor/records?" + query)
        assert response.status == 400
        assert "error" in await response.json()


async def test_reset_gap_and_reconnect_metadata():
    driver = FakeDriver()
    async with client_for(driver) as (client, monitor):
        for _ in range(MAX_RECORDS + 1):
            monitor.capture_report(bytes(64))
        response = await client.get("/api/monitor/records?since=0&limit=1")
        page = await response.json()
        assert page["history_gap"] and not page["capture_reset"]
        assert page["oldest"] == 2 and page["newest"] == MAX_RECORDS + 1
        assert page["counters"]["evicted"] == 1
        assert page["records"][0]["seq"] == 2
        capture_id = page["capture_id"]
        monitor.on_connection(driver, "disconnected")
        monitor.on_connection(driver, "connected")
        response = await client.get("/api/monitor/records", params={
            "capture_id": capture_id, "since": MAX_RECORDS + 1,
        })
        reconnect = await response.json()
        assert reconnect["capture_id"] == capture_id
        assert not reconnect["capture_reset"]
        assert reconnect["connection"] == "connected"
        assert len(reconnect["records"]) == 2
        response = await client.get("/api/monitor/records", params={
            "capture_id": str(uuid4()), "since": 999999, "limit": 1,
        })
        reset = await response.json()
        assert reset["capture_reset"] and reset["history_gap"]
        assert reset["records"][0]["seq"] == reset["oldest"]


async def test_concurrent_clients_read_independent_pages_and_preserve_legacy():
    driver = FakeDriver()
    async with client_for(driver) as (first, monitor):
        async with client_for(driver) as (second, shared):
            assert shared is monitor
            traffic(monitor, driver, 3)
            one, two = await asyncio.gather(
                first.get("/api/monitor/records?since=0&limit=1"),
                second.get("/api/monitor/records?since=1&limit=2"),
            )
            assert [record["seq"] for record in (await one.json())["records"]] == [1]
            assert [record["seq"] for record in (await two.json())["records"]] == [2, 3]
            response = await first.get("/api/monitor?since=1")
            legacy = await response.json()
            assert legacy["last"] == 3
            assert [event["seq"] for event in legacy["events"]] == [2, 3]
            assert set(legacy["events"][0]) == {"seq", "t", "cmd", "type", "address", "resp", "err"}
            response = await second.get("/api/learn")
            assert (await response.json())["addresses"] == [0]


@pytest.mark.parametrize("export_format", ["jsonl", "text"])
async def test_export_snapshots_all_records_and_metadata_before_serialization(monkeypatch, export_format):
    driver = FakeDriver()
    async with client_for(driver) as (client, monitor):
        traffic(monitor, driver)
        monitor.capture_report(b"\x11")
        before = monitor.snapshot()
        original_snapshot = monitor.snapshot

        def snapshot_then_arrival():
            snapshot = original_snapshot()
            monitor.capture_report(b"\x12")
            return snapshot

        monkeypatch.setattr(monitor, "snapshot", snapshot_then_arrival)
        response = await client.get("/api/monitor/export", params={
            "format": export_format, "since": 999, "filter": "no matching records",
        })
        assert response.status == 200
        assert "attachment;" in response.headers["Content-Disposition"]
        body = await response.text()
        if export_format == "jsonl":
            assert response.content_type == "application/x-ndjson"
            lines = [json.loads(line) for line in body.splitlines()]
            metadata, records = lines[0], lines[1:]
        else:
            assert response.content_type == "text/plain"
            lines = body.splitlines()
            assert lines[0] == "DALI bus monitor export"
            metadata = json.loads(lines[1].removeprefix("Metadata: "))
            records = [json.loads(line[2:]) for line in lines[2:] if line.startswith("  {")]
            assert before["records"][0]["received_utc"] in body
        assert metadata["record_type"] == "metadata"
        assert metadata["schema_version"] == before["schema_version"]
        assert metadata["capture_id"] == before["capture_id"]
        assert metadata["counters"] == before["counters"]
        assert metadata["capabilities"] == before["capabilities"]
        assert metadata["oldest"] == 1 and metadata["newest"] == 2
        assert records == before["records"]
        assert len(original_snapshot()["records"]) == 3


@pytest.mark.parametrize("query", ["format=csv", "format=", "format=text&format=jsonl"])
async def test_export_rejects_invalid_formats(query):
    async with client_for(FakeDriver()) as (client, _):
        response = await client.get("/api/monitor/export?" + query)
        assert response.status == 400


async def test_empty_export_defaults_to_jsonl_with_metadata():
    async with client_for(FakeDriver()) as (client, monitor):
        response = await client.get("/api/monitor/export")
        assert response.status == 200
        lines = (await response.text()).splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["capture_id"] == monitor.capture_id
