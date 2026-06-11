"""
ESP3-Frame-Parser für EnOcean Serial Protocol 3.

Frame-Aufbau:
    Sync (0x55) | DataLen (2B BE) | OptLen (1B) | PacketType (1B) | HeaderCRC8
    | Data (DataLen) | OptData (OptLen) | DataCRC8

Reference: EnOcean Serial Protocol 3 (ESP3) Specification V1.27.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

SYNC_BYTE = 0x55


class PacketType(IntEnum):
    RADIO_ERP1 = 0x01
    RESPONSE = 0x02
    RADIO_SUB_TEL = 0x03
    EVENT = 0x04
    COMMON_COMMAND = 0x05
    SMART_ACK_COMMAND = 0x06
    REMOTE_MAN_COMMAND = 0x07
    RADIO_MESSAGE = 0x09
    RADIO_ERP2 = 0x0A


# CRC8 Lookup-Tabelle (Polynom 0x07, Init 0x00). Standard für ESP3.
def _build_crc8_table() -> list[int]:
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
        table.append(crc)
    return table


_CRC8_TABLE = _build_crc8_table()


def crc8(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = _CRC8_TABLE[crc ^ b]
    return crc


class ESP3DecodeError(Exception):
    pass


@dataclass(slots=True)
class ESP3Packet:
    packet_type: int
    data: bytes
    optional: bytes

    def to_bytes(self) -> bytes:
        data_len = len(self.data)
        opt_len = len(self.optional)
        header = struct.pack(">HBB", data_len, opt_len, self.packet_type)
        header_crc = crc8(header)
        body = self.data + self.optional
        body_crc = crc8(body)
        return bytes([SYNC_BYTE]) + header + bytes([header_crc]) + body + bytes([body_crc])

    @property
    def packet_type_name(self) -> str:
        try:
            return PacketType(self.packet_type).name
        except ValueError:
            return f"UNKNOWN_0x{self.packet_type:02X}"


class ESP3StreamParser:
    """
    Stateful ESP3-Frame-Parser für TCP/Serial-Streams.

    Wirf bytes per `feed(data)` rein, hole fertige Pakete via `iter_packets()`.
    Tolerant gegenüber Garbage zwischen Frames — überspringt bis zum nächsten 0x55.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> None:
        self._buf.extend(chunk)

    def iter_packets(self) -> list[ESP3Packet]:
        packets: list[ESP3Packet] = []
        while True:
            pkt = self._try_extract()
            if pkt is None:
                break
            packets.append(pkt)
        return packets

    def _try_extract(self) -> ESP3Packet | None:
        # Sync-Byte suchen
        while self._buf and self._buf[0] != SYNC_BYTE:
            self._buf.pop(0)

        # Header braucht 6 Bytes ab Sync (sync + 4 header + 1 hdr-crc)
        if len(self._buf) < 6:
            return None

        data_len = (self._buf[1] << 8) | self._buf[2]
        opt_len = self._buf[3]
        packet_type = self._buf[4]
        header_crc_expected = self._buf[5]

        # Plausibilität (max sinnvolle Größe für ESP3-Funk ~ 263 Bytes)
        if data_len > 2048 or opt_len > 255:
            # Garbage — Sync verwerfen, neu suchen
            self._buf.pop(0)
            return self._try_extract()

        header_crc_actual = crc8(bytes(self._buf[1:5]))
        if header_crc_actual != header_crc_expected:
            self._buf.pop(0)
            return self._try_extract()

        total_len = 6 + data_len + opt_len + 1
        if len(self._buf) < total_len:
            return None  # warten auf mehr Bytes

        data = bytes(self._buf[6 : 6 + data_len])
        optional = bytes(self._buf[6 + data_len : 6 + data_len + opt_len])
        data_crc_expected = self._buf[total_len - 1]
        data_crc_actual = crc8(data + optional)

        if data_crc_actual != data_crc_expected:
            self._buf.pop(0)
            return self._try_extract()

        # Frame konsumieren
        del self._buf[:total_len]
        return ESP3Packet(packet_type=packet_type, data=data, optional=optional)


# ---------------------------------------------------------------------------
# RADIO_ERP1 Telegramm-Decoder (für M1: Sender-ID + RSSI auspacken)
# ---------------------------------------------------------------------------


class RORG(IntEnum):
    RPS = 0xF6
    BS1 = 0xD5
    BS4 = 0xA5
    VLD = 0xD2
    MSC = 0xD1
    ADT = 0xA6
    SEC = 0x30
    SEC_ENCAPS = 0x31
    UTE = 0xD4
    CHAIN = 0x40


