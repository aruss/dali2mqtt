"""Tests for app.webserver HTTP endpoints (aiohttp)."""
import dali.gear.general as gear
from aiohttp.test_utils import TestClient, TestServer

from app.webserver import DaliWebServer
from conftest import FakeDriver, cname, dest_addr, groups_value, no, numeric, yes
from test_input_devices import DeviceBus


def _scan_responder(present):
    def responder(cmd):
        n = cname(cmd)
        a = dest_addr(cmd)
        if n == "QueryControlGearPresent":
            return yes() if a in present else no()
        if n == "QueryGroupsZeroToSeven" or n == "QueryGroupsEightToFifteen":
            return groups_value(0)
        if n.startswith("Query"):
            return numeric(50)
        return None
    return responder


async def _client(driver, **kw):
    web = DaliWebServer(driver, {}, **kw)
    client = TestClient(TestServer(web._app()))
    await client.start_server()
    return client


async def test_input_device_configuration_routes():
    driver = FakeDriver(DeviceBus())
    client = await _client(driver)
    try:
        response = await client.get("/api/device/0")
        assert response.status == 200
        assert len((await response.json())["instances"]) == 4
        for path, body, field, value in [
            ("instance/2/enabled", {"enabled": False}, "enabled", False),
            ("instance/0/scheme", {"scheme": 0}, "scheme", 0),
            ("application_controller", {"enabled": False}, "ok", True),
            ("save", {}, "persistence_verified", False),
        ]:
            response = await client.post(f"/api/device/0/{path}", json=body)
            assert response.status == 200, await response.text()
            assert (await response.json())[field] == value
        for path, body in [
            ("instance/0/enabled", {"enabled": "false"}),
            ("instance/0/enabled", []),
            ("instance/0/enabled", {"enabled": 1}),
            ("instance/0/scheme", {"scheme": 9}),
            ("instance/0/scheme", {"scheme": False}),
            ("instance/4/scheme", {"scheme": 2}),
            ("instance/-1/enabled", {"enabled": True}),
            ("application_controller", {}),
        ]:
            driver.reset()
            response = await client.post(f"/api/device/0/{path}", json=body)
            assert response.status == 400
            assert "error" in await response.json()
            assert all(cname(c).startswith("Query") for c in driver.sent)
        for addr, status in [(64, 400), (3, 502), ("bad", 400)]:
            response = await client.post(f"/api/device/{addr}/save")
            assert response.status == status
        response = await client.post("/api/device/0/instance/0/enabled", data="{")
        assert response.status == 400
    finally:
        await client.close()


async def test_input_device_route_readback_failure(busy):
    bus = DeviceBus()
    bus.ignore_writes = True
    client = await _client(FakeDriver(bus), busy=busy)
    try:
        response = await client.post("/api/device/0/application_controller", json={"enabled": False})
        assert response.status == 502
        assert "mismatch" in (await response.json())["error"]
        assert not busy.is_set()
    finally:
        await client.close()


async def test_scan_endpoint():
    driver = FakeDriver(responder=_scan_responder(present={6}))
    client = await _client(driver)
    try:
        r = await client.get("/api/scan")
        assert r.status == 200
        data = await r.json()
        assert [g["address"] for g in data["gear"]] == [6]
    finally:
        await client.close()


async def test_level_endpoint_sends_dapc():
    driver = FakeDriver()
    client = await _client(driver)
    try:
        r = await client.post("/api/gear/6/level", json={"level": 120})
        assert r.status == 200
        assert isinstance(driver.sent[0], gear.DAPC)
    finally:
        await client.close()


async def test_set_field_unknown_returns_400():
    driver = FakeDriver()
    client = await _client(driver)
    try:
        r = await client.post("/api/gear/6/set", json={"field": "nope", "value": 1})
        assert r.status == 400
    finally:
        await client.close()


async def test_simulate_button_endpoint():
    driver = FakeDriver()
    client = await _client(driver, simulate=lambda a: {"ok": True, "address": a})
    try:
        r = await client.post("/api/simulate_button/20")
        assert (await r.json()) == {"ok": True, "address": 20}
    finally:
        await client.close()


async def test_simulate_button_unavailable_501():
    driver = FakeDriver()
    client = await _client(driver)   # no simulate callable
    try:
        r = await client.post("/api/simulate_button/20")
        assert r.status == 501
    finally:
        await client.close()


async def test_monitor_endpoint():
    driver = FakeDriver()
    client = await _client(driver)
    try:
        r = await client.get("/api/monitor?since=0")
        assert r.status == 200
        data = await r.json()
        assert "events" in data and "last" in data
    finally:
        await client.close()
