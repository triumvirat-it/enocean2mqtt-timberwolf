"""
GatewayManager: verwaltet mehrere Gateways parallel, sammelt empfangene Telegramme
in einer zentralen Queue und stellt Aggregat-Diagnose bereit.

In M3 wird hier zusätzlich Kaskadierung/Dedup eingehängt.
"""
from __future__ import annotations

import asyncio
import logging

from ..config import AppConfig
from .base import Gateway, GatewayStatus, ReceivedTelegram
from .lan_tcp import LANGateway

log = logging.getLogger(__name__)


class GatewayManager:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self._gateways: dict[str, Gateway] = {}
        self._rx_queue: asyncio.Queue[ReceivedTelegram] = asyncio.Queue(maxsize=5000)
        self._fanin_tasks: list[asyncio.Task] = []
        self._stop_event = asyncio.Event()

        for gw_cfg in cfg.gateways:
            if gw_cfg.type in ("TCM310-LAN", "TCM515-LAN"):
                gw = LANGateway(gw_cfg)
            else:
                raise ValueError(f"Unbekannter Gateway-Typ: {gw_cfg.type}")
            self._gateways[gw_cfg.name] = gw

    @property
    def rx_queue(self) -> asyncio.Queue[ReceivedTelegram]:
        return self._rx_queue

    @property
    def gateways(self) -> dict[str, Gateway]:
        return self._gateways

    async def start(self) -> None:
        log.info("Starte %d Gateways", len(self._gateways))
        for gw in self._gateways.values():
            await gw.start()
            self._fanin_tasks.append(
                asyncio.create_task(self._fan_in(gw), name=f"fanin-{gw.name}")
            )

    async def _fan_in(self, gw: Gateway) -> None:
        """Pumpe pro-GW-Queue in zentrale Manager-Queue."""
        while not self._stop_event.is_set():
            try:
                rx = await gw.rx_queue.get()
            except asyncio.CancelledError:
                return
            try:
                self._rx_queue.put_nowait(rx)
            except asyncio.QueueFull:
                log.warning("Manager-rx_queue voll — Telegramm von %s verworfen", gw.name)

    async def stop(self) -> None:
        log.info("Stoppe Gateways...")
        self._stop_event.set()
        for task in self._fanin_tasks:
            task.cancel()
        await asyncio.gather(
            *(gw.stop() for gw in self._gateways.values()), return_exceptions=True
        )

    def status_summary(self) -> dict[str, dict]:
        return {
            name: {
                "status": gw.status.value,
                "connections": gw.diag.connections,
                "received": gw.diag.received_count,
                "sent": gw.diag.sent_count,
                "last_recv_ts": gw.diag.last_recv_ts,
                "last_send_ts": gw.diag.last_send_ts,
                "last_error": gw.diag.last_error,
            }
            for name, gw in self._gateways.items()
        }
