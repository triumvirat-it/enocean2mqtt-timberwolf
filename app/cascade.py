"""
Globale Meldungs-Kaskadierung — Dedup + RSSI-Tracking + GW-Auswahl beim Senden.

Konzept:
- Empfaengt ein Telegramm von mehreren Gateways: Dedup-Buffer laesst nur das
  erste passieren (anhand Sender-ID + Payload-Hash, Time-Window).
- Pro (Gateway, Sender-ID): RSSI-Werte sammeln (rolling window).
- Beim Senden: passenden GW waehlen anhand Strategie:
    best_rssi    — GW mit bestem zuletzt gemessenen RSSI zum Aktor
    all_gateways — ueber alle aktiven GWs senden
    floor_only   — nur den GW der dem Floor-Tag des Aktors zugeordnet ist
"""
from __future__ import annotations

import hashlib
import logging
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable

from .gateway.base import ReceivedTelegram

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dedup-Buffer
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _DedupEntry:
    """Eintrag im Dedup-Buffer: Telegramm + Empfangs-Zeitstempel."""
    fingerprint: bytes
    timestamp: float
    gateway_name: str


class DedupBuffer:
    """
    Verhindert dass dasselbe Telegramm mehrfach verarbeitet wird wenn
    mehrere Gateways es empfangen haben.

    Fingerprint = (sender_id, rorg, payload, status). Wird das gleiche
    Telegramm innerhalb des window_ms empfangen, gilt es als Duplikat.
    """

    def __init__(self, window_ms: int = 200, max_entries: int = 2000) -> None:
        self.window_s = window_ms / 1000.0
        self.max_entries = max_entries
        self._entries: deque[_DedupEntry] = deque(maxlen=max_entries)
        # Stats
        self.passed = 0
        self.dropped = 0

    @staticmethod
    def _fingerprint(rx: ReceivedTelegram) -> bytes:
        tel = rx.telegram
        # Sender + RORG + Status + Payload
        h = hashlib.blake2b(digest_size=12)
        h.update(tel.sender_id.to_bytes(4, "big"))
        h.update(bytes([tel.rorg, tel.status]))
        h.update(tel.payload)
        return h.digest()

    def is_duplicate(self, rx: ReceivedTelegram) -> bool:
        """
        Prueft ob das Telegramm innerhalb des Time-Windows schon gesehen wurde.

        Side effect: bei Nicht-Duplikat wird der Fingerprint im Buffer
        registriert.
        """
        now = rx.received_at
        fp = self._fingerprint(rx)

        # Alte Eintraege rausschmeissen
        cutoff = now - self.window_s
        while self._entries and self._entries[0].timestamp < cutoff:
            self._entries.popleft()

        # Bekannten Fingerprint suchen
        for entry in self._entries:
            if entry.fingerprint == fp:
                self.dropped += 1
                log.debug(
                    "Dedup-Drop: %s (auch via %s gesehen vor %.0fms)",
                    rx.telegram.sender_id_hex,
                    entry.gateway_name,
                    (now - entry.timestamp) * 1000,
                )
                return True

        # Neu — registrieren
        self._entries.append(_DedupEntry(fp, now, rx.gateway_name))
        self.passed += 1
        return False


# ---------------------------------------------------------------------------
# RSSI-Tabelle
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _RSSISample:
    rssi_dbm: int
    timestamp: float


class RSSITable:
    """
    Track pro (Gateway, Sender-ID) eine kurze Historie der RSSI-Werte.

    Spaeter genutzt fuer:
    - beste GW-Wahl beim Senden an einen bekannten Sender
    - UI: Funkqualitaets-Anzeige
    """

    def __init__(self, history_size: int = 20) -> None:
        self.history_size = history_size
        # Key: (gw_name, sender_id_hex) -> deque of samples
        self._data: dict[tuple[str, str], deque[_RSSISample]] = {}

    def record(self, rx: ReceivedTelegram) -> None:
        if rx.telegram.rssi_dbm is None:
            return
        key = (rx.gateway_name, rx.telegram.sender_id_hex)
        if key not in self._data:
            self._data[key] = deque(maxlen=self.history_size)
        self._data[key].append(_RSSISample(rx.telegram.rssi_dbm, rx.received_at))

    def average_rssi(self, gateway: str, sender_id: str) -> float | None:
        """Mittelwert der letzten RSSI-Werte fuer (gw, sender). None wenn unbekannt."""
        samples = self._data.get((gateway, sender_id.upper()))
        if not samples:
            return None
        return statistics.mean(s.rssi_dbm for s in samples)

    def best_gateway_for(self, sender_id: str, available_gws: Iterable[str]) -> str | None:
        """
        Gibt den GW-Namen aus available_gws zurueck, der zu diesem Sender
        den besten (= naechsten an 0) RSSI-Durchschnitt hat.

        None wenn fuer keinen GW Daten vorliegen.
        """
        sid = sender_id.upper()
        candidates: list[tuple[float, str]] = []
        for gw in available_gws:
            avg = self.average_rssi(gw, sid)
            if avg is not None:
                candidates.append((avg, gw))
        if not candidates:
            return None
        # Hoeher (= weniger negativ) ist besser
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    def snapshot(self) -> dict[str, list[dict]]:
        """Diagnose-Snapshot fuer UI/Logs."""
        out: dict[str, list[dict]] = {}
        for (gw, sid), samples in self._data.items():
            out.setdefault(gw, []).append({
                "sender_id": sid,
                "samples": len(samples),
                "avg_rssi_dbm": round(statistics.mean(s.rssi_dbm for s in samples), 1),
                "last_seen": max(s.timestamp for s in samples),
            })
        return out


