"""
Telegramm-Encoder — bauen ESP3-Frames fuer das Senden an Aktoren.

Im Gegensatz zu app/eep/profiles.py (Decoder) packen diese Funktionen
einen Befehl (z.B. "on/off") in die richtigen Bytes.
"""
from __future__ import annotations

from dataclasses import dataclass

from .gateway.esp3 import ESP3Packet, PacketType, RORG


@dataclass(slots=True)
class CommandFrame:
    """
    Ein fertig kodierter ESP3-RADIO-Frame zum Senden ueber einen Gateway.

    Sender-ID muss bereits eingearbeitet sein.
    """
    packet: ESP3Packet
    sender_id: int
    destination_id: int = 0xFFFFFFFF
    expected_response: str | None = None  # spaeter: ACK/NACK-Erkennung


def _build_erp1_packet(
    rorg: int,
    payload: bytes,
    sender_id: int,
    status: int = 0x30,
    destination_id: int = 0xFFFFFFFF,
    send_with_response: bool = False,
) -> CommandFrame:
    """
    Baut ein RADIO_ERP1 Frame zum Senden.

    Data-Format (RADIO_ERP1, RX):
        [RORG] [payload] [sender_id 4B] [status]
    Optional-Format (TX):
        [sub_tel=0x03] [destination 4B] [dBm=0xFF] [security_level=0x00]
    """
    data = bytes([rorg]) + payload + sender_id.to_bytes(4, "big") + bytes([status])
    optional = (
        bytes([0x03])  # sub_tel: 3 transmissions (Standard fuer TX)
        + destination_id.to_bytes(4, "big")
        + bytes([0xFF])  # dBm fuer TX: 0xFF = max
        + bytes([0x00])  # security level: keine
    )
    pkt = ESP3Packet(packet_type=PacketType.RADIO_ERP1, data=data, optional=optional)
    return CommandFrame(
        packet=pkt,
        sender_id=sender_id,
        destination_id=destination_id,
    )


# ---------------------------------------------------------------------------
# F6-02-01: Wippschalter (PTM)
# ---------------------------------------------------------------------------


def encode_f6_02_button(
    sender_id: int,
    rocker: str,                # "AI", "A0", "BI", "B0"
    pressed: bool = True,
    second_action: bool = False,
    rocker_2: str | None = None,
) -> CommandFrame:
    """
    Sendet einen Wippentaster-Druck (F6-02-01).

    rocker: 'AI'/'A0'/'BI'/'B0'.
    pressed: True = Energy bow active (Taste gedrueckt), False = Released.
    """
    action_map = {"AI": 0, "A0": 1, "BI": 2, "B0": 3}
    if rocker not in action_map:
        raise ValueError(f"Unknown rocker action: {rocker}")

    b = (action_map[rocker] << 5) & 0xE0
    if pressed:
        b |= 0x10
    if second_action and rocker_2:
        b |= (action_map[rocker_2] << 1) & 0x0E
        b |= 0x01

    # Status: NU=1 bei N-Telegramm (Press), NU=0 bei U-Telegramm (Release)
    status = 0x30 if pressed else 0x20
    return _build_erp1_packet(RORG.RPS, bytes([b]), sender_id, status=status)


# ---------------------------------------------------------------------------
# Eltako-Schaltaktor (FSR14, FSR61, FSSA-230V) — proprietaeres A5-Format
# ---------------------------------------------------------------------------


def encode_eltako_switch(
    sender_id: int,
    state: bool,
) -> CommandFrame:
    """
    Sendet einen Eltako-Schaltbefehl (FSR-Serie).

    Format laut Eltako-Funkbus:
        DB3 = 0x70 (ON) oder 0x50 (OFF)
        DB2 = 0x00
        DB1 = 0x00
        DB0 = 0x09 (Data-Telegramm + Switch-Marker)
    """
    payload = bytes([
        0x70 if state else 0x50,   # DB3
        0x00,                       # DB2
        0x00,                       # DB1
        0x09,                       # DB0: bit3=data, bit0=switch
    ])
    return _build_erp1_packet(RORG.BS4, payload, sender_id)


