"""Tests fuer Telegramm-Encoder."""
from __future__ import annotations

from app.encoders import (
    encode_4bs_teach_in,
    encode_a5_10_03_setpoint,
    encode_a5_10_setpoint,
    encode_a5_38_08_dim,
    encode_a5_38_08_switch,
    encode_eltako_shutter,
    encode_eltako_switch,
    encode_f6_02_button,
)
from app.eep.profiles import decode_a5_10_room_panel
from app.gateway.esp3 import (
    ESP3StreamParser,
    PacketType,
    RORG,
    decode_radio_erp1,
)


def test_a5_10_setpoint_neutral_roundtrip():
    # Heizregler-TX: Ist 21°C, Verschiebung 0, Tag. Bereich ±10.
    frame = encode_a5_10_setpoint(
        0xFF99DC64, temperature_c=21.0, setpoint_offset_c=0.0, day=True)
    pkt = _roundtrip(frame)
    assert pkt.packet_type == PacketType.RADIO_ERP1
    tel = decode_radio_erp1(pkt)
    assert tel.rorg == RORG.BS4
    assert tel.sender_id == 0xFF99DC64
    db3, db2, db1, db0 = tel.payload
    assert db2 == 128            # Verschiebung 0 -> Mitte
    assert db0 == 0x09           # Daten-Bit (0x08) + Tag (bit0=1)
    dec = decode_a5_10_room_panel(tel.payload, tel.status, tel.rorg)
    assert abs(dec["temperature_c"] - 21.0) < 0.3
    assert abs(dec["setpoint_offset_c"]) < 0.2
    assert dec["day"] is True


def test_a5_10_setpoint_offset_temp_night():
    frame = encode_a5_10_setpoint(
        0x01020304, temperature_c=18.0, setpoint_offset_c=5.0,
        offset_min_c=-10, offset_max_c=10, day=False)
    tel = decode_radio_erp1(_roundtrip(frame))
    db3, db2, db1, db0 = tel.payload
    assert db2 == 191            # (5-(-10))/20*255
    assert db0 == 0x08           # Nacht (bit0=0), Daten-Bit gesetzt
    dec = decode_a5_10_room_panel(tel.payload, tel.status, tel.rorg)
    assert abs(dec["temperature_c"] - 18.0) < 0.3
    assert abs(dec["setpoint_offset_c"] - 5.0) < 0.3
    assert dec["day"] is False


def test_a5_10_setpoint_range_clamps():
    # Unterer Anschlag -> DB2=0, Ueberschreitung -> geklemmt auf 255.
    low = decode_radio_erp1(_roundtrip(encode_a5_10_setpoint(
        0x1, setpoint_offset_c=-10, offset_min_c=-10, offset_max_c=10)))
    assert low.payload[1] == 0
    high = decode_radio_erp1(_roundtrip(encode_a5_10_setpoint(
        0x1, setpoint_offset_c=20, offset_min_c=-10, offset_max_c=10)))
    assert high.payload[1] == 255


def test_eltakos_lerntelegramm_wird_woertlich_gesendet():
    """
    Eltako gibt die Lerntelegramme seiner Sensoren byteweise vor
    ("Inhalte der Eltako-Funktelegramme"): FTR78S (A5-10-03) = 40 18 2D 80.
    Die allgemeine Rechnung aus EEP + Hersteller-ID kommt auf 40 18 0D 80 —
    ein Byte daneben. Wo der Hersteller die Bytes nennt, gelten seine.
    """
    from app.encoders import encode_4bs_raw

    gerechnet = decode_radio_erp1(_roundtrip(
        encode_4bs_teach_in(0xFF800016, 0x10, 0x03, manufacturer_id=0x00D)))
    assert bytes(gerechnet.payload).hex().upper() == "40180D80"

    woertlich = decode_radio_erp1(_roundtrip(
        encode_4bs_raw(0xFF800016, bytes.fromhex("40182D80"))))
    assert bytes(woertlich.payload).hex().upper() == "40182D80"
    assert woertlich.sender_id == 0xFF800016
    assert woertlich.rorg == RORG.BS4


