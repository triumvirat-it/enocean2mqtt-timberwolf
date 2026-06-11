"""
EEP-Profile-Decoder. Fokus: gaengige EnOcean-Profile im praktischen Einsatz.

Hilfreich: EnOcean Equipment Profiles (EEP) v2.6.7
Eltako-Schaltbefehle: Eltako-Funkbus-Doku (proprietäre RORG=0xA5-Codes)

Convention:
    decoder(payload: bytes, status: int, rorg: int) -> dict
    Rückgabe-dict ist JSON-fähig (nur primitive Typen).
"""
from __future__ import annotations

from .profile import EEPProfile, EEPProfileRegistry, FieldDef
from .registry import EEPRegistry


# ---------------------------------------------------------------------------
# F6 — RPS (Repeated Switch)
# ---------------------------------------------------------------------------


def decode_f6_02(payload: bytes, status: int, rorg: int) -> dict:
    """
    F6-02-01/02 Rocker Switch — 2 Wippen mit je 2 Tastern.
    Payload: 1 Byte.
        bit 7..5: R1 (rocker 1) action
        bit 4:    energy bow (1=gedrückt, 0=losgelassen)
        bit 3..1: R2 action (nur wenn second action)
        bit 0:    Second action flag
    Status NU-Bit (bit 4): 1=N-message (single), 0=U-message (release).

    Liefert zusaetzlich rocker_side ('A'/'B'/None), rocker_action
    ('press_top'/'press_bottom'/'release') und event (kombiniert,
    fuer is_topic_split — A_top/A_bottom/B_top/B_bottom/release).
    Damit kann ein 2-Channel-Wippentaster (FT55 etc.) seine Wippen
    pro Channel routen (Pipeline filtert nach rocker_side).
    """
    if not payload:
        return {"error": "empty payload"}
    b = payload[0]
    n_message = (status & 0x10) != 0  # 1 = button event, 0 = release/pressed-out
    energy_bow = (b & 0x10) != 0
    second_action = (b & 0x01) != 0

    # Rocker-Action-Mapping
    action_map = {0: "AI", 1: "A0", 2: "BI", 3: "B0"}
    r1 = action_map.get((b >> 5) & 0x07, f"R1={(b >> 5) & 0x07}")
    r2 = action_map.get((b >> 1) & 0x07, None) if second_action else None

    # Wippe + Aktion + Event aus dem r1-Code ableiten.
    # User-Konvention (am physischen FT55 abgelesen): am Aktor steht oben '0'
    # und unten '1' (in der enocean-lib-Notation 'I'). Also '0' -> oben,
    # 'I' -> unten. Das EnOcean-Action-Mapping aus action_map liefert die
    # Strings, hier wird nur die UX-Bedeutung zugeordnet.
    rocker_side = None
    rocker_action = "release"
    event = "release"
    if energy_bow and r1 and len(r1) == 2 and r1[0] in ("A", "B"):
        rocker_side = r1[0]
        if r1[1] == "0":
            rocker_action = "press_top"
            event = f"{rocker_side}_top"
        elif r1[1] == "I":
            rocker_action = "press_bottom"
            event = f"{rocker_side}_bottom"

    return {
        "rocker_1": r1 if energy_bow else None,
        "rocker_2": r2,
        "pressed": energy_bow,
        "type": "N" if n_message else "U",
        "raw": f"0x{b:02X}",
        "rocker_side": rocker_side,
        "rocker_action": rocker_action,
        "event": event,
    }


def decode_f6_05_smoke(payload: bytes, status: int, rorg: int) -> dict:
    """
    F6-05-02 Rauch-/Wassermelder (z.B. AFRISO ASD20, Eltako FAFM).

    DB0-Werte (typisch):
      0x10 = kein Alarm, Batterie OK
      0x30 = Alarm aktiv (Rauch/Wasser erkannt)
      0x70 = Batterie schwach (ohne Alarm)
      0x90 = Alarm + Batterie schwach
    """
    if not payload:
        return {"error": "empty"}
    b = payload[0]
    alarm = (b & 0x20) != 0
    battery_low = (b & 0x40) != 0
    return {
        "alarm": alarm,
        "state": "alarm" if alarm else "ok",
        "battery_low": battery_low,
        "battery_status": "low" if battery_low else "ok",
        "raw": f"0x{b:02X}",
    }


def decode_f6_10_window_handle(payload: bytes, status: int, rorg: int) -> dict:
    """
    F6-10-00 Fenstergriff (Hoppe/MecoTec).
    Werte: 0xF0=closed, 0xE0=open, 0xD0=tilted (typisch, exakte Werte je Hersteller)
    """
    if not payload:
        return {"error": "empty payload"}
    b = payload[0] & 0xF0
    state_map = {0xF0: "closed", 0xE0: "open", 0xD0: "tilted"}
    return {"state": state_map.get(b, f"unknown_0x{b:02X}"), "raw": f"0x{payload[0]:02X}"}


# ---------------------------------------------------------------------------
# A5 — 4BS (4 Byte Sensor)
# ---------------------------------------------------------------------------


def _is_teach_in(payload: bytes) -> bool:
    """4BS: bit 3 von DB0 (= payload[3]) = LRN-Bit. 0 = teach-in, 1 = data."""
    return len(payload) >= 4 and (payload[3] & 0x08) == 0


def decode_a5_02_temperature(payload: bytes, status: int, rorg: int) -> dict:
    """A5-02-XX Temperatursensoren (verschiedene Bereiche)."""
    if _is_teach_in(payload):
        return {"teach_in": True}
    if len(payload) < 4:
        return {"error": "short payload"}
    db1 = payload[1]
    # Default-Range A5-02-05: -10..+40°C (255=-10°C, 0=+40°C)
    temperature = round(40.0 - (db1 / 255.0) * 50.0, 1)
    return {"temperature_c": temperature, "raw_db1": db1}


def decode_a5_04_temp_humidity(payload: bytes, status: int, rorg: int) -> dict:
    """
    A5-04-01 Temperatur + Feuchte (Range 0..40°C / 0..100%).

    Per offizieller EnOcean-EEP-Spec (kipe/enocean EEP.xml):
      offset=8..15  (DB2 = payload[1]) = Rel. Humidity, 0..250 -> 0..100%
      offset=16..23 (DB1 = payload[2]) = Temperature,    0..250 -> 0..40°C

    HISTORISCHER BUG: bis 0.9.20 wurden Humidity/Temperature vertauscht
    gelesen (humidity aus payload[2], temp aus payload[1]). Bei symmetrischen
    Werten faellt das im Alltag nicht auf, weil die UI das Feld einfach
    "humidity_pct" nennt — der Zahlenwert ist aber dann der TEMPERATUR-Wert
    skaliert auf 0..100%, nicht die Feuchte.
    """
    if _is_teach_in(payload):
        return {"teach_in": True}
    if len(payload) < 4:
        return {"error": "short payload"}
    return {
        "humidity_pct":  round(payload[1] * 100.0 / 250.0, 1),
        "temperature_c": round(payload[2] * 40.0  / 250.0, 1),
    }


def decode_a5_07_motion(payload: bytes, status: int, rorg: int) -> dict:
    """A5-07-XX Bewegungsmelder (PIR)."""
    if _is_teach_in(payload):
        return {"teach_in": True}
    if len(payload) < 4:
        return {"error": "short payload"}
    db1 = payload[1]  # supply voltage 0..250 → 0..5V
    db0 = payload[3]
    pir_status = (db0 & 0x80) != 0  # bit 7
    return {
        "motion": pir_status,
        "voltage_v": round(db1 * 5.0 / 250.0, 2),
    }


