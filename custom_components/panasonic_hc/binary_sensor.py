"""Binary sensors for Panasonic H&C."""

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
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


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Initialise binary sensor platform."""

    thermostat: PanasonicHC = hass.data[DOMAIN][config_entry.entry_id]

    async_add_entities([PanasonicHCFanStandby(thermostat)])


class PanasonicHCFanStandby(BinarySensorEntity):
    """The wall controller's standby icon (status byte[2] bit 0x02).

    On while "the indoor unit is stopped or slight blow operation is in process": the indoor
    fan is held. In heating that happens whenever the compressor is off; in cooling the fan
    keeps running with the compressor off, so it stays off there. With the unit switched off
    it reads off in cooling; whether it does after heating is not known. The bit is shown as
    reported, without regard to power. Unknown until the first status reply.
    """

    _attr_name = "Indoor Fan Standby"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:fan-clock"
    _attr_should_poll = False
    _attr_available = False

    def __init__(self, thermostat: PanasonicHC) -> None:
        """Initialize the binary sensor."""

        self._thermostat = thermostat
        self._attr_unique_id = f"{dr.format_mac(thermostat.mac_address)}_fan_standby"
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

        if (status := self._thermostat.status) is None:
            return
        # Status.running is True when the bit is clear (fan not held).
        self._attr_is_on = None if status.running is None else not status.running
        self.async_write_ha_state()
