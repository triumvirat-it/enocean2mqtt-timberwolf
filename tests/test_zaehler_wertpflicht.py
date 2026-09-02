"""
Zaehlerstaende duerfen nicht durch Nicht-Werte ueberschrieben werden.

Regression zum Vorfall vom 02.09.2026 — dem dritten seiner Art:

Der Zaehlersammler (Eltako F3Z14D) sendet die Momentanleistung von Phase 1 und 2
mit Divisor 100, also `DB0 = 0x0E`. Der Decoder kannte nur eine feste Liste
erlaubter DB0-Bytes (0x09/0x0C/0x19/0x1C) und antwortete mit
`{"info": "unknown_db0_0x0E"}`. Diese Antwort wurde auf das **retained**
state-Topic des Zaehlers publiziert — und hat damit den letzten gueltigen
Zaehlerstand im Broker geloescht. Jede Visualisierung dahinter zeigte danach
Unsinn (Tagesverbraeuche in Hoehe von Zaehlerstaenden, Kosten von 0,00 EUR).

Dieselbe Mechanik steckte hinter den Vorfaellen im Juli und August 2026:
eine Nachricht ohne brauchbaren Messwert landet auf einem Wert-Topic und bleibt
dort kleben.

Zwei Regeln sichern das ab, beide hier getestet:
  1. DB0 wird bitweise nach EEP A5-12-01 ausgewertet (Tarif/Datentyp/Divisor)
     statt gegen eine Liste geprueft.
  2. Auf ein state-Topic geht nur, was einen Messwert traegt. Diagnosemeldungen
     bleiben im raw-Topic, im Live-Log und im Verwurf-Protokoll.
"""
from __future__ import annotations

import asyncio

import pytest

from app.cascade import Cascade
from app.config import DefaultsConfig
from app.devices import Device, DeviceChannel, DeviceRegistry
from app.eep import register_default_profiles
from app.eep.profiles import decode_a5_12_gas, decode_a5_12_meter
from app.gateway.base import ReceivedTelegram
from app.gateway.esp3 import RORG, RadioTelegram
from app.pipeline import hat_messwert


# ---------------------------------------------------------------- Decoder ---

@pytest.mark.parametrize("db0, erwartet", [
    (0x08, 40302.0),      # Zaehlerstand, Divisor 1
    (0x09, 4030.2),       # Zaehlerstand, Divisor 10   (Eltako-Normalfall)
    (0x0A, 403.02),       # Zaehlerstand, Divisor 100
    (0x0B, 40.302),       # Zaehlerstand, Divisor 1000
])
def test_zaehlerstand_mit_jedem_divisor(db0, erwartet):
    out = decode_a5_12_meter(bytes([0x00, 0x9D, 0x6E, db0]), 0x00, 0xA5)
    assert out["energy_kwh"] == pytest.approx(erwartet)
    assert "current_w" not in out


@pytest.mark.parametrize("db0, erwartet", [
    (0x0C, 40302.0),      # Leistung, Divisor 1
    (0x0D, 4030.2),       # Leistung, Divisor 10
    (0x0E, 403.02),       # Leistung, Divisor 100  <- der Ausloeser des Vorfalls
    (0x0F, 40.302),       # Leistung, Divisor 1000
])
def test_leistung_mit_jedem_divisor(db0, erwartet):
    out = decode_a5_12_meter(bytes([0x00, 0x9D, 0x6E, db0]), 0x00, 0xA5)
    assert out["current_w"] == pytest.approx(erwartet)
    assert "energy_kwh" not in out


def test_der_ausloeser_selbst():
    """Genau das Telegramm aus dem Mitschnitt vom 02.09.2026, 05:03:05 Uhr:
    raw=0000000E, Phase 1 des Zaehlersammlers — 0 W, sauber dekodiert."""
    out = decode_a5_12_meter(bytes.fromhex("0000000E"), 0x00, 0xA5)
    assert out == {"tariff": 0, "current_w": 0.0}
    assert "info" not in out


def test_nachttarif_bleibt_erkannt():
    out = decode_a5_12_meter(bytes([0x00, 0x9D, 0x6E, 0x19]), 0x00, 0xA5)
    assert out["tariff"] == 1
    assert out["energy_kwh"] == pytest.approx(4030.2)


def test_reservierte_bits_gelten_weiter_als_unbekannt():
    """Bits 7..5 sind laut EEP reserviert. Sind sie gesetzt, verstehen wir das
    Telegramm nicht — dann lieber gar kein Wert als ein falscher."""
    out = decode_a5_12_meter(bytes([0x01, 0x02, 0x03, 0x4F]), 0x00, 0xA5)
    assert "current_w" not in out
    assert "energy_kwh" not in out
    assert out["info"] == "unknown_db0_0x4F"


