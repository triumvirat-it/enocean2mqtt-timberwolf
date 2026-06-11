"""Tests für ESP3-Frame-Parser. Hauptfokus: CRC, Frame-Sync-Recovery, ERP1-Decoding."""
from __future__ import annotations

import pytest

from app.gateway.esp3 import (
    ESP3Packet,
    ESP3StreamParser,
    PacketType,
    RORG,
    crc8,
    decode_radio_erp1,
)


def test_crc8_known_vector():
    # ESP3 Spec V1.27, Sektion 1.3 — bekannter Testvektor
    assert crc8(b"\x00") == 0x00
    assert crc8(b"\x01") == 0x07
    assert crc8(b"\x00\x07\x07\x01") == 0x7A  # Beispielheader F6 RPS


def test_roundtrip_packet_serialize_and_parse():
    pkt = ESP3Packet(
        packet_type=PacketType.RADIO_ERP1,
        data=bytes.fromhex("F6500102030405"),  # RPS, sender 01020304, status 05
        optional=bytes.fromhex("00FFFFFFFF4A00"),  # subTel=0, dest=broadcast, rssi=-74, sec=0
    )
    raw = pkt.to_bytes()
    parser = ESP3StreamParser()
    parser.feed(raw)
    out = parser.iter_packets()
    assert len(out) == 1
    assert out[0].packet_type == PacketType.RADIO_ERP1
    assert out[0].data == pkt.data
    assert out[0].optional == pkt.optional


def test_parser_recovers_from_garbage_prefix():
    pkt = ESP3Packet(packet_type=0x01, data=b"\xF6\x50\x01\x02\x03\x04\x30", optional=b"")
    raw = b"\xDE\xAD\xBE\xEF" + pkt.to_bytes()
    parser = ESP3StreamParser()
    parser.feed(raw)
    out = parser.iter_packets()
    assert len(out) == 1


def test_parser_handles_split_chunks():
    pkt = ESP3Packet(packet_type=0x01, data=b"\xF6\x50\x01\x02\x03\x04\x30", optional=b"")
    raw = pkt.to_bytes()
    parser = ESP3StreamParser()
    parser.feed(raw[:3])
    assert parser.iter_packets() == []
    parser.feed(raw[3:])
    out = parser.iter_packets()
    assert len(out) == 1


def test_parser_rejects_bad_header_crc():
    pkt = ESP3Packet(packet_type=0x01, data=b"\xF6\x50\x01\x02\x03\x04\x30", optional=b"")
    raw = bytearray(pkt.to_bytes())
    raw[5] ^= 0xFF  # Header-CRC kaputt
    parser = ESP3StreamParser()
    parser.feed(bytes(raw))
    # → ein Byte (Sync) wird verworfen, Rest enthält keinen gültigen Frame mehr
    assert parser.iter_packets() == []


def test_decode_erp1_rps_button():
    """F6-02-01 Wippe gedrückt — typisches PTM-Telegramm."""
    data = bytes([RORG.RPS, 0x50, 0x01, 0x02, 0x03, 0x04, 0x30])  # RORG, payload, sender, status
    optional = bytes([0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0x4A, 0x00])  # subTel=0, dest=broadcast, rssi=-74
    pkt = ESP3Packet(packet_type=PacketType.RADIO_ERP1, data=data, optional=optional)
    tel = decode_radio_erp1(pkt)
    assert tel is not None
    assert tel.rorg == RORG.RPS
    assert tel.sender_id == 0x01020304
    assert tel.sender_id_hex == "01020304"
    assert tel.payload == b"\x50"
    assert tel.status == 0x30
    assert tel.rssi_dbm == -0x4A


def test_decode_returns_none_for_non_radio():
    pkt = ESP3Packet(packet_type=PacketType.RESPONSE, data=b"\x00", optional=b"")
    assert decode_radio_erp1(pkt) is None
