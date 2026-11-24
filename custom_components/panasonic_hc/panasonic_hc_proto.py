"""Data structures for Panasonic H&C Bluetooth controllers."""

from enum import Enum
import io
import logging


_LOGGER = logging.getLogger(__name__)


def _cksum(data):
    cksum = 0
    for i in data:
        cksum = (cksum + i) & 255
    return cksum


def _decode(data):
    data = list(data)
    for x in range(len(data)):
        data[x] = data[x] ^ 105

    for x in range(len(data) - 1, 0, -1):
        data[x] = data[x] ^ data[x - 1]

    data[0] = data[0] ^ 202

    if _cksum(data[1:-1]) != data[-1]:
        raise ValueError("Bad Checksum")

    return bytes(data)


def _encode(data):
    data = list(data)
    data[-1] = _cksum(data[1:-1])

    data[0] = data[0] ^ 202
    for x in range(1, len(data)):
        data[x] = data[x] ^ data[x - 1]

    for x in range(len(data)):
        data[x] = data[x] ^ 105

    return bytes(data)


# A field whose length byte is one of these carries no value: the unit is answering "this
# item is not supported" or "rejected / busy, ask again" (the app re-sends the latter).
FIELD_MARKERS = {0xFE: "not supported", 0xFF: "rejected"}


class MODE(Enum):
    heat = 1
    cool = 2
    fan_only = 3
    dry = 4
    auto = 5


class FANSPEED(Enum):
    auto = 2
    high = 3
    medium = 4
    low = 5


