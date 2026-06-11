#!/bin/sh
# Initialisiert das /data-Volume mit Default-Configs beim ersten Start.
# Pflegt bestehende User-Configs (überschreibt nichts).
set -e

TEMPLATE_DIR="/app/data-template"
DATA_DIR="${CONFIG_DIR:-/data}"

mkdir -p "$DATA_DIR"

for tmpl in "$TEMPLATE_DIR"/*.example.yaml; do
    [ -f "$tmpl" ] || continue
    base=$(basename "$tmpl" .example.yaml)
    target="$DATA_DIR/$base.yaml"
    template_target="$DATA_DIR/$base.example.yaml"

    # Beispiel-Vorlage immer bereitstellen (überschreiben, falls Image aktualisiert)
    cp "$tmpl" "$template_target"

    # Echte Konfig nur anlegen, wenn noch keine da ist
    if [ ! -f "$target" ]; then
        cp "$tmpl" "$target"
        echo "[entrypoint] Erstkonfig erstellt: $target — bitte anpassen!"
    fi
done

exec python -m app.main
