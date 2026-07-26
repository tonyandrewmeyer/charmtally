"""charmtally — a feature-adoption survey of the Canonical charm fleet."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("charmtally")
except PackageNotFoundError:  # pragma: no cover — running from a bare checkout
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
