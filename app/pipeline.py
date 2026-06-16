"""
Verarbeitungs-Pipeline: nimmt ReceivedTelegrams aus dem GatewayManager,
dekodiert via EEP-Registry, looked up Device in der Registry,
und veröffentlicht Raw + Named-Topic auf MQTT.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .cascade import Cascade
from .devices import Device, DeviceChannel, DeviceRegistry
from .eep import decode_telegram, get_profile_registry
from .gateway import GatewayManager, ReceivedTelegram
from .mqtt_client import MQTTPublisher
from .tx_router import classify_channel_kind, ptm_press_is_on

log = logging.getLogger(__name__)


_INVERT_STATE_MAP = {
    "moving_up": "moving_down",
    "moving_down": "moving_up",
    "end_up": "end_down",
    "end_down": "end_up",
}


def _tele_channel_matches(actual, expected) -> bool:
    """
    Vergleicht decoded[telegram_channel_field] mit channel.meta.tele_channel.
    Generisch fuer Int (DSZ14DRS-Doppeltarif, channel=0/1) UND String
    (FT55-Wippentaster, rocker_side='A'/'B').
    """
    try:
        return int(actual) == int(expected)
    except (TypeError, ValueError):
        return str(actual) == str(expected)


def _eep_rorg(eep: str | None) -> int | None:
    """
    RORG-Byte aus einem EEP-String (das erste Segment ist hex-codiert):
    'F6-02-01' -> 0xF6, 'D2-01-XX' -> 0xD2, 'A5-12-01' -> 0xA5.
    None wenn nicht parsbar.
    """
    if not eep:
        return None
    try:
        return int(eep.split("-", 1)[0], 16)
    except (ValueError, IndexError):
        return None


# A5-12-XX (Eltako-Zaehlerfamilie) — Mapping eines Field-Filters auf das
# fachlich passende Feld eines anderen A5-12-EEPs. Wird verwendet bei
#   (a) Auto-EEP-Wechsel via Lerntelegramm (PCT14-Folgesync) — siehe _process
#   (b) Manueller EEP-Umschaltung im Edit-Dialog (POST .../switch-eep)
# Beispiel: ein Strom-Channel mit field=energy_kwh wird auf A5-12-02 (Gas)
# umgestellt -> field wechselt auf volume_m3. Damit publishen pipeline +
# UI die korrekte Einheit, ohne dass der User manuell yaml editieren muss.
A5_12_FIELD_MAP: dict[str, dict[str, str]] = {
    # Zaehlerstaende
    "energy_kwh": {"A5-12-01": "energy_kwh", "A5-12-02": "volume_m3",
                   "A5-12-03": "volume_m3"},
    "volume_m3":  {"A5-12-01": "energy_kwh", "A5-12-02": "volume_m3",
                   "A5-12-03": "volume_m3"},
    # Momentanverbrauch / Durchfluss
    "current_w":  {"A5-12-01": "current_w",  "A5-12-02": "flow_m3h",
                   "A5-12-03": "flow_l_min"},
    "flow_m3h":   {"A5-12-01": "current_w",  "A5-12-02": "flow_m3h",
                   "A5-12-03": "flow_l_min"},
    "flow_l_min": {"A5-12-01": "current_w",  "A5-12-02": "flow_m3h",
                   "A5-12-03": "flow_l_min"},
}


def remap_a5_12_field(old_field: str | None, new_eep: str | None) -> str | None:
    """
    Mappt einen A5-12-Field-Filter auf den fachlich passenden Feldnamen unter
    einem anderen A5-12-EEP. Liefert None, wenn das Mapping nicht existiert
    (z.B. tariff_independent serial_number — bleibt wie es ist).
    """
    if not old_field or not new_eep:
        return None
    mapping = A5_12_FIELD_MAP.get(old_field)
    if not mapping:
        return None
    return mapping.get(new_eep)


def _invert_shutter_direction(decoded: dict) -> dict:
    """
    Vertauscht moving_up/moving_down und end_up/end_down im decoded-dict.
    Wird bei Rolladen-Aktoren mit physisch invertierter Verkabelung verwendet
    (channel.meta.invert_direction = True).
    """
    state = decoded.get("state")
    if state in _INVERT_STATE_MAP:
        new = dict(decoded)
        new["state"] = _INVERT_STATE_MAP[state]
        return new
    return decoded


class TelegramPipeline:
    def __init__(
        self,
        manager: GatewayManager,
        publisher: MQTTPublisher,
        devices: DeviceRegistry,
        cascade: Cascade | None = None,
    ) -> None:
        self.manager = manager
        self.publisher = publisher
        self.devices = devices
        self.cascade = cascade or Cascade()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._unknown_seen: set[str] = set()  # Senders die wir noch nicht kennen — für Anlern-UI
        # Plausibilitäts-Cache fuer monoton steigende Werte (z.B. Energiezähler).
        # Schluessel: (sender_id, telegram_channel, field) -> letzter Wert.
        # Bei energy_kwh-Telegrammen verwerfen wir Werte die unplausibel niedriger
        # sind als der letzte (z.B. Eltako DSZ14DRS sendet zwischendurch Sub-Tarif
        # mit value=0 obwohl Hauptzähler bei 42148 ist).
        self._monotonic_cache: dict[tuple, float] = {}
        # Cache fuer Seriennummer-Teile bei A5-12-01 (DSZ14DRS sendet alle
        # 10 Minuten 2 Teile). Wenn beide Teile bekannt sind, ergaenzen wir
        # decoded["serial_number"] mit dem vollstaendigen String "S-AABBCC".
        # Schluessel: sender_hex -> {"high": "46", "low": "0261"}
        self._serial_cache: dict[str, dict] = {}
        # Press-Release-Tracking fuer RPS-Wippentaster (F6-02-*):
        # Beim Druck-Event (event in A_top/A_bottom/B_top/B_bottom) speichern
        # wir den Timestamp pro (sender_id, rocker_side). Beim Release-Event
        # (rocker_side=None) wird die Differenz zum letzten Press berechnet
        # und als press_duration_ms an decoded angehaengt — damit MQTT-
        # Subscriber kurz/lang differenzieren koennen.
        # Schluessel: sender_id_upper -> {rocker_side: (ts, event)}
        self._press_starts: dict[str, dict[str, tuple[float, str]]] = {}
        # Web-UI Hooks (werden von server.py injiziert wenn UI laeuft)
        self.on_telegram_post = None  # callable(rx, device_match: dict|None)
        # Aktor-Feedback-Hook (wird von main.py mit tx_router.handle_feedback verknüpft)
        self.on_actor_feedback = None  # callable(sender_id_hex, decoded_dict)
        # Pfad zu devices.yaml fuer Auto-Save bei EEP-Wechsel via Lerntelegramm.
        # Wird von main.py gesetzt; wenn None, kein Auto-Save (z.B. in Tests).
        self.devices_save_path = None
        # M61: Observed-Sender-Registry — wird von main.py injiziert.
        # Wenn gesetzt: bei jedem Telegramm pruefen ob die Sender-ID zu einem
        # bekannten Gateway-Block gehoert UND keinem Channel zugewiesen ist —
        # dann als observed speichern damit sie spaeter nicht als "frei" fuer
        # Neuanlernen vorgeschlagen wird.
        self.observed_senders = None
        # Liste der Gateway-Configs fuer Block-Matching (von main.py gesetzt)
        self._gateway_configs = None
        # Globale Defaults (cfg.defaults) — fuer ptm_on_press-Polung beim
        # Ableiten des PTM-Schaltzustands. Von main.py injiziert.
        self.defaults = None

    @property
    def unknown_senders(self) -> set[str]:
        return self._unknown_seen

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="pipeline")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                rx: ReceivedTelegram = await asyncio.wait_for(
                    self.manager.rx_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            try:
                await self._process(rx)
            except Exception as exc:  # noqa: BLE001
                log.exception("Pipeline-Fehler: %s", exc)

    async def _process(self, rx: ReceivedTelegram) -> None:
        # Cascade entscheidet ob Telegramm durchgereicht wird (Dedup +
        # RSSI-Tracking). Bei Duplikaten: nichts publishen.
        if not self.cascade.handle(rx):
            return

        tel = rx.telegram
        sender_hex = tel.sender_id_hex

        # Multi-Lookup: ein Sender kann mehrere Channels haben (FWZ14 sendet
        # kWh + W; FWS61 sendet Wetter + Sonne).
        matches = self.devices.lookup_all_by_sender_id(sender_hex)

        # M61: Observed-Sender — wenn das Telegramm zu einem GW-Block gehoert
        # aber keinem Channel/PTM/Observer zugewiesen ist, persistieren.
        # Damit erkennt das System auch "fremde" Sender (z.B. Wand-PTMs die
        # noch nirgends als Channel oder Beobachter angelegt sind) und kann
        # die Gateway-Block-Free-Liste sauber halten.
        if self.observed_senders and self._gateway_configs:
            try:
                from .sender_routing import find_gateway_for_sender
                # Nur wenn der Sender nirgends bekannt ist
                ptm_matches = self.devices.lookup_actors_by_ptm(sender_hex)
                obs_matches = self.devices.lookup_actors_observing(sender_hex)
                if not matches and not ptm_matches and not obs_matches:
                    gw_name = find_gateway_for_sender(
                        sender_hex, self._gateway_configs,
                    )
                    if gw_name:
                        self.observed_senders.record(
                            gateway=gw_name,
                            sender_id=sender_hex,
                            rorg=tel.rorg_name,
                            rssi_dbm=tel.rssi_dbm,
                        )
                        # Persistiert throttled (max alle 30s)
                        self.observed_senders.save()
            except Exception as exc:  # noqa: BLE001
                log.debug("ObservedSender-Hook-Fehler: %s", exc)
        # Normalfall: alle Channels eines Senders haben dasselbe EEP -> EEP des
        # ersten Channels nehmen. SONDERFALL OPUS Bridge UP-eSchalter: dieselbe
        # RX-ID traegt einen D2-01-Aktor-Status UND ein aufgesetztes F6-02-PTM-
        # Wippensignal — also Channels mit UNTERSCHIEDLICHEN RORGs. Dann muss
        # der Decoder zum tatsaechlichen RORG des Telegramms passen, sonst
        # zerkaut z.B. der D2-01-Decoder ein RPS-Wippentelegramm zu cmd:0.
        match_rorgs = {_eep_rorg(c.eep) for _d, c in matches if c.eep}
        mixed_rorg = len({r for r in match_rorgs if r is not None}) > 1
        eep = matches[0][1].eep if matches else None
        if matches and mixed_rorg:
            for _d, c in matches:
                if c.eep and _eep_rorg(c.eep) == tel.rorg:
                    eep = c.eep
                    break
        decoded = decode_telegram(eep, tel)

        # Press-Release-Tracking fuer RPS-Wippentaster: beim Press-Event den
        # Timestamp + event pro (sender_id, rocker_side) merken, beim Release
        # die Differenz als press_duration_ms anhaengen UND last_press_event
        # mit dem ursprung-event setzen — damit UI/MQTT auch nach dem Release
        # weiss WELCHE Taste gedrueckt war (sonst nur "release").
        event = decoded.get("event")
        if event in ("A_top", "A_bottom", "B_top", "B_bottom"):
            side = decoded.get("rocker_side")
            if side:
                self._press_starts.setdefault(sender_hex, {})[side] = (rx.received_at, event)
        elif event == "release":
            starts = self._press_starts.get(sender_hex)
            if starts:
                # Letzter Druck egal welche Wippe.
                latest_side = max(starts, key=lambda k: starts[k][0])
                last_ts, last_evt = starts[latest_side]
                duration_ms = int((rx.received_at - last_ts) * 1000)
                if duration_ms >= 0:
                    decoded["press_duration_ms"] = duration_ms
                decoded["last_press_event"] = last_evt
                # Release-Telegramm an die zuletzt gedrueckte Wippe zuordnen,
                # damit das Sub-Cache (sender_id#rocker_side) und das Channel-
                # Routing (meta.tele_channel) zum richtigen Channel finden.
                # Sonst landet press_duration_ms im "#None"-Cache, den kein
                # Channel je liest -> Anzeige + MQTT Sub-Topic gehen verloren.
                decoded["rocker_side"] = latest_side
                # press_start konsumieren — beim naechsten Release wird die
                # ggf. andere Wippe genommen (falls beide gedrueckt waren).
                del starts[latest_side]
                if not starts:
                    self._press_starts.pop(sender_hex, None)

        # Rolladen-Aktor: physisch invertierte Verkabelung kann via
        # channel.meta.invert_direction kompensiert werden. Wir wenden das
        # einmalig auf decoded an, damit alle Konsumenten (Live-Log, MQTT,
        # _apply_feedback) den korrigierten Zustand sehen.
        if matches and (matches[0][1].meta or {}).get("invert_direction"):
            decoded = _invert_shutter_direction(decoded)

        # M69: Dim-Speed-Sync — wenn der Aktor in seinem Status-Telegramm
        # ramp_s > 0 zurueckmeldet (manche Eltako-Dimmer tun das), uebernehmen
        # wir den Wert in channel.meta.dim_speed. FLD61 sendet immer 0 zurueck
        # (= "intern eingestellt"), das ignorieren wir.
        if (decoded.get("command") == 2 and decoded.get("feedback") is True
                and decoded.get("ramp_s") and matches):
            speed_from_actor = int(decoded["ramp_s"])
            if speed_from_actor > 0:
                changed = False
                for _d_cand, c_cand in matches:
                    if c_cand.meta.get("dim_speed") != speed_from_actor:
                        c_cand.meta["dim_speed"] = speed_from_actor
                        changed = True
                if changed and self.devices_save_path:
                    try:
                        self.devices.save(self.devices_save_path)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("dim_speed-Feedback-save fehlgeschlagen: %s", exc)

        # A5-12-XX Auto-EEP-Wechsel via Lerntelegramm (PCT14-Folgesync).
        # Wenn der Decoder ein detected_eep liefert (Eltako-Lerntelegramm mit
        # Sub-TYPE 1/2/3) und ein gefundener Channel ein anderes EEP hat, stellen
        # wir das Channel-EEP automatisch um. Field-Filter werden gemappt
        # (energy_kwh<->volume_m3, current_w<->flow_m3h/flow_l_min).
        detected_eep = decoded.get("detected_eep")
        if detected_eep and matches and detected_eep.startswith("A5-12-"):
            changed = False
            for _d_cand, c_cand in matches:
                old_eep = c_cand.eep
                if old_eep == detected_eep:
                    continue
                if old_eep and old_eep.startswith("A5-12-"):
                    log.info(
                        "Auto-EEP-Wechsel via Lerntelegramm: %s/%s %s -> %s",
                        _d_cand.device_id, c_cand.channel_id,
                        old_eep, detected_eep,
                    )
                    c_cand.eep = detected_eep
                    # field-Filter mappen wenn vorhanden
                    if c_cand.meta and "field" in c_cand.meta:
                        new_field = remap_a5_12_field(c_cand.meta["field"], detected_eep)
                        if new_field:
                            c_cand.meta["field"] = new_field
                    changed = True
            if changed and self.devices_save_path:
                try:
                    self.devices.save(self.devices_save_path)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Konnte devices.yaml nach Auto-EEP-Wechsel nicht speichern: %s", exc)

        # Seriennummer-Aggregat (A5-12-01 DSZ14(W)DRS): wenn dieses Telegramm
        # einen Teil bringt UND der andere Teil bereits gesehen wurde,
        # ergaenzen wir decoded["serial_number"] mit der vollstaendigen Nummer.
        # Damit kann der serial_number-Channel published werden.
        if "serial_high" in decoded or "serial_low" in decoded:
            sn = self._serial_cache.setdefault(sender_hex, {})
            if "serial_high" in decoded:
                sn["high"] = decoded["serial_high"]
            if "serial_low" in decoded:
                sn["low"] = decoded["serial_low"]
            if "high" in sn and "low" in sn:
                decoded["serial_number"] = f"S-{sn['high']}{sn['low']}"

        # STRENGE Monotonie-Pruefung fuer energy_kwh.
        # Hintergrund: HomeAssistant-Tageszaehler-Statistiken berechnen den
        # Tagesverbrauch als Differenz zwischen aktuellem Zaehlerstand und dem
        # zu Tagesbeginn. Wenn der Zaehler temporaer einen NIEDRIGEREN Wert
        # meldet (Bus-Artefakt, Sub-Tarif-Reset, Nachttarif-Init mit 0, ...),
        # interpretiert HA das spaeter beim Reset als "Tagesverbrauch =
        # (alter Stand) + (echter Stand)" und zeigt 50 MW an einem Tag.
        #
        # Strategie:
        #   - Erste Lesung mit Wert 0 wird verworfen (typisch bei DSZ14DRS-
        #     Sub-Tarif der nicht aktiv ist) — die UI/MQTT bekommen erst dann
        #     einen Wert, wenn ein echter Zaehlerstand reinkommt
        #   - Spaetere Lesung muss >= letzten bekannten Stand sein
        # Cache-Schluessel: (sender_id, tariff)
        if "energy_kwh" in decoded:
            new_val = decoded["energy_kwh"]
            tariff = decoded.get("tariff", 0)
            cache_key = (sender_hex, int(tariff), "energy_kwh")
            last_val = self._monotonic_cache.get(cache_key)
            if isinstance(new_val, (int, float)):
                if last_val is None:
                    # Erstmessung: 0-Werte sind verdaechtig (Sub-Tarif inaktiv,
                    # Init-Reset) — verwerfen, bis ein echter Wert kommt.
                    if new_val < 0.001:
                        log.info(
                            "Erste energy_kwh=%s verworfen (vermutlich "
                            "inaktiver Sub-Tarif): %s tariff=%s",
                            new_val, sender_hex, tariff,
                        )
                        return
                    self._monotonic_cache[cache_key] = float(new_val)
                elif new_val < last_val:
                    log.warning(
                        "Monotonie verletzt — verworfen: %s tariff=%s "
                        "energy_kwh %.2f -> %.2f (Zaehlerstand laeuft nie zurueck)",
                        sender_hex, tariff, last_val, new_val,
                    )
                    return
                else:
                    self._monotonic_cache[cache_key] = float(new_val)

        common = {
            "ts": round(rx.received_at, 3),
            "gw": rx.gateway_name,
            "rorg": tel.rorg_name,
            "sender_id": sender_hex,
            "rssi_dbm": tel.rssi_dbm,
            "status": f"0x{tel.status:02X}",
            "destination_id": tel.destination_id_hex,
        }

        # Immer raw-Topic publishen (Diagnose, Anlernen)
        raw_payload = {**common, **decoded}
        await self.publisher.publish_raw(rx.gateway_name, sender_hex, raw_payload)

        # device_match fuer Live-Log + Hook: nicht einfach matches[0], sondern
        # das Match das tatsaechlich zum Telegramm passt (field + tele_channel).
        # Bei DSZ14DRS mit 4 Channels (1_energy_kwh/1_current_w/2_energy_kwh/
        # 2_current_w) muss z.B. ein channel=1-energy_kwh-Telegramm den Channel
        # "2_energy_kwh" als Match haben, nicht den ersten "1_energy_kwh".
        preg_local = get_profile_registry()
        device_match = None
        decoded_clean_match = {k: v for k, v in decoded.items() if not k.startswith("_")}
        for d_cand, c_cand in matches:
            # RORG-Routing (OPUS Bridge): bei gemischten RORGs pro Sender nur
            # den Channel betrachten, dessen EEP zum Telegramm-RORG passt.
            if mixed_rorg and c_cand.eep and _eep_rorg(c_cand.eep) != tel.rorg:
                continue
            m = c_cand.meta or {}
            f_filter = m.get("field")
            if f_filter and f_filter not in decoded_clean_match:
                continue
            t_filter = m.get("tele_channel")
            prof = preg_local.get(c_cand.eep)
            tc_f = prof.telegram_channel_field if prof else None
            if tc_f and t_filter is None:
                # Default-0 nur fuer numerische tc_fields (DSZ14DRS Doppeltarif).
                # Bei String-tc_fields (rocker_side fuer FT55) kein Default —
                # alter 1-Channel-Code soll alle Telegramme weiter sehen.
                sample = decoded_clean_match.get(tc_f)
                if isinstance(sample, (int, bool)) and not isinstance(sample, bool):
                    t_filter = 0
                elif isinstance(sample, int):
                    t_filter = 0
            if t_filter is not None and tc_f and tc_f in decoded_clean_match:
                actual = decoded_clean_match[tc_f]
                # actual=None (z.B. rocker_side beim Release) -> akzeptieren
                if actual is not None and not _tele_channel_matches(actual, t_filter):
                    continue
            device_match = (d_cand, c_cand)
            break

        if matches:
            decoded_clean = {k: v for k, v in decoded.items() if not k.startswith("_")}
            preg = get_profile_registry()
            for device, channel in matches:
                # RORG-Routing (OPUS Bridge): bei gemischten RORGs pro Sender
                # published ein Telegramm nur an Channels mit passendem RORG.
                # F6-PTM-Telegramm -> nur PTM-Channel, D2-Status -> nur Aktor.
                if mixed_rorg and channel.eep and _eep_rorg(channel.eep) != tel.rorg:
                    continue
                meta = channel.meta or {}
                # Field-Filter: wenn channel.meta.field gesetzt ist, published
                # diesen Channel nur dann, wenn das passende Feld im decoded ist
                field_filter = meta.get("field")
                if field_filter and field_filter not in decoded_clean:
                    continue  # dieses Telegramm gehoert nicht zu diesem Channel

                # Telegram-Channel-Filter (z.B. F3Z14D A5-12-01): pruefe ob
                # decoded[telegram_channel_field] == meta.tele_channel.
                # Wenn nicht, gehoert das Telegramm zu einem anderen Sub-Zaehler.
                #
                # WICHTIG: Wenn das EEP einen telegram_channel_field hat (z.B.
                # A5-12-01) aber meta.tele_channel nicht gesetzt ist (einfache
                # FWZ14, DSZ14DRS), dann nehmen wir DEFAULT 0. Hintergrund:
                # Eltako DSZ14DRS-3x65A sendet sporadisch Sub-Tarife (channel=1)
                # mit value=0 die sonst den Haupt-Zaehlerstand auf 0
                # ueberschreiben. Wer mehrere Sub-Tarife will, legt sie explizit
                # als eigene Channels an (wie F3Z14D mit tele_channel=0/1/2).
                tele_chan_filter = meta.get("tele_channel")
                profile = preg.get(channel.eep)
                tc_field = profile.telegram_channel_field if profile else None
                if tc_field and tele_chan_filter is None:
                    # Default-0 nur fuer numerische tc_fields (DSZ14DRS-Doppeltarif).
                    # Bei String-Feldern (rocker_side, FT55) kein Default —
                    # alter 1-Channel-Code soll alle Telegramme weiter sehen.
                    sample = decoded_clean.get(tc_field)
                    if isinstance(sample, int) and not isinstance(sample, bool):
                        tele_chan_filter = 0
                if tele_chan_filter is not None:
                    if tc_field and tc_field in decoded_clean:
                        actual = decoded_clean[tc_field]
                        # actual=None (rocker_side beim Release) -> akzeptieren
                        # (an alle Sub-Channels routen)
                        if actual is not None and not _tele_channel_matches(
                                actual, tele_chan_filter):
                            continue  # anderer Sub-Zaehler / andere Wippe

                # Payload-Aufbau:
                # - Split-Channel (meta.field gesetzt): {"value": <wert>, "unit": "kWh"}
                # - Single-Channel: kompletter decoded-dict plus "_units" Map mit
                #   den Einheiten pro Feld (z.B. {"temperature_c": "°C", ...}).
                # Einheiten kommen aus den FieldDefs des EEP-Profiles.
                # User kann pro Channel die Einheit ueberschreiben
                # (meta.unit_override). Sonst Default aus EEPProfile.FieldDef.
                unit_override = meta.get("unit_override")
                if field_filter:
                    val = decoded_clean[field_filter]
                    payload_fields = {"value": val}
                    if unit_override:
                        payload_fields["unit"] = unit_override
                    elif profile:
                        fdef = profile.get_field(field_filter)
                        if fdef and fdef.unit:
                            payload_fields["unit"] = fdef.unit
                else:
                    payload_fields = dict(decoded_clean)
                    if profile and profile.fields:
                        units = {
                            f.name: f.unit
                            for f in profile.fields
                            if f.unit and f.name in decoded_clean
                        }
                        if units:
                            payload_fields["units"] = units
                    if unit_override:
                        payload_fields["unit"] = unit_override

                # PTM-Schaltzustand fuer die Automatik-Anbindung: F6-02-Wippen-
                # taster liefern Press/Release-Ereignisse. Wir publishen on:true/
                # false als SAUBERES EVENT — NUR beim tatsaechlichen Druck (genau
                # eine Nachricht pro Tastendruck), NICHT beim Loslassen
                # wiederholen. So zaehlt eine Automatik pro Druck genau einen
                # Schritt (EIN-Seite => true, AUS-Seite => false; Polung via
                # ptm_on_press, pro Kanal ueber meta.ptm_on_press). Das Release-
                # Telegramm traegt bewusst KEIN "on".
                if "rocker_action" in decoded_clean:
                    pol = (meta.get("ptm_on_press")
                           or getattr(self.defaults, "ptm_on_press", "I"))
                    on_val = ptm_press_is_on(decoded_clean, pol)
                    if on_val is not None:
                        payload_fields["on"] = on_val
                    # Momentan-Signale fuer flankenbasierte Automatik (z.B.
                    # Dimm-Sequenz pro Tastendruck): taster_ein/taster_aus sind
                    # true GENAU im Druck-Telegramm der jeweiligen Wippenseite
                    # (Polung via ptm_on_press) und false sonst — insbesondere
                    # beim Loslassen. So entsteht pro Druck eine vollstaendige
                    # false->true->false-Flanke, auch bei mehreren Druecken
                    # derselben Seite hintereinander.
                    payload_fields["taster_ein"] = on_val is True
                    payload_fields["taster_aus"] = on_val is False

                # Schalter->Aktor: bei PTM-Telegrammen merken wir das so vor,
                # dass die UI das richtige Topic mitbekommt
                named_payload = {
                    "ts": common["ts"],
                    "device": device.device_id,
                    "channel": channel.channel_id,
                    "name": channel.name,
                    "rssi_dbm": tel.rssi_dbm,
                    "gw": rx.gateway_name,
                    **payload_fields,
                }
                # M121: Rolladen-Channels publishen ihr /state-Topic NICHT hier
                # mit den rohen Eltako-Feedback-Codes (moving_up/end_down/...).
                # Den /state-Topic besitzt der TXRouter und schreibt dort den
                # Aktor-State (Prozent-Position + moving_up/moving_down). Sonst
                # wuerden sich beide Payloads auf dem retained Topic ueberschreiben.
                # Das Feedback-Telegramm aktualisiert den State weiterhin ueber
                # den on_actor_feedback-Hook (handle_feedback) weiter unten.
                if classify_channel_kind(channel) == "shutter":
                    continue
                await self.publisher.publish_device(
                    device, channel, named_payload
                )
                log.info(
                    "[%s] → %s/%s = %s",
                    rx.gateway_name, device.device_id, channel.channel_id,
                    payload_fields,
                )
        else:
            # Unbekannter Sender — vormerken für Anlern-UI (M4)
            if sender_hex not in self._unknown_seen:
                self._unknown_seen.add(sender_hex)
                log.info(
                    "Neuer/unbekannter Sender %s über %s (%s, RSSI %s dBm)",
                    sender_hex, rx.gateway_name, tel.rorg_name, tel.rssi_dbm,
                )

        # Aktor-Feedback-Hook: pruefen ob der Sender einem Aktor zugeordnet ist
        if self.on_actor_feedback:
            try:
                self.on_actor_feedback(sender_hex, decoded)
            except Exception as exc:  # noqa: BLE001
                log.warning("on_actor_feedback-Hook-Fehler: %s", exc)

        # Web-UI Hook (Live-Log, Anlern-Modus)
        if self.on_telegram_post:
            try:
                match_info = None
                if device_match:
                    d, c = device_match
                    match_info = {
                        "device_id": d.device_id,
                        "channel_id": c.channel_id,
                        "name": c.name,
                        "eep": c.eep,
                        "decoded": {k: v for k, v in decoded.items() if not k.startswith("_")},
                    }
                # decoded auch unbeziehungsweise im Anlern-Modus brauchen wir
                # (LRN-Filter braucht teach_in-Flag)
                decoded_clean_all = {
                    k: v for k, v in decoded.items() if not k.startswith("_")
                }
                self.on_telegram_post(rx, match_info, decoded_clean_all)
            except Exception as exc:  # noqa: BLE001
                log.warning("on_telegram_post-Hook-Fehler: %s", exc)
