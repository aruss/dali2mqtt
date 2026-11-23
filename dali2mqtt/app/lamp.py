"""Class to represent dali lamps"""
import json
import asyncio
from pprint import pformat
import dali.gear.general as gear

from .config import Config
from .consts import *

from .devicesnamesconfig import DevicesNamesConfig
from .functions import normalize
from .dali_config import DaliConfig

logging.basicConfig(format=LOG_FORMAT)
logger = logging.getLogger(__name__)

COLOUR_OPERATION_TIMEOUT = 5


class Lamp:
    def __init__(self, driver, mqtt, dali_lamp, busy=None):
        self.config = Config()
        logger.setLevel(ALL_SUPPORTED_LOG_LEVELS[self.config[CONF_LOG_LEVEL]])

        self.driver = driver
        self.mqtt = mqtt
        self.virtual = True #DevicesNamesConfig().get_virtual(f"DALI Lamp {self.address}")
        self.dali_lamp = dali_lamp
        self.address = dali_lamp.address

        self.friendly_name = DevicesNamesConfig().get_friendly_name(f"DALI Lamp {self.address}")
        self.device_name = f"lamp_{self.address}"

        self.current_scene = None
        self.colour_config = DaliConfig(driver, busy=busy)
        self.colour_capabilities = {}


    async def read_current_state(self):
        _scenes = []
        for i in range(0, 16):
            v = (await self.driver.send(gear.QuerySceneLevel(self.dali_lamp, i))).value
            _scenes.append(v)
        self.scenes = _scenes
        logger.debug(f"Scenes: {json.dumps(self.scenes)}")

        self.groups = []
        self.min_physical_level = (await self.driver.send(gear.QueryPhysicalMinimum(self.dali_lamp))).value
        self.min_level = (await self.driver.send(gear.QueryMinLevel(self.dali_lamp))).value
        self.min_levels = max(self.min_physical_level, self.min_level)
        self.max_level = 254
