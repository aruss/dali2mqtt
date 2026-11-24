"""Run in the pinned Home Assistant test image with the add-on mounted at /addon."""
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from homeassistant.components.mqtt import device_trigger, event, sensor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.jung import ABSOLUTE, JungProfiles


class SchemaCheck:
    def __init__(self):
        self.topics = set()

    def publish(self, topic, payload, qos, retain):
        assert retain and topic not in self.topics
        self.topics.add(topic)
        component = topic.split("/")[1]
        schemas = {"device_automation": device_trigger.TRIGGER_DISCOVERY_SCHEMA,
                   "event": event.DISCOVERY_SCHEMA, "sensor": sensor.DISCOVERY_SCHEMA}
        schemas[component](json.loads(payload))


if __name__ == "__main__":
    checker = SchemaCheck()
    with TemporaryDirectory() as directory:
        profiles = JungProfiles(directory, "dali2mqtt", "homeassistant")
        profiles.assign(1, ABSOLUTE)
        profiles.refresh(checker)
    assert len(checker.topics) == 12
    print("Validated the Action entity, ten device triggers, and the Rotary input sensor.")
