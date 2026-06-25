"""Sensors for Panasonic H&C."""

import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
import homeassistant.helpers.device_registry as dr
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
from .panasonic_hc import PanasonicHC

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Initialise sensor platform."""

    thermostat: PanasonicHC = hass.data[DOMAIN][config_entry.entry_id]

    async_add_entities(
        [
            PanasonicHCEnergy(thermostat),
            PanasonicHCOutdoorTemp(thermostat),
            PanasonicHCFault(thermostat),
        ],
    )


class PanasonicHCEnergy(SensorEntity):
    """Sensor entity to represent daily power usage."""

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

        if self._thermostat.curhour is not None:
            today = sum(
                self._thermostat.consumption[24 : 24 + self._thermostat.curhour]
            )
            self._attr_native_value = today
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
    """Outdoor temperature reported by the unit (field 0x21).

    NOTE: the wire decode (signed BE16 / 10 °C) is confirmed, but which sensor the unit
    reports here is not verified for all models, so the value may need a hardware check.
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
