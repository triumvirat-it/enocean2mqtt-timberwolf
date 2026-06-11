"""
Device-Scaffold-Engine.

Wandelt eine abstrakte Geraete-Beschreibung (Hersteller + Modell + Raum +
Adresse, als `CsvRow`-Zeilen) in fertige `Device`/`DeviceChannel`-Objekte um —
inklusive EEP-Lookup ueber die Produkt-DB, Multi-Channel-Splits, FAM14-Bus-
Adressen und Slot-Fill-Logik.

Wird von der Web-UI beim manuellen Anlegen eines Geraets aus dem Produktkatalog
genutzt (`_scaffold_device_channels` in app/webui/server.py).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .devices import Device, DeviceChannel
from .eep import get_profile_registry
from .products import ProductDB, ProductInfo

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CSV-Parsing
# ---------------------------------------------------------------------------


@dataclass
class CsvRow:
    """Eine abstrakte Geraete-/Kanal-Zeile (Eingabe fuer das Scaffold)."""

    # Aus Gruppen-Header geerbt:
    manufacturer: str = ""
    model: str = ""

    # Aus Geraete-Zeile:
    room: str = ""
    name: str = ""
    address_raw: str = ""

    # Abgeleitet:
    is_group_header: bool = False
    is_data: bool = False
    is_channel: bool = False  # z.B. "2.1 Schaltaktor 0"
    channel_number: str = ""  # "2.1"


_CHANNEL_RE = re.compile(r"^(\d+\.\d+)\s+(.*)$")


# ---------------------------------------------------------------------------
# Adress-Extraktion
# ---------------------------------------------------------------------------


# Adress-Schreibweisen:
#   "0505090001h/t05 09 00 01/t05090001"            - eine ID (Type-Byte+4Byte-ID)
#   "55h/A/tFF A5 8C 55/tFF810055 / 04220001h/..."  - zwei IDs (Aktor + PTM-Sender)
#   "nicht angelernt"                                - kein ID

# t<8Hex> ohne weiterem Hex danach: "t05090001/" oder "t05090001" am Ende
_T_PREFIX_ID_RE = re.compile(r"t([0-9A-Fa-f]{8})(?![0-9A-Fa-f])")
# 10-Hex+h: "0505090001h" -> letzte 8 sind die ID
_HEX10_H_RE = re.compile(r"([0-9A-Fa-f]{10})h")
# 8-Hex+h mit klarer Wortgrenze davor (kein Hex davor, kein "t" das wir oben schon haben)
_HEX8_H_RE = re.compile(r"(?<![0-9A-Fa-fth])([0-9A-Fa-f]{8})h")


def _resolve_rx_tx(ids: list[str]) -> tuple[str | None, str | None]:
    """
    Trennt Rx-Aktor-ID und Tx-PTM-ID nach Eltako-Konvention.

    Bei 2 IDs in der Adress-Spalte ist die Reihenfolge:
      - ERSTE ID  = Tx-PTM (die "schaltende" Adresse, im Aktor angelernt;
                    damit sendet die Zentrale Schaltbefehle an den Aktor)
      - ZWEITE ID = Rx-Aktor (der Aktor selbst, von dieser ID kommen
                    Feedback-Telegramme zurueck)

    Bei 1 ID handelt es sich um einen reinen Sensor (FWZ14, FT55, etc.) —
    diese ID ist die enocean_id und es gibt keine Tx-PTM.

    Returns: (rx_id, tx_id) — also (enocean_id, learned_pair_id)
    """
    if not ids:
        return None, None
    if len(ids) == 1:
        return ids[0], None
    return ids[1], ids[0]


def extract_enocean_ids(address_raw: str) -> list[str]:
    """
    Extrahiert alle 8-stelligen EnOcean-Sender-IDs aus dem Adress-Feld.

    Das Adressformat hat mehrere Schreibweisen pro Adresse — wir nehmen
    alle eindeutigen IDs in Reihenfolge ihres ersten Vorkommens.

    Returns: Liste von IDs als hex-uppercase. Leer wenn nicht angelernt.
    """
    if "nicht angelernt" in address_raw.lower():
        return []
    # Leerzeichen entfernen (Bytes mit Leerzeichen kollabieren)
    s = address_raw.replace(" ", "")

    seen: set[str] = set()
    out: list[str] = []

    def _add(hex_id: str) -> None:
        u = hex_id.upper()
        if u not in seen:
            seen.add(u)
            out.append(u)

    # 1) "tXXXXXXXX" Variante (am haeufigsten, eindeutig)
    for m in _T_PREFIX_ID_RE.finditer(s):
        _add(m.group(1))
    # 2) "XXXXXXXXXXh" 10-stellig (Type-Byte + 4-Byte-ID) -> letzte 8
    for m in _HEX10_H_RE.finditer(s):
        _add(m.group(1)[-8:])
    # 3) "XXXXXXXXh" 8-stellig direkt (ohne t-Prefix)
    for m in _HEX8_H_RE.finditer(s):
        _add(m.group(1))

    return out


# EEP-Hinweis im Namen wie "(EEP F60502)" -> "F6-05-02"
_INLINE_EEP_RE = re.compile(r"\(EEP\s+([0-9A-Fa-f]{6})\)")


def extract_inline_eep(text: str) -> str | None:
    """Findet EEP-Hinweis wie '(EEP F60502)' im Geraete-Namen."""
    m = _INLINE_EEP_RE.search(text)
    if not m:
        return None
    hex6 = m.group(1).upper()
    return f"{hex6[0:2]}-{hex6[2:4]}-{hex6[4:6]}"


# ---------------------------------------------------------------------------
# Konvertierung CSV-Rows -> Device-Liste
# ---------------------------------------------------------------------------


_SLUG_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def make_slug(s: str) -> str:
    out = (
        s.lower()
        .replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
        .replace("&", "und")
    )
    out = _SLUG_NON_ALNUM_RE.sub("_", out).strip("_")
    return out or "geraet"


def room_to_tags(room: str) -> list[str]:
    """
    Splittet einen hierarchischen Raum-Pfad wie 'Beispielwohnung/Wohnzimmer'
    in mehrere Tags ['Beispielwohnung', 'Wohnzimmer'].

    Konvention: Raum-Pfade sind '/'-getrennt; jedes
    Segment wird ein eigener Tag.
    """
    if not room:
        return []
    return [seg.strip() for seg in room.split("/") if seg.strip()]


def convert_rows_to_devices(
    rows: list[CsvRow],
    product_db: ProductDB | None = None,
) -> tuple[list[Device], dict[str, int]]:
    """
    Wandelt die geparsten CSV-Zeilen in Device-Objekte um.

    Multi-Channel-Splits werden ueber EEPProfileRegistry abgeleitet:
    jedes Feld mit is_topic_split=True bekommt einen eigenen Channel.

    Returns: (devices, stats)
    """
    stats: dict[str, int] = {
        "csv_rows": 0,
        "devices_created": 0,
        "channels_created": 0,
        "with_id": 0,
        "without_id": 0,
        "eep_from_csv": 0,
        "eep_from_db": 0,
        "no_eep": 0,
    }

    profile_reg = get_profile_registry()

    devices: list[Device] = []
    used_ids: set[str] = set()  # device_id slugs

    NON_ENOCEAN_MFR = {
        "Philips",
        "WorldWeatherOnline",
        "Cameras",
        "Generic-Camera",
    }
    stats["skipped_non_enocean"] = 0

    # Wir gruppieren Channel-Zeilen (z.B. "2.1 Foo", "2.2 Bar") unter dem
    # vorherigen "Header-Geraet" (z.B. "F3Z14D").
    # WICHTIG: current_parent muss bei jedem neuen Group-Header zurueckgesetzt
    # werden, damit die Channels des NAECHSTEN Geraets nicht ans VORHERIGE
    # Geraet angehaengt werden (Bug bei FDG14, F3Z14D ohne Stub-Row).
    current_parent: Device | None = None
    last_group: CsvRow | None = None  # zuletzt gesehener Group-Header

    for row in rows:
        if row.is_group_header:
            current_parent = None
            last_group = row
            continue
        if not row.is_data:
            continue
        if row.manufacturer in NON_ENOCEAN_MFR:
            stats["skipped_non_enocean"] += 1
            continue
        stats["csv_rows"] += 1

        ids = extract_enocean_ids(row.address_raw)
        # Bei Eltako-Aktoren (FUD/FSR/FSB/FLD) hat die CSV zwei IDs in der
        # Reihenfolge "Tx-PTM / Rx-Aktor". Wir trennen das so dass enocean_id
        # immer die Rx-Aktor-ID ist (für Feedback) und learned_pair_id die
        # Tx-PTM-ID (mit der die Zentrale sendet).
        rx_id, tx_id = _resolve_rx_tx(ids)
        if ids:
            stats["with_id"] += 1
        else:
            stats["without_id"] += 1

        # Inline-EEP aus dem Namen
        eep_inline = extract_inline_eep(row.name)
        # DB-Lookup (Produkt-Katalog)
        info = product_db.lookup(row.manufacturer, row.model) if product_db else None

        # CSV-Import-Skip: Modelle die als reine RX-Aktoren ohne Status-
        # Telegramme markiert sind (z.B. Thermokon SRC-DO8) komplett
        # ueberspringen — sonst entstehen leere Channels.
        if info and info.csv_import == "skip":
            stats.setdefault("skipped_rx_only", 0)
            stats["skipped_rx_only"] += 1
            # current_parent NICHT zuruecksetzen — der naechste Group-Header
            # macht das ohnehin. Aber wenn weitere Channel-Zeilen folgen,
            # gehoeren sie nicht zu einem ueberspringenen Device.
            current_parent = None
            continue

        eep = eep_inline or (info.eep if info else None)

        if eep_inline:
            stats["eep_from_csv"] += 1
        elif info and info.eep:
            stats["eep_from_db"] += 1
        else:
            stats["no_eep"] += 1

        # Profile-Lookup: erst EEP, dann Modellname-Fallback (FWS61 etc.)
        profile = profile_reg.get(eep) or profile_reg.match_by_model(row.model)
        if profile and not eep:
            # Modellname-Match — EEP nachtragen
            eep = profile.eep_id

        split_fields = profile.topic_split_fields() if profile else []
        # Whitelist-Filter: products.yaml kann fuer ein konkretes Modell die
        # EEP-Felder auf eine Untermenge einschraenken (z.B. RTS55 ohne SP
        # nutzt nur temperature_c, obwohl A5-10-03 auch setpoint_offset_c
        # hat). Leere Liste = keine Einschraenkung.
        if info and info.included_fields:
            inc = set(info.included_fields)
            split_fields = [f for f in split_fields if f.name in inc]
        # is_multi_channel orientiert sich am ORIGINAL-Profile (vor Filter):
        # auch wenn included_fields auf 1 Feld reduziert, soll der Channel
        # weiterhin im Split-Modus angelegt werden (mit field-Filter).
        # Bei FDG14 (A5-38-08 mit nur dim_percent) bleibt is_multi_channel False.
        is_multi_channel = profile.needs_multi_channel() if profile else False

        base_meta_template = {
            "device_type": info.device_type if info else None,
            "device_config": info.config_str if info else None,
        }

        # Wenn die erste data-row nach einem Group-Header schon ein Channel ist
        # (also direkt "1.1 Dimmer 0" ohne vorhergehende Stub-Zeile), legen wir
        # ein "virtuelles" Device aus den Group-Header-Daten an. So bekommt der
        # FDG14 sein eigenes Device mit allen 16 Kanaelen.
        if row.is_channel and current_parent is None and last_group is not None:
            base_slug = make_slug(
                f"{last_group.manufacturer}_{last_group.model}_{row.room}"
            )
            device_id = _unique_slug(base_slug, used_ids)
            used_ids.add(device_id)

            tags = room_to_tags(row.room)
            if last_group.manufacturer:
                tags.append(last_group.manufacturer)

            device = Device(
                device_id=device_id,
                manufacturer=last_group.manufacturer,
                model=last_group.model,
                name=last_group.model or "Gerät",
                room=row.room,
                floor=tags[0] if tags else "",
                channels=[],
                notes=info.description if info and info.description else "",
            )
            devices.append(device)
            stats["devices_created"] += 1
            current_parent = device
            # Tags greifen wir uns hier auch fuer alle nachfolgenden Channels
            # (wir merken sie an base_meta_template-Erweiterung via _tags_for_group)
            base_meta_template = {**base_meta_template, "tags": tags}

        if row.is_channel and current_parent is not None:
            # M79e: Bei bundled_ptm-Produkten (OPUS Bridge u.a.) hat das Device
            # bereits eine FESTE Channel-Struktur (N Aktor + M PTM). Channel-
            # Zeilen aus CSV bringen IDs/Namen mit — die werden in die nächsten
            # leeren Slots eingetragen, KEINE neuen Channels angelegt.
            # Ueberzaehlige CSV-Channels (z.B. "Ungenutzt") werden ignoriert.
            parent_info = (
                product_db.lookup(current_parent.manufacturer, current_parent.model)
                if product_db else None
            )
            if parent_info and parent_info.bundled_ptm_count > 0:
                if not rx_id and not tx_id:
                    # leerer Channel ("Ungenutzt") → ignorieren
                    continue
                # nächsten freien Slot fuellen
                target = next(
                    (c for c in current_parent.channels if not c.enocean_id),
                    None,
                )
                if target is None:
                    continue  # alle Slots voll → überzählig ignorieren
                target.enocean_id = rx_id
                if tx_id:
                    target.learned_pair_id = tx_id
                if row.name:
                    n = _clean_name(row.name)
                    if n:
                        target.name = n
                continue
            # Normal-Pfad fuer alle anderen Multi-Channel-Geraete:
            # Multi-Channel-Geraete (z.B. F3Z14D-Phasen) splitten pro Feld
            # UND tragen ggf. den Telegramm-channel-Filter ein.
            tele_chan_field = profile.telegram_channel_field if profile else None
            # Aus row.channel_number ("3.1" -> 0, "3.2" -> 1) den 0-basierten
            # Telegramm-Sub-Channel ableiten, wenn das EEP eine
            # telegram_channel_field-Konvention hat.
            tele_channel = None
            if tele_chan_field:
                try:
                    after_dot = row.channel_number.split(".", 1)[1]
                    tele_channel = int(after_dot) - 1
                except (IndexError, ValueError):
                    tele_channel = None

            if is_multi_channel:
                for f in split_fields:
                    meta = {**base_meta_template, "field": f.name}
                    if tele_channel is not None:
                        meta["tele_channel"] = tele_channel
                    channel = DeviceChannel(
                        channel_id=f"{row.channel_number}_{f.topic_segment()}",
                        name=_clean_name(row.name) + " " + f.label,
                        enocean_id=rx_id,
                        eep=eep or "UNKNOWN",
                        direction="rx",
                        learned_pair_id=tx_id,
                        meta=meta,
                    )
                    current_parent.channels.append(channel)
                    stats["channels_created"] += 1
            else:
                meta = dict(base_meta_template)
                if tele_channel is not None:
                    meta["tele_channel"] = tele_channel
                channel = DeviceChannel(
                    channel_id=row.channel_number,
                    name=_clean_name(row.name),
                    enocean_id=rx_id,
                    eep=eep or "UNKNOWN",
                    direction="rx",
                    learned_pair_id=tx_id,
                    meta=meta,
                )
                current_parent.channels.append(channel)
                stats["channels_created"] += 1
        else:
            # Neues Geraet
            base_slug = make_slug(f"{row.manufacturer}_{row.model}_{row.name}")
            device_id = _unique_slug(base_slug, used_ids)
            used_ids.add(device_id)

            tags = room_to_tags(row.room)
            if row.manufacturer:
                tags.append(row.manufacturer)

            device = Device(
                device_id=device_id,
                manufacturer=row.manufacturer,
                model=row.model,
                name=_clean_name(row.name),
                room=row.room,
                floor=tags[0] if tags else "",
                channels=[],
                notes=info.description if info and info.description else "",
            )

            base_meta = {**base_meta_template, "tags": tags}

            # Header-Stub-Fix: wenn diese Data-Zeile zu einem Mehrkanal-Geraet
            # gehoert (also nachfolgend kommen 2.1, 2.2, ... Channel-Zeilen),
            # erkennen wir das daran, dass DIESE Zeile keine eigene Sender-ID
            # hat. Dann legen wir das Device an, aber KEINEN Channel "1" —
            # die nachfolgenden Channel-Zeilen liefern die echten Channels.
            #
            # Beispiel FSR14-2x:
            #   Group:  "1 Eltako FSR14-2x"
            #   Data:   "Pool / FSR14-2x" (keine ID)        <- Stub, skip channel
            #   Data:   "Pool / 2.1 ..." (FF810022)         <- echter Channel
            #   Data:   "Pool / 2.2 ..." (FF81006A)         <- echter Channel
            is_header_stub = (
                not ids
                and not row.is_channel
                and _looks_like_multichannel_header(rows, row)
            )

            # Vier Faelle:
            # 1. Header-Stub (FSR14-2x): nur Device, Channels kommen aus
            #    nachfolgenden X.Y-Zeilen
            # 2. N*M Sub-Tarif + Feld-Split (DSZ14DRS Doppeltarif, F3Z14D
            #    3-Phasen): 1 Sender-ID, N Sub-Zaehler per Telegramm-channel-
            #    Byte, M Felder pro Sub-Zaehler. -> N*M Channels.
            # 3. Multi-Channel-EEP (FWZ14, FWS61): 1 Sender-ID, mehrere Topics
            #    pro Feld des EEP-Profile (kein tele_channel-Split).
            # 4. Multi-Channel-Bauteil (FDG14, FUD61-16): mehrere interne
            #    Hardware-Channels mit je eigener ID, aber CSV liefert nur EINE
            #    Stammzeile. Wir nehmen channel_count aus products.yaml und
            #    erzeugen n leere Channels — User pflegt IDs nach (oder lernt an).
            product_n = info.channel_count if info else 1
            tele_chan_field = profile.telegram_channel_field if profile else None
            multi_kind = info.multi_channel_kind if info else ""

            # RX-only-Sende-Aktor (Thermokon STC-DO8 Heizregler): channel_count
            # schlichte TX-Kanaele anlegen. KEINE Feld-Aufteilung (anders als
            # bei RX-Sensoren) — jeder Kanal ist ein Heizkreis, den wir per
            # A5-10-06 besenden. TX-Sender-IDs traegt der User nach.
            if info and info.actuator_tx:
                n = info.channel_count
                tpl = info.channel_name_template
                actx_eep = eep or info.eep or "UNKNOWN"
                cid = (lambda i: "1") if n == 1 else (lambda i: f"1.{i}")
                for i in range(1, n + 1):
                    name_i = (tpl.replace("{idx0}", str(i - 1))
                                 .replace("{idx}", str(i)))
                    device.channels.append(DeviceChannel(
                        channel_id=cid(i),
                        name=name_i,
                        enocean_id=None,
                        eep=actx_eep,
                        direction="tx",
                        meta=dict(base_meta),
                    ))
                    stats["channels_created"] += 1
                devices.append(device)
                stats["devices_created"] += 1
                current_parent = device
                continue

            # M79e: Bundled-PTM-Produkte (OPUS Bridge UP-eSchalter, Jal, LED-
            # Funk-Dimmer) haben eine FESTE Channel-Struktur: N Aktor + M PTM.
            # Die Eingabe liefert oft mehrere Sub-Channel-Zeilen mit eigenen
            # Namen/IDs — diese werden in die vordefinierten Slots eingetragen,
            # ohne die Struktur zu erweitern. Ueberzaehlige CSV-Zeilen werden
            # ignoriert (z.B. "Ungenutzt"-Slots).
            if info and info.bundled_ptm_count > 0:
                actor_n = info.channel_count
                ptm_n = info.bundled_ptm_count
                actor_tpl = info.channel_name_template
                actor_eep = eep or info.eep or "UNKNOWN"
                ptm_eep_local = info.bundled_ptm_eep or "F6-02-01"
                actor_direction = "bi" if info.ui_kind != "rx" else "rx"
                total = actor_n + ptm_n
                # Channel-ID-Schema: bei N+M=1 single ("1"), sonst dotted ("1.1"...)
                cid = (lambda i: "1") if total == 1 else (lambda i: f"1.{i}")
                for i in range(1, actor_n + 1):
                    name_i = (actor_tpl.replace("{idx0}", str(i - 1))
                                       .replace("{idx}", str(i)))
                    device.channels.append(DeviceChannel(
                        channel_id=cid(i),
                        name=name_i,
                        enocean_id=None,
                        eep=actor_eep,
                        direction=actor_direction,
                        meta=dict(base_meta),
                    ))
                    stats["channels_created"] += 1
                for i in range(1, ptm_n + 1):
                    device.channels.append(DeviceChannel(
                        channel_id=cid(actor_n + i),
                        name=f"PTM Wippe {i}",
                        enocean_id=None,
                        eep=ptm_eep_local,
                        direction="rx",
                        meta={**base_meta, "is_bundled_ptm": True},
                    ))
                    stats["channels_created"] += 1
                # CSV-Header-Zeile selbst koennte IDs mitbringen — wenn ja,
                # in ersten leeren Slot eintragen (passiert nur bei nicht-stub).
                if rx_id and not is_header_stub:
                    target = device.channels[0]
                    target.enocean_id = rx_id
                    if tx_id:
                        target.learned_pair_id = tx_id
                devices.append(device)
                stats["devices_created"] += 1
                current_parent = device
                continue   # Skip alle weiteren Channel-Erzeugungs-Pfade

            if is_header_stub:
                pass  # nur Device, kein Channel jetzt
            # Felder die tarif-INdependent sind (z.B. Seriennummer) werden 1x
            # pro Device angelegt, NICHT pro Tarif verdoppelt.
            split_tariff_dep = [f for f in split_fields if not f.tariff_independent]
            split_tariff_indep = [f for f in split_fields if f.tariff_independent]

            if False:
                pass
            elif product_n > 1 and multi_kind == "separate_ids":
                # F3Z14D-Style: N separate Sub-Zaehler mit eigener Sender-ID.
                # KEINE Auto-Generation pro Phase — wir verlassen uns darauf,
                # dass die CSV-Channel-Zeilen folgen (z.B. "3.1 Flow Heiz...",
                # "3.2 Flow Elektrolyse"). Falls keine Channel-Zeilen kommen,
                # entstehen N leere Slots als Platzhalter.
                if not _looks_like_multichannel_header(rows, row):
                    # Keine Channel-Zeilen folgen -> wir legen N leere Slots an
                    tpl = info.channel_name_template if info else "Eingang {idx}"
                    for i in range(1, product_n + 1):
                        ch_name = tpl.replace("{idx0}", str(i - 1)).replace("{idx}", str(i))
                        for f in split_tariff_dep:
                            ch = DeviceChannel(
                                channel_id=f"{i}_{f.topic_segment()}",
                                name=f"{ch_name} {f.label}",
                                enocean_id=(rx_id if i == 1 else None),
                                eep=eep or "UNKNOWN",
                                direction="rx",
                                learned_pair_id=(tx_id if i == 1 else None),
                                meta={**base_meta, "field": f.name},
                            )
                            device.channels.append(ch)
                            stats["channels_created"] += 1
                    for f in split_tariff_indep:
                        ch = DeviceChannel(
                            channel_id=f.topic_segment(),
                            name=f.label,
                            enocean_id=rx_id,
                            eep=eep or "UNKNOWN",
                            direction="rx",
                            learned_pair_id=tx_id,
                            meta={**base_meta, "field": f.name},
                        )
                        device.channels.append(ch)
                        stats["channels_created"] += 1
                # else: Channel-Zeilen werden im naechsten Loop-Durchlauf
                # automatisch zu Channels — kein Auto-Gen hier.
            elif product_n > 1 and len(split_tariff_dep) > 1:
                # N*M Channels: N Hardware-Sub-Einheiten x M Felder pro EEP.
                # Nur greifen wenn beides > 1 — bei genau einem topic_split_field
                # (z.B. A5-38-08 mit dim_percent) waere N*1 = N Channels mit
                # field-Filter, das macht das Single-Channel-Verhalten zur
                # Unmoeglichkeit. Stattdessen faellt das in Fall 4 zurueck.
                #
                # Zwei Sub-Faelle:
                #
                # (a) telegram_channel_field gesetzt (A5-12-01 mit channel-Byte):
                #     EINE Sender-ID, N Sub-Einheiten per Telegramm-channel-Byte
                #     unterschieden (z.B. DSZ14DRS Doppeltarif, F3Z14D 3-Phasen).
                #     -> alle Channels haben rx_id, plus meta.tele_channel = i
                #
                # (b) kein telegram_channel_field (z.B. Thermokon SRC-DO8 mit
                #     A5-10-03): N separate Hardware-Kanaele, jeder mit eigener
                #     Sender-ID (Eltako-Base+Offset-Konvention). CSV liefert
                #     i.d.R. nur eine ID; weitere muss User nachpflegen / anlernen.
                #     -> nur Channel-Gruppe 1 hat rx_id, der Rest hat None
                tpl = info.channel_name_template if info else "Kanal {idx}"
                for i in range(product_n):
                    # ID-Vergabe: bei tele_channel-Variante alle gleich, sonst
                    # nur erste Gruppe (i==0) bekommt die importierte ID
                    if tele_chan_field:
                        ch_rx_id = rx_id
                        ch_tx_id = tx_id
                    else:
                        ch_rx_id = rx_id if i == 0 else None
                        ch_tx_id = tx_id if i == 0 else None
                    for f in split_tariff_dep:
                        ch_name = (
                            tpl.replace("{idx0}", str(i))
                               .replace("{idx}", str(i + 1))
                        )
                        meta = {**base_meta, "field": f.name}
                        if tele_chan_field:
                            meta["tele_channel"] = i
                        channel = DeviceChannel(
                            channel_id=f"{i + 1}_{f.topic_segment()}",
                            name=f"{ch_name} {f.label}",
                            enocean_id=ch_rx_id,
                            eep=eep or "UNKNOWN",
                            direction="rx",
                            learned_pair_id=ch_tx_id,
                            meta=meta,
                        )
                        device.channels.append(channel)
                        stats["channels_created"] += 1
                # Tarif-unabhaengige Felder (z.B. Seriennummer) genau 1x pro
                # Device anlegen, OHNE tele_channel-Filter.
                for f in split_tariff_indep:
                    channel = DeviceChannel(
                        channel_id=f.topic_segment(),
                        name=f.label,
                        enocean_id=rx_id,
                        eep=eep or "UNKNOWN",
                        direction="rx",
                        learned_pair_id=tx_id,
                        meta={**base_meta, "field": f.name},
                    )
                    device.channels.append(channel)
                    stats["channels_created"] += 1
            elif is_multi_channel:
                # Direkt mehrere Channels (z.B. FWZ14 sendet kWh+W, FWS61 8 Werte).
                # Hier wird KEIN tele_channel-Filter gesetzt — pipeline.py defaultet
                # dann auf 0, sodass Sub-Tarif-Telegramme (channel=1) verworfen
                # werden (z.B. DSZ14DRS-Bug-Schutz bei einfacher Konfig).
                for f in split_fields:
                    channel = DeviceChannel(
                        channel_id=f.topic_segment(),
                        name=_clean_name(row.name) + " " + f.label,
                        enocean_id=rx_id,
                        eep=eep or "UNKNOWN",
                        direction="rx",
                        learned_pair_id=tx_id,
                        meta={**base_meta, "field": f.name},
                    )
                    device.channels.append(channel)
                    stats["channels_created"] += 1
            elif product_n > 1:
                # N separate Hardware-Channels mit je eigener ID (FDG14 mit
                # A5-38-08, FUD61-16). CSV liefert nur Channel 1 mit ID — die
                # anderen N-1 Channels haben leere enocean_id und User pflegt
                # sie nach (oder lernt sie via Anlern-UI an).
                tpl = info.channel_name_template if info else "Kanal {idx}"
                direction = "bi" if (info and info.ui_kind != "rx") else "rx"
                for i in range(1, product_n + 1):
                    ch_name = (
                        tpl.replace("{idx0}", str(i - 1))
                           .replace("{idx}", str(i))
                    )
                    channel = DeviceChannel(
                        channel_id=f"1.{i}",
                        name=ch_name,
                        # Erste ID nur an Channel 1 — bei FDG14 sind die
                        # weiteren 15 Sender-IDs nicht in der CSV
                        enocean_id=(rx_id if i == 1 else None),
                        eep=eep or "UNKNOWN",
                        direction=direction,
                        learned_pair_id=(tx_id if i == 1 else None),
                        meta=dict(base_meta),
                    )
                    device.channels.append(channel)
                    stats["channels_created"] += 1
            else:
                # Single-Channel-Geraet
                channel = DeviceChannel(
                    channel_id="1",
                    name=_clean_name(row.name),
                    enocean_id=rx_id,
                    eep=eep or "UNKNOWN",
                    direction="rx",
                    learned_pair_id=tx_id,
                    meta=base_meta,
                )
                device.channels.append(channel)
                stats["channels_created"] += 1

            # M79e: Bundled-PTM wird nicht mehr hier behandelt — das Device
            # wird oben (siehe `if info and info.bundled_ptm_count > 0`)
            # bereits mit fester Struktur erzeugt und der continue dort
            # ueberspringt diesen Punkt vollstaendig.

            # Zusatz-Channels fuer Sub-Telegramm-Kanaele jenseits des Standard-
            # Tarif-Schemas. Beispiel: DSZ14DRS-3x65A sendet channel=8 als
            # Gesamt-Wirkleistung. Dies wird in products.yaml als
            # `extra_telegram_channels: [{channel: 8, label: "Gesamt"}]`
            # angegeben. Pro extra channel + topic_split_field ein eigener
            # Channel mit meta.tele_channel und meta.field.
            if not is_header_stub and info and info.extra_telegram_channels and split_fields:
                for extra in info.extra_telegram_channels:
                    try:
                        ex_tc = int(extra.get("channel"))
                    except (TypeError, ValueError):
                        continue
                    ex_label = extra.get("label") or f"Ch{ex_tc}"
                    for f in split_fields:
                        channel = DeviceChannel(
                            channel_id=f"extra_{ex_tc}_{f.topic_segment()}",
                            name=f"{ex_label} {f.label}",
                            enocean_id=rx_id,
                            eep=eep or "UNKNOWN",
                            direction="rx",
                            learned_pair_id=tx_id,
                            meta={**base_meta, "field": f.name, "tele_channel": ex_tc},
                        )
                        device.channels.append(channel)
                        stats["channels_created"] += 1

            devices.append(device)
            stats["devices_created"] += 1
            current_parent = device

    return devices, stats


_LEADING_CHANNEL_RE = re.compile(r"^\d+\.\d+\s+")
_TRAILING_EEP_PAREN_RE = re.compile(r"\s*\(EEP\s+[0-9A-Fa-f]{6}\)\s*$")


def _looks_like_multichannel_header(
    all_rows: list[CsvRow], current_row: CsvRow,
) -> bool:
    """
    True wenn current_row der Header-Stub eines Multi-Channel-Geraets ist:
    direkt danach kommen mindestens 1 Channel-Zeile (is_channel=True) BEVOR
    der naechste Group-Header oder das Ende kommt.
    """
    try:
        idx = all_rows.index(current_row)
    except ValueError:
        return False
    for r in all_rows[idx + 1:]:
        if r.is_group_header:
            return False
        if not r.is_data:
            continue
        return r.is_channel  # erste folgende Data-Zeile entscheidet
    return False


def _clean_name(name: str) -> str:
    """Saeubert Channel-Praefix '2.1 ' und EEP-Klammerhinweis aus dem Namen."""
    name = _LEADING_CHANNEL_RE.sub("", name)
    name = _TRAILING_EEP_PAREN_RE.sub("", name)
    return name.strip()


def _unique_slug(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


