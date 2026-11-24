"""Handle communication with supported Panasonic H&C devices."""

import asyncio
from collections.abc import Callable
import logging
import time

from bleak_retry_connector import establish_connection, BleakClientWithServiceCache
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from .const import DEFAULT_LONG_POLL_INTERVAL, DEFAULT_SHORT_POLL_INTERVAL
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
    PanasonicBLEReadReq,
    PanasonicBLEStatusReq,
    PanasonicBLETemp,
)

MIN_TEMP = 16
MAX_TEMP = 32  # FIXME: check these

# Room ("current") temperature sanity filter. Some 0x81 frames don't carry the room
# temperature at byte 5, and there is no known way to tell them apart, so a reading is only
# accepted if it is physically plausible and not a sudden jump from the last accepted one. A
# jump that keeps repeating is real (or the stored value was the bad one) and is accepted
# after CURTEMP_JUMP_CONFIRM consistent readings, so a bad value can never stick for good.
CURTEMP_MIN = -10.0
CURTEMP_MAX = 50.0
CURTEMP_MAX_STEP = 20.0
CURTEMP_JUMP_CONFIRM = 3

BLE_CHAR_WRITE = "4d200002-eff3-4362-b090-a04cab3f1da0"
BLE_CHAR_NOTIFY = "4d200003-eff3-4362-b090-a04cab3f1da0"

# Service-monitor "item codes" read via field 0x2C (addressed to the OUTDOOR unit): the wired
# controller's "Sensor Temperature Display Function" / "Sensor info". The numbering depends on
# the product family. This is the PACi SINGLE-SPLIT table, printed identically in the PACi
# service manuals from 2016 on (e.g. S-200/250PE4E + U-200/250PZH4E8, p.6-19):
#   indoor   02 intake temp | 03 heat exchanger E1 | 04 heat exchanger E2 | 05-08 not used
#   outdoor  0A discharge temp TD | 0D intake (compressor SUCTION) temp TS | 0E outdoor heat
#            exchanger C1 | 0F outdoor heat exchanger C2 | 11 outdoor air TO | 13 current value
#            CTL2 | 14 current value CTL1 | 15 outdoor MV (expansion valve) value | 19 frequency
# The names used here until now came from the VRF (ECOi) table in the CZ-RTC4 manual, which
# differs in the outdoor block: there 0D/0E are "heat exchanger gas/liquid 1" and 06 is an
# indoor discharge-air thermistor. On PACi 0D is the suction line, which five months of
# history confirm: it is cold in cooling AND heating, sits slightly above the evaporator
# (median +1 K in cooling, +2.5 K in heating), close to the 0-2 K superheat the manual says the
# expansion valve regulates to, and falls below -30 C in defrost. No table gives units; the manuals' worked example reads "0085" as 85 C, and the
# recorded data agree with whole units for every code below.
#
# Each entry is (code, key, scale): the reply is a signed big-endian 16-bit raw value and the
# displayed value = raw * scale.
#
# Code 0x06 is not assigned on PACi (it is the optional indoor discharge-air thermistor "BL" on
# other families). It was once exposed as "Outdoor Coil Temperature" at x0.1 because raw -29
# scaled to a plausible -2.9 C on a cold day; in fact the raw value sits at -29/-28 regardless
# of mode, season or whether the unit is on, i.e. an input with nothing connected. The entity
# is kept for other unit families but disabled by default.
#
# Code 0x14 "current value (CTL1)" reads 0 whenever the compressor is stopped and scales to the
# unit's own kWh figure at about 0.23 kW per amp, so it is in amps and behaves like the
# single-phase input current. Whether it or 0x13 (CTL2) is the compressor-side current is not
# settled by the manuals; it is what tells us the compressor is running.
MONITOR_COMPRESSOR_CODE = 0x14  # current value CTL1 -> compressor running when > 0
MONITOR_COMPRESSOR_SCALE = 1.0  # amps
MONITOR_SENSOR_CODES = [
    (0x11, "outdoor_temp", 1.0),              # Outdoor air temp TO
    (0x06, "indoor_discharge_air_temp", 1.0), # Not assigned on PACi (reads a fixed ~-29)
    (0x03, "indoor_coil_temp", 1.0),          # Indoor heat exchanger E1
    (0x02, "return_air_temp", 1.0),           # Indoor unit intake (return air) thermistor. Only
                                              # reads house air while the indoor fan runs.
    (0x0A, "outdoor_discharge_temp", 1.0),    # Compressor discharge TD
    (0x0D, "suction_temp", 1.0),              # Compressor suction TS
    (0x0E, "outdoor_hx_temp", 1.0),           # Outdoor heat exchanger C1
    (0x15, "outdoor_eev", 1.0),               # Outdoor expansion valve (MOV1) position, pulses:
                                              # 480 = fully open (parked there when stopped).
]
# No data on this PACi single-split: 0x08 and 0x1D (VRF-only items). Not read yet but in the
# PACi table: 0x04 (E2), 0x0F (C2), 0x13 (CTL2), 0x19 (frequency) -- the DEBUG sweep below
# logs them.
MONITOR_SENTINELS = (0x7FFF, -0x8000)  # "no data" markers

