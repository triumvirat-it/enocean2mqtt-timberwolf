"""Tests fuer Dedup, RSSI-Tracking und Gateway-Auswahl."""
from __future__ import annotations

import time

import pytest

from app.cascade import (
    Cascade,
    DedupBuffer,
    GatewaySelector,
    RSSITable,
    SelectableGateway,
)
from app.gateway.base import ReceivedTelegram
from app.gateway.esp3 import RORG, RadioTelegram


def _make_rx(
    gw: str = "gw1",
    sender_id: int = 0xDEADBEEF,
    rssi: int = -65,
    payload: bytes = b"\x30",
    received_at: float | None = None,
) -> ReceivedTelegram:
    tel = RadioTelegram(
        rorg=RORG.RPS,
        payload=payload,
        sender_id=sender_id,
        status=0x30,
        sub_tel=0,
        destination_id=0xFFFFFFFF,
        rssi_dbm=rssi,
        security_level=0,
    )
    return ReceivedTelegram(
        gateway_name=gw,
        telegram=tel,
        received_at=received_at if received_at is not None else time.time(),
    )


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


def test_dedup_first_telegram_passes():
    buf = DedupBuffer(window_ms=200)
    assert buf.is_duplicate(_make_rx()) is False
    assert buf.passed == 1
    assert buf.dropped == 0


def test_dedup_second_same_telegram_dropped():
    buf = DedupBuffer(window_ms=200)
    now = time.time()
    assert buf.is_duplicate(_make_rx(gw="gw1", received_at=now)) is False
    # Gleiches Telegramm 50ms spaeter via anderem GW -> drop
    assert buf.is_duplicate(_make_rx(gw="gw2", received_at=now + 0.05)) is True
    assert buf.passed == 1
    assert buf.dropped == 1


def test_dedup_after_window_passes_again():
    buf = DedupBuffer(window_ms=100)
    now = time.time()
    buf.is_duplicate(_make_rx(received_at=now))
    # Nach 200ms (window=100ms) ist alter Eintrag raus
    assert buf.is_duplicate(_make_rx(received_at=now + 0.2)) is False


def test_dedup_different_payload_passes():
    buf = DedupBuffer(window_ms=200)
    now = time.time()
    buf.is_duplicate(_make_rx(payload=b"\x30", received_at=now))
    # Andere Payload -> kein Duplikat
    assert buf.is_duplicate(_make_rx(payload=b"\x70", received_at=now + 0.05)) is False


def test_dedup_different_sender_passes():
    buf = DedupBuffer(window_ms=200)
    now = time.time()
    buf.is_duplicate(_make_rx(sender_id=0x11111111, received_at=now))
    assert buf.is_duplicate(_make_rx(sender_id=0x22222222, received_at=now + 0.05)) is False


# ---------------------------------------------------------------------------
# RSSI-Tabelle
# ---------------------------------------------------------------------------


def test_rssi_records_per_gw_sender():
    t = RSSITable()
    t.record(_make_rx(gw="gw1", sender_id=0xAAAA, rssi=-60))
    t.record(_make_rx(gw="gw1", sender_id=0xAAAA, rssi=-65))
    t.record(_make_rx(gw="gw2", sender_id=0xAAAA, rssi=-80))

    avg_gw1 = t.average_rssi("gw1", "0000AAAA")
    avg_gw2 = t.average_rssi("gw2", "0000AAAA")
    assert -65 < avg_gw1 < -60 or avg_gw1 == -62.5
    assert avg_gw2 == -80


def test_rssi_best_gateway_picks_strongest():
    t = RSSITable()
    t.record(_make_rx(gw="weit_weg", sender_id=0xCC, rssi=-85))
    t.record(_make_rx(gw="nah_dran", sender_id=0xCC, rssi=-55))
    t.record(_make_rx(gw="mittel", sender_id=0xCC, rssi=-70))

    best = t.best_gateway_for("000000CC", ["weit_weg", "nah_dran", "mittel"])
    assert best == "nah_dran"


def test_rssi_best_gateway_filters_available():
    t = RSSITable()
    t.record(_make_rx(gw="offline_gw", sender_id=0xDD, rssi=-50))
    t.record(_make_rx(gw="online_gw", sender_id=0xDD, rssi=-75))

    # offline_gw nicht in available_gws -> online_gw gewinnt trotz schlechterem RSSI
    best = t.best_gateway_for("000000DD", ["online_gw"])
    assert best == "online_gw"


def test_rssi_returns_none_for_unknown_sender():
    t = RSSITable()
    assert t.best_gateway_for("ABCDEF01", ["gw1"]) is None


# ---------------------------------------------------------------------------
# Gateway-Selector
# ---------------------------------------------------------------------------


def test_selector_all_gateways():
    rssi = RSSITable()
    gws = [
        SelectableGateway("gw1", True, ["Wohnen"]),
        SelectableGateway("gw2", True, ["Buero"]),
        SelectableGateway("gw3", False, ["Garten"]),
    ]
    sel = GatewaySelector("all_gateways", rssi, gws)
    assert sel.choose("00FFFFFF") == ["gw1", "gw2"]  # gw3 ist disabled


def test_selector_floor_only_picks_by_tag():
    rssi = RSSITable()
    gws = [
        SelectableGateway("gw1", True, ["Wohnen"]),
        SelectableGateway("gw2", True, ["Buero"]),
    ]
    sel = GatewaySelector("floor_only", rssi, gws)
    assert sel.choose("00FFFFFF", floors=["Buero"]) == ["gw2"]
    assert sel.choose("00FFFFFF", floors=["Wohnen"]) == ["gw1"]


def test_selector_best_rssi_uses_table():
    rssi = RSSITable()
    rssi.record(_make_rx(gw="gw1", sender_id=0xAAAAAA, rssi=-80))
    rssi.record(_make_rx(gw="gw2", sender_id=0xAAAAAA, rssi=-55))

    gws = [
        SelectableGateway("gw1", True, ["Wohnen"]),
        SelectableGateway("gw2", True, ["Wohnen"]),
    ]
    sel = GatewaySelector("best_rssi", rssi, gws)
    chosen = sel.choose("00AAAAAA")
    assert chosen == ["gw2"]


def test_selector_best_rssi_falls_back_to_floor():
    rssi = RSSITable()  # leer
    gws = [
        SelectableGateway("gw1", True, ["Wohnen"]),
        SelectableGateway("gw2", True, ["Buero"]),
    ]
    sel = GatewaySelector("best_rssi", rssi, gws)
    # Kein RSSI -> Fallback auf Floor
    assert sel.choose("00FFFFFF", floors=["Buero"]) == ["gw2"]


# ---------------------------------------------------------------------------
# Cascade-Container (Integration)
# ---------------------------------------------------------------------------


def test_cascade_passes_first_drops_dup_keeps_rssi():
    cascade = Cascade(dedup_window_ms=200)
    now = time.time()
    rx1 = _make_rx(gw="gw1", sender_id=0x123456, rssi=-60, received_at=now)
    rx2 = _make_rx(gw="gw2", sender_id=0x123456, rssi=-72, received_at=now + 0.05)

    assert cascade.handle(rx1) is True   # passes through
    assert cascade.handle(rx2) is False  # drop (Duplikat)

    # Trotz Drop wurde RSSI fuer beide GWs aufgenommen
    assert cascade.rssi.average_rssi("gw1", "00123456") == -60
    assert cascade.rssi.average_rssi("gw2", "00123456") == -72

    assert cascade.stats.received_total == 2
    assert cascade.stats.duplicates_dropped == 1
    assert cascade.stats.passed_through == 1
