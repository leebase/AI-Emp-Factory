"""The least-privilege Agent Board seam for one ordinary employee instance."""

from .client import (
    BoardClient,
    BoardError,
    ConfigError,
    HTTPError,
    TransportError,
    UsageError,
)

Client = BoardClient

__all__ = [
    "BoardClient",
    "Client",
    "BoardError",
    "ConfigError",
    "HTTPError",
    "TransportError",
    "UsageError",
]
