"""Handle communication with supported Panasonic H&C devices."""

import asyncio
from collections.abc import Callable
import logging
import time

from bleak_retry_connector import establish_connection, BleakClientWithServiceCache
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from .panasonic_hc_proto import (
    FANSPEED,
    MODE,
    PanasonicBLEEnergySaving,
    PanasonicBLEErrorReq,
    PanasonicBLEFanMode,
    PanasonicBLEIconReq,
    PanasonicBLEMode,
    PanasonicBLEMonitorReq,
    PanasonicBLENanoe,
    PanasonicBLEParcel,
    PanasonicBLEPower,
    PanasonicBLEPowerReq,
    PanasonicBLEPowerReqHour,
    PanasonicBLEStatusReq,
    PanasonicBLETemp,
)

MIN_TEMP = 16
MAX_TEMP = 32  # FIXME: check these

BLE_CHAR_WRITE = "4d200002-eff3-4362-b090-a04cab3f1da0"
BLE_CHAR_NOTIFY = "4d200003-eff3-4362-b090-a04cab3f1da0"
CONSUMPTION_INTERVAL = 300

# Service-monitor "data number" (DN) codes read via field 0x2C to the OUTDOOR unit, from the
# Remote Controller Servicing Functions table (ECOi/PACi service manual). The unit returns
# "- - - -" (a sentinel) for codes it doesn't support.
#
# Each entry is (code, key, scale): the reply is a signed big-endian 16-bit raw value and the
# displayed value = raw * scale. Scaling is PER-CODE -- Panasonic's monitor table mixes
# whole-degree (x1) and tenth-degree (x0.1) fields, so there is no single global divisor.
# Hardware-confirmed: outdoor air (0x11) raw 6 == 6 C (x1); outdoor coil (0x06) raw -29 ==
# -2.9 C (x0.1, a healthy evaporator temp heating against 6 C ambient).
# Code->name from the ECOi R1 service manual "Remote Controller Servicing Functions" table
# (PACi shares the numbering; single-split uses only the "Unit No.1" column). Per-code scale
# is NOT documented anywhere -- it is non-uniform (air x1, coil x0.1) and must be calibrated
# against hardware, hence the sweep below. Scales marked "(unconf)" are best-guesses.
MONITOR_COMPRESSOR_CODE = 0x14  # CT2 compressor current -> "running" signal, polled often
MONITOR_COMPRESSOR_SCALE = 1.0  # amps; raw ~3 while running == ~3 A (unconf scale)
MONITOR_SENSOR_CODES = [
    (0x11, "outdoor_temp", 1.0),            # Outdoor air temp TO (confirmed 7 C)
    (0x06, "outdoor_coil_temp", 0.1),       # Outdoor coil/evap (confirmed -2.9 C; the ONLY x0.1 code)
    (0x03, "indoor_coil_temp", 1.0),        # Indoor heat-exch E1 (confirmed 12 C, x1)
    (0x02, "room_temp", 1.0),               # Intake / return-air temp TA (confirmed 13 C, x1)
    (0x0A, "outdoor_discharge_temp", 1.0),  # Discharge TD (confirmed x1: 28 C idle, rises when running)
    (0x0D, "hx_gas_temp", 1.0),             # Outdoor heat-exch gas temp (sweep-confirmed: 10 C, x1)
    (0x0E, "hx_liquid_temp", 1.0),          # Outdoor heat-exch liquid temp (sweep-confirmed: 6 C, x1)
    (0x15, "outdoor_eev", 1.0),             # EXPERIMENTAL: sweep read ~480 (idle) -- candidate for
                                            # the outdoor expansion-valve (MOV) step (0-480 pulses).
                                            # Keep only if it tracks compressor load; else drop.
]
# Dropped after the sweep: 0x08 (indoor EEV) and 0x1D (suction/LP-sat) return no data on this
# PACi single-split. The indoor temp codes 0x02-0x05 DO answer at the outdoor address, so the
# routing is correct -- EEV/suction simply aren't exposed over BLE here. All temps are x1 except
# 0x06 (the lone tenths field). The DEBUG sweep below still probes the full range for future
# exploration (e.g. 0x15 reads ~480, a possible outdoor expansion-valve step -- unconfirmed).
MONITOR_SENTINELS = (0x7FFF, -0x8000)  # "no data" markers

