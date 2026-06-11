"""
Sender-ID ↔ Gateway-Routing (M60).

EnOcean-Funkmodule (TCM310, FAM14, etc.) haben eine Base-ID die durch 128
(0x80) teilbar ist und koennen damit 128 aufeinanderfolgende Sender-IDs
verwenden (Base + 0..127). Eine Sender-ID gehoert eindeutig zu genau einem
solchen Block — wenn wir die Base-IDs unserer Gateways kennen, koennen wir
also automatisch erschliessen, ueber welches Gateway eine bestimmte ID
gesendet werden MUSS, ohne dass der User es explizit angibt.

Diese Information ist auch dann verfuegbar wenn das Gateway aktuell
deaktiviert ist (z.B. weil ein anderes System es waehrend der Migration noch belegt).
"""
from __future__ import annotations

from typing import Iterable


_BLOCK_MASK = 0xFFFFFF80  # alle Bits ausser den unteren 7 — der 128er-Block


def _parse_hex_id(hex_id: str | None) -> int | None:
    """8-Hex-String → 32-bit int. None / ungueltig → None."""
    if not hex_id:
        return None
    try:
        v = int(hex_id, 16)
    except (TypeError, ValueError):
        return None
    if v < 0 or v > 0xFFFFFFFF:
        return None
    return v


def gateway_block_contains(base_id_hex: str, sender_id_hex: str) -> bool:
    """
    True, wenn die Sender-ID im 128er-Block ab base_id_hex liegt.

    Beispiel: base FF800080, sender FF8000A3 → True (gleicher Block).
              base FF800080, sender FF800100 → False (anderer Block).
    """
    base = _parse_hex_id(base_id_hex)
    sid = _parse_hex_id(sender_id_hex)
    if base is None or sid is None:
        return False
    return (base & _BLOCK_MASK) == (sid & _BLOCK_MASK)


def find_gateway_for_sender(sender_id_hex: str, gateways: Iterable) -> str | None:
    """
    Liefert den Namen des Gateways dessen Base-ID-Block die Sender-ID enthaelt.
    `gateways` ist iterable ueber Objekte mit `name` + `base_id`.
    Beruecksichtigt auch deaktivierte Gateways (relevant beim Cut-over).
    Bei mehreren Treffern wird der erste genommen.
    """
    if not sender_id_hex:
        return None
    for g in gateways:
        base = getattr(g, "base_id", None)
        if not base:
            continue
        if gateway_block_contains(base, sender_id_hex):
            return getattr(g, "name", None)
    return None


def block_range(base_id_hex: str) -> tuple[int, int] | None:
    """
    (low, high) als (Base, Base+127), beide inklusive. None wenn base ungueltig.
    """
    base = _parse_hex_id(base_id_hex)
    if base is None:
        return None
    block_start = base & _BLOCK_MASK
    return block_start, block_start + 0x7F


def bcd_to_hex(decimal_value: int) -> int:
    """
    BCD-Kodierung (Binary-Coded Decimal): jede Dezimalziffer wird in einen
    4-Bit-Nibble geschrieben (M72). Eltako verwendet das in den Sender-IDs
    der FAM14-Bus-Module:

        Adresse  2 → 0x02   (PCT14-Adr 2)
        Adresse  9 → 0x09
        Adresse 10 → 0x10   (NICHT 0x0A — die "10" wird literal hingeschrieben!)
        Adresse 17 → 0x17
        Adresse 26 → 0x26
        Adresse 99 → 0x99   (Maximum bei 2-Digit-BCD)

    Returns: BCD-kodierter Hex-Wert. Fuer Werte > 99 (3-Digit BCD) wird das
    bis 0xFF unterstuetzt; ueber 99 gibt es bei Eltako-FAM14 ohnehin nicht.
    """
    if decimal_value < 0:
        raise ValueError(f"BCD nicht definiert fuer negative Werte: {decimal_value}")
    if decimal_value > 99:
        raise ValueError(
            f"BCD-2-Digit-Limit: Wert {decimal_value} > 99 nicht unterstuetzt"
        )
    tens = decimal_value // 10
    ones = decimal_value % 10
    return (tens << 4) | ones


