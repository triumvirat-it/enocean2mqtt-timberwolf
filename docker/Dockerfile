# Multi-arch build: arm64 (Timberwolf 3500 = BCM2711) + amd64 (Entwicklung)
#
# Bau-Befehl auf Windows mit Docker Desktop + WSL2:
#   docker buildx build --platform linux/arm64 -f docker/Dockerfile \
#       --load -t enocean2mqtt-tw:m2 .
#
# Anschließend für Portainer-Import:
#   docker save enocean2mqtt-tw:m2 -o enocean2mqtt-tw_m2_arm64.tar
#
# WICHTIG — Base bleibt bei BULLSEYE (Debian 11, glibc 2.31), NICHT auf
# python:3.11-slim (= Debian Trixie, glibc 2.41) upgraden:
# Die neue glibc (>=2.34) nutzt den clone3-Syscall fuer Thread-Erzeugung.
# Das ALTE Docker auf dem Timberwolf (Portainer 1.17.1) hat ein seccomp-Profil,
# das clone3 mit EPERM blockt -> "can't start new thread". Die asyncio-App
# laeuft single-threaded, aber paho-mqtt braucht einen Thread -> MQTT wuerde
# stumm sterben. glibc 2.31 (bullseye) nutzt noch clone -> seccomp erlaubt es.
FROM python:3.11-slim-bullseye AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_PROGRESS_BAR=off \
    PIP_NO_COLOR=1 \
    PIP_ROOT_USER_ACTION=ignore \
    CONFIG_DIR=/data

WORKDIR /app

# nano + curl: Editieren der Config via Portainer-Console + Healthcheck
# Bewusst kein apt-get install: Timberwolf-Host hat zu wenig RAM
# fuer dpkg-Decompression von Debian-Trixie-Paketen ("Cannot allocate memory").
# Coreutils sind im python:3.11-slim Base bereits enthalten.
# Editor: User editiert /data/gateways.yaml via 'cat > file <<EOF' Heredoc
# in der Portainer-Console (Web-UI mit Editor kommt mit M4).

COPY requirements.txt .
# --progress-bar off als Fallback (falls ENV-Var nicht greift)
# --use-deprecated=legacy-resolver: einfacherer (single-threaded) Resolver,
# umgeht Thread-Limits auf BCM2711 (RasPi 4)
RUN pip install \
        --no-cache-dir \
        --progress-bar off \
        --no-color \
        -r requirements.txt

# Anwendungs-Code
COPY app/ ./app/

# Templates für Erstkonfig (werden vom Entrypoint nach /data kopiert wenn leer)
COPY data/gateways.example.yaml /app/data-template/gateways.example.yaml
COPY data/devices.example.yaml /app/data-template/devices.example.yaml

# Entrypoint
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Non-Root-User
RUN useradd --create-home --uid 1000 enocean \
    && mkdir -p /data \
    && chown -R enocean:enocean /app /data
USER enocean

VOLUME ["/data"]
EXPOSE 8080

# Simpler Healthcheck ohne procps: prueft via /proc dass PID 1 lebt.
# Echter HTTP-Healthcheck kommt mit der Web-UI (M4).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD test -d /proc/1 || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