@dataclass(slots=True)
class RadioTelegram:
    rorg: int
    payload: bytes  # Daten OHNE RORG, OHNE Sender-ID, OHNE Status
    sender_id: int  # 32-bit
    status: int
    sub_tel: int | None  # aus optional
    destination_id: int | None  # aus optional
    rssi_dbm: int | None  # aus optional (negiert)
    security_level: int | None
    # M81b: ESP3-Packet-Typ. Default RADIO_ERP1 (0x01) — bei non-ERP1 Pakete
    # (REMOTE_MAN_COMMAND, RADIO_MESSAGE, RESPONSE etc.) wird hier der echte
    # Typ-Name eingetragen damit das Live-Log auch ReMan-Telegramme zeigt.
    packet_type_name: str = "RADIO_ERP1"

    @property
    def sender_id_hex(self) -> str:
        return f"{self.sender_id:08X}"

    @property
    def destination_id_hex(self) -> str | None:
        return f"{self.destination_id:08X}" if self.destination_id is not None else None

    @property
    def rorg_name(self) -> str:
        # Bei non-ERP1 zeigen wir den Packet-Typ-Namen (REMOTE_MAN_COMMAND etc.)
        # statt den RORG. So sieht der User im Live-Log direkt was reinkam.
        if self.packet_type_name != "RADIO_ERP1":
            return self.packet_type_name
        try:
            return RORG(self.rorg).name
        except ValueError:
            return f"0x{self.rorg:02X}"

    @property
    def is_raw_packet(self) -> bool:
        """True wenn dies ein nicht-RADIO_ERP1 ESP3-Paket ist (ReMan, SmartAck, ...)."""
        return self.packet_type_name != "RADIO_ERP1"


def decode_radio_erp1(packet: ESP3Packet) -> RadioTelegram | None:
    """
    Dekodiert ein RADIO_ERP1-Frame in Sender-ID, Payload, RSSI etc.
    Returns None wenn packet_type != RADIO_ERP1.
    """
    if packet.packet_type != PacketType.RADIO_ERP1:
        return None
    data = packet.data
    if len(data) < 6:  # RORG + min payload + sender-id(4) + status(1)
        raise ESP3DecodeError(f"ERP1-Frame zu kurz: {len(data)} Bytes")

    rorg = data[0]
    sender_id = int.from_bytes(data[-5:-1], "big")
    status = data[-1]
    payload = data[1:-5]

    sub_tel: int | None = None
    destination_id: int | None = None
    rssi_dbm: int | None = None
    security_level: int | None = None

    opt = packet.optional
    if len(opt) >= 7:
        sub_tel = opt[0]
        destination_id = int.from_bytes(opt[1:5], "big")
        rssi_dbm = -opt[5]  # in dBm, negativ
        security_level = opt[6]

    return RadioTelegram(
        rorg=rorg,
        payload=payload,
        sender_id=sender_id,
        status=status,
        sub_tel=sub_tel,
        destination_id=destination_id,
        rssi_dbm=rssi_dbm,
        security_level=security_level,
    )


def decode_any_packet(packet: ESP3Packet) -> RadioTelegram:
    """
    M81b: Liefert IMMER ein RadioTelegram — auch fuer non-ERP1 Pakete
    (REMOTE_MAN_COMMAND, RADIO_MESSAGE, SMART_ACK, RESPONSE, EVENT etc.).

    Bei ERP1: vollstaendige Dekodierung (wie decode_radio_erp1).
    Bei sonstigen Pakettypen: synthetisches Telegramm mit:
      - rorg = 0
      - sender_id = 0
      - payload = pkt.data
      - packet_type_name = pkt.packet_type_name (zeigt sich im Live-Log)

    So koennen wir z.B. ReMan-Pairing-Telegramme im Live-Log sichtbar machen.
    """
    if packet.packet_type == PacketType.RADIO_ERP1:
        try:
            t = decode_radio_erp1(packet)
            if t is not None:
                return t
        except ESP3DecodeError:
            pass
    # M85c: REMOTE_MAN_COMMAND (0x07) — Source-ID + RSSI aus optional ziehen.
    # ESP3 REMOTE_MAN_COMMAND optional (RX):
    #   [Destination-ID 4B][Source-ID 4B][dBm 1B][SendWithDelay 1B]
    # Damit zeigt das Live-Log WER ein ReMan-Telegramm gesendet hat
    # (Gateway vs. Aktor-Antwort) statt ueberall 00000000.
    sender_id = 0
    destination_id = None
    rssi_dbm = None
    if packet.packet_type == PacketType.REMOTE_MAN_COMMAND:
        opt = packet.optional
        if len(opt) >= 8:
            destination_id = int.from_bytes(opt[0:4], "big")
            sender_id = int.from_bytes(opt[4:8], "big")
        if len(opt) >= 9:
            rssi_dbm = -opt[8]
    return RadioTelegram(
        rorg=0,
        payload=packet.data,
        sender_id=sender_id,
        status=0,
        sub_tel=None,
        destination_id=destination_id,
        rssi_dbm=rssi_dbm,
        security_level=None,
        packet_type_name=packet.packet_type_name,
    )
