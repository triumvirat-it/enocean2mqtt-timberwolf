"""
Tests fuer das Verwurf-Protokoll (app/reject_audit.py) und die Verdrahtung in
der Pipeline: JEDES verworfene Feld muss mit dem richtigen Grund protokolliert
werden. Deckt den byte-genauen DB3-Bitfehler-Check, die Magnitude-Obergrenze
und die Energie-Monotonie-Verwuerfe ab.
"""
from __future__ import annotations

import asyncio
import gzip
import os
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from app.cascade import Cascade
from app.config import DefaultsConfig
from app.devices import Device, DeviceChannel, DeviceRegistry
from app.eep import register_default_profiles
from app.gateway.base import ReceivedTelegram
from app.gateway.esp3 import RORG, RadioTelegram
from app.reject_audit import RejectAuditWriter


# ======================================================================
# Writer-Basis (Formatierung, Puffern, Rotation) — analog telegram_audit
# ======================================================================

def _rx_double(payload_hex: str, sender="05000012", gw="LAN Gateway UG",
               rorg="BS4", rssi=-78, ts=None):
    if ts is None:
        ts = datetime(2026, 7, 16, 3, 16, 52, 50000).timestamp()
    tel = SimpleNamespace(
        sender_id_hex=sender, rorg_name=rorg, rssi_dbm=rssi,
        payload=bytes.fromhex(payload_hex),
    )
    return SimpleNamespace(received_at=ts, gateway_name=gw, telegram=tel)


def test_format_line_enthaelt_grund_und_rohpayload():
    rx = _rx_double("0800000C")
    line = RejectAuditWriter.format_line(
        rx, "flow_m3h", 524288.0, "db3_nonzero",
        eep="A5-12-02", detail="DB3=0x08",
    )
    assert "raw=0800000C" in line
    assert "id=05000012" in line
    assert "field=flow_m3h" in line
    assert "value=524288.0" in line
    assert "reason=db3_nonzero" in line
    assert "eep=A5-12-02" in line
    assert "detail=DB3=0x08" in line
    assert line.startswith("2026-07-16 03:16:52.05 ")


def test_record_puffert_und_schreibt(tmp_path):
    w = RejectAuditWriter(tmp_path / "reject_audit.log")
    w.record(_rx_double("0800000C"), "flow_m3h", 524288.0, "db3_nonzero")
    w.record(_rx_double("40C2440C"), "current_w", 4244036.0, "db3_nonzero")
    assert len(w._buf) == 2
    lines = w._buf
    w._buf = []
    w._write_sync(lines)
    content = (tmp_path / "reject_audit.log").read_text(encoding="utf-8")
    assert content.count("\n") == 2
    assert "reason=db3_nonzero" in content


def test_record_wirft_nie(tmp_path):
    w = RejectAuditWriter(tmp_path / "reject_audit.log")
    w.record(object(), "x", 1, "grund")   # kaputtes rx
    assert w._buf == []


def test_rollover_und_prune(tmp_path):
    w = RejectAuditWriter(tmp_path / "reject_audit.log", retention_days=45)
    active = tmp_path / "reject_audit.log"
    active.write_text("gestern\n", encoding="utf-8")
    yesterday = datetime.now() - timedelta(days=1)
    os.utime(active, (yesterday.timestamp(), yesterday.timestamp()))
    w._rollover_if_stale()
    arch = tmp_path / f"reject_audit.{yesterday.date().isoformat()}.log.gz"
    assert not active.exists() and arch.exists()
    with gzip.open(arch, "rt", encoding="utf-8") as f:
        assert f.read() == "gestern\n"
    # prune
    alt = date.today() - timedelta(days=60)
    p_alt = tmp_path / f"reject_audit.{alt.isoformat()}.log.gz"
    p_alt.write_bytes(b"x")
    w._prune()
    assert not p_alt.exists()


# ======================================================================
# Pipeline-Integration: der richtige Grund landet im Protokoll
# ======================================================================

class _CapturePublisher:
    def __init__(self):
        self.devices = []
        self.raw = []

    async def publish_raw(self, gw_name, sender_id, payload):
        self.raw.append((sender_id, payload))

    async def publish_device(self, device, channel, payload):
        self.devices.append((device.device_id, channel.channel_id, payload))