def fam14_address_to_sender_id(
    fam14_base_hex: str,
    bus_address: int,
    addressing: str = "hex",
) -> str | None:
    """
    Berechnet die Eltako-Sender-ID eines FAM14-Bus-Moduls aus Base-ID und
    PCT14-Bus-Adresse (M72, M77).

    Eltako-FAM14 verwendet einheitlich HEX-direct (Adresse als Hex direkt
    ins letzte Byte). Live verifiziert mit 5 verschiedenen Modul-Typen:

      Bei FAM14-Base FF800080:
        FDG14    Adr 10 → 0x0A → FF80008A   (LED Handlaeufe)
        DSZ14DRS Adr 26 → 0x1A → FF80009A   (Privatzaehler)
        FWZ14    Adr 27 → 0x1B → FF80009B
        FSR14    Adr 28 → 0x1C → FF80009C   (Pool-Pumpe)
        FSR14    Adr 51 → 0x33 → FF8000B3   (Couch)
        FSR14    Adr 127 → 0x7F → FF8000FF  (max im Block)

    addressing="bcd" steht als Library-Option zur Verfuegung falls andere
    Hersteller das nutzen — Default ist "hex" (Eltako-FAM14).

    M76 BCD-Hypothese fuer Eltako wurde mit M77 widerlegt (Live-Log).

    Returns: 8-Hex-Sender-ID (uppercase). None wenn Base ungueltig oder
    Adresse außerhalb [1, 127] (hex) bzw. [1, 99] (bcd).
    """
    base = _parse_hex_id(fam14_base_hex)
    if base is None:
        return None
    mode = (addressing or "bcd").lower()
    if mode == "hex":
        if not (1 <= bus_address <= 127):
            return None
        offset = bus_address & 0x7F
    else:
        # Default = "bcd"
        if not (1 <= bus_address <= 99):
            return None
        offset = bcd_to_hex(bus_address)
    # FAM14-Base hat bit7=1 (z.B. 0x80) im letzten Byte und vergibt Adressen
    # 0..127. BCD 1-99 und Hex 1-127 liegen komplett in dem Block.
    sender_int = (base & 0xFFFFFF80) | offset
    return f"{sender_int:08X}"


FTS14EM_GROUPS = (1, 101, 201, 301, 401)
FTS14EM_SUBDIAL_POSITIONS = (0, 10, 20, 30, 40, 50, 60, 70, 80, 90)
# Datenbyte pro Eingang im UT-Modus (Wippen-Codierung F6-02-01).
# Muster wiederholt sich alle 4 Eingaenge: 70/50/30/10 + 70/50/30/10 + 70/50.
_FTS14EM_DATA_BYTES_UT = (0x70, 0x50, 0x30, 0x10) * 3  # 12 Werte, wir nutzen [0:10]


