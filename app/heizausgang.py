"""
Heizausgaenge — welche Geraetekanaele als Heizkreis in Frage kommen (24.09.2026).

Warum eigenes Modul: Die Auswahlliste der Heizkreise hat frueher nur Kanaele
gezeigt, die schon fertig als Sendekanal eines Raumbedienteil-Profils
eingerichtet waren (EEP A5-10*, direction=tx). Damit fehlte genau das Geraet,
das dazugekauft wurde: Hat eine Anlage mehr Kreise als der 8-fach-Aktor
Ausgaenge, haengt der neunte z. B. an einem einzelnen **OPUS GN-A-U230V-HKR-1**. Der kam
aus dem MHC-Bestand als Empfangsgeraet herein (direction=rx, Adresse in
`enocean_id`) und tauchte deshalb nirgends auf.

Der Grundsatz hier: **Was ein Heizungsaktor ist, entscheidet das Geraet, nicht
die Einrichtung.** Die Liste zeigt jeden Kanal, der ein Heizungsaktor ist, und
sagt dazu, ob er sendebereit ist und was ihm sonst fehlt. Ein Eintrag, der
nicht funken kann, wird also nicht versteckt — er wird erklaert.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

from .devices import Device, DeviceChannel, SenderBinding
from .eep import get_profile_registry
from .sender_routing import find_gateway_for_sender
from .tx_router import classify_channel_kind

log = logging.getLogger(__name__)


@dataclass
class Heizausgang:
    """Ein Kanal, der als Heizkreis in Frage kommt."""

    device_id: str
    channel_id: str
    device: str
    channel: str
    eep: str
    sender_id: str = ""
    via_gateway: str = ""
    bereit: bool = False
    grund: str = ""
    einrichtbar: bool = False
    # Was der Produktkatalog ueber den Aktor sagt. NICHT die Wahrheit, nur ein
    # Anhalt: beim SRC-DO8 steht dort ±10 K, ab Werk regelt er mit ±5 K. Die
    # Zahl gehoert deshalb als Hinweis neben die Auswahl, nicht ins Formular.
    basis_c: float | None = None
    spanne_k: float | None = None

    def als_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "channel_id": self.channel_id,
            "device": self.device,
            "channel": self.channel,
            "eep": self.eep,
            "sender_id": self.sender_id,
            "via_gateway": self.via_gateway,
            "bereit": self.bereit,
            "grund": self.grund,
            "einrichtbar": self.einrichtbar,
            "basis_c": self.basis_c,
            "spanne_k": self.spanne_k,
        }


def ist_heizungsaktor(kanal: DeviceChannel) -> bool:
    """
    Heizungsaktor = der Produktkatalog sagt es (device_type Valve_N /
    Heating / Ventil, ausgewertet von classify_channel_kind) ODER der Kanal ist
    bereits als Sendekanal eines Raumbedienteil-Profils eingerichtet.

    Die zweite Haelfte ist die alte Regel — sie bleibt, damit kein Ausgang aus
    der Liste faellt, der heute schon regelt.
    """
    if classify_channel_kind(kanal) == "valve":
        return True
    return ((kanal.eep or "").upper().startswith("A5-10")
            and (kanal.direction or "").lower() == "tx")


def _katalogwerte(kanal: DeviceChannel) -> tuple[float | None, float | None]:
    """(Basissollwert, Verschiebungsbereich ±K) aus der Kanal-Konfiguration."""
    paare: dict[str, str] = {}
    for teil in ((kanal.meta or {}).get("device_config") or "").split(";"):
        if "=" in teil:
            k, v = teil.split("=", 1)
            paare[k.strip()] = v.strip()

    def zahl(name: str) -> float | None:
        try:
            return float(paare[name])
        except (KeyError, ValueError):
            return None

    basis = zahl("BaseTemp")
    omin, omax = zahl("SetpointOffsetMin"), zahl("SetpointOffsetMax")
    spanne = None
    if omin is not None or omax is not None:
        # Die kleinere der beiden Haelften gilt — nach oben wie nach unten
        # wird symmetrisch verschoben.
        haelften = [abs(x) for x in (omin, omax) if x is not None]
        spanne = min(haelften) if haelften else None
    return basis, spanne


def heizausgaenge(
    geraete: Iterable[Device],
    gateways: Iterable[Any] = (),
) -> list[Heizausgang]:
    """Alle Heizungsaktor-Kanaele mit ihrem Sendezustand."""
    registry = get_profile_registry()
    gw_liste = list(gateways)
    ziele: list[Heizausgang] = []

    for geraet in geraete:
        for kanal in geraet.channels:
            if not ist_heizungsaktor(kanal):
                continue
            profil = registry.get(kanal.eep)
            sender = kanal.active_sender_id or ""
            basis, spanne = _katalogwerte(kanal)
            ziel = Heizausgang(
                device_id=geraet.device_id,
                channel_id=kanal.channel_id,
                device=geraet.name or geraet.device_id,
                channel=kanal.name or kanal.channel_id,
                eep=kanal.eep,
                sender_id=sender,
                via_gateway=kanal.active_via_gateway or "",
                basis_c=basis,
                spanne_k=spanne,
            )

            if profil is None or profil.encoder is None:
                ziel.grund = (f"Fuer das Profil {kanal.eep} gibt es keinen "
                              f"Sendeweg — dieser Aktor laesst sich (noch) "
                              f"nicht als Heizkreis fahren.")
            elif not sender:
                if kanal.enocean_id:
                    ziel.einrichtbar = bool(
                        find_gateway_for_sender(kanal.enocean_id, gw_liste))
                    if ziel.einrichtbar:
                        ziel.grund = (
                            f"Noch kein Sendekanal: Die Adresse "
                            f"{kanal.enocean_id.upper()} ist hinterlegt, der "
                            f"Kanal steht aber auf Empfang. Einmal einrichten, "
                            f"dann funkt er.")
                    else:
                        ziel.grund = (
                            f"Die Adresse {kanal.enocean_id.upper()} gehoert zu "
                            f"keinem bekannten Gateway — mit einer fremden ID "
                            f"kann keines unserer Gateways senden.")
                else:
                    ziel.grund = ("Keine Sender-ID hinterlegt — der Aktor muss "
                                  "erst angelernt werden.")
            else:
                ziel.bereit = True

            ziele.append(ziel)

    return ziele


def ausgang_einrichten(
    geraet: Device,
    kanal: DeviceChannel,
    gateways: Iterable[Any],
    belegte_sender: Iterable[str] = (),
) -> str:
    """
    Macht aus einem eingelesenen Heizungsaktor einen Sendekanal.

    Aus dem MHC-Bestand kommt so ein Geraet als Empfaenger herein: die im Aktor
    angelernte Adresse steht in `enocean_id`, `senders` ist leer. Gesendet wird
    aber ueber ein SenderBinding — deshalb wird die Adresse hierher umgehaengt
    und der Kanal auf `tx` gestellt.

    Das Gateway muss dabei feststehen: Ein Gateway kann nur mit IDs aus seinem
    eigenen 128er-Block senden. Ohne passenden Block waere die Zuordnung
    geraten, und der Aktor bliebe still, ohne dass ein Fehler auftaucht.

    Liefert den Gateway-Namen. Wirft ValueError mit deutschem Klartext, wenn es
    nicht geht — der Aufrufer reicht ihn als Fehlermeldung durch.
    """
    if kanal.active_sender_id:
        raise ValueError("Der Kanal hat bereits eine Sende-ID.")
    if not kanal.enocean_id:
        raise ValueError(
            "Der Kanal hat keine Adresse, die sich als Sende-ID eignet — "
            "der Aktor muss angelernt werden.")

    sender = kanal.enocean_id.upper()
    doppelt = {s.upper() for s in belegte_sender}
    if sender in doppelt:
        raise ValueError(
            f"Die ID {sender} ist bereits der Sendekanal eines anderen "
            f"Aktors. Zwei Stellen auf derselben ID ueberschreiben sich "
            f"gegenseitig, ohne dass ein Fehler auftaucht.")

    gateway = find_gateway_for_sender(sender, list(gateways))
    if not gateway:
        raise ValueError(
            f"Zur ID {sender} gibt es kein Gateway mit passendem ID-Block — "
            f"ein Gateway kann nur mit seinen eigenen IDs senden.")

    kanal.senders = [SenderBinding(sender_id=sender, via_gateway=gateway,
                                   label=kanal.name or "Heizkreis", active=True)]
    kanal.direction = "tx"
    # Die Adresse ist jetzt unsere Sende-ID und keine Empfangs-ID mehr. Bliebe
    # sie stehen, landete unser eigenes Telegramm als vermeintliche Messung
    # dieses Geraets im Live-Log und auf dem MQTT-Topic.
    kanal.enocean_id = None
    log.info("Heizausgang eingerichtet: %s/%s sendet als %s ueber %s",
             geraet.device_id, kanal.channel_id, sender, gateway)
    return gateway


def belegte_sender_ids(geraete: Iterable[Device]) -> list[str]:
    """Alle aktiven Sende-IDs — fuer die Doppelbelegungspruefung."""
    ids: list[str] = []
    for geraet in geraete:
        for kanal in geraet.channels:
            sid = kanal.active_sender_id
            if sid:
                ids.append(sid.upper())
    return ids