#        await self.driver.send(gear.DAPC(self.dali_lamp, 254))
#        await self.driver.send(gear.SetMaxLevel(self.dali_lamp))
#(await self.driver.send(gear.QueryMaxLevel(self.dali_lamp))).value

        await self._getLevelDALI()
        try:
            self.colour_capabilities = await asyncio.wait_for(
                self.colour_config.colour_capabilities(self.address), COLOUR_OPERATION_TIMEOUT
            )
        except Exception as err:
            logger.warning("DT8 capabilities unavailable for A%s: %s", self.address, err)
        self._register_discovery()
        await self.pollColour()
        self.setSceneToNoneMQTT()

        self.mqtt.publish(
            MQTT_BRIGHTNESS_STATE_TOPIC.format(self.config[CONF_MQTT_BASE_TOPIC], self.device_name),
            self.level,
            retain=True,
        )
        self.mqtt.publish(
            MQTT_STATE_TOPIC.format(self.config[CONF_MQTT_BASE_TOPIC], self.device_name),
            MQTT_PAYLOAD_ON if self.level > 0 else MQTT_PAYLOAD_OFF,
            retain=True,
        )
        logger.info(
            "   - short address: %d, actual brightness level: %d (minimum: %d, max: %d, physical minimum: %d)",
            self.address,
            self.level,
            self.min_level,
            self.max_level,
            self.min_physical_level,
        )

    def send_brightness_to_mqtt(self, level):
        self.mqtt.publish(
            MQTT_BRIGHTNESS_STATE_TOPIC.format(self.config[CONF_MQTT_BASE_TOPIC], self.device_name),
            level,
            retain=True,
        )
        self.mqtt.publish(
            MQTT_STATE_TOPIC.format(self.config[CONF_MQTT_BASE_TOPIC], self.device_name),
            MQTT_PAYLOAD_ON if level > 0 else MQTT_PAYLOAD_OFF,
            retain=True,
        )


    def __repr__(self):
        return f"LAMP A{self.address}"

    __str__ = __repr__

    def addGroup(self, group):
        self.groups.append(group)

    def _register_discovery(self):
        json_config = {
            "name": self.friendly_name,
            "unique_id": f"{self.config[CONF_MQTT_BASE_TOPIC]}_{self.device_name}",
            "state_topic": MQTT_STATE_TOPIC.format(self.config[CONF_MQTT_BASE_TOPIC], self.device_name),
            "command_topic": MQTT_COMMAND_TOPIC.format(
                self.config[CONF_MQTT_BASE_TOPIC], self.device_name
            ),
            "payload_off": MQTT_PAYLOAD_OFF.decode("utf-8"),
            "brightness_state_topic": MQTT_BRIGHTNESS_STATE_TOPIC.format(
                self.config[CONF_MQTT_BASE_TOPIC], self.device_name
            ),
            "brightness_command_topic": MQTT_BRIGHTNESS_COMMAND_TOPIC.format(
                self.config[CONF_MQTT_BASE_TOPIC], self.device_name
            ),
            "brightness_scale": 255,
            "on_command_type": "brightness",
            "availability_topic": MQTT_DALI2MQTT_STATUS.format(self.config[CONF_MQTT_BASE_TOPIC]),
            "payload_available": MQTT_AVAILABLE,
            "payload_not_available": MQTT_NOT_AVAILABLE,
            "device": {
                "identifiers": f"{self.config[CONF_MQTT_BASE_TOPIC]}_A{self.address}",
                "via_device": self.config[CONF_MQTT_BASE_TOPIC],
                "name": f"DALI Lamp {self.address}",
                "sw_version": f"dali2mqtt {VERSION}",
                "manufacturer": AUTHOR,
                "connections": [("DALI", f"A{self.address}")]
            },
        }
        
        caps = self.colour_capabilities
        if caps.get("color_temp"):
            json_config.update(
                color_temp_command_topic=self.colour_topic("color_temp", "set"),
                color_temp_state_topic=self.colour_topic("color_temp", "status"),
                min_mireds=caps["min_mireds"], max_mireds=caps["max_mireds"],
            )
        if caps.get("rgb"):
            json_config.update(
                rgb_command_topic=self.colour_topic("rgb", "set"),
                rgb_state_topic=self.colour_topic("rgb", "status"),
            )
        if caps.get("color_temp") or caps.get("rgb"):
            json_config["color_mode_state_topic"] = self.colour_topic("color_mode", "status")

        self.mqtt.publish(
            HA_DISCOVERY_PREFIX_LIGHT.format(self.config[CONF_HA_DISCOVERY_PREFIX], self.config[CONF_MQTT_BASE_TOPIC], self.device_name),
            json.dumps(json_config),
            retain=True,
        )

        json_config = {
            "name": self.friendly_name + " Scene",
            "unique_id": f"{self.config[CONF_MQTT_BASE_TOPIC]}_{self.device_name}_scene",
            "state_topic": MQTT_SCENE_STATE_TOPIC.format(self.config[CONF_MQTT_BASE_TOPIC], self.device_name),
            "command_topic": MQTT_SCENE_COMMAND_TOPIC.format(
                self.config[CONF_MQTT_BASE_TOPIC], self.device_name
            ),
            "options": [],
            "availability_topic": MQTT_DALI2MQTT_STATUS.format(self.config[CONF_MQTT_BASE_TOPIC]),
            "payload_available": MQTT_AVAILABLE,
            "payload_not_available": MQTT_NOT_AVAILABLE,
            "device": {
                "identifiers": f"{self.config[CONF_MQTT_BASE_TOPIC]}_A{self.address}",
                "via_device": self.config[CONF_MQTT_BASE_TOPIC],
                "name": f"DALI Lamp {self.address}",
                "sw_version": f"dali2mqtt {VERSION}",
                "manufacturer": AUTHOR,
                "connections": [("DALI", f"A{self.address}")]
            },
        }
        json_config["options"].append("-")
        for index, scene in enumerate(self.scenes):
            if str(scene).upper() != "MASK":
                json_config["options"].append(f"Scene {index}")

        self.mqtt.publish(
            HA_DISCOVERY_PREFIX_SELECT.format(self.config[CONF_HA_DISCOVERY_PREFIX], self.config[CONF_MQTT_BASE_TOPIC], self.device_name),
            json.dumps(json_config),
            retain=True,
        )   

    async def setLevel(self, level, dali=True):
        if self.level == level:
            return
        old = self.level
        if dali:
            await self._sendLevelDALI(level)
        self.level = level
        if dali:
            for _x in self.groups:
                _x.recalc_level()

        self._sendLevelMQTT(level, old)

    async def setScene(self, scene, dali=True):
        self.setSceneToNoneMQTT()
        if 0 <= scene <= 15 and self.scenes[scene] != "MASK":
            level = self.scenes[scene]

            self.mqtt.publish(
                MQTT_SCENE_STATE_TOPIC.format(self.config[CONF_MQTT_BASE_TOPIC], self.device_name), f"Scene {scene}",
                retain=True)
            self.current_scene = scene

            if self.level != level:
                old = self.level
                self.level = level
                if dali:
                    for _x in self.groups:
                        _x.recalc_level()
                    self._sendSceneDALI(scene)

                self._sendLevelMQTT(level, old)

    def _sendLevelMQTT(self, level, old_level):
        self.mqtt.publish(
            MQTT_BRIGHTNESS_STATE_TOPIC.format(self.config[CONF_MQTT_BASE_TOPIC], self.device_name),
            level,
            retain=True,
        )
        if old_level == 0 or level == 0:
            self.mqtt.publish(
                MQTT_STATE_TOPIC.format(self.config[CONF_MQTT_BASE_TOPIC], self.device_name),
                MQTT_PAYLOAD_ON if level > 0 else MQTT_PAYLOAD_OFF,
                retain=True,
            )

    def setSceneToNoneMQTT(self):
        self.mqtt.publish(
            MQTT_SCENE_STATE_TOPIC.format(self.config[CONF_MQTT_BASE_TOPIC], self.device_name), "-", retain=True)

    async def pollLevel(self):
        old = self.level
        await self._getLevelDALI()
        if old != self.level:
            self.mqtt.publish(
                MQTT_BRIGHTNESS_STATE_TOPIC.format(self.config[CONF_MQTT_BASE_TOPIC], self.device_name),
                self.level,
                retain=True,
            )
            self.mqtt.publish(
                MQTT_STATE_TOPIC.format(self.config[CONF_MQTT_BASE_TOPIC], self.device_name),
                MQTT_PAYLOAD_ON if self.level > 0 else MQTT_PAYLOAD_OFF,
                retain=True,
            )
        await self.pollColour()

    def colour_topic(self, mode, suffix):
        return f"{self.config[CONF_MQTT_BASE_TOPIC]}/{self.device_name}/{mode}/{suffix}"

    async def pollColour(self):
        if not (self.colour_capabilities.get("color_temp") or self.colour_capabilities.get("rgb")):
            return
        try:
            state = await asyncio.wait_for(
                self.colour_config.colour_state(self.address, self.colour_capabilities),
                COLOUR_OPERATION_TIMEOUT,
            )
        except Exception as err:
            logger.warning("DT8 state unavailable for A%s: %s", self.address, err)
            return
        for mode, value in state.items():
            if mode == "rgb":
                value = ",".join(str(v) for v in value)
            self.mqtt.publish(self.colour_topic(mode, "status"), value, retain=True)

    async def setColour(self, mode, payload):
        if not self.colour_capabilities.get(mode):
            raise ValueError(f"A{self.address} does not support {mode}")
        if mode == "color_temp":
            if not payload.isascii() or not payload.isdecimal():
                raise ValueError("Temperature must be integer mireds")
            value = int(payload)
            caps = self.colour_capabilities
            if not caps["min_mireds"] <= value <= caps["max_mireds"]:
                raise ValueError("Temperature outside controller limits")
            await asyncio.wait_for(
                self.colour_config.set_colour_temp(self.address, value), COLOUR_OPERATION_TIMEOUT
            )
        elif mode == "rgb":
            parts = payload.split(",")
            if len(parts) != 3 or any(not v.strip().isascii() or not v.strip().isdecimal() for v in parts):
                raise ValueError("RGB must contain three integers")
            values = [int(v) for v in parts]
            if any(v > 255 for v in values):
                raise ValueError("RGB outside 0-255")
            await asyncio.wait_for(
                self.colour_config.set_colour_rgb(self.address, *(round(v * 254 / 255) for v in values)),
                COLOUR_OPERATION_TIMEOUT,
            )
        await self.pollColour()

    async def _sendLevelDALI(self, level):
        orig_level = level
        level = normalize(level, 1, 255, self.min_levels, 254) if level else 0
