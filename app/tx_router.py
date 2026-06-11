"""
TX-Router: Sende-Befehle von MQTT in physische EnOcean-Telegramme uebersetzen.

Verantwortung:
- Mapping device_id+channel_id -> Encoder + Sender-ID
- Gateway-Auswahl via Cascade (best RSSI / floor / all)
- ESP3-Frame an Gateway zum Senden uebergeben
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .actor_state import ActorState, ActorStateStore, commit_movement, estimate_position_now
from .cascade import Cascade, GatewaySelector, SelectableGateway
from .config import AppConfig
from .devices import Device, DeviceChannel, DeviceRegistry
from .eep import get_profile_registry
from .encoders import (
    CommandFrame,
    encode_4bs_teach_in,
    encode_a5_38_08_dim,
    encode_a5_38_08_switch,
    encode_eltako_shutter,
    encode_eltako_switch,
    encode_f6_02_button,
    encode_reman_lock,
    encode_reman_query_id,
    encode_reman_set_linktable,
    encode_reman_unlock,
)
from .gateway import GatewayManager
from .sender_routing import find_gateway_for_sender

log = logging.getLogger(__name__)

# M124: Dauer eines Kurzzeit-/Tipp-Befehls (Lamellenverstellung) in Sekunden.
SHUTTER_STEP_S = 0.4


def classify_channel_kind(channel: DeviceChannel) -> str:
    """
    Aktor-Klassifikation (rx/switch/dimmer/shutter/valve).

    M73: device_type vom Channel (aus dem Produktkatalog / Scaffold)
    hat VORRANG vor profile.ui_kind, weil A5-38-08 zwei Sub-Profile hat:
      - A5-38-08(01) = Switching (FSR14, FSR61) → ui_kind=switch
      - A5-38-08(02) = Dimming   (FDG14, FUD14, FLD61, FUD61) → ui_kind=dimmer
    Aber unsere EEPProfileRegistry kennt nur das Master-EEP. Mit device_type
    koennen wir das eindeutig disambiguieren.

    Frei als Modulfunktion, damit auch die Pipeline (Publish-Seite) den Typ
    kennt, ohne eine TXRouter-Instanz zu brauchen.
    """
    dt = (channel.meta.get("device_type") or "").lower()
    if "switch" in dt or "schalter" in dt:
        return "switch"
    if "jalousie" in dt or "shutter" in dt or "rolladen" in dt:
        return "shutter"
    if "dimmer" in dt:
        return "dimmer"
    if "valve" in dt or "ventil" in dt:
        return "valve"
    # Profile als Fallback
    profile = get_profile_registry().get(channel.eep)
    if profile and profile.ui_kind:
        if profile.ui_kind in ("rx", "switch", "dimmer", "shutter", "valve"):
            return profile.ui_kind
    # Legacy-Fallback fuer Channels ohne bekannten EEP + ohne device_type
    cfg = (channel.meta.get("device_config") or "").lower()
    if "dimmer" in cfg or "dimming" in cfg:
        return "dimmer"
    if not channel.learned_pair_id:
        return "rx"
    return "switch"


class TXRouter:
    """Routet MQTT-Commands zu konkreten EnOcean-Telegrammen."""

    def __init__(
        self,
        cfg: AppConfig,
        manager: GatewayManager,
        devices: DeviceRegistry,
        cascade: Cascade,
        state_store: ActorStateStore | None = None,
    ) -> None:
        self.cfg = cfg
        self.manager = manager
        self.devices = devices
        self.cascade = cascade
        self.state_store = state_store
        # M121: optionaler MQTTPublisher (gesetzt von main.py), um den
        # Aktor-State (Rolladen-Position + moving_up/moving_down) auf das
        # named /state-Topic zu publishen. Ohne Publisher: kein Aktor-State.
        self.publisher = None
        self._selector = self._build_selector()
        # M69: optional Pfad fuer devices.yaml-Persist (gesetzt von main.py).
        # Wird benutzt um channel.meta.dim_speed bei TX zu speichern.
        self.devices_save_path = None
        # M84: optionaler Hook (gesetzt von main.py) um ausgehende Telegramme
        # ins Live-Log einzutragen. Signatur:
        #   on_tx(sender_id_hex, rorg_name, payload_hex, gateway, label)
        self.on_tx = None
        # M85: Liste von asyncio.Future die auf eine eingehende Aktor-Antwort
        # waehrend des ReMan-Pairings warten. Die Pipeline ruft
        # notify_reman_received() bei jedem eingehenden REMOTE_MAN_COMMAND.
        self._reman_waiters: list = []

    def _build_selector(self) -> GatewaySelector:
        gws = [
            SelectableGateway(
                name=g.name,
                enabled=g.enabled,
                floor_assignments=list(g.floor_assignments),
            )
            for g in self.cfg.gateways
        ]
        return GatewaySelector(
            strategy=self.cfg.cascade.send_strategy,
            rssi_table=self.cascade.rssi,
            gateways=gws,
        )

    async def handle_command(
        self,
        device_id: str,
        channel_id: str,
        command: dict[str, Any],
    ) -> None:
        """
        Verarbeitet einen MQTT-Command oder UI-Befehl.

        Erweitert: position (0-100) bei Rolladen, dim (0-100) bei Dimmer.
        Command-Verarbeitung loggt zusaetzlich State im ActorStore.
        """
        # Device finden
        device = next(
            (d for d in self.devices.all() if d.device_id == device_id),
            None,
        )
        if not device:
            log.warning("TX: Unbekanntes Device %s", device_id)
            return

        channel = next((c for c in device.channels if c.channel_id == channel_id), None)
        if not channel:
            log.warning("TX: Channel %s in Device %s nicht gefunden", channel_id, device_id)
            return

        # M124: Rolladen-Befehlsschema der Timberwolf-Visu auf dem EINEN
        # .../set-Topic (ein JSON-Key je Connector, Wert = Richtung 0=Auf/1=Ab):
        #   {"move": 0|1}  Langbefehl -> volle Fahrt bis Endanschlag
        #   {"step": 0|1}  Kurzbefehl -> kurzer Tipp (Lamelle)
        #   {"stop": true} -> Stopp  (false/0 -> nichts tun)
        if self._kind(channel) == "shutter" and (
            "move" in command or "step" in command or "stop" in command
        ):
            command = self._shutter_visu_command(command)
            if command is None:
                return  # z.B. {"stop": false} -> ignorieren

        # M124: Langbefehl (Visu) = volle Fahrt bis Endanschlag. Aus dem reinen
        # Richtungsbefehl {"command": "up"/"down", "full": True} eine Fahrt mit
        # Laufzeit+Puffer machen — der Endschalter stoppt. Bewusst NICHT ueber
        # eine absolute Position (deren "bereits am Ziel"-Kurzschluss wuerde die
        # Fahrt verschlucken, wenn die getrackte Position schon am Anschlag steht).
        if command.get("full") and self.state_store and self._kind(channel) == "shutter":
            st = self.state_store.get(device_id, channel_id)
            direction = command.get("command", "stop")
            duration_s = max(0.1, st.time_for(direction)) + 2.0
            command = {"command": direction, "duration_s": duration_s}

        # Nackten Wert auf den passenden Befehl des Aktortyps abbilden:
        # ein MQTT-Pegelsteller publisht oft nur eine Zahl (z.B. "30") statt
        # JSON. _handle_incoming verpackt das zu {"value": 30}. Daraus machen
        # wir bei Rolladen {"position": 30}, bei Dimmer {"dim": 30}, bei
        # Schalter {"state": <bool>}.
        command = self._normalize_value_command(channel, command)

        # Spezialbefehl: "position" (explizit oder aus value gemappt): fahre zu
        # absoluter Rolladen-Position.
        if "position" in command and self.state_store:
            await self._handle_position_command(device_id, channel_id, command)
            return

        # Sender-ID ermitteln (entweder learned_pair_id ODER fallback auf GW base_id)
        sender_id = self._resolve_sender_id(channel)
        if sender_id is None:
            log.error("TX: Keine Sender-ID fuer %s/%s — Channel benoetigt 'learned_pair_id' "
                      "oder 'enocean_id'", device_id, channel_id)
            return

        # Dimmer-An/Aus OHNE expliziten Wert auf den gemerkten Dimmwert
        # uebersetzen (Eltako-Dimmer ignorieren den reinen Schaltbefehl).
        command = self._apply_dimmer_onoff_memory(
            device_id, channel_id, channel, command,
        )

        # Encoder waehlen nach EEP / config_hint
        try:
            frame = self._build_frame(channel, sender_id, command)
        except (ValueError, KeyError) as exc:
            log.error("TX: Encoding-Fehler fuer %s/%s: %s", device_id, channel_id, exc)
            return
        if frame is None:
            log.warning("TX: Kein Encoder fuer EEP %s (Device %s/%s)",
                        channel.eep, device_id, channel_id)
            return

        # M87: VLD-Aktoren (D2-01/D2-05, OPUS BRiDGE) erwarten ADRESSIERTE
        # Telegramme — Destination = Aktor-RX-ID, NICHT Broadcast.
        # Belegt aus Live-Mitschnitt: Schaltbefehl an OPUS-Bridge hat
        # opt-destination = Aktor-ID (01A03001). Mit Broadcast ignoriert der
        # Aktor den Befehl.
        eep_up = (channel.eep or "").upper()
        if eep_up.startswith("D2-") and channel.enocean_id:
            try:
                dest = int(channel.enocean_id, 16)
                frame = self._set_frame_destination(frame, dest)
            except (ValueError, TypeError):
                pass

        # Gateway(s) waehlen
        floors = self._device_floors(device)
        # M59: aktiver SenderBinding kann eigenes via_gateway haben
        explicit_gw = channel.active_via_gateway or channel.via_gateway
        # M60: wenn nicht explizit → ueber Sender-ID den passenden Block finden
        if not explicit_gw and channel.active_sender_id:
            explicit_gw = find_gateway_for_sender(
                channel.active_sender_id, self.cfg.gateways,
            )
        if explicit_gw:
            gw_names = [explicit_gw]
        else:
            # Aktor-Adresse fuer RSSI-Lookup
            actor_id = channel.enocean_id or ""
            gw_names = self._selector.choose(actor_id, floors)

        if not gw_names:
            log.error("TX: Kein Gateway verfuegbar fuer %s/%s", device_id, channel_id)
            return

        # Senden
        await self._send_to_gateways(frame, gw_names, device_id, channel_id)

        # State im ActorStateStore nachfuehren
        if self.state_store:
            self._update_state_after_send(device_id, channel_id, channel, command)
            # M121: Rolladen-Position + Bewegungs-Flags aufs /state-Topic
            await self._publish_actor_state(device, channel)

    def _update_state_after_send(
        self,
        device_id: str,
        channel_id: str,
        channel: DeviceChannel,
        command: dict[str, Any],
    ) -> None:
        """Optimistic State-Update nach dem Senden — Feedback korrigiert spaeter."""
        st = self.state_store.get(device_id, channel_id)
        now = time.time()
        st.last_command_at = now

        kind = self._kind(channel)
        if kind == "shutter":
            cmd = command.get("command") or command.get("state")
            dur = float(command.get("duration_s", 0.0))
            if cmd == "stop":
                commit_movement(st)
                st.last_command = "stop"
            elif cmd in ("up", "down"):
                # Wenn schon eine Bewegung lief: aktuelle Pos einfrieren bevor neue beginnt
                if st.moving:
                    commit_movement(st)
                st.moving = cmd
                st.moving_started_at = now
                # Wenn kein expliztes target gesetzt war (also kein _handle_position_command):
                if st.moving_target is None:
                    # Fahrtende-Position basierend auf Dauer (richtungsabhaengige Laufzeit)
                    delta = (dur / max(0.1, st.time_for(cmd))) * 100
                    if cmd == "up":
                        st.moving_target = max(0.0, st.position_percent - delta)
                    else:
                        st.moving_target = min(100.0, st.position_percent + delta)
                st.last_command = f"{cmd} {dur:.1f}s"
        elif kind == "dimmer":
            # M68: dim_percent nur ueberschreiben wenn der Befehl Aktor ANschaltet.
            # Beim Aus schicken wir zwar protokollarisch dim=0 mit (weil
            # Eltako-Dimmer Command 2 brauchen), aber der Memory-Wert soll
            # erhalten bleiben.
            new_on = command.get("state")
            keep_dim_memory = (new_on is False)
            if "dim" in command and not keep_dim_memory:
                st.dim_percent = int(command["dim"])
            if "state" in command:
                st.on = bool(new_on)
            elif "dim" in command:
                st.on = st.dim_percent > 0
            st.last_command = f"dim={st.dim_percent}%"
            # M69: dim_speed persistent in channel.meta speichern wenn mit
            # einem Befehl mitgesendet
            if "ramp" in command:
                try:
                    new_speed = int(command["ramp"])
                except (ValueError, TypeError):
                    new_speed = None
                if new_speed is not None and channel.meta.get("dim_speed") != new_speed:
                    channel.meta["dim_speed"] = new_speed
                    if self.devices_save_path:
                        try:
                            self.devices.save(self.devices_save_path)
                        except Exception as exc:  # noqa: BLE001
                            log.debug("dim_speed save fehlgeschlagen: %s", exc)
        elif kind == "switch":
            if "state" in command:
                st.on = _parse_state(command["state"])
                st.last_command = "on" if st.on else "off"
        elif kind == "valve":
            # RX-only Heizregler (STC-DO8): wir merken nur den zuletzt
            # gesendeten Sollwert/Temp zur Anzeige — der Aktor gibt kein Feedback.
            off = command.get("setpoint_offset", command.get("setpoint_offset_c"))
            temp = command.get("temperature", command.get("temperature_c"))
            parts = []
            if off is not None:
                parts.append(f"Soll {float(off):+.1f}K")
            if temp is not None:
                parts.append(f"Ist {float(temp):.1f}°C")
            if "night" in command:
                parts.append("Nacht" if _parse_state(command["night"]) else "Tag")
            elif "day" in command:
                parts.append("Tag" if _parse_state(command["day"]) else "Nacht")
            if parts:
                st.last_command = " · ".join(parts)

        self.state_store.mark_dirty()

    def _adjust_travel_time(
        self, st, target_position: float, now: float, direction: str,
    ) -> None:
        """
        Auto-Adjust mit Sanity-Checks — pro Richtung getrennt (direction=
        'up'=Heben -> travel_time_up_s, sonst Senken -> travel_time_s).

        Eltako-Aktoren ohne Stromfluss-Messung melden „end_up/end_down" oft
        unzuverlässig — wenn der Motor durch seinen Endschalter stoppt aber
        der Aktor noch Strom liefert, kommt das Endlagen-Telegramm verspätet
        oder gar nicht. Daher: nur kleine Korrekturen akzeptieren, sonst
        ignorieren und Warning loggen.
        """
        if not st.moving or not st.moving_started_at:
            return
        actual_seconds = now - st.moving_started_at
        delta_pct = abs(target_position - st.position_percent)
        if delta_pct < 10.0 or actual_seconds < 3.0:
            return  # zu kurze Fahrt — keine zuverlässige Messung

        # Tatsaechliche Laufzeit fuer 0-100% Fahrt extrapoliert
        measured_full_time = actual_seconds * (100.0 / delta_pct)
        old = st.time_for(direction)

        # Sanity-Check: gemessene Laufzeit darf nicht völlig absurd sein
        # (zwischen 50% und 200% der bisherigen)
        ratio = measured_full_time / max(old, 0.1)
        if ratio < 0.5 or ratio > 2.0:
            log.warning(
                "Auto-Adjust ABGELEHNT %s/%s: %.1fs gemessen, %.1fs erwartet "
                "(ratio=%.2f — Aktor liefert wahrscheinlich unzuverlässiges Endlagen-Feedback)",
                st.device_id, st.channel_id, measured_full_time, old, ratio,
            )
            return

        # Smoothing: 80% alter Wert + 20% neuer Wert (vorsichtig)
        new = round(0.8 * old + 0.2 * measured_full_time, 1)
        if abs(new - old) >= 0.5:
            log.info(
                "Auto-Adjust Laufzeit %s/%s (%s): %.1fs -> %.1fs (gemessen %.1fs fuer %.0f%%)",
                st.device_id, st.channel_id, direction, old, new, actual_seconds, delta_pct,
            )
            if direction == "up":
                st.travel_time_up_s = new
            else:
                st.travel_time_s = new

    def _kind(self, channel: DeviceChannel) -> str:
        """Aktor-Klassifikation — delegiert an die Modulfunktion."""
        return classify_channel_kind(channel)

    async def _publish_actor_state(self, device: Device, channel: DeviceChannel) -> None:
        """
        Publiziert den Rolladen-Aktor-State (Position + Bewegungs-Flags) auf das
        named /state-Topic. No-op fuer Nicht-Rolladen oder ohne Publisher.

        Wird vom TXRouter besessen, NICHT von der Pipeline — die Pipeline
        ueberspringt fuer Rolladen-Channels ihr generisches Decoded-Publish
        (sonst wuerden sich zwei verschiedene Payloads auf demselben retained
        Topic ueberschreiben).
        """
        if not (self.publisher and self.state_store):
            return
        if self._kind(channel) != "shutter":
            return
        st = self.state_store.get(device.device_id, channel.channel_id)
        payload = {
            "position": int(round(st.position_percent)),
            "moving_up": st.moving == "up",
            "moving_down": st.moving == "down",
            "calibrated": st.calibrated,
        }
        try:
            await self.publisher.publish_device(device, channel, payload)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Aktor-State-Publish %s/%s fehlgeschlagen: %s",
                device.device_id, channel.channel_id, exc,
            )

    def _schedule_actor_state_publish(self, device: Device, channel: DeviceChannel) -> None:
        """
        Sync-Aufrufer (handle_feedback laeuft als nicht-awaiteter Hook in der
        Pipeline-Coroutine): plant das async-Publish als Task ein. Ohne
        laufenden Loop einfach No-op.
        """
        if not (self.publisher and self.state_store):
            return
        if self._kind(channel) != "shutter":
            return
        try:
            asyncio.create_task(self._publish_actor_state(device, channel))
        except RuntimeError:
            pass  # kein laufender Event-Loop

    def handle_feedback(
        self,
        sender_id_hex: str,
        decoded: dict[str, Any],
    ) -> bool:
        """
        Wird von der Pipeline fuer JEDES Telegramm aufgerufen. Zwei Wege
        koennen den State aktualisieren:

        (a) Aktor-Feedback: das Telegramm kommt vom Aktor selbst (Eltako-
            Feedback-Telegramme mit decoded["feedback"] == True). Wir finden
            den Aktor-Channel ueber seine enocean_id == sender_id und tragen
            den State ein. Das ist die sichere Wahrheit.

        (b) PTM-optimistisch: das Telegramm kommt vom gepaarten PTM-Schalter
            (z.B. FF810055). Wir finden alle Aktor-Channels die diesen PTM
            als learned_pair_id haben und toggeln deren State optimistisch.
            Korrektur kommt spaeter durch (a).

        Returns True wenn mindestens ein State aktualisiert wurde.
        """
        if not self.state_store:
            return False
        sid = sender_id_hex.upper()
        updated = False

        # (a) Aktor-eigenes Feedback?
        if decoded.get("feedback"):
            for d, c in self.devices.lookup_all_by_sender_id(sid):
                self._apply_feedback(d.device_id, c.channel_id, c, decoded)
                self._schedule_actor_state_publish(d, c)
                updated = True
            if updated:
                return True

        # (b) Beobachter-Update: jeder Aktor der diesen Sender als Beobachter
        # gelernt hat (Wand-PTMs, Bewegungsmelder, Zeitschaltuhren). Plus
        # rueckwaertskompatibel: alte learned_pair_id-Eintraege.
        # Match-Filter (rocker/event) je Observer-Binding wird hier
        # angewendet — FT55-Wippen koennen so pro Wippe getrennt anlernen.
        from .devices import _normalize_observer, observer_match
        actors_observing = self.devices.lookup_actors_observing(sid)
        seen: set[tuple[str, str]] = set()
        sid_up = sid.upper()
        for d, c in actors_observing:
            key = (d.device_id, c.channel_id)
            if key in seen:
                continue
            # Mind. ein Observer-Eintrag dieses Channels muss zur Sender-ID
            # passen UND zum Match-Filter zum aktuellen decoded.
            channel_match = False
            for obs in (c.observers or []):
                obs_sid, obs_match_rule = _normalize_observer(obs)
                if obs_sid != sid_up:
                    continue
                if observer_match(obs_match_rule, decoded):
                    channel_match = True
                    break
            if not channel_match:
                continue
            seen.add(key)
            self._apply_ptm_press(d.device_id, c.channel_id, c, decoded)
            self._schedule_actor_state_publish(d, c)
            updated = True

        # Alte learned_pair_id-PTM-Verknuepfungen: kein Match-Filter (legacy).
        for d, c in self.devices.lookup_actors_by_ptm(sid):
            key = (d.device_id, c.channel_id)
            if key in seen:
                continue
            seen.add(key)
            self._apply_ptm_press(d.device_id, c.channel_id, c, decoded)
            self._schedule_actor_state_publish(d, c)
            updated = True

        return updated

    def _apply_ptm_press(
        self,
        device_id: str,
        channel_id: str,
        channel: DeviceChannel,
        decoded: dict[str, Any],
    ) -> None:
        """
        Optimistisches Update: ein gepaarter PTM-Schalter wurde gedrueckt.
        Wir toggeln den Aktor-State sofort. Wenn der Aktor selbst spaeter
        ein Feedback-Telegramm sendet, korrigiert _apply_feedback ggf. wieder.

        Wir reagieren auf:
          - F6-PTM-Press (decoded.pressed = True)
          - M74: A5-38-08-Schaltbefehle vom PTM (Command 1 oder 2 mit on-Flag).
            FSR14 sendet kein Status-Feedback, aber der beobachtete Sende-Befehl
            (z.B. FF810022 [01 00 00 09]) sagt uns implizit den neuen Aktor-
            Zustand — wir aktualisieren den State daraus.
        """
        # M74: A5-38-08 Schaltbefehl vom PTM = optimistic state update
        is_a5_3808_cmd = (
            decoded.get("command") in (1, 2)
            and "on" in decoded
            and not decoded.get("feedback")
        )
        if is_a5_3808_cmd:
            st = self.state_store.get(device_id, channel_id)
            now = time.time()
            st.last_command_at = now
            kind = self._kind(channel)
            new_on = bool(decoded["on"])
            if kind in ("switch", "dimmer"):
                st.on = new_on
                # Bei Dimmer nur den dim_percent uebernehmen wenn ON gemeldet
                # wird (siehe M68 — beim OFF Memory-Wert nicht ueberschreiben)
                if (kind == "dimmer" and new_on
                        and "dim_percent" in decoded
                        and int(decoded["dim_percent"]) > 0):
                    st.dim_percent = int(decoded["dim_percent"])
                st.last_command = "ptm:" + ("on" if new_on else "off")
                self.state_store.mark_dirty()
            return

        if not decoded.get("pressed"):
            return
        st = self.state_store.get(device_id, channel_id)
        now = time.time()
        st.last_command_at = now

        kind = self._kind(channel)
        rocker = decoded.get("rocker_1") or ""
        # AI = oben-links, A0 = unten-links, BI = oben-rechts, B0 = unten-rechts.
        # PTM-Polung: welche Wippen-Haelfte schaltet EIN? Global per
        # cfg.defaults.ptm_on_press, pro Kanal via channel.meta.ptm_on_press
        # ueberschreibbar. "I" (Default) = oben (AI/BI) = EIN; "0" = unten.
        pol = (channel.meta.get("ptm_on_press")
               or getattr(getattr(self.cfg, "defaults", None), "ptm_on_press", "I"))
        if pol == "0":
            is_on_press = rocker in ("A0", "B0")
            is_off_press = rocker in ("AI", "BI")
        else:
            is_on_press = rocker in ("AI", "BI")
            is_off_press = rocker in ("A0", "B0")

        if kind == "switch":
            if is_on_press:
                st.on = True
                st.last_command = "ptm:on"
            elif is_off_press:
                st.on = False
                st.last_command = "ptm:off"
            else:
                # Wippe nicht eindeutig -> einfach toggeln
                st.on = not st.on
                st.last_command = "ptm:toggle"
        elif kind == "shutter":
            if is_on_press:
                if st.moving:
                    commit_movement(st, now=now)
                st.moving = "up"
                st.moving_started_at = now
                st.last_command = "ptm:up"
            elif is_off_press:
                if st.moving:
                    commit_movement(st, now=now)
                st.moving = "down"
                st.moving_started_at = now
                st.last_command = "ptm:down"
        elif kind == "dimmer":
            if is_on_press:
                st.on = True
                if st.dim_percent <= 0:
                    st.dim_percent = 100
            elif is_off_press:
                st.on = False
            st.last_command = "ptm:" + ("on" if st.on else "off")

        self.state_store.mark_dirty()

    def _apply_feedback(
        self,
        device_id: str,
        channel_id: str,
        channel: DeviceChannel,
        decoded: dict[str, Any],
    ) -> None:
        if not decoded.get("feedback"):
            return
        st = self.state_store.get(device_id, channel_id)
        now = time.time()
        st.last_feedback_at = now

        kind = self._kind(channel)
        if kind == "shutter":
            state = decoded.get("state")
            # WICHTIG: Eltako-FSB sendet das Status-Telegramm erst NACH dem
            # Stopp des Aktors. duration_s im Telegramm = wie lange er
            # tatsaechlich gefahren ist. Wir kommittieren also direkt die
            # neue Position und setzen moving=None.
            elt_duration = float(decoded.get("duration_s") or 0.0)

            if state == "end_up":
                # Aktor meldet Endlage oben erreicht (Heben)
                self._adjust_travel_time(st, target_position=0.0, now=now, direction="up")
                st.position_percent = 0.0
                st.moving = None
                st.moving_started_at = None
                st.moving_target = None
            elif state == "end_down":
                # Aktor meldet Endlage unten erreicht (Senken)
                self._adjust_travel_time(st, target_position=100.0, now=now, direction="down")
                st.position_percent = 100.0
                st.moving = None
                st.moving_started_at = None
                st.moving_target = None
            elif state == "stop":
                commit_movement(st, now=now)
            elif state == "moving_up":
                # Telegramm "moving_up + duration_s" bedeutet: Aktor ist
                # gestoppt, hat duration_s aufwaerts gefahren. Falls
                # duration_s >= Heben-Laufzeit: vermutlich Endlage oben erreicht.
                travel = max(0.1, st.time_for("up"))
                if elt_duration >= travel:
                    st.position_percent = 0.0
                else:
                    st.position_percent = max(0.0, st.position_percent - (elt_duration / travel) * 100.0)
                st.moving = None
                st.moving_started_at = None
                st.moving_target = None
            elif state == "moving_down":
                # Analog fuer abwaerts (Senken-Laufzeit).
                travel = max(0.1, st.time_for("down"))
                if elt_duration >= travel:
                    st.position_percent = 100.0
                else:
                    st.position_percent = min(100.0, st.position_percent + (elt_duration / travel) * 100.0)
                st.moving = None
                st.moving_started_at = None
                st.moving_target = None
        elif kind == "dimmer":
            # M66: dim_percent NICHT ueberschreiben wenn der Aktor "aus" meldet.
            # Eltako-Dimmer (FLD61, FUD61, FDG14) senden im OFF-Telegramm oft
            # dim_percent=0 mit. Das ist nur ein protokollarischer Wert — der
            # tatsaechliche Memory-Wert im Aktor bleibt erhalten. Wir spiegeln
            # das hier, damit der naechste "An"-Befehl auf den letzten realen
            # Wert zurueckkehrt statt auf 0.
            new_on = bool(decoded.get("on", st.on))
            if "dim_percent" in decoded and new_on:
                st.dim_percent = int(decoded["dim_percent"])
            if "on" in decoded:
                st.on = new_on
        elif kind == "switch":
            if "on" in decoded:
                st.on = bool(decoded["on"])

        self.state_store.mark_dirty()

    async def send_teach_in(
        self,
        device_id: str,
        channel_id: str,
        sender_id_hex: str | None = None,
    ) -> dict[str, Any]:
        """
        Sendet ein 4BS-Lerntelegramm an einen Aktor (M57 + M59).

        Wenn sender_id_hex angegeben ist, MUSS dieser Sender in
        channel.senders existieren — wir lernen GENAU diesen ID an.
        Damit kann der User auch INAKTIVE Sender anlernen, ohne sie
        erst auf active=True umschalten zu muessen.

        Wenn None: nutzt active_sender_id (Default).

        Erlaubt nur fuer 4BS-EEPs (RORG=0xA5).
        """
        device = next(
            (d for d in self.devices.all() if d.device_id == device_id),
            None,
        )
        if not device:
            raise ValueError(f"Geraet '{device_id}' nicht gefunden")
        channel = next(
            (c for c in device.channels if c.channel_id == channel_id),
            None,
        )
        if not channel:
            raise ValueError(
                f"Channel '{channel_id}' in Geraet '{device_id}' nicht gefunden"
            )

        # Welche Sender-ID anlernen?
        if sender_id_hex:
            target_sid = sender_id_hex.upper()
            binding = next(
                (s for s in channel.senders if s.sender_id.upper() == target_sid),
                None,
            )
            if not binding:
                raise ValueError(
                    f"Sender-ID {target_sid} ist nicht in channel.senders — "
                    "erst im Edit-Dialog hinzufuegen"
                )
            sender_hex = binding.sender_id
            via_gateway = binding.via_gateway
        else:
            sender_hex = channel.active_sender_id
            via_gateway = channel.active_via_gateway
            if not sender_hex:
                raise ValueError(
                    "Kein aktiver Sender am Channel — erst im Edit-Dialog"
                    " eine Sende-ID hinzufuegen + als aktiv markieren"
                )

        try:
            sender_id = int(sender_hex, 16)
        except ValueError as exc:
            raise ValueError(
                f"Sender-ID '{sender_hex}' ist kein gueltiges Hex"
            ) from exc

        # EEP parsen: nur 4BS (A5-XX-XX) unterstuetzen
        eep = (channel.eep or "").upper()
        if not eep.startswith("A5-") or len(eep.split("-")) != 3:
            raise ValueError(
                f"Lerntelegramm-Send nur fuer 4BS-EEPs (A5-XX-XX) unterstuetzt"
                f" — Channel-EEP ist '{channel.eep}'"
            )
        _, func_hex, type_hex = eep.split("-")
        try:
            func = int(func_hex, 16)
            type_ = int(type_hex, 16)
        except ValueError as exc:
            raise ValueError(f"EEP {eep} hat ungueltige Hex-Bytes") from exc

        # Eltako-Aktoren (FSR/FSB/FUD/F3Z + OPUS-kompatible) erwarten das
        # Lerntelegramm mit Hersteller-ID 0x00D (Eltako) — NICHT 0x7FF.
        # Verifiziert aus "Inhalte der Eltako-Funktelegramme":
        #   A5-3F-7F (FSB Rolladen) -> 0xFFF80D80
        #   A5-38-08 (FSR/FUD)      -> 0xE0400D80
        # Mit 0x7FF ergibt sich FFFFFF80 und der Aktor ignoriert das Telegramm
        # (blinkt im LRN-Modus einfach weiter).
        frame = encode_4bs_teach_in(sender_id, func, type_, manufacturer_id=0x00D)

        # Gateway-Auswahl: zuerst Sender-spezifisches via_gateway, sonst
        # ueber Sender-ID-Block (M60), sonst erstes enabled Gateway.
        if not via_gateway:
            via_gateway = find_gateway_for_sender(sender_hex, self.cfg.gateways)
        if via_gateway:
            gw_names = [via_gateway]
        else:
            gw_names = [g.name for g in self.cfg.gateways if g.enabled][:1]
        if not gw_names:
            raise ValueError(
                "Kein Gateway verfuegbar — entweder am Sender ein 'via_gateway'"
                " setzen oder mind. 1 enabled Gateway in der Konfig"
            )

        await self._send_to_gateways(frame, gw_names, device_id, channel_id)

        log.info(
            "Lerntelegramm gesendet: %s/%s eep=%s sender=%08X via=%s",
            device_id, channel_id, eep, sender_id, gw_names[0],
        )
        return {
            "ok": True,
            "sender_id": f"{sender_id:08X}",
            "gateway": gw_names[0],
            "eep": eep,
            "func": f"0x{func:02X}",
            "type": f"0x{type_:02X}",
        }

    def notify_reman_received(self, sender_id_hex: str, payload_hex: str = "") -> None:
        """
        M85/M89: Wird von der Pipeline bei jedem eingehenden REMOTE_MAN_COMMAND
        aufgerufen. Weckt wartende Pairing-Coroutinen NUR bei einer echten
        Aktor-Antwort.

        Verifiziert aus Protokoll-Mitschnitt: Die Aktor-Antwort hat
        ein gesetztes High-Nibble in der Function (0x06xx) — z.B. QueryID-
        Response = 0x0604. Unsere eigenen Befehle/Echos haben 0x00xx
        (0x0001/0x0002/0x0004). Nur Function >= 0x0100 (Response) weckt.
        """
        try:
            pl = bytes.fromhex(payload_hex) if payload_hex else b""
        except ValueError:
            pl = b""
        if len(pl) >= 2:
            function = (pl[0] << 8) | pl[1]
            # M90: Unsere eigenen gesendeten Befehle/Echos ignorieren
            # (Unlock 0x0001, Lock 0x0002, Query 0x0004, SetLinkTable 0x0212).
            # Aktor-Antworten wecken: QueryID-Response 0x0604, ReComAck 0x0006.
            if function in (0x0001, 0x0002, 0x0004, 0x0212):
                return
        # payload an die wartende Coroutine mitgeben (Function-Erkennung dort)
        for fut in list(self._reman_waiters):
            if not fut.done():
                try:
                    fut.set_result((sender_id_hex, payload_hex))
                except Exception:  # noqa: BLE001
                    pass

    async def _await_reman(self, timeout: float):
        """Wartet auf die naechste Aktor-ReMan-Antwort. Liefert
        (sender_id_hex, payload_hex) oder None bei Timeout."""
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._reman_waiters.append(fut)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            if fut in self._reman_waiters:
                self._reman_waiters.remove(fut)

    async def send_reman_pairing(
        self,
        security_code_hex: str,
        sender_id_hex: str,
        gateway: str | None = None,
        actor_id_hex: str | None = None,
        eep: str = "D2-01-01",
        max_attempts: int = 12,
    ) -> dict[str, Any]:
        """
        M89/M90: OPUS-BRiDGE-Pairing via EnOcean Remote Management.

        Vollstaendige Pairing-Sequenz (aus Protokoll-Mitschnitt):
          Phase 1 (Identifikation):
            Lock → Unlock 2× (300ms) → QueryID (Fn 0x0004)
            → Aktor antwortet QueryID-Response (Fn 0x0604), Source = Aktor-EOID
          Phase 2 (Verknuepfung):
            SetLinkTableContent (Fn 0x0212, adressiert an Aktor) traegt unsere
            Sende-ID (sender_id) als Inbound-Link ein
            → Aktor antwortet ReComAck (Fn 0x0006)
          Lock zum Abschluss.

        Danach akzeptiert der Aktor Schaltbefehle von sender_id.
        """
        try:
            security_code = int(security_code_hex, 16)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Security-Code '{security_code_hex}' kein Hex") from exc
        try:
            sender_eoid = int(sender_id_hex, 16)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Sende-ID '{sender_id_hex}' kein Hex") from exc

        # EEP parsen (z.B. "D2-01-01")
        try:
            er, ef, et = (int(x, 16) for x in eep.split("-"))
        except (ValueError, AttributeError):
            er, ef, et = 0xD2, 0x01, 0x01

        if not gateway:
            gateway = find_gateway_for_sender(sender_id_hex, self.cfg.gateways)
        if gateway:
            gw_names = [gateway]
        else:
            gw_names = [g.name for g in self.cfg.gateways if g.enabled][:1]
        if not gw_names:
            raise ValueError("Kein Gateway verfuegbar zum Pairing-Senden")

        gw_obj = self.manager.gateways.get(gw_names[0])
        chip_id = getattr(gw_obj, "chip_id", None) if gw_obj else None

        lock = encode_reman_lock(security_code)
        unlock = encode_reman_unlock(security_code)
        query = encode_reman_query_id()

        # ---- Phase 1: Identifikation (QueryID → 0x0604) ----
        actor_id = None
        attempts = 0
        for attempt in range(max_attempts):
            attempts = attempt + 1
            await self._send_to_gateways(lock, gw_names, "pairing", "lock")
            await asyncio.sleep(0.1)
            await self._send_to_gateways(unlock, gw_names, "pairing", "unlock")
            await asyncio.sleep(0.3)
            await self._send_to_gateways(unlock, gw_names, "pairing", "unlock")
            await asyncio.sleep(0.3)
            # QueryID + warten auf Antwort in einem Schritt
            waiter = asyncio.ensure_future(self._await_reman(2.0))
            await self._send_to_gateways(query, gw_names, "pairing", "query-id")
            resp = await waiter
            if resp:
                actor_id = resp[0]
                break

        # Fallback auf vom User vorgegebene Aktor-ID
        if (not actor_id or actor_id == "00000000") and actor_id_hex:
            actor_id = actor_id_hex.upper()

        # ---- Phase 2: SetLinkTable (Sende-ID eintragen → 0x0006) ----
        link_ok = False
        if actor_id and actor_id != "00000000":
            try:
                actor_int = int(actor_id, 16)
                slt = encode_reman_set_linktable(
                    actor_int, sender_eoid, er, ef, et,
                    chip_id=(chip_id or 0), index=0, channel=0, inbound=True,
                )
                for _ in range(3):
                    await self._send_to_gateways(unlock, gw_names, "pairing", "unlock")
                    await asyncio.sleep(0.3)
                    waiter = asyncio.ensure_future(self._await_reman(2.0))
                    await self._send_to_gateways(slt, gw_names, "pairing", "set-linktable")
                    ack = await waiter
                    if ack:
                        # ReComAck (Fn 0x0006) oder irgendeine Antwort = Erfolg
                        link_ok = True
                        break
            except (ValueError, TypeError) as exc:
                log.error("SetLinkTable-Fehler: %s", exc)

        await self._send_to_gateways(lock, gw_names, "pairing", "lock")

        log.info(
            "ReMan-Pairing: code=%08X via=%s versuche=%d aktor=%s linktable_ok=%s",
            security_code, gw_names[0], attempts, actor_id, link_ok,
        )
        return {
            "ok": True,
            "sender_id": sender_id_hex.upper(),
            "gateway": gw_names[0],
            "attempts": attempts,
            "actor_responded": actor_id is not None,
            "linktable_written": link_ok,
            "actor_id": actor_id if actor_id and actor_id != "00000000" else None,
        }

    def _resolve_sender_id(self, channel: DeviceChannel) -> int | None:
        """
        Welche Sender-ID nutzen wir beim Senden?

        Reihenfolge (M59):
        1. channel.active_sender_id (Multi-Sender-Konzept) — Default
        2. channel.learned_pair_id (Legacy-Fallback)
        3. None -> nicht sendbar
        """
        # M59: aktiver SenderBinding hat Vorrang
        sid = channel.active_sender_id
        if not sid:
            sid = channel.learned_pair_id
        if sid:
            try:
                return int(sid, 16)
            except ValueError:
                return None
        return None

    def _device_floors(self, device: Device) -> list[str]:
        """Floor-Tags des Geraets fuer Gateway-Selector."""
        floors: list[str] = []
        if device.floor:
            floors.append(device.floor)
        # Plus: Tags aus dem ersten Channel (falls dort gepflegt)
        if device.channels:
            tags = device.channels[0].meta.get("tags", [])
            if isinstance(tags, list):
                floors.extend(t for t in tags if isinstance(t, str))
        return floors

    def _set_frame_destination(self, frame: CommandFrame, dest_id: int) -> CommandFrame:
        """
        M87: Setzt die Destination-ID eines RADIO_ERP1-Frames (adressiertes
        Telegramm statt Broadcast). Patcht die optional-Bytes:
            optional = [sub_tel 1B][destination 4B][dBm 1B][security 1B]
        """
        from .gateway.esp3 import ESP3Packet
        pkt = frame.packet
        opt = bytearray(pkt.optional)
        if len(opt) >= 5:
            opt[1:5] = dest_id.to_bytes(4, "big")
        new_pkt = ESP3Packet(
            packet_type=pkt.packet_type,
            data=pkt.data,
            optional=bytes(opt),
        )
        return CommandFrame(
            packet=new_pkt,
            sender_id=frame.sender_id,
            destination_id=dest_id,
        )

    def _shutter_visu_command(
        self, command: dict[str, Any]
    ) -> dict[str, Any] | None:
        """
        M124: Uebersetzt das Timberwolf-Visu-Schema {"move"/"step"/"stop": v}
        in einen internen Rolladen-Befehl. Richtung: 0=Auf/up, 1=Ab/down.

          {"move": 0|1}  -> volle Fahrt ({"command": up/down, "full": True})
          {"step": 0|1}  -> Tipp       ({"command": up/down, "duration_s": ...})
          {"stop": true} -> {"command": "stop"};  false/0 -> None (ignorieren)

        Bei invertiertem Aktor hier 0/1 tauschen.
        """
        if "stop" in command:
            return {"command": "stop"} if _parse_state(command["stop"]) else None
        if "move" in command:
            up = not _parse_state(command["move"])
            return {"command": "up" if up else "down", "full": True}
        if "step" in command:
            up = not _parse_state(command["step"])
            return {"command": "up" if up else "down", "duration_s": SHUTTER_STEP_S}
        return command

    def _normalize_value_command(
        self,
        channel: DeviceChannel,
        command: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Mappt einen nackten Wert ({"value": N}, von _handle_incoming aus einer
        reinen Zahl-Payload erzeugt) auf den fachlich passenden Befehl des
        Aktortyps. Andere/zusammengesetzte Commands bleiben unveraendert.
          shutter -> {"position": N}   (0..100)
          dimmer  -> {"dim": N}        (0..100)
          switch  -> {"state": bool}
        """
        if set(command.keys()) != {"value"}:
            return command
        val = command["value"]
        kind = self._kind(channel)
        if kind == "shutter":
            try:
                return {"position": max(0.0, min(100.0, float(val)))}
            except (TypeError, ValueError):
                return command
        if kind == "dimmer":
            try:
                return {"dim": max(0, min(100, int(float(val))))}
            except (TypeError, ValueError):
                return command
        if kind == "switch":
            return {"state": _parse_state(val)}
        return command

    def _apply_dimmer_onoff_memory(
        self,
        device_id: str,
        channel_id: str,
        channel: DeviceChannel,
        command: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Uebersetzt einen reinen An/Aus-Befehl an einen DIMMER in einen
        Dim-Wert, weil Eltako-Dimmer (FUD/FDG/FLD) den A5-38-08-Schaltbefehl
        (Command 1) ignorieren — nur Command 2 (Dimmen mit Wert) wirkt:
          {"state": true}  -> auf den lokal gemerkten Dimmwert (actor_state;
                              0 -> 100% als Notnagel)
          {"state": false} -> auf 0
        Hat der Befehl bereits einen "dim"-Wert, bleibt er unveraendert.
        """
        if (self._kind(channel) != "dimmer" or "dim" in command
                or not ("state" in command or "on" in command)):
            return command
        want_on = _parse_state(command.get("state", command.get("on")))
        if not want_on:
            return {**command, "dim": 0}
        remembered = 0
        if self.state_store:
            st = self.state_store.get(device_id, channel_id)
            remembered = int(round(st.dim_percent or 0))
        return {**command, "dim": remembered if remembered > 0 else 100}

    def _build_frame(
        self,
        channel: DeviceChannel,
        sender_id: int,
        command: dict[str, Any],
    ) -> CommandFrame | None:
        """
        Waehlt den richtigen Encoder. Primaer ueber EEPProfile.encoder
        (Single Source of Truth). Fallback ueber Legacy-Heuristik fuer
        UNKNOWN-EEPs mit device_config-Hint.
        """
        eep = (channel.eep or "").upper()
        cfg = (channel.meta.get("device_config") or "").lower()

        # A5-10 Sollwert-TX (Heizregler): den Verschiebungs-Bereich (±K) aus der
        # Kanal-Konfig (SetpointOffsetMin/Max) in den Befehl mappen, damit der
        # Encoder die Verschiebung korrekt auf das DB2-Byte (0..255) abbildet.
        if eep.startswith("A5-10") and (
            "offset_min" not in command or "offset_max" not in command
        ):
            pairs: dict[str, str] = {}
            for part in (channel.meta.get("device_config") or "").split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    pairs[k.strip()] = v.strip()
            command = dict(command)
            for src, dst in (("SetpointOffsetMin", "offset_min"),
                             ("SetpointOffsetMax", "offset_max")):
                if dst not in command and src in pairs:
                    try:
                        command[dst] = float(pairs[src])
                    except ValueError:
                        pass

        # Profile-Encoder bevorzugen
        profile = get_profile_registry().get(eep)
        if profile and profile.encoder:
            try:
                frame = profile.encoder(sender_id, command)
                if frame is not None:
                    return frame
            except (ValueError, KeyError) as exc:
                log.error("EEP %s Encoder-Fehler: %s", eep, exc)
                return None

        # Legacy-Fallback fuer Channels ohne EEP-Profile aber mit device_config
        is_eltako_dim = "eltako_pseudocentralcommand_dimming" in cfg
        is_eltako_switch_cfg = (
            "eep_a53808_01_centralcommand_switching" in cfg and "eltako" in cfg
        )
        if is_eltako_switch_cfg:
            state = _parse_state(command.get("state", False))
            return encode_eltako_switch(sender_id, state)
        if is_eltako_dim:
            if "dim" in command:
                dim = int(command["dim"])
                ramp = int(command.get("ramp", 0))
                on = _parse_state(command.get("state", dim > 0))
                return encode_a5_38_08_dim(sender_id, dim, ramp_s=ramp, on=on)
            state = _parse_state(command.get("state", False))
            return encode_a5_38_08_switch(sender_id, state)
        return None

    async def _handle_position_command(
        self,
        device_id: str,
        channel_id: str,
        command: dict[str, Any],
    ) -> None:
        """
        Faehrt einen Rolladen zu einer absoluten Position (0-100%).

        Berechnet aus aktueller Position + Ziel + Laufzeit die nötige
        Fahrtdauer und sendet einen Eltako-Shutter-Befehl.
        """
        if not self.state_store:
            log.error("Position-Befehl ohne ActorStateStore unmoeglich")
            return

        state = self.state_store.get(device_id, channel_id)
        if not state.calibrated:
            log.warning("TX: Position-Befehl an %s/%s aber nicht eichgefahren",
                        device_id, channel_id)

        target = float(command["position"])
        target = max(0.0, min(100.0, target))

        # Aktuelle Position berechnen (falls gerade Bewegung lief)
        current = estimate_position_now(state)
        if state.moving:
            commit_movement(state)
            current = state.position_percent

        diff = target - current
        if abs(diff) < 1.0:
            log.info("TX position %s/%s: bereits an Ziel (%.1f%%)",
                     device_id, channel_id, current)
            return

        direction = "down" if diff > 0 else "up"
        # Richtungsabhaengige Laufzeit (Heben dauert laenger als Senken)
        tt = state.time_for(direction)
        duration_s = abs(diff) / 100.0 * tt
        # Etwas Puffer einbauen damit Endlage zuverlaessig erreicht wird wenn 0 oder 100
        if target <= 0.1 or target >= 99.9:
            duration_s = tt + 2.0

        log.info("TX position %s/%s: %.1f%% -> %.1f%% (%s, %.1fs)",
                 device_id, channel_id, current, target, direction, duration_s)

        # State markieren
        state.moving = direction
        state.moving_started_at = time.time()
        state.moving_target = target
        state.position_percent = current  # Startpunkt fixieren
        state.last_command = f"position={target:.0f}"
        state.last_command_at = time.time()
        self.state_store.mark_dirty()

        # Klassischen Befehl an den Aktor senden
        await self.handle_command(
            device_id, channel_id,
            {"command": direction, "duration_s": duration_s},
        )

    async def _send_to_gateways(
        self,
        frame: CommandFrame,
        gw_names: list[str],
        device_id: str,
        channel_id: str,
    ) -> None:
        sent = 0
        for name in gw_names:
            gw = self.manager.gateways.get(name)
            if not gw:
                log.warning("TX: Gateway %s nicht im Manager", name)
                continue
            try:
                await gw.send(frame.packet)
                sent += 1
                log.info(
                    "TX %s/%s via %s sender=%08X dest=%08X data=%s",
                    device_id, channel_id, name,
                    frame.sender_id, frame.destination_id,
                    frame.packet.data.hex(),
                )
                # M84: ausgehendes Telegramm ins Live-Log eintragen
                if self.on_tx:
                    try:
                        # RORG/Typ-Name + Payload-Hex aus dem Frame ableiten.
                        pkt = frame.packet
                        if pkt.packet_type == 0x01 and len(pkt.data) >= 6:
                            # RADIO_ERP1: [RORG][payload][sender4][status]
                            rorg = pkt.data[0]
                            payload_hex = pkt.data[1:-5].hex()
                            try:
                                from .gateway.esp3 import RORG as _RORG
                                rorg_name = _RORG(rorg).name
                            except Exception:  # noqa: BLE001
                                rorg_name = f"0x{rorg:02X}"
                        else:
                            rorg_name = pkt.packet_type_name
                            payload_hex = pkt.data.hex()
                        tx_label = f"{device_id}/{channel_id}"
                        self.on_tx(
                            f"{frame.sender_id:08X}", rorg_name,
                            payload_hex, name, tx_label,
                        )
                    except Exception:  # noqa: BLE001
                        pass
            except Exception as exc:  # noqa: BLE001
                log.error("TX-Fehler ueber %s: %s", name, exc)
        if sent == 0:
            log.error("TX: Konnte ueber keinen GW senden (versucht: %s)", gw_names)


def _parse_state(value: Any) -> bool:
    """JSON-Wert in bool umwandeln, tolerant gegenueber Strings."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        return v in ("on", "true", "1", "an", "ein", "open", "up")
    return False
