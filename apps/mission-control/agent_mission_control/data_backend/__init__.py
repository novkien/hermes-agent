"""Bounded in-process Hermes data backend."""

from .config import Settings, SourceSpec, build_settings
from .compat import HttpDataBackend, LegacyDataBackendFacade, ParityComparator, ParityReport
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
    "HttpDataBackend",
    "LegacyDataBackendFacade",
    "LocalDataBackend",
    "ParityComparator",
    "ParityReport",
    "Settings",
    "SourceSpec",
    "build_settings",
]
