"""
Energiezaehler-Plausibilitaet (A5-12-01, Eltako DSZ14DRS).

Regression: ein einzelnes gestoertes Telegramm mit einem unplausibel hohen
Zaehlerstand (Funk-Bitfehler / Bus-Artefakt) durfte den Monotonie-Filter
NICHT dauerhaft vergiften. Vorher wurde der Ausreisser als neuer Hoechststand
in den Cache uebernommen; ab da galten alle echten (niedrigeren) Werte als
"Monotonie verletzt" und wurden fuer immer verworfen — der Bueromzaehler
haengte auf 99472 fest, obwohl er real bei ~47172 stand, und HomeAssistant
bekam tagelang keinen neuen Verbrauch mehr.
"""
from __future__ import annotations

import asyncio

from app.cascade import Cascade
from app.config import DefaultsConfig
from app.devices import Device, DeviceChannel, DeviceRegistry
from app.eep import register_default_profiles
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


def _meter_payload(energy_kwh: float) -> bytes:
    """A5-12-01 kWh-Datentelegramm (Normaltarif, db0=0x09)."""
    data_raw = round(energy_kwh / 0.1)
    return bytes([(data_raw >> 16) & 0xFF, (data_raw >> 8) & 0xFF,
                  data_raw & 0xFF, 0x09])


def _meter_device() -> Device:
    return Device(
        device_id="zaehler_buero", name="Stromzaehler Buero",
        channels=[
            DeviceChannel(channel_id="energie", name="Energie",
                          enocean_id="0518A1B2", eep="A5-12-01",
                          meta={"field": "energy_kwh", "tele_channel": "0"}),
        ],
    )


def _rx(energy_kwh: float) -> ReceivedTelegram:
    tel = RadioTelegram(
        rorg=RORG.BS4, payload=_meter_payload(energy_kwh),
        sender_id=0x0518A1B2, status=0x00, sub_tel=0,
        destination_id=0xFFFFFFFF, rssi_dbm=-70, security_level=0,
    )
    return ReceivedTelegram(gateway_name="gw1", telegram=tel)


def _pipeline():
    from app.pipeline import TelegramPipeline
    register_default_profiles()
    pub = _CapturePublisher()
    pipe = TelegramPipeline(
        manager=object(), publisher=pub,
        devices=DeviceRegistry([_meter_device()]), cascade=Cascade(),
    )
    pipe.defaults = DefaultsConfig()  # energy_max_jump_kwh = 1000
    return pipe, pub


def _published_energy(pub: _CapturePublisher) -> list[float]:
    # publish_device verpackt den Wert als {"value": ..., "unit": "kWh"}.
    return [p["value"] for _d, _c, p in pub.devices
            if p.get("unit") == "kWh" and "value" in p]


def test_outlier_jump_does_not_poison_cache():
    pipe, pub = _pipeline()
    asyncio.run(pipe._process(_rx(47000.0)))   # Baseline
    asyncio.run(pipe._process(_rx(99472.0)))   # Ausreisser (+52472 kWh)
    asyncio.run(pipe._process(_rx(47172.4)))   # echter Folgewert

    energies = _published_energy(pub)
    # Baseline und echter Folgewert kommen durch, der Ausreisser NICHT.
    assert 47000.0 in energies
    assert 47172.4 in energies
    assert 99472.0 not in energies


def test_normal_increase_still_passes():
    pipe, pub = _pipeline()
    asyncio.run(pipe._process(_rx(47000.0)))
    asyncio.run(pipe._process(_rx(47005.5)))   # +5,5 kWh, plausibel
    assert _published_energy(pub) == [47000.0, 47005.5]


def test_jump_guard_off_when_zero():
    pipe, pub = _pipeline()
    pipe.defaults = DefaultsConfig(energy_max_jump_kwh=0)  # Schutz aus
    asyncio.run(pipe._process(_rx(47000.0)))
    asyncio.run(pipe._process(_rx(99472.0)))
    assert 99472.0 in _published_energy(pub)
