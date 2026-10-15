# Heizungsregelung

Die Brücke regelt Fußbodenheizungen selbst: Sie liest Raumtemperaturen aus
MQTT, bestimmt nach einem Zeitplan den Sollwert und spielt gegenüber dem
Heizregler das **Raumbedienteil** — sie sendet Ist-Temperatur und
Sollwertverschiebung als A5-10-06-Telegramm.

Bedient wird das im Reiter **Heizung**. Die Konfiguration liegt in
`heizzonen.yaml` unter `CONFIG_DIR`, der laufende Zustand in
`heizung_state.yaml`.

---

## Der Aufbau

```
Wohnbereich                      Klammer um Räume mit gemeinsamem Zeitplan
  ├── x Zeitzonen                Start/Ende/Wochentage/Sollwert
  ├── x Kontakte                 senken ALLE Räume ab
  └── x Räume
        ├── x Fühler             + Verrechnungsregel
        ├── 0..1 Sollwertrad     z. B. der Thanos an der Wand
        ├── x Kontakte           senken NUR diesen Raum ab
        └── x Heizkreise         je ein Ausgang des Reglers
```

Der **Raum** trägt alles Räumliche, der **Heizkreis** nur, was am Aktor hängt
(Basissollwert und Verschiebungsbereich des Ausgangs). Mehrere Heizkreise in
einem Raum teilen sich dessen Ist-Wert und bekommen je ein eigenes Telegramm
mit ihrer eigenen Sender-ID.

**Mehrere Fühler je Raum sind der Normalfall** — ein 1-Wire-Fühler in der
Decke und ein Raumbedienteil an der Wand, oder „Vorne" und „Hinten". Die
Verrechnung ist wählbar: `mittelwert` (gewichtet), `kleinster` (regelt nach
dem kältesten Punkt, der Raum wird sicher warm) oder `groesster` (sparsamer).
Fällt ein Fühler aus, fliegt er aus der Verrechnung und wird im Hinweis
genannt — sonst ändert sich ein Mittelwert still.

---

## So kommt der Sollwert zustande

```
Sollwert der aktiven Zeitzone
  + Korrektur des Wohnbereichs
  + Korrektur des Raums
  + Sollwertrad (begrenzt auf offset_grenze_k)
  − Basissollwert des Ausgangs
  = Verschiebung, begrenzt auf ±verschiebung_max_k
```

**Zeitzonen nur für Zeiten mit Heizbedarf anlegen.** Alles dazwischen —
Nacht, Wochenende, Urlaub — fällt auf den **Rückfall-Sollwert** des
Wohnbereichs (`restzeit`, fest oder aus der Visu wie eine Zeitzone). Ist er
ausgeschaltet, bleibt der zuletzt gültige Sollwert stehen.

Ist ein Tür-/Fensterkontakt offen oder steht ein Raum auf Dauerabsenkung,
gilt der Absenk-Sollwert: bevorzugt der einer benannten Zeitzone
(`absenkung_zeitzone`), sonst der Rückfall-Sollwert, zuletzt der laufende
Sollwert minus `absenkung_k`.

Die Uhrzeiten der Zeitzonen gelten in der **Zeitzone aus der Konfiguration**
(`zeitzone`, Vorgabe `Europe/Berlin`), nicht in der des Containers — der läuft
auf UTC, und der Plan läge im Sommer sonst zwei Stunden daneben.

---

## Betrieb: Korrekturen, Schalter, Absenkung

