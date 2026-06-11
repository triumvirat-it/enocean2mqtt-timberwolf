# Deploy auf den Timberwolf via Portainer

Da der Timberwolf keinen SSH-Zugang am Host bietet, läuft das Deployment über **Portainer**.
Dieses Dokument beschreibt Schritt für Schritt, wie du das Image lokal baust und dann auf den Timberwolf bringst.

---

## Voraussetzungen — einmalig

### 1. Docker Desktop auf Windows installieren

1. Download: <https://www.docker.com/products/docker-desktop/>
2. Installer ausführen (Admin-Rechte erforderlich)
3. Im Installer: **„Use WSL 2 instead of Hyper-V"** angekreuzt lassen
4. Nach Restart: Docker Desktop einmal starten → akzeptieren → "Use recommended settings"
5. Im Tray-Icon Rechtsklick → Settings → **General** → „Use containerd for pulling and storing images" aktivieren (Multi-Arch-Support)
6. Settings → **Resources → WSL Integration**: Default-Distro aktivieren
7. PowerShell öffnen, Check:
   ```powershell
   docker --version
   docker buildx ls
   ```
   `buildx` muss da sein und mindestens den Builder `default` zeigen.

### 2. ARM64-Emulation einmalig aktivieren

Auf Windows-PC (amd64) bauen wir ein arm64-Image — dafür braucht's QEMU-Emulation:

```powershell
docker run --privileged --rm tonistiigi/binfmt --install all
```

Das ist ein einmaliger Setup-Schritt. Danach kannst du arm64-Images bauen.

---

## Bei jedem Build / Update

### 1. Image bauen

In PowerShell ins Projektverzeichnis wechseln:
```powershell
cd C:\xampp-8.2.12\xampp\htdocs\enocean2mqtt_timberwolf
```

Multi-Arch-Build für arm64 starten (--load lädt es ins lokale Docker):
```powershell
docker buildx build `
    --platform linux/arm64 `
    -f docker/Dockerfile `
    --load `
    -t enocean2mqtt-tw:m2 `
    .
