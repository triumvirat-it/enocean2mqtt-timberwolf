"""
Tests fuer die Auswahlliste der Heizkreise (app/heizausgang.py).

Der Anlass steht im Modul: Mehr Kreise als der 8-fach-Aktor Ausgaenge hat.
Der Rest haengt an einem einzelnen OPUS GN-A-U230V-HKR-1, der als Empfangsgeraet eingelesen wurde — und deshalb in
der Auswahl fehlte.
"""
from __future__ import annotations

import pytest

from app.config import GatewayConfig
from app.devices import Device, DeviceChannel, SenderBinding
from app.heizausgang import (
    ausgang_einrichten,
    belegte_sender_ids,
    heizausgaenge,
    ist_heizungsaktor,
)

GATEWAYS = [
    GatewayConfig(name="LAN Gateway UG", host="192.0.2.18",
                  base_id="FF900080"),
    GatewayConfig(name="LAN Gateway OG", host="192.0.2.19",
                  base_id="FF800000"),
]

HKR_CONFIG = ("SetpointOffsetMin=-20;SetpointOffsetMax=20;"
              "ProvidesTempLoweringMode=3;BaseTemp=20;")


def hkr_kanal(enocean_id: str = "FF800016") -> DeviceChannel:
    """Der einzelne Stellventilaktor, so wie er aus dem MHC-Bestand kommt."""
    return DeviceChannel(
        channel_id="1", name="Raum B / GN-A-U230V-HKR-1",
        enocean_id=enocean_id, eep="A5-10-03", direction="rx",
        meta={"device_type": "Valve_1", "device_config": HKR_CONFIG},
    )


def hkr_geraet(enocean_id: str = "FF800016") -> Device:
    return Device(device_id="hkr_raum_b", manufacturer="Jäger Direkt - OPUS",
                  model="GN-A-U230V-HKR-1", name="Heizung Raum B",
                  channels=[hkr_kanal(enocean_id)])


def verteiler() -> Device:
    """Ein fertig eingerichteter Ausgang des 8-fach-Aktors."""
    return Device(
        device_id="src_do8", manufacturer="Thermokon", model="SRC-DO8",
        name="Heizverteileraktor UG",
        channels=[DeviceChannel(
            channel_id="1.1", name="Heizkreis 1", eep="A5-10-06",
            direction="tx",
            senders=[SenderBinding(sender_id="FF900081",
                                   via_gateway="LAN Gateway UG", active=True)],
            meta={"device_type": "Valve_8",
                  "device_config": "SetpointOffsetMin=-10;SetpointOffsetMax=10;"
                                   "BaseTemp=21;"},
        )],
    )


def raumbedienteil() -> Device:
    """Ein echtes Raumpanel an der Wand — kein Heizungsaktor."""
    return Device(
        device_id="rts55_bad", model="RTS55", name="Bad",
        channels=[DeviceChannel(
            channel_id="temperature_c", name="Bad Temperatur",
            enocean_id="01020304", eep="A5-10-03", direction="rx",
            meta={"device_type": "RoomTemperatureControl",
                  "field": "temperature_c"},
        )],
    )


# --- Wer gehoert in die Liste ------------------------------------------------


def test_stellventilaktor_ist_ein_heizungsaktor():
    assert ist_heizungsaktor(hkr_kanal()) is True


def test_raumbedienteil_ist_keiner():
    """
    Gleiches Profil (A5-10-03), voellig andere Rolle: Das Panel liefert den
    Ist-Wert, es regelt nichts. Stuende es in der Liste, koennte man einen
    Heizkreis auf einen Fuehler zeigen lassen.
    """
    assert ist_heizungsaktor(raumbedienteil().channels[0]) is False


def test_liste_zeigt_den_neuen_aktor_neben_den_eingerichteten():
    ziele = heizausgaenge([verteiler(), hkr_geraet(), raumbedienteil()],
                          GATEWAYS)
    kennungen = {(z.device_id, z.channel_id) for z in ziele}
    assert ("src_do8", "1.1") in kennungen
    assert ("hkr_raum_b", "1") in kennungen
    assert ("rts55_bad", "temperature_c") not in kennungen


def test_eingerichteter_ausgang_ist_bereit():
    ziel = heizausgaenge([verteiler()], GATEWAYS)[0]
    assert ziel.bereit is True
    assert ziel.sender_id == "FF900081"
    assert ziel.via_gateway == "LAN Gateway UG"
    assert ziel.grund == ""


def test_neuer_aktor_ist_einrichtbar_und_sagt_warum():
    ziel = heizausgaenge([hkr_geraet()], GATEWAYS)[0]
    assert ziel.bereit is False
    assert ziel.einrichtbar is True
    assert "FF800016" in ziel.grund


def test_fremde_adresse_ist_nicht_einrichtbar():
    """
    Ein Gateway kann nur mit IDs aus seinem eigenen 128er-Block senden. Passt
    die Adresse in keinen, bringt Einrichten nichts — das muss dranstehen,
    statt einen Ausgang anzubieten, der stumm bliebe.
    """
    ziel = heizausgaenge([hkr_geraet("01A03001")], GATEWAYS)[0]
    assert ziel.einrichtbar is False
    assert "Gateway" in ziel.grund