class PanasonicBLEParcel:
    """A BLE Parcel."""

    idx = 0

    class PanasonicBLEPacket:
        """A BLE Parcel Packet."""

        class PACKET_TYPE(Enum):
            SET_TEMP = 76
            SET_POWER = 65
            SET_MODE = 66
            SET_POWERSAVE = 84

        # Set when the unit answered the field with a marker instead of a value: see
        # FIELD_MARKERS. A marked field has no data and must never be decoded as one.
        marker = None

        def __init__(self, ptype, pdata):
            self.ptype = ptype
            self.pdata = pdata

        @staticmethod
        def parse(fd: io.BytesIO):
            """Construct Packet from fd."""

            ptype = fd.read(1)[0]
            plen = fd.read(1)[0]
            if plen in FIELD_MARKERS:
                # 0xFE / 0xFF in the length byte are not lengths: no value bytes follow and
                # the next field starts straight away. Reading 254/255 "value" bytes here
                # used to swallow the rest of the frame, and a lone marked field decoded its
                # checksum byte as data (a 0x27 marker read as "A00 / no fault").
                packet = PanasonicBLEParcel.PanasonicBLEPacket(ptype, b"")
                packet.marker = FIELD_MARKERS[plen]
                return packet
            pdata = fd.read(plen)

            # A field shorter than its decoder expects must not take the rest of the parcel
            # down with it: fall back to the undecoded packet, which every handler ignores.
            try:
                if ptype == 129:
                    return PanasonicBLEParcel.PanasonicBLEPacketStatus(ptype, pdata)
                if ptype == 33:
                    return PanasonicBLEParcel.PanasonicBLEPacketOutdoorTemp(ptype, pdata)

                if ptype == 105 and pdata[0:3] == bytes([2, 0, 19]):
                    return PanasonicBLEParcel.PanasonicBLEPacketConsumption(ptype, pdata)
                if ptype == 39:
                    return PanasonicBLEParcel.PanasonicBLEPacketError(ptype, pdata)
                if ptype == 92 and plen >= 2:
                    return PanasonicBLEParcel.PanasonicBLEPacketNanoe(ptype, pdata)
            except (IndexError, ValueError) as e:
                _LOGGER.debug(
                    "Undecodable field 0x%02x [%s]: %s", ptype, pdata.hex(" "), e
                )

            return PanasonicBLEParcel.PanasonicBLEPacket(ptype, pdata)

        def encode(self):
            return self.ptype.to_bytes() + len(self.pdata).to_bytes() + self.pdata

        def __str__(self):
            if self.marker:
                return f"{self.ptype}, <{self.marker}>"
            return f"{self.ptype}, {list(self.pdata)}"

    class PanasonicBLEPacketStatus(PanasonicBLEPacket):
        def __init__(self, ptype, pdata):
            super().__init__(ptype, pdata)
            self.curtemp = None
            self.power = self.pdata[0] & 1
            # Raw operation-mode nibble: 1 heat, 2 cool, 3 fan, 4 dry, 5 auto(-heat),
            # 6 auto-cool. These are also the 0x4C profile selectors, so the raw value is kept
            # for writes. Both auto values map to MODE.auto; a value outside the table (0, 7)
            # decodes to None so the caller can keep the last known mode instead of the whole
            # status frame being thrown away.
            self.mode_code = (self.pdata[0] >> 5) & 7
            m = 5 if self.mode_code == 6 else self.mode_code
            self.mode = MODE(m) if m in MODE._value2member_map_ else None
            self.temp = (self.pdata[4] - 70) / 2
            f = (self.pdata[1] >> 5) & 7
            self.fanspeed = FANSPEED(f) if f in FANSPEED._value2member_map_ else None
            # byte[8] bit 0 is the energy-saving flag. The other bits are unrelated (the app
            # reads bits 6-7 as the ECONAVI state), so the byte must not be tested whole.
            self.powersave = self.pdata[8] & 0x01 if len(self.pdata) > 8 else 0
            # byte[3] is the operation-PROHIBITED / RC-lock bitmask (which functions are
            # locked/restricted: bit0 start-stop, bit1 mode, bit2 set-temp, bit3 flap,
            # bit4 fan speed, bit5 energy-saving). In the official app it drives the
            # "layout_prohibited" lock overlay.
            self.prohibited = bool(self.pdata[3]) if len(self.pdata) > 3 else False
            # byte[2] bit 0x02 is the wall controller's standby icon, "the indoor unit is
            # stopped or slight blow operation is in progress": SET = indoor fan held,
            # CLEAR = fan running normally. In HEATING the fan is held whenever the
            # compressor is off, so there it doubles as an idle flag (how it was first
            # calibrated). In COOLING the fan keeps running while the compressor is off, so
            # the bit stays clear and says nothing about the compressor: in 12 days of
            # recorded history (about 43 hours of cooling, in four sessions) it never set,
            # though the compressor was stopped 75% of that cooling time. The official app does not read this bit at all. It is therefore only a
            # fallback for the running state -- see PanasonicHC.compressor_running. None
            # when the byte isn't present.
            self.running = (self.pdata[2] & 0x02) == 0 if len(self.pdata) > 2 else None

            if len(self.pdata) >= 6:
                self.curtemp = (self.pdata[5] - 70) / 2

        def __str__(self):
            s = super().__str__()
            s += f"\nTemp: {self.temp}"
            if self.curtemp is not None:
                s += f" ({self.curtemp})"
            s += f'\nPower: { "on" if self.power else "off" }'
            s += f"\nMode: {self.mode.name if self.mode else self.mode_code}"
            s += f"\nFan: {self.fanspeed.name if self.fanspeed else '?'}"
            s += f'\nPowersave: { "on" if self.powersave else "off" }'
            return s

    class PanasonicBLEPacketOutdoorTemp(PanasonicBLEPacket):
        def __init__(self, ptype, pdata):
            super().__init__(ptype, pdata)
            # Field 0x21 (33) is a signed big-endian 16-bit value in tenths of a degree C
            # (decode confirmed from the decompiled SensorTempParser). The device reports
            # several sensors distinguished by a sub-index the wire layout here doesn't
            # expose; raw 0x7FFF / -0x8000 are the "no data" sentinels (9999 / 10000).
            # NOTE: apart from the DEBUG-only capability probe, whose replies are logged and
            # not applied, nothing requests this field; the Outdoor Temperature entity is fed
            # by monitor code 0x11 (a 0x21 field arriving unasked would also update it). The
            # app reads channel 0x20
            # from the outdoor unit for its main-screen outdoor temperature; the reply does
            # not say which channel it answers.
            if len(pdata) < 2:
                raise ValueError("short 0x21 field")
            raw = int.from_bytes(pdata[0:2], "big", signed=True)
            self.temp = None if raw in (0x7FFF, -0x8000) else raw / 10

        def __str__(self):
            s = super().__str__()
            s += f"\nOutdoor Temp: {self.temp}"
            return s

    class PanasonicBLEPacketError(PanasonicBLEPacket):
        def __init__(self, ptype, pdata):
            super().__init__(ptype, pdata)
            # Field 0x27 (39) alert-HISTORY entry (the app's "Service check" list, which
            # keeps an alarm until the history is cleared -- it is not a live fault flag):
            # pdata[0..1] = identity word of the unit that raised it, pdata[2] = error code
            # byte. Decode per the decompiled ErrorCode class: letter = "ACEFHJLP"[(b>>5)&7],
            # number = b & 0x1F. "A00" means no entry.
            if len(pdata) < 3:
                raise ValueError("short 0x27 field")
            code = pdata[2]
            self.error = f"{'ACEFHJLP'[(code >> 5) & 7]}{code & 0x1F:02d}"

    class PanasonicBLEPacketNanoe(PanasonicBLEPacket):
        def __init__(self, ptype, pdata):
            super().__init__(ptype, pdata)
            # Field 0x5C (92): byte[1] bit0 = nanoeX on, bit2 = internal cleaning on
            # (decompiled NanoeXSettingsInteractor). Only when byte[0] is 0: with byte[0] = 1
            # the same field carries the nanoeX generator health code instead.
            if pdata[0] != 0:
                raise ValueError("0x5C field is not an on/off state")
            self.nanoe = bool(pdata[1] & 1)

    class PanasonicBLEPacketConsumption(PanasonicBLEPacket):
        def __init__(self, ptype, pdata):
            self.hour = None
            self.index = None
            self.pos = None
            self.values = None

            super().__init__(ptype, pdata)
            if pdata[3] == 1:
                self.hour = pdata[8]
            elif pdata[3] == 2:
                self.index = pdata[11]
            elif pdata[3] >= 3 and pdata[3] <= 14:
                # convert remaining data to two-byte floats
                self.pos = (pdata[3] - 3) * 4
                self.values = [
                    ((pdata[x + 4] << 8) + (pdata[x + 5] & 255)) / 10
                    for x in range(0, len(pdata) - 4, 2)
                ]

    class COMPONENT(Enum):
        I_UNIT1 = 1
        O_UNIT1 = 9
        ALL_UNITS = 247
        APP = 249
        ALL_I_UNIT = 251
        BLE_MODULE_UART = 254

    class OPERATION(Enum):
        SET = 0
        SET_RES = 1
        REQ = 2
        REQ_RES = 3
        NOTIFY = 4

    def __init__(self, src=None, dst=None, op=None, packets=None):
        self.src = self.COMPONENT[src]
        self.dst = self.COMPONENT[dst]
        self.op = self.OPERATION[op]
        self.packets = packets

    @staticmethod
    def parse(data: bytes):
        """Construct PanasonicBLEParcel from fd."""

        data = _decode(data)
        fd = io.BytesIO(data)

        if fd.read(1)[0] != 0x11:
            raise ValueError("Bad packet")

        src = PanasonicBLEParcel.COMPONENT(fd.read(1)[0])
        dst = PanasonicBLEParcel.COMPONENT(fd.read(1)[0])
        op = PanasonicBLEParcel.OPERATION(fd.read(1)[0])

        num_packets = fd.read(1)[0]
        packets = [
            PanasonicBLEParcel.PanasonicBLEPacket.parse(fd) for _ in range(num_packets)
        ]

        return PanasonicBLEParcel(
            src=src.name, dst=dst.name, op=op.name, packets=packets
        )

    def encode(self):
        fd = io.BytesIO()
        fd.write(b"\x11")
        fd.write(self.src.value.to_bytes())
        fd.write(self.dst.value.to_bytes())
        fd.write(self.op.value.to_bytes())

        fd.write(len(self.packets).to_bytes())
        for p in self.packets:
            fd.write(p.encode())

        fd.write(b"\x00")  # cksum

        return _encode(fd.getvalue())

    def __str__(self):
        s = f"{self.src.name} => {self.dst.name} {self.op.name}\n"
        for p in self.packets:
            s += f"\t{p}\n"
        return s

    def __iter__(self):
        self.idx = 0
        return self

    def __next__(self):
        if self.idx >= len(self.packets):
            raise StopIteration

        pkt = self.packets[self.idx]
        self.idx += 1
        return pkt