# Read-only calibration sweep: when DEBUG logging is enabled, probe this documented code range
# (0x02..0x22) once per diagnostic cycle and log each raw value. This reveals which codes the
# unit populates and lets per-code scaling be calibrated against a known state. It is a pure
# read (field 0x2C / op REQ, exactly like the app's Sensor Info screen) and is skipped entirely
# unless DEBUG is on, so it adds no traffic in normal operation.
MONITOR_SWEEP_RANGE = range(0x02, 0x23)

# Monitor replies carry only the value -- nothing in the parsed reply says which code it
# answers -- so a reply can only be matched to its request by order. Each read therefore waits
# for its own reply before the next request goes out. (A fixed 0.8 s wait used to stand in for
# this; whenever the unit took longer, the late reply was credited to the NEXT code and every
# later read in that cycle shifted one slot: compressor current showed up as a 0 C outdoor
# temp, outdoor air as the next sensor, and so on -- seen on roughly 1% of cycles.) If no
# reply arrives within the timeout the read is abandoned and we stay quiet for the drain time,
# so a straggler is discarded instead of being matched to the following request.
MONITOR_REPLY_TIMEOUT = 3.0
MONITOR_DRAIN_TIME = 2.0
MONITOR_MAX_MISSES = 2  # consecutive unanswered reads before abandoning the cycle's block

# Read-only capability probe, sent once per connection and ONLY when DEBUG logging is on. It
# asks for everything the official app reads that this integration does not use yet, so one
# debug capture shows which of them this unit answers ("probe -> ..." lines, each followed by
# the raw "BLE frame" reply). Every field is a REQ built exactly as the app builds it, grouped
# into frames the way the app groups them (the icons are split into smaller frames); replies
# are logged and never applied to state. A few come from the app's Maintenance screens, which
# it opens only after switching the controller into a maintenance state -- the probe does not
# do that: it sends them in normal state, as this integration has long done with the 0x2C
# Sensor Info reads and alarm entry 1 without trouble. Deliberately absent: anything that
# writes, the 0x69 sub-3 read (it returns the controller's administrator password), and the
# maintenance-only reads with no such precedent (0x41 test-run state, 0x39 voltage setting).
_MUX = 0x69
_ICON_CODES_ALL = (1, 6, 9, 10, 11, 12, 13, 14, 15, 16, 18, 19, 20, 21, 22, 23, 24, 26, 27,
                   28, 29, 30, 31, 32, 33, 34, 35, 36)
