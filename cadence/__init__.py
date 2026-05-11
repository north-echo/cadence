"""CADENCE: Container Supply Chain Patch Latency Measurement."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ne-cadence")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
