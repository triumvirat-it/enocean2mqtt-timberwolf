"""
Zaehlerstaende muessen einen Neustart ueberleben.

Regression, Meldung aus dem Betrieb: "enocean2mqtt hat den Zaehlerstand null
geschickt und ist dann auf den richtigen Zaehlerstand hochgegangen, dann
stimmen die taeglich gerechneten Werte nicht mehr — da ist irgendwo der alte
Zaehlerstand verloren gegangen."

Genau so war es: Der Monotonie-Schutz hielt den letzten Stand nur im
Arbeitsspeicher. Bei jedem Container-Neustart — also nach jedem Deploy — war er
weg, und der erste eintreffende Wert wurde bedingungslos zur neuen Referenz.
Kam dabei eine 0 oder ein niedriger Sub-Tarif-Stand zuerst, sprang der Zaehler
in der Visualisierung von dort auf den echten Stand, und die Tagesstatistik
rechnete die Differenz als Verbrauch eines Tages.
"""
from __future__ import annotations

import asyncio

from app.cascade import Cascade
from app.config import DefaultsConfig
from app.devices import Device, DeviceChannel, DeviceRegistry
from app.eep import register_default_profiles
from app.energy_state import EnergyStateStore
from app.gateway.base import ReceivedTelegram
from app.gateway.esp3 import RORG, RadioTelegram


class _CapturePublisher:
    def __init__(self) -> None:
        self.devices: list[tuple[str, str, dict]] = []
        self.raw: list[tuple[str, dict]] = []

    async def publish_raw(self, gw_name, sender_id, payload) -> None:
        self.raw.append((sender_id, payload))

    async def publish_device(self, device, channel, payload) -> None:
        self.devices.append((device.device_id, channel.channel_id, payload))


def _payload(energy_kwh: float) -> bytes:
    roh = round(energy_kwh / 0.1)
    return bytes([(roh >> 16) & 0xFF, (roh >> 8) & 0xFF, roh & 0xFF, 0x09])


def _device() -> Device:
    return Device(
        device_id="zaehler", name="Stromzaehler",
        channels=[
            DeviceChannel(channel_id="energie", name="Energie",
                          enocean_id="0518A1B2", eep="A5-12-01",
                          meta={"field": "energy_kwh", "tele_channel": "0"}),
        ],
    )


def _rx(energy_kwh: float) -> ReceivedTelegram:
    tel = RadioTelegram(
        rorg=RORG.BS4, payload=_payload(energy_kwh),
        sender_id=0x0518A1B2, status=0x00, sub_tel=0,
        destination_id=0xFFFFFFFF, rssi_dbm=-70, security_level=0,
    )
    return ReceivedTelegram(gateway_name="gw1", telegram=tel)


def _pipeline(speicher: EnergyStateStore | None):
    from app.pipeline import TelegramPipeline
    register_default_profiles()
    pub = _CapturePublisher()
    pipe = TelegramPipeline(
        manager=object(), publisher=pub,
        devices=DeviceRegistry([_device()]), cascade=Cascade(),
    )
    pipe.defaults = DefaultsConfig(energy_max_kw=0)
    pipe.energy_state = speicher
    return pipe, pub


def _werte(pub: _CapturePublisher) -> list[float]:
    return [p["value"] for _d, _c, p in pub.devices
            if p.get("unit") == "kWh" and "value" in p]


def test_stand_wird_gesichert_und_geladen(tmp_path):
    datei = tmp_path / "energy_state.yaml"

    speicher = EnergyStateStore(datei, save_interval_s=0)
    pipe, pub = _pipeline(speicher)
    asyncio.run(pipe._process(_rx(48727.6)))
    speicher.save()

    assert datei.exists(), "Zaehlerstand wurde nicht gesichert"

    frisch = EnergyStateStore(datei)
    frisch.load()
    assert frisch.get(("0518A1B2", 0, "energy_kwh")) == 48727.6


def test_nach_neustart_wird_eine_null_verworfen(tmp_path):
    """Der Kern des Vorfalls: Nach dem Neustart darf ein 0-Wert NICHT zur
    neuen Referenz werden — sonst rechnet die Visualisierung anschliessend
    den ganzen Zaehlerstand als Tagesverbrauch."""
    datei = tmp_path / "energy_state.yaml"

    # Lauf 1: echter Stand kommt rein und wird gesichert
    speicher = EnergyStateStore(datei, save_interval_s=0)
    pipe, _pub = _pipeline(speicher)
    asyncio.run(pipe._process(_rx(48727.6)))
    speicher.save()

    # Lauf 2: frischer Prozess (Arbeitsspeicher leer), Zaehler meldet 0
    speicher2 = EnergyStateStore(datei, save_interval_s=0)
    speicher2.load()
    pipe2, pub2 = _pipeline(speicher2)
    asyncio.run(pipe2._process(_rx(0.0)))

    assert _werte(pub2) == [], (
        "Die 0 wurde nach dem Neustart als neuer Zaehlerstand veroeffentlicht — "
        "genau daraus entsteht der Phantom-Tagesverbrauch."
    )


def test_nach_neustart_laeuft_der_zaehler_normal_weiter(tmp_path):
    datei = tmp_path / "energy_state.yaml"

    speicher = EnergyStateStore(datei, save_interval_s=0)
    pipe, _pub = _pipeline(speicher)
    asyncio.run(pipe._process(_rx(48727.6)))
    speicher.save()

    speicher2 = EnergyStateStore(datei, save_interval_s=0)
    speicher2.load()
    pipe2, pub2 = _pipeline(speicher2)
    asyncio.run(pipe2._process(_rx(48728.1)))     # plausibler Anstieg

    assert _werte(pub2) == [48728.1]


def test_ohne_speicher_verhaelt_es_sich_wie_frueher(tmp_path):
    """Ohne gesetzten Speicher (z.B. in aelteren Tests) darf nichts brechen."""
    pipe, pub = _pipeline(None)
    asyncio.run(pipe._process(_rx(48727.6)))
    assert _werte(pub) == [48727.6]


def test_defekte_datei_blockiert_den_start_nicht(tmp_path):
    datei = tmp_path / "energy_state.yaml"
    datei.write_text("kein: [gueltiges, yaml", encoding="utf-8")

    speicher = EnergyStateStore(datei)
    speicher.load()          # darf nicht werfen
    assert len(speicher) == 0