def decode_a5_10_room_panel(payload: bytes, status: int, rorg: int) -> dict:
    """
    A5-10-XX Room Operating Panel (Raumbedienteil).

    Familie deckt verschiedene Bedienteile ab:
      A5-10-01 ... -03: Temperatur + Setpoint, ohne Slide-Switch
      A5-10-05        : Temperatur + Setpoint + Occupancy-Button
      A5-10-06        : Temperatur + Setpoint + Day/Night-Slide-Switch
                        (z.B. OPUS RTS55 SP+S, Thermokon SRC-DO8-Feedback)
      A5-10-10..12    : Temperatur + Feuchte + Setpoint (echte Humidity)

    Byte-Belegung per offizieller EnOcean-EEP-Spec (verifiziert gegen
    kipe/enocean EEP.xml fuer A5-10-03 und A5-10-06):
      offset=0..7   (DB3 = payload[0]) = NICHT BELEGT in dieser Familie
                    (Thermokon SRC-DO8 sendet hier z.B. immer 0x00)
      offset=8..15  (DB2 = payload[1]) = Set Point, linear 0..255 -> 0..100%
                    Die Anlage definiert die absolute Temperatur-Spanne
                    (typisch 15..25°C bei OPUS RTS55; raw=128 = Mitte)
      offset=16..23 (DB1 = payload[2]) = Temperatur INVERTIERT 255..0 -> 0..40°C
                    temp_c = (255 - byte) * 40 / 255
      offset=28     (DB0 bit 4 MSB-first) = LRN-Bit (0=teach-in, 1=data)
      offset=31     (DB0 bit 0 LSB)      = Slide-Switch: 1=Day/On, 0=Night/Off
                    (nur in A5-10-05/06 definiert; bei -03 ist der Bit-Wert
                    nicht spezifiziert, das Profile A5-10-03 published "day"
                    deshalb nicht als eigenen Channel)

    WICHTIG: KEINE Luftfeuchte! Frueher wurde fuer A5-10-06 faelschlicherweise
    der A5-04-Temp+Humidity-Decoder verwendet und der Setpoint als
    "humidity_pct" interpretiert. Das war ein Bug — RTS55 misst keine Feuchte.

    Korrekturhistorie:
      0.9.20: Bytes hatte ich aus Eile auf payload[0]/[1] gelegt (DB3=SP,
              DB2=TMP) — das ist falsch nach Spec. Korrekt sind payload[1]/[2].
    """
    if _is_teach_in(payload):
        return {"teach_in": True}
    if len(payload) < 4:
        return {"error": "short payload"}

    setpoint_raw = payload[1]   # DB2 per Spec (offset=8)
    temp_byte = payload[2]      # DB1 per Spec (offset=16) — invertiert
    temperature_c = round((255 - temp_byte) * 40.0 / 255.0, 1)

    # Setpoint-Interpretation per Eltako/Thermokon-Heizungs-
    # Konvention: DB2 ist ein SIGNED OFFSET zur Basistemperatur, nicht ein
    # absoluter Sollwert.
    #   DB2 = 0x00 -> -10°C  (max Absenkung)
    #   DB2 = 0x80 ->   0°C  (neutral, Drehrad in Mitte)
    #   DB2 = 0xFF -> +10°C  (max Anhebung)
    # Default-Range ist ±10°C. Anlagen koennen das ueber channel.meta
    # ueberschreiben (Feld setpoint_offset_range_c) — siehe products.yaml
    # config-String "SetpointOffsetMin/Max". Timberwolf-Logik berechnet
    # absoluten Sollwert selbst (= BaseTemp + offset_c).
    setpoint_offset_c = round((setpoint_raw - 128) / 127.5 * 10.0, 1)

    return {
        "temperature_c":     temperature_c,
        "setpoint_offset_c": setpoint_offset_c,
        # Diagnose-Felder (kein eigenes Topic — nur in Live-Log/Debug sichtbar):
        "setpoint_raw":      setpoint_raw,
        # Slide-Switch (nur A5-10-05/06): bit 0 (LSB) von DB0.
        "day": (payload[3] & 0x01) != 0,
    }


def _a5_10_fan_stage(db3: int) -> str:
    """
    Luefterstufe aus DB3. Schwellen exakt laut Thermokon-thanos-Handbuch
    (Softwarebeschreibung Thanos EnOcean, Kap. 5.1, Telegrammtyp A5-10-02):
        n > 210        -> Automatik
        190 < n <= 210 -> Stufe 0 (aus)
        165 < n <= 190 -> Stufe 1
        145 < n <= 165 -> Stufe 2
        n <= 145       -> Stufe 3
    fan_raw wird zusaetzlich mitgeliefert, falls am realen Geraet doch
    abweichend justiert wurde.
    """
    if db3 > 210:
        return "auto"
    if db3 > 190:
        return "0"
    if db3 > 165:
        return "1"
    if db3 > 145:
        return "2"
    return "3"


def decode_a5_10_02(payload: bytes, status: int, rorg: int) -> dict:
    """
    A5-10-02 Raumbedienteil mit Luefterstufe + Raumbelegung.
    Geraete: Thermokon thanos (L/LQ SR, eltako-baugleich). Das thanos sendet
    Ist-Temperatur, Sollwertverstellung, Luefterstufe und Raumbelegung in
    diesem Profil (Handbuch Kap. 5, Profil A5-10-02).

    Byte-Belegung laut Handbuch:
      DB3 (payload[0]) = Luefterstufe              -> fan_stage + fan_raw
      DB2 (payload[1]) = Sollwertverstellung       -> setpoint_offset_c
      DB1 (payload[2]) = Temperatur 0..40C=255..0  -> temperature_c
      DB0 bit3         = Lerntaste (0=teach-in)
      DB0 bit0         = Raumbelegung (unbelegt=0 / belegt=1) -> occupancy

    Temperatur + Sollwert kommen aus dem gemeinsamen A5-10-Decoder.
    """
    base = decode_a5_10_room_panel(payload, status, rorg)
    if base.get("teach_in") or base.get("error"):
        return base
    db3 = payload[0]
    db0 = payload[3]
    base["fan_stage"] = _a5_10_fan_stage(db3)
    base["fan_raw"] = db3
    # Handbuch: DB0 bit0 = Raumbelegung, belegt=1 -> anwesend.
    base["occupancy"] = (db0 & 0x01) != 0
    base["occupancy_raw"] = db0 & 0x01
    # 'day' aus der Basis ist dieselbe DB0-bit0-Info — bei -02 nennen wir es
    # eindeutig Raumbelegung (occupancy); 'day' entfaellt.
    base.pop("day", None)
    return base


def _decode_a5_12_common(payload: bytes) -> dict | None:
    """
    Gemeinsame Bit-Logik fuer A5-12-01/02/03 (Strom/Gas/Wasser).
    Liefert ein dict mit {data_raw, tariff, is_flow} oder spezialfall-keys
    {teach_in: True, detected_eep: "A5-12-02"} /
    {info: "serial_number_part", ...} / {info: "unknown_db0_..."}.
    None nur bei zu kurzem Payload.
    """
    if _is_teach_in(payload):
        # Eltako-Lerntelegramme A5-12-XX:
        #   DB3=0x48, DB2=0x08 -> A5-12-01 (Strom)
        #   DB3=0x48, DB2=0x10 -> A5-12-02 (Gas)
        #   DB3=0x48, DB2=0x18 -> A5-12-03 (Wasser)
        # Sub-TYPE im DB2-Nibble (bits 3+4) -> 1/2/3
        if len(payload) >= 4 and payload[0] == 0x48:
            type_code = (payload[1] >> 3) & 0x03
            if type_code in (1, 2, 3):
                return {"teach_in": True, "detected_eep": f"A5-12-{type_code:02d}"}
        return {"teach_in": True}
    if len(payload) < 4:
        return None
    db0 = payload[3]
    # Eltako-Seriennummer-Telegramm (DSZ14(W)DRS — A5-12-01) — kommt auch bei
    # anderen Eltako-Zaehlern vor. BCD-Teile dekodieren.
    if db0 == 0x8F:
        def _bcd(b: int) -> str:
            return f"{(b >> 4) & 0xF}{b & 0xF}"
        db1 = payload[2]
        if db1 == 0x00:
            return {"serial_high": _bcd(payload[0])}
        if db1 == 0x01:
            return {"serial_low": _bcd(payload[1]) + _bcd(payload[0])}
        return {"info": "serial_unknown_part", "db1": db1}
    if (db0 & 0x08) == 0:
        return {"teach_in": True}
    if db0 not in (0x09, 0x0C, 0x19, 0x1C):
        return {"info": f"unknown_db0_0x{db0:02X}"}
    return {
        "data_raw": (payload[0] << 16) | (payload[1] << 8) | payload[2],
        "tariff": 1 if (db0 & 0x10) else 0,
        "is_flow": (db0 & 0x04) != 0,
    }


def decode_a5_12_meter(payload: bytes, status: int, rorg: int) -> dict:
    """
    A5-12-01 Stromzaehler (Eltako FWZ14, DSZ14DRS, F3Z14D) per offizieller
    Eltako-Doku "Inhalte der Eltako-Funktelegramme":

      ORG = 0x07
      Data_byte3..1 = 24-Bit binaer codierter Wert
      Data_byte0:
        Bit 4    = Tarif (0 = Normaltarif/HT, 1 = Nachttarif/NT)
        Bit 3    = LRN-Bit (0 = Lerntelegramm, 1 = Datentelegramm)
        Bit 2    = Einheit:
                     0 = Zaehlerstand in 0,1 kWh
                     1 = Augenblicksleistung in W
        Bit 1..0 = fest 01

      Moegliche DB0-Werte:
        0x09 -> kWh Normaltarif
        0x19 -> kWh Nachttarif
        0x0C -> W   Normaltarif
        0x1C -> W   Nachttarif

    Liefert energy_kwh ODER current_w (nie beide gleichzeitig), plus
    tariff = 0 (Normaltarif) oder 1 (Nachttarif). Pipeline filtert pro
    Channel auf tariff (via meta.tele_channel) und field (energy_kwh /
    current_w).
    """
    parsed = _decode_a5_12_common(payload)
    if parsed is None:
        return {"error": "short payload"}
    if "data_raw" not in parsed:
        return parsed  # teach_in / serial_high/low / info
    # A5-12-01 Strom:
    #   DB0 = 0x09 / 0x19 -> Zaehlerstand in 0,1 kWh (energy_kwh)
    #   DB0 = 0x0C / 0x1C -> Augenblicksleistung in W (current_w)
    tariff = parsed["tariff"]
    if parsed["is_flow"]:
        return {"tariff": tariff, "current_w": float(parsed["data_raw"])}
    return {"tariff": tariff, "energy_kwh": round(parsed["data_raw"] * 0.1, 2)}


