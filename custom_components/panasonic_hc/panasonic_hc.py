"""Handle communication with supported Panasonic H&C devices."""

import asyncio
from collections.abc import Callable
import datetime
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
    alarm_code,
    setpoint_temp,
)

# Setpoint range used until the unit's own per-mode limits have been read (READS_ONCE).
MIN_TEMP = 16
MAX_TEMP = 32

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

# Service-monitor "item codes" (the wired controller's sensor information), read via field 0x2C
# addressed to the OUTDOOR unit. The numbering depends on the product family; these are the
# PACi single-split codes:
#   indoor   02 intake temp | 03 heat exchanger E1 | 04 heat exchanger E2 | 05-08 not used
#   outdoor  0A discharge temp TD | 0D intake (compressor SUCTION) temp TS | 0E outdoor heat
#            exchanger C1 | 0F outdoor heat exchanger C2 | 11 outdoor air TO | 13 current value
#            CTL2 | 14 current value CTL1 | 15 outdoor MV (expansion valve) value | 19 frequency
# The VRF (ECOi) family uses a different table: there 0D/0E are "heat exchanger gas/liquid 1"
# and 06 is an indoor discharge-air thermistor. On PACi 0D is the suction line: it reads cold
# in cooling AND heating, slightly above the evaporator temperature (the expansion valve holds
# about 0-2 K superheat), and below -30 C in defrost. The table gives no units; every code
# below reads in whole units (raw 85 = 85 C).
#
# Each entry is (code, key, scale): the reply is a signed big-endian 16-bit raw value and the
# displayed value = raw * scale.
#
# Code 0x06 is not assigned on PACi (on other families it is the optional indoor discharge-air
# thermistor "BL"). On PACi its raw value sits at -29/-28 regardless of mode, season or whether
# the unit is on, i.e. an input with nothing connected. The entity is kept for other unit
# families but disabled by default.
#
# Code 0x14 "current value (CTL1)" is in amps, reads 0 whenever the compressor is stopped and
# behaves like the single-phase input current (about 0.23 kW per amp). It is what tells us the
# compressor is running. Whether it or 0x13 (CTL2) is the compressor-side current is unknown.
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
# 0x08 and 0x1D are VRF-only items with no data on a PACi single-split. In the PACi table but
# not read here: 0x04 (E2), 0x0F (C2), 0x13 (CTL2), 0x19 (frequency) -- the DEBUG sweep below
# logs them.
MONITOR_SENTINELS = (0x7FFF, -0x8000)  # "no data" markers

# Read-only calibration sweep: when DEBUG logging is enabled, read every code in 0x02..0x22
# once per diagnostics cycle and log each raw value. This shows which codes the unit populates
# and lets per-code scaling be calibrated against a known state. It is a pure read (field 0x2C,
# op REQ); values are only logged, never stored, and it is skipped entirely unless DEBUG is on,
# so it adds no traffic in normal operation.
MONITOR_SWEEP_RANGE = range(0x02, 0x23)

# Monitor replies carry only the value -- nothing in the parsed reply says which code it
# answers -- so a reply can only be matched to its request by order. Each read therefore waits
# for its own reply before the next request goes out; a fixed delay is not enough, because a
# late reply would be credited to the NEXT code and shift every later read in the cycle by one
# slot. If no reply arrives within the timeout the read is abandoned and we stay quiet for the
# drain time, so a straggler is discarded instead of being matched to the following request.
MONITOR_REPLY_TIMEOUT = 3.0
MONITOR_DRAIN_TIME = 2.0
MONITOR_MAX_MISSES = 2  # consecutive unanswered reads before abandoning the cycle's block

# Read-only capability probe, sent once per connection and ONLY when DEBUG logging is on. It
# sends the reads below (some are also regular reads, see READS_ONCE / READS_LONG) so one
# debug log shows which of them a unit answers ("probe -> ..." lines, each followed by the raw
# "BLE frame" reply). Every field is a REQ; replies are logged and never applied to state. A
# few are maintenance-function reads: the probe never switches the controller into a
# maintenance state, it sends them in normal state, as the regular 0x2C monitor reads and the
# 0x27 alarm-history read already are. Deliberately absent: anything that writes, the 0x69
# sub-3 read (it returns the controller's administrator password), and the maintenance-only
# reads with no such normal-state counterpart (0x41 test-run state, 0x39 voltage setting).
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

