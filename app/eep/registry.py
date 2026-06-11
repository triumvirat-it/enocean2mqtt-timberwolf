"""
EEP-Registry und zentraler Dispatcher.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from ..gateway.esp3 import RadioTelegram

log = logging.getLogger(__name__)

# Decoder-Signatur: (payload, status, rorg) -> dict
DecoderFn = Callable[[bytes, int, int], dict]


class EEPRegistry:
    def __init__(self) -> None:
        self._decoders: dict[str, DecoderFn] = {}
        # Mapping: RORG -> Default-Decoder wenn EEP unbekannt (raw bytes)
        self._raw_decoders: dict[int, DecoderFn] = {}

    def register(self, eep: str, fn: DecoderFn) -> None:
        eep_norm = eep.upper().replace("_", "-")
        if eep_norm in self._decoders:
            log.warning("EEP %s wird überschrieben", eep_norm)
        self._decoders[eep_norm] = fn

    def register_raw(self, rorg: int, fn: DecoderFn) -> None:
        self._raw_decoders[rorg] = fn

    def decode(self, eep: str | None, telegram: RadioTelegram) -> dict:
        """
        Versucht zuerst EEP-spezifisch, fällt auf RORG-Raw zurück.
        """
        if eep:
            eep_norm = eep.upper().replace("_", "-")
            fn = self._decoders.get(eep_norm)
            if fn:
                try:
                    decoded = fn(telegram.payload, telegram.status, telegram.rorg)
                    decoded["_eep"] = eep_norm
                    return decoded
                except Exception as exc:  # noqa: BLE001
                    log.warning("EEP %s Decode-Fehler: %s", eep_norm, exc)

        raw_fn = self._raw_decoders.get(telegram.rorg)
        if raw_fn:
            return raw_fn(telegram.payload, telegram.status, telegram.rorg)

        # Generischer Fallback
        return {
            "raw_hex": telegram.payload.hex(),
            "rorg": f"0x{telegram.rorg:02X}",
            "_eep": "UNKNOWN",
        }


_GLOBAL_REGISTRY = EEPRegistry()


def decode_telegram(eep: str | None, telegram: RadioTelegram) -> dict:
    return _GLOBAL_REGISTRY.decode(eep, telegram)


def register_default_profiles() -> EEPRegistry:
    from . import profiles  # noqa: F401 — Registrierung per Modul-Import

    profiles.register_all(_GLOBAL_REGISTRY)
    return _GLOBAL_REGISTRY