def decode_a5_12_gas(payload: bytes, status: int, rorg: int) -> dict:
    """
    A5-12-02 Gas-Zaehler (Eltako, gleiche Bit-Logik wie A5-12-01).
    Annahme: Zaehlerstand in 0,1 m^3 (Eltako-Konvention).
    """
    parsed = _decode_a5_12_common(payload)
    if parsed is None:
        return {"error": "short payload"}
    if "data_raw" not in parsed:
        return parsed
    tariff = parsed["tariff"]
    if parsed["is_flow"]:
        # Eltako-Gas-Telegramme mit "Flow" sind unueblich; falls doch, geben
        # wir den Rohwert in m^3/h aus (Annahme: 1er-Skala).
        return {"tariff": tariff, "flow_m3h": float(parsed["data_raw"])}
    return {"tariff": tariff, "volume_m3": round(parsed["data_raw"] * 0.1, 2)}


def decode_a5_12_water(payload: bytes, status: int, rorg: int) -> dict:
    """
    A5-12-03 Wasser-Zaehler (Eltako, gleiche Bit-Logik wie A5-12-01).
    Annahme: Zaehlerstand in 0,1 m^3 (= 100 Liter pro Schritt).
    """
    parsed = _decode_a5_12_common(payload)
    if parsed is None:
        return {"error": "short payload"}
    if "data_raw" not in parsed:
        return parsed
    tariff = parsed["tariff"]
    if parsed["is_flow"]:
        # Annahme: Liter/Minute fuer Wasser-Flow
        return {"tariff": tariff, "flow_l_min": float(parsed["data_raw"])}
    return {"tariff": tariff, "volume_m3": round(parsed["data_raw"] * 0.1, 2)}


def decode_a5_13_weather(payload: bytes, status: int, rorg: int) -> dict:
    """
    A5-13-01/02 Wettersensor (z.B. Eltako FWS61, MS Wetterstation).

    Sendet alternierend zwei Telegramm-Typen:
      Identifier 1: Daemmerung (lux), Aussentemperatur, Wind, Tag/Nacht, Regen
      Identifier 2: Sonneneinstrahlung Ost, Sued, West (jeweils klx)
    """
    if _is_teach_in(payload):
        return {"teach_in": True}
    if len(payload) < 4:
        return {"error": "short payload"}
    identifier = (payload[3] >> 4) & 0x0F

    if identifier == 0x01:
        # DB3 = Daemmerung 0-999 lux (linear normiert)
        dawn_lux = round(payload[0] * 999.0 / 255.0)
        # DB2 = Temperatur -40..+80°C, 0=-40, 255=+80
        outdoor_temp = round((payload[1] * 120.0 / 255.0) - 40.0, 1)
        # DB1 = Windgeschwindigkeit 0-70 m/s
        wind_speed = round(payload[2] * 70.0 / 255.0, 1)
        # DB0 nach offizieller EnOcean-EEP-Spec A5-13-01 (verifiziert gegen
        # kipe/enocean EEP.xml):
        #   bit 2 (mask 0x04) = D/N: 0 = day, 1 = night  -> INVERTIERT zur
        #                       intuitiven Lesart
        #   bit 1 (mask 0x02) = Rain: 0 = trocken, 1 = Regen
        #   bit 0 (mask 0x01) = reserviert
        # Eltako FWS61 folgt der Spec, daher day = NOT bit 2.
        day = (payload[3] & 0x04) == 0
        rain = (payload[3] & 0x02) != 0
        return {
            "ident": 1,
            "dawn_lux": dawn_lux,
            "outdoor_temp_c": outdoor_temp,
            "wind_speed_ms": wind_speed,
            "day": day,
            "rain": rain,
        }

    if identifier == 0x02:
        # DB3 = Sonne West in klx (0-150 klx, linear)
        # DB2 = Sonne Sued in klx
        # DB1 = Sonne Ost in klx
        sun_west = round(payload[0] * 150.0 / 255.0, 1)
        sun_south = round(payload[1] * 150.0 / 255.0, 1)
        sun_east = round(payload[2] * 150.0 / 255.0, 1)
        # Zusatzfeld: hoechster der 3 Richtungen — nuetzlich fuer
        # Markisen-/Beschattungs-Logik in Timberwolf (Trigger ueber Schwelle).
        sun_max = max(sun_west, sun_south, sun_east)
        return {
            "ident": 2,
            "sun_west_klx": sun_west,
            "sun_south_klx": sun_south,
            "sun_east_klx": sun_east,
            "sun_max_klx": sun_max,
        }

    return {"raw": payload.hex(), "ident": identifier}


def decode_a5_38_08_dimmer(payload: bytes, status: int, rorg: int) -> dict:
    """
    A5-38-08 Central Command Gateway — Switching (DB3=0x01) ODER Dimming
    (DB3=0x02). Dekodiert sowohl Befehle die wir gesendet haben (Echo)
    als auch Bestaetigungs-Telegramme von Eltako-Aktoren (FUD/FLD/FSR/FDG).

    Eltako-Aktoren senden zusaetzlich zum A5-Feedback noch eine kurze RPS-
    Quittung (1 Byte) mit DB=0x70 (ON) oder 0x50 (OFF).

    Eltako-Doku "Inhalte der Eltako-Funktelegramme", Kapitel FLD61/FUD/FDG:
        DB3 = 0x02 (Command: Dimming)
        DB2 = Dimmwert 0..100 (dezimal)
        DB1 = Dimmgeschwindigkeit
                0x00 = intern eingestellt am Dimmer
                0x01 = sehr schnell
                0xFF = sehr langsam
        DB0 bit3 (0x08) = LRN: 1=Daten, 0=Lerntelegramm
        DB0 bit2 (0x04) = Dimmwert blockieren: 1=blockiert, 0=normal
        DB0 bit0 (0x01) = An/Aus

    Verifizierte Beispiele aus Eltako-Doku:
        02 32 00 09 -> 50% an, Speed=intern
        02 64 01 09 -> 100% an, Speed=schnellster
        02 14 FF 09 -> 20% an, Speed=langsamster
        02 .. .. 08 -> Aus
    """
    # Eltako-RPS-Quittung (1-Byte-Payload, RORG=F6)
    if rorg == 0xF6 and len(payload) == 1:
        b = payload[0]
        if b == 0x70:
            return {"on": True, "state": "ON", "feedback": True, "raw": f"0x{b:02X}"}
        if b == 0x50:
            return {"on": False, "state": "OFF", "feedback": True, "raw": f"0x{b:02X}"}
        return {"feedback": True, "raw": f"0x{b:02X}", "_note": "unknown RPS ack"}

    if _is_teach_in(payload):
        return {"teach_in": True}
    if len(payload) < 4:
        return {"error": "short payload"}
    command = payload[0]  # 0x01=switch (FSR), 0x02=dim (FLD/FUD/FDG)
    dim_value = payload[1]  # 0..100
    db1 = payload[2]
    db0 = payload[3]

    # An/Aus: bit3 (LRN=data=1) UND bit0 (state=1)
    is_on = (db0 & 0x08) == 0x08 and (db0 & 0x01) == 0x01
    result: dict = {
        "command": command,
        "dim_percent": dim_value,
        "ramp_s": db1,   # historischer Feldname — bei Eltako-Dimming ist das
                         # eigentlich ein Speed-Code (0..0xFF), kein s-Wert
        "on": is_on,
        "state": "ON" if is_on else "OFF",
        # Eltako-spezifische Felder:
        "blocked": (db0 & 0x04) == 0x04,
        # Telegramme die direkt vom Aktor kommen (statt von uns gesendet) sind
        # implizit Feedback — wir markieren das damit der tx_router den State
        # daraus updated, auch wenn explizites feedback=True fehlt.
        "feedback": True,
    }
    if command == 0x02:
        # Dimming-Befehl: DB1 als Eltako-Speed-Code mit zusaetzlich lesbarer Form
        if db1 == 0x00:
            result["dim_speed_label"] = "intern"
        elif db1 == 0x01:
            result["dim_speed_label"] = "sehr schnell"
        elif db1 == 0xFF:
            result["dim_speed_label"] = "sehr langsam"
        else:
            result["dim_speed_label"] = f"Code {db1}"
    return result


def decode_a5_3f_7f_universal(payload: bytes, status: int, rorg: int) -> dict:
    """
    A5-3F-7F Universal 4BS — bei Eltako fast immer FSB-Rolladen.

    Eltako-FSB-Konvention im Status-Telegramm (laut offizieller Eltako-Doku
    "Inhalte der Eltako-Funktelegramme", FSB61NP-230V Abschnitt):

    A5/BS4 Status-Telegramm (kommt beim Stopp VOR Endlage):
        DB3 = Fahrzeit MSB (in 100ms)
        DB2 = Fahrzeit LSB (in 100ms)
        DB1 = Status-Code:
              0x00 = STOP
              0x01 = Aufgefahren (war auf-Bewegung, jetzt gestoppt)
              0x02 = Abgefahren (war ab-Bewegung, jetzt gestoppt)
              0x50 = Endlage UNTEN erreicht
              0x70 = Endlage OBEN erreicht
        DB0 = 0x0A (nicht blockiert) oder 0x0E (blockiert)

    Zusaetzlich sendet der Aktor bei Start ein RPS-1-Byte-Telegramm:
        0x01 = Start auf
        0x02 = Start ab
    Wir markieren das als Press-Notify ohne State-Update.
    """
    # Eltako-RPS Start-Quittung (1 Byte, RORG=F6) — laut Eltako-Doku:
    # 0x01 = Start auf, 0x02 = Start ab
    if rorg == 0xF6 and len(payload) == 1:
        b = payload[0]
        notify_map = {0x01: "press_up", 0x02: "press_down"}
        return {
            "press": notify_map.get(b),
            "raw": f"0x{b:02X}",
            "_note": "Eltako-FSB Start-Quittung",
        }
    if _is_teach_in(payload):
        return {"teach_in": True}
    if len(payload) < 4:
        return {"error": "short payload"}

    db3, db2, db1, db0 = payload[0], payload[1], payload[2], payload[3]
    # Eltako-Konvention (offizielle Doku):
    # DB1=0x01=Aufgefahren (up), 0x02=Abgefahren (down)
    # DB1=0x70=Endlage oben, 0x50=Endlage unten
    state_map = {
        0x00: "stop",
        0x01: "moving_up",
        0x02: "moving_down",
        0x50: "end_down",
        0x70: "end_up",
    }
    state = state_map.get(db1)
    if state is None:
        # Unbekannter Status — Roh-Werte zurueck, kein feedback-Flag
        return {"db3": db3, "db2": db2, "db1": db1, "db0": db0,
                "raw_status": f"0x{db1:02X}"}

    # Laufzeit in Sekunden (Eltako: DB3 = Sekunden-Hi, DB2 = Dezisekunden-Lo)
    duration_ds = (db3 * 10) + db2
    return {
        "feedback": True,
        "state": state,
        "duration_ds": duration_ds,
        "duration_s": duration_ds / 10.0,
        "moving": state in ("moving_up", "moving_down"),
        "at_end_position": state in ("end_up", "end_down"),
    }