#        level = 254
        if orig_level == 0 or orig_level =='OFF':
#            await self.driver.send(gear.Off(self.dali_lamp))
            await self.driver.send(gear.DAPC(self.dali_lamp, 0))
        else:
            await self.driver.send(gear.DAPC(self.dali_lamp, level))
        logger.info(f"Set {self.friendly_name} brightness level to {orig_level} ({level})")

    async def _sendSceneDALI(self, scene):
        await self.driver.send(gear.GoToScene(self.dali_lamp, scene))
        logger.info(f"Call scene {scene} on {self.friendly_name}")

    async def _getLevelDALI(self):
        level = (await self.driver.send(gear.QueryActualLevel(self.dali_lamp))).value
        if level == 0:
            self.level = 0
        elif self.min_levels == 254:
            self.level = 255
        else:
            self.level = normalize(max(level, self.min_levels), self.min_levels, 254, 1, 255)
        logger.debug(f"Get {self.friendly_name} brightness level {self.level} ({level})")

    async def flash(self, count, speed):
        logger.info(f"Flash lamp {self.friendly_name}: Count: {count}, Speed {speed}")
        for n in range(count):
            await self.driver.send(gear.RecallMaxLevel(self.dali_lamp))
            await asyncio.sleep(speed)
            await self.driver.send(gear.RecallMinLevel(self.dali_lamp))
            await asyncio.sleep(speed)

        if self.level >= 127:
            await self.driver.send(gear.RecallMaxLevel(self.dali_lamp))

        await self._sendLevelDALI(self.level)
