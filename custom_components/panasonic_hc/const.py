"""Constants for the Panasonic H&C integration."""

DOMAIN = "panasonic_hc"

MANUFACTURER = "Panasonic"
MODEL = "CZ-RTC6BLW"

SIGNAL_THERMOSTAT_DISCONNECTED = f"{DOMAIN}.thermostat_disconnected"
SIGNAL_THERMOSTAT_CONNECTED = f"{DOMAIN}.thermostat_connected"

# Poll-interval options (seconds), configurable via the Options flow.
# Short = the status + preheating/defrost icon poll; Long = the consumption + diagnostics poll.
CONF_SHORT_POLL_INTERVAL = "short_poll_interval"
CONF_LONG_POLL_INTERVAL = "long_poll_interval"
DEFAULT_SHORT_POLL_INTERVAL = 10
DEFAULT_LONG_POLL_INTERVAL = 300