# ---------------------------------------------------------------------------
# Eltako-spezifische 4BS Schalt-/Beschattungs-Befehle (RORG 0xA5)
# Quelle: Eltako-Funkbus Doku — FSB für Rolläden, FSR für Schaltbefehle
# ---------------------------------------------------------------------------


def decode_eltako_fsb_shutter(payload: bytes, status: int, rorg: int) -> dict:
    """
    Eltako FSB61NP Rolladenaktor — Telegramm-Format:
    DB3 = Laufzeit-Tens, DB2 = Laufzeit-Einer, DB1 = Befehl (0=stop, 1=ab, 2=auf),
    DB0 = Flags (0x08 = data, 0x09 = data+confirm).
    """
    if len(payload) < 4:
        return {"error": "short payload"}
    cmd_map = {0: "stop", 1: "down", 2: "up"}
    cmd = cmd_map.get(payload[2], f"unknown_{payload[2]}")
    duration = payload[0] * 10 + payload[1]  # in 1/10 Sekunden (laut Eltako-Doku)
    return {
        "command": cmd,
        "duration_ds": duration,  # Dezisekunden
        "duration_s": duration / 10.0,
        "raw_db0": f"0x{payload[3]:02X}",
    }


def decode_eltako_fsr_switch(payload: bytes, status: int, rorg: int) -> dict:
    """
    Eltako FSR Schalt-Telegramm (Eltako-spezifisch, RORG=0xA5):
    DB0=0x09 + DB3=0x70: ON
    DB0=0x09 + DB3=0x50: OFF
    """
    if len(payload) < 4:
        return {"error": "short payload"}
    db3 = payload[0]
    db0 = payload[3]
    state = None
    if db0 == 0x09:
        state = "ON" if db3 == 0x70 else "OFF" if db3 == 0x50 else None
    return {
        "state": state,
        "raw_db3": f"0x{db3:02X}",
        "raw_db0": f"0x{db0:02X}",
    }


# ---------------------------------------------------------------------------
# Eltako-Feedback-Telegramme — vom Aktor an Steuerung zurueck
# Wichtig fuer Position-Tracking ohne Eichfahrt im Aktor selbst.
# ---------------------------------------------------------------------------


def decode_eltako_feedback_fsr(payload: bytes, status: int, rorg: int) -> dict:
    """
    Eltako FSR/FSSA Schalt-Aktor sendet als Feedback nach Schalten:
        DB3 = 0x70 (ON) oder 0x50 (OFF)
        DB2 = 0x00
        DB1 = 0x00
        DB0 = 0x30 (Status-Marker, NU=1, T21=1, data)
    """
    if len(payload) < 4:
        return {"error": "short payload"}
    db3 = payload[0]
    db0 = payload[3]
    state = None
    if db3 == 0x70:
        state = "ON"
    elif db3 == 0x50:
        state = "OFF"
    return {
        "feedback": True,
        "state": state,
        "on": (state == "ON"),
        "raw_db3": f"0x{db3:02X}",
        "raw_db0": f"0x{db0:02X}",
    }


def decode_eltako_feedback_fud(payload: bytes, status: int, rorg: int) -> dict:
    """
    Eltako FUD/FLD Dimmer sendet als Feedback nach Dim-Befehl:
        DB3 = 0x02 (Dim command)
        DB2 = Dim-Wert 0..100 (Prozent)
        DB1 = Ramp-Zeit
        DB0 = bit 0 = on/off, bit 3 = 1 (data), bit 2 = absolute dim mode
    """
    if len(payload) < 4:
        return {"error": "short payload"}
    db2 = payload[1]
    db0 = payload[3]
    dim = max(0, min(100, db2))
    on = (db0 & 0x01) != 0
    return {
        "feedback": True,
        "dim_percent": dim,
        "on": on,
        "state": "ON" if on else "OFF",
        "ramp_s": payload[2],
    }


def decode_eltako_feedback_fsb(payload: bytes, status: int, rorg: int) -> dict:
    """
    Eltako FSB Rolladen-Aktor sendet Status-Telegramme:
        DB3 = Laufzeit-Hi (in 1/10s) bei laufender Fahrt
        DB2 = Laufzeit-Lo (in 1/10s)
        DB1 = Status-Code:
                0x01 = laeuft AB (down)
                0x02 = laeuft AUF (up)
                0x70 = Endlage OBEN erreicht
                0x50 = Endlage UNTEN erreicht
                0x00 = STOP
        DB0 = bit 3 = 1 (data)
    """
    if len(payload) < 4:
        return {"error": "short payload"}
    db3 = payload[0]
    db2 = payload[1]
    db1 = payload[2]
    db0 = payload[3]

    state_map = {
        0x00: "stop",
        0x01: "moving_down",
        0x02: "moving_up",
        0x50: "end_down",   # ist UNTEN angekommen → Position = 0%
        0x70: "end_up",     # ist OBEN angekommen → Position = 100%
    }
    state = state_map.get(db1, f"unknown_{db1:02X}")
    duration_ds = (db3 * 10) + db2

    return {
        "feedback": True,
        "state": state,
        "duration_ds": duration_ds,
        "duration_s": duration_ds / 10.0,
        "at_end_position": state in ("end_up", "end_down"),
        "moving": state in ("moving_up", "moving_down"),
        "raw_db1": f"0x{db1:02X}",
    }


# ---------------------------------------------------------------------------
# D5 — 1BS (1 Byte Sensor)
# ---------------------------------------------------------------------------


def decode_d5_00_contact(payload: bytes, status: int, rorg: int) -> dict:
    """D5-00-01 Magnetkontakt (Tür/Fenster). bit 0: 1=closed, 0=open."""
    if not payload:
        return {"error": "empty"}
    b = payload[0]
    teach_in = (b & 0x08) == 0
    if teach_in:
        return {"teach_in": True}
    closed = (b & 0x01) != 0
    return {"closed": closed, "state": "closed" if closed else "open"}


# ---------------------------------------------------------------------------
# D2 — VLD (Variable Length Data)
# ---------------------------------------------------------------------------


def decode_d2_01_actuator(payload: bytes, status: int, rorg: int) -> dict:
    """
    D2-01-XX Electronic switches and dimmers (M79f).

    Telegramm-Layout per EnOcean EEP 2.6.5 Spec:
      Byte 0[7..4] = Reserved
      Byte 0[3..0] = CMD (low nibble!)
        0x01 = Actuator Set Output
        0x03 = Actuator Status Query
        0x04 = Actuator Status Response   ← Aktor → Zentrale
      Byte 1[7..5] = PF (Power-Fail status / Time)
      Byte 1[4..0] = IO Channel (0..29 = Kanal, 30=ALL, 31=Input)
      Byte 2[7]    = DimMode-Flag (1 = Dimmer-Variante, 0 = Schalter)
      Byte 2[6..0] = OV (Output Value)
                     D2-01-01 (Switch): 0 = AUS, sonst AN
                     D2-01-09/0C/0D (Dimmer): 0..100 = Dim-%
    """
    if len(payload) < 1:
        return {"error": "empty"}
    cmd = payload[0] & 0x0F   # M79f: CMD in LOW nibble (war faelschlich high)
    out = {"cmd": cmd, "feedback": True}
    if cmd == 0x04 and len(payload) >= 3:
        out["io_channel"] = payload[1] & 0x1F
        out["pf_status"] = (payload[1] >> 5) & 0x07
        ov = payload[2] & 0x7F
        out["on"] = ov > 0
        out["state"] = "ON" if out["on"] else "OFF"
        # dim_percent NUR wenn Dim-Mode-Bit gesetzt — sonst ist OV nur das
        # ON/OFF-Indikator (Switch-Variante kennt keinen Zwischenwert).
        if payload[2] & 0x80:
            out["dim_percent"] = ov
            out["dim_active"] = True
    return out


