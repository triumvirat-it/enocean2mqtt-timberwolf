"""Tests fuer die Device-Scaffold-Engine (app/device_scaffold.py)."""
from __future__ import annotations

from app.device_scaffold import (
    CsvRow,
    convert_rows_to_devices,
    extract_enocean_ids,
    extract_inline_eep,
    make_slug,
    room_to_tags,
    _clean_name,
)
from app.products import ProductDB, _modelname_variants, _normalize_eep


# ---------------------------------------------------------------------------
# Adress-Parsing
# ---------------------------------------------------------------------------


def test_extract_single_id():
    addr = "0505090001h/t05 09 00 01/t05090001"
    assert extract_enocean_ids(addr) == ["05090001"]


def test_extract_double_id_actuator_and_ptm():
    addr = "55h/A/tFF 81 00 55/tFF810055 / 04220001h/t04 22 00 01/t04220001"
    ids = extract_enocean_ids(addr)
    assert "FF810055" in ids
    assert "04220001" in ids
    assert len(ids) == 2


def test_extract_no_id_when_not_learned():
    assert extract_enocean_ids("nicht angelernt") == []
    assert extract_enocean_ids("") == []


def test_extract_inline_eep():
    assert extract_inline_eep("ASD20 (EEP F60502)") == "F6-05-02"
    assert extract_inline_eep("Foo Bar") is None


# ---------------------------------------------------------------------------
# EEP-Normalisierung
# ---------------------------------------------------------------------------


def test_normalize_eep_strips_subvariant():
    assert _normalize_eep("A5-38-08(02)") == "A5-38-08"
    assert _normalize_eep("F6-02-01") == "F6-02-01"
    assert _normalize_eep("") is None
    assert _normalize_eep(None) is None


# ---------------------------------------------------------------------------
# Slug + Raum-Mapping
# ---------------------------------------------------------------------------


def test_make_slug_basic():
    assert make_slug("Haus & Hof Bereich") == "haus_und_hof_bereich"
    assert make_slug("Büro 2") == "buero_2"


def test_room_to_tags_hierarchical():
    assert room_to_tags("Wohnung A/Wohnzimmer") == ["Wohnung A", "Wohnzimmer"]
    assert room_to_tags("Pool") == ["Pool"]
    assert room_to_tags("") == []


def test_modelname_variants_handles_suffix():
    variants = _modelname_variants("FSB61NP (ab 41/11)")
    assert "FSB61NP (ab 41/11)" in variants
    assert "FSB61NP" in variants
    assert "FSB61NP ($ab 41/11)" in variants


def test_clean_name_strips_channel_prefix():
    assert _clean_name("2.1 Schaltaktor 0") == "Schaltaktor 0"
    assert _clean_name("ASD20 (EEP F60502)") == "ASD20"


# ---------------------------------------------------------------------------
# Scaffold (convert_rows_to_devices) — manuelles Anlegen aus dem Produktkatalog
# ---------------------------------------------------------------------------


def test_actuator_tx_scaffold_creates_8_tx_channels():
    """
    Thermokon STC-DO8 (RX-only Heizregler): manuelles Anlegen muss 8
    schlichte TX-Kanaele (A5-10-06, direction=tx) erzeugen — frueher 0
    (csv_import=skip blockte auch den Scaffold).
    """
    db = ProductDB.load_default()
    rows = [
        CsvRow(is_group_header=True, manufacturer="Thermokon", model="STC-DO8 Typ1"),
        CsvRow(is_data=True, manufacturer="Thermokon", model="STC-DO8 Typ1",
               name="STC-DO8 Typ1", room="", address_raw="nicht angelernt"),
    ]
    devices, _ = convert_rows_to_devices(rows, db)
    assert devices, "Scaffold gab kein Device zurueck"
    chs = devices[0].channels
    assert len(chs) == 8, f"erwartet 8 Kanaele, bekam {len(chs)}"
    assert all(c.eep == "A5-10-06" for c in chs), "alle Kanaele A5-10-06"
    assert all(c.direction == "tx" for c in chs), "alle Kanaele direction=tx"
    assert all(not c.enocean_id for c in chs), "TX-IDs leer (User traegt sie manuell ein)"


