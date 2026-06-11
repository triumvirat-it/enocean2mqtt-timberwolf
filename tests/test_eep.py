"""Tests für EEP-Decoder."""
from __future__ import annotations

from app.eep import register_default_profiles
from app.eep.profiles import (
    decode_a5_02_temperature,
    decode_a5_04_temp_humidity,
    decode_a5_10_02,
    decode_a5_10_room_panel,
    decode_a5_12_meter,
    decode_a5_13_weather,
    decode_d2_01_actuator,
    decode_d5_00_contact,
    decode_eltako_fsb_shutter,
    decode_f6_02,
)
from app.gateway.esp3 import RadioTelegram


def test_f6_02_rocker_pressed():
    # 0x30 = 0b0011_0000 → R1=001 (A0), EnergyBow=1 (pressed)
    out = decode_f6_02(bytes([0x30]), status=0x30, rorg=0xF6)
    assert out["pressed"] is True
    assert out["rocker_1"] == "A0"
    assert out["rocker_2"] is None


def test_f6_02_rocker_ai_pressed():
    # 0x10 = 0b0001_0000 → R1=000 (AI), EnergyBow=1
    out = decode_f6_02(bytes([0x10]), status=0x30, rorg=0xF6)
    assert out["pressed"] is True
    assert out["rocker_1"] == "AI"


def test_f6_02_release():
    out = decode_f6_02(bytes([0x00]), status=0x20, rorg=0xF6)
    assert out["pressed"] is False


def test_a5_02_05_temperature():
    # DB1=128 → temp = 40 - 128/255*50 ≈ 14.9°C
    out = decode_a5_02_temperature(bytes([0x00, 128, 0x00, 0x08]), 0x00, 0xA5)
    assert "temperature_c" in out
    assert 14.0 < out["temperature_c"] < 16.0


def test_a5_02_teach_in_flag():
    # DB0 bit3 = 0 → teach-in
    out = decode_a5_02_temperature(bytes([0x00, 0x00, 0x00, 0x00]), 0x00, 0xA5)
    assert out.get("teach_in") is True


def test_a5_10_02_full_room_panel():
    # thanos Sensor-Telegramm: [DB3=Luefter, DB2=Sollwert, DB1=Temp, DB0]
    # DB0=0x09 → bit3=1 (Daten), bit0=1 → Raumbelegung "belegt/anwesend".
    # DB3=0xFF → Luefter Automatik; DB2=128 → Sollwert 0; DB1=128 → ~19.9°C.
    out = decode_a5_10_02(bytes([0xFF, 128, 128, 0x09]), 0x00, 0xA5)
    assert 19.0 < out["temperature_c"] < 21.0
    assert out["setpoint_offset_c"] == 0.0
    assert out["fan_stage"] == "auto"
    assert out["fan_raw"] == 0xFF
    # Handbuch: belegt=1 → anwesend
    assert out["occupancy"] is True
    assert out["occupancy_raw"] == 1
    # 'day' wird bei -02 bewusst durch occupancy ersetzt
    assert "day" not in out


def test_a5_10_02_fan_stages_and_vacancy():
    # DB3=0 → Stufe 3; DB0=0x08 → bit0=0 → unbelegt/abwesend
    out = decode_a5_10_02(bytes([0x00, 128, 128, 0x08]), 0x00, 0xA5)
    assert out["fan_stage"] == "3"
    assert out["occupancy"] is False
    assert out["occupancy_raw"] == 0
    # Schwellen-Stichproben laut Handbuch-Tabelle
    assert decode_a5_10_02(bytes([0xFF, 128, 128, 0x08]), 0, 0xA5)["fan_stage"] == "auto"
    assert decode_a5_10_02(bytes([200, 128, 128, 0x08]), 0, 0xA5)["fan_stage"] == "0"
    assert decode_a5_10_02(bytes([170, 128, 128, 0x08]), 0, 0xA5)["fan_stage"] == "1"
    assert decode_a5_10_02(bytes([150, 128, 128, 0x08]), 0, 0xA5)["fan_stage"] == "2"


