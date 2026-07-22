"""
Energiezaehler-Mitschrift (energy_audit-Logger).

Damit ein "Zaehler steht auf Null / haengt fest"-Vorfall im Nachhinein
diagnostizierbar ist, protokolliert die Pipeline JEDES kWh-Telegramm mit
Rohwert, letztem Cache-Stand und der Filter-Entscheidung. Diese Tests
sichern ab, dass fuer die relevanten Faelle genau eine Audit-Zeile mit dem
richtigen Ergebnis (UEBERNOMMEN/VERWORFEN) entsteht.
"""
from __future__ import annotations

import asyncio
import logging

from app.cascade import Cascade
from app.config import DefaultsConfig
from app.devices import Device, DeviceChannel, DeviceRegistry
from app.eep import register_default_profiles
from app.gateway.base import ReceivedTelegram
from app.gateway.esp3 import RORG, RadioTelegram


class _CapturePublisher:
    async def publish_raw(self, gw_name, sender_id, payload) -> None:
        pass

    async def publish_device(self, device, channel, payload) -> None:
        pass


def _meter_payload(energy_kwh: float) -> bytes:
    data_raw = round(energy_kwh / 0.1)
    return bytes([(data_raw >> 16) & 0xFF, (data_raw >> 8) & 0xFF,
                  data_raw & 0xFF, 0x09])


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
    dev = Device(
        device_id="zaehler_buero", name="Stromzaehler Buero",
        channels=[
            DeviceChannel(channel_id="energie", name="Energie",
                          enocean_id="0518A1B2", eep="A5-12-01",
                          meta={"field": "energy_kwh", "tele_channel": "0"}),
        ],
    )
    pipe = TelegramPipeline(
        manager=object(), publisher=_CapturePublisher(),
        devices=DeviceRegistry([dev]), cascade=Cascade(),
    )
    pipe.defaults = DefaultsConfig()  # energy_max_jump_kwh = 1000
    return pipe, None


def _audit_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "energy_audit"]


def test_audit_erstwert_und_anstieg(caplog):
    pipe, _pub = _pipeline()
    with caplog.at_level(logging.INFO, logger="energy_audit"):
        asyncio.run(pipe._process(_rx(47000.0)))   # Erstwert
        asyncio.run(pipe._process(_rx(47005.5)))   # plausibler Anstieg
    lines = _audit_lines(caplog)
    assert len(lines) == 2
    assert "UEBERNOMMEN" in lines[0] and "Erstwert" in lines[0]
    assert "raw=47000.0" in lines[0]
    assert "UEBERNOMMEN" in lines[1] and "raw=47005.5" in lines[1]


def test_audit_ausreisser_wird_als_verworfen_protokolliert(caplog):
    pipe, _pub = _pipeline()
    with caplog.at_level(logging.INFO, logger="energy_audit"):
        asyncio.run(pipe._process(_rx(47000.0)))
        asyncio.run(pipe._process(_rx(99472.0)))   # +52472 kWh -> Ausreisser
        asyncio.run(pipe._process(_rx(47172.4)))   # echter Folgewert
    lines = _audit_lines(caplog)
    assert len(lines) == 3
    # Der Ausreisser muss als VERWORFEN mit Sprung-Begruendung dastehen,
    # der Cache-Stand (last) dabei noch auf der Baseline.
    assert "VERWORFEN" in lines[1]
    assert "Sprung" in lines[1]
    assert "last=47000.00" in lines[1]
    # Der echte Folgewert wird uebernommen.
    assert "UEBERNOMMEN" in lines[2] and "raw=47172.4" in lines[2]


def test_audit_rueckwaerts_wird_als_verworfen_protokolliert(caplog):
    pipe, _pub = _pipeline()
    with caplog.at_level(logging.INFO, logger="energy_audit"):
        asyncio.run(pipe._process(_rx(47000.0)))
        asyncio.run(pipe._process(_rx(0.0)))       # Sub-Tarif-0 / Rueckwaerts
    lines = _audit_lines(caplog)
    assert "VERWORFEN" in lines[1]
    assert "Monotonie" in lines[1]
