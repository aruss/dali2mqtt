"""Address-scoped Jung MQTT profiles. Assignments never access the DALI bus."""
import hashlib
import json
from pathlib import Path


GESTURES = "jung_1745_dste"
ABSOLUTE = "jung_1745_dste_absolute"
ACTIONS = {
    (0, 0): "release",
    (0, 1): "press",
    (0, 2): "short_press",
    (2, 0): "stop",
    (2, 1): "clockwise_fast",
    (2, 2): "clockwise_slow",
    (2, 3): "clockwise_increment",
    (2, 4): "counterclockwise_fast",
    (2, 5): "counterclockwise_slow",
    (2, 6): "counterclockwise_increment",
}


class JungProfiles:
    def __init__(self, directory, base, prefix):
        self.base = base
        self.prefix = prefix
        self.client = None
        self.bridge_id = hashlib.sha256(base.encode()).hexdigest()[:24]
        self.path = Path(directory) / f"jung-profiles-{self.bridge_id}.json"
        try:
            self.assignments = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.assignments = {}

    def profile(self, addr):
        return self.assignments.get(str(addr))

    def assign(self, addr, profile):
        if not 0 <= addr <= 63 or profile not in (None, GESTURES, ABSOLUTE):
            raise ValueError("Invalid control-device address or Jung profile")
        # Keep removed addresses so a disconnected broker is cleaned on reconnect.
        assignments = dict(self.assignments)
        assignments[str(addr)] = profile
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(assignments), encoding="utf-8")
        temporary.replace(self.path)
        self.assignments = assignments
        if self.client is not None:
            self.discover(addr)

    def refresh(self, client):
        self.client = client
        for addr in self.assignments:
            self.discover(int(addr))

    def discover(self, addr):
        profile = self.profile(addr)
        node = f"jung_{self.bridge_id}_d{addr}"
        device = {"identifiers": [node], "name": f"Jung 1745 DSTE D{addr}",
                  "manufacturer": "Jung", "model": "1745 DSTE"}
        topic = f"{self.base}/control_device_{addr}"
        common = {"device": device, "availability_topic": f"{self.base}/status"}

        def publish(component, key, config):
            self.client.publish(f"{self.prefix}/{component}/{node}/{key}/config",
                                json.dumps(config) if config else "", qos=1, retain=True)

        action_config = {**common, "name": "Action", "unique_id": f"{node}_action",
                         "state_topic": f"{topic}/action", "event_types": list(ACTIONS.values())}
        publish("event", "action", action_config if profile else None)
        for action in ACTIONS.values():
            publish("device_automation", action, {
                "device": device, "automation_type": "trigger", "type": "action",
                "subtype": action, "topic": f"{topic}/action",
                "value_template": "{{ value_json.event_type }}", "payload": action,
            } if profile else None)
        sensor_config = {**common, "name": "Rotary input", "unique_id": f"{node}_rotary_input",
                         "state_topic": f"{topic}/rotary_input", "state_class": "measurement"}
        publish("sensor", "rotary_input", sensor_config if profile == ABSOLUTE else None)

    def handle_event(self, event):
        addr = event["source_address"]
        profile = self.profile(addr)
        if not profile or self.client is None:
            return
        instance = event["instance_number"]
        info = event["event_info"]
        topic = f"{self.base}/control_device_{addr}"
        if instance == 3 and profile == ABSOLUTE and event["instance_type"] in (2, None):
            self.client.publish(f"{topic}/rotary_input", str(info), retain=False)
            return
        action = ACTIONS.get((instance, info))
        if action and event["instance_type"] in (None, 1 if instance == 0 else 0):
            self.client.publish(f"{topic}/action", json.dumps({"event_type": action}), retain=False)
