# MQTT-Anbindung — Werte lesen & Aktoren schalten

> Praxis-Anleitung: **welche MQTT-Topics** die Bridge anspricht und **wie man
> darüber Aktoren steuert** (Schalter, Dimmer, Rolladen, Heizregler, Taster).
> Gilt für die Timberwolf-Visu genauso wie für Home Assistant, Node-RED oder
> jeden anderen MQTT-Client.
>
> Quelle im Code: `app/mqtt_client.py` (Topics), `app/tx_router.py` +
> `app/encoders.py` (Senden), `app/eep/profiles.py` (Felder/Einheiten).

---

## 1. Kurz gesagt

- Die Bridge **empfängt** EnOcean-Funktelegramme und published sie als JSON auf
  `.../state`-Topics.
- Zum **Steuern** eines Aktors schickst du eine Nachricht auf das passende
  `.../set`-Topic. Die Bridge übersetzt sie in das richtige Funktelegramm.
- Alle Werte sind **JSON**. Bei Aktoren reicht oft auch eine **nackte Zahl** oder
  ein Wort wie `on`.

Der **Basis-Präfix** ist standardmäßig `enocean` (einstellbar in `gateways.yaml`
unter `mqtt.base_topic`). In dieser Anleitung steht überall `enocean` — falls du
den Präfix geändert hast, entsprechend ersetzen.

---

## 2. Die fünf Topic-Arten

| Topic | Richtung | Zweck |
|---|---|---|
| `enocean/gw/<gw>/<SENDER_ID>/state` | Bridge → dir | **Roh-Topic**, immer. Jedes Telegramm, unabhängig davon ob das Gerät bekannt ist. Für Diagnose & Anlernen. |
| `enocean/<etage>/<raum>/<gerät>/<kanal>/state` | Bridge → dir | **Named-Topic**, nur wenn das Gerät angelegt ist. Der lesbare, dekodierte Wert. |
| `enocean/<etage>/<raum>/<gerät>/<kanal>/set` | du → Bridge | **Befehl** an einen Aktor. |
| `enocean/_/status` | Bridge → dir | `online` / `offline` (retained). Zeigt, ob die Bridge läuft. |
| `enocean/_/teachin` | intern | Anlern-Modus über die Web-UI. |

### So entsteht der Named-Topic-Pfad

`etage` / `raum` / `gerät` / `kanal` werden **slugified**: klein, Umlaute
umgeschrieben (`ä→ae`, `ö→oe`, `ü→ue`, `ß→ss`, `&→und`), alles andere zu `_`.

Wichtige Regeln:

- **Leere Etage oder Raum fallen weg** — kein doppelter Slash. Beispiel:
  Gerät ohne Etage/Raum → `enocean/wohnzimmerlampe/state`.
- **Ein-Kanal-Geräte lassen das Kanal-Segment weg.** Ein Rolladen `Rollo Büro`
  wird zu `enocean/eg/buero/rollo_buero/state` — **nicht** `.../rollo_buero/rollo_buero/state`.
- **Nur Mehr-Kanal-Geräte** behalten das Kanal-Segment, um ihre Kanäle zu
  unterscheiden (z. B. ein Doppel-Schaltaktor: `.../aktor/kanal_1/state` und
  `.../aktor/kanal_2/state`).
- Ein Kanal kann Etage/Raum des Geräts **überschreiben** (wenn der Aktor woanders
  steht als das, was er schaltet).

> **Tipp:** Den exakten Topic-Namen musst du nicht raten — er steht in der
> Web-UI am jeweiligen Kanal, und du siehst ihn live, sobald das Gerät sendet.

---

## 3. Werte empfangen (Sensoren lesen)

Jedes bekannte Gerät published auf sein `.../state`-Topic ein JSON-Objekt.
Enthalten sind immer ein paar Metadaten plus die dekodierten Messwerte:

```json
{
  "ts": 1737540000.123,
  "device": "fwz14_kueche",
  "channel": "energie",
  "name": "Küche Energie",
  "rssi_dbm": -72,
  "gw": "gw_eg",
  "energy_kwh": 4213.7,
  "units": { "energy_kwh": "kWh" }
}
```