# ---------------------------------------------------------------------------
# Eltako-Rolladen (FSB14, FSB61NP)
# ---------------------------------------------------------------------------


def encode_eltako_shutter(
    sender_id: int,
    command: str,           # "up", "down", "stop"
    duration_s: float = 0.0,
) -> CommandFrame:
    """
    Sendet einen Eltako-Rolladen-Befehl (FSB-Serie).

    Format EXAKT laut "Inhalte der Eltako-Funktelegramme" (FSB14/61/71,
    FUNC=0x3F, Typ=0x7F, ORG=0x07/4BS):
        DB3 = Laufzeit in 100ms, MSB
        DB2 = Laufzeit in 100ms, LSB
        DB1 = Kommando: 0x00=Stopp, 0x01=Auf, 0x02=Ab
        DB0 = 0x0A  (Bit3=1 Datentelegramm, Bit1=1 Laufzeit in DB3:DB2 in 100ms,
                     Bit2=0 nicht blockiert) — dasselbe Format wie das
                     Bestaetigungstelegramm des Aktors (DB0=0x0A).

    M99-Fix: vorher db3=Sekunden, db2=Zehntel + DB0=0x09 (z.B. 60s -> 255 dezi
    -> db3=25,db2=5 = "19 05 .. 09"). Das war KEIN gueltiges Eltako-Fahrkommando
    (DB3 Muell im Sekunden-Modus, Bit0 undefiniert) -> Aktor bewegte sich nicht.
    """
    # Eltako-Konvention (offizielle Doku): 0x01=auf, 0x02=ab, 0x00=stop
    cmd_map = {"stop": 0x00, "up": 0x01, "down": 0x02}
    if command not in cmd_map:
        raise ValueError(f"Unknown shutter command: {command}")

    # Laufzeit in 100ms-Schritten als 16-bit (DB3=MSB, DB2=LSB).
    duration_100ms = int(round(duration_s * 10))
    duration_100ms = max(0, min(0xFFFF, duration_100ms))
    db3 = (duration_100ms >> 8) & 0xFF
    db2 = duration_100ms & 0xFF
    payload = bytes([db3, db2, cmd_map[command], 0x0A])
    return _build_erp1_packet(RORG.BS4, payload, sender_id)


# ---------------------------------------------------------------------------
# A5-38-08: Central Command Gateway (Dimming/Switching) — Standard
# ---------------------------------------------------------------------------


def encode_a5_38_08_switch(
    sender_id: int,
    state: bool,
) -> CommandFrame:
    """
    A5-38-08 Sub-Profil 01 (Switching).

    DB3 = 0x01 (Command: Switch)
    DB2 = 0x00 (Time)
    DB1 = 0x00 (Ramp)
    DB0 = bit3=1 (data), bit0=state (0=off, 1=on)
    """
    payload = bytes([0x01, 0x00, 0x00, 0x08 | (0x01 if state else 0x00)])
    return _build_erp1_packet(RORG.BS4, payload, sender_id)


def encode_a5_38_08_dim(
    sender_id: int,
    dim_value: int,           # 0..100
    ramp_s: int = 0,          # bei Eltako: Dimmgeschwindigkeit-Code, NICHT Sekunden
    on: bool = True,
) -> CommandFrame:
    """
    A5-38-08 Sub-Profil 02 (Dimming).

    Nach Eltako-Doku "Inhalte der Eltako-Funktelegramme" fuer FLD61, FUD14,
    FUD61, FDG14 etc.:

    DB3 = 0x02 (Command-ID: Dimming)
    DB2 = Dimmwert 0..100 (dezimal)
    DB1 = Dimmgeschwindigkeit:
            0x00 = im Dimmer intern eingestellt
            0x01 = sehr schnell ... 0xFF = sehr langsam
    DB0 = bit3 (0x08) LRN: 1 = Datentelegramm (0 = Lerntelegramm)
          bit2 (0x04) Dimmwert blockieren: 1 = blockiert, 0 = nicht (Default 0!)
          bit0 (0x01) An/Aus: 1 = an, 0 = aus

    Eltako-Doku-Beispiele (zur Verifikation):
        02 32 00 09 -> 50% an, interner Dimmspeed
        02 64 01 09 -> 100% an, schnellster Dimmspeed
        02 14 FF 09 -> 20% an, langsamster Dimmspeed
        02 XX XX 08 -> Dimmer aus

    Wichtig: Bit2 darf NICHT gesetzt sein bei normalen Dim-Befehlen —
    sonst lockt sich der FLD61 auf den Wert und ignoriert weitere Aenderungen.
    """
    dim_value = max(0, min(100, dim_value))
    ramp_s = max(0, min(255, ramp_s))
    db0 = 0x08 | (0x01 if on else 0x00)  # KEIN bit2 (kein Wert-Blockieren)
    payload = bytes([0x02, dim_value, ramp_s, db0])
    return _build_erp1_packet(RORG.BS4, payload, sender_id)


