"""
Observed-Sender-Registry (M61).

Speichert pro Gateway alle Sender-IDs die wir im laufenden Betrieb empfangen
haben, die aber NICHT als Channel zugeordnet sind. Damit:

1. wissen wir welche IDs in den Gateway-Blöcken bereits "in der Luft" sind
   (z.B. fremde Wand-PTMs, Bewegungsmelder, Aktoren die wir noch nicht
   konfiguriert haben) → diese duerfen nicht als "frei" fuer Neuanlernen
   vorgeschlagen werden, sonst klauen wir IDs die irgendwo angelernt sind

2. Kann der User diese IDs explizit als Channel anlegen wenn er sie kennt

Persistenz: {config_dir}/observed_senders.yaml. Bei jedem record() in-memory
und alle N Sekunden auf Disk (Batch-Save, kein Sync-IO im Hot-Path).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable

import yaml

log = logging.getLogger(__name__)


@dataclass
class ObservedSender:
    """Ein im Live-Log gesehener Sender, dem (noch) kein Channel zugewiesen ist."""

    gateway: str
    sender_id: str  # uppercase 8-hex
    first_seen: float = 0.0
    last_seen: float = 0.0
    count: int = 0
    rorg: str = ""
    rssi_dbm: int | None = None


class ObservedSenderRegistry:
    """
    In-Memory-Cache + Disk-Persistenz fuer beobachtete Sender-IDs pro Gateway.
    Thread-safe genug fuer single-loop asyncio (kein Threading hier).
    """

    def __init__(self, persist_path: Path | None = None) -> None:
        self.persist_path = persist_path
        # (gateway, sender_id_upper) -> ObservedSender
        self._items: dict[tuple[str, str], ObservedSender] = {}
        self._dirty = False
        self._last_save_at = 0.0
        self.save_throttle_s = 30.0
        if persist_path and persist_path.exists():
            self._load()

    def _load(self) -> None:
        try:
            with self.persist_path.open("r", encoding="utf-8") as f:
                doc = yaml.safe_load(f) or {}
            for raw in (doc.get("observed") or []):
                obs = ObservedSender(
                    gateway=raw.get("gateway", ""),
                    sender_id=(raw.get("sender_id", "") or "").upper(),
                    first_seen=float(raw.get("first_seen") or 0.0),
                    last_seen=float(raw.get("last_seen") or 0.0),
                    count=int(raw.get("count") or 0),
                    rorg=raw.get("rorg", ""),
                    rssi_dbm=raw.get("rssi_dbm"),
                )
                if obs.gateway and obs.sender_id:
                    self._items[(obs.gateway, obs.sender_id)] = obs
            log.info("ObservedSenderRegistry: %d Eintraege geladen", len(self._items))
        except Exception as exc:  # noqa: BLE001
            log.warning("ObservedSenderRegistry konnte %s nicht laden: %s",
                        self.persist_path, exc)

    def save(self, force: bool = False) -> None:
        if not self.persist_path:
            return
        if not (self._dirty or force):
            return
        now = time.time()
        if not force and (now - self._last_save_at) < self.save_throttle_s:
            return
        try:
            data = {
                "observed": [
                    asdict(o)
                    for o in sorted(
                        self._items.values(),
                        key=lambda x: (x.gateway, x.sender_id),
                    )
                ],
            }
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.persist_path.with_suffix(self.persist_path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
            tmp.replace(self.persist_path)
            self._dirty = False
            self._last_save_at = now
        except Exception as exc:  # noqa: BLE001
            log.warning("ObservedSenderRegistry save fehlgeschlagen: %s", exc)

    def record(
        self,
        gateway: str,
        sender_id: str,
        rorg: str = "",
        rssi_dbm: int | None = None,
        ts: float | None = None,
    ) -> None:
        """Eintrag aktualisieren oder neu anlegen. Markiert dirty."""
        if not gateway or not sender_id:
            return
        sid = sender_id.upper()
        now = ts if ts is not None else time.time()
        key = (gateway, sid)
        obs = self._items.get(key)
        if not obs:
            obs = ObservedSender(
                gateway=gateway, sender_id=sid,
                first_seen=now, last_seen=now, count=0,
                rorg=rorg, rssi_dbm=rssi_dbm,
            )
            self._items[key] = obs
        obs.last_seen = now
        obs.count += 1
        if rorg:
            obs.rorg = rorg
        if rssi_dbm is not None:
            obs.rssi_dbm = rssi_dbm
        self._dirty = True

    def forget(self, sender_id: str) -> int:
        """
        Entfernt alle Eintraege mit dieser sender_id (uebergreifend ueber alle
        Gateways) — wird aufgerufen wenn der User diese ID als Channel anlegt.
        Liefert Anzahl entfernter Eintraege.
        """
        sid = sender_id.upper()
        keys_to_remove = [k for k in self._items if k[1] == sid]
        for k in keys_to_remove:
            del self._items[k]
        if keys_to_remove:
            self._dirty = True
        return len(keys_to_remove)

    def all_for_gateway(self, gateway: str) -> list[ObservedSender]:
        return [o for o in self._items.values() if o.gateway == gateway]

    def all_sender_ids_for_gateway(self, gateway: str) -> list[str]:
        return [o.sender_id for o in self._items.values() if o.gateway == gateway]

    def all_sender_ids(self) -> list[str]:
        """Alle observed sender_ids — gateway-unabhaengig."""
        return list({o.sender_id for o in self._items.values()})

    def remap_to_correct_gateway(self, gateways: Iterable) -> tuple[int, int]:
        """
        Repariert observed-Eintraege gegen die aktuelle Gateway-Config.

        Jeder Eintrag SOLL zu dem Gateway gehoeren, dessen Base-ID-Block die
        Sender-ID enthaelt. Wenn:
          - ein anderes (passendes) Gateway existiert → Eintrag umhaengen
          - kein passendes Gateway existiert → Eintrag loeschen

        Returns: (n_remapped, n_dropped)
        """
        from .sender_routing import find_gateway_for_sender

        gateways = list(gateways)
        to_remap: list[tuple[tuple, str]] = []
        to_remove: list[tuple] = []
        for key, obs in self._items.items():
            correct_gw = find_gateway_for_sender(obs.sender_id, gateways)
            if not correct_gw:
                to_remove.append(key)
            elif correct_gw != obs.gateway:
                to_remap.append((key, correct_gw))

        for k in to_remove:
            del self._items[k]
        for (old_key, new_gw) in to_remap:
            obs = self._items.pop(old_key)
            obs.gateway = new_gw
            new_key = (new_gw, obs.sender_id)
            existing = self._items.get(new_key)
            if existing:
                # Merge: max(last_seen), sum(count), neuere rssi
                existing.count += obs.count
                if obs.last_seen > existing.last_seen:
                    existing.last_seen = obs.last_seen
                    if obs.rssi_dbm is not None:
                        existing.rssi_dbm = obs.rssi_dbm
            else:
                self._items[new_key] = obs

        if to_remap or to_remove:
            self._dirty = True
        return len(to_remap), len(to_remove)

    def cleanup_against_known_ids(self, known_ids: Iterable[str]) -> int:
        """
        Entfernt observed-Eintraege deren sender_id in known_ids vorkommt
        (= bereits einem Channel zugewiesen). Returns Anzahl entfernter.
        Wird beim Startup und in /api/gateways aufgerufen — damit observed
        und assigned sich nie ueberschneiden.
        """
        known_set = {(k or "").upper() for k in known_ids if k}
        if not known_set:
            return 0
        keys_to_remove = [
            k for k in self._items if k[1] in known_set
        ]
        for k in keys_to_remove:
            del self._items[k]
        if keys_to_remove:
            self._dirty = True
        return len(keys_to_remove)
