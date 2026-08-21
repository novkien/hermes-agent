"""Bounded in-process Hermes data backend."""

from .config import Settings, SourceSpec, build_settings
from .dispatch import read_call
from .local import LocalDataBackend
from .protocol import (
    BackendHealth,
    BackendResult,
    DataBackend,
    DataBackendError,
    JsonObject,
)

__all__ = [
    "BackendHealth",
    "BackendResult",
    "DataBackend",
    "DataBackendError",
    "JsonObject",
    "LocalDataBackend",
    "read_call",
    "Settings",
    "SourceSpec",
    "build_settings",
]
