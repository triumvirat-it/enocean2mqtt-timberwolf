"""
Entry-Point. Lädt Konfig, startet Gateway-Manager → Pipeline → MQTT → Web-UI.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path

from .actor_state import ActorStateStore
from .cascade import Cascade
from .config import AppConfig
from .devices import DeviceRegistry
from .eep import register_default_profiles
from .gateway import GatewayManager
from .mqtt_client import MQTTPublisher
from .observed_senders import ObservedSenderRegistry
from .pipeline import TelegramPipeline
from .products import ProductDB
from .tx_router import TXRouter
from .webui import WebUIServer

log = logging.getLogger("enocean2mqtt")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


async def amain() -> int:
    config_dir = Path(os.environ.get("CONFIG_DIR", "/data"))
    cfg = AppConfig.load(config_dir)
    setup_logging(os.environ.get("LOG_LEVEL", cfg.log_level))

    log.info(
        "enocean2mqtt_timberwolf startet — %d Gateway(s), MQTT %s:%d",
        len(cfg.gateways), cfg.mqtt.host, cfg.mqtt.port,
    )

    register_default_profiles()

    devices_path = config_dir / "devices.yaml"
    devices = DeviceRegistry.load(devices_path)

    products = ProductDB.load_default()

    manager = GatewayManager(cfg)
    publisher = MQTTPublisher(cfg.mqtt)
    # Publisher braucht die DeviceRegistry, um eingehende .../set-Topics
    # (Namens-Slugs) auf device_id/channel_id aufzuloesen.
    publisher.devices = devices
    cascade = Cascade(
        dedup_window_ms=cfg.cascade.dedup_window_ms,
        rssi_history=cfg.cascade.rssi_history_size,
    )
    state_store = ActorStateStore(config_dir / "actor_state.yaml")
    pipeline = TelegramPipeline(manager, publisher, devices, cascade=cascade)
    # Auto-EEP-Wechsel (M47): Pipeline darf devices.yaml schreiben wenn ein
    # Lerntelegramm einen Channel-EEP-Wechsel ausloest (z.B. F3Z14D-Eingang
    # via PCT14 von Strom auf Gas umkonfiguriert).
    pipeline.devices_save_path = config_dir / "devices.yaml"
    # M61: Observed-Sender-Registry — Pipeline persistiert Sender-IDs die wir
    # live empfangen, aber die nirgends als Channel/PTM/Beobachter zugewiesen
    # sind. Damit erkennen wir "fremde" Sender im jeweiligen Gateway-Block.
    observed_senders = ObservedSenderRegistry(
        persist_path=config_dir / "observed_senders.yaml",
    )
    # M62: einmaliger Start-Cleanup — alle observed-Eintraege deren ID
    # bereits einem Channel zugewiesen ist entfernen (verhindert dass alte
    # YAML-Eintraege mit den neuen Channel-Sendern doppelt zaehlen).
    known_ids = []
    for d in devices.all():
        for ch in d.channels:
            for s in ch.senders:
                known_ids.append(s.sender_id)
    n_cleaned = observed_senders.cleanup_against_known_ids(known_ids)
    if n_cleaned:
        log.info("ObservedSender-Startup-Cleanup: %d Eintraege entfernt", n_cleaned)
    # M63: observed-Eintraege gegen aktuelle Gateway-Config remappen —
    # wenn eine ID nicht zum Block des aktuell zugeordneten Gateways passt,
    # umhaengen oder entfernen.
    n_remap, n_drop = observed_senders.remap_to_correct_gateway(cfg.gateways)
    if n_remap or n_drop:
        log.info("ObservedSender-Remap: %d umgehaengt, %d entfernt", n_remap, n_drop)
    if n_cleaned or n_remap or n_drop:
        observed_senders.save(force=True)
    pipeline.observed_senders = observed_senders
    pipeline._gateway_configs = cfg.gateways
    pipeline.defaults = cfg.defaults
    tx_router = TXRouter(cfg, manager, devices, cascade, state_store=state_store)
    # M69: dim_speed wird beim Senden in channel.meta persistiert
    tx_router.devices_save_path = config_dir / "devices.yaml"
    # M121: Aktor-State (Rolladen-Position + moving_up/moving_down) aufs
    # named /state-Topic — der TXRouter besitzt dieses Topic fuer Rolladen.
    tx_router.publisher = publisher
    publisher.set_command_handler(tx_router.handle_command)
    pipeline.on_actor_feedback = tx_router.handle_feedback

    webui = WebUIServer(
        cfg, manager, devices, cascade, tx_router, products, config_dir,
        state_store=state_store,
        observed_senders=observed_senders,
        publisher=publisher,
    )
    # Pipeline-Hook: Telegramme an UI weiterreichen (Live-Log + Anlern-Modus)
    def _on_telegram(rx, match, decoded=None):
        webui.log_buffer.add(rx, device_match=match, decoded=decoded)
        webui.teach.observe(rx, is_unknown=(match is None), decoded=decoded)
        # M85: eingehendes REMOTE_MAN_COMMAND = potenzielle Aktor-Antwort
        # waehrend eines laufenden OPUS-Pairings → tx_router wecken.
        # M85b: payload mitgeben damit der Echo-Filter unser eigenes
        # Unlock/Lock-Funk-Echo von einer echten Aktor-Antwort unterscheidet.
        try:
            if rx.telegram.packet_type_name == "REMOTE_MAN_COMMAND":
                tx_router.notify_reman_received(
                    rx.telegram.sender_id_hex, rx.telegram.payload.hex(),
                )
        except Exception:  # noqa: BLE001
            pass
    pipeline.on_telegram_post = _on_telegram
    # M84: ausgehende Telegramme (TX) ebenfalls ins Live-Log
    tx_router.on_tx = webui.log_buffer.add_tx
    # M86: Roh-Funk-Monitor — jedes ESP3-Paket ungefiltert ins Live-Log
    # (vor Cascade/Dedup). Auf allen Gateways setzen.
    for _gw in manager.gateways.values():
        _gw.on_raw_packet = webui.log_buffer.add_raw

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    await publisher.start()
    await publisher.wait_connected(timeout=15.0)
    await manager.start()
    await pipeline.start()
    await webui.start()

    log.info("Bereit — warte auf Telegramme")

    try:
        await stop_event.wait()
    finally:
        log.info("Shutdown läuft...")
        # Aktor-State (Dimmwert-Memory, Rolladen-Position, Eichfahrt) sichern,
        # damit er den Restart ueberlebt. Vorher wurde nur mark_dirty() gesetzt
        # aber beim Shutdown nie gespeichert -> Memory ging bei jedem Deploy
        # verloren und "An" sprang auf 100%.
        try:
            state_store.save()
        except Exception as exc:  # noqa: BLE001
            log.warning("state_store.save beim Shutdown fehlgeschlagen: %s", exc)
        await webui.stop()
        await pipeline.stop()
        await manager.stop()
        await publisher.stop()

    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(amain()))
    except FileNotFoundError as exc:
        print(f"Konfig-Fehler: {exc}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