def test_a5_10_02_teach_in_passthrough():
    # DB0 bit3 = 0 → teach-in; kein Luefter/Belegungs-Feld
    out = decode_a5_10_02(bytes([0x00, 0x00, 0x00, 0x00]), 0x00, 0xA5)
    assert out.get("teach_in") is True
    assert "fan_stage" not in out


def test_a5_12_meter_cumulative():
    # Echte User-Telegramme aus Live-Log (Eltako DSZ14DRS-3x65A) gegen
    # offizielle Eltako-Doku "Inhalte der Eltako-Funktelegramme":
    #   DB0=0x09 -> kWh Normaltarif (data*0.1 = kWh)
    #   DB0=0x19 -> kWh Nachttarif
    #   DB0=0x0C -> W   Normaltarif
    #   DB0=0x1C -> W   Nachttarif

    # 07 10 9F 09: data = 0x07109F = 463007; *0.1 = 46300.7 kWh; Normaltarif
    out = decode_a5_12_meter(bytes([0x07, 0x10, 0x9F, 0x09]), 0x00, 0xA5)
    assert out["tariff"] == 0
    assert out["energy_kwh"] == 46300.7
    assert "current_w" not in out

    # 00 00 00 19: data = 0; Nachttarif kWh = 0
    out = decode_a5_12_meter(bytes([0x00, 0x00, 0x00, 0x19]), 0x00, 0xA5)
    assert out["tariff"] == 1
    assert out["energy_kwh"] == 0.0

    # 00 03 24 0C: data = 0x000324 = 804; Normaltarif W = 804.0
    out = decode_a5_12_meter(bytes([0x00, 0x03, 0x24, 0x0C]), 0x00, 0xA5)
    assert out["tariff"] == 0
    assert out["current_w"] == 804.0
    assert "energy_kwh" not in out

    # Nachttarif Power: DB0=0x1C
    out = decode_a5_12_meter(bytes([0x00, 0x03, 0x24, 0x1C]), 0x00, 0xA5)
    assert out["tariff"] == 1
    assert out["current_w"] == 804.0

    # Seriennummer-Telegramme (DSZ14(W)DRS, BCD-decoded):
    # Teil 1: DB1=0x00, DB3=0x46 -> "46"
    out = decode_a5_12_meter(bytes([0x46, 0x00, 0x00, 0x8F]), 0x00, 0xA5)
    assert "current_w" not in out
    assert "energy_kwh" not in out
    assert out.get("serial_high") == "46"

    # Teil 2: DB1=0x01, DB2=0x02, DB3=0x61 -> "02" + "61" = "0261"
    out = decode_a5_12_meter(bytes([0x61, 0x02, 0x01, 0x8F]), 0x00, 0xA5)
    assert "current_w" not in out
    assert "energy_kwh" not in out
    assert out.get("serial_low") == "0261"

    # Unbekannte DB0-Werte mit gesetztem LRN-Bit (z.B. zukuenftiger Spec-Wert):
    # liefern info-Feld, KEINEN Power/Energy-Wert.
    out = decode_a5_12_meter(bytes([0x01, 0x02, 0x03, 0x4F]), 0x00, 0xA5)
    assert "current_w" not in out
    assert "energy_kwh" not in out
    assert "unknown" in out.get("info", "")


def test_a5_04_temp_humidity():
    # Per EEP-Spec (kipe/enocean EEP.xml):
    #   HUM offset=8  -> payload[1] = DB2 (0..250 -> 0..100%)
    #   TMP offset=16 -> payload[2] = DB1 (0..250 -> 0..40°C)
    # Klare Asymmetrie damit Byte-Vertauschung sofort auffaellt.
    # 60% Humidity -> byte[1] = 60*250/100 = 150 = 0x96
    # 28°C Temperatur -> byte[2] = 28*250/40 = 175 = 0xAF
    out = decode_a5_04_temp_humidity(bytes([0x00, 0x96, 0xAF, 0x08]), 0x00, 0xA5)
    assert out["humidity_pct"] == 60.0
    assert out["temperature_c"] == 28.0