class PanasonicBLEMode(PanasonicBLEParcel):
    def __init__(self, mode):
        super().__init__(
            src="APP",
            dst="I_UNIT1",
            op="SET",
            packets=[PanasonicBLEParcel.PanasonicBLEPacket(66, bytes([mode]))],
        )


class PanasonicBLEFanMode(PanasonicBLEParcel):
    def __init__(self, mode, mode_nibble):
        # Fan speed is stored per operating mode. The 0x4C packet's low nibble selects
        # which mode's fan profile to write; 0x10 marks the fan field present. The
        # original code only wrote nibbles 1 (heat) and 2 (cool), so changing fan speed
        # had no effect in fan_only/dry/auto. Target the current mode instead.
        super().__init__(
            src="APP",
            dst="I_UNIT1",
            op="SET",
            packets=[
                PanasonicBLEParcel.PanasonicBLEPacket(
                    76, bytes([0x10 | mode_nibble, mode, 0, 0])
                ),
            ],
        )


class PanasonicBLEEnergySaving(PanasonicBLEParcel):
    def __init__(self, state):
        state = 11 if state else 9
        super().__init__(
            src="APP",
            dst="I_UNIT1",
            op="SET",
            packets=[PanasonicBLEParcel.PanasonicBLEPacket(84, bytes([state]))],
        )