# ---------------------------------------------------------------------------
# Teach-In Telegramme
# ---------------------------------------------------------------------------


def encode_a5_10_setpoint(
    sender_id: int,
    *,
    temperature_c: float = 20.0,
    setpoint_offset_c: float = 0.0,
    offset_min_c: float = -10.0,
    offset_max_c: float = 10.0,
    day: bool = True,
    night_setback_c: float = 0.0,
) -> CommandFrame:
    """
    Sendet ein A5-10-06-Raumbedienteil-Telegramm an einen Heizregler
    (z.B. Thermokon STC-DO8 Typ1/Typ2): Ist-Temperatur + Sollwert-
    verschiebung + Tag/Nacht. Damit "spielt" der Timberwolf den Raumfuehler.

    Byte-Layout (EEP A5-10-06, verifiziert ueber Thermokon-thanos-Handbuch):
      DB3 = Absenktemperatur (Betrag) — 0 = der Aktor verwaltet die Absenkung
      DB2 = Sollwertverstellung: 0..255 = offset_min_c..offset_max_c
      DB1 = Temperatur: 0..40 C = 255..0 (invertiert)
      DB0 = bit3 Daten(=1, kein Teach), bit0 Tag(=1)/Nacht(=0)

    offset_min_c/offset_max_c MUESSEN zur Aktor-Konfiguration passen
    (z.B. ±10 K), sonst stimmt die uebertragene Verschiebung in K nicht.
    """
    span = (offset_max_c - offset_min_c) or 1.0
    db2 = max(0, min(255, round((setpoint_offset_c - offset_min_c) / span * 255.0)))
    db1 = max(0, min(255, round((40.0 - temperature_c) / 40.0 * 255.0)))
    db3 = max(0, min(255, round(abs(night_setback_c) / 40.0 * 255.0)))
    db0 = 0x08 | (0x01 if day else 0x00)
    payload = bytes([db3, db2, db1, db0])
    return _build_erp1_packet(RORG.BS4, payload, sender_id, status=0x00)


def encode_4bs_teach_in(
    sender_id: int,
    rorg_func: int,
    rorg_type: int,
    manufacturer_id: int = 0x7FF,
) -> CommandFrame:
    """
    4BS-Teach-In mit EEP-Information (variant 3 of 4BS teach-in).

    DB3 = (FUNC << 2) | (TYPE_HI >> 5)
    DB2 = ((TYPE & 0x1F) << 3) | (MFR_ID_HI >> 8)
    DB1 = MFR_ID_LO
    DB0 = 0b1000_0000 (teach-in bit3=0, with EEP)
    """
    db3 = (rorg_func << 2) | ((rorg_type >> 5) & 0x03)
    db2 = ((rorg_type & 0x1F) << 3) | ((manufacturer_id >> 8) & 0x07)
    db1 = manufacturer_id & 0xFF
    db0 = 0x80  # LRN-Bit=0 (teach), bit7=1 (variant 3 = with EEP info)
    payload = bytes([db3, db2, db1, db0])
    return _build_erp1_packet(RORG.BS4, payload, sender_id)


