from exceptions import DeviceTimeoutError
from mqtt import MqttMessage, MqttConfigMessage

from interruptingcow import timeout
from workers.base import BaseWorker
import logger
import json
import time
from contextlib import contextmanager

#REQUIREMENTS = ["git+https://github.com/mksa1981/xiaomi_sensor_poller.git", "bluepy"]

ATTR_BATTERY = "battery"
ATTR_LOW_BATTERY = 'low_battery'

monitoredAttrs = ["temperature", "humidity", ATTR_BATTERY]
_LOGGER = logger.get(__name__)

class Lywsdcgq_HomeassistantWorker(BaseWorker):
    """
    This worker for the LYWSDCGQ creates the sensor entries in
    MQTT for Home Assistant. It also creates a binary sensor for
    low batteries. It supports connection retries.
    """
    def _setup(self):
        import xiaomi_poller.misensor as misensor
        
        _LOGGER.info("Adding %d %s devices", len(self.devices), repr(self))
        # Initialize scanner platform
        self.poller = misensor.BLEScanner()
        # Setup passive scanner platform
        self.poller.setup_platform(self.devices, self.poller_settings)

        for name, mac in self.devices.items():
            _LOGGER.debug("Adding %s device '%s' (%s)", repr(self), name, mac)

    def config(self):
        ret = []
        for name, data in self.devices.items():
            ret += self.config_device(name, data)
        return ret

    def config_device(self, name, mac):
        ret = []
        device = {
            "identifiers": [mac, self.format_discovery_id(mac, name)],
            "manufacturer": "Xiaomi",
            "model": "Mijia LYWSDCGQ",
            "name": self.format_discovery_name(name),
        }

        for attr in monitoredAttrs:
            payload = {
                "unique_id": self.format_discovery_id(mac, name, attr),
                "state_topic": self.format_prefixed_topic(name, attr),
                "name": self.format_discovery_name(name, attr),
                "device": device,
            }

            if attr == "humidity":
                payload.update({"icon": "mdi:water", "unit_of_measurement": "%"})
            elif attr == "temperature":
                payload.update(
                    {"device_class": "temperature", "unit_of_measurement": "°C"}
                )
            elif attr == ATTR_BATTERY:
                payload.update({"device_class": "battery", "unit_of_measurement": "%"})

            ret.append(
                MqttConfigMessage(
                    MqttConfigMessage.SENSOR,
                    self.format_discovery_topic(mac, name, attr),
                    payload=payload,
                )
            )
        return ret

    def status_update(self):
        _LOGGER.info("Updating %d %s devices", len(self.devices), repr(self))

         # Trigger update of sensors
        self.poller.update_ble()
        for name, data in self.devices.items():
            _LOGGER.debug("Updating %s device '%s' (%s)", repr(self), name, data)
            from btlewrap import BluetoothBackendException

           

            try:
                with timeout(self.per_device_timeout, exception=DeviceTimeoutError):
                    yield self.update_device_state(name, data, self.poller)
            except BluetoothBackendException as e:
                logger.log_exception(
                    _LOGGER,
                    "Error during update of %s device '%s' (%s): %s",
                    repr(self),
                    name,
                    data,
                    type(e).__name__,
                    suppress=True,
                )
            except DeviceTimeoutError:
                logger.log_exception(
                    _LOGGER,
                    "Time out during update of %s device '%s' (%s)",
                    repr(self),
                    name,
                    data,
                    suppress=True,
                )
            except RuntimeError as e:
                logger.log_exception(
                    _LOGGER,
                    "Error during update of %s device '%s' (%s): %s",
                    repr(self),
                    name,
                    data,
                    type(e).__name__,
                    suppress=True,
                )

    def update_device_state(self, name, mac, poller):
        ret = []
        sensors=poller.get_sensors()
        fmac=mac.replace(':', '')
        # verify the sensor was found by the sensor poller
        if fmac in sensors:
            for sensor in sensors[fmac]:
                attrs=sensor.device_state_attributes
                if sensor.device_class == 'temperature' and 'mean' in attrs:
                    ret.append(
                        MqttMessage(
                            topic=self.format_topic(name, 'temperature'),
                            payload=attrs['mean'],
                        )
                    )
                if sensor.device_class == 'humidity' and 'mean' in attrs:
                    ret.append(
                        MqttMessage(
                            topic=self.format_topic(name, 'humidity'),
                            payload=attrs['mean'],
                        )
                    )
                if sensor.device_class == 'battery' and sensor.state != None:
                    ret.append(
                        MqttMessage(
                            topic=self.format_topic(name, 'battery'),
                            payload=sensor.state,
                        )
                    )
        return ret
