"""
LAN-Gateway-Implementierung: TCM310-LAN-GW oder TCM515-LAN-GW über TCP-Socket (ESP3).
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..config import GatewayConfig
from .base import Gateway, GatewayStatus, ReceivedTelegram
from .esp3 import (
    ESP3Packet,
    ESP3StreamParser,
    PacketType,
    decode_any_packet,
    decode_radio_erp1,
)

log = logging.getLogger(__name__)


class LANGateway(Gateway):
    """ESP3 über TCP — TCM310/TCM515 LAN-Gateway."""

    READ_CHUNK = 1024
    CONNECT_BACKOFF_INITIAL = 1.0
    CONNECT_BACKOFF_MAX = 30.0

    def __init__(self, cfg: GatewayConfig) -> None:
        super().__init__(cfg.name)
        self.cfg = cfg
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._writer_lock = asyncio.Lock()
        # M88: Chip-ID des Modems (aus CO_RD_VERSION) — fuer ReMan-Pairing.
        self.chip_id: int | None = None
        self._awaiting_version = False
        if not cfg.enabled:
            self.status = GatewayStatus.DISABLED

    async def _run_loop(self) -> None:
        if not self.cfg.enabled:
            log.info("[%s] deaktiviert — überspringe", self.name)
            return

        backoff = self.CONNECT_BACKOFF_INITIAL
        while not self._stop_event.is_set():
            try:
                await self._connect_and_read()
                backoff = self.CONNECT_BACKOFF_INITIAL  # erfolgreiche Verbindung → reset
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.status = GatewayStatus.ERROR
                self.diag.last_error = str(exc)
                log.warning(
                    "[%s] Verbindungsfehler: %s — neuer Versuch in %.1fs",
                    self.name, exc, backoff,
                )
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                    return  # Stop signal
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, self.CONNECT_BACKOFF_MAX)

    async def _connect_and_read(self) -> None:
        self.status = GatewayStatus.CONNECTING
        log.info("[%s] verbinde mit %s:%d", self.name, self.cfg.host, self.cfg.port)
        self._reader, self._writer = await asyncio.open_connection(
            self.cfg.host, self.cfg.port
        )
        self.status = GatewayStatus.CONNECTED
        self.diag.connections += 1
        self.diag.last_connect_ts = time.time()
        log.info("[%s] verbunden", self.name)

        # M88: Chip-ID des Modems abfragen (CO_RD_VERSION = Common Command 0x03).
        # Die Chip-ID wird als Source-ID fuer ReMan-Pairing benoetigt (OPUS
        # BRiDGE akzeptiert ReMan nur von einer echten Chip-ID, nicht von
        # einer FF-Block-ID). Antwort wird in _dispatch_packet abgefangen.
        self._awaiting_version = True
        try:
            ver_pkt = ESP3Packet(
                packet_type=PacketType.COMMON_COMMAND,
                data=bytes([0x03]),  # CO_RD_VERSION
                optional=b"",
            )
            await self.send(ver_pkt)
        except Exception as exc:  # noqa: BLE001
            log.debug("[%s] CO_RD_VERSION-Abfrage fehlgeschlagen: %s", self.name, exc)

        parser = ESP3StreamParser()
        try:
            while not self._stop_event.is_set():
                chunk = await self._reader.read(self.READ_CHUNK)
                if not chunk:
                    raise ConnectionError("Verbindung vom Gateway geschlossen")
                parser.feed(chunk)
                for pkt in parser.iter_packets():
                    await self._dispatch_packet(pkt)
        finally:
            if self._writer:
                try:
                    self._writer.close()
                    await self._writer.wait_closed()
                except Exception:  # noqa: BLE001
                    pass
            self._writer = None
            self._reader = None
            self.status = GatewayStatus.DISCONNECTED

    async def _dispatch_packet(self, pkt: ESP3Packet) -> None:
        # M88: CO_RD_VERSION-Response abfangen → Chip-ID extrahieren.
        # RESPONSE-data: [ret_code 1][app_ver 4][api_ver 4][chip_id 4][...]
        # Chip-ID ist data[9:13].
        if (self._awaiting_version
                and pkt.packet_type == PacketType.RESPONSE
                and len(pkt.data) >= 13
                and pkt.data[0] == 0x00):
            self.chip_id = int.from_bytes(pkt.data[9:13], "big")
            self._awaiting_version = False
            log.info("[%s] Modem Chip-ID: %08X", self.name, self.chip_id)
            return  # Version-Response nicht ins Live-Log
        # M86: Raw-Monitor — JEDES Paket ungefiltert ans Live-Log, BEVOR
        # Pipeline/Cascade (Dedup). So sieht der User wirklich alles auf dem
        # Funkbus, auch Duplikate vom zweiten Gateway und seltene Pakettypen.
        if self.on_raw_packet:
            try:
                self.on_raw_packet(
                    self.name, pkt.packet_type_name,
                    pkt.data.hex(), pkt.optional.hex(),
                )
            except Exception:  # noqa: BLE001
                pass
        # M81b: Wir behandeln ALLE ESP3-Pakete — auch non-ERP1 (REMOTE_MAN_COMMAND,
        # RADIO_MESSAGE, SMART_ACK, RESPONSE etc.). Bei non-ERP1 wird ein
        # synthetisches RadioTelegram erzeugt mit packet_type_name als RORG-
        # Anzeige im Live-Log. So sind ReMan-Pairing-Telegramme sichtbar.
        try:
            telegram = decode_any_packet(pkt)
        except Exception as exc:  # noqa: BLE001
            log.debug("[%s] Decode-Fehler: %s", self.name, exc)
            return

        self.diag.received_count += 1
        self.diag.last_recv_ts = time.time()
        log.info(
            "[%s] RX %s id=%s rssi=%s dBm payload=%s",
            self.name,
            telegram.rorg_name,
            telegram.sender_id_hex,
            telegram.rssi_dbm,
            telegram.payload.hex(),
        )

        rx = ReceivedTelegram(gateway_name=self.name, telegram=telegram)
        try:
            self._rx_queue.put_nowait(rx)
        except asyncio.QueueFull:
            log.warning("[%s] rx_queue voll — Telegramm verworfen", self.name)

    async def send(self, packet: ESP3Packet) -> None:
        """ESP3-Frame senden (TX). Voll ausgebaut in M5."""
        if not self._writer:
            raise RuntimeError(f"Gateway {self.name} nicht verbunden")
        async with self._writer_lock:
            self._writer.write(packet.to_bytes())
            await self._writer.drain()
        self.diag.sent_count += 1
        self.diag.last_send_ts = time.time()
