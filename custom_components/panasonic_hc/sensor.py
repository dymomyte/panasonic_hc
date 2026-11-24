"""Sensors for Panasonic H&C."""

import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfEnergy,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
import homeassistant.helpers.device_registry as dr
import homeassistant.helpers.entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    MANUFACTURER,
    MODEL,
    SIGNAL_THERMOSTAT_CONNECTED,
    SIGNAL_THERMOSTAT_DISCONNECTED,
)
from .fault_codes import describe as describe_fault
from .panasonic_hc import PanasonicHC

_LOGGER = logging.getLogger(__name__)

# Monitor keys that no longer exist; their entities are removed from the registry at setup.
STALE_MONITOR_KEYS = ("outdoor_coil_temp",)

# Monitor keys renamed when their labels were corrected against the PACi service-manual table:
# old key -> new key. The registry entry is moved to the new unique id, so the entity keeps
# its entity_id, history and statistics and only its name changes.
RENAMED_MONITOR_KEYS = {
    "room_temp": "return_air_temp",
    "hx_gas_temp": "suction_temp",
    "hx_liquid_temp": "outdoor_hx_temp",
}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Initialise sensor platform."""

    thermostat: PanasonicHC = hass.data[DOMAIN][config_entry.entry_id]

    # Monitor code 0x06 used to be exposed as "Outdoor Coil Temperature" (wrong sensor, wrong
    # scale). It now has a new key, so remove the old registry entry instead of leaving a
    # permanently unavailable entity behind on upgrade.
    registry = er.async_get(hass)
    for stale_key in STALE_MONITOR_KEYS:
        stale_uid = f"{dr.format_mac(thermostat.mac_address)}_monitor_{stale_key}"
        if entity_id := registry.async_get_entity_id("sensor", DOMAIN, stale_uid):
            _LOGGER.info("Removing retired sensor %s", entity_id)
            registry.async_remove(entity_id)
    mac = dr.format_mac(thermostat.mac_address)
    for old_key, new_key in RENAMED_MONITOR_KEYS.items():
        old_uid, new_uid = f"{mac}_monitor_{old_key}", f"{mac}_monitor_{new_key}"
        if not (entity_id := registry.async_get_entity_id("sensor", DOMAIN, old_uid)):
            continue
        if registry.async_get_entity_id("sensor", DOMAIN, new_uid):
            # Both exist (the new version ran once, then an older one): keep the new entry.
            registry.async_remove(entity_id)
        else:
            _LOGGER.info("Relabelling sensor %s (%s -> %s)", entity_id, old_key, new_key)
            registry.async_update_entity(entity_id, new_unique_id=new_uid)

    async_add_entities(
        [
            PanasonicHCEnergy(thermostat),
            PanasonicHCOutdoorTemp(thermostat),
            PanasonicHCFault(thermostat),
            PanasonicHCFaultDescription(thermostat),
            # Diagnostic service-monitor readings (read via 0x2C to the outdoor unit). Codes,
            # names and scaling live in panasonic_hc.MONITOR_SENSOR_CODES.
            # Code 0x06 is the optional indoor discharge (supply) air thermistor "BL" on other
            # unit families; the PACi single-split table does not assign it, and such a unit
            # reports a fixed ~-29 C, so the entity is disabled by default. (It was once
            # mislabelled "Outdoor Coil Temperature".)
            PanasonicHCMonitorSensor(
                thermostat, "indoor_discharge_air_temp", "Indoor Discharge Air Temperature",
                SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS,
                enabled_default=False,
            ),
            PanasonicHCMonitorSensor(
                thermostat, "indoor_coil_temp", "Indoor Coil Temperature",
                SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS,
            ),
            # Code 0x14, "current value (CTL1)" in the PACi table. Zero whenever the compressor
            # is stopped (also with the indoor fan running), and about 0.23 kW per amp against
            # the unit's own kWh figure, so it is the outdoor unit's input current rather than
            # a compressor-only phase current. Was named "Compressor Current"; key unchanged.
            PanasonicHCMonitorSensor(
                thermostat, "compressor_current", "Outdoor Unit Current",
                SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE,
            ),
            # Names follow the PACi single-split service-manual table (see
            # panasonic_hc.MONITOR_SENSOR_CODES); all whole units.
            # Code 0x02: the indoor unit's intake thermistor. It reads house air only while the
            # indoor fan runs; with the fan off it reads the space the unit sits in (several
            # degrees off the rooms on a ducted install). The room temperature the unit
            # regulates on is the climate entity's current temperature, a different sensor.
            PanasonicHCMonitorSensor(
                thermostat, "return_air_temp", "Return Air Temperature",
                SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS,
            ),
            # Code 0x0A: compressor discharge pipe (TD), the hottest point of the circuit.
            PanasonicHCMonitorSensor(
                thermostat, "outdoor_discharge_temp", "Compressor Discharge Temperature",
                SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS,
            ),
            # Code 0x0D: compressor suction line (TS) -- cold in cooling and in heating. It was
            # labelled "Heat Exchanger Gas Temperature" from the VRF table.
            PanasonicHCMonitorSensor(
                thermostat, "suction_temp", "Compressor Suction Temperature",
                SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS,
            ),
            # Code 0x0E: outdoor heat exchanger (C1): the condenser in cooling (several K above
            # outdoor air under load, within about 1 K of it at low load and when stopped), the
            # evaporator in heating (below outdoor air). It was labelled "Heat Exchanger Liquid
            # Temperature".
            PanasonicHCMonitorSensor(
                thermostat, "outdoor_hx_temp", "Outdoor Heat Exchanger Temperature",
                SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS,
            ),
            # Code 0x15: outdoor electronic expansion valve (MOV) position in pulses; 480 is
            # fully open, where it parks when the compressor stops. No device class.
            PanasonicHCMonitorSensor(
                thermostat, "outdoor_eev", "Outdoor Expansion Valve Position", None, "steps",
            ),
        ],
    )


class PanasonicHCEnergy(SensorEntity):
    """Sensor entity to represent daily power usage.

    The value is the controller's own estimate (its app calls it "estimated energy
    consumption": current times a configured supply voltage), summed over the hours of the
    controller's day completed so far. Each hour appears about half an hour after it ends.

    Two known defects, both at the daily rollover, seen in five months of history:
      * the last hour of the controller's day is never counted, and
      * for the first hour of a new day the value is that hour's figure from two days
        earlier (the ring slot not yet overwritten), which Home Assistant keeps in its
        long-term total whenever the real figure that replaces it is smaller (about 10 kWh
        over five months, roughly 1%).
    The cause is not yet known: it may be how the controller reports its hour and ring
    pointer across the rollover, or how they are mapped here. The recorded states show the
    hour is never 0 in that first hour, so the obvious fix
    (report yesterday's total while the hour is 0) would never run; a DEBUG capture of the
    0x69 sub-19 frames across the rollover is needed before changing this.
    """

    _attr_name = "Daily Energy"
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_should_poll = False
    _attr_available = False

    def __init__(self, thermostat: PanasonicHC) -> None:
        """Initialize the sensor entity."""

        self._thermostat = thermostat
        self._attr_unique_id = dr.format_mac(thermostat.mac_address)
        self._attr_device_info = DeviceInfo(
            name=f"{MODEL}_{thermostat.mac_address[-8:].replace(':','')}",
            manufacturer=MANUFACTURER,
            model=MODEL,
            connections={(CONNECTION_BLUETOOTH, thermostat.mac_address)},
        )

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""

        self._thermostat.register_update_callback(self._async_on_updated)

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_THERMOSTAT_DISCONNECTED}_{self._thermostat.mac_address}",
                self._async_on_disconnected,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_THERMOSTAT_CONNECTED}_{self._thermostat.mac_address}",
                self._async_on_connected,
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""

        self._thermostat.unregister_update_callback(self._async_on_updated)

    @callback
    def _async_on_disconnected(self) -> None:
        self._attr_available = False
        self.async_write_ha_state()

    @callback
    def _async_on_connected(self) -> None:
        self._attr_available = True
        self.async_write_ha_state()

    @callback
    def _async_on_updated(self) -> None:
        """Handle updated data from the thermostat."""

        curhour = self._thermostat.curhour
        if curhour is not None:
            # consumption[0:24] = yesterday by hour, [24:48] = today by hour.
            total = sum(self._thermostat.consumption[24 : 24 + curhour])
            self._attr_native_value = round(total, 1)
            self.async_write_ha_state()


class _PanasonicHCSensorBase(SensorEntity):
    """Shared lifecycle for the extra Panasonic H&C sensors (device wiring + availability)."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_available = False

    def __init__(self, thermostat: PanasonicHC, key: str) -> None:
        """Initialize the sensor entity."""

        self._thermostat = thermostat
        self._attr_unique_id = f"{dr.format_mac(thermostat.mac_address)}_{key}"
        self._attr_device_info = DeviceInfo(
            name=f"{MODEL}_{thermostat.mac_address[-8:].replace(':','')}",
            manufacturer=MANUFACTURER,
            model=MODEL,
            connections={(CONNECTION_BLUETOOTH, thermostat.mac_address)},
        )

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""

        self._thermostat.register_update_callback(self._async_on_updated)
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_THERMOSTAT_DISCONNECTED}_{self._thermostat.mac_address}",
                self._async_on_disconnected,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_THERMOSTAT_CONNECTED}_{self._thermostat.mac_address}",
                self._async_on_connected,
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""

        self._thermostat.unregister_update_callback(self._async_on_updated)

    @callback
    def _async_on_disconnected(self) -> None:
        self._attr_available = False
        self.async_write_ha_state()

    @callback
    def _async_on_connected(self) -> None:
        self._attr_available = True
        self.async_write_ha_state()

    @callback
    def _async_on_updated(self) -> None:
        """Subclasses override to copy state from the thermostat."""


