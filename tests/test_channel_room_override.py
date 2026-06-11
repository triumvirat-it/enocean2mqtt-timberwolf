"""M95: per-Channel Etage/Raum-Override treibt den MQTT-Topic."""
from __future__ import annotations

from app.config import MQTTConfig
from app.devices import Device, DeviceChannel
from app.mqtt_client import MQTTPublisher


def _pub():
    return MQTTPublisher(MQTTConfig())


def _dali():
    # DALI-Dimmer: sitzt im Technikraum, schaltet aber in versch. Raeume.
    return Device(
        device_id="dali",
        name="DALI Dimmer",
        floor="Technik",
        room="Technik/Schrank",
        channels=[
            DeviceChannel(channel_id="1", name="Licht Bad", eep="A5-38-08",
                          floor="OG", room="Badezimmer"),          # Override
            DeviceChannel(channel_id="2", name="Licht Flur", eep="A5-38-08"),  # erbt
        ],
    )


def test_channel_override_drives_topic():
    pub, dev = _pub(), _dali()
    t = pub.device_topic(dev, dev.channels[0])
    assert "/og/badezimmer/" in t          # Channel-Raum, nicht Geraete-Standort
    assert "schrank" not in t
    assert "technik" not in t


def test_channel_without_override_inherits_device():
    pub, dev = _pub(), _dali()
    t = pub.device_topic(dev, dev.channels[1])
    assert "/technik/schrank/" in t        # erbt floor/room vom Device


def test_override_defaults_empty():
    ch = DeviceChannel(channel_id="1", name="x", eep="A5-38-08")
    assert ch.floor == ""
    assert ch.room == ""


def test_whitespace_override_falls_back_to_device():
    pub = _pub()
    dev = Device(
        device_id="d", name="D", floor="EG", room="Kueche",
        channels=[DeviceChannel(channel_id="1", name="c", eep="A5-38-08",
                                floor="   ", room="")],
    )
    t = pub.device_topic(dev, dev.channels[0])
    assert "/eg/kueche/" in t               # nur Whitespace -> Device-Fallback