class PanasonicBLEPower(PanasonicBLEParcel):
    def __init__(self, state):
        state += 2  # 2==OFF, 3==ON
        super().__init__(
            src="APP",
            dst="I_UNIT1",
            op="SET",
            packets=[PanasonicBLEParcel.PanasonicBLEPacket(65, bytes([state]))],
        )


class PanasonicBLEStatusReq(PanasonicBLEParcel):
    def __init__(self):
        super().__init__(
            src="APP",
            dst="I_UNIT1",
            op="REQ",
            packets=[PanasonicBLEParcel.PanasonicBLEPacket(129, bytes([4, 0, 14]))],
        )


class PanasonicBLEErrorReq(PanasonicBLEParcel):
    def __init__(self):
        # Read the most-recent alert-history entry (index 1) for field 0x27.
        super().__init__(
            src="APP",
            dst="I_UNIT1",
            op="REQ",
            packets=[PanasonicBLEParcel.PanasonicBLEPacket(39, bytes([1, 3]))],
        )


class PanasonicBLEMonitorReq(PanasonicBLEParcel):
    def __init__(self, code):
        # Read a service-monitor sensor by its data-number (DN) code via field 0x2C
        # ("RC monitor"), exactly as the app's Sensor Info screen does. Payload =
        # [unitWord_hi, unitWord_lo, code, 0x02]; 0x0800 is the unit word for a single
        # indoor unit (group 1 / address 1). Reply 0x2C = signed big-endian 16-bit value.
        #
        # DESTINATION = O_UNIT1 (the outdoor unit), not the indoor unit. The app's
        # SensorInfoQueryCommands sends every 0x2C DN-code read to `indoorUnit.d`, which
        # UnitStructureParser sets to the *synthesised outdoor* address O_UNIT1 (= 9), while
        # only the 0x21 analog-temp read (i==256) and the 0x69 RC-temp reads go to the indoor
        # side. These DN codes are outdoor/refrigerant-circuit sensors (outdoor air, discharge,
        # compressor current, indoor coil reported by the outdoor controller), so the outdoor
        # unit answers them. Addressing 0x2C to I_UNIT1 — as this did before — gets no reply at
        # all (confirmed by a 2 h frame capture with zero 0x2C frames).
        super().__init__(
            src="APP",
            dst="O_UNIT1",
            op="REQ",
            packets=[PanasonicBLEParcel.PanasonicBLEPacket(0x2C, bytes([0x08, 0x00, code, 0x02]))],
        )


