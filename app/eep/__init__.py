"""
EEP-Decoder / Encoder.

Wir nutzen ein eigenes leichtgewichtiges Plug-in-System statt der `enocean`-Lib,
weil:
  - asyncio-native, kein Threading
  - Eltako-Spezialitäten (FSB Rolladen, FSR Schaltbefehl) brauchen wir individuell
  - klare Output-Struktur für MQTT-Topics

Jeder Profile-Decoder gibt ein dict zurück, das direkt als JSON published wird.
"""
from .profile import EEPProfile, EEPProfileRegistry, FieldDef
from .registry import EEPRegistry, decode_telegram, register_default_profiles

_PROFILE_REGISTRY: EEPProfileRegistry | None = None


def get_profile_registry() -> EEPProfileRegistry:
    """
    Singleton-Zugriff auf alle bekannten EEPProfiles.
    Wird lazy initialisiert — registriert beim ersten Aufruf alle Profile.
    """
    global _PROFILE_REGISTRY
    if _PROFILE_REGISTRY is None:
        from . import profiles  # noqa: WPS433 — lazy import

        reg = EEPProfileRegistry()
        profiles.register_profiles(reg)
        _PROFILE_REGISTRY = reg
    return _PROFILE_REGISTRY


def get_profile(eep_id: str | None) -> EEPProfile | None:
    """Schnellzugriff auf ein einzelnes Profile."""
    return get_profile_registry().get(eep_id)


__all__ = [
    "EEPProfile",
    "EEPProfileRegistry",
    "EEPRegistry",
    "FieldDef",
    "decode_telegram",
    "get_profile",
    "get_profile_registry",
    "register_default_profiles",
]
