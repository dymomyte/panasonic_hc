"""Config flow for Panasonic H&C."""

from __future__ import annotations

import logging
from typing import Any

from habluetooth import BluetoothServiceInfoBleak
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_MAC
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.device_registry import format_mac

from .const import (
    CONF_LONG_POLL_INTERVAL,
    CONF_SHORT_POLL_INTERVAL,
    DEFAULT_LONG_POLL_INTERVAL,
    DEFAULT_SHORT_POLL_INTERVAL,
    DOMAIN,
    MODEL,
)

SCHEMA_MAC = vol.Schema({vol.Required(CONF_MAC): str})

_LOGGER = logging.getLogger(__name__)


class PanasonicHCConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Panasonic H&C controller."""

    def __init__(self) -> None:
        """Initialise config flow."""
        self.mac_address: str
        super().__init__()

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> PanasonicHCOptionsFlow:
        """Return the options flow handler."""

        return PanasonicHCOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle flow initiated by user."""

        errors: dict[str, str] = {}
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=SCHEMA_MAC,
                errors=errors,
            )

        self.mac_address = format_mac(user_input[CONF_MAC])

        if not validate_mac(self.mac_address):
            errors[CONF_MAC] = "invalid_mac_address"
            return self.async_show_form(
                step_id="user",
                data_schema=SCHEMA_MAC,
                errors=errors,
            )

        await self.async_set_unique_id(self.mac_address)
        self._abort_if_unique_id_configured(updates=user_input)

        return self.async_create_entry(
            title=f"{MODEL}_{self.mac_address[-8:].replace(':','')}", data={}
        )

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle bluetooth discovery phase."""

        _LOGGER.info("Discovered Bluetooth Device")

        self.mac_address = format_mac(discovery_info.address)

        await self.async_set_unique_id(self.mac_address)
        self._abort_if_unique_id_configured()

        self.context.update({"title_placeholders": {CONF_MAC: self.mac_address}})

        return await self.async_step_init()

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle flow start."""

        if user_input is None:
            return self.async_show_form(
                step_id="init", description_placeholders={CONF_MAC: self.mac_address}
            )

        await self.async_set_unique_id(self.mac_address)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=f"{MODEL}_{self.mac_address[-8:].replace(':','')}",
            data={},
        )


class PanasonicHCOptionsFlow(OptionsFlow):
    """Handle the options (poll intervals) for a Panasonic H&C entry."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the poll-interval options."""

        if user_input is not None:
            # NumberSelector yields floats; store plain ints.
            return self.async_create_entry(
                data={
                    CONF_SHORT_POLL_INTERVAL: int(user_input[CONF_SHORT_POLL_INTERVAL]),
                    CONF_LONG_POLL_INTERVAL: int(user_input[CONF_LONG_POLL_INTERVAL]),
                }
            )

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SHORT_POLL_INTERVAL,
                    default=options.get(
                        CONF_SHORT_POLL_INTERVAL, DEFAULT_SHORT_POLL_INTERVAL
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=5,
                        max=600,
                        step=1,
                        unit_of_measurement="seconds",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_LONG_POLL_INTERVAL,
                    default=options.get(
                        CONF_LONG_POLL_INTERVAL, DEFAULT_LONG_POLL_INTERVAL
                    ),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=30,
                        max=3600,
                        step=1,
                        unit_of_measurement="seconds",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)


def validate_mac(mac: str) -> bool:
    """Return whether or not given value is a valid MAC address."""

    return bool(
        mac
        and len(mac) == 17
        and mac.count(":") == 5
        and all(int(part, 16) < 256 for part in mac.split(":") if part)
    )