class _FakeReject:
    """Faengt die record()-Aufrufe der Pipeline ab (gleiche Signatur wie
    RejectAuditWriter.record)."""
    def __init__(self):
        self.calls = []

    def record(self, rx, field, value, reason, eep=None, detail="", last_good=None):
        self.calls.append(dict(field=field, value=value, reason=reason,
                               eep=eep, detail=detail, last_good=last_good))


def _a5_payload(value: int, db0: int) -> bytes:
    return bytes([(value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF, db0])


def _rx(sender_int: int, value: int, db0: int, rorg=RORG.BS4) -> ReceivedTelegram:
    tel = RadioTelegram(
        rorg=rorg, payload=_a5_payload(value, db0),
        sender_id=sender_int, status=0x00, sub_tel=0,
        destination_id=0xFFFFFFFF, rssi_dbm=-78, security_level=0,
    )
    return ReceivedTelegram(gateway_name="gw_ug", telegram=tel)


def _pipeline(device: Device, defaults=None):
    from app.pipeline import TelegramPipeline
    register_default_profiles()
    pub = _CapturePublisher()
    pipe = TelegramPipeline(
        manager=object(), publisher=pub,
        devices=DeviceRegistry([device]), cascade=Cascade(),
    )
    pipe.defaults = defaults or DefaultsConfig()
    rej = _FakeReject()
    pipe.reject_audit = rej
    return pipe, pub, rej


def _gas_device():
    return Device(
        device_id="gas", name="Gaszaehler",
        channels=[DeviceChannel(
            channel_id="flow", name="Gasverbrauch Leistung",
            enocean_id="05000012", eep="A5-12-02",
            meta={"field": "flow_m3h", "tele_channel": "0"})],
    )


def _strom_device():
    return Device(
        device_id="strom", name="Stromzaehler",
        channels=[DeviceChannel(
            channel_id="energie", name="Tagtarif Energie",
            enocean_id="05000081", eep="A5-12-01",
            meta={"field": "energy_kwh", "tele_channel": "0"})],
    )


def test_db3_bitfehler_wird_protokolliert():
    pipe, pub, rej = _pipeline(_gas_device())
    # 0x080000, db0=0x0C (Flow) -> DB3=0x08 -> db3_nonzero
    asyncio.run(pipe._process(_rx(0x05000012, 0x080000, 0x0C)))
    assert len(rej.calls) == 1
    c = rej.calls[0]
    assert c["reason"] == "db3_nonzero"
    assert c["field"] == "flow_m3h"
    assert c["eep"] == "A5-12-02"
    assert "DB3=0x08" in c["detail"]


def test_over_max_wird_protokolliert():
    pipe, pub, rej = _pipeline(_gas_device())
    # 200 m^3/h: DB3=0x00 (byte-sauber), aber > Grenze 100 -> over_max
    asyncio.run(pipe._process(_rx(0x05000012, 200, 0x0C)))
    assert len(rej.calls) == 1
    assert rej.calls[0]["reason"] == "over_max_flow_m3h"


def test_sauberer_wert_erzeugt_keinen_reject():
    pipe, pub, rej = _pipeline(_gas_device())
    asyncio.run(pipe._process(_rx(0x05000012, 7, 0x0C)))   # 7 m^3/h, DB3=0
    assert rej.calls == []


def test_energy_jump_wird_protokolliert():
    pipe, pub, rej = _pipeline(_strom_device())
    # Erstwert setzt den Cache (energy_kwh = raw*0.1): raw 1000 -> 100 kWh
    asyncio.run(pipe._process(_rx(0x05000081, 1000, 0x09)))
    assert rej.calls == []
    # Sprung: raw 50000 -> 5000 kWh, +4900 > max_jump 1000 -> energy_jump
    asyncio.run(pipe._process(_rx(0x05000081, 50000, 0x09)))
    assert len(rej.calls) == 1
    assert rej.calls[0]["reason"] == "energy_jump"
    assert rej.calls[0]["field"] == "energy_kwh"
