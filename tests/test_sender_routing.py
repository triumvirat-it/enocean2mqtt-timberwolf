"""
Tests fuer FAM14-Bus Sender-ID-Berechnung (M72 + M77).

Eltako-FAM14 nutzt einheitlich HEX-direct (Bus-Adr dezimal direkt als Hex
ins letzte Byte). Live verifiziert am User-System mit 5 Modul-Typen:

    FDG14    Adr 10 → 0x0A → FF80008A   (Dimmer 8 / LED Handlaeufe)
    DSZ14DRS Adr 26 → 0x1A → FF80009A   (Privatzaehler 460656)
    FWZ14    Adr 27 → 0x1B → FF80009B
    FSR14    Adr 28 → 0x1C → FF80009C   (Pool-Pumpe)
    FSR14    Adr 51 → 0x33 → FF8000B3   (Couch)

bcd_to_hex bleibt als Library-Funktion fuer evtl. andere Hersteller.
"""
from app.sender_routing import (
    bcd_to_hex,
    fam14_address_to_sender_id,
    fts14em_data_byte,
    fts14em_sender_id,
)


# --- Live-verifizierte HEX-direct Konvention -------------------------------


def test_fdg14_hex_encoding():
    """FDG14 Dimmer Bus-Adr 10 → 0x0A → FF80008A (live verifiziert)."""
    assert fam14_address_to_sender_id("FF800080", 10) == "FF80008A"
    assert fam14_address_to_sender_id("FF800080", 11) == "FF80008B"
    assert fam14_address_to_sender_id("FF800080", 2) == "FF800082"
    assert fam14_address_to_sender_id("FF800080", 17) == "FF800091"


def test_dsz14drs_hex_encoding():
    """DSZ14DRS Privatzaehler Bus-Adr 26 → 0x1A → FF80009A (live verifiziert)."""
    assert fam14_address_to_sender_id("FF800080", 26) == "FF80009A"


def test_fwz14_hex_encoding():
    """FWZ14 Bus-Adr 27 → 0x1B → FF80009B (live verifiziert)."""
    assert fam14_address_to_sender_id("FF800080", 27) == "FF80009B"


def test_fsr14_pool_hex_encoding():
    """FSR14-2x Pool: Bus-Adr 28/29 → FF80009C/9D."""
    assert fam14_address_to_sender_id("FF800080", 28) == "FF80009C"
    assert fam14_address_to_sender_id("FF800080", 29) == "FF80009D"


def test_fsr14_couch_hex_encoding():
    """FSR14-2x Couch: Bus-Adr 51 → FF8000B3 (live verifiziert mit RPS [70])."""
    assert fam14_address_to_sender_id("FF800080", 51) == "FF8000B3"
    assert fam14_address_to_sender_id("FF800080", 52) == "FF8000B4"


def test_fsr14_4x_block_18_21_hex():
    """FSR14-4x Adr 18-21 → FF800092..95."""
    assert fam14_address_to_sender_id("FF800080", 18) == "FF800092"
    assert fam14_address_to_sender_id("FF800080", 19) == "FF800093"
    assert fam14_address_to_sender_id("FF800080", 20) == "FF800094"
    assert fam14_address_to_sender_id("FF800080", 21) == "FF800095"


# --- Default & Limits ------------------------------------------------------


def test_default_is_hex():
    """Ohne addressing-Parameter wird HEX-direct genutzt (M77 Default)."""
    assert fam14_address_to_sender_id("FF800080", 10) == "FF80008A"
    assert fam14_address_to_sender_id("FF800080", 28) == "FF80009C"


def test_hex_limit_127():
    """HEX kann den ganzen 7-bit-Block ausschoepfen — bis Adr 127."""
    assert fam14_address_to_sender_id("FF800080", 127, "hex") == "FF8000FF"
    assert fam14_address_to_sender_id("FF800080", 128, "hex") is None


def test_invalid_base():
    assert fam14_address_to_sender_id("XYZ", 10) is None
    assert fam14_address_to_sender_id("", 10) is None


def test_zero_address_invalid():
    """Bus-Adresse 0 nicht erlaubt (reserviert)."""
    assert fam14_address_to_sender_id("FF800080", 0, "hex") is None
    assert fam14_address_to_sender_id("FF800080", 0, "bcd") is None


# --- BCD-Funktion bleibt verfuegbar (Library-Support) ----------------------


def test_bcd_function_still_works():
    """bcd_to_hex bleibt als Library-Funktion fuer andere Hersteller."""
    assert bcd_to_hex(0) == 0x00
    assert bcd_to_hex(9) == 0x09
    assert bcd_to_hex(10) == 0x10
    assert bcd_to_hex(99) == 0x99


def test_bcd_addressing_opt_in():
    """addressing='bcd' explizit fuer evtl. nicht-Eltako-Hersteller."""
    assert fam14_address_to_sender_id("FF800080", 10, "bcd") == "FF800090"
    assert fam14_address_to_sender_id("FF800080", 99, "bcd") == "FF800099"
    assert fam14_address_to_sender_id("FF800080", 100, "bcd") is None


# --- FTS14EM Quasi-Dezimal-IDs (Datenblatt 30 014 060-1) -------------------


def test_fts14em_ut_first_group_basic():
    """Gruppe 1, Drehschalter 0, E1..E10 → 0x1001..0x1010 (Quasi-Dezimal)."""
    assert fts14em_sender_id(1, 0, 1, "UT") == "00001001"
    assert fts14em_sender_id(1, 0, 2, "UT") == "00001002"
    assert fts14em_sender_id(1, 0, 9, "UT") == "00001009"
    # Quasi-Dezimal-Sprung: nach 1009 kommt 1010, nicht 100A
    assert fts14em_sender_id(1, 0, 10, "UT") == "00001010"


