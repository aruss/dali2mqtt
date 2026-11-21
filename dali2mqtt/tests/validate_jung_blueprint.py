"""Offline HA schema and action-engine checks; run in the HA 2025.3.4 image.

Mount the repository at /repo and run this file with the image's Python.
No broker, live Home Assistant, USB device, or external service is contacted.
"""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from homeassistant import loader
from homeassistant.components.automation.config import async_validate_config_item
from homeassistant.components.blueprint.models import Blueprint, BlueprintInputs
from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import condition
from homeassistant.helpers.script import Script
from homeassistant.util import yaml

BLUEPRINT = Path(__file__).resolve().parents[2] / "example/blueprints/automation/jung_1745_dste.yaml"


async def main():
    with TemporaryDirectory() as directory:
        hass = HomeAssistant(directory)
        loader.async_setup(hass)
        blueprint = Blueprint(yaml.load_yaml(str(BLUEPRINT)), expected_domain="automation", schema=BLUEPRINT_SCHEMA)
        assert blueprint.validate() is None
        calls = []

        async def service(call):
            data = dict(call.data)
            if isinstance(data.get("entity_id"), list) and len(data["entity_id"]) == 1:
                data["entity_id"] = data["entity_id"][0]
            calls.append((call.domain + "." + call.service, data))
            entities = call.data.get("entity_id", [])
            if isinstance(entities, str):
                entities = [entities]
            for entity in entities:
                if call.domain == "input_boolean":
                    hass.states.async_set(entity, "on" if call.service == "turn_on" else "off")
                if call.domain == "timer":
                    hass.states.async_set(entity, "active" if call.service == "start" else "idle")

        for domain, services in {"input_boolean": ["turn_on", "turn_off"], "timer": ["start", "cancel"], "light": ["toggle", "turn_on"]}.items():
            for name in services:
                hass.services.async_register(domain, name, service)

        async def make_knob(addr):
            values = {"device_address": addr, "target_light": f"light.knob_{addr}",
                      "pressed": f"input_boolean.pressed_{addr}",
                      "rotated_while_pressed": f"input_boolean.rotated_{addr}",
                      "recovery_timer": f"timer.recovery_{addr}"}
            inputs = BlueprintInputs(blueprint, {"use_blueprint": {"path": "jung.yaml", "input": values}})
            inputs.validate()
            config = await async_validate_config_item(hass, "automation", inputs.async_substitute())
            variables = config["variables"]
            checks = [await condition.async_from_config(hass, c) for c in config["conditions"]]
            script = Script(hass, config["actions"], f"knob {addr}", "automation", script_mode=config["mode"], max_runs=config["max"])
            hass.states.async_set(values["pressed"], "off")
            hass.states.async_set(values["rotated_while_pressed"], "off")
            hass.states.async_set(values["recovery_timer"], "idle")
            hass.states.async_set(values["target_light"], "on", {
                "supported_color_modes": ["color_temp"], "min_color_temp_kelvin": 2000,
                "max_color_temp_kelvin": 6500, "color_temp_kelvin": 4000})

            async def fire(instance=None, info=None, *, kind="bus", payload=None):
                trigger = {"id": kind}
                if kind == "bus":
                    trigger["payload_json"] = payload if payload is not None else {
                        "event": True, "source_address": addr, "instance_number": instance, "event_info": info}
                rendered = variables.async_render(hass, {"trigger": trigger})
                if all(check(hass, rendered) for check in checks):
                    await script.async_run(rendered, context=Context())
            return values, fire

        values, fire = await make_knob(0)
        other, fire_other = await make_knob(7)
        def lights():
            return [(name, data) for name, data in calls if name.startswith("light.")]
        def clear():
            calls.clear()
        def state(key):
            return hass.states.get(values[key]).state

        # A normal click, including the physical release-before-short ordering.
        await fire(0, 1)
        await fire(0, 0)
        await fire(0, 2)
        assert [name for name, _ in lights()] == ["light.toggle"]
        assert state("pressed") == "off"
        assert ("timer.start", {"entity_id": values["recovery_timer"], "duration": "00:00:30"}) in calls

        # All direction/speed combinations, stop does not change the light.
        clear()
        for code in range(7):
            await fire(2, code)
        assert [data["brightness_step_pct"] for _, data in lights()] == [10, 5, 2, -10, -5, -2]

        # Press + rotation suppresses toggle both before and after release.
        clear()
        await fire(0, 1)
        for code in range(1, 7):
            await fire(2, code)
        assert [data["color_temp_kelvin"] for _, data in lights()] == [4200, 4100, 4050, 3800, 3900, 3950]
        await fire(0, 2)
        await fire(0, 0)
        await fire(0, 2)
        assert all(name != "light.toggle" for name, _ in lights())
        assert state("rotated_while_pressed") == "on"
        await fire(0, 1)
        await fire(0, 2)
        assert lights()[-1][0] == "light.toggle"

        # Temperature limits, midpoint fallback, unsupported light.
        for current, code, expected in [(6490, 1, 6500), (2010, 4, 2000), (None, 1, 4450)]:
            entity = hass.states.get(values["target_light"])
            hass.states.async_set(entity.entity_id, "on", {**entity.attributes, "color_temp_kelvin": current})
            clear()
            await fire(2, code)
            assert lights()[-1][1]["color_temp_kelvin"] == expected
        hass.states.async_set(values["target_light"], "on", {"supported_color_modes": ["brightness"]})
        clear()
        await fire(2, 1)
        assert lights() == [] and state("rotated_while_pressed") == "on"

        # Ignore stale timeout queued behind activity; expire missed release.
        await fire(kind="timeout")
        assert state("pressed") == "on"
        hass.states.async_set(values["recovery_timer"], "idle")
        await fire(kind="timeout")
        assert state("pressed") == state("rotated_while_pressed") == "off"
        clear()
        await fire(2, 1)
        assert lights()[-1][1]["brightness_step_pct"] == 10
        for _ in range(2):  # startup and bridge disconnect share reset branch
            await fire(0, 1)
            await fire(kind="reset")
            assert state("pressed") == state("rotated_while_pressed") == "off"
            assert state("recovery_timer") == "idle"

        # Reject other device, gear commands, malformed JSON, wrong instance/code.
        clear()
        for payload in [{}, [], {"event": False}, {"event": True, "source_address": 7, "instance_number": 0, "event_info": 2},
                        {"event": True, "source_address": 0, "instance_number": 1, "event_info": 2},
                        {"event": True, "source_address": 0, "instance_number": 0, "event_info": 3}]:
            await fire(payload=payload)
        assert calls == []

        # Independent knobs, queued actions retain press -> rotation -> release order.
        clear()
        await asyncio.gather(fire(0, 1), fire(2, 1), fire(0, 0), fire(0, 2))
        assert lights() == []  # unsupported colour light; suppressed toggle
        await fire_other(0, 2)
        assert lights() == [("light.toggle", {"entity_id": other["target_light"]})]
        assert hass.states.get(other["pressed"]).state == "off"
        await hass.async_block_till_done()
        print("PASS: HA blueprint schema, trigger/action validation, real templates and queued action engine; click, all rotary speeds/directions, pressed rotation, release ordering, toggle suppression, bounds, midpoint, unsupported light, timeout, startup/disconnect reset, malformed events and two independent knobs")


asyncio.run(main())
