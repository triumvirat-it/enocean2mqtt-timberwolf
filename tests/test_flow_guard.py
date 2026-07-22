"""
Plausibilitaets-Guard fuer Momentan-/Durchfluss-Werte (A5-12-02/03, current_w).

Regression: ein einzelnes gestoertes Funktelegramm eines Eltako-Zaehlers
(z.B. Gas raw=0800000C -> flow_m3h=524288 m^3/h, bei RSSI -78) fror im UI-
Feld-Cache ein — solche "Momentanwert"-Telegramme kommen selten, der Ausreisser
wurde nie durch einen echten Wert ueberschrieben. Der Guard verwirft Werte
oberhalb einer konfigurierbaren Obergrenze; das ausloesende Roh-Telegramm bleibt
im telegram_audit.log sichtbar, damit die Grenze nachschaerfbar ist.
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


def _gas_flow_payload(m3h: int) -> bytes:
    """A5-12-02 Gas-Momentanwert (db0=0x0C: Datentelegramm, Flow-Bit gesetzt,
    Normaltarif)."""
    return bytes([(m3h >> 16) & 0xFF, (m3h >> 8) & 0xFF, m3h & 0xFF, 0x0C])


def _gas_device() -> Device:
    return Device(
        device_id="gaszaehler", name="Gaszaehler",
        channels=[
            DeviceChannel(channel_id="flow", name="Gasverbrauch Leistung",
                          enocean_id="05000012", eep="A5-12-02",
                          meta={"field": "flow_m3h", "tele_channel": "0"}),
        ],
    )


def _rx(m3h: int) -> ReceivedTelegram:
    tel = RadioTelegram(
        rorg=RORG.BS4, payload=_gas_flow_payload(m3h),
        sender_id=0x05000012, status=0x00, sub_tel=0,
        destination_id=0xFFFFFFFF, rssi_dbm=-78, security_level=0,
    )
    return ReceivedTelegram(gateway_name="gw_ug", telegram=tel)


def _pipeline(defaults: DefaultsConfig | None = None):
    from app.pipeline import TelegramPipeline
    register_default_profiles()
    pub = _CapturePublisher()
    pipe = TelegramPipeline(
        manager=object(), publisher=pub,
        devices=DeviceRegistry([_gas_device()]), cascade=Cascade(),
    )
    pipe.defaults = defaults or DefaultsConfig()  # flow_max_m3h = 100
    return pipe, pub


def _published_flow(pub: _CapturePublisher) -> list[float]:
    return [p["value"] for _d, _c, p in pub.devices
            if p.get("unit") == "m³/h" and "value" in p]


def test_flow_ausreisser_wird_verworfen():
    pipe, pub = _pipeline()
    asyncio.run(pipe._process(_rx(524288)))   # 0x080000 — das Gas-Phantom
    assert _published_flow(pub) == []          # nichts published, kein Einfrieren


def test_plausibler_flow_kommt_durch():
    pipe, pub = _pipeline()
    asyncio.run(pipe._process(_rx(7)))         # 7 m^3/h — realistischer Spitzenwert
    assert _published_flow(pub) == [7.0]


def test_guard_aus_bei_null_grenze():
    pipe, pub = _pipeline(DefaultsConfig(flow_max_m3h=0))  # Schutz aus
    asyncio.run(pipe._process(_rx(524288)))
    assert 524288.0 in _published_flow(pub)


def test_raw_telegramm_bleibt_trotz_verwurf_sichtbar():
    """Auch wenn der Wert verworfen wird, muss das Roh-Telegramm publishbar
    bleiben (raw-Topic) — damit man den Vorfall ueberhaupt sieht."""
    pipe, pub = _pipeline()
    asyncio.run(pipe._process(_rx(524288)))
    # raw-Publish passiert immer; flow_m3h darf darin NICHT mehr stehen.
    assert pub.raw, "kein raw-Publish"
    _sid, raw_payload = pub.raw[-1]
    assert "flow_m3h" not in raw_payload
