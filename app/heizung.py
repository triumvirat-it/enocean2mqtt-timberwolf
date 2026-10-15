"""
Heizungsregelung — Heizzonen mit Zeitplan.

Was das macht: Pro Heizzone liegt ein Ist-Wert auf einem beliebigen MQTT-Topic
(1-Wire, EnOcean, egal — Hauptsache im Broker). Ein Zeitplan aus beliebig vielen
Fenstern (Start/Ende/Wochentage) bestimmt den Sollwert. Die Bruecke rechnet
daraus die Sollwertverschiebung und spielt gegenueber dem Heizregler
(Thermokon SRC-DO8 / STC-DO8) das Raumbedienteil — sie sendet zyklisch
A5-10-06 mit Ist-Temperatur + Verschiebung.

Drei Dinge, die aus fremden Datenblaettern und eigenen Vorfaellen stammen und
beim Anfassen NICHT umgangen werden duerfen:

1. **Nie einen erfundenen Ist-Wert senden.** Fehlt der Raumwert oder ist er zu
   alt, wird NICHT gesendet. Ein ausgedachter Wert (z.B. 20 °C) waere fuer den
   Regler nicht von einer Messung zu unterscheiden — er wuerde auf eine
   Temperatur regeln, die niemand gemessen hat. Das ist dieselbe Fehlerklasse
   wie die Diagnosemeldung auf dem Zaehler-Topic (Vorfall 02.09.2026):
   etwas ohne Messwert darf nicht wie ein Messwert behandelt werden.

2. **Zyklisch senden, deutlich haeufiger als alle 90 Minuten.** Der SRC-DO8
   erklaert einen Fuehler nach 90 Minuten Funkstille fuer ausgefallen und faehrt
   dann mit einer Ersatz-Stellgroesse weiter (Werkseinstellung: letzter Wert).
   Vorgabe hier: alle 5 Minuten.

3. **Die Sollwertverschiebung muss beidseitig gleich parametriert sein.** Das
   Telegramm uebertraegt die Verschiebung als 0..255 ueber den IM AKTOR
   eingestellten Bereich. Steht der Aktor auf ±5 K (Werkseinstellung) und wir
   kodieren mit ±10 K, kommt die halbe Verschiebung an — still und unbemerkt.
   `verschiebung_max_k` der Zone MUSS dem Geraet entsprechen.

Der Zustand (zuletzt gueltiger Sollwert) ueberlebt den Neustart — sonst waere
die Regel "ausserhalb der Fenster bleibt der letzte Wert stehen" nach jedem
Deploy wirkungslos.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

log = logging.getLogger(__name__)


WOCHENTAGE = ("mo", "di", "mi", "do", "fr", "sa", "so")

# Der Heizregler erklaert einen Fuehler nach 90 Minuten Funkstille fuer
# ausgefallen (Datenblatt STC-DO8 Typ 1, "Stellgroesse bei Sensorausfall").
SENSORAUSFALL_NACH_S = 90 * 60


# ---------------------------------------------------------------------------
# Modell
# ---------------------------------------------------------------------------


class Sollwertbezug(BaseModel):
    """
    Ein Sollwert, der fest hinterlegt sein kann oder aus der Visu kommt.

    Gemeinsame Grundlage von Zeitzone und Rueckfall-Sollwert — beide werden
    gleich gelesen (`fenster_sollwert`), damit ein Sollwertsteller ueberall
    auf dieselbe Weise eingehaengt wird.
    """

    sollwert_c: float = Field(
        21.0, ge=5.0, le=35.0,
        description="Fester Sollwert. Gilt, solange kein Wert ueber "
        "sollwert_topic eingetroffen ist.",
    )
    sollwert_topic: str = Field(
        "",
        description="Optional: MQTT-Topic, von dem der Sollwert kommt. Die "
        "Timberwolf-Visu veroeffentlicht die Sollwertsteller als EIN JSON je "
        "Topic mit einem Feld je Steller, z.B. heizung/zone1 → "
        '{"TMPTagWohnen": 22.5, "TMPNachtWohnen": 18}.',
    )
    sollwert_feld: str = Field(
        "",
        description="Feld in diesem JSON, z.B. 'TMPTagWohnen'. Punkte fuer "
        "verschachtelte Felder.",
    )


class Heizfenster(Sollwertbezug):
    """Ein Sollwert, der zwischen zwei Uhrzeiten an bestimmten Tagen gilt."""

    name: str = Field(..., description="Anzeigename, z.B. 'Morgens'")
    von: str = Field("00:00", description="Startzeit HH:MM")
    bis: str = Field("24:00", description="Endzeit HH:MM (exklusiv)")
    tage: list[str] = Field(
        default_factory=list,
        description="Wochentage mo/di/mi/do/fr/sa/so. Leer = jeden Tag.",
    )
    nur_wenn_schalter: str = Field(
        "",
        description="ID eines Schalters. Das Fenster gilt nur, solange dieser "
        "Schalter an ist — z.B. 'Sa/So 21 °C, nur wenn Gaeste da'. Leer = "
        "gilt immer. Zeigt der Schalter auf nichts (geloescht), gilt das "
        "Fenster NICHT: lieber kalt als dauerhaft geheizt, ohne dass es "
        "jemand angeordnet hat.",
    )

    @field_validator("tage")
    @classmethod
    def _tage_normieren(cls, v: list[str]) -> list[str]:
        return [t.lower()[:2] for t in v if t.lower()[:2] in WOCHENTAGE]


class Wertquelle(BaseModel):
    """Ein MQTT-Topic und ein Feld darin."""

    topic: str = ""
    feld: str = Field("", description="Feld im JSON. Leer = der Payload ist "
                                      "selbst die Zahl.")
    name: str = Field("", description="Anzeigename, z.B. 'Thanos Wohnzimmer'.")

    def anzeigename(self) -> str:
        return self.name or (f"{self.topic}:{self.feld}" if self.feld else self.topic)


class Fuehler(Wertquelle):
    """
    Eine Quelle fuer eine Raumtemperatur.

    Mehrere Fuehler je Raum sind ausdruecklich vorgesehen — ein grosser Raum
    kann „Vorne" und „Hinten" gemessen werden, oder ein 1-Wire-Fuehler in der
    Decke und ein Raumbedienteil an der Wand.
    """

    gewicht: float = Field(
        1.0, gt=0.0, le=100.0,
        description="Gewicht im Mittelwert. 2.0 zaehlt doppelt.",
    )


class Kontakt(Wertquelle):
    """
    Ein Tuer- oder Fensterkontakt, der eine Zone zwangsweise absenkt.

    Warum: Steht eine Terrassentuer offen, soll die Zone absenken. Steht die
    Tuer zu einem kuehleren Bereich offen, soll ebenfalls abgesenkt werden —
    sonst heizt der warme Bereich gegen den kalten an.

    `bedeutung` sagt, was das Feld beschreibt: Ein EnOcean-Fensterkontakt
    (D5-00-01) meldet `{"closed": true}` — das Feld heisst also
    'geschlossen'. Ein Feld, das direkt "offen" meldet, bekommt 'offen'.
    """

    bedeutung: Literal["offen", "geschlossen"] = Field(
        "geschlossen",
        description="Beschreibt das Feld den OFFENEN oder den GESCHLOSSENEN "
        "Zustand? D5-00-01 liefert 'closed' → 'geschlossen'.",
    )


class Restsollwert(Sollwertbezug):
    """
    Was gilt, wenn gerade KEINE Zeitzone laeuft.

    Konzept: Zeitzonen werden nur noch fuer Zeiten mit
    Heizbedarf angelegt. Alles dazwischen — Nacht, Wochenende, Urlaub — faellt
    auf diesen einen Sollwert zurueck, statt dass fuer jede Nacht ein eigenes
    Fenster gepflegt wird. Er hat Topic und Feld wie eine Zeitzone, kann also
    ebenso aus der Visu kommen ('BueroNacht').

    Ist er aus, gilt weiter die alte Regel: der zuletzt gueltige Sollwert
    bleibt stehen.
    """

    aktiv: bool = Field(
        False,
        description="Aus = der zuletzt gueltige Sollwert bleibt stehen (das "
        "bisherige Verhalten).",
    )
    name: str = Field("Ausserhalb der Zeitzonen",
                      description="Anzeigename, z.B. 'Nachtabsenkung'.")


class Schalter(BaseModel):
    """
    Ein benannter Schalter fuer die Betriebsart, z.B. "Gaeste".

    Warum es ihn gibt: Manche Raeume haengen an zwei Rhythmen — etwa werktags
    am Arbeitszeitplan und am Wochenende nur dann, wenn Besuch da ist. Ein Kontakt kann nur absenken, ein
    Zeitplan kennt nur Wochentage. Gebraucht wird ein Umstand, den der Mensch
    setzt und an dem sowohl Zeitzonen als auch Raeume haengen koennen.

    Der Stand steht in heizzonen.yaml (ueberlebt also den Neustart) und wird
    retained veroeffentlicht, damit die Timberwolf-Visu ihn anzeigen und ueber
    <basis>/schalter/<id>/set auch umlegen kann.
    """

    id: str = Field(..., description="Technischer Schluessel, MQTT-tauglich")
    name: str = Field("", description="Anzeigename, z.B. 'Gaeste da'")
    an: bool = Field(False, description="Aktueller Stand.")

    def anzeigename(self) -> str:
        return self.name or self.id


class Heizkreis(BaseModel):
    """
    Ein Heizkreis: ein Ausgang des Heizreglers (SRC-DO8), der einen Raum waermt.

    Er traegt nur, was am **Aktor** haengt. Alles, was den Raum betrifft —
    Fuehler, Sollwertrad, Fensterkontakte — steht beim Raum, weil mehrere
    Heizkreise sich denselben Raum teilen.
    """

    device_id: str = ""
    channel_id: str = ""
    name: str = Field("", description="Anzeigename, z.B. 'Wohnen vorne'")

    basissollwert_c: float | None = Field(
        None,
        description="Basissollwert DIESES Ausgangs, falls er vom Bereich "
        "abweicht. Der SRC-DO8 haelt ihn je Ausgang (z. B. "
        "21,1 °C statt 21,0 °C). None = wie Bereich.",
    )
    verschiebung_max_k: float | None = Field(
        None, gt=0.0, le=20.0,
        description="Verschiebungsbereich DIESES Ausgangs. None = wie "
        "Bereich. Bis 20 K, weil einzelne Stellventilaktoren so weit gehen "
        "(OPUS GN-A-U230V-HKR-1: ±20 K um Basis 20 °C) — der SRC-DO8 bleibt "
        "bei ±5 K ab Werk.",
    )

    def anzeigename(self) -> str:
        return self.name or f"{self.device_id}/{self.channel_id}"


class Raum(BaseModel):
    """
    Ein Raum im Bereich.

    Das Modell folgt der Anforderung:
    *"Ein Bereich kann x Räume aufnehmen. Ein Raum hat x IST und 0 oder
    einen Offset-Steller. Ein Raum kann korrekt in die Nachtabsenkung gefahren
    werden wenn x IO offen stehen. Ein Raum hat x Heizkreise."*
    """

    id: str = Field("", description="Technischer Schluessel, MQTT-tauglich")
    name: str = Field("", description="Anzeigename, z.B. 'Wohnzimmer'")
    aktiv: bool = True

    fuehler: list[Fuehler] = Field(
        default_factory=list,
        description="Raumfuehler. Mehrere sind vorgesehen: 1-Wire in der Decke "
        "und Raumbedienteil an der Wand, oder Vorne und Hinten.",
    )
    fuehler_regel: Literal["mittelwert", "kleinster", "groesster"] | None = Field(
        None, description="Verrechnung mehrerer Fuehler. None = wie Bereich.",
    )

    offset_quelle: Wertquelle | None = Field(
        None,
        description="Das eine Sollwertrad dieses Raums (0 oder 1). Ein Thanos "
        "sendet es auf demselben Topic wie seine Temperatur, im Feld "
        "'setpoint_offset_c'.",
    )
    sollwert_offset_k: float = Field(
        0.0, ge=-10.0, le=10.0,
        description="FESTE KORREKTUR dieses Raums — dauerhafte Eigenschaft, "
        "nicht Bedienung: Fuehler haengt zu kalt, oder der Raum soll "
        "grundsaetzlich waermer sein (z. B. Bad +2 K). Sie bleibt "
        "stehen, auch wenn spaeter ein Sollwertrad dazukommt, und ist NICHT "
        "auf offset_grenze_k begrenzt. Fuer die Bedienung gibt es "
        "`handverschiebung_k`.",
    )
    handverschiebung_k: float = Field(
        0.0, ge=-10.0, le=10.0,
        description="SOLLWERTVERSCHIEBUNG von Hand — das Sollwertrad in "
        "Software, fuer Raeume ohne Rad an der Wand. Sie unterliegt derselben "
        "Grenze wie das Rad (`offset_grenze_k` des Bereichs) und wird vom "
        "echten Rad UEBERSCHRIEBEN, sobald dieses einen brauchbaren Wert "
        "liefert — nicht addiert, sonst haette man zwei Raeder, die sich "
        "gegenseitig hochschaukeln.",
    )

    kontakte: list[Kontakt] = Field(
        default_factory=list,
        description="Tuer-/Fensterkontakte, die NUR diesen Raum absenken. "
        "Kommen zu denen des Bereichs dazu.",
    )

    dauerabsenkung: bool = Field(
        False,
        description="Raum dauerhaft auf den Absenk-Sollwert des Bereichs "
        "— fuer laengere Nichtnutzung (z. B. Gaestezimmer). Er funkt dabei "
        "WEITER, nur mit dem Absenkwert. Das ist der Unterschied zu "
        "`aktiv=False`: dort verstummt der Kreis und der Aktor faehrt nach 90 "
        "Minuten mit seiner Ersatz-Stellgroesse weiter (Werk: letzter Wert), "
        "der Ausgang bleibt also eingefroren stehen.",
    )
    dauerabsenkung_ausnahme_schalter: str = Field(
        "",
        description="ID eines Schalters, der die Dauerabsenkung aufhebt, "
        "solange er an ist (z.B. 'Gaeste'). Leer = die Absenkung gilt immer.",
    )

    heizkreise: list[Heizkreis] = Field(default_factory=list)

    def anzeigename(self) -> str:
        return self.name or self.id or "Raum"


class Heizzone(BaseModel):
    """
    Ein **Bereich**: die Klammer um mehrere Raeume, die denselben Zeitplan
    teilen.

    Aufbau:

        Bereich
          ├── x Zeitzonen           (Zeitplan, gilt fuer alle Raeume)
          ├── x Kontakte            (senken ALLE Raeume ab)
          └── x Raeume
                ├── x Fuehler + Verrechnungsregel
                ├── 0..1 Sollwertrad
                ├── x Kontakte      (senken NUR diesen Raum)
                └── x Heizkreise    (je ein Ausgang des Reglers)
    """

    id: str = Field(..., description="Technischer Schluessel, MQTT-tauglich")
    name: str = ""
    aktiv: bool = True

    raeume: list[Raum] = Field(default_factory=list)

    fuehler_regel: Literal["mittelwert", "kleinster", "groesster"] = Field(
        "mittelwert",
        description="Vorgabe, wie mehrere Fuehler eines Raums zu einem Wert "
        "werden. 'kleinster' regelt nach dem kaeltesten Punkt (der Raum wird "
        "sicher warm), 'groesster' nach dem waermsten (sparsamer). Ein Raum "
        "kann davon abweichen.",
    )
    ist_max_alter_s: int = Field(
        1800,
        ge=0,
        description="Aelter als das gilt der Ist-Wert als veraltet und es wird "
        "NICHT gesendet. 0 = Alter egal.",
    )
    ist_min_c: float = Field(
        0.0,
        description="Untere Plausibilitaetsgrenze des Raum-Istwerts.",
    )
    ist_max_c: float = Field(
        40.0,
        description="Obere Plausibilitaetsgrenze. Werte ausserhalb gelten als "
        "Stoerung der Quelle und werden wie ein fehlender Wert behandelt. "
        "Warum: Am 23.09.2026 lieferte ein 1-Wire-Fuehler 2153,12 statt 21,53 "
        "(fehlende Kommastelle in der Quelle). Ohne Grenze haette die Zone das "
        "als Raumtemperatur genommen und den Heizkreis zugedreht.",
    )

    # --- Sollwert-Rechnung (muss zur Aktor-Parametrierung passen!)
    basissollwert_c: float = Field(
        21.0,
        description="Im Aktor eingestellter Basissollwert. Werkseinstellung "
        "SRC-DO8: 21,0 °C.",
    )
    verschiebung_max_k: float = Field(
        5.0,
        gt=0.0,
        le=20.0,
        description="Im Aktor eingestellter Bereich der Sollwertverschiebung "
        "(±K). Werkseinstellung SRC-DO8: ±5 K. MUSS uebereinstimmen, sonst "
        "kommt eine falsch skalierte Verschiebung an. Obergrenze 20 K wegen "
        "einzelner Stellventilaktoren (OPUS GN-A-U230V-HKR-1).",
    )
    sollwert_offset_k: float = Field(
        0.0,
        ge=-10.0,
        le=10.0,
        description="Dauerhafte Feinkorrektur dieser Zone (z.B. Fuehler haengt "
        "zu warm). Wird auf jeden Fenster-Sollwert aufgeschlagen.",
    )
    offset_grenze_k: float = Field(
        3.0, ge=0.0, le=10.0,
        description="Wie weit das Sollwertrad den Sollwert verschieben darf. "
        "Begrenzt, damit ein voll aufgedrehtes Rad nicht den ganzen Zeitplan "
        "aushebelt. 0 = Rad wirkt nicht.",
    )

    # --- Zwangsabsenkung ueber Tuer-/Fensterkontakte
    kontakte: list[Kontakt] = Field(
        default_factory=list,
        description="Kontakte, die den GANZEN Bereich absenken, solange "
        "einer von ihnen offen ist — unabhaengig vom Zeitplan.",
    )
    absenkung_zeitzone: str = Field(
        "",
        description="Name der Zeitzone, deren Sollwert bei offenem Kontakt "
        "gilt (z.B. 'Zeitzone 2 (Nacht)'). So bleibt der Nachtsollwert die "
        "eine Quelle der Wahrheit — wird er in der Visu verstellt, folgt die "
        "Absenkung. Leer = stattdessen absenkung_k abziehen.",
    )
    absenkung_k: float = Field(
        3.0, ge=0.0, le=15.0,
        description="Um so viel wird abgesenkt, wenn keine Zeitzone benannt "
        "ist.",
    )

    sende_intervall_s: int = Field(
        300,
        ge=30,
        le=3600,
        description="Sendetakt. Muss deutlich unter 90 Minuten liegen, sonst "
        "erklaert der Regler den Fuehler fuer ausgefallen.",
    )

    fenster: list[Heizfenster] = Field(default_factory=list)
    restzeit: Restsollwert = Field(
        default_factory=Restsollwert,
        description="Sollwert fuer alle Zeiten ohne Zeitzone (Nachtabsenkung). "
        "Siehe Restsollwert.",
    )

    def anzeigename(self) -> str:
        return self.name or self.id


class Heizungseinstellungen(BaseModel):
    """Alles, was in heizzonen.yaml steht."""

    aktiv: bool = Field(True, description="Hauptschalter der Heizungsregelung.")
    basis_topic: str = Field(
        "heizung",
        description="Wurzel-Topic der Heizungsregelung. Getrennt vom "
        "Basis-Topic der Bruecke, weil der Timberwolf die Topic-Wurzeln als "
        "eigene MQTT-Geraete fuehrt — 'heizung' ist dort bereits angelegt.",
    )
    takt_s: int = Field(
        20, ge=5, le=300,
        description="Wie oft der Regler nachsieht (nicht: wie oft gesendet "
        "wird). Bestimmt, wie genau Fenstergrenzen getroffen werden.",
    )
    zeitzone: str = Field(
        "Europe/Berlin",
        description="Zeitzone, in der die Zeitzonen-Uhrzeiten gelten. MUSS "
        "hier stehen und nicht am Container haengen: Der Docker-Container "
        "laeuft auf UTC (kein TZ in seiner Umgebung), der Zeitplan lief "
        "dadurch im Sommer zwei Stunden zu spaet — eine Zeitzone 'Buerozeit 05:30' "
        "begann in Wirklichkeit um 07:30. Sichtbar wurde "
        "das erst mit dem Rueckfall-Sollwert; vorher hing ausserhalb der "
        "Fenster einfach der letzte Tagwert nach und kaschierte den Versatz. "
        "Am Container haette ein Recreate die Einstellung wieder verloren.",
    )
    schalter: list[Schalter] = Field(
        default_factory=list,
        description="Betriebsart-Schalter, die ueber alle Bereiche hinweg "
        "gelten (z.B. 'Gaeste').",
    )
    zonen: list[Heizzone] = Field(default_factory=list)

    def schalterstand(self) -> dict[str, bool]:
        """{Schalter-ID: an} — die Form, in der das Regelwerk sie braucht."""
        return {s.id: bool(s.an) for s in self.schalter if s.id}

    def schalter_name(self, schalter_id: str) -> str:
        """Anzeigename eines Schalters; unbekannt -> die ID selbst."""
        treffer = next((s for s in self.schalter if s.id == schalter_id), None)
        return treffer.anzeigename() if treffer else schalter_id


# ---------------------------------------------------------------------------
# Zeitplan
# ---------------------------------------------------------------------------


_zeitzone_gewarnt: set[str] = set()


def jetzt_in(zeitzone: str) -> datetime:
    """
    Die aktuelle Zeit in der eingestellten Zone.

    Warum nicht einfach `datetime.now()`: Das ist die Uhr des Containers, und
    die steht auf UTC. Der Zeitplan lief damit im Sommer zwei Stunden zu spaet
    ("Buerozeit 05:30" begann um 07:30 Ortszeit). Die Zeitzone
    gehoert deshalb in die Konfiguration, wo ein Container-Recreate sie nicht
    wegnehmen kann.

    Ist die Zone unbekannt (fehlende tzdata), wird gewarnt und auf die Uhr des
    Containers zurueckgefallen — lieber ein moeglicherweise versetzter Plan als
    gar keine Regelung.
    """
    name = (zeitzone or "").strip()
    if not name:
        return datetime.now()
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(name))
    except Exception as exc:  # noqa: BLE001
        if name not in _zeitzone_gewarnt:
            _zeitzone_gewarnt.add(name)
            log.error("Heizung: Zeitzone '%s' nicht verfuegbar (%s) — es gilt "
                      "die Uhr des Containers (%s). Der Zeitplan kann dadurch "
                      "versetzt laufen!", name, exc,
                      datetime.now().strftime("%H:%M"))
        return datetime.now()


def minuten_aus_zeit(text: str) -> int | None:
    """'06:30' -> 390. '24:00' -> 1440. Ungueltiges -> None."""
    try:
        teile = str(text).strip().split(":")
        if len(teile) != 2:
            return None
        stunde, minute = int(teile[0]), int(teile[1])
    except (ValueError, AttributeError):
        return None
    if not (0 <= stunde <= 24 and 0 <= minute <= 59):
        return None
    wert = stunde * 60 + minute
    return wert if wert <= 1440 else None


def _tag_passt(fenster: Heizfenster, wochentag: int) -> bool:
    """wochentag: 0=Montag .. 6=Sonntag (wie datetime.weekday())."""
    if not fenster.tage:
        return True
    return WOCHENTAGE[wochentag % 7] in fenster.tage


def laeuft_seit_minuten(fenster: Heizfenster, jetzt: datetime) -> int | None:
    """
    Wie lange laeuft dieses Fenster gerade schon? None = es laeuft nicht.

    Fenster ueber Mitternacht (von > bis, z.B. 22:00–06:00) werden unterstuetzt;
    massgeblich ist dann der Wochentag des *Starts*.
    """
    von = minuten_aus_zeit(fenster.von)
    bis = minuten_aus_zeit(fenster.bis)
    if von is None or bis is None or von == bis:
        return None

    jetzt_m = jetzt.hour * 60 + jetzt.minute
    heute = jetzt.weekday()

    if von < bis:
        if _tag_passt(fenster, heute) and von <= jetzt_m < bis:
            return jetzt_m - von
        return None

    # ueber Mitternacht
    if jetzt_m >= von and _tag_passt(fenster, heute):
        return jetzt_m - von
    if jetzt_m < bis and _tag_passt(fenster, (heute - 1) % 7):
        return jetzt_m + 1440 - von
    return None


def fenster_sollwert(
    fenster: Sollwertbezug, istwerte: "IstwertSpeicher",
) -> tuple[float, str, str]:
    """
    Der gueltige Sollwert eines Fensters: (Wert, Quelle, Meldung).

    Ist ein `sollwert_topic` hinterlegt, gilt der Wert des Sollwertstellers aus
    der Visu. Der feste `sollwert_c` ist dann nur der Rueckfall, bis der erste
    Wert eingetroffen ist — und die Schranke, falls der Steller Unsinn meldet.
    """
    if not fenster.sollwert_topic:
        return float(fenster.sollwert_c), "fenster", ""

    eintrag = istwerte.hole_zahl(fenster.sollwert_topic, fenster.sollwert_feld)
    if eintrag is None:
        feldname = fenster.sollwert_feld or "den Wert"
        return (float(fenster.sollwert_c), "fenster",
                f"Auf '{fenster.sollwert_topic}' ist fuer '{feldname}' noch "
                f"nichts eingetroffen — es gilt der hinterlegte "
                f"{fenster.sollwert_c:g} °C.")
    if not (5.0 <= eintrag[0] <= 35.0):
        return (float(fenster.sollwert_c), "fenster",
                f"Sollwertsteller '{fenster.sollwert_feld}' meldet "
                f"{eintrag[0]:g} — ausserhalb 5..35 °C, ignoriert.")
    return float(eintrag[0]), "sollwertsteller", ""


def fenster_freigegeben(fenster: Heizfenster,
                        schalter: dict[str, bool] | None) -> bool:
    """
    Laesst der Schalter dieses Fenster zu?

    Ohne Bedingung: immer. Mit Bedingung: nur, wenn der genannte Schalter
    bekannt UND an ist. Ein Fenster, dessen Schalter geloescht wurde, gilt
    nicht — es soll niemand dauerhaft heizen, weil eine Bedingung ins Leere
    zeigt.
    """
    if not fenster.nur_wenn_schalter:
        return True
    return bool((schalter or {}).get(fenster.nur_wenn_schalter, False))


def aktives_fenster(zone: Heizzone, jetzt: datetime,
                    schalter: dict[str, bool] | None = None) -> Heizfenster | None:
    """
    Das gerade gueltige Fenster. Ueberlappen sich mehrere, gewinnt das zuletzt
    gestartete (und bei gleichem Start das weiter hinten stehende) — so
    ueberschreibt ein kurzes Sonderfenster ein langes Grundfenster.

    Genau darauf ruht das Gaeste-Wochenende: ein Grundfenster Sa/So 18 °C, das
    immer laeuft, und darunter ein zweites Sa/So 21 °C mit Bedingung
    "nur wenn Gaeste". Ist der Schalter aus, faellt es weg und das
    Grundfenster traegt — es entsteht keine Luecke, in der der letzte Sollwert
    stehen bliebe.
    """
    treffer: list[tuple[int, int, Heizfenster]] = []
    for idx, f in enumerate(zone.fenster):
        if not fenster_freigegeben(f, schalter):
            continue
        seit = laeuft_seit_minuten(f, jetzt)
        if seit is not None:
            treffer.append((seit, -idx, f))
    if not treffer:
        return None
    treffer.sort(key=lambda t: (t[0], t[1]))
    return treffer[0][2]


# ---------------------------------------------------------------------------
# Ist-Werte aus MQTT
# ---------------------------------------------------------------------------


def wert_aus_payload(payload: str | bytes, feld: str) -> float | None:
    """
    Zahl aus einem MQTT-Payload ziehen.

    Erlaubt sind: nackte Zahl ('21.4'), flaches JSON ({"temperature_c": 21.4})
    und verschachteltes JSON ueber einen Punktpfad ('sensor.temp').
    Alles andere -> None (und damit: nicht senden).
    """
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    text = (payload or "").strip()
    if not text:
        return None

    try:
        daten = json.loads(text)
    except json.JSONDecodeError:
        try:
            return float(text.replace(",", "."))
        except ValueError:
            return None

    if isinstance(daten, (int, float)) and not isinstance(daten, bool):
        return float(daten)

    if isinstance(daten, dict):
        if feld:
            wert: Any = daten
            for teil in feld.split("."):
                if not isinstance(wert, dict) or teil not in wert:
                    return None
                wert = wert[teil]
            if isinstance(wert, bool):
                return None
            if isinstance(wert, (int, float)):
                return float(wert)
            if isinstance(wert, str):
                try:
                    return float(wert.replace(",", "."))
                except ValueError:
                    return None
            return None
        # Kein Feldname angegeben: nur eindeutig, wenn genau ein Zahlenfeld da ist
        zahlen = [
            v for k, v in daten.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
            and not k.startswith("_")
        ]
        if len(zahlen) == 1:
            return float(zahlen[0])
    return None


# Texte, die den Zustand SELBST benennen. Bei ihnen ist `bedeutung`
# gegenstandslos: "closed" heisst zu, egal wie das Feld heisst.
_TEXT_OFFEN = {"open", "opened", "offen", "geoeffnet", "geöffnet", "auf"}
_TEXT_ZU = {"closed", "close", "geschlossen", "zu", "shut"}
# Texte, die nur wahr/falsch bedeuten — hier entscheidet `bedeutung`.
_WAHR = {"true", "1", "on", "an", "ja", "yes"}
_FALSCH = {"false", "0", "off", "aus", "nein", "no"}


def wahrheit_aus_payload(wert: Any) -> bool | None:
    """Wahr/falsch aus einem Kontaktwert — oder None, wenn unklar."""
    if isinstance(wert, bool):
        return wert
    if isinstance(wert, (int, float)):
        return bool(wert)
    if isinstance(wert, str):
        text = wert.strip().lower()
        if text in _WAHR:
            return True
        if text in _FALSCH:
            return False
    return None


def kontakt_ist_offen(kontakt: Kontakt, speicher: "IstwertSpeicher") -> bool | None:
    """
    True = offen, False = zu, None = kein verwertbarer Zustand bekannt.

    None ist wichtig: Ein Kontakt, den wir nicht verstehen, darf nicht als
    "geschlossen" durchgehen — sonst waere die Absenkung stillschweigend
    ausser Betrieb.
    """
    eintrag = speicher.hole(kontakt.topic, kontakt.feld)
    if eintrag is None:
        return None
    wert = eintrag[0]

    # Benennt der Wert den Zustand selbst ("open"/"closed"), gilt er direkt —
    # sonst wuerde die Einstellung 'geschlossen' ihn ein zweites Mal drehen
    # und aus einer geschlossenen Tuer eine offene machen.
    if isinstance(wert, str):
        text = wert.strip().lower()
        if text in _TEXT_OFFEN:
            return True
        if text in _TEXT_ZU:
            return False

    wahr = wahrheit_aus_payload(wert)
    if wahr is None:
        return None
    # 'geschlossen' heisst: das Feld meldet den GESCHLOSSENEN Zustand.
    return wahr if kontakt.bedeutung == "offen" else not wahr


def rohwert_aus_payload(payload: str | bytes, feld: str) -> Any:
    """
    Wie `wert_aus_payload`, aber ohne Zahlenzwang — fuer Kontakte, die
    `true`/`false` oder "open"/"closed" melden.
    """
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    text = (payload or "").strip()
    if not text:
        return None
    try:
        daten = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(daten, dict):
        if not feld:
            return None
        wert: Any = daten
        for teil in feld.split("."):
            if not isinstance(wert, dict) or teil not in wert:
                return None
            wert = wert[teil]
        return wert
    return daten


class IstwertSpeicher:
    """
    Letzter Wert je (Topic, Feld), mit Zeitstempel.

    Das Feld gehoert in den Schluessel, weil ein einzelnes Topic mehrere Werte
    traegt: Die Timberwolf-Visu veroeffentlicht alle Sollwertsteller einer
    Gruppe als EIN JSON, z.B. heizung/zone1 →
    {"TMPTagWohnen": 22.5, "TMPNachtWohnen": 18}.
    """

    def __init__(self) -> None:
        self._werte: dict[tuple[str, str], tuple[Any, float]] = {}

    def setze(self, topic: str, wert: Any, ts: float | None = None,
              feld: str = "") -> None:
        if isinstance(wert, (int, float)) and not isinstance(wert, bool):
            wert = float(wert)
        self._werte[(topic, feld)] = (
            wert, float(ts if ts is not None else time.time()),
        )

    def hole(self, topic: str, feld: str = "") -> tuple[Any, float] | None:
        """Der rohe Wert — kann eine Zahl, ein Wahrheitswert oder Text sein."""
        return self._werte.get((topic, feld))

    def hole_zahl(self, topic: str, feld: str = "") -> tuple[float, float] | None:
        """Nur echte Zahlen. Ein Wahrheitswert ist keine Temperatur."""
        eintrag = self._werte.get((topic, feld))
        if eintrag is None:
            return None
        wert, ts = eintrag
        if isinstance(wert, bool) or not isinstance(wert, (int, float)):
            return None
        return float(wert), ts

    def topics(self) -> list[tuple[str, str]]:
        return sorted(self._werte)

    def __len__(self) -> int:
        return len(self._werte)


# ---------------------------------------------------------------------------
# Zustand (ueberlebt den Neustart)
# ---------------------------------------------------------------------------


class HeizungsZustand:
    """
    Zuletzt gueltiger Sollwert je Zone — auf Platte.

    Warum: Festgelegt ist, dass ausserhalb aller Zeitfenster der zuletzt
    gueltige Sollwert stehen bleibt. Laege der nur im Arbeitsspeicher, waere er
    nach jedem Deploy weg und die Zone stuende ohne Sollwert da. Genau diese
    Wurzel hat schon einmal die Zaehlerstatistik zerlegt (energy_state.py).
    """

    def __init__(self, path: str | Path, save_interval_s: float = 30.0) -> None:
        self.path = Path(path)
        self.save_interval_s = save_interval_s
        self._sollwerte: dict[str, float] = {}
        self._quellen: dict[str, str] = {}
        self._zeiten: dict[str, float] = {}
        # Zuletzt empfangene Stellung der Sollwertsteller, je (Topic, Feld).
        # Warum das mitgesichert wird: Die Timberwolf-Visu veroeffentlicht die
        # Steller NICHT retained — nachgemessen am 23.09.2026, in 20 Sekunden
        # kam auf 'heizung/bereich1' nichts an, obwohl dort 22/18 stehen. Ohne
        # Sicherung faengt die Regelung nach jedem Neustart wieder beim
        # hinterlegten Rueckfallwert an, bis jemand einen Steller anfasst.
        # Raum-ISTWERTE werden bewusst NICHT gesichert: eine alte Raum-
        # temperatur darf nach einem Neustart nicht als aktuelle Messung
        # durchgehen.
        self._steller: dict[tuple[str, str], tuple[float, float]] = {}
        self._dirty = False
        self._last_save = 0.0

    def steller(self) -> dict[tuple[str, str], tuple[float, float]]:
        return dict(self._steller)

    def merke_steller(self, topic: str, feld: str, wert: float,
                      ts: float | None = None) -> None:
        alt = self._steller.get((topic, feld))
        neu = (float(wert), float(ts if ts is not None else time.time()))
        if alt is None or abs(alt[0] - neu[0]) > 1e-6:
            self._dirty = True
        self._steller[(topic, feld)] = neu

    def sollwert(self, zone_id: str) -> float | None:
        return self._sollwerte.get(zone_id)

    def quelle(self, zone_id: str) -> str:
        return self._quellen.get(zone_id, "")

    def merke(self, zone_id: str, sollwert: float, quelle: str,
              ts: float | None = None) -> None:
        alt = self._sollwerte.get(zone_id)
        if alt is None or abs(alt - sollwert) > 1e-6 or self._quellen.get(zone_id) != quelle:
            self._dirty = True
        self._sollwerte[zone_id] = float(sollwert)
        self._quellen[zone_id] = quelle
        self._zeiten[zone_id] = float(ts if ts is not None else time.time())

    def vergiss(self, zone_id: str) -> None:
        if zone_id in self._sollwerte:
            self._sollwerte.pop(zone_id, None)
            self._quellen.pop(zone_id, None)
            self._zeiten.pop(zone_id, None)
            self._dirty = True

    def load(self) -> None:
        if not self.path.exists():
            log.info("Kein gespeicherter Heizungs-Zustand (%s) — die Zonen "
                     "starten ohne letzten Sollwert.", self.path)
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                doc: dict[str, Any] = yaml.safe_load(f) or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("Heizungs-Zustand nicht lesbar (%s): %s — es wird neu "
                        "begonnen.", self.path, exc)
            return
        for zone_id, eintrag in (doc.get("zonen") or {}).items():
            if not isinstance(eintrag, dict):
                continue
            wert = eintrag.get("sollwert_c")
            if not isinstance(wert, (int, float)):
                continue
            self._sollwerte[str(zone_id)] = float(wert)
            self._quellen[str(zone_id)] = str(eintrag.get("quelle") or "")
            ts = eintrag.get("ts")
            if isinstance(ts, (int, float)):
                self._zeiten[str(zone_id)] = float(ts)

        for schluessel, eintrag in (doc.get("sollwertsteller") or {}).items():
            if not isinstance(eintrag, dict):
                continue
            wert = eintrag.get("wert")
            if not isinstance(wert, (int, float)):
                continue
            topic, _, feld = str(schluessel).partition("|")
            ts = eintrag.get("ts")
            self._steller[(topic, feld)] = (
                float(wert), float(ts) if isinstance(ts, (int, float)) else 0.0,
            )

        log.info("Heizungs-Zustand geladen: %d Zone(n), %d Sollwertsteller "
                 "aus %s", len(self._sollwerte), len(self._steller), self.path)

    def save(self) -> None:
        doc = {
            "zonen": {
                zid: {
                    "sollwert_c": round(wert, 2),
                    "quelle": self._quellen.get(zid, ""),
                    "ts": round(self._zeiten.get(zid, 0.0), 1),
                }
                for zid, wert in sorted(self._sollwerte.items())
            },
            "sollwertsteller": {
                f"{topic}|{feld}": {"wert": round(wert, 2), "ts": round(ts, 1)}
                for (topic, feld), (wert, ts) in sorted(self._steller.items())
            },
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True)
            tmp.replace(self.path)
            self._dirty = False
            self._last_save = time.time()
        except Exception as exc:  # noqa: BLE001
            log.warning("Heizungs-Zustand nicht speicherbar (%s): %s",
                        self.path, exc)

    def save_if_due(self, now: float | None = None) -> bool:
        if not self._dirty:
            return False
        jetzt = now if now is not None else time.time()
        if jetzt - self._last_save < self.save_interval_s:
            return False
        self.save()
        return True


# ---------------------------------------------------------------------------
# Zonen-Ablage
# ---------------------------------------------------------------------------


class HeizzonenSpeicher:
    """heizzonen.yaml lesen und atomar zurueckschreiben."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.einstellungen = Heizungseinstellungen()

    def load(self) -> Heizungseinstellungen:
        if not self.path.exists():
            self.einstellungen = Heizungseinstellungen()
            return self.einstellungen
        try:
            with self.path.open("r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            self.einstellungen = Heizungseinstellungen.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            # Eine kaputte Datei darf den Start nicht verhindern — die Bruecke
            # macht mehr als Heizung.
            log.error("heizzonen.yaml nicht lesbar (%s): %s — Heizungsregelung "
                      "startet ohne Zonen.", self.path, exc)
            self.einstellungen = Heizungseinstellungen()
        return self.einstellungen

    def save(self, einstellungen: Heizungseinstellungen | None = None) -> None:
        if einstellungen is not None:
            self.einstellungen = einstellungen
        daten = self.einstellungen.model_dump(mode="json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            yaml.safe_dump(daten, f, sort_keys=False, allow_unicode=True)
        tmp.replace(self.path)

    def zone(self, zone_id: str) -> Heizzone | None:
        return next((z for z in self.einstellungen.zonen if z.id == zone_id), None)


# ---------------------------------------------------------------------------
# Regelwerk
# ---------------------------------------------------------------------------


@dataclass
class Kreislage:
    """Was der Regler ueber einen einzelnen Heizkreis weiss."""

    device_id: str
    channel_id: str
    name: str
    status: str = "ok"
    hinweis: str = ""
    ist_c: float | None = None
    ist_alter_s: float | None = None
    basissollwert_c: float = 21.0
    verschiebung_max_k: float = 5.0
    verschiebung_k: float | None = None
    verschiebung_begrenzt: bool = False
    sendbar: bool = False
    # Vom Regler nachgetragen — wann dieser Kreis zuletzt wirklich gefunkt hat.
    gesendet_vor_s: float | None = None
    gesendet_verschiebung_k: float | None = None

    def als_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "channel_id": self.channel_id,
            "name": self.name,
            "status": self.status,
            "hinweis": self.hinweis,
            "ist_c": None if self.ist_c is None else round(self.ist_c, 2),
            "ist_alter_s": None if self.ist_alter_s is None else round(self.ist_alter_s, 1),
            "basissollwert_c": self.basissollwert_c,
            "verschiebung_max_k": self.verschiebung_max_k,
            "verschiebung_k": None if self.verschiebung_k is None else round(self.verschiebung_k, 2),
            "verschiebung_begrenzt": self.verschiebung_begrenzt,
            "sendbar": self.sendbar,
            "gesendet_vor_s": (None if self.gesendet_vor_s is None
                               else round(self.gesendet_vor_s, 1)),
            "gesendet_verschiebung_k": self.gesendet_verschiebung_k,
        }


@dataclass
class Raumlage:
    """Was der Regler ueber einen Raum weiss."""

    raum_id: str
    name: str
    aktiv: bool = True
    status: str = "ok"
    hinweis: str = ""
    fuehler: list[str] = dc_field(default_factory=list)
    benutzte_fuehler: list[str] = dc_field(default_factory=list)
    ist_c: float | None = None
    ist_alter_s: float | None = None
    offset_quelle: str = ""          # Anzeigename des Sollwertrads
    raumsteller_k: float = 0.0       # was Rad ODER Handfeld gerade beitraegt
    raumsteller_quelle: str = ""     # rad | hand | "" (nichts wirksam)
    kontakte_offen: list[str] = dc_field(default_factory=list)
    abgesenkt: bool = False
    dauerabsenkung: bool = False     # dauerhaft abgesenkt (nicht per Kontakt)
    sollwert_c: float | None = None  # aus dem Zeitplan, nach evtl. Absenkung
    # Was am Aktor ankommt: Basis + (begrenzte) Verschiebung, also inklusive
    # fester Korrektur und Sollwertrad. Das ist die Zahl, die ein Mensch
    # erwartet, wenn er einem Raum +2 K gibt.
    sollwert_wirksam_c: float | None = None
    sendbar: bool = False
    # Aktortest: laeuft gerade ein Testlauf, und bis wann (Unixzeit)?
    test_aktiv: bool = False
    test_rest_s: float | None = None
    kreise: list[Kreislage] = dc_field(default_factory=list)

    def als_dict(self) -> dict[str, Any]:
        return {
            "raum": self.raum_id,
            "name": self.name,
            "aktiv": self.aktiv,
            "status": self.status,
            "hinweis": self.hinweis,
            "fuehler": self.fuehler,
            "benutzte_fuehler": self.benutzte_fuehler,
            "ist_c": None if self.ist_c is None else round(self.ist_c, 2),
            "ist_alter_s": None if self.ist_alter_s is None else round(self.ist_alter_s, 1),
            "offset_quelle": self.offset_quelle,
            "raumsteller_k": round(self.raumsteller_k, 2),
            "raumsteller_quelle": self.raumsteller_quelle,
            "kontakte_offen": self.kontakte_offen,
            "abgesenkt": self.abgesenkt,
            "dauerabsenkung": self.dauerabsenkung,
            "sollwert_c": None if self.sollwert_c is None else round(self.sollwert_c, 2),
            "sollwert_wirksam_c": (None if self.sollwert_wirksam_c is None
                                   else round(self.sollwert_wirksam_c, 2)),
            "sendbar": self.sendbar,
            "test_aktiv": self.test_aktiv,
            "test_rest_s": (None if self.test_rest_s is None
                            else round(self.test_rest_s)),
            "kreise": [k.als_dict() for k in self.kreise],
        }


@dataclass
class Zonenlage:
    """Was der Regler ueber eine Zone gerade weiss. Reine Auskunft, kein I/O."""

    zone_id: str
    name: str
    aktiv: bool
    status: str                      # ok | teilweise | inaktiv | kein_istwert
                                     # | istwert_alt | istwert_unplausibel
                                     # | kein_sollwert | kein_ziel | kein_fuehler
    hinweis: str = ""
    raeume: list[Raumlage] = dc_field(default_factory=list)
    ist_c: float | None = None
    ist_alter_s: float | None = None
    fenster: str | None = None
    sollwert_c: float | None = None
    sollwert_plan_c: float | None = None   # ohne Kontakt-Absenkung
    sollwert_quelle: str = ""        # fenster | sollwertsteller | restzeit
                                     # | letzter_wert | kontakt
    kontakte_offen: list[str] = dc_field(default_factory=list)
    abgesenkt: bool = False
    verschiebung_k: float | None = None
    verschiebung_begrenzt: bool = False
    sendbar: bool = False

    def als_dict(self) -> dict[str, Any]:
        return {
            "zone": self.zone_id,
            "name": self.name,
            "aktiv": self.aktiv,
            "status": self.status,
            "hinweis": self.hinweis,
            "ist_c": None if self.ist_c is None else round(self.ist_c, 2),
            "ist_alter_s": None if self.ist_alter_s is None else round(self.ist_alter_s, 1),
            "raeume": [r.als_dict() for r in self.raeume],
            "fenster": self.fenster,
            "sollwert_c": None if self.sollwert_c is None else round(self.sollwert_c, 2),
            "sollwert_plan_c": None if self.sollwert_plan_c is None else round(self.sollwert_plan_c, 2),
            "sollwert_quelle": self.sollwert_quelle,
            "kontakte_offen": self.kontakte_offen,
            "abgesenkt": self.abgesenkt,
            "verschiebung_k": None if self.verschiebung_k is None else round(self.verschiebung_k, 2),
            "verschiebung_begrenzt": self.verschiebung_begrenzt,
        }


@dataclass
class Raumwert:
    """Ergebnis der Fuehler-Verrechnung eines Raums."""

    wert: float | None = None
    alter_s: float | None = None
    status: str = "ok"          # ok | kein_fuehler | kein_istwert
                                # | istwert_alt | istwert_unplausibel
    hinweis: str = ""
    benutzt: list[str] = dc_field(default_factory=list)
    verworfen: list[str] = dc_field(default_factory=list)


def raum_istwert(
    fuehler: list[Fuehler],
    regel: str,
    istwerte: IstwertSpeicher,
    ts: float,
    min_c: float,
    max_c: float,
    max_alter_s: int,
) -> Raumwert:
    """
    Mehrere Fuehler eines Raums zu einem Ist-Wert verrechnen.

    Jeder Fuehler wird einzeln geprueft: unplausible und veraltete Werte
    fliegen raus. Bleibt mindestens einer uebrig, wird nach `regel` verrechnet
    und im Hinweis vermerkt, welcher fehlt — denn wenn von "Vorne | Hinten"
    einer ausfaellt, aendert sich der Mittelwert still.
    Bleibt keiner uebrig, gibt es **keinen** Ist-Wert: dann wird nicht
    gesendet, statt einen auszudenken.
    """
    ergebnis = Raumwert()
    if not fuehler:
        ergebnis.status = "kein_fuehler"
        ergebnis.hinweis = "Kein Raumfuehler hinterlegt."
        return ergebnis

    brauchbar: list[tuple[float, float, float]] = []   # (wert, gewicht, alter)
    grund_unplausibel = False
    grund_alt = False

    for f in fuehler:
        eintrag = istwerte.hole_zahl(f.topic, f.feld)
        if eintrag is None:
            ergebnis.verworfen.append(f"{f.anzeigename()}: nichts empfangen")
            continue
        wert, wert_ts = eintrag
        alter = max(0.0, ts - wert_ts)
        if not (min_c <= wert <= max_c):
            grund_unplausibel = True
            ergebnis.verworfen.append(
                f"{f.anzeigename()}: {wert:g} ausserhalb {min_c:g}..{max_c:g} °C")
            continue
        if max_alter_s and alter > max_alter_s:
            grund_alt = True
            ergebnis.verworfen.append(
                f"{f.anzeigename()}: {int(alter)} s alt")
            continue
        brauchbar.append((wert, f.gewicht, alter))
        ergebnis.benutzt.append(f"{f.anzeigename()} {wert:g} °C")

    if not brauchbar:
        if grund_unplausibel:
            ergebnis.status = "istwert_unplausibel"
        elif grund_alt:
            ergebnis.status = "istwert_alt"
        else:
            ergebnis.status = "kein_istwert"
        ergebnis.hinweis = "Kein brauchbarer Raumwert: " + \
            "; ".join(ergebnis.verworfen)
        return ergebnis

    if regel == "kleinster":
        ergebnis.wert = min(w for w, _, _ in brauchbar)
    elif regel == "groesster":
        ergebnis.wert = max(w for w, _, _ in brauchbar)
    else:
        summe = sum(w * g for w, g, _ in brauchbar)
        gewichte = sum(g for _, g, _ in brauchbar)
        ergebnis.wert = summe / gewichte if gewichte else None
    ergebnis.alter_s = max(a for _, _, a in brauchbar)

    if ergebnis.verworfen:
        ergebnis.hinweis = (
            f"Nur {len(brauchbar)} von {len(fuehler)} Fuehlern benutzt — "
            + "; ".join(ergebnis.verworfen)
        )
    return ergebnis


def offene_kontakte(kontakte: list[Kontakt],
                    istwerte: IstwertSpeicher) -> tuple[list[str], list[str]]:
    """(offene, unbekannte) — nach Anzeigenamen."""
    offen: list[str] = []
    unbekannt: list[str] = []
    for k in kontakte:
        if not k.topic:
            continue
        zustand = kontakt_ist_offen(k, istwerte)
        if zustand is None:
            unbekannt.append(k.anzeigename())
        elif zustand:
            offen.append(k.anzeigename())
    return offen, unbekannt


def absenkwert(zone: Heizzone, sollwert_c: float | None,
               istwerte: IstwertSpeicher) -> float | None:
    """
    Der Sollwert, der bei offenem Kontakt gilt.

    Bevorzugt der Sollwert einer benannten Zeitzone — so bleibt der
    Nachtsollwert die eine Quelle der Wahrheit, auch wenn er in der Visu
    verstellt wird. Danach der Rueckfall-Sollwert des Bereichs: seit dem
    Konzeptwechsel gibt es keine Nacht-Zeitzone mehr, auf
    die man zeigen koennte — die Nachttemperatur steht dort. Erst zuletzt der
    laufende Sollwert minus `absenkung_k`.
    """
    if zone.absenkung_zeitzone:
        ziel = next((f for f in zone.fenster
                     if f.name == zone.absenkung_zeitzone), None)
        if ziel is not None:
            wert, _, _ = fenster_sollwert(ziel, istwerte)
            return wert
    if zone.restzeit.aktiv:
        wert, _, _ = fenster_sollwert(zone.restzeit, istwerte)
        return wert
    if sollwert_c is None:
        return None
    return sollwert_c - zone.absenkung_k


def doppelte_heizkreise(zonen: list[Heizzone]) -> list[str]:
    """
    Ausgaenge, die in mehr als einem Raum stehen — je einer eine Klartext-Zeile.

    Warum das geprueft werden MUSS: Ein Ausgang des SRC-DO8
    hoert genau eine Sender-ID. Steht derselbe Heizkreis in zwei Raeumen oder
    Bereichen, funken beide auf denselben Ausgang und ueberschreiben sich
    im Sendetakt — der Sollwert kippt alle paar Minuten hin und her, und zwar
    ohne Fehlermeldung: jede der beiden Stellen haelt fuer sich alles fuer in
    Ordnung.
    """
    belegt: dict[tuple[str, str], str] = {}
    meldungen: list[str] = []
    for zone in zonen:
        for raum in zone.raeume:
            for kreis in raum.heizkreise:
                if not kreis.device_id or not kreis.channel_id:
                    continue
                wo = f"{zone.anzeigename()} / {raum.anzeigename()}"
                schon = belegt.get((kreis.device_id, kreis.channel_id))
                if schon:
                    meldungen.append(
                        f"Heizkreis {kreis.device_id}/{kreis.channel_id} steht "
                        f"in zwei Raeumen: {schon} und {wo}. Ein Ausgang "
                        f"gehoert genau einem Raum — sonst funken beide "
                        f"dagegen."
                    )
                else:
                    belegt[(kreis.device_id, kreis.channel_id)] = wo
    return meldungen


def doppelte_raum_ids(zonen: list[Heizzone]) -> list[str]:
    """
    Raum-Kennungen, die innerhalb eines Bereichs zweimal vorkommen.

    Die Kennung ist der Schluessel des Raums in der Lage und im MQTT-Zustand
    des Bereichs. Zweimal dieselbe — was beim Verschieben eines Raums
    leicht passiert — und man sieht zwei Kaestchen, die sich denselben Platz
    teilen.
    """
    meldungen: list[str] = []
    for zone in zonen:
        gesehen: set[str] = set()
        for raum in zone.raeume:
            schluessel = raum.id or slug_oder_name(raum)
            if schluessel in gesehen:
                meldungen.append(
                    f"Im Bereich '{zone.anzeigename()}' gibt es die "
                    f"Raum-Kennung '{schluessel}' zweimal."
                )
            gesehen.add(schluessel)
    return meldungen


def slug_oder_name(raum: Raum) -> str:
    """Schluessel eines Raums fuers MQTT-Topic, falls keine id gesetzt ist."""
    from .mqtt_client import slugify
    return slugify(raum.name or "raum")


def beurteile_raum(
    zone: Heizzone,
    raum: Raum,
    sollwert_c: float | None,
    istwerte: IstwertSpeicher,
    ts: float,
    absenk_c: float | None = None,
    schalter: dict[str, bool] | None = None,
    test_bis_ts: float | None = None,
) -> Raumlage:
    """
    Ein Raum: seine Fuehler zu einem Ist-Wert, sein Sollwertrad, seine
    Kontakte — und daraus je Heizkreis ein Telegramm.

    `test_bis_ts` setzt den **Aktortest**: bis zu diesem Zeitpunkt geht statt
    des Zeitplans die groesstmoegliche Sollwertverschiebung raus, damit der
    Regler den Kreis aufmacht und man im Verteiler zusehen kann, ob sich der
    Stellmotor bewegt.
    """
    lage = Raumlage(
        raum_id=raum.id or slug_oder_name(raum),
        name=raum.anzeigename(),
        aktiv=raum.aktiv,
        fuehler=[f.anzeigename() for f in raum.fuehler],
    )

    # --- Ist-Wert aus allen Fuehlern dieses Raums
    regel = raum.fuehler_regel or zone.fuehler_regel
    messung = raum_istwert(raum.fuehler, regel, istwerte, ts,
                           zone.ist_min_c, zone.ist_max_c, zone.ist_max_alter_s)
    lage.ist_c = messung.wert
    lage.ist_alter_s = messung.alter_s
    lage.benutzte_fuehler = list(messung.benutzt)

    # --- Das eine Sollwertrad dieses Raums (z.B. Thanos an der Wand)
    rad_meldung = ""
    quelle = raum.offset_quelle
    if quelle and quelle.topic and zone.offset_grenze_k > 0:
        lage.offset_quelle = quelle.anzeigename()
        eintrag = istwerte.hole_zahl(quelle.topic, quelle.feld)
        if eintrag is not None:
            roh_rad = eintrag[0]
            if -20.0 <= roh_rad <= 20.0:
                lage.raumsteller_k = max(-zone.offset_grenze_k,
                                         min(zone.offset_grenze_k, roh_rad))
                lage.raumsteller_quelle = "rad"
                if abs(roh_rad - lage.raumsteller_k) > 1e-6:
                    rad_meldung = (
                        f"Sollwertrad steht auf {roh_rad:+.1f} K, wirksam sind "
                        f"{lage.raumsteller_k:+.1f} K (Grenze "
                        f"±{zone.offset_grenze_k:g} K)."
                    )
            else:
                rad_meldung = (f"Sollwertrad meldet {roh_rad:g} — unplausibel, "
                               f"ignoriert.")

    # --- Kein Rad (oder keins, das liefert)? Dann gilt das Handfeld.
    #     Ausdruecklich ueberschreibend, nicht addierend: sonst haette ein Raum
    #     mit Rad zwei Verstellungen, die sich aufaddieren, und niemand wuesste
    #     mehr, warum es 2 K zu warm ist.
    if lage.raumsteller_quelle == "" and raum.handverschiebung_k and \
            zone.offset_grenze_k > 0:
        roh_hand = float(raum.handverschiebung_k)
        lage.raumsteller_k = max(-zone.offset_grenze_k,
                                 min(zone.offset_grenze_k, roh_hand))
        lage.raumsteller_quelle = "hand"
        if abs(roh_hand - lage.raumsteller_k) > 1e-6:
            rad_meldung = (
                f"Sollwertverschiebung steht auf {roh_hand:+.1f} K, wirksam "
                f"sind {lage.raumsteller_k:+.1f} K (Grenze "
                f"±{zone.offset_grenze_k:g} K)."
            )
        elif lage.offset_quelle:
            rad_meldung = (
                f"Sollwertrad '{lage.offset_quelle}' liefert nichts — es gilt "
                f"die Verschiebung von Hand ({lage.raumsteller_k:+.1f} K)."
            )

    # --- Eigene Kontakte senken NUR diesen Raum ab
    offen, unbekannt = offene_kontakte(raum.kontakte, istwerte)
    lage.kontakte_offen = list(offen)
    if offen and absenk_c is not None:
        sollwert_c = absenk_c
        lage.abgesenkt = True

    # --- Dauerabsenkung: laengere Nichtnutzung, per Schalter aufhebbar.
    #     Sie senkt wie ein offener Kontakt, haelt den Kreis aber am Funken —
    #     anders als `aktiv=False`, wo der Aktor nach 90 Minuten in den
    #     Sensorausfall laeuft und auf dem letzten Wert stehen bleibt.
    dauer_meldung = ""
    if raum.dauerabsenkung:
        ausnahme = raum.dauerabsenkung_ausnahme_schalter
        frei = bool(ausnahme) and bool((schalter or {}).get(ausnahme, False))
        if not frei:
            lage.dauerabsenkung = True
            if absenk_c is None:
                dauer_meldung = ("Dauerhaft abgesenkt eingestellt, aber der "
                                 "Bereich hat keinen Absenk-Sollwert — es "
                                 "wird NICHT abgesenkt.")
            elif sollwert_c is not None and absenk_c >= sollwert_c - 1e-6:
                # Sichtbar machen statt still nichts tun: steht die Absenkung
                # des Bereichs auf 0 K (oder zeigt die Absenk-Zeitzone auf
                # denselben Wert), haelt man einen Raum fuer stillgelegt, der
                # in Wahrheit voll mitheizt.
                sollwert_c = absenk_c
                dauer_meldung = (
                    f"Dauerhaft abgesenkt eingestellt, aber die Absenkung des "
                    f"Bereichs ergibt {absenk_c:g} °C — das senkt nichts. "
                    f"Absenk-Zeitzone oder Absenkung in K pruefen."
                )
            else:
                sollwert_c = absenk_c
                lage.abgesenkt = True
                dauer_meldung = "Dauerhaft abgesenkt."

    lage.sollwert_c = sollwert_c

    # --- Aktortest: geht vor Zeitplan, Absenkung und Dauerabsenkung.
    #     Er faelscht dabei KEINEN Ist-Wert — gesendet wird die echte
    #     Raumtemperatur mit maximaler Sollwertverschiebung. Ein ausgedachter
    #     Messwert waere fuer den Regler nicht von einer Messung zu
    #     unterscheiden und bliebe bei einem Absturz mitten im Test stehen.
    if test_bis_ts is not None and test_bis_ts > ts:
        lage.test_aktiv = True
        lage.test_rest_s = test_bis_ts - ts

    # --- Je Heizkreis ein eigenes Telegramm
    lage.kreise = [
        _beurteile_kreis(zone, raum, kreis, lage, sollwert_c, messung,
                         test=lage.test_aktiv)
        for kreis in raum.heizkreise
    ]

    # --- Der wirksame Sollwert: was der Aktor daraus macht. Bevorzugt aus dem
    #     Heizkreis, denn dort ist die Verschiebung bereits auf den Bereich des
    #     Geraets begrenzt — die Anzeige soll nicht mehr versprechen, als
    #     ankommt.
    mit_verschiebung = next((k for k in lage.kreise
                             if k.verschiebung_k is not None), None)
    if mit_verschiebung is not None:
        lage.sollwert_wirksam_c = (mit_verschiebung.basissollwert_c
                                   + mit_verschiebung.verschiebung_k)
    elif sollwert_c is not None:
        lage.sollwert_wirksam_c = (sollwert_c + zone.sollwert_offset_k
                                   + raum.sollwert_offset_k
                                   + lage.raumsteller_k)

    if not raum.aktiv:
        lage.status = "inaktiv"
        lage.hinweis = "Raum ist abgeschaltet."
    elif not raum.heizkreise:
        lage.status = "kein_ziel"
        lage.hinweis = "Dem Raum ist kein Heizkreis zugeordnet."
    elif sollwert_c is None and not lage.test_aktiv:
        # Im Test ist der Zeitplan gleichgueltig — gesendet wird ohnehin die
        # volle Verschiebung. Sonst liesse sich gerade der Raum nicht pruefen,
        # bei dem noch kein Sollwert eingerichtet ist.
        lage.status = "kein_sollwert"
        lage.hinweis = ("Keine Zeitzone aktiv und noch kein frueherer "
                        "Sollwert bekannt.")
    elif messung.wert is None:
        lage.status = messung.status
        lage.hinweis = messung.hinweis
        if messung.status == "istwert_alt":
            lage.hinweis += (" — es wird nichts gesendet; nach 90 Minuten "
                             "ohne Telegramm meldet der Regler Sensorausfall.")
    elif lage.test_aktiv:
        lage.sendbar = any(k.sendbar for k in lage.kreise)
        lage.hinweis = (
            f"Aktortest laeuft: volle Sollwertverschiebung, der Kreis muss "
            f"aufmachen. Endet von selbst in "
            f"{max(0, round((lage.test_rest_s or 0) / 60))} Minuten."
        )
    else:
        lage.sendbar = any(k.sendbar for k in lage.kreise)
        if dauer_meldung:
            lage.hinweis = dauer_meldung
            if offen:
                lage.hinweis += f" Zusaetzlich offen: {', '.join(offen)}."
        elif lage.abgesenkt:
            lage.hinweis = f"Abgesenkt: {', '.join(offen)} offen."
        elif unbekannt:
            lage.hinweis = (f"Kontakt ohne verwertbaren Zustand: "
                            f"{', '.join(unbekannt)} — es wird nicht abgesenkt.")
        elif rad_meldung:
            lage.hinweis = rad_meldung
        elif messung.hinweis:
            lage.hinweis = messung.hinweis
        else:
            begrenzt = next((k for k in lage.kreise
                             if k.verschiebung_begrenzt), None)
            if begrenzt is not None:
                lage.hinweis = begrenzt.hinweis

    return lage


def _beurteile_kreis(
    zone: Heizzone,
    raum: Raum,
    kreis: Heizkreis,
    raumlage: "Raumlage",
    sollwert_c: float | None,
    messung: "Raumwert",
    test: bool = False,
) -> Kreislage:
    """
    Der Anteil, der wirklich am Ausgang haengt: Basissollwert und
    Verschiebungsbereich. Ist-Wert und Rad kommen vom Raum.

    `test=True` schickt die groesstmoegliche Verschiebung — der Aktortest.
    """
    basis = (kreis.basissollwert_c if kreis.basissollwert_c is not None
             else zone.basissollwert_c)
    spanne = (kreis.verschiebung_max_k if kreis.verschiebung_max_k is not None
              else zone.verschiebung_max_k)

    lage = Kreislage(
        device_id=kreis.device_id,
        channel_id=kreis.channel_id,
        name=kreis.anzeigename(),
        basissollwert_c=basis,
        verschiebung_max_k=spanne,
        ist_c=raumlage.ist_c,
        ist_alter_s=raumlage.ist_alter_s,
    )

    if test:
        # Volle Verschiebung nach oben. Der Regler bekommt damit einen
        # Sollwert weit ueber der gemessenen Temperatur und macht auf.
        lage.verschiebung_k = spanne
    elif sollwert_c is not None:
        roh = (sollwert_c + zone.sollwert_offset_k + raum.sollwert_offset_k
               + raumlage.raumsteller_k) - basis
        lage.verschiebung_k = max(-spanne, min(spanne, roh))
        lage.verschiebung_begrenzt = abs(roh - lage.verschiebung_k) > 1e-6

    if not raum.aktiv:
        lage.status = "inaktiv"
    elif not kreis.device_id or not kreis.channel_id:
        lage.status = "kein_ziel"
        lage.hinweis = "Kein Ausgang gewaehlt."
    elif sollwert_c is None and not test:
        lage.status = "kein_sollwert"
    elif messung.wert is None:
        # Auch im Test wird kein Ist-Wert erfunden: das Telegramm traegt
        # beides, und ein ausgedachter Messwert waere vom echten nicht zu
        # unterscheiden. Ohne Messwert bleibt der Test stumm — dann ist der
        # fehlende Fuehler das eigentliche Problem.
        lage.status = messung.status
        lage.hinweis = messung.hinweis
    elif test:
        lage.sendbar = True
        lage.hinweis = (f"Test: Sollwert {basis + spanne:.1f} °C "
                        f"(Basis +{spanne:g} K) — der Kreis muss aufmachen.")
    else:
        lage.sendbar = True
        if lage.verschiebung_begrenzt:
            lage.hinweis = (
                f"Sollwert {sollwert_c:.1f} °C liegt ausserhalb dessen, was "
                f"der Ausgang kann (Basis {basis:.1f} °C ± {spanne:.0f} K) — "
                f"gesendet wird die Grenze."
            )
    return lage


def beurteile_zone(
    zone: Heizzone,
    jetzt: datetime,
    istwerte: IstwertSpeicher,
    zustand: HeizungsZustand,
    now_ts: float | None = None,
    schalter: dict[str, bool] | None = None,
    tests: dict[str, float] | None = None,
) -> Zonenlage:
    """
    Der gesamte Entscheidungsweg einer Zone — ohne Seiteneffekte, damit er
    vollstaendig testbar bleibt.
    """
    ts = now_ts if now_ts is not None else jetzt.timestamp()
    lage = Zonenlage(
        zone_id=zone.id,
        name=zone.anzeigename(),
        aktiv=zone.aktiv,
        status="ok",
    )

    # --- Sollwert: aktives Fenster, sonst der zuletzt gueltige Wert
    fenster = aktives_fenster(zone, jetzt, schalter)
    if fenster is not None:
        lage.fenster = fenster.name
        lage.sollwert_c = float(fenster.sollwert_c)
        lage.sollwert_quelle = "fenster"
        # Kommt der Sollwert von einem Sollwertsteller in der Visu, gilt der —
        # der feste Wert im Fenster ist dann nur der Rueckfall, bis der erste
        # Wert eingetroffen ist.
        lage.sollwert_c, lage.sollwert_quelle, meldung = fenster_sollwert(
            fenster, istwerte,
        )
        if meldung:
            lage.hinweis = meldung
    elif zone.restzeit.aktiv:
        # Keine Zeitzone laeuft: der Rueckfall-Sollwert des Bereichs.
        lage.fenster = zone.restzeit.name
        lage.sollwert_c, quelle, meldung = fenster_sollwert(
            zone.restzeit, istwerte,
        )
        lage.sollwert_quelle = ("sollwertsteller" if quelle == "sollwertsteller"
                                else "restzeit")
        if meldung:
            lage.hinweis = meldung
    else:
        letzter = zustand.sollwert(zone.id)
        if letzter is not None:
            lage.sollwert_c = float(letzter)
            lage.sollwert_quelle = "letzter_wert"

    # --- Zwangsabsenkung des GANZEN Bereichs ueber Tuer-/Fensterkontakte.
    #     Der Zeitplan-Sollwert bleibt in sollwert_plan_c erhalten, damit er
    #     weiterhin als "letzter gueltiger Wert" gemerkt werden kann — sonst
    #     bliebe nach dem Schliessen der Tuer die Absenkung haengen.
    lage.sollwert_plan_c = lage.sollwert_c
    absenk_c = absenkwert(zone, lage.sollwert_c, istwerte)
    offen, unbekannt = offene_kontakte(zone.kontakte, istwerte)
    lage.kontakte_offen = list(offen)
    if offen and absenk_c is not None:
        lage.sollwert_c = absenk_c
        lage.sollwert_quelle = "kontakt"
        lage.abgesenkt = True
        lage.hinweis = f"Abgesenkt: {', '.join(offen)} offen."
    elif unbekannt:
        lage.hinweis = (f"Kontakt ohne verwertbaren Zustand: "
                        f"{', '.join(unbekannt)} — es wird nicht abgesenkt.")

    # --- Je Raum einzeln beurteilen; darin je Heizkreis ein Telegramm.
    lage.raeume = [
        beurteile_raum(zone, raum, lage.sollwert_c, istwerte, ts,
                       absenk_c=absenk_c, schalter=schalter,
                       test_bis_ts=(tests or {}).get(
                           raum.id or slug_oder_name(raum)))
        for raum in zone.raeume
    ]

    # --- Zusammenfassung fuer die Zeile in der Uebersicht
    if lage.raeume:
        erster = lage.raeume[0]
        lage.ist_c = erster.ist_c
        lage.ist_alter_s = erster.ist_alter_s
        alle_kreise = [k for r in lage.raeume for k in r.kreise]
        if alle_kreise:
            lage.verschiebung_k = alle_kreise[0].verschiebung_k
        lage.verschiebung_begrenzt = any(k.verschiebung_begrenzt
                                         for k in alle_kreise)

    if not zone.aktiv:
        lage.status = "inaktiv"
        lage.hinweis = "Bereich ist abgeschaltet."
    elif not zone.raeume:
        lage.status = "kein_ziel"
        lage.hinweis = "Dem Bereich ist kein Raum zugeordnet."
    else:
        lage.sendbar = any(r.sendbar for r in lage.raeume)
        stumm = [r for r in lage.raeume if r.aktiv and not r.sendbar]
        if not stumm:
            hinweise = [r.hinweis for r in lage.raeume if r.hinweis]
            if hinweise:
                lage.hinweis = hinweise[0]
        elif lage.sendbar:
            # Ein Teil regelt, ein Teil nicht — das muss sichtbar sein, sonst
            # bleibt ein Raum kalt und es faellt niemandem auf.
            lage.status = "teilweise"
            lage.hinweis = (
                f"{len(stumm)} von {len(lage.raeume)} Raeumen regeln nicht: "
                + "; ".join(f"{r.name} ({r.hinweis})" for r in stumm)
            )
        else:
            lage.status = stumm[0].status
            lage.hinweis = stumm[0].hinweis

    return lage


TEST_DAUER_S = 900.0
"""
Wie lange ein Aktortest laeuft, bevor er von selbst endet (15 Minuten).

Er MUSS von selbst enden: Waehrend des Tests bekommt der Kreis den vollen
Sollwert. Bliebe ein vergessener Test stehen, heizte der Raum auf Anschlag
weiter, und zwar genau so unauffaellig wie alle teuren Fehler in diesem
Projekt. 15 Minuten reichen, um in den Verteiler zu sehen; laengere Tests
setzt man einfach neu.
"""


class Heizungsregler:
    """
    Der Takt: sieht regelmaessig nach, was gelten soll, und schickt es los.

    Die Entscheidung steckt vollstaendig in `beurteile_zone`; hier passiert nur
    noch Senden, Veroeffentlichen und das Merken des letzten Sollwerts.
    """

    def __init__(
        self,
        speicher: HeizzonenSpeicher,
        zustand: HeizungsZustand,
        tx_router=None,
        publisher=None,
    ) -> None:
        self.speicher = speicher
        self.zustand = zustand
        self.tx_router = tx_router
        self.publisher = publisher
        self.istwerte = IstwertSpeicher()
        self._letztes_senden: dict[str, float] = {}
        self._letzte_verschiebung: dict[str, float] = {}
        self._letzte_lage: dict[str, dict[str, Any]] = {}
        self._letzter_schalter: dict[str, dict[str, Any]] = {}
        # Laufende Aktortests: "<zone>|<raum>" -> Unixzeit, bis wann.
        # Bewusst NUR im Arbeitsspeicher und nicht in heizzonen.yaml: ein Test
        # ist Betrieb, kein Zustand der Anlage. Ein Neustart beendet ihn — das
        # ist die sichere Richtung.
        self._test_bis: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------- Topics

    @property
    def basis_topic(self) -> str:
        return (self.speicher.einstellungen.basis_topic or "heizung").strip("/")

    def zonen_topic(self, zone_id: str, suffix: str = "state") -> str:
        return f"{self.basis_topic}/{zone_id}/{suffix}"

    def fenster_topic(self, zone_id: str, fenster_name: str,
                      suffix: str = "state") -> str:
        from .mqtt_client import slugify
        return f"{self.basis_topic}/{zone_id}/fenster/{slugify(fenster_name)}/{suffix}"

    def schalter_topic(self, schalter_id: str, suffix: str = "state") -> str:
        return f"{self.basis_topic}/schalter/{schalter_id}/{suffix}"

    def jetzt(self) -> datetime:
        """Die Uhr, nach der der Zeitplan laeuft (Konfiguration, nicht Container)."""
        return jetzt_in(self.speicher.einstellungen.zeitzone)

    def wertquellen(self) -> list[tuple[str, str]]:
        """Alle (Topic, Feld)-Paare, aus denen Werte gelesen werden."""
        quellen: set[tuple[str, str]] = set()
        for zone in self.speicher.einstellungen.zonen:
            for raum in zone.raeume:
                for fuehler in raum.fuehler:
                    if fuehler.topic:
                        quellen.add((fuehler.topic, fuehler.feld))
                if raum.offset_quelle and raum.offset_quelle.topic:
                    quellen.add((raum.offset_quelle.topic,
                                 raum.offset_quelle.feld))
            for f in self._sollwertbezuege(zone):
                if f.sollwert_topic:
                    quellen.add((f.sollwert_topic, f.sollwert_feld))
        return sorted(quellen)

    @staticmethod
    def _sollwertbezuege(zone: Heizzone) -> list[Sollwertbezug]:
        """Zeitzonen UND der Rueckfall-Sollwert — beide haengen an der Visu."""
        return [*zone.fenster, zone.restzeit]

    def _stellerquellen(self) -> set[tuple[str, str]]:
        return {
            (f.sollwert_topic, f.sollwert_feld)
            for zone in self.speicher.einstellungen.zonen
            for f in self._sollwertbezuege(zone)
            if f.sollwert_topic
        }

    def gespeicherte_steller_uebernehmen(self) -> int:
        """
        Beim Start: gesicherte Stellerwerte in den Wertspeicher zuruecklegen.

        Nur fuer Stellerquellen, die es in der Konfiguration noch gibt — sonst
        wuerden geloeschte Zeitzonen ewig mitgeschleppt.
        """
        gueltig = self._stellerquellen()
        uebernommen = 0
        for (topic, feld), (wert, ts) in self.zustand.steller().items():
            if (topic, feld) in gueltig:
                self.istwerte.setze(topic, wert, ts=ts, feld=feld)
                uebernommen += 1
        if uebernommen:
            log.info("Heizung: %d gesicherte Sollwertsteller uebernommen — "
                     "die Visu sendet sie nicht retained.", uebernommen)
        return uebernommen

    def kontaktquellen(self) -> list[tuple[str, str]]:
        """(Topic, Feld) aller Tuer-/Fensterkontakte."""
        quellen: set[tuple[str, str]] = set()
        for zone in self.speicher.einstellungen.zonen:
            for k in zone.kontakte:
                if k.topic:
                    quellen.add((k.topic, k.feld))
            for raum in zone.raeume:
                for k in raum.kontakte:
                    if k.topic:
                        quellen.add((k.topic, k.feld))
        return sorted(quellen)

    def abo_topics(self) -> list[str]:
        """Alle Topics, die der Regler hoeren muss."""
        topics: set[str] = {t for t, _ in self.wertquellen()}
        topics |= {t for t, _ in self.kontaktquellen()}
        for s in self.speicher.einstellungen.schalter:
            if s.id:
                topics.add(self.schalter_topic(s.id, "set"))
        for zone in self.speicher.einstellungen.zonen:
            topics.add(self.zonen_topic(zone.id, "set"))
            for f in zone.fenster:
                topics.add(self.fenster_topic(zone.id, f.name, "set"))
        return sorted(topics)

    # ------------------------------------------------------ MQTT-Eingang

    async def mqtt_nachricht(self, topic: str, payload: bytes) -> bool:
        """
        Eine eingehende MQTT-Nachricht verarbeiten.
        True = war fuer uns, False = nicht zustaendig.
        """
        # 1) Wertquellen: Raum-Istwerte und Sollwertsteller. Ein Topic kann
        #    mehrere Felder tragen (die Visu schickt alle Steller einer Gruppe
        #    in EINEM JSON), darum wird je (Topic, Feld) einzeln ausgewertet.
        getroffen = False

        # Kontakte zuerst: deren Wert ist kein Messwert, sondern offen/zu.
        for quelle_topic, feld in self.kontaktquellen():
            if quelle_topic != topic:
                continue
            getroffen = True
            roh = rohwert_aus_payload(payload, feld)
            if roh is None:
                log.debug("Heizung: Kontakt '%s' Feld '%s' nicht lesbar",
                          topic, feld or "(ohne Feldnamen)")
            else:
                self.istwerte.setze(topic, roh, feld=feld)

        for quelle_topic, feld in self.wertquellen():
            if quelle_topic != topic:
                continue
            getroffen = True
            wert = wert_aus_payload(payload, feld)
            if wert is None:
                log.debug("Heizung: '%s' enthaelt keinen brauchbaren Wert "
                          "fuer Feld '%s'", topic, feld or "(ohne Feldnamen)")
                continue
            self.istwerte.setze(topic, wert, feld=feld)
            # Sollwertsteller zusaetzlich auf Platte — sie kommen nicht
            # retained, waeren nach einem Neustart also weg.
            if (topic, feld) in self._stellerquellen():
                self.zustand.merke_steller(topic, feld, wert)
        if getroffen:
            return True

        # 2) Sollwert eines Fensters / Zonen-Befehl
        if not topic.startswith(self.basis_topic + "/") or not topic.endswith("/set"):
            return False
        return await self._befehl_verarbeiten(topic, payload)

    async def _befehl_verarbeiten(self, topic: str, payload: bytes) -> bool:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            return False

        rest_roh = topic[len(self.basis_topic) + 1: -len("/set")]
        if rest_roh.split("/")[0] == "schalter":
            return await self._schalter_befehl(rest_roh, text)

        try:
            daten = json.loads(text)
        except json.JSONDecodeError:
            try:
                daten = {"sollwert_c": float(text.strip().replace(",", "."))}
            except ValueError:
                log.warning("Heizung: Befehl auf '%s' nicht lesbar: %r", topic, text)
                return True
        if not isinstance(daten, dict):
            daten = {"sollwert_c": daten}

        rest = topic[len(self.basis_topic) + 1: -len("/set")]
        teile = rest.split("/")
        zone = self.speicher.zone(teile[0]) if teile else None
        if zone is None:
            log.warning("Heizung: Befehl fuer unbekannte Zone '%s'", topic)
            return True

        geaendert = False
        if len(teile) == 3 and teile[1] == "fenster":
            from .mqtt_client import slugify
            ziel = next((f for f in zone.fenster
                         if slugify(f.name) == teile[2]), None)
            if ziel is None:
                log.warning("Heizung: Befehl fuer unbekanntes Fenster '%s'", topic)
                return True
            wert = daten.get("sollwert_c", daten.get("sollwert", daten.get("value")))
            if isinstance(wert, (int, float)) and not isinstance(wert, bool):
                if 5.0 <= float(wert) <= 35.0:
                    ziel.sollwert_c = float(wert)
                    geaendert = True
                    log.info("Heizung: Sollwert '%s'/'%s' per MQTT auf %.1f °C",
                             zone.anzeigename(), ziel.name, ziel.sollwert_c)
                else:
                    log.warning("Heizung: Sollwert %.1f fuer '%s' ausserhalb "
                                "5..35 °C — ignoriert", float(wert), topic)
        elif len(teile) == 1:
            wert = daten.get("sollwert_offset_k", daten.get("offset"))
            if isinstance(wert, (int, float)) and not isinstance(wert, bool):
                if -10.0 <= float(wert) <= 10.0:
                    zone.sollwert_offset_k = float(wert)
                    geaendert = True
            if isinstance(daten.get("aktiv"), bool):
                zone.aktiv = bool(daten["aktiv"])
                geaendert = True

        if geaendert:
            try:
                self.speicher.save()
            except Exception as exc:  # noqa: BLE001
                log.warning("Heizung: heizzonen.yaml nicht speicherbar: %s", exc)
            await self.einmal_regeln()
        return True

    # ---------------------------------------------------------- Schalter

    async def _schalter_befehl(self, rest: str, text: str) -> bool:
        """
        '<basis>/schalter/<id>/set' — Betriebsart von aussen umlegen.

        Erlaubt sind `true`/`false`, `on`/`off`, `an`/`aus`, `1`/`0` und
        `{"an": true}`. Die Visu schickt je nach Baustein das eine oder das
        andere; was nicht zu verstehen ist, wird abgelehnt statt geraten.
        """
        teile = rest.split("/")
        if len(teile) != 2 or not teile[1]:
            log.warning("Heizung: Schalter-Befehl '%s' ohne ID", rest)
            return True
        schalter = next((s for s in self.speicher.einstellungen.schalter
                         if s.id == teile[1]), None)
        if schalter is None:
            log.warning("Heizung: Befehl fuer unbekannten Schalter '%s'", teile[1])
            return True

        roh: Any = text.strip()
        try:
            geladen = json.loads(roh)
            roh = geladen.get("an", geladen.get("value")) \
                if isinstance(geladen, dict) else geladen
        except json.JSONDecodeError:
            pass
        stand = wahrheit_aus_payload(roh)
        if stand is None:
            log.warning("Heizung: Schalter '%s' — Wert %r nicht verstanden",
                        schalter.id, text)
            return True

        if stand != schalter.an:
            schalter.an = stand
            log.info("Heizung: Schalter '%s' per MQTT auf %s",
                     schalter.anzeigename(), "an" if stand else "aus")
            try:
                self.speicher.save()
            except Exception as exc:  # noqa: BLE001
                log.warning("Heizung: heizzonen.yaml nicht speicherbar: %s", exc)
            await self.einmal_regeln()
        return True

    async def _schalter_veroeffentlichen(
        self, einst: Heizungseinstellungen,
    ) -> None:
        """
        Schalterstand retained auf MQTT — damit die Visu ihn anzeigen kann und
        nach einem Broker-Neustart nicht im Dunkeln steht.
        """
        for s in einst.schalter:
            if not s.id:
                continue
            nutzlast = {"id": s.id, "name": s.anzeigename(), "an": bool(s.an)}
            if self._letzter_schalter.get(s.id) == nutzlast:
                continue
            self._letzter_schalter[s.id] = nutzlast
            await self._publish(self.schalter_topic(s.id), nutzlast,
                                retain=True)

    # ------------------------------------------------------------ Regeln

    async def einmal_regeln(self, jetzt: datetime | None = None) -> list[Zonenlage]:
        """Ein Durchlauf ueber alle Zonen. Auch von der Web-UI aufrufbar."""
        jetzt = jetzt or self.jetzt()
        ts = time.time()
        lagen: list[Zonenlage] = []
        einst = self.speicher.einstellungen
        schalter = einst.schalterstand()
        await self._schalter_veroeffentlichen(einst)

        for zone in einst.zonen:
            lage = beurteile_zone(zone, jetzt, self.istwerte, self.zustand,
                                  now_ts=ts, schalter=schalter,
                                  tests=self._tests_der_zone(zone.id, ts))
            lagen.append(lage)

            # Gemerkt wird der ZEITPLAN-Sollwert, nicht die Kontakt-Absenkung:
            # sonst bliebe nach dem Schliessen der Tuer die Absenkung als
            # "letzter gueltiger Wert" haengen.
            if lage.sollwert_plan_c is not None and lage.fenster:
                self.zustand.merke(zone.id, lage.sollwert_plan_c,
                                   lage.fenster, ts)

            if einst.aktiv and lage.sendbar:
                await self._vielleicht_senden(zone, lage, ts)

            # Nachtragen, wann jeder Kreis zuletzt wirklich gefunkt hat —
            # damit in der Oberflaeche steht, ob und wann automatisch
            # gesendet wurde, statt nur was gesendet WUERDE.
            for raumlage in lage.raeume:
                for kreis in raumlage.kreise:
                    schluessel = f"{zone.id}|{kreis.device_id}|{kreis.channel_id}"
                    letzte = self._letztes_senden.get(schluessel)
                    if letzte:
                        kreis.gesendet_vor_s = max(0.0, ts - letzte)
                        kreis.gesendet_verschiebung_k = \
                            self._letzte_verschiebung.get(schluessel)

            await self._vielleicht_veroeffentlichen(zone, lage)

        self.zustand.save_if_due(ts)
        return lagen

    # ------------------------------------------------------- Aktortest

    def _tests_der_zone(self, zone_id: str, ts: float) -> dict[str, float]:
        """
        Laufende Tests dieser Zone als {raum_id: bis_ts}. Abgelaufene fliegen
        dabei raus und werden protokolliert — man soll im Log sehen koennen,
        wann der Raum wieder normal regelt.
        """
        treffer: dict[str, float] = {}
        for schluessel, bis in list(self._test_bis.items()):
            zone, _, raum = schluessel.partition("|")
            if zone != zone_id:
                continue
            if bis <= ts:
                del self._test_bis[schluessel]
                log.info("Heizung: Aktortest '%s' beendet (Zeit abgelaufen), "
                         "der Raum regelt wieder nach Plan", schluessel)
                continue
            treffer[raum] = bis
        return treffer

    async def test_setzen(self, zone_id: str, raum_id: str, an: bool,
                          dauer_s: float = TEST_DAUER_S,
                          ) -> tuple[list[Zonenlage], str]:
        """
        Aktortest eines Raums ein- oder ausschalten und sofort funken.
        Liefert (Lagen, Warnung) — die Warnung ist leer, wenn alles glatt ist.

        Sofort, nicht beim naechsten Takt: Wer im Verteiler steht und die
        Kaestchen anhakt, will die Stellmotoren jetzt laufen sehen und nicht
        in fuenf Minuten.
        """
        schluessel = f"{zone_id}|{raum_id}"
        if an:
            self._test_bis[schluessel] = time.time() + max(60.0, dauer_s)
            log.info("Heizung: Aktortest '%s' gestartet (%.0f Minuten)",
                     schluessel, max(60.0, dauer_s) / 60)
        else:
            self._test_bis.pop(schluessel, None)
            log.info("Heizung: Aktortest '%s' von Hand beendet", schluessel)

        lagen = await self.jetzt_senden(zone_id=zone_id, raum_id=raum_id)

        # Nach dem Test muss der normale Wert hinterher — sonst bleibt der
        # Kreis auf dem Testwert stehen. Passiert nur in einem noch gar nicht
        # eingerichteten Bereich, aber dann still, und genau das nicht.
        warnung = ""
        if not an:
            zonenlage = next((l for l in lagen if l.zone_id == zone_id), None)
            raumlage = next((r for r in (zonenlage.raeume if zonenlage else [])
                             if r.raum_id == raum_id), None)
            if raumlage is not None and not raumlage.sendbar:
                warnung = (
                    f"Test beendet, aber es konnte nichts nachgeschickt "
                    f"werden ({raumlage.hinweis or raumlage.status}). Der "
                    f"Kreis bleibt auf dem Testwert stehen, bis wieder ein "
                    f"Sollwert gilt."
                )
                log.warning("Heizung: %s", warnung)
        return lagen, warnung

    @property
    def laufende_tests(self) -> dict[str, float]:
        """Fuer die Oberflaeche: alle laufenden Tests mit ihrem Ende."""
        return dict(self._test_bis)

    async def jetzt_senden(self, zone_id: str = "", raum_id: str = "") -> list[Zonenlage]:
        """
        Sendetakt zuruecksetzen und sofort einen Durchlauf fahren.

        Ohne Angabe gilt das fuer alles, sonst nur fuer den genannten
        Bereich bzw. Raum. Gedacht fuer den Knopf "Jetzt senden" — sonst
        muesste man bis zum naechsten Takt warten, um zu sehen, ob es geht.
        """
        for zone in self.speicher.einstellungen.zonen:
            if zone_id and zone.id != zone_id:
                continue
            for raum in zone.raeume:
                if raum_id and raum.id != raum_id:
                    continue
                for kreis in raum.heizkreise:
                    schluessel = f"{zone.id}|{kreis.device_id}|{kreis.channel_id}"
                    self._letztes_senden.pop(schluessel, None)
                    self._letzte_verschiebung.pop(schluessel, None)
        return await self.einmal_regeln()

    async def _vielleicht_senden(self, zone: Heizzone, lage: Zonenlage,
                                 ts: float) -> None:
        """
        Jeder Heizkreis wird einzeln bedient — er hat seinen eigenen Raumwert
        und damit sein eigenes Telegramm. Ist- und Sollwert reisen zusammen:
        ein A5-10-06 traegt beides plus Tag/Nacht.
        """
        if self.tx_router is None:
            return

        for raumlage in lage.raeume:
            for kreis in raumlage.kreise:
                if not kreis.sendbar:
                    continue
                schluessel = f"{zone.id}|{kreis.device_id}|{kreis.channel_id}"
                letzte = self._letztes_senden.get(schluessel, 0.0)
                alte = self._letzte_verschiebung.get(schluessel)
                geaendert = (alte is None
                             or abs((kreis.verschiebung_k or 0.0) - alte) > 0.01)
                faellig = (ts - letzte) >= zone.sende_intervall_s
                if not (geaendert or faellig):
                    continue

                befehl = {
                    "temperature": round(kreis.ist_c or 0.0, 1),
                    "setpoint_offset": round(kreis.verschiebung_k or 0.0, 2),
                    "offset_min": -kreis.verschiebung_max_k,
                    "offset_max": kreis.verschiebung_max_k,
                    # Thermokon bekommt die VERSCHIEBUNG (siehe oben), Eltako
                    # die Solltemperatur selbst. Wir schicken beides mit; der
                    # Encoder des jeweiligen Profils nimmt sich, was sein
                    # Geraet versteht (Eltako "Inhalte der Funktelegramme",
                    # FTR78S: Data_byte2 = Solltemperatur 8..30 °C).
                    "setpoint": round(kreis.basissollwert_c
                                      + (kreis.verschiebung_k or 0.0), 2),
                    "base": kreis.basissollwert_c,
                    # Tag/Nacht bleibt auf "Tag": die Absenkung machen wir
                    # selbst ueber die Zeitzonen. Wuerden wir zusaetzlich das
                    # Nacht-Bit setzen, zoege der Aktor seine eigene
                    # Nachtabsenkung (Werk 4 K) noch davon ab.
                    "day": True,
                }
                try:
                    await self.tx_router.handle_command(
                        kreis.device_id, kreis.channel_id, befehl
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("Heizung: Senden an %s/%s fehlgeschlagen: %s",
                                kreis.device_id, kreis.channel_id, exc)
                    continue

                self._letztes_senden[schluessel] = ts
                self._letzte_verschiebung[schluessel] = kreis.verschiebung_k or 0.0
                log.info("Heizung '%s' / %s / %s: Ist %.1f °C, Soll %.1f °C "
                         "(%s) → Verschiebung %+.1f K",
                         zone.anzeigename(), raumlage.name, kreis.name,
                         kreis.ist_c or 0.0, raumlage.sollwert_c or 0.0,
                         lage.fenster or lage.sollwert_quelle,
                         kreis.verschiebung_k or 0.0)

    async def _vielleicht_veroeffentlichen(self, zone: Heizzone,
                                           lage: Zonenlage) -> None:
        if self.publisher is None:
            return
        nutzlast = lage.als_dict()
        nutzlast["gesendet_vor_s"] = (
            None if zone.id not in self._letztes_senden
            else round(time.time() - self._letztes_senden[zone.id], 1)
        )
        # Nur bei Aenderung publizieren — der Takt laeuft alle 20 s, das wuerde
        # den Broker sonst mit unveraenderten Nachrichten fluten.
        vergleich = dict(nutzlast)
        vergleich.pop("gesendet_vor_s", None)
        vergleich.pop("ist_alter_s", None)
        if self._letzte_lage.get(zone.id) == vergleich:
            return
        self._letzte_lage[zone.id] = vergleich

        await self._publish(self.zonen_topic(zone.id), nutzlast)
        for f in zone.fenster:
            wert, quelle, _ = fenster_sollwert(f, self.istwerte)
            await self._publish(self.fenster_topic(zone.id, f.name), {
                "name": f.name,
                "sollwert_c": wert,
                "quelle": quelle,
                "von": f.von,
                "bis": f.bis,
                "tage": f.tage or list(WOCHENTAGE),
                "aktiv": lage.fenster == f.name,
            })
        if zone.restzeit.aktiv:
            wert, quelle, _ = fenster_sollwert(zone.restzeit, self.istwerte)
            await self._publish(
                self.fenster_topic(zone.id, zone.restzeit.name), {
                    "name": zone.restzeit.name,
                    "sollwert_c": wert,
                    "quelle": quelle,
                    "von": "", "bis": "", "tage": list(WOCHENTAGE),
                    "aktiv": lage.fenster == zone.restzeit.name,
                })

    async def _publish(self, topic: str, nutzlast: dict[str, Any],
                       retain: bool | None = None) -> None:
        if self.publisher is None:
            return
        try:
            await self.publisher.publish_json(topic, nutzlast, retain=retain)
        except Exception as exc:  # noqa: BLE001
            log.debug("Heizung: Publish '%s' fehlgeschlagen: %s", topic, exc)

    # ------------------------------------------------------------- Betrieb

    async def start(self) -> None:
        if self._task is not None:
            return
        self.gespeicherte_steller_uebernehmen()
        self._stop.clear()
        self._task = asyncio.create_task(self._schleife(), name="heizung")
        log.info("Heizungsregelung gestartet — %d Zone(n), Takt %d s",
                 len(self.speicher.einstellungen.zonen),
                 self.speicher.einstellungen.takt_s)

    async def _schleife(self) -> None:
        while not self._stop.is_set():
            try:
                await self.einmal_regeln()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("Heizungsregelung: Durchlauf fehlgeschlagen: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=max(5, self.speicher.einstellungen.takt_s),
                )
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        try:
            self.zustand.save()
        except Exception as exc:  # noqa: BLE001
            log.warning("Heizungs-Zustand beim Beenden nicht gesichert: %s", exc)

    async def abos_erneuern(self) -> None:
        """Nach dem Aendern der Zonen die MQTT-Abos nachziehen."""
        if self.publisher is None:
            return
        try:
            await self.publisher.subscribe_extra(self.abo_topics())
        except Exception as exc:  # noqa: BLE001
            log.warning("Heizung: Abos nicht erneuerbar: %s", exc)
