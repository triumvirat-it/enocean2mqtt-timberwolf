"""Tests fuer den Voll-Telegramm-Mitschnitt (app/telegram_audit.py)."""
from __future__ import annotations

import asyncio
import gzip
import os
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from app.telegram_audit import TelegramAuditWriter


def _rx(payload_hex: str, sender="05000012", gw="LAN Gateway UG",
        rorg="BS4", rssi=-78, ts=None):
    """Minimales ReceivedTelegram-Double mit genau den Feldern die der
    Writer liest."""
    if ts is None:
        ts = datetime(2026, 7, 16, 3, 16, 52, 50000).timestamp()
    tel = SimpleNamespace(
        sender_id_hex=sender,
        rorg_name=rorg,
        rssi_dbm=rssi,
        payload=bytes.fromhex(payload_hex),
    )
    return SimpleNamespace(received_at=ts, gateway_name=gw, telegram=tel)


# ---- Formatierung ------------------------------------------------------

def test_format_line_enthaelt_rohpayload_und_dekodiert():
    # Das Gas-Phantom: raw=0800000C -> flow_m3h=524288
    rx = _rx("0800000C")
    line = TelegramAuditWriter.format_line(
        rx, {"tariff": 0, "flow_m3h": 524288.0}, device_name="Gasverbrauch Leistung"
    )
    assert "raw=0800000C" in line
    assert "id=05000012" in line
    assert "gw=LAN Gateway UG" in line
    assert "rssi=-78" in line
    assert "BS4" in line
    assert '"Gasverbrauch Leistung"' in line
    assert '"flow_m3h":524288' in line
    assert line.startswith("2026-07-16 03:16:52.05 ")


def test_format_line_filtert_interne_keys():
    rx = _rx("03870909")
    line = TelegramAuditWriter.format_line(rx, {"volume_m3": 23117.7, "_eep": "A5-12-02"})
    assert "_eep" not in line
    assert "volume_m3" in line


def test_format_line_ohne_device_zeigt_strich():
    rx = _rx("0800000C")
    line = TelegramAuditWriter.format_line(rx, {"x": 1}, device_name=None)
    assert " dev=- " in line


# ---- Puffern + Schreiben ----------------------------------------------

def test_record_puffert_und_write_sync_schreibt(tmp_path):
    w = TelegramAuditWriter(tmp_path / "telegram_audit.log")
    w.record(_rx("0800000C"), {"flow_m3h": 524288.0})
    w.record(_rx("03870909"), {"volume_m3": 23117.7})
    assert len(w._buf) == 2
    lines = w._buf
    w._buf = []
    w._write_sync(lines)
    content = (tmp_path / "telegram_audit.log").read_text(encoding="utf-8")
    assert content.count("\n") == 2
    assert "524288" in content
    assert "23117.7" in content


def test_disabled_writer_puffert_nicht(tmp_path):
    w = TelegramAuditWriter(tmp_path / "telegram_audit.log", enabled=False)
    w.record(_rx("0800000C"), {"flow_m3h": 524288.0})
    assert w._buf == []


def test_record_wirft_nie(tmp_path):
    """Ein kaputtes rx-Objekt darf den Empfangspfad nicht toeten."""
    w = TelegramAuditWriter(tmp_path / "telegram_audit.log")
    w.record(object(), {"x": 1})   # kein .telegram / .received_at
    assert w._buf == []            # geschluckt, kein Eintrag


# ---- Rotation + gzip + Aufbewahrung -----------------------------------

def test_rollover_archiviert_alten_tag_als_gzip(tmp_path):
    w = TelegramAuditWriter(tmp_path / "telegram_audit.log")
    active = tmp_path / "telegram_audit.log"
    active.write_text("gestrige zeile\n", encoding="utf-8")
    # mtime auf gestern setzen -> gilt als "stale"
    yesterday = datetime.now() - timedelta(days=1)
    os.utime(active, (yesterday.timestamp(), yesterday.timestamp()))

    w._rollover_if_stale()

    assert not active.exists()  # aktive Datei wurde archiviert
    arch = tmp_path / f"telegram_audit.{yesterday.date().isoformat()}.log.gz"
    assert arch.exists()
    with gzip.open(arch, "rt", encoding="utf-8") as f:
        assert f.read() == "gestrige zeile\n"


def test_rollover_laesst_heutige_datei_in_ruhe(tmp_path):
    w = TelegramAuditWriter(tmp_path / "telegram_audit.log")
    active = tmp_path / "telegram_audit.log"
    active.write_text("heute\n", encoding="utf-8")  # mtime = jetzt
    w._rollover_if_stale()
    assert active.exists()
    assert list(tmp_path.glob("*.gz")) == []


def test_prune_loescht_nur_zu_alte_archive(tmp_path):
    w = TelegramAuditWriter(tmp_path / "telegram_audit.log", retention_days=45)
    today = date.today()
    alt = today - timedelta(days=60)      # ausserhalb -> weg
    frisch = today - timedelta(days=10)   # innerhalb -> bleibt
    p_alt = tmp_path / f"telegram_audit.{alt.isoformat()}.log.gz"
    p_frisch = tmp_path / f"telegram_audit.{frisch.isoformat()}.log.gz"
    p_alt.write_bytes(b"x")
    p_frisch.write_bytes(b"x")

    w._prune()

    assert not p_alt.exists()
    assert p_frisch.exists()


# ---- Async Lifecycle (Ende-zu-Ende) -----------------------------------

def test_start_flush_stop_schreibt_datei(tmp_path):
    """Treibt start()/record()/stop() ueber asyncio.run — ohne pytest-asyncio,
    das im Projekt nicht konfiguriert ist."""
    async def scenario():
        w = TelegramAuditWriter(tmp_path / "telegram_audit.log", flush_interval=0.05)
        await w.start()
        w.record(_rx("0800000C"), {"flow_m3h": 524288.0})
        await asyncio.sleep(0.12)   # mindestens ein Flush-Intervall
        await w.stop()              # stop() macht am Ende einen finalen Flush

    asyncio.run(scenario())
    content = (tmp_path / "telegram_audit.log").read_text(encoding="utf-8")
    assert "raw=0800000C" in content
    assert "524288" in content