def test_a5_10_03_room_panel():
    # Per EEP-Spec A5-10-03:
    #   SP  offset=8  -> payload[1] = DB2 (linear 0..255, als signed Offset
    #                    auf ±10°C interpretiert: 0x80=0°C)
    #   TMP offset=16 -> payload[2] = DB1 (INVERTED 255..0 -> 0..40°C)
    # Setpoint neutral, Temperatur 22.0°C
    # DB1: 22°C -> (255 - 22*255/40) = (255 - 140.25) ≈ 115 = 0x73
    out = decode_a5_10_room_panel(bytes([0x00, 0x80, 0x73, 0x09]), 0x00, 0xA5)
    assert out["temperature_c"] == 22.0
    assert out["setpoint_offset_c"] == 0.0
    assert out["day"] is True   # DB0 bit 0 = 1


def test_a5_10_03_setpoint_offset_positive():
    # Drehrad +5°C: DB2 = 128 + 5/10 * 127.5 = 191.75 ≈ 0xC0
    out = decode_a5_10_room_panel(bytes([0x00, 0xC0, 0x80, 0x08]), 0x00, 0xA5)
    assert out["setpoint_offset_c"] == 5.0
    assert out["day"] is False  # DB0 bit 0 = 0


def test_a5_10_03_setpoint_offset_negative():
    # Drehrad max Absenkung: DB2 = 0x00 -> -10.04 → ≈ -10.0
    out = decode_a5_10_room_panel(bytes([0x00, 0x00, 0x80, 0x08]), 0x00, 0xA5)
    assert out["setpoint_offset_c"] == -10.0


def test_a5_10_thermokon_src_do8_reconstructed():
    # Referenz-Messung: Ist 19.8°C, Korrektur 1.0°C, Day=True (Slider Position O).
    # DB1: (255 - 19.8*255/40) = (255 - 126.225) ≈ 129 = 0x81
    # DB2: 128 + 1.0/10 * 127.5 = 140.75 ≈ 0x8D
    # DB0 bit 0 = 1 (Day)
    out = decode_a5_10_room_panel(bytes([0x00, 0x8D, 0x81, 0x09]), 0x00, 0xA5)
    assert out["temperature_c"] == 19.8
    assert out["setpoint_offset_c"] == 1.0
    assert out["day"] is True


def test_a5_13_01_day_night_bit_position():
    # M24/M33: day-Bit ist DB0 bit 2 (mask 0x04), INVERTIERT (0=day, 1=night)
    # per offizieller EnOcean-Spec A5-13-01.
    # Identifier=1 in DB0[7:4] = 0x10; LRN-Bit (bit 3) = 0x08.
    # Tag, kein Regen -> bit 2 = 0, bit 1 = 0 -> DB0 = 0x10 | 0x08 = 0x18
    out = decode_a5_13_weather(bytes([0xFF, 0x7B, 0x00, 0x18]), 0x00, 0xA5)
    assert out["ident"] == 1
    assert out["day"] is True
    assert out["rain"] is False


def test_a5_13_01_rain_bit():
    # Nacht + Regen: bit 2 = 1, bit 1 = 1 -> DB0 = 0x10 | 0x08 | 0x04 | 0x02 = 0x1E
    out = decode_a5_13_weather(bytes([0x10, 0x7B, 0x00, 0x1E]), 0x00, 0xA5)
    assert out["day"] is False
    assert out["rain"] is True


