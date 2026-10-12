import asyncio
import aiofiles
import json
import logging
import math
import os.path

import voluptuous as vol

from homeassistant.components.fan import (
    FanEntity, FanEntityFeature,
    PLATFORM_SCHEMA, DIRECTION_REVERSE, DIRECTION_FORWARD)
from homeassistant.const import (
    CONF_NAME, STATE_OFF, STATE_ON)
from homeassistant.core import Event, EventStateChangedData, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util.percentage import (
    ordered_list_item_to_percentage,
    percentage_to_ordered_list_item
)
from . import COMPONENT_ABS_DIR, Helper
from .controller import get_controller

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "SmartIR Fan"
DEFAULT_DELAY = 0.5

CONF_UNIQUE_ID = 'unique_id'
CONF_DEVICE_CODE = 'device_code'
CONF_CONTROLLER_DATA = "controller_data"
CONF_DELAY = "delay"
CONF_POWER_SENSOR = 'power_sensor'
CONF_POWER_SPEED_SENSOR = 'power_speed_sensor'
CONF_POWER_SPEED_MAP = 'power_speed_map'
CONF_POWER_SPEED_DELAY = 'power_speed_delay'

SPEED_OFF = "off"

def _nonnegative_finite(value):
    """Validate watt limits and settling time without accepting NaN/infinity."""
    value = vol.Coerce(float)(value)
    if not math.isfinite(value) or value < 0:
        raise vol.Invalid('Expected a finite, nonnegative number')
    return value


def _validate_power_speed_config(config):
    """Require explicit ranges and an on/off sensor for speed feedback."""
    if CONF_POWER_SPEED_SENSOR in config or CONF_POWER_SPEED_MAP in config:
        for key in (CONF_POWER_SENSOR, CONF_POWER_SPEED_SENSOR, CONF_POWER_SPEED_MAP):
            if key not in config:
                raise vol.Invalid('Power speed feedback requires ' + key)
        ranges = sorted(config[CONF_POWER_SPEED_MAP].values())
        if not ranges:
            raise vol.Invalid('power_speed_map must not be empty')
        for index, (lower, upper) in enumerate(ranges):
            if lower >= upper:
                raise vol.Invalid('Power ranges require min < max')
            if index and lower < ranges[index - 1][1]:
                raise vol.Invalid('Power ranges must not overlap')
    return config


