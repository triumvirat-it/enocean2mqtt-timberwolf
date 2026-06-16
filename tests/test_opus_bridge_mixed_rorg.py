"""
OPUS Bridge UP-eSchalter: eine RX-ID traegt ZWEI Channels mit
UNTERSCHIEDLICHEN RORGs — einen D2-01-Aktor-Status und ein aufgesetztes
F6-02-PTM-Wippensignal. Die Pipeline muss jedes Telegramm mit dem zum
tatsaechlichen RORG passenden Decoder dekodieren und nur an den passenden
Channel publishen.

Regression: vorher dekodierte die Pipeline pro Sender-ID nur EINMAL mit dem
EEP des ERSTEN Channels (Annahme "alle gleich pro Sender"). Ein RPS-Wippen-
telegramm landete so im D2-01-Decoder und kam als {"cmd": 0} raus statt als
press_top/press_bottom.
"""
from __future__ import annotations

import asyncio

from app.cascade import Cascade
from app.devices import Device, DeviceChannel, DeviceRegistry
from app.eep import register_default_profiles
from app.gateway.base import ReceivedTelegram
from app.gateway.esp3 import RORG, RadioTelegram
from app.pipeline import TelegramPipeline


class _CapturePublisher:
    """Faengt publish_raw + publish_device ab statt zu senden."""

    def __init__(self) -> None:
        self.devices: list[tuple[str, str, dict]] = []  # (device_id, channel_id, payload)
        self.raw: list[tuple[str, dict]] = []           # (sender_id, payload)

    async def publish_raw(self, gw_name, sender_id, payload) -> None:
        self.raw.append((sender_id, payload))

    async def publish_device(self, device, channel, payload) -> None:
        self.devices.append((device.device_id, channel.channel_id, payload))


def _opus_bridge_device() -> Device:
    # Aktor-Status (D2-01) und aufgesetztes PTM (F6-02) auf derselben RX-ID.
    return Device(
        device_id="opus_bridge_flur",
        name="Eingang Wand Flur",
        channels=[
            DeviceChannel(channel_id="1.1", name="Eingang Wand Flur",
                          enocean_id="019D00E2", eep="D2-01-XX"),
            DeviceChannel(channel_id="1.2", name="PTM Funktaster",
                          enocean_id="019D00E2", eep="F6-02-01"),
        ],
    )


def _rx(rorg: int, payload: bytes, status: int) -> ReceivedTelegram:
    tel = RadioTelegram(
        rorg=rorg, payload=payload, sender_id=0x019D00E2, status=status,
        sub_tel=0, destination_id=0xFFFFFFFF, rssi_dbm=-68, security_level=0,
    )
    return ReceivedTelegram(gateway_name="gw1", telegram=tel)


def _pipeline() -> tuple[TelegramPipeline, _CapturePublisher]:
    register_default_profiles()
    pub = _CapturePublisher()
    pipe = TelegramPipeline(
        manager=object(), publisher=pub,            # manager wird in _process nicht genutzt
        devices=DeviceRegistry([_opus_bridge_device()]),
        cascade=Cascade(),
    )
    return pipe, pub


def test_rps_ptm_telegram_decoded_as_f6_not_d2():
    # PTM drueckt Wippe A oben: RPS-Byte 0x30 (R1='A0'), N-Message (status 0x30).
    pipe, pub = _pipeline()
    asyncio.run(pipe._process(_rx(RORG.RPS, b"\x30", 0x30)))

    # Nur der F6-PTM-Channel (1.2) wird published, NICHT der D2-Aktor (1.1).
    chans = {c for _d, c, _p in pub.devices}
    assert chans == {"1.2"}, f"erwartete nur PTM-Channel, war {chans}"

    payload = next(p for _d, c, p in pub.devices if c == "1.2")
    assert payload.get("rocker_action") == "press_top"
    assert payload.get("event") == "A_top"
    # Kein D2-Muell mehr.
    assert "cmd" not in payload