def test_a5_13_02_sun_max():
    # M24: sun_max_klx = max(ost, sued, west) als zusaetzliches Feld.
    # Ident=2 in DB0[7:4] = 0x20; LRN-Bit (bit 3) = 0x08 -> DB0 = 0x28
    # Bytes: DB3=West, DB2=Sued, DB1=Ost (alle 0..150 klx linear)
    # Test mit deutlichem Unterschied: Ost=20 klx (DB1=34=0x22),
    # Sued=80 klx (DB2=136=0x88), West=50 klx (DB3=85=0x55)
    out = decode_a5_13_weather(bytes([0x55, 0x88, 0x22, 0x28]), 0x00, 0xA5)
    assert out["ident"] == 2
    assert out["sun_east_klx"] == 20.0
    assert out["sun_south_klx"] == 80.0
    assert out["sun_west_klx"] == 50.0
    assert out["sun_max_klx"] == 80.0


def test_d5_00_contact_closed():
    # DB0: bit3=1 (data), bit0=1 (closed)
    out = decode_d5_00_contact(bytes([0x09]), 0x00, 0xD5)
    assert out["closed"] is True
    assert out["state"] == "closed"


def test_d5_00_contact_open():
    out = decode_d5_00_contact(bytes([0x08]), 0x00, 0xD5)
    assert out["closed"] is False


def test_eltako_fsb_command_down():
    out = decode_eltako_fsb_shutter(bytes([0x00, 0x32, 0x01, 0x09]), 0x00, 0xA5)
    assert out["command"] == "down"
    assert out["duration_s"] == 5.0  # 0*10 + 50 = 50ds = 5s


def test_full_registry_dispatch():
    reg = register_default_profiles()
    tel = RadioTelegram(
        rorg=0xF6,
        payload=bytes([0x30]),
        sender_id=0x01020304,
        status=0x30,
        sub_tel=0,
        destination_id=0xFFFFFFFF,
        rssi_dbm=-65,
        security_level=0,
    )
    decoded = reg.decode("F6-02-01", tel)
    assert decoded["_eep"] == "F6-02-01"
    assert decoded["pressed"] is True


def test_registry_fallback_to_raw():
    reg = register_default_profiles()
    tel = RadioTelegram(
        rorg=0xF6, payload=b"\x30", sender_id=1, status=0x30,
        sub_tel=None, destination_id=None, rssi_dbm=None, security_level=None,
    )
    decoded = reg.decode("UNKNOWN-99-99", tel)
    # Sollte Raw-Fallback nutzen
    assert "raw_hex" in decoded or "pressed" in decoded



# --- M79f: D2-01-XX Decoder (CMD low nibble) ---


def test_d2_01_actuator_status_response_user_payload():
    """Live-Beispiel vom User: OPUS BRiDGE 1 Kanal Status-Response 04 60 81."""
    out = decode_d2_01_actuator(bytes([0x04, 0x60, 0x81]), 0, 0xD2)
    assert out["cmd"] == 0x04   # CMD aus LOW nibble (war faelschlich 0)
    assert out["io_channel"] == 0
    assert out["on"] is True
    assert out["state"] == "ON"


def test_d2_01_actuator_off():
    """OV=0 → AUS."""
    out = decode_d2_01_actuator(bytes([0x04, 0x00, 0x00]), 0, 0xD2)
    assert out["cmd"] == 0x04
    assert out["on"] is False
    assert out["state"] == "OFF"
    assert "dim_percent" not in out


def test_d2_01_actuator_switch_on_full():
    """Switch: OV=100 → AN."""
    out = decode_d2_01_actuator(bytes([0x04, 0x00, 0x64]), 0, 0xD2)
    assert out["on"] is True
    assert out["state"] == "ON"
    assert "dim_percent" not in out   # Bit 7 = 0 → kein Dim


def test_d2_01_actuator_dim_response():
    """Dimmer: bit 7 = 1, OV=50 → 50% Dim aktiv."""
    out = decode_d2_01_actuator(bytes([0x04, 0x00, 0xB2]), 0, 0xD2)
    # 0xB2 = 1011 0010 → bit7=1 (Dim), OV=0x32=50
    assert out["on"] is True
    assert out["dim_percent"] == 50
    assert out["dim_active"] is True