def test_aktor_ohne_sendeweg_steht_drin_aber_nicht_bereit():
    """
    Ein bidirektionaler Heizkoerperantrieb (A5-20-01) ist ein Heizungsaktor,
    aber fuer dieses Profil gibt es keinen Encoder. Er wird gezeigt und der
    Grund genannt — verschweigen waere schlechter, dann sucht man ihn ewig.
    """
    geraet = Device(
        device_id="md15", model="MD15", name="Heizkörperantrieb",
        channels=[DeviceChannel(channel_id="1", name="Ventil",
                                enocean_id="FF800020", eep="A5-20-01",
                                direction="rx",
                                meta={"device_type": "Valve_1"})],
    )
    ziel = heizausgaenge([geraet], GATEWAYS)[0]
    assert ziel.bereit is False
    assert ziel.einrichtbar is False
    assert "A5-20-01" in ziel.grund


def test_katalogwerte_kommen_mit():
    """
    Basis und Bereich stehen im Aktor und muessen dort gegengeprueft werden.
    Der Katalogwert ist nur ein Anhalt — aber beim HKR-1 ein anderer als beim
    SRC-DO8 (±20 statt ±10), und genau das soll man sehen.
    """
    hkr = heizausgaenge([hkr_geraet()], GATEWAYS)[0]
    assert hkr.basis_c == 20.0
    assert hkr.spanne_k == 20.0
    do8 = heizausgaenge([verteiler()], GATEWAYS)[0]
    assert do8.basis_c == 21.0
    assert do8.spanne_k == 10.0


# --- Einrichten --------------------------------------------------------------


def test_einrichten_macht_aus_der_adresse_eine_sende_id():
    geraet = hkr_geraet()
    kanal = geraet.channels[0]
    gateway = ausgang_einrichten(geraet, kanal, GATEWAYS, [])
    assert gateway == "LAN Gateway OG"     # FF800016 liegt im Block FF800000
    assert kanal.active_sender_id == "FF800016"
    assert kanal.active_via_gateway == "LAN Gateway OG"
    assert kanal.direction == "tx"
    # Die Adresse ist jetzt unsere Sende-ID. Bliebe sie als Empfangs-ID stehen,
    # landete unser eigenes Telegramm als vermeintliche Messung im Live-Log.
    assert kanal.enocean_id is None
    assert heizausgaenge([geraet], GATEWAYS)[0].bereit is True


def test_einrichten_lehnt_eine_schon_belegte_id_ab():
    """
    Zwei Stellen auf derselben Sender-ID ueberschreiben sich im Sendetakt,
    ohne Fehlermeldung — dieselbe Falle wie ein Heizkreis in zwei Raeumen.
    """
    geraet = hkr_geraet("FF900081")
    with pytest.raises(ValueError, match="bereits"):
        ausgang_einrichten(geraet, geraet.channels[0], GATEWAYS,
                           belegte_sender_ids([verteiler()]))


def test_einrichten_ohne_passendes_gateway_bricht_ab():
    geraet = hkr_geraet("01A03001")
    with pytest.raises(ValueError, match="Gateway"):
        ausgang_einrichten(geraet, geraet.channels[0], GATEWAYS, [])


def test_einrichten_eines_fertigen_ausgangs_aendert_nichts():
    geraet = verteiler()
    with pytest.raises(ValueError, match="bereits"):
        ausgang_einrichten(geraet, geraet.channels[0], GATEWAYS, [])
    assert geraet.channels[0].active_sender_id == "FF900081"


# --- Der ganze Weg bis zum Telegramm ----------------------------------------


def _router(tmp_path):
    """TXRouter ohne Gateways — gebaut wird das Telegramm trotzdem."""
    import yaml

    from app.cascade import Cascade
    from app.config import AppConfig
    from app.devices import DeviceRegistry
    from app.gateway import GatewayManager
    from app.tx_router import TXRouter

    (tmp_path / "gateways.yaml").write_text(
        yaml.safe_dump({"mqtt": {"host": "h"}, "gateways": []}), encoding="utf-8")
    cfg = AppConfig.load(tmp_path)
    return TXRouter(cfg, GatewayManager(cfg), DeviceRegistry([]), Cascade())


def test_der_eingerichtete_aktor_bekommt_wirklich_ein_telegramm(tmp_path):
    """
    Die Probe aufs Ganze: Kanal einrichten, Befehl des Reglers durchschicken,
    Telegramm zaehlen. Vorher endete der Weg still — A5-10-03 hatte keinen
    Encoder, der TXRouter baute kein Telegramm, und niemand hat es gemerkt.
    """
    from app.gateway.esp3 import RORG

    geraet = hkr_geraet()
    kanal = geraet.channels[0]
    ausgang_einrichten(geraet, kanal, GATEWAYS, [])

    frame = _router(tmp_path)._build_frame(
        kanal, int(kanal.active_sender_id, 16),
        {"temperature": 21.0, "setpoint_offset": 1.0,
         "offset_min": -20.0, "offset_max": 20.0, "day": True},
    )
    assert frame is not None
    nutzlast = frame.packet.data[1:5]
    assert nutzlast[0] == 0x00 and nutzlast[3] == 0x0F
    assert frame.packet.data[0] == RORG.BS4


def test_ohne_angabe_gilt_der_basissollwert_aus_der_geraetekonfiguration(tmp_path):
    """
    Kommt der Befehl von aussen (MQTT) nur mit einer Verschiebung, zieht der
    TXRouter `BaseTemp` aus der Kanal-Konfiguration und macht daraus eine
    Solltemperatur — denn Eltako will die Temperatur selbst.
    Hier: BaseTemp 20 °C + 2 K = 22 °C -> (22-8)/22*255 = 162.
    """
    geraet = hkr_geraet()
    kanal = geraet.channels[0]
    ausgang_einrichten(geraet, kanal, GATEWAYS, [])

    frame = _router(tmp_path)._build_frame(
        kanal, 0x01, {"temperature": 21.0, "setpoint_offset": 2.0})
    assert frame.packet.data[2] == 162