# Read-only calibration sweep: when DEBUG logging is enabled, probe this documented code range
# (0x02..0x22) once per diagnostic cycle and log each raw value. This reveals which codes the
# unit populates and lets per-code scaling be calibrated against a known state. It is a pure
# read (field 0x2C / op REQ, exactly like the app's Sensor Info screen) and is skipped entirely
# unless DEBUG is on, so it adds no traffic in normal operation.
MONITOR_SWEEP_RANGE = range(0x02, 0x23)

# Status-icon (SettingIcon mDn) codes polled via the 0x69 sub-23 query, mapped to the HVAC
# action they represent: 9/10 = preheating/preparing, 11 = defrost.
ICON_PREHEATING = (9, 10)
ICON_DEFROST = 11
ICON_CODES = (9, 10, 11)

_LOGGER = logging.getLogger(__name__)


def _descramble(data: bytes) -> bytes:
    """Reverse the link-layer XOR obfuscation to recover the plaintext frame.

    Mirrors the device scrambling (see PROTOCOL.md in the phc-rev-eng project): XOR every
    byte with 0x69, undo the cumulative byte chain, then XOR byte[0] with 0xCA. Used only
    for debug logging of raw frames; unlike PanasonicBLEParcel.parse it does no checksum or
    length validation, so it works on any frame including ones the parser would reject.
    """

    v = bytearray(data)
    for i in range(len(v)):
        v[i] ^= 0x69
    for i in range(len(v) - 1, 0, -1):
        v[i] ^= v[i - 1]
    if v:
        v[0] ^= 0xCA
    return bytes(v)


class PanasonicHCException(Exception):
    """PanasonicHC Exception."""


class Status:
    """Class representing current HVAC status."""

    def __init__(
        self,
        power: bool,
        mode: str,
        powersave: bool,
        curtemp: float,
        settemp: float,
        fanspeed: str,
        prohibited: bool = False,
        running: bool | None = None,
    ) -> None:
        """Initialise Status."""

        self.power = power
        self.mode = mode
        self.powersave = powersave
        self.curtemp = curtemp
        self.settemp = settemp
        self.fanspeed = fanspeed
        # Whether any operation is currently locked/restricted on the unit (RC lock /
        # central control). Not used for HVACAction; available for a future "locked" sensor.
        self.prohibited = prohibited
        # Compressor/thermo running (status byte[2]): True = actively heating/cooling,
        # False = idle/standby ("slight blow"), None = unknown. Drives HVACAction.
        self.running = running