| | Wirkung |
|---|---|
| **Feste Korrektur** (`sollwert_offset_k` am Raum) | Dauerhafte Eigenschaft des Raums, z. B. Fühler hängt zu kalt oder Bad +2 K. Bleibt auch neben einem Sollwertrad stehen, nicht auf `offset_grenze_k` begrenzt. |
| **Verschiebung von Hand** (`handverschiebung_k`) | Das Sollwertrad in Software für Räume ohne Rad an der Wand. Gleiche Grenze wie das Rad; ein echtes Rad **ersetzt** sie, sobald es einen Wert liefert (nicht addiert). |
| **Dauerabsenkung** (`dauerabsenkung`) | Raum dauerhaft auf Absenk-Sollwert, z. B. selten genutzte Räume. Er funkt **weiter** — anders als `aktiv=false`, wo der Aktor nach 90 Minuten mit seiner Ersatz-Stellgröße einfriert. Optional aufgehoben durch einen Schalter (`dauerabsenkung_ausnahme_schalter`). |
| **Betriebsart-Schalter** (`schalter`) | Benannter Umstand, den der Mensch setzt, z. B. „Gäste da“. Zeitzonen können daran hängen (`nur_wenn_schalter`), Dauerabsenkungen können durch ihn aufgehoben werden. Umlegen im Reiter Heizung oder über MQTT; der Stand überlebt den Neustart. Zeigt eine Zeitzone auf einen gelöschten Schalter, gilt sie **nicht**. |
| **Aktortest** | Je Raum: volle Verschiebung, damit der Kreis aufmacht und man im Verteiler sieht, ob der Stellmotor läuft. Wirkt sofort, ohne Speichern, endet nach 15 Minuten von selbst. |

Räume lassen sich zwischen Wohnbereichen verschieben. Basissollwert und
Verschiebungsbereich ziehen dabei mit, weil sie zum Ausgang gehören, nicht
zum Bereich.

---

## Aktoren

Jeder Heizkreis ist ein Sendekanal der Bridge. Zwei Familien:

| Aktor | EEP | Was gesendet wird |
|---|---|---|
| Thermokon SRC-DO8 / STC-DO8 | A5-10-06 | Ist-Temperatur + **Verschiebung** über den im Aktor eingestellten Bereich |
| Eltako FHK61-230V (auch als OPUS GN-A-U230V-HKR-1) | A5-10-03 | Ist-Temperatur + **absolute** Solltemperatur 8…30 °C, DB0 = `0x0F` wie echte Raumbedienteile |

In der Auswahl der Heizkreise erscheint **jeder Heizungsaktor**, auch einer,
der nur als Empfangsgerät eingelesen wurde. Was ihm zum Senden fehlt, steht
daneben; „Einrichten“ macht die im Aktor angelernte Adresse zur Sende-ID und
bestimmt das Gateway aus dem ID-Block.

---

## Die vier Regeln, die zusammengehören

Sie sind aus dem Datenblatt und aus echten Vorfällen entstanden. Beim
Anfassen der Logik keine davon umgehen; jede ist mit Tests abgesichert
(`tests/test_heizung.py`).

**1. Nie einen erfundenen Ist-Wert senden.** Fehlt der Raumwert, ist er älter
als `ist_max_alter_s` oder liegt er außerhalb von `ist_min_c`…`ist_max_c`,
wird *nicht* gesendet. Der Regler kann eine ausgedachte Temperatur nicht von
einer gemessenen unterscheiden. Die Plausibilitätsgrenze ist nicht
theoretisch: Ein 1-Wire-Fühler lieferte 2153,12 statt 21,53 (fehlende
Kommastelle in der Quelle).

**2. Zyklisch senden.** Der SRC-DO8 erklärt einen Fühler nach **90 Minuten**
Funkstille für ausgefallen und fährt mit einer Ersatz-Stellgröße weiter.
Vorgabe `sende_intervall_s` = 300.

**3. Der Verschiebungsbereich muss dem Aktor entsprechen.** Das Telegramm
überträgt keinen Sollwert, sondern einen Anteil des *im Aktor* eingestellten
Bereichs. Am 23.09.2026 stand im Portal ±5 K, im Gerät ±8 K — aus gewollten
+2,5 K wurden +4,0 K, der Aktor regelte auf 25 statt 23,5 °C, ohne dass
irgendwo ein Fehler auftauchte. Gegenprobe: Die Heizkreis-Zeile zeigt
„W: x °C"; derselbe Wert muss am Gerät unter *Eff. Sollwert / Stellgröße*
stehen.

