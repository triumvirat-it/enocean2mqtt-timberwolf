"""Helper-Skript fuer den lokalen Headless-UI-Test (CONFIG_DIR=test-data, Web-UI auf :8080)."""
import os
import sys

os.environ.setdefault("CONFIG_DIR", "test-data")
os.environ.setdefault("LOG_LEVEL", "INFO")
# Webui-Port aus test-data/gateways.yaml ist 8080
from app.main import main
main()