PROBE_READS = [
    ("outdoor temperature, tenths (0x21 channel 0x20)", "O_UNIT1", [(0x21, [0x00, 0x20, 0x02])]),
    ("Sensor Info code 100 hex (0x21 channel 0x6D)", "I_UNIT1", [(0x21, [0x00, 0x6D, 0x02])]),
    ("controller state (0x69 sub-27 idx 1)", "BLE_MODULE_UART", [(_MUX, [2, 0, 27, 1, 5])]),
    ("controlled room temperature, Sensor Info code 00 (0x69 sub-27 idx 3)", "BLE_MODULE_UART",
     [(_MUX, [2, 0, 27, 3, 5])]),
    ("controller temperature, Sensor Info code 01 (0x69 sub-27 idx 4)", "BLE_MODULE_UART",
     [(_MUX, [2, 0, 27, 4, 5])]),
    ("current alarm + unit (0x69 sub-27 idx 5,6)", "BLE_MODULE_UART",
     [(_MUX, [2, 0, 27, idx, 5]) for idx in (5, 6)]),
    ("filter sign hours (0x4B)", "I_UNIT1", [(0x4B, [0x04])]),
    ("model name, indoor unit (0x08)", "I_UNIT1", [(0x08, [0x12])]),
    ("model name, outdoor unit (0x08)", "O_UNIT1", [(0x08, [0x12])]),
    ("model name, controller (0x08)", "BLE_MODULE_UART", [(0x08, [0x12])]),
    ("unit structure (0x0D)", "I_UNIT1", [(0x0D, [0x10])]),
    ("per-mode setpoint + fan (0x0F)", "I_UNIT1", [(0x0F, [0x08])]),
    ("per-mode setpoint limits (0x0A)", "I_UNIT1", [(0x0A, [0x0C])]),
    ("nanoeX state (0x5C)", "I_UNIT1", [(0x5C, [0x00, 0x02])]),
    ("lock mask (0x46)", "I_UNIT1", [(0x46, [0x01])]),
    ("room-temperature sensor selection (0x07 sub 0x32)", "I_UNIT1", [(0x07, [0x00, 0x32, 0x02])]),
    ("alarm history entries 1-4 (0x27)", "I_UNIT1", [(0x27, [n, 3]) for n in (1, 2, 3, 4)]),
    ("controller clock + energy ring headers (0x05; 0x69 sub-19 idx 1,2,15,18)",
     "BLE_MODULE_UART",
     [(0x05, [0x07])] + [(_MUX, [2, 0, 19, idx, 12]) for idx in (1, 2, 15, 18)]),
    ("energy, daily ring (0x69 sub-19 idx 16,17)", "BLE_MODULE_UART",
     [(_MUX, [2, 0, 19, idx, 12]) for idx in (16, 17)]),
    ("energy, monthly ring (0x69 sub-19 idx 19-25)", "BLE_MODULE_UART",
     [(_MUX, [2, 0, 19, idx, 12]) for idx in range(19, 26)]),
    ("capability pages (0x69 sub-24 idx 1-4)", "BLE_MODULE_UART",
     [(_MUX, [2, 0, 24, idx, 12]) for idx in (1, 2, 3, 4)]),
    ("per-unit capability pages (0x69 sub-25 idx 2,3)", "BLE_MODULE_UART",
     [(_MUX, [2, 0, 25, idx, 12]) for idx in (2, 3)]),
    # Icons in three frames: none longer than the 12-field consumption request already in use.
    *[
        (f"status icons {part + 1}/3 (0x69 sub-23)", "BLE_MODULE_UART",
         [(_MUX, [2, 0, 23, dn, 5]) for dn in _ICON_CODES_ALL[part * 10:part * 10 + 10]])
        for part in range(3)
    ],
]
PROBE_SPACING = 2.0  # seconds between probe requests, so each reply follows its own request

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
        mode_code: int | None = None,
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
        # Raw operation-mode nibble from the unit (distinguishes auto-heat 5 / auto-cool 6);
        # used to pick the 0x4C profile for setpoint writes.
        self.mode_code = mode_code