- `ts` = Zeitstempel, `rssi_dbm` = Empfangsstärke, `gw` = über welches Gateway.
- Die eigentlichen Messwerte hängen vom Gerätetyp (EEP) ab — siehe Tabelle unten.
- `units` gibt die Einheit je Feld an (kann pro Kanal überschrieben werden).
- **Split-Kanäle** (z. B. ein Zähler-Kanal, der nur „Energie" zeigt) publishen
  stattdessen kompakt `{"value": 4213.7, "unit": "kWh", ...}`.

### Häufige Sensor-Felder

| Gerätetyp (EEP) | Beispielgeräte | Wichtige Felder |
|---|---|---|
| `A5-02-xx` | Temperaturfühler | `temperature_c` |
| `A5-04-xx` | Temp + Feuchte | `temperature_c`, `humidity_pct` |
| `A5-07-xx` | Bewegungsmelder | `motion` (bool), `voltage_v` |
| `A5-10-02` | Raumbediengerät (thanos) | `temperature_c`, `setpoint_offset_c`, `fan_stage`, `occupancy` |
| `A5-10-03/06` | Raumbediengerät | `temperature_c`, `setpoint_offset_c`, `day` |
| `A5-12-01` | Stromzähler (FWZ14/DSZ14DRS) | `energy_kwh`, `current_w`, `tariff` (0=Tag/1=Nacht), `serial_number` |
| `A5-12-02` | Gaszähler | `volume_m3`, `flow_m3h` |
| `A5-12-03` | Wasserzähler | `volume_m3`, `flow_l_min` |
| `A5-13-01` | Wetterstation (FWS61) | `outdoor_temp_c`, `wind_speed_ms`, `rain`, `day`, `dawn_lux`, `sun_east/south/west_klx`, `sun_max_klx` |
| `F6-05-02` | Rauchmelder | `alarm` (bool), `battery_low` |
| `F6-10-00` | Fenstergriff | `state` = `closed`/`tilted`/`open` |
| `D5-00-01` | Fenster-/Türkontakt | `closed` (bool), `state` = `open`/`closed` |

> **Energiezähler-Schutz:** `energy_kwh` läuft nie zurück. Werte, die niedriger
> als der letzte Stand sind, eine erste `0`-Lesung oder unplausible Sprünge nach
> oben (> 1000 kWh, einstellbar) werden **verworfen** — sonst würden
> HA-Tageszähler Phantomverbräuche rechnen.

---

## 4. Aktoren schalten (das `.../set`-Topic)

Du schickst eine Nachricht auf das `.../set`-Topic des Kanals. Erlaubt ist:

- ein **JSON-Objekt** mit den passenden Schlüsseln (siehe unten), **oder**
- ein **nackter Wert** — eine Zahl (`30`) oder ein Wort (`on`, `off`). Die Bridge
  macht daraus automatisch den fachlich richtigen Befehl je Aktortyp.

„Wahr"-Werte sind tolerant: `true`, `1`, `on`, `an`, `ein`, `open`, `up` gelten
alle als EIN.

Welchen Aktortyp ein Kanal hat (Schalter/Dimmer/Rolladen/Ventil), ergibt sich aus
dem Produktkatalog bzw. dem EEP — du musst das nicht angeben.

### 4a. Schalter (Switch)

Geräte: Eltako FSR14/FSR61, OPUS BRiDGE (Schalt-Variante D2-01), FSSA-230V,
Standard-A5-38-08-Schaltaktoren.

```
Topic:   enocean/eg/kueche/steckdose/set
Payload: {"state": true}      → EIN
         {"state": false}     → AUS
```

Kurzform (nackter Wert):

```
on   / off
true / false
1    / 0
```

Bei **Mehr-Kanal-Aktoren** (z. B. OPUS BRiDGE 2-Kanal) den Kanal mitgeben:

```json
{"state": true, "io_channel": 1}
```

### 4b. Dimmer

Geräte: Eltako FUD14/FUD61/FLD61/FDG14, OPUS BRiDGE Dimmer (D2-01-0x),
A5-38-08-Dimmvarianten.

```
Topic:   enocean/og/flur/deckenlampe/set
Payload: {"dim": 40}                → auf 40 %
         {"dim": 40, "ramp": 5}     → auf 40 %, Dimmgeschwindigkeit 5
         {"state": true}            → EIN, auf den zuletzt gemerkten Wert
         {"state": false}           → AUS (Merkwert bleibt erhalten)
```

Kurzform: nackte Zahl `0…100` = Dimmwert; `on`/`off` = ein/aus.

- `ramp` ist bei Eltako ein **Geschwindigkeits-Code** (0 = im Dimmer eingestellt,
  1 = sehr schnell … 255 = sehr langsam), keine Sekundenangabe.
- **Warum `{"state": true}` beim Dimmer auf den Merkwert geht:** Eltako-Dimmer
  ignorieren einen reinen Schaltbefehl. Die Bridge merkt sich den letzten
  Dimmwert und fährt ihn beim „Ein" wieder an (0 → notfalls 100 %).

### 4c. Rolladen / Jalousie (Shutter)

Geräte: Eltako FSB14/FSB61NP (A5-3F-7F), OPUS BRiDGE Rollladen (D2-05).

Es gibt **ein** `.../set`-Topic pro Rolladen. Darüber laufen alle Befehle. Das
Schema ist auf die Timberwolf-Visu abgestimmt (**Richtung: 0 = Auf, 1 = Ab**):

```
Topic:   enocean/og/schlafzimmer/rollo/set

{"move": 0}     → volle Fahrt AUF (bis Endanschlag)
{"move": 1}     → volle Fahrt AB
{"step": 0}     → kurzer Tipp AUF   (Lamellenverstellung, ~0,4 s)
{"step": 1}     → kurzer Tipp AB
{"stop": true}  → Stopp
{"stop": false} → wird ignoriert (kein Befehl)
```

**Absolute Position** anfahren (0 = ganz offen/oben, 100 = ganz zu/unten):

```json
{"position": 60}
```

Kurzform: nackte Zahl `0…100` = Position.

> Für die absolute Position muss der Rolladen **eingefahren (kalibriert)** sein —
> die Bridge rechnet aus der gemessenen Laufzeit, wie lange sie fahren muss. Ist
> ein Aktor invers verdrahtet, lässt sich die Richtung pro Kanal umdrehen
> (`meta.invert_direction`).

**Rückmeldung** des Rolladens (auf dem gleichen `.../state`-Topic):

```json
{"position": 60, "moving_up": false, "moving_down": false, "calibrated": true}
```

### 4d. Heizregler / Ventil (Valve, A5-10-06 TX)

Die Bridge spielt gegenüber einem Heizregler (z. B. Thermokon STC-DO8) das
**Raumbedienteil**: sie sendet Ist-Temperatur, Sollwert-Korrektur und Tag/Nacht.

```
Topic:   enocean/eg/bad/heizung/set

{"temperature": 21.5, "setpoint_offset": 1.0, "day": true}
{"setpoint_offset": -2}        → Sollwert 2 K absenken
{"night": true}                → Nachtabsenkung (= day:false)
```

- `setpoint_offset` = Verschiebung in K gegenüber der Basistemperatur.
- `offset_min` / `offset_max` müssen zur Aktor-Konfiguration passen (Standard
  ±10 K) — stehen normalerweise schon in der Kanal-Konfig und müssen nicht
  mitgeschickt werden.

### 4d-2. Heizungsregelung (Wohnbereiche, Räume, Zeitzonen)

Statt einzelne Sollwert-Befehle zu schicken, kann die Brücke selbst regeln:
Ist-Werte aus beliebigen Topics, Sollwert nach Zeitplan, Ausgabe an die
Heizkreise. Angelegt wird das im Reiter **Heizung**.

```
heizung/<zone>/state                    # Zustand des Wohnbereichs (raus)
heizung/<zone>/set                      # {"sollwert_offset_k":0.5}, {"aktiv":false}
heizung/<zone>/fenster/<name>/state     # Sollwert dieser Zeitzone (raus)
heizung/<zone>/fenster/<name>/set       # {"sollwert_c":22.5} oder 22.5
```

Die Wurzel `heizung` steht **neben** dem Basis-Topic der Brücke, nicht
darunter: Der Timberwolf führt jede Topic-Wurzel als eigenes MQTT-Gerät.
Umstellbar im Reiter Heizung (`basis_topic`).

Der Zustand enthält je Wohnbereich die Räume und je Raum die Heizkreise:

```json
{"zone":"bereich1","name":"Bürobereich","status":"ok","sollwert_c":23.5,
 "sollwert_quelle":"fenster","fenster":"Bürostart",
 "raeume":[{"raum":"buero_1","name":"Büro 1","ist_c":23.2,"status":"ok",
            "kreise":[{"channel_id":"1.4","verschiebung_k":2.5,"sendbar":true}]}]}
```

`status` ist `ok`, `teilweise`, `inaktiv`, `kein_ziel`, `kein_fuehler`,
`kein_sollwert`, `kein_istwert`, `istwert_alt` oder `istwert_unplausibel`.

**Ohne brauchbaren Ist-Wert wird nichts gesendet** — ein ausgedachter Wert
wäre für den Regler nicht von einer Messung zu unterscheiden. Der Wert darf
als nackte Zahl (`21.4`) oder als JSON-Feld (`{"value":21.4}`, auch
verschachtelt über `sensor.temp`) ankommen und muss innerhalb der
Plausibilitätsgrenzen des Wohnbereichs liegen (Vorgabe 0…40 °C).

Ein Topic kann mehrere Werte tragen: Der Thanos liefert auf demselben Topic
`temperature_c` als Fühler und `setpoint_offset_c` als Sollwertrad. Der
Wertspeicher ist deshalb nach *(Topic, Feld)* geschlüsselt.

→ Vollständige Beschreibung inklusive Regelwerk, Fallstricke und
Inbetriebnahme: **[Heizungsregelung.md](Heizungsregelung.md)**

### 4e. Wippentaster senden (F6-02, PTM emulieren)

Die Bridge kann selbst einen Funktaster nachbilden (z. B. um einen fest
verknüpften Aktor direkt anzusprechen):

```
{"rocker": "AI", "pressed": true}     → Wippe A, Seite I, gedrückt
{"state": true}                        → als Druck interpretiert
```

Wippen-Codes: `AI` / `A0` / `BI` / `B0`.

---

## 5. Was der Taster-Eingang (F6-02) publisht

Ein empfangener Wippentaster meldet auf seinem `.../state`-Topic den Tastendruck.
Für flankenbasierte Logik (Timberwolf-„Triggered"-Baustein) gilt eine
**wichtige Besonderheit**:

- **Nur beim Druck** (nicht beim Loslassen) steht im Payload:
  - EIN-Seite: `"on": true` **und** `"taster_ein": true`
  - AUS-Seite: `"on": false` **und** `"taster_aus": true`
- Beim Loslassen steht **keins von beiden** — so löst eine Logik pro Druck genau
  einmal aus, und der AUS-Druck berührt nur `taster_aus`.

Welche Wippenhälfte „EIN" bedeutet, steuert die **Polung** `ptm_on_press`:
`I` = oben schaltet ein (Standard), `0` = unten. Global in den Einstellungen,
pro Kanal via `meta.ptm_on_press` überschreibbar.

Zusätzliche Felder: `event` (`A_top`/`A_bottom`/`B_top`/`B_bottom`/`release`),
`rocker_side` (`A`/`B`), `press_duration_ms` (Druckdauer beim Loslassen).

---

## 6. Vollständige Aktor-Referenz (Befehls-Schlüssel)

| Aktortyp | EEP(s) | Befehle auf `.../set` |
|---|---|---|
| **Schalter** | `ELTAKO-FSR`, `A5-38-08` (Switch), `D2-01-00/01/08` | `{"state": true/false}`, `on`/`off`, `{"io_channel": n}` |
| **Dimmer** | `A5-38-08` (Dim), `D2-01-03/09/0C/0D/12` | `{"dim": 0..100}`, `{"ramp": code}`, `{"state": ...}`, nackte Zahl |
| **Rolladen** | `A5-3F-7F`, `ELTAKO-FSB`, `D2-05-xx` | `{"move":0/1}`, `{"step":0/1}`, `{"stop":true}`, `{"position":0..100}` |
| **Heizregler** | `A5-10-06` (TX) | `{"temperature":c}`, `{"setpoint_offset":k}`, `{"day"/"night":bool}` |
| **Taster (TX)** | `F6-02-01/02` | `{"rocker":"AI/A0/BI/B0","pressed":true}` |

Wenn ein Aktor **nicht schaltet**, obwohl der Befehl ankommt, fehlt ihm meist die
**Sende-ID** (`active_sender_id` / gelernte Pair-ID) — die legst du im Edit-Dialog
des Kanals an und lernst sie am Aktor an.

---

## 7. Praxis-Beispiele (mosquitto_pub)

```bash
# Küchenlicht ein
mosquitto_pub -t "enocean/eg/kueche/licht/set" -m "on"

# Deckenlampe auf 30 %
mosquitto_pub -t "enocean/og/flur/deckenlampe/set" -m '{"dim": 30}'

# Rolladen Schlafzimmer komplett zu
mosquitto_pub -t "enocean/og/schlafzimmer/rollo/set" -m '{"move": 1}'

# Rolladen auf 50 %
mosquitto_pub -t "enocean/og/schlafzimmer/rollo/set" -m "50"

# Rolladen stoppen
mosquitto_pub -t "enocean/og/schlafzimmer/rollo/set" -m '{"stop": true}'

# Heizung Bad: Sollwert +1 K, Tag
mosquitto_pub -t "enocean/eg/bad/heizung/set" -m '{"setpoint_offset": 1, "day": true}'

# Alle Werte mitlesen
mosquitto_sub -t "enocean/#" -v
```

### Legacy-Format mit echten IDs

Falls du die IDs statt der Namen nutzen willst (z. B. für feste Automationen, die
sich nicht bei Umbenennung ändern sollen):

```
enocean/devices/<device_id>/<channel_id>/set
```

Dieses Format bleibt dauerhaft unterstützt.

---

## 8. Fallstricke & gut zu wissen

- **Retained:** `.../state` und `enocean/_/status` sind retained — ein neu
  verbundener Client bekommt sofort den letzten Stand.
- **QoS** ist standardmäßig 1.
- **Roh-Topic zum Anlernen:** Ein noch unbekanntes Gerät taucht nur unter
  `enocean/gw/<gw>/<SENDER_ID>/state` auf. Dort siehst du die Sender-ID, um das
  Gerät in der UI anzulegen.
- **Ein Sender, mehrere Werte:** Manche Geräte (z. B. Eltako-Zähler: kWh + W,
  Doppeltarif) tragen mehrere Kanäle unter einer Sender-ID. Die Bridge trennt sie
  über Feld- und Tarif-Filter — jeder Kanal bekommt sein eigenes `.../state`.
- **Rolladen-`state` gehört dem Aktor-Tracker:** Auf dem Rolladen-`.../state`
  steht die **Prozent-Position**, nicht die rohen Eltako-Fahrcodes.
- **Optimistischer Status:** Nach einem Schaltbefehl setzt die Bridge den Zustand
  sofort; ein späteres Rückmelde-Telegramm des Aktors korrigiert bei Bedarf.
- **OPUS BRiDGE / D2-Aktoren** werden **adressiert** angesprochen (Ziel = Aktor-ID).
  Damit das klappt, muss die Bridge zuvor per Remote-Management am Aktor
  eingetragen sein (Pairing über die Web-UI).
```