class PanasonicBLEIconReq(PanasonicBLEParcel):
    def __init__(self, code):
        # Read a status-icon (SettingIcon mDn `code`) via the 0x69 sub-23 "setting function"
        # query, as the app's Information screen does. Reply 0x69 value = [2,0,23,code,active];
        # active (byte[4]) != 0 means the icon is lit. Codes: 9 heat-preparing, 10 op-preparing,
        # 11 defrost. Sent to the BLE module (which aggregates the controller's icon state).
        super().__init__(
            src="APP",
            dst="BLE_MODULE_UART",
            op="REQ",
            packets=[PanasonicBLEParcel.PanasonicBLEPacket(0x69, bytes([2, 0, 23, code, 5]))],
        )


class PanasonicBLENanoe(PanasonicBLEParcel):
    def __init__(self, on):
        # Field 0x5C nanoeX: byte[1] bit1 (0x02) = "field present", bit0 (0x01) = on.
        # So ON = [0x00, 0x03], OFF = [0x00, 0x02]. Sent to all indoor units, matching
        # the official app's NanoeXSettingsInteractor.
        super().__init__(
            src="APP",
            dst="ALL_I_UNIT",
            op="SET",
            packets=[
                PanasonicBLEParcel.PanasonicBLEPacket(92, bytes([0, 3 if on else 2]))
            ],
        )


class PanasonicBLEPowerReq(PanasonicBLEParcel):
    def __init__(self):
        super().__init__(
            src="APP",
            dst="BLE_MODULE_UART",
            op="REQ",
            packets=[
                PanasonicBLEParcel.PanasonicBLEPacket(105, bytes([2, 0, 19, x, 12]))
                for x in range(1, 3)
            ],
        )


class PanasonicBLEPowerReqHour(PanasonicBLEParcel):
    def __init__(self):
        super().__init__(
            src="APP",
            dst="BLE_MODULE_UART",
            op="REQ",
            packets=[
                PanasonicBLEParcel.PanasonicBLEPacket(105, bytes([2, 0, 19, x, 12]))
                for x in range(3, 15)
            ],
        )


class PanasonicBLETemp(PanasonicBLEParcel):
    def __init__(self, temp, mode_nibble=None):
        # 0x4C with the 0x08 "setpoint present" flag; the low nibble selects WHICH mode's
        # stored setpoint is written (1 heat, 2 cool, 4 dry, 5 auto/auto-heat, 6 auto-cool).
        # Heat and cool are always written, as before. If the unit is currently in another
        # mode that has a setpoint (dry / auto), pass its nibble so that profile is written
        # too -- otherwise the change does not apply to the mode actually running.
        temp = int(temp * 2 + 70)
        nibbles = [1, 2]
        if mode_nibble in (4, 5, 6):
            nibbles.append(mode_nibble)
        super().__init__(
            src="APP",
            dst="I_UNIT1",
            op="SET",
            packets=[
                PanasonicBLEParcel.PanasonicBLEPacket(76, bytes([0x08 | n, 0, temp, 0]))
                for n in nibbles
            ],
        )


class PanasonicBLEReadReq(PanasonicBLEParcel):
    """Read (REQ) one or more fields from one address.

    Used by the DEBUG-only capability probe. The operation is fixed to REQ, so this class
    cannot build a write whatever it is given."""

    def __init__(self, dst, fields):
        super().__init__(
            src="APP",
            dst=dst,
            op="REQ",
            packets=[
                PanasonicBLEParcel.PanasonicBLEPacket(fid, bytes(value))
                for fid, value in fields
            ],
        )


class PanasonicBLEOuting(PanasonicBLEParcel):
    def __init__(self, state):
        super().__init__(
            src="APP",
            dst="BLE_MODULE_UART",
            op="SET",
            packets=[
                PanasonicBLEParcel.PanasonicBLEPacket(105, bytes([0, 0, 17, 2, state]))
            ],
        )