**4. Der Zustand überlebt den Neustart.** Der letzte Sollwert und die Stellung
der Sollwertsteller liegen in `heizung_state.yaml`. **Raum-Istwerte bewusst
nicht** — eine alte Raumtemperatur darf nach einem Neustart nicht als
aktuelle Messung durchgehen. Deshalb steht nach jedem Ausrollen kurz
„kein Ist-Wert", bis die Quelle das nächste Mal sendet.

---

## MQTT

Alle Werte kommen aus dem Broker; die Brücke fragt nichts ab, sie hört zu.
Die Wurzel steht **neben** dem Basis-Topic der Brücke (`basis_topic`, Vorgabe
`heizung`), weil der Timberwolf jede Topic-Wurzel als eigenes MQTT-Gerät führt.

```
heizung/<zone>/state                    # Zustand (raus, retained)
heizung/<zone>/set                      # {"sollwert_offset_k":0.5}, {"aktiv":false}
heizung/<zone>/fenster/<name>/state     # Sollwert dieser Zeitzone (raus)
heizung/<zone>/fenster/<name>/set       # {"sollwert_c":22.5} oder 22.5
heizung/schalter/<id>/state             # Stand eines Betriebsart-Schalters (raus, retained)
heizung/schalter/<id>/set               # Schalter umlegen
```

Ein Sollwertsteller der Visu kommt als **ein** JSON mit mehreren Feldern auf
**einem** Topic (`heizung/bereich1` → `{"TMPTagWohnen": 22, ...}`). Deshalb ist
der Wertspeicher nach *(Topic, Feld)* geschlüsselt — dasselbe Topic kann
gleichzeitig Ist-Wert und Sollwertrad liefern, wie beim Thanos
(`temperature_c` und `setpoint_offset_c`).

**Tipp:** Publish im Timberwolf auf *retained* stellen. Dann sind Sollwerte
und Fühlerwerte direkt nach einem Neustart da statt erst beim nächsten Zyklus.

---

## Schnittstellen

| | |
|---|---|
| `GET /api/heizung` | Konfiguration + Lage je Wohnbereich/Raum/Heizkreis |
| `PUT /api/heizung` | Konfiguration ersetzen (weist doppelte Zonen-IDs ab) |
| `POST /api/heizung/regeln` | Durchlauf ohne den Sendetakt anzufassen |
| `POST /api/heizung/senden?zone=&raum=` | Sendetakt zurücksetzen und sofort funken |
| `POST /api/heizung/schalter/{id}?an=` | Betriebsart-Schalter umlegen (ohne Speichern der Konfiguration) |
| `POST /api/heizung/test?zone=&raum=&an=&dauer_min=` | Aktortest an/aus (Vorgabe 15 Minuten) |
| `GET /api/heizung/ziele` | jeder Heizungsaktor, mit dem, was ihm zum Senden fehlt |
| `POST /api/heizung/ziele/einrichten?device_id=&channel_id=` | eingelesenen Aktor zum Sendekanal machen |

---

## Inbetriebnahme

1. Heizregler als Gerät mit A5-10-06- bzw. A5-10-03-Kanälen (Richtung `tx`)
   anlegen und je Kanal die im Aktor gelernte Sender-ID eintragen — oder einen
   eingelesenen Aktor in der Heizkreis-Auswahl über „Einrichten“ übernehmen.
2. Am Gerät ablesen und im Portal eintragen: **Basissollwert** und
   **Sollwertverschiebung** je Ausgang. Prüfen, dass der **Raumfühlertyp**
   nicht `SR0x` ist — dann setzt der Regler die Verschiebung fest auf 0 K.
3. Wohnbereich, Zeitzonen, Räume, Fühler und Heizkreise anlegen, speichern.
4. „Jetzt senden" drücken und am Gerät unter *Eff. Sollwert* gegenprüfen:
   `W:` muss dem Wert in der Heizkreis-Zeile entsprechen.
5. Je Raum den **Aktortest** fahren und im Verteiler nachsehen, ob der
   richtige Stellmotor aufmacht.