def fts14em_sender_id(
    group: int,
    subdial_pos: int,
    input_nr: int,
    mode: str = "UT",
) -> str | None:
    """
    Berechnet die Sender-ID eines FTS14EM-Eingangs aus der Drehschalter-
    Konfiguration. Eltako nutzt "Quasi-Dezimal": die Hex-Stellen werden wie
    Dezimalziffern gelesen, deshalb wird (group + subdial_pos + input_nr)
    als dezimaler String gebildet und dann als Hex interpretiert.

    Datenblatt FTS14EM 30 014 060-1:
      Unterer Drehschalter:  Gruppe 1 | 101 | 201 | 301 | 401  + Modus UT/RT
      Oberer Drehschalter:   0 | 10 | 20 | 30 | 40 | 50 | 60 | 70 | 80 | 90

    Quasi-Dezimal-Formel:
      quasi_dec = 1000 + group + subdial_pos + (input_nr - 1)
    Die so gebildete Dezimalzahl wird als Hex-Ziffernfolge interpretiert
    (jede Dezimalstelle landet 1:1 in einem Hex-Nibble, weil alle Stellen
    immer 0..9 sind — daher "Quasi-Dezimal").

    UT-Modus (input_nr 1..10):
      G=1,   S=0,   E=1  -> 1001 -> 0x00001001  (rechts oben)
      G=1,   S=0,   E=10 -> 1010 -> 0x00001010  (Quasi-Dezimal: nach 1009 -> 1010)
      G=1,   S=40,  E=1  -> 1041 -> 0x00001041
      G=101, S=0,   E=5  -> 1105 -> 0x00001105
      G=1,   S=90,  E=10 -> 1100 -> 0x00001100  (Gruppe 1 max, Taster 100)
      G=401, S=90,  E=10 -> 1500 -> 0x00001500  (Bus-Maximum, Taster 500)

    RT-Modus (input_nr in 2,4,6,8,10 — gepaart E1+E2, E3+E4, ...):
      G=1, S=0, E=2  -> 1002 -> 0x00001002  (Wippe rechts E1/E2)
      G=1, S=0, E=10 -> 1010 -> 0x00001010

    Returns: 8-Hex-Sender-ID (uppercase) oder None bei ungueltiger Eingabe.
    """
    if group not in FTS14EM_GROUPS:
        return None
    if subdial_pos not in FTS14EM_SUBDIAL_POSITIONS:
        return None
    m = (mode or "").upper()
    if m == "UT":
        if not (1 <= input_nr <= 10):
            return None
    elif m == "RT":
        if input_nr not in (2, 4, 6, 8, 10):
            return None
    else:
        return None
    quasi_dec_str = str(1000 + group + subdial_pos + (input_nr - 1))
    return f"{int(quasi_dec_str, 16):08X}"


def fts14em_data_byte(input_nr: int, mode: str = "UT") -> int | None:
    """
    Liefert das RPS-Datenbyte pro Eingang. Wippen-Codierung F6-02-01:
      0x70 = rechts oben, 0x50 = rechts unten, 0x30 = links oben, 0x10 = links unten.

    UT (E1..E10): Muster 70/50/30/10 wiederholt sich.
    RT (gepaart): das Datenbyte ist das des zweiten Eingangs des Paares —
      also fuer (E1+E2) -> 0x50 (rechts unten), (E3+E4) -> 0x10 (links unten),
      etc. Eltako-Datenblatt-Notation "70/50" bezieht sich auf das
      Wippen-Paar (oben/unten der gleichen Seite).
    """
    m = (mode or "").upper()
    if m == "UT":
        if not (1 <= input_nr <= 10):
            return None
        return _FTS14EM_DATA_BYTES_UT[input_nr - 1]
    if m == "RT":
        if input_nr not in (2, 4, 6, 8, 10):
            return None
        # RT-Paar: input_nr ist der gerade Eingang (E2/E4/...). Datenbyte
        # entspricht dem "unten" der jeweiligen Wippenseite.
        return _FTS14EM_DATA_BYTES_UT[input_nr - 1]
    return None


def used_and_free_ids(
    base_id_hex: str,
    used_sender_ids: Iterable[str],
) -> tuple[list[str], list[str]]:
    """
    Liefert (used_sorted, free_sorted) jeweils als 8-Hex-Strings (uppercase)
    fuer einen Gateway-Block. used_sender_ids: Iterable bekannter Sender-IDs
    in beliebigem Format — gefiltert auf den Block dieses Gateways.

    Beispiel:
      base FF810000, used = ["FF810055", "FF810022", "FF8000A3"]
      → used = ["FF810022", "FF810055"]  (nur die im Block, sortiert)
      → free = ["FF810000", "FF810001", ..., "FF81007F"] minus used
    """
    rng = block_range(base_id_hex)
    if rng is None:
        return [], []
    low, high = rng
    used_set = set()
    for raw in used_sender_ids:
        v = _parse_hex_id(raw)
        if v is None:
            continue
        if low <= v <= high:
            used_set.add(v)
    used_sorted = sorted(used_set)
    all_in_block = set(range(low, high + 1))
    free_sorted = sorted(all_in_block - used_set)
    return (
        [f"{x:08X}" for x in used_sorted],
        [f"{x:08X}" for x in free_sorted],
    )