PLATFORM_SCHEMA = vol.All(PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_UNIQUE_ID): cv.string,
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Required(CONF_DEVICE_CODE): cv.positive_int,
    vol.Required(CONF_CONTROLLER_DATA): cv.string,
    vol.Optional(CONF_DELAY, default=DEFAULT_DELAY): cv.string,
    vol.Optional(CONF_POWER_SENSOR): cv.entity_id,
    vol.Optional(CONF_POWER_SPEED_SENSOR): cv.entity_id,
    vol.Optional(CONF_POWER_SPEED_MAP): {
        str: vol.All([_nonnegative_finite], vol.Length(min=2, max=2))
    },
    vol.Optional(CONF_POWER_SPEED_DELAY, default=10): _nonnegative_finite,
}), _validate_power_speed_config)

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the IR Fan platform."""
    device_code = config.get(CONF_DEVICE_CODE)
    device_files_subdir = os.path.join('codes', 'fan')
    device_files_absdir = os.path.join(COMPONENT_ABS_DIR, device_files_subdir)

    if not os.path.isdir(device_files_absdir):
        os.makedirs(device_files_absdir)

    device_json_filename = str(device_code) + '.json'
    device_json_path = os.path.join(device_files_absdir, device_json_filename)

    if not os.path.exists(device_json_path):
        _LOGGER.warning("Couldn't find the device Json file. The component will " \
                        "try to download it from the GitHub repo.")

        try:
            codes_source = ("https://raw.githubusercontent.com/"
                            "smartHomeHub/SmartIR/master/"
                            "codes/fan/{}.json")

            await Helper.downloader(codes_source.format(device_code), device_json_path)
        except Exception:
            _LOGGER.error("There was an error while downloading the device Json file. " \
                          "Please check your internet connection or if the device code " \
                          "exists on GitHub. If the problem still exists please " \
                          "place the file manually in the proper directory.")
            return

    try:
        async with aiofiles.open(device_json_path, mode='r') as j:
            _LOGGER.debug(f"loading json file {device_json_path}")
            content = await j.read()
            device_data = json.loads(content)
            _LOGGER.debug(f"{device_json_path} file loaded")
    except Exception:
        _LOGGER.error("The device JSON file is invalid")
        return

    try:
        fan = SmartIRFan(hass, config, device_data)
    except vol.Invalid as error:
        _LOGGER.error("Invalid fan power speed configuration: %s", error)
        return
    async_add_entities([fan])

class SmartIRFan(FanEntity, RestoreEntity):
    def __init__(self, hass, config, device_data):
        self.hass = hass
        self._unique_id = config.get(CONF_UNIQUE_ID)
        self._name = config.get(CONF_NAME)
        self._device_code = config.get(CONF_DEVICE_CODE)
        self._controller_data = config.get(CONF_CONTROLLER_DATA)
        self._delay = config.get(CONF_DELAY)
        self._power_sensor = config.get(CONF_POWER_SENSOR)
        self._power_speed_sensor = config.get(CONF_POWER_SPEED_SENSOR)
        self._power_speed_map = config.get(CONF_POWER_SPEED_MAP, {})
        self._power_speed_delay = config.get(CONF_POWER_SPEED_DELAY, 10)
        self._pending_speed = None
        self._cancel_speed_sync = None

        self._manufacturer = device_data['manufacturer']
        self._supported_models = device_data['supportedModels']
        self._supported_controller = device_data['supportedController']
        self._commands_encoding = device_data['commandsEncoding']
        self._speed_list = device_data['speed']
        self._commands = device_data['commands']
        if self._power_speed_sensor and set(self._power_speed_map) != set(self._speed_list):
            raise vol.Invalid('power_speed_map must contain exactly the device speed strings')
        
        self._speed = SPEED_OFF
        self._direction = None
        self._last_on_speed = None
        self._oscillating = None
        self._support_flags = (
            FanEntityFeature.SET_SPEED
            | FanEntityFeature.TURN_OFF
            | FanEntityFeature.TURN_ON)

        if (DIRECTION_REVERSE in self._commands and \
            DIRECTION_FORWARD in self._commands):
            self._direction = DIRECTION_REVERSE
            self._support_flags = (
                self._support_flags | FanEntityFeature.DIRECTION)
        if ('oscillate' in self._commands):
            self._oscillating = False
            self._support_flags = (
                self._support_flags | FanEntityFeature.OSCILLATE)


        self._temp_lock = asyncio.Lock()
        self._on_by_remote = False

        #Init the IR/RF controller
        self._controller = get_controller(
            self.hass,
            self._supported_controller, 
            self._commands_encoding,
            self._controller_data,
            self._delay)

    async def async_added_to_hass(self):
        """Run when entity about to be added."""
        await super().async_added_to_hass()
    
        last_state = await self.async_get_last_state()

        if last_state is not None:
            if 'speed' in last_state.attributes:
                self._speed = last_state.attributes['speed']
            elif last_state.state == STATE_ON:
                percentage = last_state.attributes.get('percentage')
                if percentage is not None and 0 < percentage <= 100:
                    self._speed = percentage_to_ordered_list_item(self._speed_list, percentage)
                else:
                    self._speed = None
                    self._on_by_remote = True

            if self._speed not in [SPEED_OFF, *self._speed_list]:
                self._speed = None
                self._on_by_remote = last_state.state == STATE_ON

            #If _direction has a value the direction controls appears 
            #in UI even if SUPPORT_DIRECTION is not provided in the flags
            if ('direction' in last_state.attributes and \
                self._support_flags & FanEntityFeature.DIRECTION):
                self._direction = last_state.attributes['direction']

            if 'last_on_speed' in last_state.attributes:
                speed = last_state.attributes['last_on_speed']
                if speed in self._speed_list:
                    self._last_on_speed = speed

        if self._power_sensor:
            self.async_on_remove(async_track_state_change_event(
                self.hass, self._power_sensor, self._async_power_sensor_changed))
            self._sync_power_state()
        if self._power_speed_sensor:
            self.async_on_remove(async_track_state_change_event(
                self.hass, self._power_speed_sensor, self._async_power_speed_sensor_changed))
            self.async_on_remove(self._cancel_pending_speed)
            self._schedule_speed_sync()

    @property
    def unique_id(self):
        """Return a unique ID."""
        return self._unique_id

    @property
    def name(self):
        """Return the display name of the fan."""
        return self._name

    @property
    def state(self):
        """Return the current state."""
        if (self._on_by_remote or \
            self._speed in self._speed_list):
            return STATE_ON
        return SPEED_OFF

    @property
    def percentage(self):
        """Return speed percentage of the fan."""
        if (self._speed == SPEED_OFF):
            return 0
        if self._speed not in self._speed_list:
            return None

        return ordered_list_item_to_percentage(self._speed_list, self._speed)

    @property
    def speed_count(self):
        """Return the number of speeds the fan supports."""
        return len(self._speed_list)

    @property
    def oscillating(self):
        """Return the oscillation state."""
        return self._oscillating

    @property
    def current_direction(self):
        """Return the direction state."""
        return self._direction

    @property
    def last_on_speed(self):
        """Return the last non-idle speed."""
        return self._last_on_speed

    @property
    def supported_features(self):
        """Return the list of supported features."""
        return self._support_flags

    @property
    def extra_state_attributes(self):
        """Platform specific attributes."""
        return {
            'last_on_speed': self._last_on_speed,
            'device_code': self._device_code,
            'manufacturer': self._manufacturer,
            'supported_models': self._supported_models,
            'supported_controller': self._supported_controller,
            'commands_encoding': self._commands_encoding,
        }

    async def async_set_percentage(self, percentage: int):
        """Set the desired speed for the fan."""
        self._cancel_pending_speed()
        if (percentage == 0):
             self._speed = SPEED_OFF
        else:
            self._speed = percentage_to_ordered_list_item(
                self._speed_list, percentage)

        if not self._speed == SPEED_OFF:
            self._last_on_speed = self._speed

        await self.send_command()
        self.async_write_ha_state()

    async def async_oscillate(self, oscillating: bool) -> None:
        """Set oscillation of the fan."""
        self._oscillating = oscillating

        await self.send_command()
        self.async_write_ha_state()

    async def async_set_direction(self, direction: str):
        """Set the direction of the fan"""
        self._direction = direction

        if self._speed in self._speed_list:
            await self.send_command()

        self.async_write_ha_state()

    async def async_turn_on(self, percentage: int = None, preset_mode: str = None, **kwargs):
        """Turn on the fan."""
        if percentage is None:
            percentage = ordered_list_item_to_percentage(
                self._speed_list, self._last_on_speed or self._speed_list[0])

        await self.async_set_percentage(percentage)

    async def async_turn_off(self):
        """Turn off the fan."""
        await self.async_set_percentage(0)

    async def send_command(self):
        async with self._temp_lock:
            self._on_by_remote = False
            speed = self._speed
            direction = self._direction or 'default'
            oscillating = self._oscillating

            if speed == SPEED_OFF:
                command = self._commands['off']
            elif oscillating:
                command = self._commands['oscillate']
            else:
                if speed not in self._speed_list:
                    return
                command = self._commands[direction][speed] 

            try:
                await self._controller.send(command)
            except Exception as e:
                _LOGGER.exception(e)

    @callback
    def _async_power_sensor_changed(self, event: Event[EventStateChangedData]) -> None:
        """Handle power sensor changes."""
        self._sync_power_state()
        self._schedule_speed_sync()
        self.async_write_ha_state()

    @callback
    def _sync_power_state(self):
        """Apply current on/off feedback, including the initial sensor state."""
        state = self.hass.states.get(self._power_sensor)
        if state is None:
            return
        if state.state == STATE_ON and self._speed == SPEED_OFF:
            self._on_by_remote = True
            self._speed = None
        elif state.state == STATE_OFF:
            self._on_by_remote = False
            self._speed = SPEED_OFF

    def _speed_from_power(self):
        """Return a mapped speed only while on/off feedback confirms on."""
        if not self._power_speed_sensor:
            return None
        running = self.hass.states.get(self._power_sensor)
        power = self.hass.states.get(self._power_speed_sensor)
        if running is None or running.state != STATE_ON or power is None:
            return None
        try:
            watts = float(power.state)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(watts):
            return None
        for speed, (lower, upper) in self._power_speed_map.items():
            if lower <= watts < upper:
                return speed
        return None

    @callback
    def _cancel_pending_speed(self):
        """Cancel settling on invalid feedback, commands, or entity removal."""
        if self._cancel_speed_sync is not None:
            self._cancel_speed_sync()
            self._cancel_speed_sync = None
        self._pending_speed = None

    @callback
    def _async_power_speed_sensor_changed(self, event: Event[EventStateChangedData]) -> None:
        self._schedule_speed_sync()

    @callback
    def _schedule_speed_sync(self):
        """Require a continuously matching range, not an unchanged watt value."""
        speed = self._speed_from_power()
        if speed is None or speed == self._speed:
            self._cancel_pending_speed()
            return
        if speed == self._pending_speed:
            return
        self._cancel_pending_speed()
        self._pending_speed = speed
        self._cancel_speed_sync = async_call_later(
            self.hass, self._power_speed_delay, self._apply_power_speed)

    @callback
    def _apply_power_speed(self, now):
        """Update state only; never invoke a control service or send IR."""
        speed = self._pending_speed
        self._cancel_speed_sync = None
        self._pending_speed = None
        if speed is None or self._speed_from_power() != speed:
            return
        self._speed = speed
        self._last_on_speed = speed
        self._on_by_remote = True
        self.async_write_ha_state()