```

Dauer: 2-5 min (erstes Mal länger wegen Layer-Download).

### 2. Als TAR exportieren

```powershell
docker save enocean2mqtt-tw:m2 -o enocean2mqtt-tw_m2_arm64.tar
```

Ergibt eine ~150-250 MB große `.tar`-Datei im Projektverzeichnis.

### 3. In Portainer hochladen

1. Portainer öffnen: <https://192.168.1.59/proxy/portainer/>
2. Linkes Menü → **Images**
3. Oben rechts → **Import** (oder „Upload" — je nach Portainer-Version)
4. **Choose File** → `enocean2mqtt-tw_m2_arm64.tar`
5. **Upload** — kann 1-2 min dauern (~150 MB übers Netz)
6. Nach Upload erscheint `enocean2mqtt-tw:m2` in der Image-Liste

### 4. Volume anlegen (nur beim allerersten Mal)

1. Linkes Menü → **Volumes**
2. **+ Add volume**
3. Name: `enocean2mqtt-data`
4. Driver: `local`
5. **Create the volume**

### 5. Container erstellen

1. Linkes Menü → **Containers** → **+ Add container**
2. **Name:** `enocean2mqtt`
3. **Image:** `enocean2mqtt-tw:m2` (manuell eintragen — KEIN Pull, lokales Image)
4. **Always pull the image:** OFF (sehr wichtig — sonst sucht er online)
5. **Network ports configuration:** *publish a new network port*
   - host `8080` → container `8080`
6. Unten **Advanced container settings**:
   - **Volumes** → Tab „Volumes" → **map additional volume**
     - container: `/data`
     - volume: `enocean2mqtt-data`
   - **Network** → Network: `bridge` (Default)
   - **Restart policy** → `Unless stopped`
   - **Env** → kein Env nötig (Default `CONFIG_DIR=/data` ist gesetzt)
7. Oben: **Deploy the container**

### 6. Container startet — Erstkonfig anpassen

Der Entrypoint legt automatisch `gateways.yaml` und `devices.yaml` aus den Templates an.
Aber die Werte sind Beispiele — du musst sie anpassen:

1. Container-Liste → Container `enocean2mqtt`
2. Quick action `>_` (das Konsolen-Icon) anklicken
3. Command: `/bin/sh` → Connect
4. Im Container:
   ```sh
   nano /data/gateways.yaml
   ```
5. **IP-Adressen** deiner LAN-GWs eintragen, MQTT-Host bestätigen
6. Speichern mit `Ctrl+O` → Enter → `Ctrl+X`
7. Console verlassen
8. **Container neu starten:** in der Container-Übersicht → Restart

### 7. Logs prüfen

Container-Übersicht → Quick action **Logs** (Schreibblock-Icon)

Du solltest sehen:
```
... INFO enocean2mqtt | enocean2mqtt_timberwolf startet — 1 Gateway(s), MQTT 192.168.1.59:1883
... INFO app.gateway.lan_tcp | [LAN Gateway1] verbinde mit 192.168.1.219:5000
... INFO app.gateway.lan_tcp | [LAN Gateway1] verbunden
... INFO enocean2mqtt.mqtt_client | MQTT verbunden: 192.168.1.59:1883
... INFO enocean2mqtt | Bereit — warte auf Telegramme
```

### 8. Test — PTM-Taster drücken

Drücke einen EnOcean-Funktaster in der Nähe deines GW.
In den Logs solltest du sehen:
```
... INFO app.gateway.lan_tcp | [LAN Gateway1] RX RPS id=00350001 rssi=-67 dBm payload=30
... INFO enocean2mqtt.pipeline | Neuer/unbekannter Sender 00350001 ...
```

### 9. MQTT-Topics in Timberwolf-Objektsystem prüfen

Im Timberwolf-WebUI → **MQTT** → Topic-Browser
Du solltest Topics sehen:
```
enocean/_/status              → "online"
enocean/gw/lan_gateway1/00350001/state    → JSON mit pressed, rocker_1, rssi etc.
```

Diese kannst du jetzt in dein **Objektsystem** ziehen — Topic auswählen, JSON-Path
(z.B. `$.pressed` oder `$.temperature_c`) auf ein Objekt mappen.

---

## Typische Probleme

| Problem | Lösung |
|---|---|
| `buildx build` schlägt fehl mit „no matching manifest" | `docker run --privileged --rm tonistiigi/binfmt --install all` ausführen |
| Image-Upload in Portainer hängt | Browser-Tab nicht schließen, kann bei 150 MB einen Moment dauern. Falls länger als 5 min: Netzwerk-Verbindung prüfen. |
| Container startet, aber connected nicht zum GW | Logs prüfen — `Verbindungsfehler`-Zeilen. Eventuell ist eine andere Anwendung noch mit dem Gateway verbunden (nur eine darf gleichzeitig). |
| MQTT-Verbindung schlägt fehl | Im MQTT-Broker-Container Quick-Action `>_` → `cat /mosquitto/config/mosquitto.conf` — prüfen ob `allow_anonymous true` und `listener 1883 0.0.0.0` gesetzt ist |
| Keine Telegramme im Topic, obwohl Logs RX zeigen | base_topic im MQTT-Topic-Browser im Timberwolf-UI eventuell falsch — schau unter `enocean/#` |

---

## Update-Workflow

Wenn ich eine neue Version baue (M3, M4, ...):

1. Code-Änderungen pullen / aktualisieren
2. Schritte 1-2 oben wiederholen → neue TAR
3. In Portainer: alten Container **Remove** (Volume `enocean2mqtt-data` bleibt!)
4. Image-Liste → altes `enocean2mqtt-tw:m2` löschen
5. Neue TAR importieren
6. Container neu erstellen mit neuem Image-Tag

Die Konfig in `/data` bleibt erhalten durch das Volume.