def test_gas_divisor_wird_beruecksichtigt():
    out = decode_a5_12_gas(bytes([0x03, 0x87, 0x18, 0x09]), 0x00, 0xA5)
    assert out["volume_m3"] == pytest.approx(23119.2)   # Live-Stand des Gaszaehlers


# ------------------------------------------------------------- Wertpflicht ---

def test_hat_messwert_erkennt_diagnose():
    assert not hat_messwert({"info": "unknown_db0_0x0E"})
    assert not hat_messwert({"error": "short payload"})
    assert not hat_messwert({"teach_in": True, "detected_eep": "A5-12-01"})
    assert not hat_messwert({"tariff": 0})            # Tarif allein ist kein Wert
    assert not hat_messwert({"_eep": "A5-12-01"})     # nur interne Schluessel


def test_hat_messwert_erkennt_werte():
    assert hat_messwert({"tariff": 0, "current_w": 0.0})
    assert hat_messwert({"energy_kwh": 6699.4})
    assert hat_messwert({"serial_high": "46"})
    assert hat_messwert({"temperature_c": 21.5})


class _CapturePublisher:
    def __init__(self) -> None:
        self.devices: list[tuple[str, str, dict]] = []
        self.raw: list[tuple[str, dict]] = []

    async def publish_raw(self, gw_name, sender_id, payload) -> None:
        self.raw.append((sender_id, payload))

    async def publish_device(self, device, channel, payload) -> None:
        self.devices.append((device.device_id, channel.channel_id, payload))


def _zaehler_device() -> Device:
    """Nachbau des Zaehlersammler-Kanals aus der Anlage: ein Kanal fuer den
    Zaehlerstand, einer fuer die Leistung, beide an derselben Sender-ID."""
    return Device(
        device_id="zaehlersammler", name="Zaehlersammler",
        channels=[
            DeviceChannel(channel_id="1_energy_kwh", name="Phase 1 Energie",
                          enocean_id="FFE90BA6", eep="A5-12-01",
                          meta={"field": "energy_kwh"}),
            DeviceChannel(channel_id="1_current_w", name="Phase 1 Leistung",
                          enocean_id="FFE90BA6", eep="A5-12-01",
                          meta={"field": "current_w"}),
        ],
    )


def _rx(raw_hex: str) -> ReceivedTelegram:
    tel = RadioTelegram(
        rorg=RORG.BS4, payload=bytes.fromhex(raw_hex),
        sender_id=0xFFE90BA6, status=0x00, sub_tel=0,
        destination_id=0xFFFFFFFF, rssi_dbm=-57, security_level=0,
    )
    return ReceivedTelegram(gateway_name="gw_ug", telegram=tel)


def _pipeline():
    from app.pipeline import TelegramPipeline
    register_default_profiles()
    pub = _CapturePublisher()
    pipe = TelegramPipeline(
        manager=object(), publisher=pub,
        devices=DeviceRegistry([_zaehler_device()]), cascade=Cascade(),
    )
    pipe.defaults = DefaultsConfig()
    return pipe, pub


def test_zaehlerstand_wird_publiziert():
    pipe, pub = _pipeline()
    asyncio.run(pipe._process(_rx("0105B209")))   # 6699.4 kWh, wie in der Anlage
    werte = [p.get("value") for _d, c, p in pub.devices if c == "1_energy_kwh"]
    assert werte == [pytest.approx(6699.4)]


def test_telegramm_ohne_messwert_ueberschreibt_nichts():
    """Der Kern des Vorfalls: Ein nicht verstandenes Telegramm darf das
    state-Topic NICHT anfassen — sonst ist der Zaehlerstand im Broker weg."""
    pipe, pub = _pipeline()
    asyncio.run(pipe._process(_rx("0105B209")))   # guter Zaehlerstand
    vorher = len(pub.devices)

    asyncio.run(pipe._process(_rx("010203CF")))   # reservierte Bits -> unbekannt

    assert len(pub.devices) == vorher, (
        "Ein Telegramm ohne Messwert wurde auf ein state-Topic publiziert — "
        "genau das loescht den letzten guten Zaehlerstand im Broker."
    )


def test_diagnose_bleibt_im_raw_topic_sichtbar():
    """Nicht publizieren heisst nicht verschweigen: Das Roh-Telegramm muss
    weiter im raw-Topic auftauchen, sonst sieht niemand das Problem."""
    pipe, pub = _pipeline()
    asyncio.run(pipe._process(_rx("010203CF")))
    assert pub.raw, "kein raw-Publish"
    _sid, raw_payload = pub.raw[-1]
    assert raw_payload.get("info") == "unknown_db0_0xCF"