def test_bundled_ptm_channels_for_opus_bridge():
    """
    M79: OPUS Bridge UP-eSchalter erzeugt Aktor- + 2 PTM-Channels.
    Regression-Test fuer enocean_id=None (war "" → 500-Internal-Error).
    """
    db = ProductDB.load_default()
    rows = [
        CsvRow(
            is_group_header=True,
            manufacturer="Jäger Direkt - OPUS",
            model="OPUS Bridge UP-eSchalter 1 Kanal V1",
        ),
        CsvRow(
            is_data=True,
            manufacturer="Jäger Direkt - OPUS",
            model="OPUS Bridge UP-eSchalter 1 Kanal V1",
            name="Test Bridge",
            room="",
            address_raw="nicht angelernt",
        ),
    ]
    devices, _ = convert_rows_to_devices(rows, db)
    assert devices, "Scaffold gab kein Device zurueck"
    d = devices[0]
    # 1 Aktor + 2 PTM-Wippen = 3 Channels
    assert len(d.channels) == 3, (
        f"erwartet 3 Channels (1 Aktor + 2 PTM), bekam {len(d.channels)}: "
        f"{[c.name for c in d.channels]}"
    )
    actor_channels = [c for c in d.channels if c.eep == "D2-01-01"]
    ptm_channels = [c for c in d.channels if c.eep == "F6-02-01"]
    assert len(actor_channels) == 1, f"Actors: {[(c.name, c.eep) for c in d.channels]}"
    assert len(ptm_channels) == 2
    for ptm in ptm_channels:
        assert ptm.enocean_id is None
        assert ptm.direction == "rx"
        assert ptm.meta.get("is_bundled_ptm") is True


def test_bundled_ptm_2_kanal_with_extra_subchannels():
    """
    M79e: OPUS Bridge UP-eSchalter 2 Kanal V2 kann >4 Sub-Channels haben
    (z.B. "1.1", "1.2", "3.1 Ungenutzt", "3.2 Foo", "3.3 PTM Funktaster").
    Wir muessen exakt 4 Channels erzeugen (2 Aktor + 2 PTM) — ueberzaehlige
    Zeilen werden in vorhandene Slots gemerged oder verworfen.
    """
    db = ProductDB.load_default()
    rows = [
        CsvRow(
            is_group_header=True,
            manufacturer="Jäger Direkt - OPUS",
            model="OPUS Bridge UP-eSchalter 2 Kanal V2",
        ),
        # Header-Stub ohne IDs
        CsvRow(
            is_data=True,
            manufacturer="Jäger Direkt - OPUS",
            model="OPUS Bridge UP-eSchalter 2 Kanal V2",
            name="Test Bridge 2-Kanal",
            room="Test",
            address_raw="nicht angelernt",
        ),
        # Sub-Channels mit eigenen IDs (Format: tXXXXXXXX)
        CsvRow(
            is_data=True, is_channel=True, channel_number="1.1",
            manufacturer="Jäger Direkt - OPUS",
            model="OPUS Bridge UP-eSchalter 2 Kanal V2",
            name="1.1 Schaltaktor 0",
            room="Test", address_raw="0101A03002h/t01A03002",
        ),
        CsvRow(
            is_data=True, is_channel=True, channel_number="1.2",
            manufacturer="Jäger Direkt - OPUS",
            model="OPUS Bridge UP-eSchalter 2 Kanal V2",
            name="1.2 Schaltaktor 1",
            room="Test", address_raw="0101A03003h/t01A03003",
        ),
        # Ueberzaehliger "Ungenutzt"-Slot ohne ID → muss verworfen werden
        CsvRow(
            is_data=True, is_channel=True, channel_number="3.1",
            manufacturer="Jäger Direkt - OPUS",
            model="OPUS Bridge UP-eSchalter 2 Kanal V2",
            name="3.1 Ungenutzt",
            room="Test", address_raw="nicht angelernt",
        ),
        # PTM Wippen → IDs landen in PTM-Slots
        CsvRow(
            is_data=True, is_channel=True, channel_number="3.2",
            manufacturer="Jäger Direkt - OPUS",
            model="OPUS Bridge UP-eSchalter 2 Kanal V2",
            name="3.2 PTM Wippe links",
            room="Test", address_raw="01019D0002h/t019D0002",
        ),
        CsvRow(
            is_data=True, is_channel=True, channel_number="3.3",
            manufacturer="Jäger Direkt - OPUS",
            model="OPUS Bridge UP-eSchalter 2 Kanal V2",
            name="3.3 PTM Wippe rechts",
            room="Test", address_raw="01019D0002h/t019D0002",
        ),
    ]
    devices, _ = convert_rows_to_devices(rows, db)
    assert devices
    d = devices[0]
    assert len(d.channels) == 4, (
        f"erwartet 4 Channels (2 Aktor + 2 PTM), bekam {len(d.channels)}: "
        f"{[c.name for c in d.channels]}"
    )
    actors = [c for c in d.channels if c.eep == "D2-01-01"]
    ptms = [c for c in d.channels if c.eep == "F6-02-01"]
    assert len(actors) == 2, f"Actors: {[(c.name, c.eep) for c in d.channels]}"
    assert len(ptms) == 2
    actor_ids = sorted([c.enocean_id for c in actors if c.enocean_id])
    assert actor_ids == ["01A03002", "01A03003"]
    ptm_ids = [c.enocean_id for c in ptms]
    assert ptm_ids == ["019D0002", "019D0002"]
    assert all(c.name != "3.1 Ungenutzt" for c in d.channels)