def test_a5_10_03_sendet_wie_ein_echtes_raumbedienteil():
    """
    Der Stellventilaktor (OPUS GN-A-U230V-HKR-1) ist auf A5-10-03 angelernt.
    Wir bilden das Raumbedienteil nach — also muss das Telegramm aussehen wie
    das eines Raumbedienteils.

    DB0 = 0x0F ist gemessen, nicht aus der Spezifikation: OPUS RTS55
    senden 0x0F (z.B. 00 7A 70 0F). Mit dem spezifikations-
    treuen 0x08 (nur das Daten-Bit) hat der Aktor nicht
    reagiert; bit0 = 0 heisst in dieser Familie "Nacht/abwesend".
    """
    frame = encode_a5_10_03_setpoint(
        0xFF800016, temperature_c=21.5, setpoint_c=19.0)
    tel = decode_radio_erp1(_roundtrip(frame))
    db3, db2, db1, db0 = tel.payload
    assert tel.sender_id == 0xFF800016
    assert db3 == 0x00           # nicht belegt
    assert db0 == 0x0F           # wie die RTS55 im Haus
    dec = decode_a5_10_room_panel(tel.payload, tel.status, tel.rorg)
    assert abs(dec["temperature_c"] - 21.5) < 0.3


def test_a5_10_03_sollwert_ist_absolut_8_bis_30_grad():
    """
    Eltako, "Inhalte der Eltako-Funktelegramme", FTR78S (A5-10-03):
    `Data_byte2 = Solltemperatur 8..30 °C (0..255)`.

    Das ist KEINE Verschiebung. Mit der Verschiebungs-Rechnung der Thermokon-
    Regler landete unser ueblicher Wert 0x33 beim FHK61 bei 12,4 °C — der
    Kreis waere zugeblieben, ohne dass irgendwo ein Fehler auftaucht.
    """
    def db2(soll):
        return decode_radio_erp1(_roundtrip(
            encode_a5_10_03_setpoint(0x1, setpoint_c=soll))).payload[1]

    assert db2(8.0) == 0        # unterer Anschlag der Skala
    assert db2(30.0) == 255     # oberer Anschlag
    assert db2(19.0) == 128     # Mitte: (19-8)/22*255 = 127,5 -> 128
    assert db2(21.0) == 151     # (21-8)/22*255 = 150,7
    # Ausserhalb der Skala wird geklemmt, nicht umgebrochen.
    assert db2(40.0) == 255
    assert db2(0.0) == 0


def test_a5_10_03_profil_kann_senden():
    """
    Ohne Encoder am Profil baut der TXRouter kein Telegramm und der Aktor
    bleibt still — genau der Zustand vor dem 24.09.2026.
    """
    from app.eep import get_profile_registry

    profil = get_profile_registry().get("A5-10-03")
    assert profil is not None and profil.encoder is not None
    frame = profil.encoder(0xFF800016, {"temperature": 20.0,
                                        "setpoint": 19.0})
    assert frame is not None
    tel = decode_radio_erp1(_roundtrip(frame))
    assert tel.payload[3] == 0x0F
    assert tel.payload[1] == 128

    # Kommt nur eine Verschiebung an, wird sie mit dem Basissollwert des
    # Kanals in eine Temperatur umgerechnet (21 - 2 = 19 °C).
    gemischt = profil.encoder(0xFF800016, {"temperature": 20.0,
                                           "setpoint_offset": -2.0,
                                           "base": 21.0})
    assert decode_radio_erp1(_roundtrip(gemischt)).payload[1] == 128


def _roundtrip(frame):
    """Frame serialisieren + wieder parsen, gibt den geparsten Decoder zurueck."""
    raw = frame.packet.to_bytes()
    parser = ESP3StreamParser()
    parser.feed(raw)
    packets = parser.iter_packets()
    assert len(packets) == 1
    return packets[0]


def test_f6_02_button_press_ai():
    frame = encode_f6_02_button(0x01020304, "AI", pressed=True)
    pkt = _roundtrip(frame)
    assert pkt.packet_type == PacketType.RADIO_ERP1
    tel = decode_radio_erp1(pkt)
    assert tel.rorg == RORG.RPS
    assert tel.sender_id == 0x01020304
    # 0x00=AI, +Energy bow 0x10 -> 0x10
    assert tel.payload == bytes([0x10])


def test_f6_02_button_press_b0():
    frame = encode_f6_02_button(0x11223344, "B0", pressed=True)
    pkt = _roundtrip(frame)
    tel = decode_radio_erp1(pkt)
    # 0b011 << 5 = 0x60, +0x10 = 0x70
    assert tel.payload == bytes([0x70])


def test_eltako_switch_on_off():
    on = encode_eltako_switch(0xDEADBEEF, True)
    off = encode_eltako_switch(0xDEADBEEF, False)
    pkt_on = _roundtrip(on)
    pkt_off = _roundtrip(off)

    tel_on = decode_radio_erp1(pkt_on)
    tel_off = decode_radio_erp1(pkt_off)
    assert tel_on.payload == bytes([0x70, 0x00, 0x00, 0x09])
    assert tel_off.payload == bytes([0x50, 0x00, 0x00, 0x09])
    assert tel_on.sender_id == 0xDEADBEEF


