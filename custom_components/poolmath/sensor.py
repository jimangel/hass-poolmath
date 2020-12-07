import logging

import asyncio
import voluptuous as vol

from datetime import timedelta

from homeassistant.core import callback
from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import (
    CONF_NAME, CONF_URL, TEMP_FAHRENHEIT, ATTR_ICON, ATTR_NAME, ATTR_UNIT_OF_MEASUREMENT
)
from homeassistant.exceptions import PlatformNotReady
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect
import homeassistant.helpers.config_validation as cv

from .client import PoolMathClient
from .const import (DOMAIN, ATTRIBUTION, CONF_TARGET, CONF_TIMEOUT, 
                    DEFAULT_NAME, DEFAULT_TIMEOUT, ICON_POOL, ICON_GAUGE,
                    ATTR_ATTRIBUTION, ATTR_DESCRIPTION, ATTR_TARGET_SOURCE,
                    ATTR_LOG_TIMESTAMP, ATTR_TARGET_MIN, ATTR_TARGET_MAX)

LOG = logging.getLogger(__name__)

DATA_UPDATED = 'poolmath_data_updated'

SCAN_INTERVAL = timedelta(minutes=15)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_URL): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): cv.positive_int,
        
        # NOTE: targets are not really implemented, other than tfp
        vol.Optional(CONF_TARGET, default='tfp'): cv.string # targets/*.yaml file with min/max targets
    }
)

# FIXME: add strings translation support for names/descriptiongs/units?
# see https://www.troublefreepool.com/blog/2018/12/12/abcs-of-pool-water-chemistry/
POOL_MATH_SENSOR_SETTINGS = {
    'cc': { ATTR_NAME: 'CC',
            ATTR_UNIT_OF_MEASUREMENT: 'mg/L',
            ATTR_DESCRIPTION: 'Combined Chlorine',
            ATTR_ICON: ICON_GAUGE },
    'fc': { ATTR_NAME: 'FC',
            ATTR_UNIT_OF_MEASUREMENT: 'mg/L',
            ATTR_DESCRIPTION: 'Free Chlorine',
            ATTR_ICON: ICON_GAUGE },
    'ph': { ATTR_NAME: 'pH',
            ATTR_UNIT_OF_MEASUREMENT: 'pH',
            ATTR_DESCRIPTION: 'Acidity/Basicity',
            ATTR_ICON: ICON_GAUGE },
    'ta': { ATTR_NAME: 'TA',
            ATTR_UNIT_OF_MEASUREMENT: 'ppm',
            ATTR_DESCRIPTION: 'Total Alkalinity',
            ATTR_ICON: ICON_GAUGE },
    'ch': { ATTR_NAME: 'CH',
            ATTR_UNIT_OF_MEASUREMENT: 'ppm',
            ATTR_DESCRIPTION: 'Calcium Hardness',
            ATTR_ICON: ICON_GAUGE },
    'cya': { ATTR_NAME: 'CYA',
             ATTR_UNIT_OF_MEASUREMENT: 'ppm',
             ATTR_DESCRIPTION: 'Cyanuric Acid',
             ATTR_ICON: ICON_GAUGE },
    'salt': { ATTR_NAME: 'Salt',
              ATTR_UNIT_OF_MEASUREMENT: 'ppm',
              ATTR_DESCRIPTION: 'Salt',
              ATTR_ICON: ICON_GAUGE },
    'bor':  { ATTR_NAME: 'Borate',
              ATTR_UNIT_OF_MEASUREMENT: 'ppm',
              ATTR_DESCRIPTION: 'Borate',
              ATTR_ICON: ICON_GAUGE },
    'borate': { ATTR_NAME: 'Borate',
                ATTR_UNIT_OF_MEASUREMENT: 'ppm',
                ATTR_DESCRIPTION: 'Borate',
                ATTR_ICON: ICON_GAUGE },
    'csi':    { ATTR_NAME: 'CSI',
                ATTR_UNIT_OF_MEASUREMENT: 'CSI',
                ATTR_DESCRIPTION: 'Calcite Saturation Index',
                ATTR_ICON: ICON_GAUGE },
    'temp':   { ATTR_NAME: 'Temp',
                ATTR_UNIT_OF_MEASUREMENT: TEMP_FAHRENHEIT,
                ATTR_DESCRIPTION: 'Temperature',
                ATTR_ICON: 'mdi:coolant-temperature' }
}