# ---------------------------------------------------------------------------
# M82: D2-01-XX VLD Aktor — Set Output (Schaltbefehl)
# ---------------------------------------------------------------------------
#
# Live-Log-Beobachtung (Mitschnitt → OPUS BRiDGE):
#   FFBD00BB VLD [01 00 01]  → AN  (CMD=1 Set Output, IO=0, OV=1)
#   FFBD00BB VLD [01 00 00]  → AUS (CMD=1 Set Output, IO=0, OV=0)
#   01A03001 VLD [04 60 81]  → Status-Response ON  (vom Aktor zurueck)
#   01A03001 VLD [04 60 80]  → Status-Response OFF
#
# EnOcean EEP D2-01-XX Spec — Byte 0 Bits 3..0 = CMD (low nibble!):
#   CMD=0x01 Actuator Set Output


def encode_d2_01_set_output(
    sender_id: int,
    output_value: int,
    io_channel: int = 0,
) -> CommandFrame:
    """
    D2-01-XX Actuator Set Output (CMD=0x01).

    output_value: 0 = OFF, 1..100 = Dim-% / ON (bei Switch-Variante: >0 = AN).
                  Standard fuer Switch ist 0 (AUS) oder 100 (AN).
    io_channel: 0..29 = Aktor-Kanal, 30 = ALL.

    Payload (3 Byte):
        Byte 0 = 0x01 (CMD=1, low nibble)
        Byte 1 = IO Channel (0..30)
        Byte 2 = Output Value (0..100)

    Beispiel (live verifiziert):
        encode_d2_01_set_output(0xFFBD00BB, 1, 0)  → [01 00 01]
        encode_d2_01_set_output(0xFFBD00BB, 0, 0)  → [01 00 00]
    """
    if not (0 <= output_value <= 100):
        raise ValueError(f"output_value muss 0..100 sein: {output_value}")
    if not (0 <= io_channel <= 30):
        raise ValueError(f"io_channel muss 0..30 sein: {io_channel}")
    payload = bytes([
        0x01,                       # CMD=1 (Set Output) in LOW nibble
        io_channel & 0x1F,          # IO Channel (5 bit)
        output_value & 0x7F,        # Output Value (7 bit)
    ])
    return _build_erp1_packet(RORG.VLD, payload, sender_id)


# ---------------------------------------------------------------------------
# M83: EnOcean Remote Management (ReMan) — Pairing fuer OPUS BRiDGE
# ---------------------------------------------------------------------------
#
# Live-Log-Beobachtung (Mitschnitt Pairing-Sequenz):
#   REMOTE_MAN_COMMAND [00 01 07 FF 50 F4 C1 E6]  → UNLOCK mit Security Code
#   REMOTE_MAN_COMMAND [00 02 07 FF 50 F4 C1 E6]  → LOCK   mit Security Code
#   REMOTE_MAN_COMMAND [00 04 07 FF 00 00 00 00]  → QUERY  (kein Code)
#
# Format (8 Byte):
#   Byte 0    = 0x00       (Flags/Reserved)
#   Byte 1    = CMD-ID     (0x01=UNLOCK, 0x02=LOCK, 0x04=QUERY)
#   Byte 2-3  = 0x07 0xFF  (Manufacturer 0x7FF = EnOcean Wildcard)
#   Byte 4-7  = Security Code (32-bit Big-Endian Klartext)


def _build_reman_packet(
    data: bytes,
    sender_id: int = 0,
    destination_id: int = 0xFFFFFFFF,
) -> CommandFrame:
    """
    M83/M89: Baut ein REMOTE_MAN_COMMAND ESP3-Paket (packet_type=0x07).

    Verifiziert gegen Protokoll-Mitschnitt (SYS_EX_Telegram):
      data:     [Function 2B] [Manufacturer-ID 2B] [Message-Data ...]
      optional: NUR bei adressierten Telegrammen (Dest != FFFFFFFF/FFFFFFFE).
                Bei Broadcast → OptLen=0, Modem setzt Source/dBm selbst.

    Beispiele:
      Unlock:  00 01 07 FF <SC4>  Lock: 00 02 07 FF <SC4>
      QueryID: 00 04 07 FF 00 00 00  (3 Daten-Bytes)
    """
    if destination_id in (0xFFFFFFFF, 0xFFFFFFFE):
        optional = b""
    else:
        optional = (
            destination_id.to_bytes(4, "big")
            + sender_id.to_bytes(4, "big")
            + bytes([0xFF])
            + bytes([0x00])
        )
    pkt = ESP3Packet(
        packet_type=PacketType.REMOTE_MAN_COMMAND,
        data=data,
        optional=optional,
    )
    return CommandFrame(
        packet=pkt,
        sender_id=sender_id,
        destination_id=destination_id,
    )