def test_fts14em_ut_screenshot_e2_garage():
    """User-PCT14-Screenshot: ID 00 00 10 02 = E2 (rechts unten), Gruppe 1,
    Drehschalter 0 — angelernt auf FSR14-4x Kanal 4 (Garagenlicht)."""
    assert fts14em_sender_id(1, 0, 2, "UT") == "00001002"
    assert fts14em_data_byte(2, "UT") == 0x50  # rechts unten


def test_fts14em_ut_higher_groups():
    """Gruppen 101/201/301/401."""
    assert fts14em_sender_id(101, 0, 1, "UT") == "00001101"
    assert fts14em_sender_id(101, 0, 10, "UT") == "00001110"
    assert fts14em_sender_id(201, 40, 5, "UT") == "00001245"
    assert fts14em_sender_id(401, 90, 10, "UT") == "00001500"  # max


def test_fts14em_ut_subdial_positions():
    """Oberer Drehschalter 0..90 in 10er-Schritten."""
    assert fts14em_sender_id(1, 40, 1, "UT") == "00001041"
    assert fts14em_sender_id(1, 90, 10, "UT") == "00001100"  # Gruppe 1 max


def test_fts14em_rt_pairs():
    """RT-Modus: nur gerade Eingangs-Nummern (gepaarte Wippen)."""
    # E1+E2 = 1002 (rechte Wippe), E3+E4 = 1004 (linke Wippe), ...
    assert fts14em_sender_id(1, 0, 2, "RT") == "00001002"
    assert fts14em_sender_id(1, 0, 4, "RT") == "00001004"
    assert fts14em_sender_id(1, 0, 10, "RT") == "00001010"
    # Ungerade Eingaenge in RT verboten
    assert fts14em_sender_id(1, 0, 1, "RT") is None
    assert fts14em_sender_id(1, 0, 3, "RT") is None


def test_fts14em_data_byte_ut_pattern():
    """Datenbyte-Muster 70/50/30/10 wiederholt sich alle 4 Eingaenge."""
    assert fts14em_data_byte(1, "UT") == 0x70   # rechts oben
    assert fts14em_data_byte(2, "UT") == 0x50   # rechts unten
    assert fts14em_data_byte(3, "UT") == 0x30   # links oben
    assert fts14em_data_byte(4, "UT") == 0x10   # links unten
    assert fts14em_data_byte(5, "UT") == 0x70   # Muster wiederholt
    assert fts14em_data_byte(8, "UT") == 0x10
    assert fts14em_data_byte(9, "UT") == 0x70
    assert fts14em_data_byte(10, "UT") == 0x50


def test_pipeline_tele_channel_matches_int():
    """_tele_channel_matches: Integer-Vergleich fuer DSZ14DRS-Doppeltarif."""
    from app.pipeline import _tele_channel_matches
    assert _tele_channel_matches(0, 0) is True
    assert _tele_channel_matches(1, 0) is False
    assert _tele_channel_matches(1, 1) is True


def test_pipeline_tele_channel_matches_string():
    """_tele_channel_matches: String-Vergleich fuer FT55-rocker_side."""
    from app.pipeline import _tele_channel_matches
    assert _tele_channel_matches("A", "A") is True
    assert _tele_channel_matches("A", "B") is False
    assert _tele_channel_matches("B", "B") is True


def test_decode_f6_02_rocker_events():
    """F6-02-01-Decoder liefert event + rocker_side fuer alle 5 Stuetzpunkte.
    User-Konvention (FT55 physisch): oben='0', unten='I' (live verifiziert)."""
    from app.eep.profiles import decode_f6_02
    # Live-Log raw=0x10 -> rocker_1='AI' -> User-Konvention: unten gedrueckt
    d = decode_f6_02(bytes([0x10]), status=0x30, rorg=0xF6)
    assert d["rocker_side"] == "A"
    assert d["rocker_action"] == "press_bottom"
    assert d["event"] == "A_bottom"
    assert d["pressed"] is True
    # Live-Log raw=0x30 -> rocker_1='A0' -> User-Konvention: oben gedrueckt
    d = decode_f6_02(bytes([0x30]), status=0x30, rorg=0xF6)
    assert d["rocker_side"] == "A"
    assert d["rocker_action"] == "press_top"
    assert d["event"] == "A_top"
    # raw=0x50 -> 'BI' -> B unten
    d = decode_f6_02(bytes([0x50]), status=0x30, rorg=0xF6)
    assert d["rocker_side"] == "B"
    assert d["event"] == "B_bottom"
    # raw=0x70 -> 'B0' -> B oben
    d = decode_f6_02(bytes([0x70]), status=0x30, rorg=0xF6)
    assert d["rocker_side"] == "B"
    assert d["event"] == "B_top"
    # Release (0x00 + U-message)
    d = decode_f6_02(bytes([0x00]), status=0x20, rorg=0xF6)
    assert d["rocker_side"] is None
    assert d["event"] == "release"
    assert d["pressed"] is False


def test_fts14em_invalid_inputs():
    """Out-of-range Werte ergeben None."""
    assert fts14em_sender_id(2, 0, 1, "UT") is None       # Gruppe 2 ungueltig
    assert fts14em_sender_id(1, 5, 1, "UT") is None       # Drehschalter 5 ungueltig
    assert fts14em_sender_id(1, 0, 0, "UT") is None       # Eingang 0 ungueltig
    assert fts14em_sender_id(1, 0, 11, "UT") is None      # Eingang 11 ungueltig
    assert fts14em_sender_id(1, 0, 1, "XY") is None       # Modus unbekannt
    assert fts14em_data_byte(11, "UT") is None
    assert fts14em_data_byte(0, "UT") is None