def decode_d2_05_blind(payload: bytes, status: int, rorg: int) -> dict:
    """
    D2-05-XX Blind Position Control (Rolladen / Jalousie). M79f.

    Byte 0[3..0] = CMD (low nibble).
    CMD=0x04 (Position-Reply): byte1=position (0=offen, 100=zu),
                               byte2=angle (Lamelle), byte3=channel+lock.
    """
    if len(payload) < 1:
        return {"error": "empty"}
    cmd = payload[0] & 0x0F   # M79f: low nibble
    out = {"cmd": cmd, "feedback": True}
    if cmd == 0x04 and len(payload) >= 4:
        pos = payload[1]
        if pos <= 100:
            out["position_percent"] = pos
            out["on"] = pos > 0
            out["angle"] = payload[2]
            out["io_channel"] = payload[3] & 0x0F
    return out


# ---------------------------------------------------------------------------
# Raw-Fallbacks pro RORG
# ---------------------------------------------------------------------------


def raw_decode(payload: bytes, status: int, rorg: int) -> dict:
    return {
        "raw_hex": payload.hex(),
        "rorg": f"0x{rorg:02X}",
        "status": f"0x{status:02X}",
        "_eep": "UNKNOWN",
    }


# ---------------------------------------------------------------------------
# EEPProfile-Definitionen
# Jeder Profile-Block sammelt alles was die App ueber den EEP wissen muss:
# Decoder, Encoder, UI-Kind, Field-Defs (mit Einheit), Multi-Channel-Split-Felder.
# ---------------------------------------------------------------------------

# Helper: Encoder-Wrapper, der command-dict in encoder-spezifische Args umsetzt.
# Die eigentlichen Encoder leben in app/encoders.py — wir importieren lazy
# um Zyklen zu vermeiden.


def _make_encoder_dispatch(encoder_kind: str):
    """
    Liefert einen Closure, der (sender_id, command_dict) -> CommandFrame mappt.
    encoder_kind: "f6_02", "eltako_fsr", "eltako_fsb", "a5_38_08_switch",
                  "a5_38_08_dim".
    """
    def dispatch(sender_id: int, command: dict, **kw):
        from .. import encoders as enc

        cmd = command.get("command", command.get("state"))
        if encoder_kind == "f6_02":
            rocker = command.get("rocker", "AI")
            pressed = bool(command.get("pressed", True))
            if "state" in command and "rocker" not in command:
                v = command["state"]
                pressed = bool(v) or v in ("on", "ON", "true", True)
            return enc.encode_f6_02_button(sender_id, rocker, pressed=pressed)
        if encoder_kind == "eltako_fsr":
            state = _parse_bool(command.get("state", False))
            return enc.encode_eltako_switch(sender_id, state)
        if encoder_kind == "eltako_fsb":
            c = cmd if isinstance(cmd, str) else "stop"
            duration = float(command.get("duration_s", 0.0))
            return enc.encode_eltako_shutter(sender_id, c, duration_s=duration)
        if encoder_kind == "a5_38_08_switch":
            state = _parse_bool(command.get("state", False))
            return enc.encode_a5_38_08_switch(sender_id, state)
        if encoder_kind == "a5_38_08_dim":
            if "dim" in command:
                dim = int(command["dim"])
                ramp = int(command.get("ramp", 0))
                on = _parse_bool(command.get("state", dim > 0))
                return enc.encode_a5_38_08_dim(sender_id, dim, ramp_s=ramp, on=on)
            state = _parse_bool(command.get("state", False))
            return enc.encode_a5_38_08_switch(sender_id, state)
        # A5-10-06 Raumbedienteil-TX an Heizregler (Thermokon STC-DO8):
        # Ist-Temp + Sollwertverschiebung + Tag/Nacht.
        if encoder_kind == "a5_10_06":
            temp = float(command.get("temperature",
                                     command.get("temperature_c", 20.0)))
            off = float(command.get("setpoint_offset",
                                    command.get("setpoint_offset_c", 0.0)))
            omin = float(command.get("offset_min", -10.0))
            omax = float(command.get("offset_max", 10.0))
            # Tag/Nacht: "day" bevorzugt; alternativ "night" (invertiert).
            if "night" in command:
                day = not _parse_bool(command.get("night"))
            else:
                day = _parse_bool(command.get("day", True))
            setback = float(command.get("night_setback", 0.0))
            return enc.encode_a5_10_setpoint(
                sender_id, temperature_c=temp, setpoint_offset_c=off,
                offset_min_c=omin, offset_max_c=omax, day=day,
                night_setback_c=setback,
            )
        # M82: D2-01-XX VLD-Aktor (OPUS BRiDGE etc.) — Set Output
        if encoder_kind == "d2_01_set_output":
            io_channel = int(command.get("io_channel", 0))
            if "dim" in command:
                ov = max(0, min(100, int(command["dim"])))
            else:
                state = _parse_bool(command.get("state", False))
                # Switch-Variante: 100 = AN, 0 = AUS
                ov = 100 if state else 0
            return enc.encode_d2_01_set_output(sender_id, ov, io_channel=io_channel)
        return None

    return dispatch


def _parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("on", "true", "1", "an", "ein", "open", "up")
    return False