def _reman_data(function: int, security_code: int | None = None) -> bytes:
    """ReMan-data: Function(2B) + Mfr 0x07FF + optional Security-Code(4B).
    Bei QueryID (kein Code) werden 3 Null-Daten-Bytes (00 00 00) gesendet."""
    head = bytes([(function >> 8) & 0xFF, function & 0xFF, 0x07, 0xFF])
    if security_code is None:
        return head + bytes([0x00, 0x00, 0x00])
    return head + security_code.to_bytes(4, "big")


def encode_reman_unlock(
    security_code: int,
    sender_id: int = 0,
    destination_id: int = 0xFFFFFFFF,
) -> CommandFrame:
    """UNLOCK (Fn 0x0001) mit Security Code. Broadcast → ohne Optional."""
    return _build_reman_packet(
        _reman_data(0x0001, security_code), sender_id, destination_id,
    )


def encode_reman_lock(
    security_code: int,
    sender_id: int = 0,
    destination_id: int = 0xFFFFFFFF,
) -> CommandFrame:
    """LOCK (Fn 0x0002) mit Security Code."""
    return _build_reman_packet(
        _reman_data(0x0002, security_code), sender_id, destination_id,
    )


def encode_reman_query_id(
    sender_id: int = 0,
    destination_id: int = 0xFFFFFFFF,
) -> CommandFrame:
    """
    QUERY ID (Fn 0x0004) — DAS ist die antwort-ausloesende Nachricht beim
    OPUS-Pairing (M89). Der Aktor antwortet mit Fn 0x0604 (QueryID-Response).
    Daten: 00 04 07 FF 00 00 00 (3 Null-Bytes, kein Security Code).
    """
    return _build_reman_packet(
        _reman_data(0x0004, None), sender_id, destination_id,
    )


def encode_reman_set_linktable(
    actor_id: int,
    sender_eoid: int,
    eep_rorg: int,
    eep_func: int,
    eep_type: int,
    chip_id: int = 0,
    index: int = 0,
    channel: int = 0,
    inbound: bool = True,
) -> CommandFrame:
    """
    M90: SetLinkTableContent (ReCom Fn 0x0212) — traegt eine Sender-ID in die
    Inbound-LinkTable des Aktors ein (= Berechtigung zum Schalten).

    Verifiziert aus Protokoll-Mitschnitt (RMCC_SetLinkTableContent_Telegram):
      ESP30-Data: 02 12 07 FF | <10 Byte Link-Entry>
      Link-Entry: [Bound 1B][Index 1B][Sender-EOID 4B][RORG][FUNC][TYPE][Channel]
        Bound: 0x00 = Inbound (Sender darf Aktor steuern), 0x80 = Outbound
      ADRESSIERT an den Aktor (Destination = actor_id) → mit Optional-Block.

    sender_eoid: die ID die eingetragen wird (Block-ID, z.B. FF800102) — DAS
                 ist die ID mit der danach geschaltet wird.
    chip_id:     Modem-Chip-ID (Source im Optional-Block).
    Aktor antwortet mit ReComAck (Fn 0x0006).
    """
    bound = 0x00 if inbound else 0x80
    data = bytes([
        0x02, 0x12, 0x07, 0xFF,             # Fn 0x0212 + Mfr 0x07FF
        bound, index & 0xFF,
        (sender_eoid >> 24) & 0xFF, (sender_eoid >> 16) & 0xFF,
        (sender_eoid >> 8) & 0xFF, sender_eoid & 0xFF,
        eep_rorg & 0xFF, eep_func & 0xFF, eep_type & 0xFF,
        channel & 0xFF,
    ])
    return _build_reman_packet(data, sender_id=chip_id, destination_id=actor_id)