# Regular reads: (destination, [(field, request bytes)]). The monthly-ring frame sends its
# header (idx 18) together with pages 19-25; the header is taken before its pages (see
# on_notification).
# SAFETY: they go out through PanasonicBLEReadReq, which can only build REQs, and every 0x69
# value is the read form [02 00 sub idx len]. That -- not a different field -- is what keeps
# them clear of the requests that must never be sent: field 0x69 is shared with the
# maintenance-mode WRITE [00 00 1b 01 x] (sub 27, like the alarm read below) and with the
# sub-3 READ, which returns the controller's administrator password. Also never: SET 0x27 or
# 0x4B with an empty value (clear alarm history / filter sign), 0x41 C0/80 (test run),
# 0x61/0x62/0x64 (maintenance restart), SET 0x39 (supply voltage).
READS_ONCE = [  # static: asked until answered (up to 3 rounds per connection), then kept
    ("I_UNIT1", [(0x08, [0x12])]),           # model name + version word, indoor unit
    ("O_UNIT1", [(0x08, [0x12])]),           # ... outdoor unit
    ("BLE_MODULE_UART", [(0x08, [0x12])]),   # ... wall controller
    ("BLE_MODULE_UART", [(_MUX, [2, 0, 24, 2, 12])]),  # setpoint limits per mode
]
READS_LONG = [  # with the energy + diagnostics block
    # Monthly energy ring: header (current slot + valid mask) and all 7 pages in one frame.
    ("BLE_MODULE_UART", [(_MUX, [2, 0, 19, idx, 12]) for idx in range(18, 26)]),
    # The alarm the controller is showing now (idx 5) and the unit it came from (idx 6).
    ("BLE_MODULE_UART", [(_MUX, [2, 0, 27, idx, 5]) for idx in (5, 6)]),
    # Outdoor air temperature in 0.1 C: analog channel 0x20 of the outdoor unit.
    ("O_UNIT1", [(0x21, [0x00, 0x20, 0x02])]),
    # nanoeX setting (selector 0 = on/off state).
    ("I_UNIT1", [(0x5C, [0x00, 0x02])]),
]
# Setpoint-limit pairs (upper, lower) in the order the controller sends them (0x69 sub-24
# idx 2).
LIMIT_MODES = ("cool", "heat", "dry", "auto")
MONTHS = 13  # slots in the monthly energy ring

# Status-icon codes polled via the 0x69 sub-23 query, mapped to the HVAC action they
# represent: 9/10 = preheating/preparing, 11 = defrost.
ICON_PREHEATING = (9, 10)
ICON_DEFROST = 11
ICON_CODES = (9, 10, 11)

_LOGGER = logging.getLogger(__name__)


