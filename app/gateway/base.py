"""
Abstrakte Gateway-Basis. Konkrete Implementierungen: LANGateway (TCP), evtl. später USB.
"""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

from .esp3 import ESP3Packet, RadioTelegram


class GatewayStatus(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"
    DISABLED = "disabled"


@dataclass(slots=True)
class GatewayDiagnostics:
    connections: int = 0
    sent_count: int = 0
    received_count: int = 0
    last_connect_ts: float | None = None
    last_send_ts: float | None = None
    last_recv_ts: float | None = None
    last_error: str | None = None

    def msgs_per_sec(self, window: float = 60.0) -> float:
        """Grobe Schätzung anhand letzter Empfangszeit — verfeinert in M3."""
        if not self.last_recv_ts:
            return 0.0
        age = time.time() - self.last_recv_ts
        return self.received_count / max(age, 1.0) if age < window else 0.0


@dataclass(slots=True)
class ReceivedTelegram:
    """Was Konsumenten (Cascade/Handler) bekommen: Telegramm + Quell-GW."""

    gateway_name: str
    telegram: RadioTelegram
    received_at: float = field(default_factory=time.time)


class Gateway(ABC):
    """
    Abstract Base Class — eine konkrete EnOcean-Gateway-Verbindung.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.status: GatewayStatus = GatewayStatus.DISCONNECTED
        self.diag = GatewayDiagnostics()
        self._rx_queue: asyncio.Queue[ReceivedTelegram] = asyncio.Queue(maxsize=1000)
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        # M86: Raw-Monitor-Hook — wird VOR der Pipeline/Cascade fuer JEDES
        # empfangene ESP3-Paket aufgerufen (ungefiltert, kein Dedup). Signatur:
        #   on_raw_packet(gateway_name, packet_type_name, data_hex, optional_hex)
        # Damit zeigt das Live-Log wirklich alles was auf dem Funkbus passiert.
        self.on_raw_packet = None

    @property
    def rx_queue(self) -> asyncio.Queue[ReceivedTelegram]:
        return self._rx_queue

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name=f"gw-{self.name}")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()

    @abstractmethod
    async def _run_loop(self) -> None:
        """Connect-Loop mit Reconnect, lesen, parsen, in rx_queue legen."""
        raise NotImplementedError

    @abstractmethod
    async def send(self, packet: ESP3Packet) -> None:
        """Pakete senden. Implementierung in M5 vervollständigt."""
        raise NotImplementedError