# FIXME: this should be a profile probably, and allow user to select from
# a set of different profiles based on their needs (and make these ranges
# attributes of the sensors).  Profiles should be in YAML, not hardcoded here.
#
# FIXME: Load from targets/ based on targets config key...
# FIXME: targets should probably all be in code, since some values are computed based on other values
TFP_TARGET = 'tfp'
TFP_RECOMMENDED_TARGET_LEVELS = {
    'cc':     { ATTR_TARGET_MIN: 0,    ATTR_TARGET_MAX: 0.1  },
    'ph':     { ATTR_TARGET_MIN: 7.2,  ATTR_TARGET_MAX: 7.8, 'target': 7.4 },
    'ta':     { ATTR_TARGET_MIN: 50,   ATTR_TARGET_MAX: 90   },
#    'ch':     { ATTR_TARGET_MIN: 250,  ATTR_TARGET_MAX: 650  }, # with salt: 350-450 ppm
#    'cya':    { ATTR_TARGET_MIN: 30,   ATTR_TARGET_MAX: 50   }, # with salt: 70-80 ppm
    'salt':   { ATTR_TARGET_MIN: 3000, ATTR_TARGET_MAX: 3200, 'target': 3100 },
}

async def async_setup_platform(hass, config, async_add_entities_callback, discovery_info=None):
    """Set up the Pool Math sensor integration."""
    url = config.get(CONF_URL)
    name = config.get(CONF_NAME)
    timeout = config.get(CONF_TIMEOUT)

    client = PoolMathClient(url, name=name, timeout=timeout)

    # create the core Pool Math service sensor, which is responsible for updating all other sensors
    sensors = [ PoolMathServiceSensor(hass, config, "Pool Math Service", client, async_add_entities_callback) ]
    async_add_entities_callback(sensors, True)

def get_pool_targets(targets_key):
    if targets_key == TFP_TARGET:
        return TFP_RECOMMENDED_TARGET_LEVELS
    else:
        LOG.error(f"Only '{TFP_TARGET}' target currently supported, ignoring {CONF_TARGET}.")
        return None



class PoolMathServiceSensor(Entity):
    """Sensor monitoring the Pool Math cloud service and updating any related sensors"""

    def __init__(self, hass, config, name, poolmath_client, async_add_entities_callback):
        """Initialize the Pool Math service sensor."""
        self._hass = hass
        self._name = name
        self._attrs = {
            ATTR_ATTRIBUTION: ATTRIBUTION,
            CONF_URL: config.get(CONF_URL)
        }

        self._poolmath_client = poolmath_client
        self._async_add_entities_callback = async_add_entities_callback
        
        self._managed_sensors = {}

        self._update_state_from_client()

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def state(self):
        """Return the sensors currently being monitored from Pool Math."""
        return self._state

    @property
    def icon(self):
        return ICON_POOL

    @property
    def should_poll(self):
        return True

    def _update_state_from_client(self):
        # re-updated the state with list of sensors that are being monitored (in case any new sensors were discovered)
        self._state = self._poolmath_client.sensor_names
        self._attrs = {
            ATTR_LOG_TIMESTAMP: self._poolmath_client.latest_log_timestamp
        }

    async def async_update(self):
        """Get the latest data from the source and updates the state."""

        # trigger an update of this sensor (and all related sensors)
        client = self._poolmath_client
        soup = await client.async_update()

        # iterate through all the log entries and update sensor states
        timestamp = await client.process_log_entry_callbacks(soup, client._update_sensors_callback)
        self._attrs[ATTR_LOG_TIMESTAMP] = timestamp

        self._update_state_from_client()

    @property
    def device_state_attributes(self):
        """Return the any state attributes."""
        return self._attrs


    # FIXME: move all the below sensor specific code out of the client
    def get_sensor(self, sensor_type):
        sensor = self._managed_sensors.get(sensor_type, None)
        if sensor:
            return sensor

        pool_id = self._poolmath_client.pool_id

        config = POOL_MATH_SENSOR_SETTINGS.get(sensor_type, None)
        if config is None:
            LOG.warning(f"Unknown Pool Math sensor '{sensor_type}' discovered for {pool_id}")
            return None

        name = self._name + ' ' + config[ATTR_NAME]
        sensor = UpdatableSensor(self._hass, pool_id, name, config, sensor_type)
        self._managed_sensors[sensor_type] = sensor

        # register sensor with Home Assistant
        asyncio.run_coroutine_threadsafe(self._async_add_entities_callback([sensor], True), self._hass.loop)

        return sensor

    async def _update_sensors_callback(self, log_type, timestamp, state):
        sensor = self.get_sensor(log_type)
        if sensor and sensor.state != state:
            LOG.info(f"Pool Math returned updated {log_type}={state} (timestamp={timestamp})")
            sensor.inject_state(state, timestamp)

    @property
    def sensor_names(self):
        return self._managed_sensors.keys()