class PanasonicHC:
    """Class representing the Panasonic Controller."""

    def __init__(self, ble_device: BLEDevice, mac_address: str) -> None:
        """Initialise Panasonic H&C Controller."""

        self.last_update = 0
        self.device = ble_device
        self.mac_address = mac_address
        self._on_update_callbacks: list[Callable] = []
        self._conn = None
        self._lock = asyncio.Lock()
        self.status = None
        self.curhour = None
        self.curindex = None
        self.consumption = [0] * 48
        self.outdoor_temp = None
        self.error_code = None
        self.nanoe = None
        # Service-monitor readings (key -> value) and compressor current (diagnostic).
        self.monitor: dict[str, float] = {}
        self.compressor_current = None
        self._monitor_pending: str | None = None
        # Status-icon flags (from 0x69 sub-23) -> HVAC action preheating/defrosting.
        self._icons: dict[int, bool] = {}
        self.preheating = None
        self.defrosting = None

    @property
    def is_connected(self) -> bool:
        """Return true if connected to thermostat."""

        return self._conn.is_connected

    def register_update_callback(self, on_update: Callable) -> None:
        """Register a callback to be called on updated data."""

        self._on_update_callbacks.append(on_update)

    def unregister_update_callback(self, on_update: Callable) -> None:
        """Unregister update callback."""

        if on_update in self._on_update_callbacks:
            self._on_update_callbacks.remove(on_update)

    async def async_connect(self) -> None:
        """Connect to thermostat."""

        try:
            self._conn = await establish_connection(
                BleakClientWithServiceCache,
                self.device,
                name=self.device.name or self.device.address
            )
            await asyncio.sleep(0.5)
            await self._conn.start_notify(BLE_CHAR_NOTIFY, self.on_notification)
            await asyncio.sleep(0.5)
            await self.async_get_status()
        except (BleakError, TimeoutError) as e:
            raise PanasonicHCException("Could not connect to Thermostat") from e

    async def async_disconnect(self) -> None:
        """Shutdown thermostat connection."""

        try:
            await self._conn.disconnect()
        except (BleakError, TimeoutError) as e:
            raise PanasonicHCException("Could not disconnect from Thermostat") from e

    async def async_get_status(self) -> None:
        """Query current status."""

        # always update status (the 0x81 reply carries the compressor/idle bit, byte[2])
        await self._async_write_command(PanasonicBLEStatusReq())

        # Poll the preheating/defrost status icons (0x69 sub-23) every cycle -> HVAC action
        # precedence. The unit answers these (active byte = 0 when the state is inactive), and
        # preheating in particular is short-lived, so they need to be polled at the status rate
        # to be caught rather than parked in the slow diagnostic block.
        for code in ICON_CODES:
            await asyncio.sleep(0.4)
            await self._async_write_command(PanasonicBLEIconReq(code))

        # update consumption + slower diagnostics if interval has passed
        now = time.time()
        if now > self.last_update + CONSUMPTION_INTERVAL:
            await asyncio.sleep(0.5)
            await self._async_write_command(PanasonicBLEPowerReq())
            await asyncio.sleep(0.5)
            await self._async_write_command(PanasonicBLEPowerReqHour())
            await asyncio.sleep(0.5)
            await self._async_write_command(PanasonicBLEErrorReq())
            await asyncio.sleep(0.5)
            await self._read_monitor(
                MONITOR_COMPRESSOR_CODE, "compressor_current", MONITOR_COMPRESSOR_SCALE
            )
            for code, key, scale in MONITOR_SENSOR_CODES:
                await asyncio.sleep(0.5)
                await self._read_monitor(code, key, scale)
            if _LOGGER.isEnabledFor(logging.DEBUG):
                await self._sweep_monitor_codes()
            self.last_update = now

    async def _sweep_monitor_codes(self) -> None:
        """Read-only calibration sweep over MONITOR_SWEEP_RANGE (DEBUG only).

        Logs the raw signed value of every code so a debug capture reveals which codes the unit
        populates and lets per-code scaling be calibrated. Values are logged, not stored as
        entities. Uses the same harmless 0x2C read as the named sensors."""

        for code in MONITOR_SWEEP_RANGE:
            _LOGGER.debug("monitor sweep -> code 0x%02x", code)
            self._monitor_pending = (f"sweep_0x{code:02x}", 1.0)
            await self._async_write_command(PanasonicBLEMonitorReq(code))
            await asyncio.sleep(1.0)  # generous spacing so each reply matches its request

    async def _read_monitor(self, code: int, key: str, scale: float) -> None:
        """Request one service-monitor code (0x2C). The reply is matched to (key, scale) in
        on_notification (one read in flight at a time, so request order = reply order)."""

        self._monitor_pending = (key, scale)
        await self._async_write_command(PanasonicBLEMonitorReq(code))
        await asyncio.sleep(0.8)  # allow the 0x2C reply to arrive and be recorded

    async def _async_write_command(self, command: PanasonicBLEParcel):
        """Write a command to the write characteristic."""

        if not self.is_connected:
            raise PanasonicHCException("Not Connected")

        data = command.encode()

        async with self._lock:
            try:
                await self._conn.write_gatt_char(BLE_CHAR_WRITE, data)
            except (BleakError, TimeoutError) as e:
                raise PanasonicHCException("Error during write") from e

    def on_notification(self, handle: BleakGATTCharacteristic, data: bytes) -> None:
        """Handle data from BLE GATT Notifications."""

        # Log the de-scrambled plaintext of every inbound frame (debug only). Handy for
        # protocol work: shows fields the parser ignores and the raw bytes of each frame.
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("BLE frame: %s", _descramble(data).hex(" "))

        try:
            do_callback = False
            parcel = PanasonicBLEParcel.parse(data=data)
            _LOGGER.debug("Received packet data: %s", parcel)
            for packet in parcel:
                if isinstance(packet, PanasonicBLEParcel.PanasonicBLEPacketStatus):
                    # Some packets do not have curtemp at this index, I'm not sure how to
                    # identify them, so we will do some sanity filtering
                    if self.status:
                        if not packet.curtemp or abs(packet.curtemp - self.status.curtemp) > 20:
                            packet.curtemp = self.status.curtemp

                    self.status = Status(
                        packet.power,
                        packet.mode.name,
                        packet.powersave,
                        packet.curtemp,
                        packet.temp,
                        packet.fanspeed.name,
                        packet.prohibited,
                        packet.running,
                    )
                    do_callback = True
                elif isinstance(
                    packet, PanasonicBLEParcel.PanasonicBLEPacketOutdoorTemp
                ):
                    if packet.temp is not None:
                        self.outdoor_temp = packet.temp
                        do_callback = True
                elif isinstance(
                    packet, PanasonicBLEParcel.PanasonicBLEPacketError
                ):
                    self.error_code = packet.error
                    do_callback = True
                elif isinstance(
                    packet, PanasonicBLEParcel.PanasonicBLEPacketNanoe
                ):
                    self.nanoe = packet.nanoe
                    do_callback = True
                elif packet.ptype == 0x2C and self._monitor_pending:
                    # Reply to a service-monitor read (matched to the pending key+scale).
                    key, scale = self._monitor_pending
                    self._monitor_pending = None
                    if len(packet.pdata) >= 2:
                        raw = int.from_bytes(packet.pdata[0:2], "big", signed=True)
                        # Calibration aid: log every monitor reply's raw value (and sentinel).
                        _LOGGER.debug(
                            "monitor %s: raw=%d (0x%04x)%s",
                            key, raw, raw & 0xFFFF,
                            " [no-data]" if raw in MONITOR_SENTINELS else "",
                        )
                        # Sweep reads are for logging/calibration only -- don't expose them.
                        if raw not in MONITOR_SENTINELS and not key.startswith("sweep_"):
                            # Displayed value = raw * per-code scale (see MONITOR_SENSOR_CODES).
                            value = round(raw * scale, 1)
                            self.monitor[key] = value
                            if key == "outdoor_temp":
                                self.outdoor_temp = value
                            elif key == "compressor_current":
                                self.compressor_current = value
                            do_callback = True
                elif (
                    packet.ptype == 0x69
                    and len(packet.pdata) >= 5
                    and packet.pdata[2] == 23
                ):
                    # Status-icon reply (0x69 sub-23): pdata[3]=icon mDn, pdata[4]=active.
                    self._icons[packet.pdata[3]] = packet.pdata[4] != 0
                    self.preheating = any(self._icons.get(c) for c in ICON_PREHEATING)
                    self.defrosting = bool(self._icons.get(ICON_DEFROST))
                    do_callback = True
                elif isinstance(
                    packet, PanasonicBLEParcel.PanasonicBLEPacketConsumption
                ):
                    if packet.hour is not None:
                        self.curhour = packet.hour
                    if packet.index is not None:
                        self.curindex = packet.index
                    if packet.values is not None:
                        for i, value in enumerate(packet.values):
                            offset = self.curhour + 24 - 1 - self.curindex
                            if offset < 0:
                                offset += 48
                            idx = offset + packet.pos + i
                            if idx >= 48:
                                idx -= 48
                            _LOGGER.debug("Writing %s to index %s", value, idx)
                            self.consumption[idx] = value
                        do_callback = True

            _LOGGER.debug(
                "Consumption: %s, curindex: %s, curhour: %s",
                self.consumption,
                self.curindex,
                self.curhour,
            )
            if do_callback:
                for callback in self._on_update_callbacks:
                    callback()
        except Exception as e:
            _LOGGER.error("Error parsing packet: %s", e)

    async def async_set_power(self, state: bool) -> None:
        """Set power state."""

        await self._async_write_command(PanasonicBLEPower(1 if state else 0))

    async def async_set_temperature(self, temp: float) -> None:
        """Set target temperature."""

        await self._async_write_command(PanasonicBLETemp(temp))

    async def async_set_mode(self, mode: str):
        """Set thermostat mode."""

        await self._async_write_command(PanasonicBLEMode(MODE[mode].value))

    async def async_set_fanmode(self, mode: str):
        """Set fan speed for the current operating mode."""

        # Target the current mode's fan profile (MODE values equal the 0x4C nibbles)
        # so fan speed also applies in fan_only/dry/auto, not just heat/cool.
        nibble = MODE[self.status.mode].value if self.status else MODE.heat.value
        await self._async_write_command(
            PanasonicBLEFanMode(FANSPEED[mode].value, nibble)
        )

    async def async_set_energysaving(self, state: bool):
        """Toggle EnergySaving mode."""

        await self._async_write_command(PanasonicBLEEnergySaving(state))

    async def async_set_nanoe(self, state: bool):
        """Turn nanoeX on or off (applies to all indoor units)."""

        await self._async_write_command(PanasonicBLENanoe(state))
        # Reflect optimistically; the device also confirms via a 0x5C notification.
        self.nanoe = state
        for callback in self._on_update_callbacks:
            callback()
