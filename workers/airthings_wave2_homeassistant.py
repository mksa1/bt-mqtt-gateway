from exceptions import DeviceTimeoutError
from mqtt import MqttMessage, MqttConfigMessage

from interruptingcow import timeout
from workers.base import BaseWorker
import logger
import json
import time
from contextlib import contextmanager

import bluepy.btle as btle
import argparse
import signal
import struct
import sys
import yaml

"""
0 - 49 Bq/m3  (0 - 1.3 pCi/L):
No action necessary.
50 - 99 Bq/m3 (1.4 - 2.6 pCi/L):
Experiment with ventilation and sealing cracks to reduce levels.
100 Bq/m3 - 299 Bq/m3 (2.7 - 8 pCi/L):
Keep measuring. If levels are maintained for more than 3 months,
contact a professional radon mitigator.
300 Bq/m3 (8.1 pCi/L) and up:
Keep measuring. If levels are maintained for more than 1 month,
contact a professional radon mitigator.
"""
VERY_LOW = [0, 49, 'very low']
LOW = [50, 99, 'low']
MODERATE = [100, 299, 'moderate']
HIGH = [300, None, 'high']

REQUIREMENTS = ["bluepy"]

ATTR_BATTERY = "battery"
ATTR_LOW_BATTERY = 'low_battery'
ATTR_RADON_LEVEL_STA = 'radon_level_sta'
ATTR_RADON_LEVEL_LTA = 'radon_level_lta'
DEVICE_CLASS_RADON='radon'

VOLUME_BECQUEREL = 'Bq/m3'

monitoredAttrs = ["temperature", "humidity", ATTR_RADON_LEVEL_STA, ATTR_RADON_LEVEL_LTA]
_LOGGER = logger.get(__name__)

class Airthings_Wave2_HomeassistantWorker(BaseWorker):
    """
    This worker for the Airthings Wave2 creates the sensor entries in
    MQTT for Home Assistant. It also creates a binary sensor for
    low batteries. It supports connection retries.
    """
    def _setup(self):
        _LOGGER.info("Adding %d %s devices", len(self.devices), repr(self))
        for name, mac in self.devices.items():
            _LOGGER.debug("Adding %s device '%s' (%s)", repr(self), name, mac)
            self.devices[name] = {
                "mac": mac,
                "poller": Wave2Poller(mac),
            }

    def config(self):
        ret = []
        for name, data in self.devices.items():
            ret += self.config_device(name, data["mac"])
        return ret

    def config_device(self, name, mac):
        ret = []
        device = {
            "identifiers": [mac, self.format_discovery_id(mac, name)],
            "manufacturer": "Airthings",
            "model": "Wave2",
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
            elif attr == ATTR_RADON_LEVEL_STA:
                payload.update(
                    {"davice_class": DEVICE_CLASS_RADON, "unit_of_measurement": VOLUME_BECQUEREL}
                )
            elif attr == ATTR_RADON_LEVEL_LTA:
                    payload.update(
                    {"davice_class": DEVICE_CLASS_RADON, "unit_of_measurement": VOLUME_BECQUEREL}
                )
            ret.append(
                MqttConfigMessage(
                    MqttConfigMessage.SENSOR,
                    self.format_discovery_topic(mac, name, attr),
                    payload=payload,
                )
            )

        return ret

    def status_update(self):
        from bluepy import btle
        _LOGGER.info("Updating %d %s devices", len(self.devices), repr(self))

        for name, data in self.devices.items():
            _LOGGER.debug("Updating %s device '%s' (%s)", repr(self), name, data["mac"])
            # from btlewrap import BluetoothBackendException

            try:
                with timeout(self.per_device_timeout, exception=DeviceTimeoutError):
                    yield self.update_device_state(name, data["poller"])
            except btle.BTLEException as e:
                logger.log_exception(
                    _LOGGER,
                    "Error during update of %s device '%s' (%s): %s",
                    repr(self),
                    name,
                    data["mac"],
                    type(e).__name__,
                    suppress=True,
                )
            except DeviceTimeoutError:
                logger.log_exception(
                    _LOGGER,
                    "Time out during update of %s device '%s' (%s)",
                    repr(self),
                    name,
                    data["mac"],
                    suppress=True,
                )

    def update_device_state(self, name, poller):
        ret = []
        poller.connect()
        values = poller.read()
        for attr in monitoredAttrs:

            attrValue = None
            if attr == "humidity":
                attrValue = values.humidity
            elif attr == "temperature":
                attrValue = values.temperature
            elif attr == ATTR_RADON_LEVEL_STA:
                attrValue = values.radon_sta
            elif attr == ATTR_RADON_LEVEL_LTA:
                attrValue = values.radon_lta

            ret.append(
                MqttMessage(
                    topic=self.format_topic(name, attr),
                    payload=attrValue,
                )
            )
        poller.disconnect()
        return ret


class Wave2Poller():
    
    CURR_VAL_UUID = btle.UUID("b42e4dcc-ade7-11e4-89d3-123b93f75cba")

    def __init__(self, mac, maxattempt=4):
        self._periph = None
        self._char = None
        self.mac = mac
        self.maxattempt = maxattempt
        self._temperature = None
        self._humidity = None
        self._radon_sta = None
        self._radon_lta = None


    def is_connected(self):
        try:
            return self._periph.getState() == "conn"
        except Exception:
            return False

    def connect(self, retries=1):
        tries = 0
        while (tries < retries and self.is_connected() is False):
            tries += 1
            try:
                self._periph = btle.Peripheral(self.mac)
                self._char = self._periph.getCharacteristics(uuid=self.CURR_VAL_UUID)[0]
            except Exception:
                if tries == retries:
                    raise
                else:
                    pass

    def __del__(self):
        self.disconnect()
                
    def read(self):
        rawdata = self._char.read()
        return CurrentValues.from_bytes(rawdata)

    def disconnect(self):
        if self._periph is not None:
            self._periph.disconnect()
            self._periph = None
            self._char = None


class CurrentValues():

    def __init__(self, humidity, radon_sta, radon_lta, temperature):
        self.humidity = humidity
        self.radon_sta = radon_sta
        self.radon_lta = radon_lta
        self.temperature = temperature

    @classmethod
    def from_bytes(cls, rawdata):
        data = struct.unpack("<4B8H", rawdata)
        if data[0] != 1:
            raise ValueError("Incompatible current values version (Expected 1, got {})".format(data[0]))
        return cls(data[1]/2.0, data[4], data[5], data[6]/100.0)

    def __str__(self):
        msg = "Humidity: {} %rH, ".format(self.humidity)
        msg += "Temperature: {} *C, ".format(self.temperature)
        msg += "Radon STA: {} Bq/m3, ".format(self.radon_sta)
        msg += "Radon LTA: {} Bq/m3".format(self.radon_lta)
        return msg