# ---------------------------------------------------------------------------
# Gateway-Auswahl beim Senden
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SelectableGateway:
    """Was der Selector ueber einen Gateway wissen muss."""
    name: str
    enabled: bool
    floor_assignments: list[str] = field(default_factory=list)


class GatewaySelector:
    """
    Waehlt fuer eine Sende-Aktion den passenden Gateway aus.

    Strategien:
    - best_rssi: nutze RSSITable, faellt zurueck auf floor wenn unbekannt
    - all_gateways: gibt alle aktiven GWs zurueck (Multiplexing)
    - floor_only: strikt nach floor-Tag-Zuordnung
    """

    def __init__(
        self,
        strategy: str,
        rssi_table: RSSITable,
        gateways: list[SelectableGateway],
    ) -> None:
        self.strategy = strategy
        self.rssi_table = rssi_table
        self.gateways = gateways

    def _active_gw_names(self) -> list[str]:
        return [gw.name for gw in self.gateways if gw.enabled]

    def _gw_by_floor(self, floors: list[str]) -> list[str]:
        """Findet alle GWs die auf eines der Floor-Tags hoeren."""
        matched: list[str] = []
        for gw in self.gateways:
            if not gw.enabled:
                continue
            if any(f in gw.floor_assignments for f in floors):
                matched.append(gw.name)
        return matched

    def choose(
        self,
        sender_id: str,
        floors: list[str] | None = None,
    ) -> list[str]:
        """
        Gibt die Liste der Gateways zurueck, ueber die das Telegramm gesendet
        werden soll.

        - sender_id: Aktor-Adresse (zum RSSI-Lookup)
        - floors: Floor-Tags des Aktors (fuer floor-Fallback)
        """
        active = self._active_gw_names()
        if not active:
            return []

        if self.strategy == "all_gateways":
            return active

        if self.strategy == "floor_only":
            return self._gw_by_floor(floors or [])

        # Default: best_rssi mit floor-Fallback
        best = self.rssi_table.best_gateway_for(sender_id, active)
        if best:
            return [best]
        # Fallback: floor-Zuordnung
        by_floor = self._gw_by_floor(floors or [])
        if by_floor:
            return by_floor[:1]
        # Letzter Fallback: erster aktiver GW
        return active[:1]


# ---------------------------------------------------------------------------
# Cascade-Container fuer Pipeline-Integration
# ---------------------------------------------------------------------------


@dataclass
class CascadeStats:
    received_total: int = 0
    duplicates_dropped: int = 0
    passed_through: int = 0


class Cascade:
    """
    High-level Bundle fuer die Pipeline: Dedup + RSSI-Tracking.

    Verwendung:
        cascade = Cascade(dedup_window_ms=200, rssi_history=20)
        if not cascade.handle(rx):
            return  # Duplikat, ignorieren
        ...
    """

    def __init__(self, dedup_window_ms: int = 200, rssi_history: int = 20) -> None:
        self.dedup = DedupBuffer(window_ms=dedup_window_ms)
        self.rssi = RSSITable(history_size=rssi_history)
        self.stats = CascadeStats()

    def handle(self, rx: ReceivedTelegram) -> bool:
        """
        Verarbeitet ein eingehendes Telegramm.

        Returns True wenn das Telegramm durchgereicht werden soll,
        False wenn es ein Duplikat ist und ignoriert werden kann.

        Side effect: RSSI-Tabelle wird IMMER aktualisiert (auch bei
        Duplikaten — wir lernen die Funkqualitaet aus jedem Empfang).
        """
        self.stats.received_total += 1
        # RSSI immer mitschneiden (Multi-GW-Diagnose)
        self.rssi.record(rx)

        if self.dedup.is_duplicate(rx):
            self.stats.duplicates_dropped += 1
            return False

        self.stats.passed_through += 1
        return True
