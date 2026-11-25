"""Sensors for Panasonic H&C."""

import datetime
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
from .panasonic_hc import MONTHS, PanasonicHC

_LOGGER = logging.getLogger(__name__)

# Monitor keys that no longer exist; their entities are removed from the registry at setup.
STALE_MONITOR_KEYS = ("outdoor_coil_temp",)

# Monitor keys renamed to the PACi service-manual names: old key -> new key. At setup the
# registry entry is moved to the new unique id, so the entity keeps its entity_id, history and
# statistics and only its name changes.
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

    # Remove the registry entries of retired monitor keys so an upgrade does not leave a
    # permanently unavailable entity behind. "outdoor_coil_temp" showed code 0x06 under a wrong
    # name and scale; that code is now "indoor_discharge_air_temp".
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
            # Both exist (this version ran, then an older one re-added the old key): keep the new.
            registry.async_remove(entity_id)
        else:
            _LOGGER.info("Relabelling sensor %s (%s -> %s)", entity_id, old_key, new_key)
            registry.async_update_entity(entity_id, new_unique_id=new_uid)

    async_add_entities(
        [
            PanasonicHCEnergy(thermostat),
            PanasonicHCMonthlyEnergy(thermostat),
            PanasonicHCOutdoorTemp(thermostat),
            PanasonicHCActiveAlarm(thermostat),
            PanasonicHCFault(thermostat),
            PanasonicHCFaultDescription(thermostat),
            # Diagnostic service-monitor readings (read via 0x2C to the outdoor unit). Codes,
            # names and scaling live in panasonic_hc.MONITOR_SENSOR_CODES.
            # Code 0x06 is the optional indoor discharge (supply) air thermistor "BL" on other
            # unit families; the PACi single-split table does not assign it, and such a unit
            # reports a fixed ~-29 C, so the entity is disabled by default.
            PanasonicHCMonitorSensor(
                thermostat, "indoor_discharge_air_temp", "Indoor Discharge Air Temperature",
                SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS,
                enabled_default=False,
            ),
            PanasonicHCMonitorSensor(
                thermostat, "indoor_coil_temp", "Indoor Coil Temperature",
                SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS,
            ),
            # Code 0x14, "current value (CTL1)" in the PACi table: zero whenever the compressor
            # is stopped (also with the indoor fan running), and the controller's energy figure
            # works out at about 0.23 kW per amp of it, so it is the outdoor unit's input
            # current rather than a compressor-only phase current. The key keeps the entity's
            # former name ("Compressor Current") so its unique id does not change.
            PanasonicHCMonitorSensor(
                thermostat, "compressor_current", "Outdoor Unit Current",
                SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE,
            ),
            # Names follow the PACi single-split service-manual table; the VRF table names some
            # codes differently (see panasonic_hc.MONITOR_SENSOR_CODES). All are whole units.
            # Code 0x02: the indoor unit's intake thermistor. It reads room air only while the
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
            # Code 0x0D: compressor suction line (TS) -- cold in cooling and in heating. The VRF
            # table calls this code "Heat Exchanger Gas Temperature".
            PanasonicHCMonitorSensor(
                thermostat, "suction_temp", "Compressor Suction Temperature",
                SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS,
            ),
            # Code 0x0E: outdoor heat exchanger (C1): the condenser in cooling (several K above
            # outdoor air under load, within about 1 K of it at low load and when stopped), the
            # evaporator in heating (below outdoor air).
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


class _ControllerEnergySensor(SensorEntity):
    """Shared base for the energy totals the controller keeps per period (day, month).

    Each reading is (period, total so far or None, the previous period's final total or
    None). Home Assistant's long-term statistics take a fall to below 90% of the last value
    as a meter reset, and any other fall as a correction, so:
      * within a period the value never steps down (a brief drop, for example to 0, would
        count the period twice);
      * at a period change the finished period's final total is reported first when it is
        the period just before (its last part only arrives with the change), and the new
        period is started from 0, so the reset is seen even when the new period is already
        close to the old total (after an outage);
      * nothing is applied while the entity is unavailable: the reading taken while
        (re)connecting is applied once it is available again, or a change it shows would be
        recorded as "unavailable" and lost.
    The period is not carried over a Home Assistant restart or a reload of the integration
    (an options change reloads it): a restored value can be up to 15 minutes old, and
    reporting a finished period from it could count that period twice. So when the period
    changed since the last recorded value, the first reading after a restart is taken as it
    is: the end of the old period is lost, and if the new period is already at 90% or more of
    the last recorded value, Home Assistant sees no reset and the new period's energy up to
    that point is lost too. (A link outage without a restart is handled as above.)
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_should_poll = False
    _attr_available = False

    def __init__(self, thermostat: PanasonicHC, unique_id: str) -> None:
        """Initialize the sensor entity."""

        self._thermostat = thermostat
        self._period = None  # controller period of the reported value
        self._attr_unique_id = unique_id
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
        # The first ring read after a (re)connect arrives before this signal. Apply it now,
        # so a period change it shows is recorded with real values and not as "unavailable".
        self._async_on_updated()
        self.async_write_ha_state()

    def _reading(self) -> tuple | None:
        """(period, total or None, previous period's total or None), or None if no data."""
        raise NotImplementedError

    def _follows(self, old, new) -> bool:
        """Whether period new comes straight after period old."""
        raise NotImplementedError

    @callback
    def _async_on_updated(self) -> None:
        """Handle updated data from the thermostat."""

        if not self._attr_available or (reading := self._reading()) is None:
            return  # a reading that arrives while unavailable is applied on connect
        period, total, finished = reading
        last = self._attr_native_value

        if period is None or self._period is None or last is None:
            # First reading, or periods can't be told apart (controller clock not set):
            # take the figure as it is.
            if total is None:
                return
            self._attr_native_value = total
        elif period != self._period:
            if self._follows(self._period, period) and finished is not None and finished > last:
                # The period that just ended, now complete.
                self._attr_native_value = last = finished
                self.async_write_ha_state()
            new = total if total is not None else 0.0
            if new > 0 and last > 0:
                # Start the new period from 0 so Home Assistant sees the reset.
                self._attr_native_value = 0.0
                self.async_write_ha_state()
            self._attr_native_value = new
        elif total is None:
            return
        elif total >= last:
            self._attr_native_value = total
        else:
            _LOGGER.debug(
                "%s for %s fell from %s to %s kWh; keeping %s",
                self._attr_name, period, last, total, last,
            )
        self._period = period
        self.async_write_ha_state()


class PanasonicHCEnergy(_ControllerEnergySensor):
    """Energy used in the controller's current day.

    The controller's own estimate (current times a configured supply voltage), summed over
    the hours of the controller's day stored so far. Each hour's figure is stored about half
    an hour after the hour ends on the controller's clock, and the day changes when the last
    hour's figure is stored (about 00:30 on that clock, which does not follow daylight saving).

    At a day change the finished day's last hour only arrives together with the new day, and
    for about an hour the new day's first slot still holds the figure from two days earlier;
    the controller marks that slot not valid until it is written, and such slots are left out
    (PanasonicHC._update_energy). See _ControllerEnergySensor for the rest.
    """

    _attr_name = "Daily Energy"

    def __init__(self, thermostat: PanasonicHC) -> None:
        """Initialize the sensor entity."""

        super().__init__(thermostat, dr.format_mac(thermostat.mac_address))

    def _reading(self) -> tuple | None:
        return self._thermostat.energy  # (day, today, yesterday)

    def _follows(self, old, new) -> bool:
        return new - old == datetime.timedelta(days=1)


class PanasonicHCMonthlyEnergy(_ControllerEnergySensor):
    """Energy used in the controller's current month (its 13-month ring, 0x69 sub-19 idx 18-25).

    A live total the controller keeps itself. It includes every hour (also the one Daily
    Energy loses when Home Assistant is down at a day change). Months follow the controller's
    clock (no daylight saving). How the controller behaves at a month change is not yet
    confirmed; see PanasonicHC._update_month for how a not-yet-cleared slot is handled.
    """

    _attr_name = "Energy This Month"

    def __init__(self, thermostat: PanasonicHC) -> None:
        """Initialize the sensor entity."""

        super().__init__(thermostat, f"{dr.format_mac(thermostat.mac_address)}_energy_month")

    def _reading(self) -> tuple | None:
        return self._thermostat.energy_month  # (slot, this month, last month)

    def _follows(self, old, new) -> bool:
        return (new - old) % MONTHS == 1


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
    """Outdoor air temperature (thermistor TO), in 0.1 C.

    Read from field 0x21, channel 0x20 of the outdoor unit. Monitor code 0x11 is the same
    sensor cut to whole degrees (14.7 C read as 14) and is used only while the 0.1 C reading
    is missing. It follows the outdoor air, with a shift of about a degree while the
    compressor runs.
    """

    _attr_name = "Outdoor Temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, thermostat: PanasonicHC) -> None:
        """Initialize the outdoor-temperature sensor."""

        super().__init__(thermostat, "outdoor_temp")

    @callback
    def _async_on_updated(self) -> None:
        if self._thermostat.outdoor_temp is not None:
            self._attr_native_value = self._thermostat.outdoor_temp
            self.async_write_ha_state()


def _unit_name(address: int | None) -> str | None:
    """Name a unit address as the protocol numbers them (1-8 indoor, 9-16 outdoor)."""

    if not address:
        return None
    if 1 <= address <= 8:
        return f"indoor unit {address}"
    if 9 <= address <= 16:
        return f"outdoor unit {address - 8}"
    return f"address 0x{address:02x}"


class PanasonicHCActiveAlarm(_PanasonicHCSensorBase):
    """The alarm the wall controller is showing now (0x69 sub-27 idx 5); "none" when clear.

    Unlike Fault Code, which is the newest alarm-HISTORY entry and stays until the history is
    cleared, this clears when the alarm does. The "unit" attribute is the address the
    controller reports with it (idx 6); its meaning is inferred, not confirmed."""

    _attr_name = "Active Alarm"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:alert-outline"

    def __init__(self, thermostat: PanasonicHC) -> None:
        """Initialize the active-alarm sensor."""

        super().__init__(thermostat, "active_alarm")

    @callback
    def _async_on_updated(self) -> None:
        if (code := self._thermostat.alarm_code) is None:
            return
        active = code != "A00"
        self._attr_native_value = code if active else "none"
        attrs = {}
        if active:
            attrs["description"] = describe_fault(code)
            if unit := _unit_name(self._thermostat.alarm_unit):
                attrs["unit"] = unit
        self._attr_extra_state_attributes = attrs
        self.async_write_ha_state()


class PanasonicHCFault(_PanasonicHCSensorBase):
    """Most-recent alarm-HISTORY entry (field 0x27); 'A00' means none.

    It keeps showing an alarm after it has cleared, until the history is cleared; Active
    Alarm is the live one."""

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