class PanasonicHCOutdoorTemp(_PanasonicHCSensorBase):
    """Outdoor air temperature (monitor code 0x11, TO), in whole degrees.

    Checked against five months of history: it follows the outdoor air at the house, with a
    shift of about a degree while the compressor runs.
    """

    _attr_name = "Outdoor Temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, thermostat: PanasonicHC) -> None:
        """Initialize the outdoor-temperature sensor."""

        super().__init__(thermostat, "outdoor_temp")

    @callback
    def _async_on_updated(self) -> None:
        if self._thermostat.outdoor_temp is not None:
            self._attr_native_value = self._thermostat.outdoor_temp
            self.async_write_ha_state()


class PanasonicHCFault(_PanasonicHCSensorBase):
    """Most-recent fault/alert code (field 0x27); 'A00' means no fault."""

    _attr_name = "Fault Code"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, thermostat: PanasonicHC) -> None:
        """Initialize the fault-code sensor."""

        super().__init__(thermostat, "fault")

    @callback
    def _async_on_updated(self) -> None:
        if self._thermostat.error_code is not None:
            self._attr_native_value = self._thermostat.error_code
            self.async_write_ha_state()


class PanasonicHCFaultDescription(_PanasonicHCSensorBase):
    """Human-readable description of the most-recent fault code (field 0x27)."""

    _attr_name = "Fault Description"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, thermostat: PanasonicHC) -> None:
        """Initialize the fault-description sensor."""

        super().__init__(thermostat, "fault_description")

    @callback
    def _async_on_updated(self) -> None:
        if self._thermostat.error_code is not None:
            self._attr_native_value = describe_fault(self._thermostat.error_code)
            self.async_write_ha_state()


class PanasonicHCMonitorSensor(_PanasonicHCSensorBase):
    """Diagnostic sensor backed by a service-monitor reading (thermostat.monitor[key])."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, thermostat: PanasonicHC, key: str, name: str,
                 device_class, unit, enabled_default: bool = True) -> None:
        """Initialize a monitor-backed sensor."""

        super().__init__(thermostat, f"monitor_{key}")
        self._key = key
        self._attr_name = name
        self._attr_entity_registry_enabled_default = enabled_default
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit

    @callback
    def _async_on_updated(self) -> None:
        if self._key in self._thermostat.monitor:
            self._attr_native_value = self._thermostat.monitor[self._key]
            self.async_write_ha_state()