def _descramble(data: bytes) -> bytes:
    """Reverse the link-layer XOR obfuscation to recover the plaintext frame.

    XOR every byte with 0x69, undo the cumulative byte chain, then XOR byte[0] with 0xCA.
    Used only for debug logging of raw frames; unlike PanasonicBLEParcel.parse it does no
    checksum or length validation, so it works on any frame including ones the parser would
    reject.
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
        # Status byte[2] standby flag: False = indoor fan held (idle/standby, "slight blow"),
        # True = not held, None = unknown. It follows the compressor in heating but not in
        # cooling, so HVACAction uses it only when the compressor current is unknown. Shown
        # as the Indoor Fan Standby binary sensor.
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
        # Controller's hourly energy ring (0x69 sub-19), kept as read: slot -> kWh.
        self.curhour: int | None = None  # hours of the ring day stored (newest = curhour - 1)
        self.curindex: int | None = None  # slot of hour curhour - 1
        # Ring day, by the controller's clock (it changes at about 00:30 on that clock).
        self.energy_date: datetime.date | None = None
        self._energy_valid = 0  # bit n set = slot n holds data
        self._energy_ring: list[float | None] = [None] * 48
        self._energy_headers: set[int] = set()  # headers (idx 1, 2) since the last total
        self._energy_pages: set[int] = set()  # pages read since the last header
        # (controller day, today's kWh, yesterday's kWh) from the last complete ring read.
        self.energy: tuple[datetime.date | None, float, float] | None = None
        # Monthly ring: (current slot, this month's kWh or None while not trustworthy, last
        # month's kWh or None). See _update_month.
        self.energy_month: tuple[int, float | None, float | None] | None = None
        self._month_values: list[float] | None = None
        self._month_index: int | None = None
        self._month_stale: tuple[int, float] | None = None  # (slot, figure) not yet replaced
        self._month_header: tuple[int, int] | None = None  # (index, valid) awaiting pages
        self._month_pages: dict[int, float] = {}  # slot -> kWh read since that header
        self.outdoor_temp = None
        # Outdoor air in 0.1 C (field 0x21) and when it was last read; while fresh it is
        # preferred over monitor code 0x11, which is the same sensor in whole degrees.
        self._outdoor_fine_at: float | None = None
        # The alarm the controller shows now (0x69 sub-27 idx 5; "A00" = none) and the unit
        # address it reports with it (idx 6; 0 = none).
        self.alarm_code: str | None = None
        self.alarm_unit: int | None = None
        # Static, kept once answered (not re-read on reconnect, only when the entry is set up
        # again): component name -> (model, version word), mode name -> (min, max) setpoint.
        self.models: dict[str, tuple[str, str]] = {}
        self.setpoint_limits: dict[str, tuple[float, float]] = {}
        self._once_attempts = 0  # READS_ONCE rounds sent on this connection (gives up at 3)
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
            self._once_attempts = 0
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
        # precedence. A reply's active byte is 0 while the state is inactive. Preheating in
        # particular is short-lived, so they need to be polled at the status rate to be caught
        # rather than parked in the slow diagnostic block.
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
            # Before the monitor block: the 0.1 C outdoor reading must land before code 0x11.
            for dst, fields in self._once_reads_due() + READS_LONG:
                await self._async_write_command(PanasonicBLEReadReq(dst, fields))
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

    def _once_reads_due(self) -> list:
        """The READS_ONCE entries not answered yet (none after three rounds per connection,
        so a unit that does not serve one does not get asked every cycle)."""

        if self._once_attempts >= 3:
            return []
        due = [
            (dst, fields)
            for dst, fields in READS_ONCE
            if (dst not in self.models if fields[0][0] == 0x08 else not self.setpoint_limits)
        ]
        if due:
            self._once_attempts += 1
        return due

    def _outdoor_fine_fresh(self) -> bool:
        return (
            self._outdoor_fine_at is not None
            and time.monotonic() - self._outdoor_fine_at <= 2 * self.long_interval + 60
        )

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

        Logs the raw signed value of every code so the debug log shows which codes the unit
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

        # Log the de-scrambled plaintext of every inbound frame (debug only): shows fields the
        # parser ignores and the raw bytes of each frame.
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

        # An energy total needs both ring headers from the same frame: if one is rejected,
        # the other must not be paired with a header from a different read.
        if any(
            isinstance(p, PanasonicBLEParcel.PanasonicBLEPacketConsumption)
            and (p.hour is not None or p.index is not None)
            for p in parcel
        ):
            self._energy_headers.clear()
        do_callback = False
        for packet in parcel:
            # Handled one by one so a bad field cannot discard the others in the same frame.
            try:
                do_callback |= self._handle_packet(packet, parcel.src)
            except Exception as e:
                _LOGGER.error("Error handling field 0x%02x: %s", packet.ptype, e)

        if self._energy_headers == {1, 2} and len(self._energy_pages) == 12:
            self._update_energy()
        if self._month_header is not None and len(self._month_pages) == MONTHS:
            # A header, then all 13 slots read after it (same frame or later ones).
            header, self._month_header = self._month_header, None
            self._update_month(*header, [self._month_pages[i] for i in range(MONTHS)])
            do_callback = True
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

    def _handle_packet(
        self,
        packet: PanasonicBLEParcel.PanasonicBLEPacket,
        src: PanasonicBLEParcel.COMPONENT | None = None,
    ) -> bool:
        """Apply one decoded field (sent by src) to the device state. True if state changed."""

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
            # Only the outdoor unit's channel 0x20 is asked for, and the reply does not say
            # which channel it answers, so take it only from that unit.
            if packet.temp is not None and src is PanasonicBLEParcel.COMPONENT.O_UNIT1:
                self.outdoor_temp = packet.temp
                self._outdoor_fine_at = time.monotonic()
                return True
        elif isinstance(packet, PanasonicBLEParcel.PanasonicBLEPacketModel):
            if src is not None:
                self.models[src.name] = (packet.name, packet.version)
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
                    if key == "outdoor_temp" and not self._outdoor_fine_fresh():
                        # Whole degrees (truncated): only while the 0.1 C reading is missing.
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
            # Status-icon reply (0x69 sub-23): pdata[3]=icon code, pdata[4]=active.
            self._icons[packet.pdata[3]] = packet.pdata[4] != 0
            self.preheating = any(self._icons.get(c) for c in ICON_PREHEATING)
            self.defrosting = bool(self._icons.get(ICON_DEFROST))
            return True
        elif (
            packet.ptype == 0x69
            and len(packet.pdata) >= 5
            and packet.pdata[0:3] == bytes([2, 0, 27])
        ):
            # Controller state (0x69 sub-27): idx 5 = alarm shown now, idx 6 = its unit.
            if packet.pdata[3] == 5:
                self.alarm_code = alarm_code(packet.pdata[4])
                return True
            if packet.pdata[3] == 6:
                self.alarm_unit = packet.pdata[4]
                return True
        elif (
            packet.ptype == 0x69
            and len(packet.pdata) >= 12
            and packet.pdata[0:4] == bytes([2, 0, 24, 2])
        ):
            # Setpoint limits (0x69 sub-24 idx 2): (upper, lower) per mode in LIMIT_MODES.
            limits = {}
            for i, mode in enumerate(LIMIT_MODES):
                hi = setpoint_temp(packet.pdata[4 + 2 * i])
                lo = setpoint_temp(packet.pdata[5 + 2 * i])
                if 0 <= lo < hi <= 40:
                    limits[mode] = (lo, hi)
            if limits:
                self.setpoint_limits = limits
                return True
        elif isinstance(
            packet, PanasonicBLEParcel.PanasonicBLEPacketConsumption
        ):
            # The headers arrive in the frame before the pages, so a total is only worked
            # out once both headers (from one frame) and then all 12 pages are in: the slot
            # the headers point at has then been read too (on_notification calls
            # _update_energy).
            if packet.month_index is not None:
                self._month_header = (packet.month_index, packet.month_valid)
                self._month_pages = {}
            if packet.month_values is not None:
                for i, value in enumerate(packet.month_values):
                    self._month_pages[packet.month_pos + i] = value
            if packet.hour is not None:
                self.curhour = packet.hour
                self.energy_date = packet.date
                self._energy_headers.add(1)
                self._energy_pages.clear()
            if packet.index is not None:
                self.curindex = packet.index
                self._energy_valid = packet.valid
                self._energy_headers.add(2)
                self._energy_pages.clear()
            if packet.values is not None:
                for i, value in enumerate(packet.values):
                    self._energy_ring[packet.pos + i] = value
                self._energy_pages.add(packet.pos)
                return True
        return False

    def _update_energy(self) -> None:
        """Work out today's and yesterday's totals from a complete read of the ring.

        Slot curindex holds hour curhour - 1 of today, so today's hour h is in slot
        curindex - (curhour - 1) + h and yesterday's 24 slots before that. Slots whose bit
        is clear in the valid mask have not been written yet and are left out: after the
        daily rollover the controller already points at hour 0 of the new day, but that
        slot still holds the figure from two days earlier for the first hour."""

        self._energy_headers.clear()
        if self.curhour is None or self.curindex is None or None in self._energy_ring:
            return
        first = self.curindex - (self.curhour - 1)  # slot of hour 0 of today

        def total(start: int, hours: int) -> float:
            slots = [(start + h) % 48 for h in range(hours)]
            return round(
                sum((self._energy_ring[s] for s in slots if self._energy_valid >> s & 1), 0.0),
                1,
            )

        self.energy = (self.energy_date, total(first, self.curhour), total(first - 24, 24))
        _LOGGER.debug(
            "Energy ring: day %s hour %s index %s valid %012x -> today %s kWh, "
            "yesterday %s kWh; slots %s",
            self.energy_date,
            self.curhour,
            self.curindex,
            self._energy_valid,
            self.energy[1],
            self.energy[2],
            self._energy_ring,
        )

    def _update_month(self, index: int, valid: int, values: list[float]) -> None:
        """Take this month's and last month's totals from a complete read of the monthly ring.

        Slot index is the current month and is a live total. How the controller handles a
        month change is not known. The hourly ring marks a new slot not valid until it is
        written, and a slot with a clear valid bit is not trusted here either. In case the
        controller moves to the next slot with the bit still set and the figure from 13 months
        earlier still in it:
          * right after the slot moves, a figure equal to what the slot held before is not
            trusted until it changes;
          * on the 1st of the month (controller day, from the hourly ring read just before),
            the month so far cannot be more than that day so far, which also catches it
            after a restart, when there is no earlier read to compare with."""

        old, old_index = self._month_values, self._month_index
        if old is not None and old_index is not None and index != old_index:
            if values[index] > 0 and values[index] == old[index]:
                self._month_stale = (index, values[index])
        if (
            self.energy is not None
            and self.energy[0] is not None
            and self.energy[0].day == 1
            # A margin for an hourly read that is a cycle behind.
            and values[index] > self.energy[1] + 5.0
        ):
            self._month_stale = (index, values[index])
        if self._month_stale is not None and self._month_stale != (index, values[index]):
            self._month_stale = None
        current = (
            values[index] if valid >> index & 1 and self._month_stale is None else None
        )
        prev = (index - 1) % MONTHS
        finished = values[prev] if valid >> prev & 1 else None
        self._month_values, self._month_index = values, index
        self.energy_month = (index, current, finished)
        _LOGGER.debug(
            "Monthly ring: slot %s valid %04x -> this month %s kWh, last month %s kWh; %s",
            index, valid, current, finished, values,
        )

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
        # Reflect it straight away, then read it back about a second after the change (it is
        # also read with every diagnostics cycle, see READS_LONG).
        self.nanoe = state
        for callback in self._on_update_callbacks:
            callback()
        await asyncio.sleep(1.0)
        try:
            await self._async_write_command(
                PanasonicBLEReadReq("I_UNIT1", [(0x5C, [0x00, 0x02])])
            )
        except PanasonicHCException as e:
            _LOGGER.debug("nanoeX read-back not sent: %s", e)