def test_d2_actuator_status_decoded_as_d2_not_f6():
    # Aktor meldet Status-Response (cmd=0x04), Ausgang AN.
    pipe, pub = _pipeline()
    asyncio.run(pipe._process(_rx(RORG.VLD, b"\x04\x00\x01", 0x00)))

    # Nur der D2-Aktor-Channel (1.1) wird published, NICHT das PTM (1.2).
    chans = {c for _d, c, _p in pub.devices}
    assert chans == {"1.1"}, f"erwartete nur Aktor-Channel, war {chans}"

    payload = next(p for _d, c, p in pub.devices if c == "1.1")
    assert payload.get("state") == "ON"
    assert payload.get("on") is True
    # Keine Wippen-Felder.
    assert "rocker_action" not in payload


def test_ptm_on_true_on_ein_press():
    # BI-Press (payload 0x50), Default-Polung "I" -> EIN-Seite -> on:true.
    pipe, pub = _pipeline()
    asyncio.run(pipe._process(_rx(RORG.RPS, b"\x50", 0x30)))
    payload = next(p for _d, c, p in pub.devices if c == "1.2")
    assert payload.get("rocker_1") == "BI"
    assert payload.get("on") is True


def test_ptm_on_false_on_aus_press():
    # B0-Press (payload 0x70), Default-Polung "I" -> AUS-Seite -> on:false.
    pipe, pub = _pipeline()
    asyncio.run(pipe._process(_rx(RORG.RPS, b"\x70", 0x30)))
    payload = next(p for _d, c, p in pub.devices if c == "1.2")
    assert payload.get("rocker_1") == "B0"
    assert payload.get("on") is False


def test_ptm_on_retained_across_release():
    # Press (on:true), dann Release: das Release-Telegramm darf den Boolean nicht
    # verlieren — er muss aus dem Cache erhalten bleiben (retained Topic!).
    pipe, pub = _pipeline()
    asyncio.run(pipe._process(_rx(RORG.RPS, b"\x50", 0x30)))  # BI press -> on:true
    asyncio.run(pipe._process(_rx(RORG.RPS, b"\x00", 0x20)))  # Release (kein energy bow)
    rel = [p for _d, c, p in pub.devices if c == "1.2"][-1]
    assert rel.get("event") == "release"
    assert rel.get("on") is True


def test_ptm_polarity_override_per_channel():
    # Kanal-Override meta.ptm_on_press="0" dreht die Polung: BI-Press ist dann
    # die AUS-Seite -> on:false (statt true bei Default "I").
    register_default_profiles()
    dev = Device(
        device_id="opus", name="x",
        channels=[
            DeviceChannel(channel_id="1.2", name="PTM", enocean_id="019D00E2",
                          eep="F6-02-01", meta={"ptm_on_press": "0"}),
        ],
    )
    pub = _CapturePublisher()
    pipe = TelegramPipeline(manager=object(), publisher=pub,
                            devices=DeviceRegistry([dev]), cascade=Cascade())
    asyncio.run(pipe._process(_rx(RORG.RPS, b"\x50", 0x30)))  # BI press, pol "0" -> AUS
    payload = next(p for _d, c, p in pub.devices if c == "1.2")
    assert payload.get("on") is False


def test_same_rorg_multichannel_unaffected():
    # Gegenprobe: zwei Channels mit GLEICHEM RORG (FT55-Stil) — RORG-Routing
    # darf hier NICHTS aendern, beide Channels sehen das Telegramm weiter.
    register_default_profiles()
    dev = Device(
        device_id="ft55", name="Wippe",
        channels=[
            DeviceChannel(channel_id="A", name="Wippe A", enocean_id="FFAA0001",
                          eep="F6-02-01", meta={"tele_channel": "A"}),
            DeviceChannel(channel_id="B", name="Wippe B", enocean_id="FFAA0001",
                          eep="F6-02-01", meta={"tele_channel": "B"}),
        ],
    )
    pub = _CapturePublisher()
    pipe = TelegramPipeline(manager=object(), publisher=pub,
                            devices=DeviceRegistry([dev]), cascade=Cascade())
    tel = RadioTelegram(
        rorg=RORG.RPS, payload=b"\x30", sender_id=0xFFAA0001, status=0x30,
        sub_tel=0, destination_id=0xFFFFFFFF, rssi_dbm=-60, security_level=0,
    )
    asyncio.run(pipe._process(ReceivedTelegram(gateway_name="gw1", telegram=tel)))
    # Wippe A (rocker_side='A') wird published; Wippe B per tele_channel gefiltert.
    chans = {c for _d, c, _p in pub.devices}
    assert "A" in chans