def test_eltako_shutter_down_5s():
    frame = encode_eltako_shutter(0xABCDEF01, "down", duration_s=5.0)
    pkt = _roundtrip(frame)
    tel = decode_radio_erp1(pkt)
    # M99: 5.0 s = 50 x 100ms = 0x0032 -> DB3=0x00, DB2=0x32 (16-bit 100ms).
    # down = DB1=0x02, DB0=0x0A (Daten + 100ms-Modus). Spec FSB14/61/71.
    assert tel.payload == bytes([0x00, 0x32, 0x02, 0x0A])


def test_eltako_shutter_up_3s():
    frame = encode_eltako_shutter(0xABCDEF01, "up", duration_s=3.0)
    pkt = _roundtrip(frame)
    tel = decode_radio_erp1(pkt)
    # 3.0 s = 30 x 100ms = 0x001E. up = DB1=0x01, DB0=0x0A.
    assert tel.payload == bytes([0x00, 0x1E, 0x01, 0x0A])


def test_eltako_shutter_stop_has_zero_duration():
    frame = encode_eltako_shutter(0x12345678, "stop")
    pkt = _roundtrip(frame)
    tel = decode_radio_erp1(pkt)
    assert tel.payload == bytes([0x00, 0x00, 0x00, 0x0A])


def test_eltako_shutter_unknown_command_raises():
    import pytest
    with pytest.raises(ValueError):
        encode_eltako_shutter(0x1, "diagonal")


def test_a5_38_08_switch_on():
    frame = encode_a5_38_08_switch(0xFF, True)
    pkt = _roundtrip(frame)
    tel = decode_radio_erp1(pkt)
    # DB3=01 cmd, DB0=09 (data + on)
    assert tel.payload == bytes([0x01, 0x00, 0x00, 0x09])


def test_a5_38_08_dim_75_percent():
    frame = encode_a5_38_08_dim(0xFF, 75, ramp_s=2, on=True)
    pkt = _roundtrip(frame)
    tel = decode_radio_erp1(pkt)
    # DB3=02 dim, DB2=75, DB1=2 (Speed-Code), DB0=09 (data + on)
    # Bit2 (0x04 = "Dimmwert blockieren") MUSS 0 sein, sonst lockt sich
    # der FLD61/FUD/FDG auf den Wert (Eltako-Doku M56).
    assert tel.payload == bytes([0x02, 75, 2, 0x09])


def test_a5_38_08_dim_clamps_value():
    frame = encode_a5_38_08_dim(0xFF, 200)
    pkt = _roundtrip(frame)
    tel = decode_radio_erp1(pkt)
    assert tel.payload[1] == 100


def test_eltako_teach_in_telegrams():
    """M99: Eltako-Lerntelegramme exakt nach 'Inhalte der Eltako-Funktelegramme'.
    Eltako erwartet Hersteller-ID 0x00D (nicht 0x7FF) — sonst ignoriert der
    Aktor das Telegramm. FSB Rolladen (A5-3F-7F) = 0xFFF80D80,
    FSR/FUD (A5-38-08) = 0xE0400D80."""
    fsb = encode_4bs_teach_in(0xFF800106, 0x3F, 0x7F, manufacturer_id=0x00D)
    assert decode_radio_erp1(_roundtrip(fsb)).payload == bytes([0xFF, 0xF8, 0x0D, 0x80])
    fsr = encode_4bs_teach_in(0xFF800106, 0x38, 0x08, manufacturer_id=0x00D)
    assert decode_radio_erp1(_roundtrip(fsr)).payload == bytes([0xE0, 0x40, 0x0D, 0x80])


def test_4bs_teach_in_packs_eep():
    # A5-12-01 (Energiezaehler) Teach-In
    frame = encode_4bs_teach_in(0xAABBCCDD, rorg_func=0x12, rorg_type=0x01)
    pkt = _roundtrip(frame)
    tel = decode_radio_erp1(pkt)
    # DB3 = FUNC(0x12) << 2 | (TYPE_HI >> 5) = 0x48 | 0x00 = 0x48
    assert tel.payload[0] == 0x48
    # DB0 bit3 = 0 -> teach-in
    assert (tel.payload[3] & 0x08) == 0


# --- M82: D2-01-XX Set-Output Encoder ---


