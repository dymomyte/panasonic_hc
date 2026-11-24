"""The Panasonic H&C integration."""

from __future__ import annotations

import asyncio
from functools import partial
import logging

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    CONF_LONG_POLL_INTERVAL,
    CONF_SHORT_POLL_INTERVAL,
    DEFAULT_LONG_POLL_INTERVAL,
    DEFAULT_SHORT_POLL_INTERVAL,
    DOMAIN,
    SIGNAL_THERMOSTAT_CONNECTED,
    SIGNAL_THERMOSTAT_DISCONNECTED,
)
from .panasonic_hc import PanasonicHC, PanasonicHCException

PLATFORMS: list[Platform] = [Platform.CLIMATE, Platform.SENSOR, Platform.SWITCH]

# hass.data key: entry_id -> the entry's poll task, so unload can stop it before disconnecting.
DATA_POLL_TASKS = f"{DOMAIN}_poll_tasks"

type PanasonicHCConfigEntry = ConfigEntry[PanasonicHC]  # noqa: F821

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Panasonic H&C from a config entry."""

    mac_address: str | None = entry.unique_id

    device = bluetooth.async_ble_device_from_address(
        hass, mac_address.upper(), connectable=True
    )

    if device is None:
        raise ConfigEntryNotReady(f"[{mac_address}] Device could not be found")

    short_interval = int(
        entry.options.get(CONF_SHORT_POLL_INTERVAL, DEFAULT_SHORT_POLL_INTERVAL)
    )
    long_interval = int(
        entry.options.get(CONF_LONG_POLL_INTERVAL, DEFAULT_LONG_POLL_INTERVAL)
    )

    thermostat = PanasonicHC(
        ble_device=device,
        mac_address=mac_address,
        short_interval=short_interval,
        long_interval=long_interval,
        # Re-resolved on every (re)connect: the controller may now be reachable through a
        # different adapter or Bluetooth proxy than the one that saw it at setup.
        ble_device_callback=partial(
            bluetooth.async_ble_device_from_address,
            hass,
            mac_address.upper(),
            connectable=True,
        ),
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = thermostat

    entry.async_on_unload(entry.add_update_listener(update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    hass.data.setdefault(DATA_POLL_TASKS, {})[entry.entry_id] = (
        entry.async_create_background_task(
            hass, _async_run_thermostat(hass, thermostat), entry.entry_id
        )
    )

    return True


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle config entry update."""

    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""

    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        thermostat: PanasonicHC = hass.data[DOMAIN].pop(entry.entry_id)
        # Stop the poll/reconnect loop first so it cannot race the disconnect (or try to
        # reconnect behind it), then close the link. A failed disconnect must not fail the
        # unload: the platforms are already gone and the entry would be left stuck.
        if task := hass.data.get(DATA_POLL_TASKS, {}).pop(entry.entry_id, None):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        try:
            await thermostat.async_disconnect()
        except PanasonicHCException as e:
            _LOGGER.warning(
                "[%s] Error disconnecting from device: %s", thermostat.mac_address, e
            )

    return unload_ok


async def _async_run_thermostat(hass: HomeAssistant, thermostat: PanasonicHC) -> None:
    """Run the thermostat."""

    await _async_reconnect_thermostat(hass, thermostat)

    while True:
        try:
            await thermostat.async_get_status()
        except PanasonicHCException as e:
            if not thermostat.is_connected:
                _LOGGER.error(
                    "[%s] PanasonicHC device disconnected", thermostat.mac_address
                )

                async_dispatcher_send(
                    hass, f"{SIGNAL_THERMOSTAT_DISCONNECTED}_{thermostat.mac_address}"
                )
                await _async_reconnect_thermostat(hass, thermostat)
                continue

            _LOGGER.error(
                "[%s] Error updating PanasonicHC device %s", thermostat.mac_address, e
            )

        await asyncio.sleep(thermostat.short_interval)


async def _async_reconnect_thermostat(
    hass: HomeAssistant, thermostat: PanasonicHC
) -> None:
    """Reconnect thermostat."""

    while True:
        try:
            await thermostat.async_connect()
        except PanasonicHCException:
            await asyncio.sleep(10)
            continue

        _LOGGER.debug("[%s] PanasonicHC device connected", thermostat.mac_address)

        async_dispatcher_send(
            hass, f"{SIGNAL_THERMOSTAT_CONNECTED}_{thermostat.mac_address}"
        )

        return