# Lookup-Profile, gefuellt durch register_all()
PROFILES: list[EEPProfile] = [
    # F6 RPS — Wippentaster + Sondersensoren
    EEPProfile(
        eep_id="F6-02-01",
        name="Wippentaster (PTM)",
        description="Standard EnOcean-Funktaster, 2 Wippen",
        rorg=0xF6, decoder=decode_f6_02,
        encoder=_make_encoder_dispatch("f6_02"),
        ui_kind="rx",
        command_keys=["rocker", "pressed", "state"],
        # rocker_side erlaubt 2-Channel-Routing (FT55-Wippentaster). Bei
        # Devices mit nur 1 Channel und ohne meta.tele_channel sieht die
        # Pipeline alle Telegramme — die Default-0-Annahme greift nur fuer
        # numerische tc_fields (s. pipeline.py).
        telegram_channel_field="rocker_side",
        fields=[
            FieldDef("event",       "Aktion",     kind="enum", is_topic_split=True,
                     enum_labels={
                         "A_top":    "↑ A oben gedrueckt",
                         "A_bottom": "↓ A unten gedrueckt",
                         "B_top":    "↑ B oben gedrueckt",
                         "B_bottom": "↓ B unten gedrueckt",
                         "release":  "losgelassen",
                     }),
            FieldDef("pressed",     "Gedrueckt",  kind="bool", is_topic_split=False,
                     enum_labels={True: "gedrueckt", False: "losgelassen"}),
            FieldDef("rocker_side", "Wippe",      kind="string", is_topic_split=False),
            FieldDef("rocker_1",    "Wippe-Code", kind="string", is_topic_split=False),
            FieldDef("rocker_2",    "Wippe 2",    kind="string", is_topic_split=False),
            # press_duration_ms wird im Release-Telegramm von pipeline.py
            # ergaenzt — Zeit zwischen Druck und Loslassen. is_topic_split
            # fuer MQTT-Sub-Topic, aber creates_subchannel=False damit der
            # CSV-Importer NICHT pro Feld einen eigenen UI-Channel anlegt.
            FieldDef("press_duration_ms", "Druckdauer", unit="ms",
                     kind="number", is_topic_split=True,
                     creates_subchannel=False),
            FieldDef("last_press_event", "Zuletzt gedrueckt", kind="enum",
                     is_topic_split=True, creates_subchannel=False,
                     enum_labels={
                         "A_top":    "↑ A oben gedrueckt",
                         "A_bottom": "↓ A unten gedrueckt",
                         "B_top":    "↑ B oben gedrueckt",
                         "B_bottom": "↓ B unten gedrueckt",
                     }),
        ],
    ),
    EEPProfile(
        eep_id="F6-02-02",
        name="Wippentaster (PTM)",
        rorg=0xF6, decoder=decode_f6_02,
        encoder=_make_encoder_dispatch("f6_02"),
        ui_kind="rx",
        telegram_channel_field="rocker_side",
        fields=[
            FieldDef("event",       "Aktion",     kind="enum", is_topic_split=True,
                     enum_labels={
                         "A_top":    "↑ A oben gedrueckt",
                         "A_bottom": "↓ A unten gedrueckt",
                         "B_top":    "↑ B oben gedrueckt",
                         "B_bottom": "↓ B unten gedrueckt",
                         "release":  "losgelassen",
                     }),
            FieldDef("pressed",     "Gedrueckt",  kind="bool", is_topic_split=False,
                     enum_labels={True: "gedrueckt", False: "losgelassen"}),
            FieldDef("rocker_side", "Wippe",      kind="string", is_topic_split=False),
            FieldDef("rocker_1",    "Wippe-Code", kind="string", is_topic_split=False),
            FieldDef("rocker_2",    "Wippe 2",    kind="string", is_topic_split=False),
            # press_duration_ms wird im Release-Telegramm von pipeline.py
            # ergaenzt — Zeit zwischen Druck und Loslassen. is_topic_split
            # fuer MQTT-Sub-Topic, aber creates_subchannel=False damit der
            # CSV-Importer NICHT pro Feld einen eigenen UI-Channel anlegt.
            FieldDef("press_duration_ms", "Druckdauer", unit="ms",
                     kind="number", is_topic_split=True,
                     creates_subchannel=False),
            FieldDef("last_press_event", "Zuletzt gedrueckt", kind="enum",
                     is_topic_split=True, creates_subchannel=False,
                     enum_labels={
                         "A_top":    "↑ A oben gedrueckt",
                         "A_bottom": "↓ A unten gedrueckt",
                         "B_top":    "↑ B oben gedrueckt",
                         "B_bottom": "↓ B unten gedrueckt",
                     }),
        ],
    ),
    EEPProfile(
        eep_id="F6-03-01",
        name="Wippentaster (PTM 2-Wippen)",
        rorg=0xF6, decoder=decode_f6_02,
        encoder=_make_encoder_dispatch("f6_02"),
        ui_kind="rx",
    ),
    EEPProfile(
        eep_id="F6-03-02",
        name="Wippentaster",
        rorg=0xF6, decoder=decode_f6_02,
        encoder=_make_encoder_dispatch("f6_02"),
        ui_kind="rx",
    ),
    EEPProfile(
        eep_id="F6-05-01",
        name="Wassermelder",
        rorg=0xF6, decoder=decode_f6_05_smoke,
        ui_kind="rx",
        fields=[
            FieldDef("alarm",       "Wasser-Alarm",  kind="bool", icon="💧",
                     enum_labels={True: "💧 ALARM", False: "✓ trocken"}),
            FieldDef("battery_low", "Batterie",      kind="bool",
                     enum_labels={True: "🔋 schwach", False: "🔋 OK"}),
        ],
    ),
    EEPProfile(
        eep_id="F6-05-02",
        name="Rauchmelder",
        description="AFRISO ASD20, Eltako FAFM — Alarm + Batterie-Status",
        rorg=0xF6, decoder=decode_f6_05_smoke,
        ui_kind="rx",
        fields=[
            FieldDef("alarm",       "Rauch-Alarm",  kind="bool", icon="🔥",
                     enum_labels={True: "🔥 ALARM", False: "✓ kein Alarm"}),
            FieldDef("battery_low", "Batterie",     kind="bool",
                     enum_labels={True: "🔋 schwach", False: "🔋 OK"}),
        ],
    ),
    EEPProfile(
        eep_id="F6-10-00",
        name="Fenstergriff",
        description="Hoppe / MecoTec — closed / tilted / open",
        rorg=0xF6, decoder=decode_f6_10_window_handle,
        ui_kind="rx",
        fields=[
            FieldDef("state", "Fensterstatus", kind="enum",
                     enum_labels={"closed": "zu", "tilted": "gekippt", "open": "offen"}),
        ],
    ),

    # A5 4BS — Sensoren / Aktoren
    EEPProfile(
        eep_id="A5-02-01",
        name="Temperatursensor",
        rorg=0xA5, decoder=decode_a5_02_temperature, ui_kind="rx",
        fields=[FieldDef("temperature_c", "Temperatur", unit="°C", decimals=1)],
    ),
    EEPProfile(
        eep_id="A5-02-05",
        name="Temperatursensor",
        rorg=0xA5, decoder=decode_a5_02_temperature, ui_kind="rx",
        fields=[FieldDef("temperature_c", "Temperatur", unit="°C", decimals=1)],
    ),
    EEPProfile(
        eep_id="A5-04-01",
        name="Temp + Feuchte",
        rorg=0xA5, decoder=decode_a5_04_temp_humidity, ui_kind="rx",
        fields=[
            FieldDef("temperature_c", "Temperatur", unit="°C", decimals=1),
            FieldDef("humidity_pct",  "Feuchte",    unit="%",  decimals=1),
        ],
    ),
    EEPProfile(
        eep_id="A5-04-02",
        name="Temp + Feuchte",
        rorg=0xA5, decoder=decode_a5_04_temp_humidity, ui_kind="rx",
        fields=[
            FieldDef("temperature_c", "Temperatur", unit="°C", decimals=1),
            FieldDef("humidity_pct",  "Feuchte",    unit="%",  decimals=1),
        ],
    ),
    EEPProfile(
        eep_id="A5-07-01",
        name="Bewegungsmelder",
        rorg=0xA5, decoder=decode_a5_07_motion, ui_kind="rx",
        fields=[
            FieldDef("motion",    "Bewegung", kind="bool",
                     enum_labels={True: "Bewegung", False: "ruhig"}),
            FieldDef("voltage_v", "Spannung", unit="V", decimals=2, is_topic_split=False),
        ],
    ),
    EEPProfile(
        eep_id="A5-07-02",
        name="Bewegungsmelder",
        rorg=0xA5, decoder=decode_a5_07_motion, ui_kind="rx",
        fields=[
            FieldDef("motion",    "Bewegung", kind="bool",
                     enum_labels={True: "Bewegung", False: "ruhig"}),
            FieldDef("voltage_v", "Spannung", unit="V", decimals=2, is_topic_split=False),
        ],
    ),
    EEPProfile(
        eep_id="A5-10-02",
        name="Raumbedienteil (Temp+Sollwert+Lüfter+Belegung)",
        description="Thermokon thanos (L/LQ SR), eltako-baugleich — "
                    "Ist-Temperatur + Sollwert-Korrektur + Lüfterstufe + Raumbelegung",
        rorg=0xA5, decoder=decode_a5_10_02, ui_kind="rx",
        fields=[
            FieldDef("temperature_c",     "Temperatur",         unit="°C", decimals=1),
            FieldDef("setpoint_offset_c", "Sollwert-Korrektur", unit="°C", decimals=1),
            FieldDef("fan_stage", "Lüfterstufe", kind="enum",
                     enum_labels={"auto": "Automatik", "0": "Aus",
                                  "1": "Stufe 1", "2": "Stufe 2", "3": "Stufe 3"}),
            FieldDef("occupancy", "Raumbelegung", kind="bool",
                     enum_labels={True: "Belegt / Anwesend", False: "Unbelegt / Abwesend"}),
            # Diagnose (kein eigenes Topic) — zum Nachjustieren gegen Live-Log.
            FieldDef("fan_raw",      "Lüfter (roh)",   is_topic_split=False),
            FieldDef("setpoint_raw", "Sollwert (roh)", is_topic_split=False),
        ],
    ),
    EEPProfile(
        eep_id="A5-10-03",
        name="Raumbedienteil (Temp+Setpoint)",
        description="OPUS RTS55, Eltako FTR/FAE, Thermokon SRC-DO — "
                    "Ist-Temperatur + Sollwert-Korrektur am Drehrad",
        rorg=0xA5, decoder=decode_a5_10_room_panel, ui_kind="rx",
        fields=[
            FieldDef("temperature_c",     "Temperatur",         unit="°C", decimals=1),
            FieldDef("setpoint_offset_c", "Sollwert-Korrektur", unit="°C", decimals=1),
        ],
    ),
    EEPProfile(
        eep_id="A5-10-06",
        name="Raumbedienteil (Temp+Setpoint+Slider)",
        description="OPUS RTS55 S/SP+S, Thermokon SR04P MS — "
                    "Ist-Temperatur + Sollwert-Korrektur + Praesenz-/Tag-Nacht-Slider",
        rorg=0xA5, decoder=decode_a5_10_room_panel, ui_kind="rx",
        # TX: an Heizregler (Thermokon STC-DO8) sendbar — Ist-Temp +
        # Sollwertverschiebung + Tag/Nacht. ui_kind bleibt "rx" (echte
        # RX-Raumpanels), der TX-Fall wird ueber device_type=Valve_N erkannt.
        encoder=_make_encoder_dispatch("a5_10_06"),
        command_keys=["temperature", "setpoint_offset", "day", "night"],
        fields=[
            FieldDef("temperature_c",     "Temperatur",         unit="°C", decimals=1),
            FieldDef("setpoint_offset_c", "Sollwert-Korrektur", unit="°C", decimals=1),
            # Slider-Bit am Geraet: bei OPUS = Anwesend/Abwesend,
            # bei Eltako/Thermokon = Tag/Nacht. EnOcean-spec nennt es "Day/Night".
            FieldDef("day",               "Anwesend/Tag",       kind="bool",
                     enum_labels={True: "Anwesend / Tag", False: "Abwesend / Nacht"}),
        ],
    ),
    EEPProfile(
        eep_id="A5-12-00",
        name="Zaehler",
        rorg=0xA5, decoder=decode_a5_12_meter, ui_kind="rx",
        telegram_channel_field="tariff",
        fields=[
            FieldDef("energy_kwh", "Energie",  unit="kWh", icon="Σ", decimals=2),
            FieldDef("current_w",  "Leistung", unit="W",   icon="⚡", decimals=1),
        ],
    ),
    EEPProfile(
        eep_id="A5-12-01",
        name="Energiezaehler",
        description="Eltako FWZ14/DSZ14DRS — Zaehlerstand kWh + Leistung W, "
                    "Doppeltarif via DB0 bit 4 (0=Normaltarif, 1=Nachttarif), "
                    "plus Seriennummer-Telegramme (DB0=0x8F, alle 10 Min)",
        rorg=0xA5, decoder=decode_a5_12_meter, ui_kind="rx",
        # Sub-Channel-Selector ist der Tarif aus DB0 bit 4 (decoded["tariff"]).
        # Channels mit meta.tele_channel=0 sehen nur Normaltarif-Telegramme,
        # mit meta.tele_channel=1 nur Nachttarif-Telegramme.
        telegram_channel_field="tariff",
        fields=[
            FieldDef("energy_kwh", "Energie",  unit="kWh", icon="Σ", decimals=2),
            FieldDef("current_w",  "Leistung", unit="W",   icon="⚡", decimals=1),
            # Seriennummer-Bestandteile (BCD-decoded). DSZ14(W)DRS sendet das
            # alle 10 Minuten in 2 Teilen. is_topic_split=False -> kein eigener
            # Channel; die Werte landen aber im decoded-Dict, im raw-MQTT-Topic
            # und im Live-Log. Der LogBuffer kombiniert beide Teile beim Merge
            # zu serial_number = "S-AABBCC".
            FieldDef("serial_high",   "Seriennummer Praefix",
                     kind="string", is_topic_split=False),
            FieldDef("serial_low",    "Seriennummer Suffix",
                     kind="string", is_topic_split=False),
            # Seriennummer ist geraet-weit, nicht pro Tarif. Importer legt
            # damit nur 1x einen Channel ohne tele_channel-Filter an.
            FieldDef("serial_number", "Seriennummer",
                     kind="string", is_topic_split=True,
                     tariff_independent=True),
        ],
    ),
    EEPProfile(
        eep_id="A5-12-02",
        name="Gas-Zaehler",
        description="Eltako Gas-Zaehler (z.B. F3Z14D-Eingang). "
                    "Zaehlerstand in 0,1 m^3, optional Durchfluss in m^3/h.",
        rorg=0xA5, decoder=decode_a5_12_gas, ui_kind="rx",
        telegram_channel_field="tariff",
        fields=[
            FieldDef("volume_m3", "Volumen",    unit="m³",   icon="Σ", decimals=2),
            FieldDef("flow_m3h",  "Durchfluss", unit="m³/h", icon="≈", decimals=2),
            FieldDef("serial_high",   "Seriennummer Praefix",
                     kind="string", is_topic_split=False),
            FieldDef("serial_low",    "Seriennummer Suffix",
                     kind="string", is_topic_split=False),
            FieldDef("serial_number", "Seriennummer",
                     kind="string", is_topic_split=True,
                     tariff_independent=True),
        ],
    ),
    EEPProfile(
        eep_id="A5-12-03",
        name="Wasser-Zaehler",
        description="Eltako Wasser-Zaehler (z.B. F3Z14D-Eingang). "
                    "Zaehlerstand in 0,1 m^3, optional Durchfluss in L/min.",
        rorg=0xA5, decoder=decode_a5_12_water, ui_kind="rx",
        telegram_channel_field="tariff",
        fields=[
            FieldDef("volume_m3",  "Volumen",    unit="m³",    icon="Σ", decimals=2),
            FieldDef("flow_l_min", "Durchfluss", unit="L/min", icon="≈", decimals=2),
            FieldDef("serial_high",   "Seriennummer Praefix",
                     kind="string", is_topic_split=False),
            FieldDef("serial_low",    "Seriennummer Suffix",
                     kind="string", is_topic_split=False),
            FieldDef("serial_number", "Seriennummer",
                     kind="string", is_topic_split=True,
                     tariff_independent=True),
        ],
    ),
    EEPProfile(
        eep_id="A5-13-01",
        name="Wetterstation",
        description="Eltako FWS61 — Daemmerung, Temperatur, Wind, Tag/Nacht, Regen, Sonne O/S/W",
        rorg=0xA5, decoder=decode_a5_13_weather, ui_kind="rx",
        model_patterns=["FWS61", "FWS81"],
        fields=[
            FieldDef("outdoor_temp_c", "Außentemperatur", unit="°C", decimals=1),
            FieldDef("wind_speed_ms",  "Wind",            unit="m/s", icon="💨", decimals=1),
            FieldDef("rain",           "Regen",           kind="bool",
                     enum_labels={True: "🌧 Regen", False: "☀ trocken"}),
            FieldDef("day",            "Tag/Nacht",       kind="bool",
                     enum_labels={True: "Tag", False: "Nacht"}),
            FieldDef("dawn_lux",       "Dämmerung",       unit="lx"),
            FieldDef("sun_east_klx",   "Sonne Ost",       unit="klx", icon="☀", decimals=1),
            FieldDef("sun_south_klx",  "Sonne Süd",       unit="klx", icon="☀", decimals=1),
            FieldDef("sun_west_klx",   "Sonne West",      unit="klx", icon="☀", decimals=1),
            FieldDef("sun_max_klx",    "Sonne max.",      unit="klx", icon="☀", decimals=1),
        ],
    ),
    EEPProfile(
        eep_id="A5-13-02",
        name="Wetterstation (Sonne)",
        rorg=0xA5, decoder=decode_a5_13_weather, ui_kind="rx",
        fields=[
            FieldDef("sun_east_klx",  "Sonne Ost",  unit="klx", icon="☀", decimals=1),
            FieldDef("sun_south_klx", "Sonne Süd",  unit="klx", icon="☀", decimals=1),
            FieldDef("sun_west_klx",  "Sonne West", unit="klx", icon="☀", decimals=1),
            FieldDef("sun_max_klx",   "Sonne max.", unit="klx", icon="☀", decimals=1),
        ],
    ),
    EEPProfile(
        eep_id="A5-38-08",
        name="Central Command (Switch/Dim)",
        description="Standard EnOcean Schalt-/Dimm-Aktor",
        rorg=0xA5, decoder=decode_a5_38_08_dimmer,
        encoder=_make_encoder_dispatch("a5_38_08_dim"),
        ui_kind="dimmer",
        command_keys=["state", "dim", "ramp"],
        fields=[
            FieldDef("dim_percent", "Dimmwert", unit="%", icon="🌑→🌕"),
            FieldDef("on",          "Ein/Aus",  kind="bool",
                     enum_labels={True: "AN", False: "AUS"}, is_topic_split=False),
        ],
    ),
    EEPProfile(
        eep_id="A5-3F-7F",
        name="Universal 4BS / Rolladen",
        description="Eltako FSB Rolladenaktor (Status-Telegramme)",
        rorg=0xA5, decoder=decode_a5_3f_7f_universal,
        encoder=_make_encoder_dispatch("eltako_fsb"),
        ui_kind="shutter",
        command_keys=["command", "duration_s", "position"],
        fields=[
            # Eltako sendet das Telegramm beim STOPP — state ist also der
            # zuletzt-gefahrene Zustand, nicht "fährt gerade noch"
            FieldDef("state",       "Zustand",  kind="enum",
                     enum_labels={
                         "stop": "■ gestoppt",
                         "moving_up": "↑ fuhr auf",
                         "moving_down": "↓ fuhr ab",
                         "end_up": "▲ Endlage oben",
                         "end_down": "▼ Endlage unten",
                     }),
            FieldDef("duration_s",  "Dauer",    unit="s", decimals=1, is_topic_split=False),
            FieldDef("moving",      "Fährt",    kind="bool", is_topic_split=False),
            FieldDef("at_end_position", "Endlage", kind="bool", is_topic_split=False),
            # 1-Byte RPS Tasten-Quittung — kein eigenes Topic, nur Info
            FieldDef("press",       "Taste",    kind="enum",
                     enum_labels={"press_up": "▲ Taste auf",
                                  "press_down": "▼ Taste ab"},
                     is_topic_split=False),
        ],
    ),

    # Eltako-spezifisch
    EEPProfile(
        eep_id="ELTAKO-FSB",
        name="Eltako Rolladen-Aktor",
        rorg=0xA5, decoder=decode_eltako_fsb_shutter,
        encoder=_make_encoder_dispatch("eltako_fsb"),
        ui_kind="shutter",
        command_keys=["command", "duration_s", "position"],
        fields=[
            FieldDef("command",    "Befehl",   kind="enum",
                     enum_labels={"up": "auf", "down": "ab", "stop": "stop"}),
            FieldDef("duration_s", "Dauer",    unit="s", decimals=1, is_topic_split=False),
        ],
    ),
    EEPProfile(
        eep_id="ELTAKO-FSR",
        name="Eltako Schalt-Aktor",
        rorg=0xA5, decoder=decode_eltako_fsr_switch,
        encoder=_make_encoder_dispatch("eltako_fsr"),
        ui_kind="switch",
        command_keys=["state"],
        fields=[
            FieldDef("state", "Zustand", kind="enum",
                     enum_labels={"ON": "AN", "OFF": "AUS"}),
        ],
    ),
    EEPProfile(
        eep_id="ELTAKO-FSR-FEEDBACK",
        name="Eltako FSR Feedback",
        rorg=0xA5, decoder=decode_eltako_feedback_fsr, ui_kind="switch",
        fields=[
            FieldDef("on",    "Ein/Aus", kind="bool",
                     enum_labels={True: "AN", False: "AUS"}),
            FieldDef("state", "Zustand", kind="enum", is_topic_split=False),
        ],
    ),
    EEPProfile(
        eep_id="ELTAKO-FUD-FEEDBACK",
        name="Eltako Dimmer Feedback",
        rorg=0xA5, decoder=decode_eltako_feedback_fud, ui_kind="dimmer",
        fields=[
            FieldDef("dim_percent", "Dimmwert", unit="%"),
            FieldDef("on",          "Ein/Aus",  kind="bool",
                     enum_labels={True: "AN", False: "AUS"}, is_topic_split=False),
        ],
    ),
    EEPProfile(
        eep_id="ELTAKO-FSB-FEEDBACK",
        name="Eltako Rolladen Feedback",
        rorg=0xA5, decoder=decode_eltako_feedback_fsb, ui_kind="shutter",
        fields=[
            FieldDef("state", "Zustand", kind="enum",
                     enum_labels={
                         "stop": "stop",
                         "moving_up": "fährt auf",
                         "moving_down": "fährt ab",
                         "end_up": "oben",
                         "end_down": "unten",
                     }),
            FieldDef("duration_s", "Dauer", unit="s", decimals=1, is_topic_split=False),
        ],
    ),

    # D5 1BS
    EEPProfile(
        eep_id="D5-00-01",
        name="Magnetkontakt",
        description="Fenster/Türkontakt",
        rorg=0xD5, decoder=decode_d5_00_contact, ui_kind="rx",
        fields=[
            FieldDef("closed", "Geschlossen", kind="bool",
                     enum_labels={True: "geschlossen", False: "offen"}),
            FieldDef("state",  "Status",      kind="enum", is_topic_split=False),
        ],
    ),

    # D2-01 VLD: Schalter und Dimmer mit/ohne Energiemessung.
    # Sub-Profile differenzieren Schalter vs. Dimmer:
    #   -00: 1-Kanal Switch + Pilot Wire
    #   -01: 2-Kanal Switch ohne Energiemessung (OPUS BRiDGE 1/2 Kanal)
    #   -08: Switch + Energiemessung
    #   -09: Switch+Dimmer + Energiemessung
    #   -0C: Switch+Dimmer ohne Energiemessung
    #   -0D: Switch+Dimmer ohne Energiemessung (Pilot Wire)
    #   -12: Multi-Channel Switch+Dimmer
    EEPProfile(
        eep_id="D2-01-00",
        name="VLD Aktor (1-Kanal Switch + Pilot Wire)", rorg=0xD2,
        decoder=decode_d2_01_actuator, ui_kind="switch",
        encoder=_make_encoder_dispatch("d2_01_set_output"),
        command_keys=["state", "on", "io_channel"],
        fields=[
            FieldDef("on", "Status", kind="bool",
                     enum_labels={True: "AN", False: "AUS"}),
        ],
    ),
    EEPProfile(
        eep_id="D2-01-01",
        name="VLD Aktor (2-Kanal Switch ohne Energiemessung)",
        description="OPUS BRiDGE 1 Kanal, OPUS BRiDGE 2 Kanal — bidirektional",
        rorg=0xD2,
        decoder=decode_d2_01_actuator, ui_kind="switch",
        encoder=_make_encoder_dispatch("d2_01_set_output"),
        command_keys=["state", "on", "io_channel"],
        fields=[
            FieldDef("on", "Status", kind="bool",
                     enum_labels={True: "AN", False: "AUS"}),
        ],
    ),
    EEPProfile(
        eep_id="D2-01-03",
        name="VLD Aktor (1-Kanal Dimmer ohne Energiemessung)",
        description="OPUS BRiDGE Universaldimmer / LED-Funk-Dimmer — bidirektional",
        rorg=0xD2,
        decoder=decode_d2_01_actuator, ui_kind="dimmer",
        encoder=_make_encoder_dispatch("d2_01_set_output"),
        command_keys=["state", "dim", "io_channel"],
        fields=[
            FieldDef("on", "Status", kind="bool",
                     enum_labels={True: "AN", False: "AUS"}),
            FieldDef("dim_percent", "Dim-Wert", unit="%", decimals=0),
        ],
    ),
    EEPProfile(
        eep_id="D2-01-08",
        name="VLD Aktor (Switch + Energiemessung)", rorg=0xD2,
        decoder=decode_d2_01_actuator, ui_kind="switch",
        encoder=_make_encoder_dispatch("d2_01_set_output"),
        command_keys=["state", "on", "io_channel"],
        fields=[
            FieldDef("on", "Status", kind="bool",
                     enum_labels={True: "AN", False: "AUS"}),
        ],
    ),
    EEPProfile(
        eep_id="D2-01-09",
        name="VLD Aktor (Switch+Dimmer + Energiemessung)", rorg=0xD2,
        decoder=decode_d2_01_actuator, ui_kind="dimmer",
        encoder=_make_encoder_dispatch("d2_01_set_output"),
        command_keys=["state", "dim", "io_channel"],
        fields=[
            FieldDef("on", "Status", kind="bool",
                     enum_labels={True: "AN", False: "AUS"}),
            FieldDef("dim_percent", "Dim-Wert", unit="%", decimals=0),
        ],
    ),
    EEPProfile(
        eep_id="D2-01-0C",
        name="VLD Aktor (Switch+Dimmer ohne Energiemessung)", rorg=0xD2,
        decoder=decode_d2_01_actuator, ui_kind="dimmer",
        encoder=_make_encoder_dispatch("d2_01_set_output"),
        command_keys=["state", "dim", "io_channel"],
        fields=[
            FieldDef("on", "Status", kind="bool",
                     enum_labels={True: "AN", False: "AUS"}),
            FieldDef("dim_percent", "Dim-Wert", unit="%", decimals=0),
        ],
    ),
    EEPProfile(
        eep_id="D2-01-0D",
        name="VLD Aktor (Switch+Dimmer + Pilot Wire)", rorg=0xD2,
        decoder=decode_d2_01_actuator, ui_kind="dimmer",
        encoder=_make_encoder_dispatch("d2_01_set_output"),
        command_keys=["state", "dim", "io_channel"],
        fields=[
            FieldDef("on", "Status", kind="bool",
                     enum_labels={True: "AN", False: "AUS"}),
            FieldDef("dim_percent", "Dim-Wert", unit="%", decimals=0),
        ],
    ),
    EEPProfile(
        eep_id="D2-01-12",
        name="VLD Aktor (Multi-Channel Switch+Dimmer)", rorg=0xD2,
        decoder=decode_d2_01_actuator, ui_kind="dimmer",
        encoder=_make_encoder_dispatch("d2_01_set_output"),
        command_keys=["state", "dim", "io_channel"],
        fields=[
            FieldDef("on", "Status", kind="bool",
                     enum_labels={True: "AN", False: "AUS"}),
            FieldDef("dim_percent", "Dim-Wert", unit="%", decimals=0),
        ],
    ),
    # M77d: products.yaml verwendet "D2-01-XX" als Wildcard fuer die ganze
    # D2-01-Familie (so wie es im Export steht: Sub-Variante steht
    # nur im config-String). Damit der Backend-Profile-Lookup nicht ins Leere
    # laeuft und der Channel als "rx" durchrutscht, registrieren wir das XX
    # explizit. ui_kind wird im _product_to_json per device_type-Override auf
    # "dimmer" / "switch" / "valve" gesetzt — Default hier "switch".
    EEPProfile(
        eep_id="D2-01-XX",
        name="VLD Aktor (generisch)", rorg=0xD2,
        decoder=decode_d2_01_actuator, ui_kind="switch",
        encoder=_make_encoder_dispatch("d2_01_set_output"),
        command_keys=["state", "on", "io_channel"],
        fields=[
            FieldDef("on", "Status", kind="bool",
                     enum_labels={True: "AN", False: "AUS"}),
        ],
    ),
    # M79b/M79f: D2-05-XX Blind Position Control fuer OPUS Bridge UP-eSchalter
    # Jal und andere Rolladen/Jalousie-VLD-Aktoren.
    # D2-05-02 = OPUS BRiDGE Rollladen/Jalousie: Position + Lamellenwinkel,
    # bidirektional, 1-Kanal Motorschalter mit lokaler Bedienung.
    EEPProfile(
        eep_id="D2-05-00",
        name="Blind Position Control", rorg=0xD2,
        decoder=decode_d2_05_blind, ui_kind="shutter",
        fields=[
            FieldDef("position_percent", "Position", unit="%", decimals=0),
        ],
    ),
    EEPProfile(
        eep_id="D2-05-01",
        name="Blind Position Control (Position + Lamelle)", rorg=0xD2,
        decoder=decode_d2_05_blind, ui_kind="shutter",
        fields=[
            FieldDef("position_percent", "Position", unit="%", decimals=0),
            FieldDef("angle", "Lamellenwinkel", unit="°", decimals=0),
        ],
    ),
    EEPProfile(
        eep_id="D2-05-02",
        name="Blind Position Control (OPUS BRiDGE Rollladen/Jalousie)",
        description="1-Kanal Motorschalter, Position + Kippstellung, bidirektional",
        rorg=0xD2,
        decoder=decode_d2_05_blind, ui_kind="shutter",
        fields=[
            FieldDef("position_percent", "Position", unit="%", decimals=0),
            FieldDef("angle", "Lamellenwinkel", unit="°", decimals=0),
        ],
    ),
    EEPProfile(
        eep_id="D2-05-XX",
        name="Blind Position Control (generisch)", rorg=0xD2,
        decoder=decode_d2_05_blind, ui_kind="shutter",
        fields=[
            FieldDef("position_percent", "Position", unit="%", decimals=0),
        ],
    ),
]


# ---------------------------------------------------------------------------
# Registrierung in EEPRegistry (Decoder) und EEPProfileRegistry (Profile)
# ---------------------------------------------------------------------------


def register_all(reg: EEPRegistry) -> None:
    """Registriert alle bekannten Decoder im EEPRegistry."""
    for p in PROFILES:
        if p.decoder is not None:
            reg.register(p.eep_id, p.decoder)
    # Raw-Fallbacks pro RORG
    for rorg in (0xF6, 0xA5, 0xD5, 0xD2, 0xD4):
        reg.register_raw(rorg, raw_decode)


def register_profiles(preg: EEPProfileRegistry) -> None:
    """Registriert alle Profile-Objekte (UI-Felder, Encoder, Klassifikation)."""
    for p in PROFILES:
        preg.register(p)