def test_d2_01_set_output_on():
    """D2-01 Set Output: OV=1 → AN. Live-verifiziert per Protokoll-Mitschnitt."""
    from app.encoders import encode_d2_01_set_output
    from app.gateway.esp3 import decode_radio_erp1
    frame = encode_d2_01_set_output(0xFFBD00BB, 1, io_channel=0)
    tel = decode_radio_erp1(frame.packet)
    assert tel.rorg == 0xD2   # VLD
    assert tel.payload == bytes([0x01, 0x00, 0x01])
    assert tel.sender_id == 0xFFBD00BB


def test_d2_01_set_output_off():
    """D2-01 Set Output: OV=0 → AUS."""
    from app.encoders import encode_d2_01_set_output
    from app.gateway.esp3 import decode_radio_erp1
    frame = encode_d2_01_set_output(0xFFBD00BB, 0, io_channel=0)
    tel = decode_radio_erp1(frame.packet)
    assert tel.payload == bytes([0x01, 0x00, 0x00])


def test_d2_01_set_output_channel():
    """IO Channel != 0 wird ins Byte 1 gepackt."""
    from app.encoders import encode_d2_01_set_output
    from app.gateway.esp3 import decode_radio_erp1
    frame = encode_d2_01_set_output(0xFFBD00BB, 100, io_channel=5)
    tel = decode_radio_erp1(frame.packet)
    assert tel.payload == bytes([0x01, 0x05, 0x64])


# --- M83: ReMan-Encoder ---


def test_reman_unlock_format():
    """M89: UNLOCK [00 01 07 FF SEC×4], Broadcast → OHNE Optional-Block."""
    from app.encoders import encode_reman_unlock
    frame = encode_reman_unlock(0x11223344)
    assert frame.packet.data == bytes([0x00, 0x01, 0x07, 0xFF, 0x11, 0x22, 0x33, 0x44])
    assert frame.packet.packet_type == 0x07
    assert frame.packet.optional == b""   # Broadcast → kein Optional


def test_reman_lock_format():
    """M89: LOCK [00 02 07 FF SEC×4], Broadcast → ohne Optional."""
    from app.encoders import encode_reman_lock
    frame = encode_reman_lock(0x11223344)
    assert frame.packet.data == bytes([0x00, 0x02, 0x07, 0xFF, 0x11, 0x22, 0x33, 0x44])
    assert frame.packet.optional == b""


def test_reman_query_id_format():
    """M89: QUERY_ID [00 04 07 FF 00 00 00] — 7 Bytes, 3 Null-Daten-Bytes."""
    from app.encoders import encode_reman_query_id
    frame = encode_reman_query_id()
    assert frame.packet.data == bytes([0x00, 0x04, 0x07, 0xFF, 0x00, 0x00, 0x00])
    assert frame.packet.optional == b""


def test_reman_addressed_has_optional():
    """Adressiert (Dest != Broadcast) → Optional-Block Dest+Source+dBm+delay."""
    from app.encoders import encode_reman_unlock
    frame = encode_reman_unlock(0x11223344, sender_id=0x05010001,
                                destination_id=0x01A03001)
    assert len(frame.packet.optional) == 10
    assert frame.packet.optional[0:4] == bytes([0x01, 0xA0, 0x30, 0x01])
    assert frame.packet.optional[4:8] == bytes([0x05, 0x01, 0x00, 0x01])


def test_reman_set_linktable():
    """M90: SetLinkTableContent — Sender FFBD00BB / EEP D2-01-01 in Aktor 01A03001.
    Belegt aus Protokoll-Mitschnitt: data 02 12 07 FF | 00 00 <EOID4> D2 01 01 00."""
    from app.encoders import encode_reman_set_linktable
    frame = encode_reman_set_linktable(
        actor_id=0x01A03001, sender_eoid=0xFFBD00BB,
        eep_rorg=0xD2, eep_func=0x01, eep_type=0x01,
        chip_id=0x050C0002, index=0, channel=0, inbound=True,
    )
    assert frame.packet.data == bytes([
        0x02, 0x12, 0x07, 0xFF,             # Fn 0x0212 + Mfr
        0x00, 0x00,                          # Inbound, Index 0
        0xFF, 0xBD, 0x00, 0xBB,             # Sender-EOID
        0xD2, 0x01, 0x01,                    # EEP
        0x00,                                # Channel
    ])
    # Adressiert → Optional mit Dest=Aktor, Source=Chip
    assert frame.packet.optional[0:4] == bytes([0x01, 0xA0, 0x30, 0x01])
    assert frame.packet.optional[4:8] == bytes([0x05, 0x0C, 0x00, 0x02])