class PanasonicHC:
    """Class representing the Panasonic Controller."""

    def __init__(
        self,
        ble_device: BLEDevice,
        mac_address: str,
        short_interval: int = DEFAULT_SHORT_POLL_INTERVAL,
        long_interval: int = DEFAULT_LONG_POLL_INTERVAL,
        ble_device_callback: Callable[[], BLEDevice | None] | None = None,
    ) -> None:
        """Initialise Panasonic H&C Controller."""

        self.last_update = 0
        # Poll cadences (seconds). short_interval is consumed by the HA poll loop in
        # __init__.py; long_interval gates the consumption/diagnostics block in
        # async_get_status. Both default to the const fallbacks and are normally driven by
        # the entry options.
        self.short_interval = short_interval
        self.long_interval = long_interval
        self.device = ble_device
        # Returns the best current route to the controller (it can move between Bluetooth
        # proxies/adapters), so a reconnect is not pinned to the path seen at setup.
        self._ble_device_callback = ble_device_callback
        self.mac_address = mac_address
        self._on_update_callbacks: list[Callable] = []
        self._conn = None
        self._lock = asyncio.Lock()
        self.status = None
        self._curtemp: float | None = None  # last accepted room temperature
        self._curtemp_jump: tuple[float, int] | None = None  # (candidate, times seen)
        self.curhour = None
        self.curindex = None
        self.consumption = [0] * 48
        self.outdoor_temp = None
        self.error_code = None
        self.nanoe = None
        # Service-monitor readings (key -> value) and compressor current (diagnostic).
        self.monitor: dict[str, float] = {}
        self.compressor_current = None
        # time.monotonic() of the last compressor reading; None = no reading to trust
        self._compressor_current_at: float | None = None
        # Read the compressor current with every status poll while the unit is on (see
        # async_get_status). After an unanswered read these per-poll reads pause, for 30 s and
        # then twice as long after each further miss (up to the diagnostics interval), so a
        # lost reply is retried soon but a unit that does not serve the code costs little.
        self._fast_current_after = 0.0  # time.monotonic() from which they resume
        self._fast_current_backoff = 0.0
        self._probing = False  # DEBUG capability probe in progress: log replies, apply none
        self._probed = False   # ...and it has run for this connection
        self._monitor_pending: tuple[str, float] | None = None
        self._monitor_reply = asyncio.Event()
        # Status-icon flags (from 0x69 sub-23) -> HVAC action preheating/defrosting.
        self._icons: dict[int, bool] = {}
        self.preheating = None
        self.defrosting = None

    @property
    def is_connected(self) -> bool:
        """Return true if connected to thermostat."""

        return self._conn is not None and self._conn.is_connected

    def _current_device(self) -> BLEDevice:
        """Return the freshest BLEDevice for the controller (falls back to the last known)."""

        if self._ble_device_callback and (device := self._ble_device_callback()):
            self.device = device
        return self.device

    @property
    def compressor_running(self) -> bool | None:
        """Whether the compressor is running, from the measured current. None if unknown.

        A current above zero is the one signal that follows the compressor in every mode (the
        status frame's standby bit does not, in cooling). None when the code has not been
        read recently, e.g. on a unit that does not serve it, so callers can fall back."""

        if self.compressor_current is None or self._compressor_current_at is None:
            return None
        if time.monotonic() - self._compressor_current_at > 2 * self.long_interval + 60:
            return None
        return self.compressor_current > 0

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
            device = self._current_device()
            self._conn = await establish_connection(
                BleakClientWithServiceCache,
                device,
                name=device.name or device.address,
                ble_device_callback=self._current_device,
            )
            await asyncio.sleep(0.5)
            await self._conn.start_notify(BLE_CHAR_NOTIFY, self.on_notification)
            await asyncio.sleep(0.5)
            # The capability probe (DEBUG only) waits for the diagnostics cycle after this
            # first one, so it does not hold up the entities becoming available.
            self._fast_read_result(True)
            self._probed = True
            await self.async_get_status()
            self._probed = False
        except (BleakError, TimeoutError) as e:
            raise PanasonicHCException("Could not connect to Thermostat") from e

    async def async_disconnect(self) -> None:
        """Shutdown thermostat connection."""

        if self._conn is None:  # never connected -- nothing to close
            return
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
        long_due = now > self.last_update + self.long_interval

        # The HVAC action comes from the compressor current, so while the unit is on read that
        # one code with every status poll rather than only in the slow block below (a compressor
        # start or stop would otherwise show up to a whole diagnostics interval late).
        if (
            not long_due
            and self.status
            and self.status.power
            and time.monotonic() >= self._fast_current_after
        ):
            await asyncio.sleep(0.4)
            self._fast_read_result(
                await self._read_monitor(
                    MONITOR_COMPRESSOR_CODE, "compressor_current", MONITOR_COMPRESSOR_SCALE
                )
            )

        if long_due:
            await asyncio.sleep(0.5)
            await self._async_write_command(PanasonicBLEPowerReq())
            await asyncio.sleep(0.5)
            await self._async_write_command(PanasonicBLEPowerReqHour())
            await asyncio.sleep(0.5)
            await self._async_write_command(PanasonicBLEErrorReq())
            await asyncio.sleep(0.5)
            # An unanswered read costs timeout + drain, and this runs inside the status poll,
            # so give up on the block for this cycle once the unit has clearly stopped
            # answering (outdoor unit off/busy, or a model without 0x2C) rather than waiting
            # out every code.
            misses = 0
            for code, key, scale in [
                (MONITOR_COMPRESSOR_CODE, "compressor_current", MONITOR_COMPRESSOR_SCALE),
                *MONITOR_SENSOR_CODES,
            ]:
                answered = await self._read_monitor(code, key, scale)
                if code == MONITOR_COMPRESSOR_CODE:
                    self._fast_read_result(answered)
                if answered:
                    misses = 0
                else:
                    misses += 1
                    if misses >= MONITOR_MAX_MISSES:
                        _LOGGER.debug("monitor reads unanswered; skipping the rest this cycle")
                        break
                await asyncio.sleep(0.2)
            else:
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    await self._sweep_monitor_codes()
            if _LOGGER.isEnabledFor(logging.DEBUG) and not self._probed:
                self._probed = True
                await self._probe_reads()
            self.last_update = now

    async def _probe_reads(self) -> None:
        """Ask once for every read in PROBE_READS and log what comes back (DEBUG only).

        Nothing is decoded or stored: while this runs, on_notification logs each reply frame
        and applies none of it, so an unfamiliar reply cannot disturb an existing entity. It
        holds up the status poll for about a minute, once per connection."""

        self._probing = True
        try:
            for name, dst, fields in PROBE_READS:
                _LOGGER.debug(
                    "probe -> %s: to %s, %s", name, dst,
                    "; ".join(f"0x{fid:02x} [{bytes(v).hex(' ')}]" for fid, v in fields),
                )
                await self._async_write_command(PanasonicBLEReadReq(dst, fields))
                await asyncio.sleep(PROBE_SPACING)
            await asyncio.sleep(MONITOR_DRAIN_TIME)
        finally:
            self._probing = False
            _LOGGER.debug("probe finished")

    def _fast_read_result(self, answered: bool) -> None:
        """Schedule the next per-poll compressor read after one was answered or missed."""

        if answered:
            self._fast_current_backoff = 0.0
            self._fast_current_after = 0.0
        else:
            self._fast_current_backoff = min(
                max(2 * self._fast_current_backoff, 30.0), float(self.long_interval)
            )
            self._fast_current_after = time.monotonic() + self._fast_current_backoff

    async def _sweep_monitor_codes(self) -> None:
        """Read-only calibration sweep over MONITOR_SWEEP_RANGE (DEBUG only).

        Logs the raw signed value of every code so a debug capture reveals which codes the unit
        populates and lets per-code scaling be calibrated. Values are logged, not stored as
        entities. Uses the same harmless 0x2C read as the named sensors."""

        for code in MONITOR_SWEEP_RANGE:
            _LOGGER.debug("monitor sweep -> code 0x%02x", code)
            await self._read_monitor(code, f"sweep_0x{code:02x}", 1.0)

    async def _read_monitor(self, code: int, key: str, scale: float) -> bool:
        """Request one service-monitor code (0x2C) and wait for its reply.

        Returns True if the unit answered, False if the read was abandoned.

        on_notification credits the next 0x2C reply to (key, scale) and sets the event. Only
        one read is ever in flight, and the next is not sent until this one is answered or
        abandoned (see MONITOR_REPLY_TIMEOUT), so request order = reply order."""

        self._monitor_reply.clear()
        self._monitor_pending = (key, scale)
        try:
            await self._async_write_command(PanasonicBLEMonitorReq(code))
            async with asyncio.timeout(MONITOR_REPLY_TIMEOUT):
                await self._monitor_reply.wait()
        except TimeoutError:
            # Stop accepting a reply for this read, then give a late one time to arrive and
            # be dropped before the next request claims the slot.
            self._monitor_pending = None
            _LOGGER.debug("monitor %s (code 0x%02x): no reply", key, code)
            if key == "compressor_current":
                # Stop trusting the last reading: the action falls back to the status frame
                # rather than staying frozen on a state that may have changed since.
                self._compressor_current_at = None
            await asyncio.sleep(MONITOR_DRAIN_TIME)
            return False
        finally:
            self._monitor_pending = None
        return True

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
            parcel = PanasonicBLEParcel.parse(data=data)
        except Exception as e:
            _LOGGER.error("Error parsing packet: %s", e)
            return
        _LOGGER.debug("Received packet data: %s", parcel)

        if self._probing:
            return  # capability probe: the reply is logged above and deliberately not applied

        do_callback = False
        for packet in parcel:
            # Handled one by one so a bad field cannot discard the others in the same frame.
            try:
                do_callback |= self._handle_packet(packet)
            except Exception as e:
                _LOGGER.error("Error handling field 0x%02x: %s", packet.ptype, e)

        _LOGGER.debug(
            "Consumption: %s, curindex: %s, curhour: %s",
            self.consumption,
            self.curindex,
            self.curhour,
        )
        if do_callback:
            for callback in self._on_update_callbacks:
                callback()

    def _filter_curtemp(self, cur: float | None) -> float | None:
        """Return the room temperature to report, given the latest reading (see CURTEMP_*)."""

        last = self._curtemp
        if cur is None or not CURTEMP_MIN <= cur <= CURTEMP_MAX:
            return last
        if last is not None and abs(cur - last) > CURTEMP_MAX_STEP:
            candidate, seen = self._curtemp_jump or (cur, 0)
            seen = seen + 1 if abs(cur - candidate) <= 2 else 1
            self._curtemp_jump = (cur, seen)
            if seen < CURTEMP_JUMP_CONFIRM:
                return last
        self._curtemp_jump = None
        self._curtemp = cur
        return cur

    def _handle_packet(self, packet: PanasonicBLEParcel.PanasonicBLEPacket) -> bool:
        """Apply one decoded field to the device state. Returns True if state changed."""

        if isinstance(packet, PanasonicBLEParcel.PanasonicBLEPacketStatus):
            if not packet.power:
                # The compressor cannot run with the unit off. Recording that keeps a reading
                # taken before a switch-off from showing "heating"/"cooling" at the next
                # switch-on, before that poll's compressor read comes back.
                self.compressor_current = 0.0
                self._compressor_current_at = time.monotonic()
            # A mode/fan value outside the known tables decodes to None: keep the last
            # known one rather than losing power, setpoint and running state as well.
            prev = self.status
            mode = packet.mode.name if packet.mode else (prev.mode if prev else None)
            fanspeed = (
                packet.fanspeed.name
                if packet.fanspeed
                else (prev.fanspeed if prev else None)
            )
            self.status = Status(
                packet.power,
                mode,
                packet.powersave,
                self._filter_curtemp(packet.curtemp),
                packet.temp,
                fanspeed,
                packet.prohibited,
                packet.running,
                packet.mode_code,
            )
            return True
        elif isinstance(
            packet, PanasonicBLEParcel.PanasonicBLEPacketOutdoorTemp
        ):
            if packet.temp is not None:
                self.outdoor_temp = packet.temp
                return True
        elif isinstance(
            packet, PanasonicBLEParcel.PanasonicBLEPacketError
        ):
            self.error_code = packet.error
            return True
        elif isinstance(
            packet, PanasonicBLEParcel.PanasonicBLEPacketNanoe
        ):
            self.nanoe = packet.nanoe
            return True
        elif packet.ptype == 0x2C and self._monitor_pending:
            # Reply to a service-monitor read (matched to the pending key+scale).
            key, scale = self._monitor_pending
            self._monitor_pending = None
            self._monitor_reply.set()
            if key == "compressor_current" and (
                len(packet.pdata) < 2
                or int.from_bytes(packet.pdata[0:2], "big", signed=True) in MONITOR_SENTINELS
            ):
                # Answered without a value (not supported / busy / no data): as for no reply.
                self._compressor_current_at = None
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
                        self._compressor_current_at = time.monotonic()
                    return True
        elif (
            packet.ptype == 0x69
            and len(packet.pdata) >= 5
            and packet.pdata[2] == 23
        ):
            # Status-icon reply (0x69 sub-23): pdata[3]=icon mDn, pdata[4]=active.
            self._icons[packet.pdata[3]] = packet.pdata[4] != 0
            self.preheating = any(self._icons.get(c) for c in ICON_PREHEATING)
            self.defrosting = bool(self._icons.get(ICON_DEFROST))
            return True
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
                return True
        return False

    async def async_set_power(self, state: bool) -> None:
        """Set power state."""

        await self._async_write_command(PanasonicBLEPower(1 if state else 0))

    async def async_set_temperature(self, temp: float) -> None:
        """Set target temperature."""

        # Also write the profile of the mode the unit is in now when that is dry or auto
        # (heat and cool are always written) -- see PanasonicBLETemp.
        mode_code = self.status.mode_code if self.status else None
        await self._async_write_command(PanasonicBLETemp(temp, mode_code))

    async def async_set_mode(self, mode: str):
        """Set thermostat mode."""

        await self._async_write_command(PanasonicBLEMode(MODE[mode].value))

    async def async_set_fanmode(self, mode: str):
        """Set fan speed for the current operating mode."""

        # Target the current mode's fan profile (MODE values equal the 0x4C nibbles)
        # so fan speed also applies in fan_only/dry/auto, not just heat/cool.
        nibble = (
            MODE[self.status.mode].value
            if self.status and self.status.mode
            else MODE.heat.value
        )
        await self._async_write_command(
            PanasonicBLEFanMode(FANSPEED[mode].value, nibble)
        )

    async def async_set_energysaving(self, state: bool):
        """Toggle EnergySaving mode."""

        await self._async_write_command(PanasonicBLEEnergySaving(state))

    async def async_set_nanoe(self, state: bool):
        """Turn nanoeX on or off (applies to all indoor units)."""

        await self._async_write_command(PanasonicBLENanoe(state))
        # Reflect optimistically. The state is never read back from the unit (it does not
        # volunteer 0x5C), so after a restart the switch is unknown until it is next used.
        self.nanoe = state
        for callback in self._on_update_callbacks:
            callback()
