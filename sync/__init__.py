"""
Sync module for exporting spans from gate to reflect.

This module provides a background daemon that periodically exports
telemetry spans from gate's ring buffer to reflect's SQLite storage.
"""

from .daemon import SyncDaemon
from .config import SyncConfig

__all__ = ["SyncDaemon", "SyncConfig"]