# FIXME: add timestamp for when the sensor/sample was taken
class UpdatableSensor(RestoreEntity):
    """Representation of a sensor whose state is kept up-to-date by an external data source."""

    def __init__(self, hass, pool_id, name, config, sensor_type):
        """Initialize the sensor."""
        super().__init__()

        self._hass = hass
        self._name = name
        self._config = config
        self._sensor_type = sensor_type
        self._state = None

        if pool_id:
            self._unique_id = f"poolmath_{pool_id}_{sensor_type}"
        else:
            self._unique_id = None
        
        self._attrs = {
            ATTR_ATTRIBUTION: ATTRIBUTION
        }

        # FIXME: use 'targets' configuration value and load appropriate yaml
        targets_source = TFP_TARGET
        targets_map = get_pool_targets(targets_source)
        if targets_map:
            self._targets = targets_map.get(sensor_type)
            if self._targets:
                self._attrs[ATTR_TARGET_SOURCE] = targets_source
                self._attrs.update(self._targets)

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def should_poll(self):
        return True # FIXME: get scheduled updates working below

    @property
    def unit_of_measurement(self):
        """Return the unit the value is expressed in."""
        return self._config[ATTR_UNIT_OF_MEASUREMENT]

    @property
    def state(self):
        """Return the state of the device."""
        return self._state

    @property
    def device_state_attributes(self):
        """Return the any state attributes."""
        return self._attrs

    @property
    def icon(self):
        return self._config['icon']

    def inject_state(self, state, timestamp):
        state_changed = self._state != state
        self._attrs[ATTR_LOG_TIMESTAMP] = timestamp

        if state_changed:
            self._state = state

            # FIXME: see should_poll
            # notify Home Assistant that the sensor has been updated
            #if (self.hass and self.schedule_update_ha_state):
            #    self.schedule_update_ha_state(True)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        
        # for this integration, restoring state really doesn't matter right now (but leaving code below in place)
        # Reason: all the sensors are dynamically created based on Pool Math service call, which always returns
        # the latest state as well!
        if self._state:
            return

        # on restart, attempt to restore previous state (SEE COMMENT ABOVE WHY THIS ISN'T USEFUL CURRENTLY)
        # (see https://aarongodfrey.dev/programming/restoring-an-entity-in-home-assistant/)
        state = await self.async_get_last_state()
        if not state:
            return
        self._state = state.state
        LOG.debug(f"Restored sensor {self._name} previous state {self._state}")

        # restore any attributes
        if ATTR_LOG_TIMESTAMP in state.attributes:
            self._attrs[ATTR_LOG_TIMESTAMP] = state.attributes[ATTR_LOG_TIMESTAMP]

        async_dispatcher_connect(
            self._hass, DATA_UPDATED, self._schedule_immediate_update
        )

    @callback
    def _schedule_immediate_update(self):
        self.async_schedule_update_ha_state(True)
