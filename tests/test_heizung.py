"""
Heizungsregelung — Zeitplan, Raum-Istwerte, Sollwertrad und Zwangsabsenkung.

Aufbau laut Anforderung:

    Bereich
      ├── x Zeitzonen           (Zeitplan, gilt fuer alle Raeume)
      ├── x Kontakte            (senken ALLE Raeume ab)
      └── x Raeume
            ├── x Fuehler + Verrechnungsregel
            ├── 0..1 Sollwertrad
            ├── x Kontakte      (senken NUR diesen Raum)
            └── x Heizkreise    (je ein Ausgang des Reglers)

Die Regeln, die hier wirklich weh tun koennen und deshalb festgenagelt sind:

1. **Es darf nie ein erfundener Ist-Wert gesendet werden.** Der Heizregler kann
   eine ausgedachte Temperatur nicht von einer gemessenen unterscheiden.
2. **Ausserhalb aller Zeitzonen gilt der letzte Sollwert weiter** — auch nach
   einem Neustart, sonst ist die Regel nach jedem Deploy wirkungslos.
3. **Die Sollwertverschiebung muss mit dem Bereich kodiert werden, der im
   Aktor eingestellt ist.** Werkseinstellung des SRC-DO8 ist ±5 K, nicht ±10 K.
4. **Zyklisch senden** — nach 90 Minuten Funkstille meldet der Regler
   Sensorausfall.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime

from app.encoders import encode_a5_10_setpoint
from app.heizung import (
    Fuehler,
    Heizfenster,
    Heizkreis,
    Heizungseinstellungen,
    Heizungsregler,
    HeizungsZustand,
    Heizzone,
    HeizzonenSpeicher,
    IstwertSpeicher,
    Kontakt,
    Raum,
    Restsollwert,
    Schalter,
    Wertquelle,
    aktives_fenster,
    beurteile_zone,
    doppelte_heizkreise,
    doppelte_raum_ids,
    laeuft_seit_minuten,
    minuten_aus_zeit,
    rohwert_aus_payload,
    wert_aus_payload,
)


IST_TOPIC = "1wire/sensors/raum_b/temp"
THANOS = "enocean/raum_a/raumbedienteil/state"
VISU = "heizung/bereich1"


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------


class _FakeRouter:
    def __init__(self) -> None:
        self.befehle: list[tuple[str, str, dict]] = []

    async def handle_command(self, device_id, channel_id, command) -> None:
        self.befehle.append((device_id, channel_id, command))


class _FakePublisher:
    base = "enocean"

    def __init__(self) -> None:
        self.nachrichten: list[tuple[str, dict]] = []
        self.abos: list[str] = []

    async def publish_json(self, topic, payload, retain=None) -> None:
        self.nachrichten.append((topic, payload))

    async def subscribe_extra(self, topics) -> None:
        self.abos.extend(topics)


def _raum(**kw) -> Raum:
    daten = dict(
        id="bad",
        name="Bad",
        fuehler=[Fuehler(topic=IST_TOPIC, feld="value", name="Bad")],
        heizkreise=[Heizkreis(device_id="verteiler_ug",
                              channel_id="heizkreis_5", name="Bad")],
    )
    daten.update(kw)
    return Raum(**daten)


def _zone(**kw) -> Heizzone:
    daten = dict(
        id="bereich1",
        name="Bereich",
        raeume=[_raum()],
        basissollwert_c=21.0,
        verschiebung_max_k=5.0,
        # Die Tests rechnen mit einer gestellten Uhr; das Alter des Ist-Werts
        # wuerde sonst von der echten Laufzeit abhaengen. Die Altersgrenze hat
        # ihren eigenen Test.
        ist_max_alter_s=0,
        fenster=[Heizfenster(name="Morgens", von="06:00", bis="09:00",
                             sollwert_c=22.5)],
    )
    daten.update(kw)
    return Heizzone(**daten)


def _regler(tmp_path, zone: Heizzone, **einst):
    speicher = HeizzonenSpeicher(tmp_path / "heizzonen.yaml")
    speicher.einstellungen = Heizungseinstellungen(zonen=[zone], **einst)
    zustand = HeizungsZustand(tmp_path / "heizung_state.yaml")
    router, publisher = _FakeRouter(), _FakePublisher()
    return Heizungsregler(speicher, zustand, router, publisher), router, publisher


def _lage(zone: Heizzone, speicher: IstwertSpeicher,
          jetzt: datetime = datetime(2026, 9, 23, 7, 0),
          schalter: dict | None = None):
    return beurteile_zone(zone, jetzt, speicher,
                          HeizungsZustand("/nicht/vorhanden.yaml"),
                          schalter=schalter)


def _mit_ist(wert: float = 21.8, topic: str = IST_TOPIC,
             feld: str = "value") -> IstwertSpeicher:
    speicher = IstwertSpeicher()
    speicher.setze(topic, wert, feld=feld)
    return speicher


def _kreise(lage) -> list:
    return [k for r in lage.raeume for k in r.kreise]


# ---------------------------------------------------------------------------
# Zeitplan
# ---------------------------------------------------------------------------


def test_uhrzeit_parsen():
    assert minuten_aus_zeit("06:30") == 390
    assert minuten_aus_zeit("00:00") == 0
    assert minuten_aus_zeit("24:00") == 1440
    assert minuten_aus_zeit("25:00") is None
    assert minuten_aus_zeit("kaputt") is None


def test_zeitzone_gilt_innerhalb_ihrer_zeit():
    f = Heizfenster(name="Morgens", von="06:00", bis="09:00", sollwert_c=22.5)
    assert laeuft_seit_minuten(f, datetime(2026, 9, 23, 7, 30)) == 90
    assert laeuft_seit_minuten(f, datetime(2026, 9, 23, 5, 59)) is None
    # Das Ende ist exklusiv — sonst ueberlappen anschliessende Zeitzonen.
    assert laeuft_seit_minuten(f, datetime(2026, 9, 23, 9, 0)) is None


def test_zeitzone_ueber_mitternacht():
    f = Heizfenster(name="Nacht", von="22:00", bis="06:00", sollwert_c=18.0)
    assert laeuft_seit_minuten(f, datetime(2026, 9, 23, 23, 0)) == 60
    assert laeuft_seit_minuten(f, datetime(2026, 9, 24, 2, 0)) == 240
    assert laeuft_seit_minuten(f, datetime(2026, 9, 24, 7, 0)) is None


def test_zeitzone_nur_an_bestimmten_tagen():
    # 23.09.2026 ist ein Mittwoch, 26.09.2026 ein Samstag.
    f = Heizfenster(name="Werktags", von="06:00", bis="09:00",
                    tage=["mo", "di", "mi", "do", "fr"], sollwert_c=22.5)
    assert laeuft_seit_minuten(f, datetime(2026, 9, 23, 7, 0)) is not None
    assert laeuft_seit_minuten(f, datetime(2026, 9, 26, 7, 0)) is None


def test_ueberlappende_zeitzonen_die_zuletzt_gestartete_gewinnt():
    zone = _zone(fenster=[
        Heizfenster(name="Grundwaerme", von="06:00", bis="22:00", sollwert_c=21.0),
        Heizfenster(name="Duschzeit", von="07:00", bis="08:00", sollwert_c=24.0),
    ])
    f = aktives_fenster(zone, datetime(2026, 9, 23, 7, 30))
    assert f is not None and f.name == "Duschzeit"
    f = aktives_fenster(zone, datetime(2026, 9, 23, 9, 0))
    assert f is not None and f.name == "Grundwaerme"


# ---------------------------------------------------------------------------
# Werte aus MQTT
# ---------------------------------------------------------------------------


def test_istwert_aus_verschiedenen_payloads():
    assert wert_aus_payload("21.4", "value") == 21.4
    assert wert_aus_payload("21,4", "value") == 21.4
    assert wert_aus_payload('{"value": 21.4}', "value") == 21.4
    assert wert_aus_payload('{"temperature_c": 22.0}', "temperature_c") == 22.0
    assert wert_aus_payload('{"sensor": {"temp": 19.5}}', "sensor.temp") == 19.5
    assert wert_aus_payload('{"t": 20.1}', "") == 20.1


def test_istwert_verweigert_was_kein_messwert_ist():
    """Kein Wert ist besser als ein falscher — dieselbe Lehre wie beim Zaehler."""
    assert wert_aus_payload('{"value": "n/a"}', "value") is None
    assert wert_aus_payload('{"value": true}', "value") is None
    assert wert_aus_payload('{"andere": 21.0}', "value") is None
    assert wert_aus_payload("", "value") is None
    assert wert_aus_payload('{"a": 1, "b": 2}', "") is None


# ---------------------------------------------------------------------------
# Sollwert-Rechnung
# ---------------------------------------------------------------------------


def test_verschiebung_aus_sollwert_und_basis():
    lage = _lage(_zone(), _mit_ist())
    assert lage.sendbar is True
    assert lage.sollwert_c == 22.5
    assert _kreise(lage)[0].verschiebung_k == 1.5     # 22,5 - 21,0
    assert _kreise(lage)[0].verschiebung_begrenzt is False


def test_raum_korrektur_schlaegt_auf_den_sollwert_auf():
    zone = _zone(raeume=[_raum(sollwert_offset_k=-0.5)])
    lage = _lage(zone, _mit_ist())
    assert _kreise(lage)[0].verschiebung_k == 1.0     # (22,5 - 0,5) - 21,0


def test_Bereichs_korrektur_gilt_fuer_alle_raeume():
    zone = _zone(sollwert_offset_k=0.5)
    lage = _lage(zone, _mit_ist())
    assert _kreise(lage)[0].verschiebung_k == 2.0     # (22,5 + 0,5) - 21,0


def test_verschiebung_wird_auf_den_aktor_bereich_begrenzt():
    """
    Der Aktor kann nur ±5 K um die Basis. Ein Sollwert von 30 °C waere +9 K —
    wir senden die Grenze und sagen es, statt still etwas anderes zu regeln.
    """
    zone = _zone(fenster=[Heizfenster(name="Heiss", von="00:00", bis="24:00",
                                      sollwert_c=30.0)])
    lage = _lage(zone, _mit_ist(21.0))
    kreis = _kreise(lage)[0]
    assert kreis.verschiebung_k == 5.0
    assert kreis.verschiebung_begrenzt is True
    assert "ausserhalb" in kreis.hinweis


# ---------------------------------------------------------------------------
# Die Ist-Wert-Pflicht
# ---------------------------------------------------------------------------


def test_ohne_istwert_wird_nicht_gesendet():
    lage = _lage(_zone(), IstwertSpeicher())
    assert lage.sendbar is False
    assert lage.raeume[0].status == "kein_istwert"


def test_ohne_fuehler_wird_nicht_gesendet():
    lage = _lage(_zone(raeume=[_raum(fuehler=[])]), IstwertSpeicher())
    assert lage.sendbar is False
    assert lage.raeume[0].status == "kein_fuehler"


def test_veralteter_istwert_wird_nicht_gesendet():
    zone = _zone(ist_max_alter_s=900)
    jetzt = datetime(2026, 9, 23, 7, 0)
    speicher = IstwertSpeicher()
    speicher.setze(IST_TOPIC, 21.8, ts=jetzt.timestamp() - 1800, feld="value")
    lage = _lage(zone, speicher, jetzt)
    assert lage.sendbar is False
    assert lage.raeume[0].status == "istwert_alt"
    assert "90 Minuten" in lage.raeume[0].hinweis   # der Sensorausfall


def test_unplausibler_istwert_wird_wie_ein_fehlender_behandelt():
    """
    Regression aus der Anlage: Ein 1-Wire-Sammeltopic
    meldete fuer zwei Raeume 2153,12 und
    2193,75 — eine fehlende Kommastelle in der Quelle (/100 = 21,5 bzw. 21,9).
    Ohne Grenze haette die Zone das als Raumtemperatur genommen, die
    Verschiebung auf -5 K gezogen und den Heizkreis zugedreht.
    """
    lage = _lage(_zone(ist_min_c=0.0, ist_max_c=40.0), _mit_ist(2153.12))
    assert lage.sendbar is False
    assert lage.raeume[0].status == "istwert_unplausibel"
    assert lage.raeume[0].ist_c is None
    assert "2153.12" in lage.raeume[0].hinweis


def test_echte_raumtemperaturen_gehen_durch():
    """Die Werte, die in der Anlage tatsaechlich anliegen."""
    for wert in (22.4375, 22.4688, 23.0, 22.25):
        lage = _lage(_zone(), _mit_ist(wert))
        assert lage.sendbar is True, wert
        assert lage.raeume[0].ist_c == wert


def test_regler_sendet_nichts_ohne_istwert(tmp_path):
    regler, router, _ = _regler(tmp_path, _zone())
    asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 7, 0)))
    assert router.befehle == []


# ---------------------------------------------------------------------------
# "Letzter Wert bleibt stehen"
# ---------------------------------------------------------------------------


def test_ausserhalb_der_zeitzonen_gilt_der_letzte_sollwert(tmp_path):
    zone = _zone()
    morgens = datetime(2026, 9, 23, 7, 0)
    mittags = datetime(2026, 9, 23, 12, 0)
    speicher = _mit_ist(21.8)
    zustand = HeizungsZustand(tmp_path / "heizung_state.yaml")

    lage = beurteile_zone(zone, morgens, speicher, zustand)
    zustand.merke(zone.id, lage.sollwert_c, "Morgens")
    assert lage.sollwert_quelle == "fenster"

    lage = beurteile_zone(zone, mittags, speicher, zustand)
    assert lage.sollwert_c == 22.5
    assert lage.sollwert_quelle == "letzter_wert"
    assert lage.sendbar is True


def test_ohne_zeitzone_und_ohne_vorgeschichte_wird_nicht_geraten():
    lage = _lage(_zone(fenster=[]), _mit_ist(),
                 datetime(2026, 9, 23, 12, 0))
    assert lage.sendbar is False
    assert lage.raeume[0].status == "kein_sollwert"


def test_letzter_sollwert_ueberlebt_den_neustart(tmp_path):
    """
    Genau die Wurzel des Zaehler-Vorfalls vom 02.09.2026, hier vorbeugend:
    laege der letzte Sollwert nur im Arbeitsspeicher, waere die Regel
    "ausserhalb der Zeitzonen gilt der letzte Wert" nach jedem Deploy weg.
    """
    pfad = tmp_path / "heizung_state.yaml"
    alt = HeizungsZustand(pfad)
    alt.merke("bereich1", 22.5, "Morgens")
    alt.save()

    neu = HeizungsZustand(pfad)
    neu.load()
    assert neu.sollwert("bereich1") == 22.5
    assert neu.quelle("bereich1") == "Morgens"


def test_kaputter_zustand_blockiert_den_start_nicht(tmp_path):
    pfad = tmp_path / "heizung_state.yaml"
    pfad.write_text("das ist: [kein gueltiges yaml", encoding="utf-8")
    zustand = HeizungsZustand(pfad)
    zustand.load()                      # darf nicht werfen
    assert zustand.sollwert("bereich1") is None


def test_kaputte_zonendatei_blockiert_den_start_nicht(tmp_path):
    pfad = tmp_path / "heizzonen.yaml"
    pfad.write_text("zonen: [[[", encoding="utf-8")
    einst = HeizzonenSpeicher(pfad).load()   # darf nicht werfen
    assert einst.zonen == []


# ---------------------------------------------------------------------------
# Senden
# ---------------------------------------------------------------------------


def test_regler_sendet_ist_und_verschiebung(tmp_path):
    regler, router, _ = _regler(tmp_path, _zone())
    regler.istwerte.setze(IST_TOPIC, 21.8, feld="value")
    asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 7, 0)))

    assert len(router.befehle) == 1
    _, kanal, befehl = router.befehle[0]
    assert kanal == "heizkreis_5"
    assert befehl["temperature"] == 21.8
    assert befehl["setpoint_offset"] == 1.5
    assert befehl["day"] is True


def test_der_aktor_bereich_geht_mit_ins_telegramm(tmp_path):
    """
    Ohne offset_min/offset_max nimmt der Encoder seine eigene Vorgabe von
    ±10 K — und der Aktor steht ab Werk auf ±5 K. Die Verschiebung kaeme dann
    halbiert an, ohne dass irgendwo ein Fehler auftaucht.
    """
    regler, router, _ = _regler(tmp_path, _zone(verschiebung_max_k=5.0))
    regler.istwerte.setze(IST_TOPIC, 21.8, feld="value")
    asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 7, 0)))
    assert router.befehle[0][2]["offset_min"] == -5.0
    assert router.befehle[0][2]["offset_max"] == 5.0


def test_falscher_verschiebungsbereich_verschiebt_den_sollwert():
    """
    Der Fall aus der Anlage (23.09.2026): Im Portal stand ±5 K, im Aktor aber
    ±8 K. Gewollt war Sollwert 23,5 °C bei Basis 21 → +2,5 K. Der Aktor zeigte
    stattdessen W: 25,0 °C.

    Nachgerechnet: Wir kodieren DB2 = (2,5 + 5) / 10 · 255 = 191. Der Aktor
    rechnet mit seinem eigenen Bereich zurueck:
        (191 / 255) · 16 − 8 = +4,0 K  →  21 + 4 = 25 °C.
    Genau das war zu sehen. Der Test haelt die Rechnung fest, damit die
    Ursache beim naechsten Mal nicht neu gesucht werden muss.
    """
    def aktor_sieht(db2: int, bereich_k: float) -> float:
        return (db2 / 255.0) * (2 * bereich_k) - bereich_k

    rahmen = encode_a5_10_setpoint(0xFF900081, temperature_c=23.2,
                                   setpoint_offset_c=2.5,
                                   offset_min_c=-5.0, offset_max_c=5.0)
    db2 = rahmen.packet.data[2]          # RORG, DB3, DB2, ...
    assert db2 == 191

    # So, wie der Aktor es mit ±8 K auffasst — der beobachtete Fehler:
    assert round(21.0 + aktor_sieht(db2, 8.0), 1) == 25.0
    # Und so, wie es gemeint war:
    assert round(21.0 + aktor_sieht(db2, 5.0), 1) == 23.5

    # Die Abhilfe: mit dem Bereich kodieren, der im Aktor steht.
    passend = encode_a5_10_setpoint(0xFF900081, temperature_c=23.2,
                                    setpoint_offset_c=2.5,
                                    offset_min_c=-8.0, offset_max_c=8.0)
    db2_passend = passend.packet.data[2]
    assert round(21.0 + aktor_sieht(db2_passend, 8.0), 1) == 23.5


def test_gleicher_bereich_ist_nicht_egal_beim_kodieren():
    """Beleg, dass der Bereich wirklich traegt: gleiche +1,5 K, anderes Byte."""
    mit_5 = encode_a5_10_setpoint(0xFF900085, temperature_c=21.8,
                                  setpoint_offset_c=1.5,
                                  offset_min_c=-5.0, offset_max_c=5.0)
    mit_10 = encode_a5_10_setpoint(0xFF900085, temperature_c=21.8,
                                   setpoint_offset_c=1.5,
                                   offset_min_c=-10.0, offset_max_c=10.0)
    assert mit_5.packet.data != mit_10.packet.data


def test_zyklisches_nachsenden_haelt_den_sensor_am_leben(tmp_path):
    """
    Der Regler erklaert einen Fuehler nach 90 Minuten Funkstille fuer
    ausgefallen. Bei unveraenderter Verschiebung muss trotzdem nachgesendet
    werden, sobald das Sendeintervall um ist.
    """
    regler, router, _ = _regler(tmp_path, _zone(sende_intervall_s=300))
    regler.istwerte.setze(IST_TOPIC, 21.8, feld="value")
    jetzt = datetime(2026, 9, 23, 7, 0)

    asyncio.run(regler.einmal_regeln(jetzt))
    assert len(router.befehle) == 1

    asyncio.run(regler.einmal_regeln(jetzt))
    assert len(router.befehle) == 1       # nichts geaendert, kein Funk

    for schluessel in list(regler._letztes_senden):
        regler._letztes_senden[schluessel] -= 301
    asyncio.run(regler.einmal_regeln(jetzt))
    assert len(router.befehle) == 2


def test_jetzt_senden_erzwingt_trotz_laufendem_takt(tmp_path):
    """
    Beim Einrichten will man sofort sehen, ob der Aktor reagiert — nicht erst
    nach fuenf Minuten. `jetzt_senden` setzt den Sendetakt zurueck.
    """
    regler, router, _ = _regler(tmp_path, _zone(sende_intervall_s=300))
    regler.istwerte.setze(IST_TOPIC, 21.8, feld="value")
    jetzt = datetime(2026, 9, 23, 7, 0)

    asyncio.run(regler.einmal_regeln(jetzt))
    assert len(router.befehle) == 1
    asyncio.run(regler.einmal_regeln(jetzt))
    assert len(router.befehle) == 1       # Takt laeuft noch

    asyncio.run(regler.jetzt_senden())
    assert len(router.befehle) == 2


def test_jetzt_senden_nur_fuer_einen_raum(tmp_path):
    zone = _zone(raeume=[
        _raum(id="wohnen", name="Wohnzimmer",
              heizkreise=[Heizkreis(device_id="v", channel_id="hk1")]),
        _raum(id="bad2", name="Bad",
              heizkreise=[Heizkreis(device_id="v", channel_id="hk2")]),
    ])
    regler, router, _ = _regler(tmp_path, zone)
    regler.istwerte.setze(IST_TOPIC, 21.8, feld="value")
    asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 7, 0)))
    assert len(router.befehle) == 2

    asyncio.run(regler.jetzt_senden(zone_id="bereich1", raum_id="bad2"))
    assert len(router.befehle) == 3
    assert router.befehle[-1][1] == "hk2"


def test_zeitpunkt_des_letzten_sendens_steht_in_der_lage(tmp_path):
    """Damit in der Oberflaeche steht, ob und wann automatisch gefunkt wurde."""
    regler, _, _ = _regler(tmp_path, _zone())
    regler.istwerte.setze(IST_TOPIC, 21.8, feld="value")

    lagen = asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 7, 0)))
    kreis = lagen[0].raeume[0].kreise[0]
    assert kreis.gesendet_vor_s is not None
    assert kreis.gesendet_vor_s < 5
    assert kreis.gesendet_verschiebung_k == 1.5
    assert kreis.als_dict()["gesendet_vor_s"] is not None


def test_noch_nie_gesendet_bleibt_leer(tmp_path):
    regler, _, _ = _regler(tmp_path, _zone())       # ohne Ist-Wert
    lagen = asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 7, 0)))
    assert lagen[0].raeume[0].kreise[0].gesendet_vor_s is None


def test_sollwertwechsel_wird_sofort_gesendet(tmp_path):
    zone = _zone(fenster=[
        Heizfenster(name="Morgens", von="06:00", bis="09:00", sollwert_c=22.5),
        Heizfenster(name="Tags", von="09:00", bis="17:00", sollwert_c=20.0),
    ])
    regler, router, _ = _regler(tmp_path, zone)
    regler.istwerte.setze(IST_TOPIC, 21.8, feld="value")

    asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 8, 59)))
    asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 9, 1)))

    assert len(router.befehle) == 2
    assert router.befehle[0][2]["setpoint_offset"] == 1.5     # 22,5
    assert router.befehle[1][2]["setpoint_offset"] == -1.0    # 20,0


def test_abgeschalteter_Bereich_sendet_nicht(tmp_path):
    regler, router, _ = _regler(tmp_path, _zone(aktiv=False))
    regler.istwerte.setze(IST_TOPIC, 21.8, feld="value")
    asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 7, 0)))
    assert router.befehle == []


def test_abgeschalteter_raum_sendet_nicht(tmp_path):
    regler, router, _ = _regler(tmp_path, _zone(raeume=[_raum(aktiv=False)]))
    regler.istwerte.setze(IST_TOPIC, 21.8, feld="value")
    asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 7, 0)))
    assert router.befehle == []


def test_hauptschalter_aus_sendet_nicht(tmp_path):
    regler, router, _ = _regler(tmp_path, _zone(), aktiv=False)
    regler.istwerte.setze(IST_TOPIC, 21.8, feld="value")
    asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 7, 0)))
    assert router.befehle == []


# ---------------------------------------------------------------------------
# Raeume, Heizkreise und Fuehler sind nicht 1:1
# ---------------------------------------------------------------------------


def test_zwei_heizkreise_ein_thermostat(tmp_path):
    """
    Anforderung: "es kann fuer einen Raum 2 Heizkreise geben und nur 1
    Raumthermostat". Beide Kreise sitzen im selben Raum und bekommen dasselbe
    Telegramm — jeder mit seiner eigenen Sender-ID.
    """
    zone = _zone(raeume=[_raum(heizkreise=[
        Heizkreis(device_id="v", channel_id="hk5", name="Bad vorne"),
        Heizkreis(device_id="v", channel_id="hk6", name="Bad hinten"),
    ])])
    regler, router, _ = _regler(tmp_path, zone)
    regler.istwerte.setze(IST_TOPIC, 21.8, feld="value")

    asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 7, 0)))

    assert {b[1] for b in router.befehle} == {"hk5", "hk6"}
    for _, _, befehl in router.befehle:
        assert befehl["temperature"] == 21.8
        assert befehl["setpoint_offset"] == 1.5


def test_drei_heizkreise_in_einem_raum(tmp_path):
    """"Können auch 3 Heizkreise werden wenn es sehr gross ist" — keine Grenze."""
    zone = _zone(raeume=[_raum(heizkreise=[
        Heizkreis(device_id="v", channel_id=f"hk{n}") for n in (1, 2, 3)
    ])])
    regler, router, _ = _regler(tmp_path, zone)
    regler.istwerte.setze(IST_TOPIC, 21.8, feld="value")
    asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 7, 0)))
    assert len(router.befehle) == 3


def test_mehrere_thermostate_in_einem_raum_werden_gemittelt():
    """
    "oder auch mehrere Raumthermostate zu einem Raum (Vorne | Hinten)".
    Vorgabe ist der Mittelwert.
    """
    zone = _zone(raeume=[_raum(fuehler=[
        Fuehler(topic="1wire/wohnen/vorne", name="Vorne"),
        Fuehler(topic="1wire/wohnen/hinten", name="Hinten"),
    ])])
    speicher = IstwertSpeicher()
    speicher.setze("1wire/wohnen/vorne", 22.0)
    speicher.setze("1wire/wohnen/hinten", 21.0)
    lage = _lage(zone, speicher)
    assert lage.raeume[0].ist_c == 21.5
    assert lage.sendbar is True


def test_gewichteter_mittelwert():
    zone = _zone(raeume=[_raum(fuehler=[
        Fuehler(topic="a", name="Vorne", gewicht=3.0),
        Fuehler(topic="b", name="Hinten", gewicht=1.0),
    ])])
    speicher = IstwertSpeicher()
    speicher.setze("a", 22.0)
    speicher.setze("b", 18.0)
    lage = _lage(zone, speicher)
    assert lage.raeume[0].ist_c == 21.0      # (22*3 + 18*1) / 4


def test_regel_kleinster_regelt_nach_dem_kaeltesten_punkt():
    zone = _zone(fuehler_regel="kleinster", raeume=[_raum(fuehler=[
        Fuehler(topic="a", name="Vorne"), Fuehler(topic="b", name="Hinten"),
    ])])
    speicher = IstwertSpeicher()
    speicher.setze("a", 22.0)
    speicher.setze("b", 21.0)
    assert _lage(zone, speicher).raeume[0].ist_c == 21.0


def test_raum_darf_von_der_verrechnungsregel_abweichen():
    zone = _zone(fuehler_regel="mittelwert", raeume=[_raum(
        fuehler_regel="groesster",
        fuehler=[Fuehler(topic="a"), Fuehler(topic="b")])])
    speicher = IstwertSpeicher()
    speicher.setze("a", 22.0)
    speicher.setze("b", 21.0)
    assert _lage(zone, speicher).raeume[0].ist_c == 22.0


def test_ausgefallener_zweitfuehler_stoppt_die_regelung_nicht_still():
    """
    Faellt von "Vorne | Hinten" einer aus, aendert sich der Mittelwert still.
    Geregelt wird mit dem verbliebenen — aber es steht im Hinweis.
    """
    zone = _zone(raeume=[_raum(fuehler=[
        Fuehler(topic="a", name="Vorne"), Fuehler(topic="b", name="Hinten"),
    ])])
    speicher = IstwertSpeicher()
    speicher.setze("a", 22.0)
    lage = _lage(zone, speicher)
    assert lage.raeume[0].ist_c == 22.0
    assert lage.sendbar is True
    assert "Hinten" in lage.raeume[0].hinweis


def test_zwei_raeume_messen_und_regeln_getrennt(tmp_path):
    zone = _zone(raeume=[
        _raum(id="wohnen", name="Wohnzimmer"),
        _raum(id="ess", name="Esszimmer", sollwert_offset_k=0.5,
              fuehler=[Fuehler(topic="1wire/ess/tmp", name="Esszimmer")],
              heizkreise=[Heizkreis(device_id="v", channel_id="hk2")]),
    ])
    regler, router, _ = _regler(tmp_path, zone)
    regler.istwerte.setze(IST_TOPIC, 21.8, feld="value")
    regler.istwerte.setze("1wire/ess/tmp", 19.4)

    asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 7, 0)))

    nach_kanal = {b[1]: b[2] for b in router.befehle}
    assert nach_kanal["heizkreis_5"]["temperature"] == 21.8
    assert nach_kanal["heizkreis_5"]["setpoint_offset"] == 1.5
    assert nach_kanal["hk2"]["temperature"] == 19.4
    assert nach_kanal["hk2"]["setpoint_offset"] == 2.0     # 1,5 + 0,5


def test_ausgang_mit_abweichendem_basissollwert(tmp_path):
    """Der SRC-DO8 haelt den Basissollwert je Ausgang — 21,1 statt 21,0 kommt vor."""
    zone = _zone(raeume=[_raum(heizkreise=[
        Heizkreis(device_id="v", channel_id="hk1", basissollwert_c=21.1)])])
    regler, router, _ = _regler(tmp_path, zone)
    regler.istwerte.setze(IST_TOPIC, 21.8, feld="value")
    asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 7, 0)))
    assert router.befehle[0][2]["setpoint_offset"] == 1.4   # 22,5 - 21,1


def test_ein_stiller_raum_faellt_auf(tmp_path):
    """
    Regelt ein Teil und ein Teil nicht, muss das sichtbar sein — sonst bleibt
    ein Raum kalt, ohne dass es jemandem auffaellt.
    """
    zone = _zone(raeume=[
        _raum(id="wohnen", name="Wohnzimmer"),
        _raum(id="ess", name="Esszimmer",
              fuehler=[Fuehler(topic="1wire/ess/tmp")],
              heizkreise=[Heizkreis(device_id="v", channel_id="hk2")]),
    ])
    regler, router, _ = _regler(tmp_path, zone)
    regler.istwerte.setze(IST_TOPIC, 21.8, feld="value")   # nur der erste Raum

    lagen = asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 7, 0)))

    assert len(router.befehle) == 1
    assert lagen[0].status == "teilweise"
    assert "Esszimmer" in lagen[0].hinweis


def test_raum_ohne_heizkreis_meldet_sich():
    lage = _lage(_zone(raeume=[_raum(heizkreise=[])]), _mit_ist())
    assert lage.raeume[0].status == "kein_ziel"


# ---------------------------------------------------------------------------
# Sollwertrad am Raumbedienteil (Thanos an der Wand)
# ---------------------------------------------------------------------------


def _wohnzimmer(**kw) -> Heizzone:
    """
    Ein 1-Wire-Fuehler an der Decke und ein Raumbedienteil (Thanos) an der
    Wand. Am Thanos wird der Offset verstellt, beide Ist-Werte zaehlen fuer
    beide Heizkreise.
    """
    raum_kw = dict(
        id="wohnen",
        name="Wohnzimmer",
        fuehler=[
            Fuehler(topic="1wire/sensors/raum_a/decke", name="Decke"),
            Fuehler(topic=THANOS, feld="temperature_c", name="Thanos"),
        ],
        offset_quelle=Wertquelle(topic=THANOS, feld="setpoint_offset_c",
                                 name="Thanos Sollwertrad"),
        heizkreise=[
            Heizkreis(device_id="v", channel_id="hk1", name="Wohnen vorne"),
            Heizkreis(device_id="v", channel_id="hk2", name="Wohnen hinten"),
        ],
    )
    raum_kw.update(kw.pop("raum", {}))
    daten = dict(raeume=[Raum(**raum_kw)], offset_grenze_k=3.0)
    daten.update(kw)
    return _zone(**daten)


def test_thanos_liefert_istwert_und_sollwertrad(tmp_path):
    """Beide Ist-Werte gemittelt, das Rad verschiebt — fuer BEIDE Heizkreise."""
    regler, router, _ = _regler(tmp_path, _wohnzimmer())
    asyncio.run(regler.mqtt_nachricht("1wire/sensors/raum_a/decke", b"21.9"))
    asyncio.run(regler.mqtt_nachricht(THANOS, (
        b'{"temperature_c": 21.3, "setpoint_offset_c": 1.5, '
        b'"fan_stage": "3", "occupancy": true}')))

    asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 7, 0)))

    assert len(router.befehle) == 2
    for _, _, befehl in router.befehle:
        assert befehl["temperature"] == 21.6          # Mittel aus 21,9 und 21,3
        assert befehl["setpoint_offset"] == 3.0       # (22,5 + 1,5) - 21,0


def test_ein_topic_liefert_ist_und_offset_getrennt(tmp_path):
    """
    Der Thanos schickt Temperatur und Rad im selben JSON. Beide muessen als
    getrennte Werte ankommen, sonst ueberschreibt einer den anderen.
    """
    regler, _, _ = _regler(tmp_path, _wohnzimmer())
    asyncio.run(regler.mqtt_nachricht(
        THANOS, b'{"temperature_c": 21.3, "setpoint_offset_c": 1.5}'))
    assert regler.istwerte.hole(THANOS, "temperature_c")[0] == 21.3
    assert regler.istwerte.hole(THANOS, "setpoint_offset_c")[0] == 1.5


def test_voll_aufgedrehtes_rad_hebelt_den_zeitplan_nicht_aus(tmp_path):
    """
    Das Rad des Thanos stand am 23.09.2026 auf +3,2 K. Ohne Grenze wuerde ein
    voll aufgedrehtes Rad den ganzen Zeitplan ueberschreiben.
    """
    regler, router, _ = _regler(tmp_path, _wohnzimmer(offset_grenze_k=2.0))
    asyncio.run(regler.mqtt_nachricht("1wire/sensors/raum_a/decke", b"21.0"))
    asyncio.run(regler.mqtt_nachricht(
        THANOS, b'{"temperature_c": 21.0, "setpoint_offset_c": 3.2}'))

    lagen = asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 7, 0)))

    assert router.befehle[0][2]["setpoint_offset"] == 3.5   # (22,5 + 2,0) - 21,0
    assert lagen[0].raeume[0].raumsteller_k == 2.0
    assert "3.2" in lagen[0].raeume[0].hinweis


def test_ohne_rad_bleibt_es_beim_zeitplan(tmp_path):
    regler, router, _ = _regler(tmp_path, _wohnzimmer())
    asyncio.run(regler.mqtt_nachricht("1wire/sensors/raum_a/decke", b"21.9"))
    asyncio.run(regler.mqtt_nachricht(THANOS, b'{"temperature_c": 21.3}'))
    asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 7, 0)))
    assert router.befehle[0][2]["setpoint_offset"] == 1.5   # 22,5 - 21,0


def test_rad_und_fuehler_werden_abonniert(tmp_path):
    regler, _, _ = _regler(tmp_path, _wohnzimmer())
    quellen = regler.wertquellen()
    assert (THANOS, "temperature_c") in quellen
    assert (THANOS, "setpoint_offset_c") in quellen
    assert ("1wire/sensors/raum_a/decke", "") in quellen


# ---------------------------------------------------------------------------
# Zwangsabsenkung ueber Tuer- und Fensterkontakte
# ---------------------------------------------------------------------------


TUER = "enocean/raum_a/tuerkontakt/state"


def _mit_kontakt(**kw) -> Heizzone:
    """
    Anforderung: Wird die Schiebetuer geoeffnet, geht die Zone in die
    Nachtabsenkung — dasselbe fuer jede andere Tuer mit Kontakt.
    """
    daten = dict(
        kontakte=[Kontakt(topic=TUER, feld="closed", name="Schiebetür",
                          bedeutung="geschlossen")],
        absenkung_zeitzone="Nacht",
        fenster=[
            Heizfenster(name="Tag", von="06:00", bis="22:00", sollwert_c=22.5),
            Heizfenster(name="Nacht", von="22:00", bis="06:00", sollwert_c=18.0),
        ],
    )
    daten.update(kw)
    return _zone(**daten)


def test_offene_tuer_senkt_alle_raeume_auf_die_nacht_zeitzone(tmp_path):
    regler, router, _ = _regler(tmp_path, _mit_kontakt())
    regler.istwerte.setze(IST_TOPIC, 21.8, feld="value")

    asyncio.run(regler.mqtt_nachricht(TUER, b'{"closed": true}'))
    lagen = asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 12, 0)))
    assert lagen[0].sollwert_c == 22.5
    assert lagen[0].abgesenkt is False

    asyncio.run(regler.mqtt_nachricht(TUER, b'{"closed": false}'))
    lagen = asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 12, 0)))
    assert lagen[0].sollwert_c == 18.0
    assert lagen[0].abgesenkt is True
    assert lagen[0].sollwert_quelle == "kontakt"
    assert "Schiebetür" in lagen[0].hinweis
    assert router.befehle[-1][2]["setpoint_offset"] == -3.0   # 18,0 - 21,0


def test_absenkung_um_feste_kelvin_ohne_benannte_zeitzone():
    zone = _mit_kontakt(absenkung_zeitzone="", absenkung_k=4.0)
    speicher = _mit_ist(21.8)
    speicher.setze(TUER, False, feld="closed")
    lage = _lage(zone, speicher, datetime(2026, 9, 23, 12, 0))
    assert lage.sollwert_c == 18.5      # 22,5 - 4,0


def test_kontakt_versteht_die_ueblichen_schreibweisen():
    """
    Ein Text, der den Zustand selbst benennt ("closed"), gilt direkt — sonst
    wuerde die Einstellung 'geschlossen' ihn ein zweites Mal drehen und aus
    einer geschlossenen Tuer eine offene machen. Genau das hatte die erste
    Fassung.
    """
    faelle = [
        ("closed", "geschlossen", b'{"closed": false}', True),
        ("closed", "geschlossen", b'{"closed": true}', False),
        ("open", "offen", b'{"open": true}', True),
        ("open", "offen", b'{"open": false}', False),
        ("state", "geschlossen", b'{"state": "closed"}', False),
        ("state", "geschlossen", b'{"state": "open"}', True),
        ("state", "offen", b'{"state": "closed"}', False),
        ("kontakt", "offen", b'{"kontakt": 1}', True),
    ]
    for feld, bedeutung, payload, erwartet in faelle:
        zone = _mit_kontakt(kontakte=[Kontakt(
            topic="haus/tuer", feld=feld, name="Tür", bedeutung=bedeutung)])
        speicher = _mit_ist(21.8)
        speicher.setze("haus/tuer", rohwert_aus_payload(payload, feld), feld=feld)
        lage = _lage(zone, speicher, datetime(2026, 9, 23, 12, 0))
        assert lage.abgesenkt is erwartet, (feld, bedeutung, payload)


def test_unverstandener_kontakt_senkt_nicht_still_ab():
    """
    Ein Kontakt, den wir nicht lesen koennen, darf nicht als "geschlossen"
    durchgehen — sonst ist die Absenkung stillschweigend ausser Betrieb.
    """
    speicher = _mit_ist(21.8)
    speicher.setze(TUER, "vielleicht", feld="closed")
    lage = _lage(_mit_kontakt(), speicher, datetime(2026, 9, 23, 12, 0))
    assert lage.abgesenkt is False
    assert "ohne verwertbaren Zustand" in lage.hinweis


def test_fensterkontakt_senkt_nur_seinen_eigenen_raum(tmp_path):
    """
    "Bis hin zu einzelnen Raeumen wenn ein Fenster geoeffnet wird" — der
    Kontakt haengt am Raum, nicht am Bereich.
    """
    zone = _mit_kontakt(kontakte=[], raeume=[
        _raum(id="wohnen", name="Wohnzimmer",
              heizkreise=[Heizkreis(device_id="v", channel_id="hk1")]),
        _raum(id="ess", name="Esszimmer",
              kontakte=[Kontakt(topic="haus/ess/fenster", feld="closed",
                                name="Esszimmerfenster")],
              heizkreise=[Heizkreis(device_id="v", channel_id="hk2")]),
    ])
    regler, router, _ = _regler(tmp_path, zone)
    regler.istwerte.setze(IST_TOPIC, 21.8, feld="value")
    asyncio.run(regler.mqtt_nachricht("haus/ess/fenster", b'{"closed": false}'))

    lagen = asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 12, 0)))

    nach_kanal = {b[1]: b[2] for b in router.befehle}
    assert nach_kanal["hk1"]["setpoint_offset"] == 1.5      # 22,5 unveraendert
    assert nach_kanal["hk2"]["setpoint_offset"] == -3.0     # 18,0 abgesenkt
    assert lagen[0].abgesenkt is False                      # der Bereich nicht
    assert lagen[0].raeume[1].abgesenkt is True


def test_absenkung_bleibt_nach_dem_schliessen_nicht_haengen(tmp_path):
    """
    Der Zeitplan-Sollwert wird gemerkt, nicht die Absenkung — sonst waere nach
    dem Schliessen der Tuer ausserhalb der Zeitzonen der Absenkwert der
    "letzte gueltige Wert".
    """
    regler, _, _ = _regler(tmp_path, _mit_kontakt())
    regler.istwerte.setze(IST_TOPIC, 21.8, feld="value")
    asyncio.run(regler.mqtt_nachricht(TUER, b'{"closed": false}'))
    asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 12, 0)))
    assert regler.zustand.sollwert("bereich1") == 22.5      # der Plan, nicht 18


def test_kontakt_topics_werden_abonniert(tmp_path):
    regler, _, _ = _regler(tmp_path, _mit_kontakt())
    assert TUER in regler.abo_topics()
    assert (TUER, "closed") in regler.kontaktquellen()


def test_kontaktwert_gilt_nicht_als_temperatur():
    """Ein Wahrheitswert darf nie als Raumtemperatur durchgehen."""
    speicher = IstwertSpeicher()
    speicher.setze("haus/tuer", True, feld="closed")
    assert speicher.hole("haus/tuer", "closed")[0] is True
    assert speicher.hole_zahl("haus/tuer", "closed") is None


# ---------------------------------------------------------------------------
# Sollwertsteller aus der Timberwolf-Visu
# ---------------------------------------------------------------------------


def _mit_stellern(**kw) -> Heizzone:
    """So, wie eine Visu es typischerweise verdrahtet: ein Topic, zwei Felder."""
    daten = dict(fenster=[
        Heizfenster(name="Tag", von="06:00", bis="22:00",
                    sollwert_topic=VISU, sollwert_feld="TMPTagWohnen",
                    sollwert_c=21.0),
        Heizfenster(name="Nacht", von="22:00", bis="06:00",
                    sollwert_topic=VISU, sollwert_feld="TMPNachtWohnen",
                    sollwert_c=18.0),
    ])
    daten.update(kw)
    return _zone(**daten)


def test_ein_topic_traegt_mehrere_sollwertsteller(tmp_path):
    """
    Die Visu veroeffentlicht alle Steller eines Bereichs in EINEM JSON:
    heizung/bereich1 -> {"TMPTagWohnen": 22.5, "TMPNachtWohnen": 18}
    """
    regler, router, _ = _regler(tmp_path, _mit_stellern())
    regler.istwerte.setze(IST_TOPIC, 21.0, feld="value")
    asyncio.run(regler.mqtt_nachricht(
        VISU, b'{"TMPTagWohnen": 22.5, "TMPNachtWohnen": 18}'))

    assert regler.istwerte.hole(VISU, "TMPTagWohnen")[0] == 22.5
    assert regler.istwerte.hole(VISU, "TMPNachtWohnen")[0] == 18.0

    asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 12, 0)))
    assert router.befehle[-1][2]["setpoint_offset"] == 1.5      # 22,5 - 21,0

    asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 23, 0)))
    assert router.befehle[-1][2]["setpoint_offset"] == -3.0     # 18,0 - 21,0


def test_steller_wird_abonniert(tmp_path):
    regler, _, _ = _regler(tmp_path, _mit_stellern())
    assert VISU in regler.abo_topics()
    assert (VISU, "TMPTagWohnen") in regler.wertquellen()
    assert (VISU, "TMPNachtWohnen") in regler.wertquellen()


def test_ohne_eingetroffenen_steller_gilt_der_hinterlegte_wert():
    lage = _lage(_mit_stellern(), _mit_ist(21.0), datetime(2026, 9, 23, 12, 0))
    assert lage.sollwert_c == 21.0
    assert lage.sollwert_quelle == "fenster"
    assert "noch nichts eingetroffen" in lage.hinweis
    assert lage.sendbar is True        # der Rueckfall ist ein gueltiger Wert


def test_unsinniger_stellerwert_wird_ignoriert():
    speicher = _mit_ist(21.0)
    speicher.setze(VISU, 225.0, feld="TMPTagWohnen")
    lage = _lage(_mit_stellern(), speicher, datetime(2026, 9, 23, 12, 0))
    assert lage.sollwert_c == 21.0            # der hinterlegte Rueckfall
    assert "ausserhalb" in lage.hinweis


def test_stellerwerte_ueberleben_den_neustart(tmp_path):
    """
    Die Visu veroeffentlicht die Steller NICHT retained (nachgemessen am
    23.09.2026: in 20 s kam auf 'heizung/bereich1' nichts an, obwohl dort
    22/18 stehen). Ohne Sicherung faengt die Regelung nach jedem Deploy beim
    hinterlegten Rueckfallwert an, bis jemand einen Steller anfasst.
    """
    regler, _, _ = _regler(tmp_path, _mit_stellern())
    asyncio.run(regler.mqtt_nachricht(
        VISU, b'{"TMPTagWohnen": 22.5, "TMPNachtWohnen": 18}'))
    regler.zustand.save()

    neu, _, _ = _regler(tmp_path, _mit_stellern())
    neu.zustand.load()
    assert neu.istwerte.hole(VISU, "TMPTagWohnen") is None
    assert neu.gespeicherte_steller_uebernehmen() == 2
    assert neu.istwerte.hole(VISU, "TMPTagWohnen")[0] == 22.5

    neu.istwerte.setze(IST_TOPIC, 21.0, feld="value")
    lage = beurteile_zone(neu.speicher.einstellungen.zonen[0],
                          datetime(2026, 9, 23, 12, 0), neu.istwerte,
                          neu.zustand)
    assert lage.sollwert_c == 22.5
    assert lage.sollwert_quelle == "sollwertsteller"


def test_raumtemperatur_wird_nicht_mitgesichert(tmp_path):
    """
    Bewusste Ausnahme: Ein Raum-Istwert von vor dem Neustart darf NICHT als
    aktuelle Messung durchgehen — sonst wuerde die Regelung auf eine
    Temperatur regeln, die niemand gerade gemessen hat.
    """
    regler, _, _ = _regler(tmp_path, _mit_stellern())
    asyncio.run(regler.mqtt_nachricht(IST_TOPIC, b'{"value": 21.0}'))
    regler.zustand.save()

    neu, _, _ = _regler(tmp_path, _mit_stellern())
    neu.zustand.load()
    neu.gespeicherte_steller_uebernehmen()
    assert neu.istwerte.hole(IST_TOPIC, "value") is None


# ---------------------------------------------------------------------------
# MQTT-Anbindung
# ---------------------------------------------------------------------------


def test_abos_enthalten_istwert_und_sollwert_topics(tmp_path):
    regler, _, _ = _regler(tmp_path, _zone())
    abos = regler.abo_topics()
    assert IST_TOPIC in abos
    assert "heizung/bereich1/set" in abos
    assert "heizung/bereich1/fenster/morgens/set" in abos


def test_wurzel_topic_ist_einstellbar(tmp_path):
    """
    Der Timberwolf fuehrt jede Topic-Wurzel als eigenes MQTT-Geraet; dort ist
    'heizung' angelegt. Deshalb haengt die Heizung NICHT unter dem Basis-Topic
    der Bruecke, sondern in ihrem eigenen Ast.
    """
    regler, _, _ = _regler(tmp_path, _zone(), basis_topic="waerme")
    assert regler.zonen_topic("bereich1") == "waerme/bereich1/state"
    assert "waerme/bereich1/fenster/morgens/set" in regler.abo_topics()


def test_istwert_kommt_ueber_mqtt_an(tmp_path):
    regler, router, _ = _regler(tmp_path, _zone())
    asyncio.run(regler.mqtt_nachricht(IST_TOPIC, b'{"value": 20.9}'))
    assert regler.istwerte.hole(IST_TOPIC, "value")[0] == 20.9

    asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 7, 0)))
    assert router.befehle[0][2]["temperature"] == 20.9


def test_sollwert_einer_zeitzone_per_mqtt_aendern(tmp_path):
    regler, _, _ = _regler(tmp_path, _zone())
    asyncio.run(regler.mqtt_nachricht(
        "heizung/bereich1/fenster/morgens/set", b'{"sollwert_c": 23.5}'))
    assert regler.speicher.einstellungen.zonen[0].fenster[0].sollwert_c == 23.5
    neu = HeizzonenSpeicher(regler.speicher.path).load()
    assert neu.zonen[0].fenster[0].sollwert_c == 23.5


def test_sollwert_per_mqtt_als_nackte_zahl(tmp_path):
    regler, _, _ = _regler(tmp_path, _zone())
    asyncio.run(regler.mqtt_nachricht(
        "heizung/bereich1/fenster/morgens/set", b"19"))
    assert regler.speicher.einstellungen.zonen[0].fenster[0].sollwert_c == 19.0


def test_unsinniger_sollwert_per_mqtt_wird_abgewiesen(tmp_path):
    regler, _, _ = _regler(tmp_path, _zone())
    asyncio.run(regler.mqtt_nachricht(
        "heizung/bereich1/fenster/morgens/set", b"99"))
    assert regler.speicher.einstellungen.zonen[0].fenster[0].sollwert_c == 22.5


def test_Bereich_veroeffentlicht_seinen_zustand(tmp_path):
    regler, _, publisher = _regler(tmp_path, _zone())
    regler.istwerte.setze(IST_TOPIC, 21.8, feld="value")
    asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 7, 0)))

    topics = dict(publisher.nachrichten)
    assert "heizung/bereich1/state" in topics
    zustand = topics["heizung/bereich1/state"]
    assert zustand["ist_c"] == 21.8
    assert zustand["sollwert_c"] == 22.5
    assert zustand["fenster"] == "Morgens"
    assert zustand["status"] == "ok"
    assert zustand["raeume"][0]["name"] == "Bad"
    assert zustand["raeume"][0]["kreise"][0]["channel_id"] == "heizkreis_5"
    assert "heizung/bereich1/fenster/morgens/state" in topics


def test_unveraenderter_zustand_wird_nicht_wiederholt(tmp_path):
    regler, _, publisher = _regler(tmp_path, _zone())
    regler.istwerte.setze(IST_TOPIC, 21.8, feld="value")
    jetzt = datetime(2026, 9, 23, 7, 0)
    asyncio.run(regler.einmal_regeln(jetzt))
    anzahl = len(publisher.nachrichten)
    asyncio.run(regler.einmal_regeln(jetzt))
    assert len(publisher.nachrichten) == anzahl


def test_fremdes_topic_geht_den_regler_nichts_an(tmp_path):
    regler, _, _ = _regler(tmp_path, _zone())
    erledigt = asyncio.run(regler.mqtt_nachricht("ganz/woanders", b"21.0"))
    assert erledigt is False


# ---------------------------------------------------------------------------
# Dauerabsenkung, Betriebsart-Schalter, Verschiebung von Hand
#
# Anforderung: Selten genutzte Raeume sollen dauerhaft in der Absenkung stehen;
# ein Raum mit zwei Rhythmen (werktags Arbeitszeit, am Wochenende nur bei
# Besuch) braucht eine Zeitzone, die nur bei Besuch gilt.
# ---------------------------------------------------------------------------


def test_dauerabsenkung_senkt_den_raum():
    zone = _zone(raeume=[_raum(dauerabsenkung=True)], absenkung_k=3.0)
    raum = _lage(zone, _mit_ist(21.8)).raeume[0]
    assert raum.sollwert_c == 19.5          # 22.5 aus der Zeitzone minus 3 K
    assert raum.dauerabsenkung and raum.abgesenkt
    assert "Dauerhaft abgesenkt" in raum.hinweis


def test_dauerabgesenkter_raum_funkt_weiter(tmp_path):
    """
    Der Unterschied zu `aktiv=False`, und der einzige Grund, warum es die
    Dauerabsenkung ueberhaupt gibt: Ein stummer Kreis laeuft nach 90 Minuten in
    den Sensorausfall und bleibt auf der letzten Stellgroesse stehen.
    """
    regler, router, _ = _regler(tmp_path, _zone(raeume=[_raum(dauerabsenkung=True)]))
    regler.istwerte.setze(IST_TOPIC, 21.8, feld="value")
    asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 7, 0)))
    assert router.befehle, "ein dauerhaft abgesenkter Raum muss weiter senden"
    assert router.befehle[0][2]["setpoint_offset"] == -1.5   # 19.5 - 21.0


def test_abgeschalteter_raum_verstummt_dagegen(tmp_path):
    regler, router, _ = _regler(tmp_path, _zone(raeume=[_raum(aktiv=False)]))
    regler.istwerte.setze(IST_TOPIC, 21.8, feld="value")
    asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 7, 0)))
    assert router.befehle == []


def test_dauerabsenkung_nimmt_die_absenk_zeitzone():
    zone = _zone(
        raeume=[_raum(dauerabsenkung=True)],
        fenster=[
            Heizfenster(name="Morgens", von="06:00", bis="09:00", sollwert_c=22.5),
            Heizfenster(name="Nacht", von="22:00", bis="06:00", sollwert_c=17.0),
        ],
        absenkung_zeitzone="Nacht",
    )
    assert _lage(zone, _mit_ist(21.8)).raeume[0].sollwert_c == 17.0


def test_schalter_hebt_die_dauerabsenkung_auf():
    zone = _zone(raeume=[_raum(dauerabsenkung=True,
                               dauerabsenkung_ausnahme_schalter="gaeste")])
    aus = _lage(zone, _mit_ist(21.8), schalter={"gaeste": False}).raeume[0]
    an = _lage(zone, _mit_ist(21.8), schalter={"gaeste": True}).raeume[0]
    assert aus.sollwert_c == 19.5 and aus.dauerabsenkung
    assert an.sollwert_c == 22.5 and not an.dauerabsenkung


def test_dauerabsenkung_ohne_wirkung_meldet_sich():
    """
    Absenkung 0 K: der Raum heizt voll mit, obwohl er als stillgelegt
    eingetragen ist. Das darf nicht still passieren.
    """
    zone = _zone(raeume=[_raum(dauerabsenkung=True)], absenkung_k=0.0)
    raum = _lage(zone, _mit_ist(21.8)).raeume[0]
    assert raum.sollwert_c == 22.5
    assert not raum.abgesenkt
    assert "senkt nichts" in raum.hinweis


def test_zeitzone_gilt_nur_bei_eingeschaltetem_schalter():
    """Sa/So 18 °C als Grundfenster, 21 °C nur wenn Besuch da ist."""
    zone = _zone(fenster=[
        Heizfenster(name="Wochenende", von="07:00", bis="22:00",
                    tage=["sa", "so"], sollwert_c=18.0),
        Heizfenster(name="Gaeste", von="07:00", bis="22:00", tage=["sa", "so"],
                    sollwert_c=21.0, nur_wenn_schalter="gaeste"),
    ])
    samstag = datetime(2026, 9, 26, 10, 0)
    assert aktives_fenster(zone, samstag, {"gaeste": False}).name == "Wochenende"
    assert aktives_fenster(zone, samstag, {"gaeste": True}).name == "Gaeste"
    # Und durchgereicht bis zum Sollwert des Raums:
    assert _lage(zone, _mit_ist(19.0), samstag, {"gaeste": True}).sollwert_c == 21.0


def test_zeitzone_mit_unbekanntem_schalter_gilt_nicht():
    """Zeigt die Bedingung ins Leere, wird NICHT geheizt."""
    zone = _zone(fenster=[Heizfenster(name="Gaeste", von="06:00", bis="09:00",
                                      sollwert_c=21.0,
                                      nur_wenn_schalter="geloescht")])
    assert aktives_fenster(zone, datetime(2026, 9, 23, 7, 0), {}) is None


def test_schalter_per_mqtt_umlegen(tmp_path):
    regler, _, _ = _regler(tmp_path, _zone(),
                           schalter=[Schalter(id="gaeste", name="Gaeste da")])
    assert "heizung/schalter/gaeste/set" in regler.abo_topics()

    asyncio.run(regler.mqtt_nachricht("heizung/schalter/gaeste/set", b"on"))
    assert regler.speicher.einstellungen.schalter[0].an is True
    # und er ueberlebt den Neustart
    assert HeizzonenSpeicher(regler.speicher.path).load().schalter[0].an is True

    asyncio.run(regler.mqtt_nachricht("heizung/schalter/gaeste/set",
                                      b'{"an": false}'))
    assert regler.speicher.einstellungen.schalter[0].an is False


def test_unverstaendlicher_schalterbefehl_aendert_nichts(tmp_path):
    regler, _, _ = _regler(tmp_path, _zone(),
                           schalter=[Schalter(id="gaeste", an=True)])
    asyncio.run(regler.mqtt_nachricht("heizung/schalter/gaeste/set", b"vielleicht"))
    assert regler.speicher.einstellungen.schalter[0].an is True


def test_schalterstand_wird_veroeffentlicht(tmp_path):
    regler, _, publisher = _regler(
        tmp_path, _zone(), schalter=[Schalter(id="gaeste", name="Gaeste da", an=True)])
    regler.istwerte.setze(IST_TOPIC, 21.8, feld="value")
    asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 7, 0)))
    topics = dict(publisher.nachrichten)
    assert topics["heizung/schalter/gaeste/state"] == {
        "id": "gaeste", "name": "Gaeste da", "an": True,
    }


def test_verschiebung_von_hand_wirkt_ohne_rad():
    zone = _zone(raeume=[_raum(handverschiebung_k=1.5)])
    lage = _lage(zone, _mit_ist(21.8))
    raum = lage.raeume[0]
    assert raum.raumsteller_k == 1.5
    assert raum.raumsteller_quelle == "hand"
    assert _kreise(lage)[0].verschiebung_k == 3.0    # 22.5 + 1.5 - 21.0


def test_rad_ueberschreibt_die_verschiebung_von_hand():
    """Zwei Raeder, die sich addieren, versteht spaeter niemand mehr."""
    zone = _zone(raeume=[_raum(
        handverschiebung_k=1.5,
        offset_quelle=Wertquelle(topic=THANOS, feld="setpoint_offset_c",
                                 name="Thanos"),
    )])
    werte = _mit_ist(21.8)
    werte.setze(THANOS, -1.0, feld="setpoint_offset_c")
    raum = _lage(zone, werte).raeume[0]
    assert raum.raumsteller_k == -1.0
    assert raum.raumsteller_quelle == "rad"


def test_ohne_wert_vom_rad_gilt_die_hand():
    zone = _zone(raeume=[_raum(
        handverschiebung_k=1.5,
        offset_quelle=Wertquelle(topic=THANOS, feld="setpoint_offset_c",
                                 name="Thanos"),
    )])
    raum = _lage(zone, _mit_ist(21.8)).raeume[0]     # nichts vom Thanos da
    assert raum.raumsteller_quelle == "hand"
    assert "liefert nichts" in raum.hinweis


def test_verschiebung_von_hand_wird_begrenzt():
    zone = _zone(raeume=[_raum(handverschiebung_k=5.0)], offset_grenze_k=3.0)
    raum = _lage(zone, _mit_ist(21.8)).raeume[0]
    assert raum.raumsteller_k == 3.0
    assert "Grenze" in raum.hinweis


def test_feste_korrektur_bleibt_neben_der_hand_bestehen():
    """Bad dauerhaft +2 K (Korrektur) und +1 K von Hand ergeben +3 K."""
    zone = _zone(raeume=[_raum(sollwert_offset_k=2.0, handverschiebung_k=1.0)])
    lage = _lage(zone, _mit_ist(21.8))
    assert _kreise(lage)[0].verschiebung_k == 4.5    # 22.5 + 2 + 1 - 21


# ---------------------------------------------------------------------------
# Rueckfall-Sollwert: Zeitzonen nur noch fuer Zeiten MIT Heizbedarf
# (Konzeptwechsel)
# ---------------------------------------------------------------------------


MITTAGS = datetime(2026, 9, 23, 12, 0)      # nach der Zeitzone "Morgens"


def test_ausserhalb_der_zeitzonen_gilt_der_rueckfallsollwert():
    zone = _zone(restzeit=Restsollwert(aktiv=True, name="Nachtabsenkung",
                                       sollwert_c=18.0))
    lage = _lage(zone, _mit_ist(21.8), MITTAGS)
    assert lage.sollwert_c == 18.0
    assert lage.sollwert_quelle == "restzeit"
    assert lage.fenster == "Nachtabsenkung"
    assert _kreise(lage)[0].verschiebung_k == -3.0      # 18,0 - 21,0


def test_rueckfallsollwert_kommt_auch_aus_der_visu():
    """Wie eine Zeitzone: fester Wert als Rueckfall, die Visu hat Vorrang."""
    zone = _zone(restzeit=Restsollwert(aktiv=True, sollwert_c=18.0,
                                       sollwert_topic=VISU,
                                       sollwert_feld="BueroNacht"))
    werte = _mit_ist(21.8)
    werte.setze(VISU, 17.0, feld="BueroNacht")
    lage = _lage(zone, werte, MITTAGS)
    assert lage.sollwert_c == 17.0
    assert lage.sollwert_quelle == "sollwertsteller"


def test_rueckfallsollwert_ohne_wert_nimmt_den_hinterlegten():
    zone = _zone(restzeit=Restsollwert(aktiv=True, sollwert_c=18.0,
                                       sollwert_topic=VISU,
                                       sollwert_feld="BueroNacht"))
    lage = _lage(zone, _mit_ist(21.8), MITTAGS)
    assert lage.sollwert_c == 18.0
    assert "noch nichts eingetroffen" in lage.hinweis


def test_rueckfallsollwert_wird_gehoert_und_gesichert(tmp_path):
    zone = _zone(restzeit=Restsollwert(aktiv=True, sollwert_topic=VISU,
                                       sollwert_feld="BueroNacht"))
    regler, _, _ = _regler(tmp_path, zone)
    assert (VISU, "BueroNacht") in regler.wertquellen()

    asyncio.run(regler.mqtt_nachricht(VISU, b'{"BueroNacht": 16.5}'))
    # Die Visu sendet nicht retained — der Wert muss den Neustart ueberleben.
    assert regler.zustand.steller()[(VISU, "BueroNacht")][0] == 16.5


def test_zeitzone_hat_vorrang_vor_dem_rueckfallsollwert():
    zone = _zone(restzeit=Restsollwert(aktiv=True, sollwert_c=18.0))
    lage = _lage(zone, _mit_ist(21.8), datetime(2026, 9, 23, 7, 0))
    assert lage.sollwert_c == 22.5 and lage.fenster == "Morgens"


def test_ohne_rueckfallsollwert_bleibt_es_beim_letzten_wert():
    """Die alte Regel gilt weiter, solange niemand den Rueckfall einschaltet."""
    zone = _zone()
    zustand = HeizungsZustand("/nicht/vorhanden.yaml")
    speicher = _mit_ist(21.8)
    lage = beurteile_zone(zone, datetime(2026, 9, 23, 7, 0), speicher, zustand)
    zustand.merke(zone.id, lage.sollwert_c, "Morgens")
    lage = beurteile_zone(zone, MITTAGS, speicher, zustand)
    assert lage.sollwert_c == 22.5
    assert lage.sollwert_quelle == "letzter_wert"


def test_kontakt_senkt_auf_den_rueckfallsollwert():
    """
    Mit dem neuen Konzept gibt es keine Nacht-Zeitzone mehr, auf die
    `absenkung_zeitzone` zeigen koennte — der Rueckfall-Sollwert ist jetzt die
    Nachttemperatur und damit auch das Ziel der Zwangsabsenkung.
    """
    zone = _mit_kontakt(
        absenkung_zeitzone="",
        fenster=[Heizfenster(name="Tag", von="06:00", bis="22:00",
                             sollwert_c=22.5)],
        restzeit=Restsollwert(aktiv=True, sollwert_c=18.0),
    )
    speicher = _mit_ist(21.8)
    speicher.setze(TUER, False, feld="closed")          # offen
    lage = _lage(zone, speicher, MITTAGS)
    assert lage.sollwert_c == 18.0 and lage.abgesenkt


def test_dauerabsenkung_nimmt_den_rueckfallsollwert():
    zone = _zone(raeume=[_raum(dauerabsenkung=True)],
                 restzeit=Restsollwert(aktiv=True, sollwert_c=18.0))
    raum = _lage(zone, _mit_ist(21.8), datetime(2026, 9, 23, 7, 0)).raeume[0]
    assert raum.sollwert_c == 18.0 and raum.abgesenkt


def test_zeitplan_laeuft_in_der_eingestellten_zeitzone(tmp_path):
    """
    Vorfall: Der Container laeuft auf UTC, der Zeitplan lag im
    Sommer zwei Stunden hinten — "Buerozeit 05:30" begann um 07:30
    Ortszeit. Die Zeitzone gehoert deshalb in die Konfiguration; ein
    Container-Recreate kann sie dort nicht wegnehmen.
    """
    regler, _, _ = _regler(tmp_path, _zone(), zeitzone="Europe/Berlin")
    jetzt = regler.jetzt()
    assert jetzt.tzinfo is not None, "ohne Zeitzone regelt der Plan nach UTC"
    # Sommerzeit: Berlin liegt zwei Stunden vor UTC.
    versatz_h = jetzt.utcoffset().total_seconds() / 3600
    assert versatz_h in (1.0, 2.0)


def test_unbekannte_zeitzone_bricht_die_regelung_nicht(tmp_path):
    """Lieber ein moeglicherweise versetzter Plan als gar keine Regelung."""
    regler, _, _ = _regler(tmp_path, _zone(), zeitzone="Mittelerde/Auenland")
    assert regler.jetzt() is not None
    lagen = asyncio.run(regler.einmal_regeln())
    assert len(lagen) == 1


def test_ohne_zeitzone_gilt_die_uhr_des_containers(tmp_path):
    regler, _, _ = _regler(tmp_path, _zone(), zeitzone="")
    assert regler.jetzt().tzinfo is None


def test_raum_zeigt_den_wirksamen_sollwert():
    """
    Anforderung: Raum mit fester Korrektur +2 K. Der Raumkopf muss die
    Zahl zeigen, die am Aktor ankommt (25,5) — nicht den Zeitplanwert (23,5).
    """
    zone = _zone(raeume=[_raum(sollwert_offset_k=2.0)],
                 fenster=[Heizfenster(name="Tag", von="06:00", bis="22:00",
                                      sollwert_c=23.5)])
    raum = _lage(zone, _mit_ist(22.0), MITTAGS).raeume[0]
    assert raum.sollwert_c == 23.5              # aus dem Zeitplan
    assert raum.sollwert_wirksam_c == 25.5      # 21,0 Basis + 4,5 K Verschiebung


def test_wirksamer_sollwert_bleibt_in_der_grenze_des_aktors():
    """Mehr als ±5 K nimmt der Regler nicht — die Anzeige darf nicht mehr
    versprechen, als ankommt."""
    zone = _zone(raeume=[_raum(sollwert_offset_k=2.0)],
                 fenster=[Heizfenster(name="Tag", von="06:00", bis="22:00",
                                      sollwert_c=27.0)])
    raum = _lage(zone, _mit_ist(22.0), MITTAGS).raeume[0]
    assert raum.sollwert_wirksam_c == 26.0
    assert _kreise(_lage(zone, _mit_ist(22.0), MITTAGS))[0].verschiebung_begrenzt


def test_raum_kennung_darf_im_bereich_nicht_doppelt_sein():
    """
    Faellt beim Verschieben eines Raums in einen anderen Bereich an: die
    Kennung ist der Schluessel des Raums in der Lage und im MQTT-Zustand.
    """
    doppelt = _zone(raeume=[_raum(), _raum(name="Bad hinten")])   # beide id='bad'
    assert len(doppelte_raum_ids([doppelt])) == 1
    assert "bad" in doppelte_raum_ids([doppelt])[0]

    sauber = _zone(raeume=[_raum(), _raum(id="bad_hinten", name="Bad hinten")])
    assert doppelte_raum_ids([sauber]) == []

    # Dieselbe Kennung in ZWEI Bereichen ist dagegen in Ordnung.
    a = _zone(id="a", raeume=[_raum()])
    b = _zone(id="b", raeume=[_raum()])
    assert doppelte_raum_ids([a, b]) == []


def test_derselbe_heizkreis_darf_nicht_in_zwei_raeumen_stehen():
    """
    Ein Ausgang hoert eine Sender-ID. Stuende er in zwei Raeumen, funkten beide
    dagegen und der Sollwert kippte im Sendetakt hin und her.
    """
    doppelt = _zone(raeume=[_raum(), _raum(id="gast", name="Gaestezimmer")])
    meldungen = doppelte_heizkreise([doppelt])
    assert len(meldungen) == 1
    assert "heizkreis_5" in meldungen[0] and "Gaestezimmer" in meldungen[0]

    sauber = _zone(raeume=[
        _raum(),
        _raum(id="gast", name="Gaestezimmer",
              heizkreise=[Heizkreis(device_id="verteiler_ug",
                                    channel_id="heizkreis_6")]),
    ])
    assert doppelte_heizkreise([sauber]) == []


# ---------------------------------------------------------------------------
# Aktortest (24.09.2026): voller Sollwert auf Zeit, zum Nachsehen im Verteiler
# ---------------------------------------------------------------------------


def test_aktortest_faehrt_den_kreis_auf_vollen_sollwert():
    zone = _zone()
    lage = beurteile_zone(zone, datetime(2026, 9, 23, 7, 0), _mit_ist(21.8),
                          HeizungsZustand("/nicht/vorhanden.yaml"),
                          now_ts=1000.0, tests={"bad": 2000.0})
    raum = lage.raeume[0]
    kreis = raum.kreise[0]
    assert raum.test_aktiv is True
    # Statt Zeitplan (22,5 °C -> +1,5 K) geht die volle Verschiebung raus.
    assert kreis.verschiebung_k == zone.verschiebung_max_k
    assert kreis.sendbar is True
    assert "muss aufmachen" in kreis.hinweis
    assert "Aktortest" in raum.hinweis


def test_aktortest_schlaegt_die_absenkung():
    """
    Wer im Verteiler steht, will den Motor laufen sehen — auch bei einem Raum,
    der gerade abgesenkt oder dauerhaft abgesenkt ist.
    """
    zone = _zone(raeume=[_raum(dauerabsenkung=True)],
                 restzeit=Restsollwert(aktiv=True, sollwert_c=17.0))
    lage = beurteile_zone(zone, datetime(2026, 9, 23, 7, 0), _mit_ist(21.8),
                          HeizungsZustand("/nicht/vorhanden.yaml"),
                          now_ts=1000.0, tests={"bad": 2000.0})
    assert lage.raeume[0].kreise[0].verschiebung_k == zone.verschiebung_max_k


def test_abgelaufener_test_regelt_wieder_nach_plan():
    zone = _zone()
    lage = beurteile_zone(zone, datetime(2026, 9, 23, 7, 0), _mit_ist(21.8),
                          HeizungsZustand("/nicht/vorhanden.yaml"),
                          now_ts=3000.0, tests={"bad": 2000.0})
    assert lage.raeume[0].test_aktiv is False
    assert lage.raeume[0].kreise[0].verschiebung_k == 1.5   # 22,5 - 21,0


def test_aktortest_erfindet_keinen_istwert():
    """
    Der Test verstellt den SOLL-Wert, nie den Ist-Wert. Ohne Messung bleibt er
    stumm — dann ist der fehlende Fuehler das eigentliche Problem.
    """
    zone = _zone()
    lage = beurteile_zone(zone, datetime(2026, 9, 23, 7, 0), IstwertSpeicher(),
                          HeizungsZustand("/nicht/vorhanden.yaml"),
                          now_ts=1000.0, tests={"bad": 2000.0})
    assert lage.raeume[0].kreise[0].sendbar is False
    assert lage.sendbar is False


def test_aktortest_wird_sofort_gefunkt_und_endet_von_selbst(tmp_path):
    """
    Anhaken muss sofort wirken (man steht im Verteiler) — und der Test muss
    von selbst enden, sonst heizt ein vergessener Testlauf den Raum auf
    Anschlag weiter.
    """
    zone = _zone()
    regler, router, _ = _regler(tmp_path, zone)
    regler.istwerte.setze(IST_TOPIC, 21.8, feld="value")

    asyncio.run(regler.test_setzen("bereich1", "bad", True, dauer_s=60.0))
    assert router.befehle, "beim Anhaken wurde nicht gefunkt"
    assert router.befehle[-1][2]["setpoint_offset"] == 5.0

    # Zeit vorspulen: der Test laeuft ab, der naechste Durchlauf regelt normal.
    regler._test_bis["bereich1|bad"] = time.time() - 1.0  # noqa: SLF001
    lagen = asyncio.run(regler.einmal_regeln(datetime(2026, 9, 23, 7, 0)))
    assert lagen[0].raeume[0].test_aktiv is False
    assert regler.laufende_tests == {}
    assert router.befehle[-1][2]["setpoint_offset"] == 1.5


def test_aktortest_von_hand_beenden(tmp_path):
    zone = _zone(restzeit=Restsollwert(aktiv=True, sollwert_c=19.0))
    regler, router, _ = _regler(tmp_path, zone)
    regler.istwerte.setze(IST_TOPIC, 21.8, feld="value")
    _, warnung = asyncio.run(regler.test_setzen("bereich1", "bad", True))
    assert router.befehle[-1][2]["setpoint_offset"] == 5.0
    _, warnung = asyncio.run(regler.test_setzen("bereich1", "bad", False))
    assert regler.laufende_tests == {}
    assert warnung == ""
    # Der Rueckfall-Sollwert 19 °C bei Basis 21 °C -> -2 K.
    assert router.befehle[-1][2]["setpoint_offset"] == -2.0


def test_beendeter_test_ohne_sollwert_warnt(tmp_path):
    """
    Die einzige Stelle, an der ein Test etwas hinterlassen koennte: ein noch
    gar nicht eingerichteter Bereich hat nach dem Test keinen Sollwert, den
    man nachschicken koennte — der Kreis bliebe stumm auf dem Testwert stehen.
    Das muss laut gesagt werden.
    """
    zone = _zone(fenster=[], restzeit=Restsollwert(aktiv=False))
    regler, _, _ = _regler(tmp_path, zone)
    regler.istwerte.setze(IST_TOPIC, 21.8, feld="value")
    asyncio.run(regler.test_setzen("bereich1", "bad", True))
    _, warnung = asyncio.run(regler.test_setzen("bereich1", "bad", False))
    assert "bleibt auf dem Testwert" in warnung

