"""Tests für DeviceRegistry und MQTT-Slug."""
from __future__ import annotations

import tempfile
from pathlib import Path

from app.devices import Device, DeviceChannel, DeviceRegistry
from app.mqtt_client import slugify


def test_slugify_basic():
    assert slugify("Haus A & Garten/Wohnzimmer") == "haus_a_und_garten_wohnzimmer"
    assert slugify("LAN Gateway1") == "lan_gateway1"
    assert slugify("Schaltaktor 0 — Couch") == "schaltaktor_0_couch"


def test_slugify_umlaute():
    assert slugify("Büro") == "buero"
    assert slugify("Köche") == "koeche"
    assert slugify("Straße") == "strasse"


def test_device_registry_lookup():
    d = Device(
        device_id="rollo_wz",
        name="Rollo WZ",
        room="Wohnen",
        channels=[
            DeviceChannel(channel_id="1", name="Rollo", enocean_id="01A00001", eep="A5-3F-7F")
        ],
    )
    reg = DeviceRegistry([d])
    match = reg.lookup_by_sender_id("01A00001")
    assert match is not None
    device, channel = match
    assert device.device_id == "rollo_wz"
    assert channel.eep == "A5-3F-7F"


def test_device_registry_roundtrip():
    d = Device(
        device_id="x",
        name="X",
        channels=[DeviceChannel(channel_id="1", name="ch1", enocean_id="DEADBEEF", eep="F6-02-01")],
    )
    reg = DeviceRegistry([d])
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "devices.yaml"
        reg.save(p)
        reg2 = DeviceRegistry.load(p)
        assert len(reg2) == 1
        m = reg2.lookup_by_sender_id("deadbeef")
        assert m is not None


def test_device_registry_remove_clears_index():
    d = Device(
        device_id="x",
        name="X",
        channels=[DeviceChannel(channel_id="1", name="c", enocean_id="DEADBEEF", eep="F6-02-01")],
    )
    reg = DeviceRegistry([d])
    assert reg.lookup_by_sender_id("DEADBEEF") is not None
    assert reg.remove("x") is True
    assert reg.lookup_by_sender_id("DEADBEEF") is None
